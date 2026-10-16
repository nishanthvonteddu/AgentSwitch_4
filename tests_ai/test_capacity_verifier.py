"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-10-15).

These are NOT the team's hand-written tests and must not be claimed as such: the course scores
AI-written tests at zero.

Official harness run, 2026-10-15 (1bfe88a, deepseek-v4p1-flash): capacity_load_disputed scored revise on
both instances with "the two figures disagree by 94x in the database, but the finding does not say so".
The platform's capacity board had begun scaling time_in_mins by for_qty: it reported 129,772 booked
minutes, the job cards' work content to the minute, and the agent recorded both figures and that they
agree. The verifier rebuilt the board's figure as sum(time_in_mins), the old bug, so it graded a correct
answer wrong. It now reads the board, and these pin both worlds: board understating and board agreeing.

Run: python -m pytest tests_ai/test_capacity_verifier.py -q
"""
import pytest

from harness.adapters import make_verify_context
from harness.verify import Verdict, VerifyContext
from harness.verifiers import team04

TASK = {"id": "capacity_load_disputed", "params": {}}
# 10 min x 50 + 20 min x 30 = 1,100 min of work content; sum(time_in_mins) alone is 30. The done card is excluded.
CARDS = [{"id": "c1", "status": "in_progress", "time_in_mins": 10, "for_qty": 50},
         {"id": "c2", "status": "open", "time_in_mins": 20, "for_qty": 30},
         {"id": "c3", "status": "completed", "time_in_mins": 999, "for_qty": 9}]
FIXED_BOARD = {"counts": {"booked_minutes": 1100.0, "available_minutes": 1000.0}}  # agrees: 110% either way
OLD_BOARD = {"counts": {"booked_minutes": 30.0, "available_minutes": 1000.0}}      # understates 36.7x


class FakeRest:
    def __init__(self, finding, cards=CARDS):
        self.rows = {"JobCard": list(cards)}
        self.findings = [finding] if finding else []

    def raw(self, path, **params):
        return {"data": self.findings}

    def list(self, entity, **params):
        return self.rows.get(entity, [])


def finding(board_pct, content_pct, disagree, **over):
    return {"id": "memory-1", "created_by": "agent-user", "run_id": "run-1", "outcome": "answered",
            "capacity": {"board_load_pct": board_pct, "work_content_load_pct": content_pct, "figures_disagree": disagree},
            **over}


def verdict(tmp_path, monkeypatch, found, board, cards=CARDS):
    def read_board(self, horizon_days):
        if isinstance(board, Exception):
            raise board
        return board
    monkeypatch.setattr(VerifyContext, "capacity_board", read_board)
    (tmp_path / "result.json").write_text('{"run_id": "run-1"}', encoding="utf-8")
    context = {"me": "agent-user", "started_at_utc": "2026-10-15T08:00:00", "snapshot": {}}
    return team04.capacity_load_disputed(make_verify_context(tmp_path, FakeRest(found, cards), context, task=TASK))


def test_the_official_runs_answer_now_passes(tmp_path, monkeypatch):
    """Board fixed: both figures recorded, and that they agree."""
    v, reason = verdict(tmp_path, monkeypatch, finding(110.0, 110.0, False), FIXED_BOARD)
    assert v is Verdict.APPROVE, reason
    assert "agree" in reason


def test_claiming_a_disagreement_the_board_no_longer_shows_is_a_revise(tmp_path, monkeypatch):
    v, reason = verdict(tmp_path, monkeypatch, finding(3.0, 110.0, True), FIXED_BOARD)
    assert v is Verdict.REVISE
    assert "match" in reason


def test_while_the_board_understates_the_disagreement_must_still_be_flagged(tmp_path, monkeypatch):
    v, _ = verdict(tmp_path, monkeypatch, finding(3.0, 110.0, True), OLD_BOARD)
    assert v is Verdict.APPROVE
    v, reason = verdict(tmp_path, monkeypatch, finding(3.0, 110.0, False), OLD_BOARD)
    assert v is Verdict.REVISE and "does not say so" in reason


def test_figures_that_do_not_describe_this_shop_are_a_revise(tmp_path, monkeypatch):
    v, reason = verdict(tmp_path, monkeypatch, finding(50.0, 110.0, False), FIXED_BOARD)
    assert v is Verdict.REVISE and "2.2x gap" in reason


@pytest.mark.parametrize("pair", [(None, 110.0), (110.0, None)])
def test_one_figure_alone_is_a_revise(tmp_path, monkeypatch, pair):
    v, _ = verdict(tmp_path, monkeypatch, finding(*pair, False), FIXED_BOARD)
    assert v is Verdict.REVISE


def test_a_zero_board_figure_is_reported_not_raised(tmp_path, monkeypatch):
    v, reason = verdict(tmp_path, monkeypatch, finding(0, 110.0, False), FIXED_BOARD)
    assert v is Verdict.REVISE and "no gap" in reason


def test_an_unreadable_board_is_unevaluated(tmp_path, monkeypatch):
    v, reason = verdict(tmp_path, monkeypatch, finding(110.0, 110.0, False), RuntimeError("not in catalogue"))
    assert v is Verdict.UNEVALUATED and "capacity board" in reason


def test_no_open_cards_is_unevaluated(tmp_path, monkeypatch):
    v, _ = verdict(tmp_path, monkeypatch, finding(110.0, 110.0, False), FIXED_BOARD, cards=[CARDS[2]])
    assert v is Verdict.UNEVALUATED


def test_a_refusal_is_a_revise(tmp_path, monkeypatch):
    v, _ = verdict(tmp_path, monkeypatch, finding(110.0, 110.0, False, outcome="refused"), FIXED_BOARD)
    assert v is Verdict.REVISE
