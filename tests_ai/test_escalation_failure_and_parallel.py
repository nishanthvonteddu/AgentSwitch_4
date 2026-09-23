"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-09-22).

These are NOT the team's hand-written tests and must not be claimed as such: the course scores
AI-written tests at zero. They cover two changes:

  1. a throwing `escalate` must still leave a finding in the database (the run used to end empty);
  2. independent reads in one tool-call batch run together, while writes stay serial and in order.

Run: python -m pytest tests_ai/test_escalation_failure_and_parallel.py -q
"""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from prod_agent import config
from prod_agent.agent import ProductionAgent
from prod_agent.mcp_client import McpClient, McpError, Session


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)
    for name in ("LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_MODEL", "OPENROUTER_API_KEY", "OPENROUTER_MODEL"):
        monkeypatch.delenv(name, raising=False)


class FakeToolCall:
    def __init__(self, name, arguments, call_id="call-1"):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=json.dumps(arguments))

    def model_dump(self, **kwargs):
        return {"id": self.id, "type": "function",
                "function": {"name": self.function.name, "arguments": self.function.arguments}}


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, **kwargs):
        return {"role": "assistant", "content": self.content,
                "tool_calls": [c.model_dump() for c in self.tool_calls]}


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=next(self.responses))])


def finding_args():
    return {"outcome": "partial", "work_order": "WO-2026-00001", "is_late": True, "currency": "INR",
            "blocking_causes": ["material_shortage"], "evidence_records": ["WO-2026-00001"],
            "potentially_blocked_work_orders": [], "blocked_sales_orders": [], "rescheduled": [],
            "not_visible": [], "refusal_reason": None}


def escalate_args():
    return {"work_order": "WO-2026-00001", "reason": "locked order, a person must act",
            "reason_code": "policy_refusal"}


def mcp_that_fails(failing_tool):
    """Fake client whose `failing_tool` raises, so escalation blows up mid-way."""
    seen = []

    def call(name, args=None):
        seen.append(name)
        if name == failing_tool:
            raise McpError("tool_error", f"{failing_tool} exploded")
        if name == "AgentSession.create":
            return {"id": "sess-1"}
        if name == "AgentMemory.create":
            return {"id": "mem-1"}
        return {}

    return SimpleNamespace(session=SimpleNamespace(instance="suryodaya"), call=call,
                           tool_names=lambda: set()), seen


# --------------------------------------------------------------- 1. escalation failure

@pytest.mark.parametrize("failing_tool", ["AgentSession.create",
                                          "endpoint.agent_governance.escalations.assignees"])
def test_failed_escalation_still_records_the_finding(failing_tool):
    """The bug: escalate raised, escalations stayed empty, record_finding's guard then refused, run ended empty."""
    mcp, seen = mcp_that_fails(failing_tool)
    llm = ScriptedLLM([
        FakeMessage(tool_calls=[FakeToolCall("escalate", escalate_args())]),
        FakeMessage(tool_calls=[FakeToolCall("record_finding", finding_args(), "call-2")]),
        FakeMessage(content="done"),
    ])
    agent = ProductionAgent(mcp, llm=llm, model="gpt-4.1", escalate_mode=True, max_steps=6)
    agent.needs_person = ["WO-2026-00001: dates are locked after submit"]

    result = agent.run("reschedule this order")

    assert result["finding"] == finding_args(), "the run must not end with nothing filed"
    assert result["finding_record"] == {"agent_memory_id": "mem-1", "run_id": agent.run_id}
    assert "AgentMemory.create" in seen
    assert len(result["escalations"]) == 1
    failure = result["escalations"][0]
    assert failure["raised"] is False
    assert failure["reason_code"] == "escalation_failed"
    assert failing_tool in failure["detail"]


def test_failed_escalation_is_traced_as_an_exception():
    mcp, _ = mcp_that_fails("AgentSession.create")
    llm = ScriptedLLM([
        FakeMessage(tool_calls=[FakeToolCall("escalate", escalate_args())]),
        FakeMessage(content="done"),
    ])
    events = []
    agent = ProductionAgent(mcp, llm=llm, model="gpt-4.1", escalate_mode=True, trace=events.append, max_steps=6)

    agent.run("reschedule this order")

    exceptions = [e for e in events if e["type"] == "exception"]
    assert [e["tool"] for e in exceptions] == ["escalate"]
    assert "McpError" in exceptions[0]["traceback"]


def test_failed_escalation_does_not_burn_the_wrap_up_retry_into_a_loop():
    """escalation_due goes false once a failure is recorded, so wrap-up forces record_finding, not escalate again."""
    mcp, _ = mcp_that_fails("AgentSession.create")
    agent = ProductionAgent(mcp, llm=object(), model="gpt-4.1", escalate_mode=True, max_steps=20)
    agent.needs_person = ["WO-2026-00001: locked"]

    assert agent._wrap_up_choice(17)["function"]["name"] == "escalate"
    agent._dispatch("escalate", escalate_args())
    assert agent._wrap_up_choice(17) is None
    assert agent._wrap_up_choice(18)["function"]["name"] == "record_finding"


