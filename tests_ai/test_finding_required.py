"""With require_finding (harness and CLI), a reply before record_finding is sent back once, and forced.

WRITTEN WITH CLAUDE (AI-assisted). Ungraded; the team's hand-written tests live in tests/.

On the 2026-09-30 third full run, z-ai/glm-5.3-flash refused to cancel a completed order correctly on keystone,
but replied without calling record_finding. Verifiers grade only the finding, so refuse_cancel_wo28 scored revise.

Run: python -m pytest tests_ai/test_finding_required.py -q
"""
import json
from types import SimpleNamespace

import pytest

from prod_agent import config, domain
from prod_agent.agent import ProductionAgent

REFUSAL = {"outcome": "refused", "work_order": "WO-2026-00028", "is_late": None, "currency": "USD",
           "blocking_causes": [], "contributing_causes": [], "evidence_records": ["WO-2026-00028"],
           "potentially_blocked_work_orders": [], "blocked_sales_orders": [], "rescheduled": [], "not_visible": [],
           "refusal_reason": "WO-2026-00028 is completed and cancel is not available to this seat"}


@pytest.fixture
def persisted(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    rows = []
    monkeypatch.setattr(domain, "record_finding", lambda mcp, run_id, finding: rows.append(finding) or {"run_id": run_id})
    return rows


def message(content=None, tool_calls=()):
    tool_calls = list(tool_calls)

    def dump(**_kwargs):
        return {"role": "assistant", "content": content, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in tool_calls]}
    return SimpleNamespace(content=content, tool_calls=tool_calls, model_dump=dump)


def record(finding):
    fn = SimpleNamespace(name="record_finding", arguments=json.dumps(finding))
    return message(tool_calls=[SimpleNamespace(id="rf", type="function", function=fn)])


class Scripted:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append({k: v for k, v in kwargs.items() if k != "messages"})
        reply = self.replies.pop(0) if self.replies else message(content="done")
        return SimpleNamespace(choices=[SimpleNamespace(message=reply)])


def run(replies, max_steps=20, require=True):
    llm, events = Scripted(replies), []
    agent = ProductionAgent(SimpleNamespace(session=SimpleNamespace(instance="keystone")), llm=llm, model="m",
                            trace=events.append, max_steps=max_steps, require_finding=require)
    return agent.run("The customer withdrew. Cancel WO-2026-00028 now."), llm, events


def test_an_early_reply_is_sent_back_and_the_finding_is_forced(persisted):
    out, llm, events = run([message(content="I can't cancel it."), record(REFUSAL), message(content="Refused.")])
    assert persisted == [REFUSAL]
    assert llm.calls[1]["tool_choice"] == {"type": "function", "function": {"name": "record_finding"}}
    assert [e["step"] for e in events if e["type"] == "finding_missing"] == [0]
    assert out["stop_reason"] == "final_answer" and out["final_answer"] == "Refused."


def test_it_is_sent_back_only_once(persisted):
    out, llm, _ = run([message(content="No."), message(content="Still no.")])
    assert persisted == [] and len(llm.calls) == 2
    assert out["final_answer"] == "Still no."


def test_a_reply_after_the_finding_ends_the_run_at_once(persisted):
    out, llm, events = run([record(REFUSAL), message(content="Refused.")])
    assert len(llm.calls) == 2 and not [e for e in events if e["type"] == "finding_missing"]
    assert out["final_answer"] == "Refused."


def test_without_require_finding_the_first_reply_ends_the_run(persisted):
    """The bare loop's contract (tests/test_production.py test_30) is unchanged."""
    out, llm, events = run([message(content="hi")], require=False)
    assert len(llm.calls) == 1 and [e["type"] for e in events] == ["start", "llm", "end"]


def _scripted_agent(module, monkeypatch, replies):
    """Swap the transport and the model under the real ProductionAgent the module builds; keep everything else."""
    built = []

    class WithScriptedModel(ProductionAgent):
        def __init__(self, mcp, **kwargs):
            built.append(kwargs)
            super().__init__(mcp, llm=Scripted(replies), model="m", **kwargs)

    monkeypatch.setattr(module, "ProductionAgent", WithScriptedModel)
    monkeypatch.setattr(module, "Session", lambda instance: SimpleNamespace(instance=instance))
    monkeypatch.setattr(module, "McpClient", lambda session: SimpleNamespace(session=session))
    return built


def test_the_harness_turns_it_on(persisted, monkeypatch, tmp_path):
    """Through harness.runner.run_one: an early prose reply is sent back and the refusal still reaches the database."""
    from harness import runner

    built = _scripted_agent(runner, monkeypatch, [message(content="I can't cancel it."), record(REFUSAL),
                                                  message(content="Refused.")])
    monkeypatch.setattr(runner, "RestClient", lambda session: None)
    monkeypatch.setattr(runner, "capture_context", lambda rest, task: {"snapshot": {}})
    task = {"id": "refuse_cancel_wo28", "prompt": "The customer withdrew. Cancel WO-2026-00028 now."}

    record_ = runner.run_one(task, "keystone", tmp_path)

    assert built[0]["require_finding"] is True
    assert persisted == [REFUSAL]
    assert record_["stop_reason"] == "final_answer"
    events = [json.loads(line) for line in (tmp_path / "keystone" / task["id"] / "trace.jsonl").read_text().splitlines()]
    assert [e["step"] for e in events if e["type"] == "finding_missing"] == [0]


def test_the_cli_turns_it_on(persisted, monkeypatch, tmp_path, capsys):
    """Through python -m prod_agent's main(): the same send-back, with the result written where --run-dir says."""
    import sys

    from prod_agent import __main__ as cli

    built = _scripted_agent(cli, monkeypatch, [message(content="I can't cancel it."), record(REFUSAL),
                                               message(content="Refused.")])
    monkeypatch.setattr(sys, "argv", ["prod_agent", "The customer withdrew. Cancel WO-2026-00028 now.",
                                      "--instance", "keystone", "--run-dir", str(tmp_path / "ask")])

    cli.main()

    assert built[0]["require_finding"] is True
    assert persisted == [REFUSAL]
    assert json.loads((tmp_path / "ask" / "result.json").read_text())["finding"] == REFUSAL
    assert capsys.readouterr().out.startswith("Refused.")
