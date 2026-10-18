"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-10-12).

These are NOT the team's hand-written tests and must not be claimed as such: the course scores
AI-written tests at zero.

Local rehearsal on the platform's model, 2026-10-12 (deepseek/deepseek-v4.1-flash through OpenRouter,
platform mode): keystone_why_late_wo3 scored revise with "no finding recorded in AgentMemory". At step 3
wrap-up forced record_finding by name; the model answered in prose instead ("I can't write the
reschedule, so this needs a person. Let me escalate.") and called nothing, and a reply without a tool
call ended the run. These pin that a prose reply to a forced record_finding is answered once with a
reminder and a second forced call, rather than ending the run with nothing to grade, while a prose
answer the loop never forced still ends the run, as the graded suite's test 30 requires.

Rehearsal 2026-10-18 (same model and host, why_late_wo48_subcontract, 2 of 4 runs): the model ignored the
reminder too ("I need to escalate first, if available", in a run with no escalate tool), once on the token
budget's last call, and the run ended with no finding. A forced record_finding now offers only that tool, a run
that cannot escalate says so, and a second prose reply gets one last ask with the step it needs.

Run: python -m pytest tests_ai/test_finding_nudge.py -q
"""
from prod_agent.agent import FINDING_LAST_ASK, FINDING_NUDGE, NO_ESCALATION_NOTE, ProductionAgent
from tests_ai.test_llm_error import FakeMessage, FakeToolCall, finding_args, isolated_environment, recording_mcp  # noqa: F401
from tests_ai.test_token_budget import FORCE_FINDING, MeteredLLM, _reply


def _prose(text="Let me escalate.", total=1000):
    return _reply(FakeMessage(content=text), total)


def _record(total=1000):
    return _reply(FakeMessage(tool_calls=[FakeToolCall("record_finding", finding_args())]), total)


def _tool(total=1000):
    return _reply(FakeMessage(tool_calls=[FakeToolCall("no_such_tool", {})]), total)


def test_the_live_failure_now_records_its_finding(recording_mcp):
    """Forced by name near the token limit, answered in prose: reminded, forced again, recorded."""
    mcp, _ = recording_mcp
    llm = MeteredLLM([_tool(10_000), _prose(total=1000), _record(), _prose("done")])
    events = []

    out = ProductionAgent(mcp, llm=llm, model="m", token_budget=30_000, trace=events.append).run("why late?")

    assert llm.requests[1]["tool_choice"] == FORCE_FINDING, "wrap-up forced it, and the model ignored that"
    assert llm.requests[2]["tool_choice"] == FORCE_FINDING
    sent = llm.requests[2]["messages"]  # the loop's own list, so it also holds what came after this request
    at = next(i for i, m in enumerate(sent) if m.get("role") == "user" and m.get("content", "").startswith(FINDING_NUDGE))
    assert sent[at - 1]["content"] == "Let me escalate.", "the reminder answers the prose reply"
    assert sent[at]["content"] == FINDING_NUDGE + NO_ESCALATION_NOTE, "this run cannot escalate, and says so"
    assert out["finding"] is not None and out["stop_reason"] == "final_answer"
    assert [e["type"] for e in events].count("finding_nudge") == 1


def test_a_prose_answer_nothing_forced_still_ends_the_run(recording_mcp):
    """The graded suite's test 30: a final answer the loop never asked to be a tool call ends the run."""
    mcp, _ = recording_mcp
    llm = MeteredLLM([_prose("hi")])
    events = []

    out = ProductionAgent(mcp, llm=llm, model="m", max_steps=3, trace=events.append).run("why late?")

    assert out["stop_reason"] == "final_answer" and len(llm.requests) == 1
    assert [e["type"] for e in events] == ["start", "llm", "end"]


def _names(request):
    return [t["function"]["name"] for t in request["tools"]]


def test_the_asks_stop_after_the_last_one(recording_mcp):
    """A reminder, then one last ask: a model that ignores both still ends the run, rather than looping."""
    mcp, _ = recording_mcp
    llm = MeteredLLM([_tool(10_000), _prose(), _prose("still no"), _prose("never")])
    events = []

    out = ProductionAgent(mcp, llm=llm, model="m", token_budget=30_000, trace=events.append).run("why late?")

    assert out["stop_reason"] == "final_answer" and out["finding"] is None
    assert len(llm.requests) == 4
    assert [e["type"] for e in events].count("finding_nudge") == 2


def test_a_forced_record_finding_offers_only_that_tool(recording_mcp):
    mcp, _ = recording_mcp
    llm = MeteredLLM([_tool(10_000), _record(), _prose("done")])

    ProductionAgent(mcp, llm=llm, model="m", token_budget=30_000).run("why late?")

    assert len(_names(llm.requests[0])) > 1, "an unforced step offers every tool"
    assert llm.requests[1]["tool_choice"] == FORCE_FINDING and _names(llm.requests[1]) == ["record_finding"]


def test_the_2026_10_18_failure_now_records_its_finding(recording_mcp):
    """Forced and ignored, reminded and ignored again on the token budget's last call: the last ask still gets its
    step past the budget, offers only record_finding, and the finding lands."""
    mcp, _ = recording_mcp
    llm = MeteredLLM([_tool(10_000), _prose(total=20_000), _prose("I need to escalate first."), _record()])
    events = []

    out = ProductionAgent(mcp, llm=llm, model="m", token_budget=30_000, trace=events.append).run("why late?")

    assert out["finding"] is not None and len(llm.requests) == 4
    assert _names(llm.requests[3]) == ["record_finding"] and llm.requests[3]["tool_choice"] == FORCE_FINDING
    last_ask = [m for m in llm.requests[3]["messages"] if m.get("role") == "user"][-1]["content"]
    assert last_ask == FINDING_LAST_ASK + NO_ESCALATION_NOTE
    assert "last_call" in [e["type"] for e in events]


def test_a_run_that_can_escalate_is_not_told_it_cannot(recording_mcp):
    mcp, _ = recording_mcp
    llm = MeteredLLM([_tool(10_000), _prose(), _record(), _prose("done")])

    ProductionAgent(mcp, llm=llm, model="m", token_budget=30_000, escalate_mode=True).run("why late?")

    asks = [m["content"] for m in llm.requests[2]["messages"] if m.get("role") == "user"]
    assert asks[-1] == FINDING_NUDGE


def test_a_prose_reply_after_the_finding_ends_the_run(recording_mcp):
    mcp, _ = recording_mcp
    llm = MeteredLLM([_record(), _prose("done")])
    events = []

    out = ProductionAgent(mcp, llm=llm, model="m", trace=events.append).run("why late?")

    assert out["stop_reason"] == "final_answer" and len(llm.requests) == 2
    assert "finding_nudge" not in [e["type"] for e in events]
