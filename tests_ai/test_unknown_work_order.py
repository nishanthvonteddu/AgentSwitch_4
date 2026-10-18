"""record_finding refuses a finding about a different order when the one asked about does not exist.

WRITTEN WITH CLAUDE (AI-assisted). Ungraded; the team's hand-written tests live in tests/.

On the 2026-09-30 second full run, z-ai/glm-5.3-flash was asked about WO-2026-09999, which does not exist. Every
lookup said found=false, and it then diagnosed WO-2026-00099 as the "closest match" and recorded that order's
causes, so refuse_unknown_work_order scored revise. These tests drive the loop with scripted model replies.

Run: python -m pytest tests_ai/test_unknown_work_order.py -q
"""
import json
from types import SimpleNamespace

import pytest

from prod_agent import config, domain
from prod_agent.agent import ProductionAgent

REQUEST = "WO-2026-09999 is late. Find out why, tell me what it blocks downstream, and reschedule what you can."


@pytest.fixture
def persisted(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    # A found order's diagnosis carries the cause finding() names: record_finding removes a cause code its order's
    # diagnosis does not contain (live 2026-10-18), and these tests are about the subject, not the causes.
    monkeypatch.setattr(domain, "diagnose", lambda mcp, ref: {"found": False, "ref": ref} if ref.endswith("09999")
                        else {"found": True, "work_order": {"number": ref}, "signals": [
                            {"code": "engineering_change_pending", "blocking": True, "record": None}]})
    rows = []
    monkeypatch.setattr(domain, "record_finding", lambda mcp, run_id, finding: rows.append(finding) or {"run_id": run_id})
    return rows


def call(call_id, name, arguments):
    fn = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    return SimpleNamespace(id=call_id, type="function", function=fn)


def message(content=None, tool_calls=()):
    tool_calls = list(tool_calls)

    def dump(**_kwargs):
        return {"role": "assistant", "content": content, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in tool_calls]}
    return SimpleNamespace(content=content, tool_calls=tool_calls, model_dump=dump)


class Scripted:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.sent.append(kwargs["messages"])
        reply = self.replies.pop(0) if self.replies else message(content="done")
        return SimpleNamespace(choices=[SimpleNamespace(message=reply)])


def finding(work_order, **over):
    base = {"outcome": "answered", "work_order": work_order, "is_late": True, "currency": "INR",
            "blocking_causes": ["engineering_change_pending"], "contributing_causes": [],
            "evidence_records": [work_order], "potentially_blocked_work_orders": [], "blocked_sales_orders": [],
            "rescheduled": [], "not_visible": [], "refusal_reason": None}
    return {**base, **over}


REFUSAL = finding("WO-2026-09999", outcome="refused", is_late=None, blocking_causes=[], evidence_records=[],
                  refusal_reason="WO-2026-09999 does not exist")


def run(replies, request=REQUEST):
    llm = Scripted(replies)
    agent = ProductionAgent(SimpleNamespace(session=SimpleNamespace(instance="suryodaya")), llm=llm, model="m")
    return agent, agent.run(request), llm


def test_the_closest_match_finding_is_refused_and_the_refusal_is_kept(persisted):
    _, out, llm = run([
        message(tool_calls=[call("c1", "diagnose_work_order", {"work_order": "WO-2026-09999"})]),
        message(tool_calls=[call("c2", "diagnose_work_order", {"work_order": "WO-2026-00099"})]),
        message(tool_calls=[call("c3", "record_finding", finding("WO-2026-00099", outcome="partial"))]),
        message(tool_calls=[call("c4", "record_finding", REFUSAL)]),
        message(content="WO-2026-09999 does not exist."),
    ])
    assert persisted == [REFUSAL]
    rejection = next(json.loads(m["content"]) for m in llm.sent[-1] if m.get("tool_call_id") == "c3")
    assert "WO-2026-09999" in rejection["error"]
    assert out["stop_reason"] == "final_answer"


def test_a_refusal_without_a_work_order_is_accepted(persisted):
    refusal = {**REFUSAL, "work_order": None}
    run([
        message(tool_calls=[call("c1", "diagnose_work_order", {"work_order": "WO-2026-09999"})]),
        message(tool_calls=[call("c2", "record_finding", refusal)]),
    ])
    assert persisted == [refusal]


def test_an_order_that_exists_is_recorded_as_usual(persisted):
    ok = finding("WO-2026-00049")
    run([
        message(tool_calls=[call("c1", "diagnose_work_order", {"work_order": "WO-2026-00049"})]),
        message(tool_calls=[call("c2", "record_finding", ok)]),
    ], request="Why is WO-2026-00049 late?")
    assert persisted == [ok]


def test_a_mistyped_lookup_does_not_block_a_question_that_named_no_order(persisted):
    """'Most overdue' names no order, so a wrong ref the model tried along the way is not a substitution."""
    ok = finding("WO-2026-00047")
    # The order is looked up before it is recorded, as a real run does: the loop's subject guard refuses a finding
    # about an order the run never examined, which is a different check from the one under test here.
    run([
        message(tool_calls=[call("c1", "diagnose_work_order", {"work_order": "WO-2026-09999"})]),
        message(tool_calls=[call("c3", "diagnose_work_order", {"work_order": "WO-2026-00047"})]),
        message(tool_calls=[call("c2", "record_finding", ok)]),
    ], request="Which open work order is the most overdue, and why?")
    assert persisted == [ok]
