"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-10-15).

These are NOT the team's hand-written tests and must not be claimed as such: the course scores
AI-written tests at zero.

What run_one does when a task does not go to plan, with no tenant, network or model: Session, McpClient,
RestClient and the agent are fakes.
- An escalation the agent raised is withdrawn however the task ends: a harness error, an exception out of
  agent.run() itself, or Ctrl-C (which is then re-raised). Before, a harness error dropped the result that
  listed it, and a real person stayed assigned to a test.
- A fixture with no honest target today means no agent run and an unevaluated verdict saying so, not a
  KeyError on the prompt's placeholder.
- A dropped server-clock read falls back to the local clock instead of killing the task.
- A crash after the agent started keeps its whole token limit charged; one before it hands it back.

Run: python -m pytest tests_ai/test_harness_run_one.py -q
"""
import http.client
import io
import json
from types import SimpleNamespace

import pytest

from harness import runner
from harness.results import ResultsBook

ESCALATION = {"raised": True, "escalation_id": "esc-1", "number": "ESC-0001"}


class FakeMcp:
    def __init__(self):
        self.calls = []
        self.session = SimpleNamespace(instance="suryodaya", base="https://tenant.invalid")

    def call(self, name, args):
        self.calls.append((name, args))
        return {"result": {"ok": True}}


class FakeAgent:
    """Stands in for ProductionAgent: raises one escalation, then does whatever the test asks of run()."""
    built = []
    behaviour = None

    def __init__(self, mcp, **kwargs):
        self.kwargs = kwargs
        self.escalations, self.session_id = [], None
        self.tokens = {"prompt": 0, "completion": 0, "total": 0}
        self.steps = 0
        FakeAgent.built.append(self)

    def run(self, prompt):
        self.prompt = prompt
        self.kwargs["trace"]({"type": "start"})
        self.session_id = "sess-1"
        self.escalations.append(dict(ESCALATION))
        self.tokens = {"prompt": 6000, "completion": 1000, "total": 7000}
        self.steps = 2
        return FakeAgent.behaviour(self)


def _finished(agent):
    return {"run_id": "r1", "stop_reason": "final_answer", "escalations": agent.escalations,
            "agent_session_id": agent.session_id, "steps": 2, "tokens": dict(agent.tokens), "seconds": 1.0}


@pytest.fixture
def fakes(monkeypatch):
    mcp = FakeMcp()
    FakeAgent.built, FakeAgent.behaviour = [], _finished
    monkeypatch.setattr(runner, "Session", lambda instance: SimpleNamespace(instance=instance, base="https://x.invalid"))
    monkeypatch.setattr(runner, "McpClient", lambda session: mcp)
    monkeypatch.setattr(runner, "RestClient", lambda session: SimpleNamespace(session=session))
    monkeypatch.setattr(runner, "capture_context",
                        lambda rest, task: {"me": "u1", "started_at_utc": "2026-10-15T00:00:00", "snapshot": {}})
    monkeypatch.setattr(runner, "ProductionAgent", FakeAgent)
    return mcp


TASK = {"id": "escalate_x", "mode": "dry_run", "escalate": True, "prompt": "WO-1 is locked; escalate it."}


def _withdrawn(mcp):
    return [a["escalation_id"] for name, a in mcp.calls if name == "endpoint.agent_governance.escalations.update"]


def _closed(mcp):
    return [a["id"] for name, a in mcp.calls if name == "AgentSession.close.active.closed"]


def _trace_file(agent) -> io.TextIOWrapper:
    """The trace file run_one opened, reached through the trace callback it handed the agent."""
    [handle] = [c.cell_contents for c in agent.kwargs["trace"].__closure__
                if isinstance(c.cell_contents, io.TextIOWrapper)]
    return handle


# ----------------------------------------------------------------------------- escalations are always withdrawn

def test_an_escalation_is_withdrawn_after_a_normal_verdict(fakes, tmp_path):
    rec = runner.run_one(TASK, "suryodaya", tmp_path)

    assert rec["reason"] == "no verifier declared"
    assert _withdrawn(fakes) == ["esc-1"] and _closed(fakes) == ["sess-1"]


@pytest.mark.parametrize("error", [RuntimeError("tools/list failed"), json.JSONDecodeError("bad", "x", 0)])
def test_an_escalation_is_withdrawn_when_the_agent_raises(fakes, tmp_path, error):
    """An exception out of agent.run() itself, as McpError from tool_names() in _invoke_batch does."""
    def boom(agent):
        raise error
    FakeAgent.behaviour = boom

    rec = runner.run_one(TASK, "suryodaya", tmp_path)

    run_dir = tmp_path / "suryodaya" / TASK["id"]
    assert rec["stop_reason"] == "harness_error" and rec["verdict"] == "unevaluated"
    assert _withdrawn(fakes) == ["esc-1"], "a real person must not stay assigned to a test escalation"
    assert _closed(fakes) == ["sess-1"]
    cleanup = json.loads((run_dir / "cleanup.json").read_text(encoding="utf-8"))
    assert cleanup["withdrawn"] == ["ESC-0001"] and cleanup["session_closed"] == "sess-1"
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert result["escalations"] == [ESCALATION], "the harness_error result keeps what was raised, on disk"
    assert _trace_file(FakeAgent.built[0]).closed


def test_ctrl_c_still_withdraws_closes_the_trace_and_is_re_raised(fakes, tmp_path):
    def interrupted(agent):
        raise KeyboardInterrupt
    FakeAgent.behaviour = interrupted

    with pytest.raises(KeyboardInterrupt):
        runner.run_one(TASK, "suryodaya", tmp_path)

    run_dir = tmp_path / "suryodaya" / TASK["id"]
    assert _withdrawn(fakes) == ["esc-1"] and _closed(fakes) == ["sess-1"]
    assert json.loads((run_dir / "cleanup.json").read_text(encoding="utf-8"))["withdrawn"] == ["ESC-0001"]
    assert _trace_file(FakeAgent.built[0]).closed


def test_an_escalation_is_withdrawn_when_the_verifier_step_itself_fails(fakes, tmp_path, monkeypatch):
    """persist_then_verify catches a raising verifier, but anything after the agent (here the verdict write)
    can still fail; the escalation must not depend on reaching the end of run_one."""
    real_write = runner._write

    def failing_verdict(path, data):
        if path.name == "verdict.json":
            raise OSError("disk full")
        real_write(path, data)
    monkeypatch.setattr(runner, "_write", failing_verdict)

    with pytest.raises(OSError, match="disk full"):
        runner.run_one(TASK, "suryodaya", tmp_path)

    assert _withdrawn(fakes) == ["esc-1"]


def test_a_task_that_never_escalated_writes_no_cleanup(fakes, tmp_path):
    def quiet(agent):
        agent.escalations.clear()
        agent.session_id = None
        return _finished(agent)
    FakeAgent.behaviour = quiet

    runner.run_one(TASK, "suryodaya", tmp_path)

    assert fakes.calls == []
    assert not (tmp_path / "suryodaya" / TASK["id"] / "cleanup.json").exists()


# ----------------------------------------------------------------------------- a fixture with no target today

FIXTURE_TASK = {"id": "not_late_x", "mode": "dry_run", "fixture": "fx", "verifier": "harness.verifiers.team04:x",
                "prompt": "{target_number} is late. Why?"}


@pytest.fixture
def no_verifier_or_agent(monkeypatch):
    monkeypatch.setattr(runner, "_load_verifier", lambda ref: pytest.fail("the verifier ran on a skipped task"))
    monkeypatch.setattr(runner, "ProductionAgent", lambda *a, **k: pytest.fail("the agent ran without a target"))


@pytest.mark.parametrize("empty", [{}, None])
def test_no_target_today_is_unevaluated_without_running_the_agent(fakes, no_verifier_or_agent, tmp_path,
                                                                  monkeypatch, empty):
    monkeypatch.setitem(runner.FIXTURES, "fx", lambda mcp: empty)

    rec = runner.run_one(FIXTURE_TASK, "suryodaya", tmp_path)

    run_dir = tmp_path / "suryodaya" / FIXTURE_TASK["id"]
    assert rec["verdict"] == "unevaluated"
    assert rec["reason"] == "fixture found no target on this instance today"
    assert rec["stop_reason"] == "no_target" and rec["tokens"]["total"] == 0
    for name in ("task.json", "fixture.json", "result.json", "verdict.json"):
        assert (run_dir / name).exists(), name
    assert (run_dir / "result.json").stat().st_mtime <= (run_dir / "verdict.json").stat().st_mtime
    assert not (run_dir / "trace.jsonl").exists()


def test_a_fixture_missing_a_field_the_prompt_needs_names_it(fakes, no_verifier_or_agent, tmp_path, monkeypatch):
    monkeypatch.setitem(runner.FIXTURES, "fx", lambda mcp: {"target": {"id": "1", "status": "stopped"}})

    rec = runner.run_one(FIXTURE_TASK, "suryodaya", tmp_path)

    assert rec["verdict"] == "unevaluated" and rec["stop_reason"] == "no_target"
    assert "{target_number}" in rec["reason"]


def test_a_fixture_with_a_target_still_runs_the_agent_on_it(fakes, tmp_path, monkeypatch):
    monkeypatch.setitem(runner.FIXTURES, "fx", lambda mcp: {"target": {"id": "1", "number": "WO-2026-00007"}})
    monkeypatch.setattr(runner, "_load_verifier", lambda ref: lambda ctx: (runner.Verdict.APPROVE, "ok"))

    rec = runner.run_one(FIXTURE_TASK, "suryodaya", tmp_path)

    assert FakeAgent.built[0].prompt == "WO-2026-00007 is late. Why?"
    assert rec["verdict"] == "approve"


# ----------------------------------------------------------------------------- the server clock

@pytest.mark.parametrize("error", [http.client.RemoteDisconnected("closed"), ConnectionResetError(54, "reset"),
                                   TimeoutError("timed out"), OSError("unreachable"),
                                   http.client.BadStatusLine("garbage")])
def test_a_dropped_clock_read_falls_back_to_the_local_clock(monkeypatch, error):
    def urlopen(*args, **kwargs):
        raise error
    monkeypatch.setattr(runner.urllib.request, "urlopen", urlopen)
    rest = SimpleNamespace(session=SimpleNamespace(base="https://tenant.invalid"), raw=lambda path: {"id": "u1"})

    context = runner.capture_context(rest, {"id": "t"})

    assert context["me"] == "u1" and context["started_at_utc"]


# ----------------------------------------------------------------------------- tokens after a crash

def test_a_crash_after_the_agent_started_leaves_its_spend_unknown(fakes, tmp_path):
    def boom(agent):
        raise RuntimeError("mid-step")
    FakeAgent.behaviour = boom

    rec = runner.run_one(TASK, "suryodaya", tmp_path)

    assert rec["tokens"] is None
    result = json.loads((tmp_path / "suryodaya" / TASK["id"] / "result.json").read_text(encoding="utf-8"))
    assert result["tokens_counted"]["total"] == 7000, "what was counted is still on file"


def test_a_crash_before_the_agent_existed_spent_nothing(fakes, tmp_path, monkeypatch):
    monkeypatch.setitem(runner.FIXTURES, "fx", lambda mcp: (_ for _ in ()).throw(RuntimeError("no BOM chain")))

    rec = runner.run_one({**FIXTURE_TASK, "id": "fx_crash"}, "suryodaya", tmp_path)

    assert rec["stop_reason"] == "harness_error" and rec["tokens"]["total"] == 0


def _record(task, instance, tokens):
    return {"task_id": task["id"], "instance": instance, "verdict": "approve", "reason": "ok", "sample": False,
            "run_id": "r", "seconds": 1.0, "steps": 1, "tokens": tokens, "stop_reason": "final_answer",
            "llm_error_code": None}


@pytest.mark.parametrize("crash", ["unknown_tokens", "run_one_raised"])
def test_a_task_whose_spend_is_unknown_keeps_its_whole_limit_charged(monkeypatch, tmp_path, crash):
    def run_one(task, instance, root, **kw):
        if task["id"] == "a":
            if crash == "run_one_raised":
                raise RuntimeError("verdict write failed after the agent ran")
            return {**_record(task, instance, None), "verdict": "unevaluated", "stop_reason": "harness_error"}
        return _record(task, instance, {"total": 10_000})

    monkeypatch.setattr(runner, "run_one", run_one)
    book = ResultsBook(tmp_path / "results.json", "suryodaya")
    plan = [({"id": i, "mode": "dry_run"}, "suryodaya") for i in ("a", "b", "c")]

    records = runner.execute(plan, tmp_path, book, budget=None, token_budget=60_000)

    # a keeps its 30k; b holds 30k and returns 20k (held 40k); c's 30k no longer fits in 60k.
    assert [r["task_id"] for r in records] == ["a", "b"]
    assert book.doc()["tasks"][2]["evidence"] == "not run: token budget"


def test_a_task_known_to_have_spent_nothing_hands_its_limit_back(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "run_one", lambda task, instance, root, **kw:
                        _record(task, instance, {"total": 0} if task["id"] == "a" else {"total": 10_000}))
    book = ResultsBook(tmp_path / "results.json", "suryodaya")
    plan = [({"id": i, "mode": "dry_run"}, "suryodaya") for i in ("a", "b", "c")]

    records = runner.execute(plan, tmp_path, book, budget=None, token_budget=60_000)

    assert [r["task_id"] for r in records] == ["a", "b", "c"]
