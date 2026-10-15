"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-10-08).

These are NOT the team's hand-written tests and must not be claimed as such: the course scores
AI-written tests at zero.

AgentSwitch's harness runner allows at most 30 minutes per instance. Suryodaya's task set took 26.4
minutes of agent time by its median durations before fixtures and verification, and one tool call
stalled for 73 minutes on 2026-10-07. These pin the clock the harness keeps: a run budget, a limit
per task that wraps the agent up early, cheapest tasks first, and read-only tasks run together only
where no verifier can see another task's writes.

Run: python -m pytest tests_ai/test_run_budget.py -q
"""
import threading
from types import SimpleNamespace

import pytest

from harness import runner
from harness.fixtures import WRITING_FIXTURES
from harness.results import ResultsBook
from prod_agent import agent as agent_module
from prod_agent.agent import ProductionAgent
from tests_ai.test_llm_error import FakeMessage, isolated_environment, recording_mcp  # noqa: F401
from tests_ai.test_rate_limit_wait import _429


# ----------------------------------------------------------------------------- the run's clock

def test_no_budget_means_no_deadline():
    assert runner.task_deadline({"id": "t"}, 0, None, 100) == (True, None)


def test_a_task_gets_the_default_limit_inside_the_budget():
    assert runner.task_deadline({"id": "t"}, 0, 1620, 10) == (True, 10 + runner.TASK_SECONDS_DEFAULT)


def test_a_task_may_ask_for_more_time():
    assert runner.task_deadline({"id": "t", "max_seconds": 420}, 0, 1620, 10) == (True, 430)


def test_the_limit_shrinks_to_what_the_budget_has_left():
    go, deadline = runner.task_deadline({"id": "t"}, 0, 1620, 1500)
    assert go and deadline == 1500 + 120 - runner.VERIFY_RESERVE_SECONDS


def test_no_task_starts_with_too_little_left():
    assert runner.task_deadline({"id": "t"}, 0, 1620, 1620 - runner.MIN_TASK_START_SECONDS + 1) == (False, None)


def test_the_budget_comes_from_the_environment_then_the_platform(monkeypatch):
    assert runner.budget_seconds(False) is None
    assert runner.budget_seconds(True) == runner.PLATFORM_BUDGET_MINUTES * 60
    monkeypatch.setenv("HARNESS_BUDGET_MINUTES", "2")
    assert runner.budget_seconds(False) == 120


def test_cheapest_tasks_run_first_and_ties_keep_file_order():
    plan = [({"id": "slow", "max_seconds": 420}, "s"), ({"id": "a"}, "s"),
            ({"id": "mid", "max_seconds": 300}, "s"), ({"id": "b"}, "s")]
    assert [t["id"] for t, _ in runner.order_plan(plan)] == ["a", "b", "mid", "slow"]


# ----------------------------------------------------------------------------- what may run together

@pytest.mark.parametrize(("task", "together"), [
    ({"mode": "dry_run"}, True),
    ({"mode": "dry_run", "fixture": "late_by_item"}, True),
    ({"mode": "apply"}, False),
    ({"mode": "dry_run", "escalate": True}, False),
    ({"mode": "dry_run", "fixture": "late_draft_chain"}, False),
    ({"mode": "dry_run", "fixture": "injected_late_order"}, False),
    ({"mode": "apply", "interference": "edit_dates_before_apply"}, False),
])
def test_only_tasks_no_other_verifier_can_see_run_together(task, together):
    assert runner.isolated(task) is together


def test_every_writer_in_the_real_task_set_runs_alone():
    for task in runner.load_tasks(False):
        if task["mode"] == "apply" or task.get("escalate") or task.get("fixture") in WRITING_FIXTURES:
            assert not runner.isolated(task), task["id"]


# ----------------------------------------------------------------------------- execute

def _record(task, instance, verdict="approve"):
    return {"task_id": task["id"], "instance": instance, "verdict": verdict, "reason": "ok", "sample": False,
            "run_id": "r", "seconds": 1.0}


def test_tasks_past_the_budget_are_on_file_as_not_run(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(runner, "run_one", lambda task, instance, root, **kw: ran.append(task["id"]) or _record(task, instance))
    plan = [({"id": n, "mode": "dry_run"}, "suryodaya") for n in ("a", "b", "c")]
    book = ResultsBook(tmp_path / "results.json", "suryodaya")
    for task, instance in plan:
        book.plan(task, instance)
    ticks = iter([0, 10, 20, 1600])  # start, then before each task

    records = runner.execute(plan, tmp_path, book, budget=1620, clock=lambda: next(ticks))

    assert ran == ["a", "b"]
    assert [r["task_id"] for r in records] == ["a", "b"]
    assert [t["evidence"] for t in book.doc()["tasks"]] == ["approve: ok", "approve: ok", "not run: time budget"]


def test_a_task_that_cannot_start_is_unevaluated_and_the_run_goes_on(monkeypatch, tmp_path):
    def run_one(task, instance, root, **kw):
        if task["id"] == "broken":
            raise FileExistsError("run directory already exists")
        return _record(task, instance)

    monkeypatch.setattr(runner, "run_one", run_one)
    plan = [({"id": "broken", "mode": "dry_run"}, "suryodaya"), ({"id": "fine", "mode": "dry_run"}, "suryodaya")]
    book = ResultsBook(tmp_path / "results.json", "suryodaya")

    records = runner.execute(plan, tmp_path, book, budget=None)

    assert [(r["task_id"], r["verdict"]) for r in records] == [("broken", "unevaluated"), ("fine", "approve")]
    assert records[0]["reason"].startswith("harness error: FileExistsError")


def test_isolated_tasks_overlap_and_writers_always_run_alone(monkeypatch, tmp_path):
    lock, active, seen = threading.Lock(), [0], {}
    all_isolated_running = threading.Barrier(3, timeout=10)

    def run_one(task, instance, root, **kw):
        with lock:
            active[0] += 1
            seen[task["id"]] = active[0]
        # Only the readers meet at the barrier. The escalating task is dry_run too, and once waited here alone: it
        # stalled the full timeout, and execute() swallowed the BrokenBarrierError into an unevaluated record.
        if runner.isolated(task):
            all_isolated_running.wait()  # only passes if all three are in flight at once
        with lock:
            active[0] -= 1
        return _record(task, instance)

    monkeypatch.setattr(runner, "run_one", run_one)
    readers = [({"id": f"read{i}", "mode": "dry_run"}, "suryodaya") for i in range(3)]
    writers = [({"id": "write", "mode": "apply"}, "suryodaya"), ({"id": "escalate", "mode": "dry_run", "escalate": True}, "suryodaya")]
    book = ResultsBook(tmp_path / "results.json", "suryodaya")

    records = runner.execute(writers[:1] + readers + writers[1:], tmp_path, book, budget=None, workers=3)

    assert seen["write"] == 1 and seen["escalate"] == 1
    assert [r["task_id"] for r in records][-2:] == ["write", "escalate"], "readers go first, then writers one by one"
    # Every task really ran to its verdict: a reader stuck at the barrier, or a writer that waited on it, would
    # come back unevaluated ("harness error: BrokenBarrierError") and still pass the ordering checks above.
    assert {r["task_id"]: r["verdict"] for r in records} == {
        "read0": "approve", "read1": "approve", "read2": "approve", "write": "approve", "escalate": "approve"}
    assert not all_isolated_running.broken


# ----------------------------------------------------------------------------- the agent under a deadline

class CapturingLLM:
    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=FakeMessage(content="done"))])


def test_short_of_time_the_finding_is_forced(recording_mcp):
    mcp, _ = recording_mcp
    llm = CapturingLLM()

    ProductionAgent(mcp, llm=llm, model="gpt-4.1", deadline=100, clock=lambda: 100 - 30).run("why is WO-1 late?")

    assert llm.requests[0]["tool_choice"] == {"type": "function", "function": {"name": "record_finding"}}


def test_with_time_to_spare_nothing_is_forced(recording_mcp):
    mcp, _ = recording_mcp
    llm = CapturingLLM()

    ProductionAgent(mcp, llm=llm, model="gpt-4.1", deadline=1000, clock=lambda: 0).run("why is WO-1 late?")

    assert "tool_choice" not in llm.requests[0]


def test_past_the_deadline_the_run_stops_without_another_model_call(recording_mcp):
    mcp, _ = recording_mcp
    llm = CapturingLLM()
    events = []

    result = ProductionAgent(mcp, llm=llm, model="gpt-4.1", trace=events.append, deadline=100,
                             clock=lambda: 101).run("why is WO-1 late?")

    assert result["stop_reason"] == "deadline"
    assert llm.requests == []
    assert [e["type"] for e in events] == ["start", "deadline", "end"]


def test_a_rate_limit_wait_is_not_taken_into_the_deadline(recording_mcp, monkeypatch):
    mcp, _ = recording_mcp
    slept = []
    monkeypatch.setattr(agent_module.time, "sleep", slept.append)

    class Limited(CapturingLLM):
        def create(self, **kwargs):
            self.requests.append(kwargs)
            raise _429()

    llm = Limited()
    result = ProductionAgent(mcp, llm=llm, model="gpt-4.1", deadline=100, clock=lambda: 0).run("why is WO-1 late?")

    assert result["stop_reason"] == "llm_error"
    assert slept == [] and len(llm.requests) == 1


def test_the_start_event_records_the_time_allowed(recording_mcp):
    mcp, _ = recording_mcp
    events = []

    ProductionAgent(mcp, llm=CapturingLLM(), model="gpt-4.1", trace=events.append, deadline=300,
                    clock=lambda: 60).run("why is WO-1 late?")

    assert events[0]["seconds_allowed"] == 240
    assert events[0]["forced_tool_choice"] == "named"


def test_the_watchdog_is_armed_only_on_the_runners_throwaway_copy(tmp_path):
    """Its os._exit skips withdrawing escalations, so on a shared tenant it would leave a real person assigned."""
    book = ResultsBook(tmp_path / "results.json", label="suryodaya")
    assert runner.watchdog_for(None, 120, book) is None
    assert runner.watchdog_for({"instance": "suryodaya"}, None, book) is None
    timer = runner.watchdog_for({"instance": "suryodaya"}, 120, book)
    try:
        assert timer is not None and timer.is_alive()
    finally:
        timer.cancel()