def test_successful_escalation_is_unchanged():
    mcp, seen = mcp_that_fails("nothing-fails")

    def call(name, args=None):
        seen.append(name)
        if name == "AgentSession.create":
            return {"id": "sess-1"}
        if name == "endpoint.agent_governance.escalations.assignees":
            return {"options": [{"id": "party-1"}]}
        if name == "endpoint.agent_governance.escalations.raise":
            return {"ok": True, "escalation": {"id": "esc-1", "number": "ESC-1", "status": "open"}}
        return {"id": "mem-1"}

    mcp.call = call
    agent = ProductionAgent(mcp, llm=object(), model="gpt-4.1", escalate_mode=True)

    result = agent._dispatch("escalate", escalate_args())

    assert result["raised"] is True
    assert result["escalation_id"] == "esc-1"
    assert agent.escalations == [result]


# --------------------------------------------------------------- 2. parallel tool calls

class ConcurrencyProbe:
    """Stands in for _dispatch and records how many calls were ever in flight at once."""

    def __init__(self, delay=0.05):
        self.delay = delay
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.overlapped_with = {}

    def __call__(self, name, args):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.overlapped_with[name] = max(self.overlapped_with.get(name, 0), self.active)
        time.sleep(self.delay)
        with self._lock:
            self.active -= 1
        return {"tool": name}


def batch_agent(probe):
    mcp = SimpleNamespace(session=SimpleNamespace(instance="suryodaya"),
                          call=lambda *a, **k: {}, tool_names=lambda: set())
    agent = ProductionAgent(mcp, llm=object(), model="gpt-4.1")
    agent._dispatch = probe
    return agent


def run_batch(agent, names):
    calls = [FakeToolCall(n, {}, f"call-{i}") for i, n in enumerate(names)]
    return calls, agent._invoke_batch(calls, [None] * len(calls))


def test_independent_reads_run_together():
    probe = ConcurrencyProbe()
    agent = batch_agent(probe)
    names = ["company_context", "diagnose_work_order", "downstream_impact", "propose_reschedule"]

    started = time.time()
    _, results = run_batch(agent, names)
    elapsed = time.time() - started

    assert probe.peak == 4, "all four independent reads should have been in flight at once"
    assert elapsed < probe.delay * len(names), "the batch still ran sequentially"
    assert [r[0]["tool"] for r in results] == names


def test_writes_never_overlap_anything():
    probe = ConcurrencyProbe()
    agent = batch_agent(probe)
    names = ["diagnose_work_order", "downstream_impact", "record_finding", "seat_entities"]

    calls, results = run_batch(agent, names)

    assert probe.overlapped_with["record_finding"] == 1, "record_finding must run alone"
    assert probe.peak == 2, "only the two adjacent reads should overlap"
    assert [r[0]["tool"] for r in results] == names
    assert [c.id for c in calls] == ["call-0", "call-1", "call-2", "call-3"]


def test_results_keep_the_order_the_model_asked_for():
    agent = batch_agent(lambda name, args: {"tool": name})
    names = ["record_finding", "diagnose_work_order", "downstream_impact", "escalate", "company_context"]

    _, results = run_batch(agent, names)

    assert [r[0]["tool"] for r in results] == names
    assert all(error is None and tb is None for _, error, _, tb in results)


def test_one_failing_call_does_not_sink_its_batch():
    def dispatch(name, args):
        if name == "downstream_impact":
            raise McpError(-32000, "nope", {"code": "permission_denied"})
        if name == "seat_entities":
            raise ValueError("tool bug")
        return {"tool": name}

    agent = batch_agent(dispatch)
    _, results = run_batch(agent, ["diagnose_work_order", "downstream_impact", "seat_entities"])

    assert results[0][0] == {"tool": "diagnose_work_order"} and results[0][1] is None
    assert results[1][1] == "permission_denied" and results[1][3] is None
    assert results[2][1] == "exception" and "ValueError: tool bug" in results[2][3]


def test_repeat_guard_still_fires_inside_one_batch():
    """The guard runs serially before dispatch, so a duplicate in the same batch is still caught."""
    probe = ConcurrencyProbe(delay=0)
    agent = batch_agent(probe)
    args = {"work_order": "WO-2026-00001"}
    calls = [FakeToolCall("diagnose_work_order", args, "call-0"),
             FakeToolCall("diagnose_work_order", args, "call-1")]
    notes = [agent._repeat_note(c.function.name, args, 0) for c in calls]

    results = agent._invoke_batch(calls, notes)

    assert results[0][0] == {"tool": "diagnose_work_order"}
    assert results[1][0]["repeat"] is True


