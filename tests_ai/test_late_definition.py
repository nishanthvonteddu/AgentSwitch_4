"""A stopped order past its due date is late.

`OPEN_WO` includes "stopped", and the platform's own dashboard defines late as
status in (not_started, in_progress, stopped) AND planned_end_date < today — the query the
Manufacturing screen issues on every load. Team 04's bug B18 rests on the same reading, citing
WO-2026-00047 (due 2026-02-25, stopped) as overdue.

A NOT_LATE_STATUSES wrapper on `diagnose` briefly forced is_late=False for stopped orders. It was
added to rescue keystone_stopped_not_late_wo75, whose premise is "stopped but due 2026-09-28": true
while AGENT_TODAY was before that date, false once it passed. The task had aged, not the rule — and
the wrapper then failed most_overdue_open_why_late, which had approved on WO-2026-00047 four days
earlier. These pin the definition so a stale task cannot quietly redefine it again.

Each case runs the real domain.diagnose (and the agent's diagnose_work_order dispatch, which is what any
wrapper would have to sit in front of) over an in-memory book, so the rule is checked where it is applied
rather than restated here.
"""
import datetime as dt

import pytest
from test_behaviour_fakes import Book, agent_on, schedule_entry, work_order

import prod_agent.domain as domain


def _today(monkeypatch, day):
    monkeypatch.setattr(domain.config, "today", lambda: day)
    monkeypatch.setattr(domain.config, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)


def _diagnose_both_ways(wo, **book):
    """domain.diagnose directly, and through the agent's tool dispatch: both must agree."""
    direct = domain.diagnose(Book(tables={"WorkOrder": [wo]}, **book), wo["number"])
    dispatched = agent_on(Book(tables={"WorkOrder": [wo]}, **book))._dispatch(
        "diagnose_work_order", {"work_order": wo["number"]})
    assert direct["is_late"] == dispatched["is_late"], "something between the tool and diagnose changed is_late"
    return direct


def test_stopped_is_an_open_status():
    assert "stopped" in domain.OPEN_WO
    assert domain.OPEN_WO == {"draft", "not_started", "in_progress", "stopped"}


def test_a_stopped_order_past_due_is_late(monkeypatch):
    """WO-2026-00047: stopped, due 2026-02-25, 216 days past on 2026-09-29. The harness scored this revise
    while the wrapper was in place, and approve before it and after it."""
    _today(monkeypatch, dt.date(2026, 9, 29))
    wo = work_order("WO-2026-00047", "stopped", start="2026-02-10", end="2026-02-25")
    diag = _diagnose_both_ways(wo)
    assert diag["is_late"] is True
    assert diag["days_past_due"] == 216


def test_a_stopped_order_not_yet_due_is_not_late(monkeypatch):
    """WO-2026-00075 on Keystone: stopped, due 2026-09-28. Not late on 2026-09-22 — because the date
    has not passed, not because it is stopped."""
    _today(monkeypatch, dt.date(2026, 9, 22))
    wo = work_order("WO-2026-00075", "stopped", start="2026-09-15", end="2026-09-28")
    diag = _diagnose_both_ways(wo, schedule=[schedule_entry(wo, "on_time")])
    assert diag["is_late"] is False
    assert any(s["code"] == "stopped_without_recorded_reason" for s in diag["signals"]), \
        "not late, and still stopped: the cause is reported either way"


def test_the_same_stopped_order_becomes_late_once_its_date_passes(monkeypatch):
    """keystone_stopped_not_late_wo75's premise aged out on 2026-09-29. The rule did not change; the date did."""
    wo = work_order("WO-2026-00075", "stopped", start="2026-09-15", end="2026-09-28")
    _today(monkeypatch, dt.date(2026, 9, 28))
    assert _diagnose_both_ways(wo)["is_late"] is False, "due today is not yet past due"
    _today(monkeypatch, dt.date(2026, 9, 29))
    assert _diagnose_both_ways(wo)["is_late"] is True


@pytest.mark.parametrize("status", ["not_started", "in_progress", "stopped"])
def test_every_open_status_past_due_is_late(monkeypatch, status):
    _today(monkeypatch, dt.date(2026, 9, 16))
    assert _diagnose_both_ways(work_order("WO-2026-00090", status, end="2026-09-10"))["is_late"] is True


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_a_closed_order_is_never_late_by_its_date(monkeypatch, status):
    """The one thing the wrapper got right, already handled by OPEN_WO."""
    _today(monkeypatch, dt.date(2026, 9, 16))
    wo = work_order("WO-2026-00028", status, start="2026-05-01", end="2026-05-10")
    diag = _diagnose_both_ways(wo, schedule=[schedule_entry(wo, "late")])
    assert diag["is_late"] is False and diag["days_past_due"] is None
