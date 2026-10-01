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
"""
import prod_agent.domain as domain


def test_stopped_is_an_open_status():
    assert "stopped" in domain.OPEN_WO
    assert domain.OPEN_WO == {"draft", "not_started", "in_progress", "stopped"}


def test_no_wrapper_overrides_the_lateness_rule():
    """diagnose must be the function defined in this module, not one wrapped after the fact."""
    assert domain.diagnose.__module__ == "prod_agent.domain"
    assert "NOT_LATE_STATUSES" not in open(domain.__file__).read()


def test_the_rule_reads_from_open_wo_and_the_due_date():
    src = open(domain.__file__).read()
    assert '"is_late": bool(status in OPEN_WO and ((due and due < today)' in src


def test_a_stopped_order_past_due_is_late(monkeypatch):
    """WO-2026-00047: stopped, due 2026-02-25, 209 days past. The harness scored this revise while
    the wrapper was in place, and approve before it and after it."""
    import datetime as dt
    monkeypatch.setattr(domain.config, "today", lambda: dt.date(2026, 9, 29))
    status, due, today = "stopped", dt.date(2026, 2, 25), domain.config.today()
    assert bool(status in domain.OPEN_WO and due < today) is True


def test_a_stopped_order_not_yet_due_is_not_late(monkeypatch):
    """WO-2026-00075 on Keystone: stopped, due 2026-09-28. Not late on 2026-09-22 — because the date
    has not passed, not because it is stopped."""
    import datetime as dt
    monkeypatch.setattr(domain.config, "today", lambda: dt.date(2026, 9, 22))
    status, due, today = "stopped", dt.date(2026, 9, 28), domain.config.today()
    assert bool(status in domain.OPEN_WO and due < today) is False


def test_a_completed_order_is_never_late_by_status_alone():
    """The one thing the wrapper got right, already handled by OPEN_WO."""
    assert "completed" not in domain.OPEN_WO and "cancelled" not in domain.OPEN_WO