# --------------------------------------------------------------- 3. shared client under threads

def test_concurrent_rpc_ids_are_unique(monkeypatch):
    """One McpClient is shared by the whole batch, so its request counter must not hand out duplicates."""
    seen, lock = [], threading.Lock()

    def fake_http(url, body=None, token=None, **kwargs):
        with lock:
            seen.append(body["id"])
        return 200, {"jsonrpc": "2.0", "id": body["id"], "result": {}}

    monkeypatch.setattr("prod_agent.mcp_client._http", fake_http)
    session = Session.__new__(Session)
    session.base, session.token, session.instance = "http://x", "t", "suryodaya"
    session._lock = threading.Lock()
    client = McpClient(session)

    with_pool = [threading.Thread(target=lambda: client._rpc("tools/list")) for _ in range(50)]
    for t in with_pool:
        t.start()
    for t in with_pool:
        t.join()

    assert len(seen) == 51, "50 threaded calls plus the initialize from the constructor"
    assert len(set(seen)) == len(seen), "duplicate JSON-RPC ids were handed out under concurrency"


# --------------------------------------------------------------- 4. bounded reads and reuse

class PagingClient:
    """Serves `rows` newest-first, counting pages so a test can prove the read stopped early."""

    def __init__(self, rows):
        self.rows = rows
        self.pages = 0
        self.filters = []

    def call(self, name, args=None):
        args = args or {}
        self.pages += 1
        self.filters.append({k: v for k, v in args.items() if k not in ("limit", "offset")})
        rows = self.rows
        if args.get("reason"):
            rows = [r for r in rows if r.get("reason") == args["reason"]]
        rows = sorted(rows, key=lambda r: r["from_time"], reverse=args.get("sort_order") == "desc")
        offset, limit = args.get("offset", 0), args.get("limit", 200)
        return {"data": rows[offset:offset + limit], "total": len(rows)}


def test_list_window_stops_at_the_window_edge():
    rows = [{"id": f"d{i}", "from_time": f"2026-{m:02d}-01"} for i, m in enumerate(range(1, 10))]
    client = McpClient.__new__(McpClient)
    client.call = PagingClient(rows).call
    client._tools = {}

    got = client.list_window("DowntimeEntry", "from_time", "2026-07-01", page=2)

    assert [r["from_time"] for r in got] == ["2026-07-01", "2026-08-01", "2026-09-01"]
    assert len(got) < len(rows), "the whole table was paged anyway"


def test_list_window_pushes_the_equality_filter_to_the_server():
    rows = [{"id": "a", "from_time": "2026-09-01", "reason": "breakdown"},
            {"id": "b", "from_time": "2026-09-02", "reason": "tool_change"}]
    paging = PagingClient(rows)
    client = McpClient.__new__(McpClient)
    client.call, client._tools = paging.call, {}

    got = client.list_window("DowntimeEntry", "from_time", "2026-01-01", reason="breakdown")

    assert [r["id"] for r in got] == ["a"]
    assert paging.filters[0]["reason"] == "breakdown"
    assert paging.filters[0]["sort_by"] == "from_time" and paging.filters[0]["sort_order"] == "desc"


def test_propose_reschedule_reuses_handed_in_reads(monkeypatch):
    from prod_agent import domain

    refetched = []
    monkeypatch.setattr(domain, "diagnose", lambda *a, **k: refetched.append("diagnose") or {"found": False})
    monkeypatch.setattr(domain, "downstream_impact", lambda *a, **k: refetched.append("downstream") or {})

    diag = {"found": True, "work_order": {"id": "w1", "number": "WO-1", "status": "in_progress",
                                          "planned_start_date": "2026-09-01", "planned_end_date": "2026-09-05",
                                          "updated_at": "t0"},
            "signals": [], "schedule": {}}
    down = {"potentially_blocked_work_orders": []}

    out = domain.propose_reschedule(object(), "WO-1", diag=diag, down=down)

    assert refetched == [], "propose_reschedule refetched work it was handed"
    assert out["found"] is True and out["work_order"]["number"] == "WO-1"


def test_agent_hands_cached_reads_to_propose_reschedule(monkeypatch):
    from prod_agent import domain

    seen = {}
    monkeypatch.setattr(domain, "diagnose", lambda mcp, ref: {"found": True, "tag": "diag"})
    monkeypatch.setattr(domain, "downstream_impact", lambda mcp, ref: {"found": True, "tag": "down"})
    monkeypatch.setattr(domain, "propose_reschedule",
                        lambda mcp, ref, diag=None, down=None: seen.update(diag=diag, down=down) or {"proposals": []})

    agent = batch_agent(None)
    del agent._dispatch  # use the real one
    agent._dispatch("diagnose_work_order", {"work_order": "WO-1"})
    agent._dispatch("downstream_impact", {"work_order": "WO-1"})
    agent._dispatch("propose_reschedule", {"work_order": "WO-1"})

    assert seen["diag"] == {"found": True, "tag": "diag"}
    assert seen["down"] == {"found": True, "tag": "down"}


def test_agent_does_not_reuse_a_read_for_a_different_work_order(monkeypatch):
    from prod_agent import domain

    seen = {}
    monkeypatch.setattr(domain, "diagnose", lambda mcp, ref: {"found": True, "ref": ref})
    monkeypatch.setattr(domain, "propose_reschedule",
                        lambda mcp, ref, diag=None, down=None: seen.update(diag=diag) or {"proposals": []})

    agent = batch_agent(None)
    del agent._dispatch
    agent._dispatch("diagnose_work_order", {"work_order": "WO-1"})
    agent._dispatch("propose_reschedule", {"work_order": "WO-2"})

    assert seen["diag"] is None, "a cached diagnosis leaked across work orders"


# --------------------------------------------------------------- 5. incomplete findings

def test_a_finding_missing_outcome_is_refused_not_persisted():
    """Seen live 2026-09-22: gemini left outcome=null on a refusal, the agent saved it, the task scored revise."""
    mcp, seen = mcp_that_fails("nothing-fails")
    incomplete = {**finding_args(), "outcome": None}
    llm = ScriptedLLM([
        FakeMessage(tool_calls=[FakeToolCall("record_finding", incomplete)]),
        FakeMessage(tool_calls=[FakeToolCall("record_finding", finding_args(), "call-2")]),
        FakeMessage(content="done"),
    ])
    agent = ProductionAgent(mcp, llm=llm, model="gpt-4.1", max_steps=6)

    result = agent.run("push the delivery date out two weeks")

    assert result["finding"] == finding_args(), "the retry, not the incomplete first attempt, must be stored"
    assert seen.count("AgentMemory.create") == 1, "the incomplete finding must never reach the database"


def test_nulled_finding_names_the_fields_for_the_model():
    mcp, _ = mcp_that_fails("nothing-fails")
    agent = ProductionAgent(mcp, llm=object(), model="gpt-4.1")

    out = agent._dispatch("record_finding", {**finding_args(), "outcome": None, "not_visible": None})

    assert out["fields"] == ["outcome", "not_visible"]
    assert agent.finding is None and agent.finding_record is None


def test_a_partial_finding_is_still_accepted():
    """tests/test_production.py records {"outcome": "refused"} alone: omitting a field stays the caller's choice."""
    mcp, _ = mcp_that_fails("nothing-fails")
    agent = ProductionAgent(mcp, llm=object(), model="gpt-4.1")

    out = agent._dispatch("record_finding", {"outcome": "refused"})

    assert "error" not in out
    assert agent.finding == {"outcome": "refused"}


def test_fields_the_schema_allows_to_be_null_are_not_flagged():
    from prod_agent.agent import _nulled_required

    nullable = {**finding_args(), "work_order": None, "is_late": None, "refusal_reason": None, "currency": None}

    assert _nulled_required(nullable) == []
    assert _nulled_required({**finding_args(), "outcome": None}) == ["outcome"]
    assert _nulled_required({"outcome": "refused"}) == [], "an omitted field is not a nulled one"


def test_guard_follows_the_schema_rather_than_a_hardcoded_list(monkeypatch):
    """A field added to the schema is covered without touching the guard."""
    from prod_agent import agent as agent_module

    schema = {"properties": {**agent_module.RECORD_FINDING_SCHEMA["properties"],
                             "new_field": {"type": "string"}},
              "required": [*agent_module.RECORD_FINDING_SCHEMA["required"], "new_field"]}
    monkeypatch.setattr(agent_module, "RECORD_FINDING_SCHEMA", schema)

    assert agent_module._nulled_required({**finding_args(), "new_field": None}) == ["new_field"]


def test_list_window_falls_back_when_the_server_ignores_sort_order():
    """B14: an invalid sort_order is silently served as asc. Early-stopping on ascending rows returns nothing."""
    rows = [{"id": f"d{i}", "from_time": f"2026-{m:02d}-01"} for i, m in enumerate(range(1, 10))]

    class AscendingAlways(PagingClient):
        def call(self, name, args=None):
            args = dict(args or {})
            args["sort_order"] = "asc"  # the server ignores what we asked for
            return super().call(name, args)

    paging = AscendingAlways(rows)
    client = McpClient.__new__(McpClient)
    client.call, client._tools = paging.call, {}

    got = client.list_window("DowntimeEntry", "from_time", "2026-07-01", page=2)

    assert [r["from_time"] for r in got] == ["2026-07-01", "2026-08-01", "2026-09-01"], \
        "the window must still be correct when the sort is ignored"
