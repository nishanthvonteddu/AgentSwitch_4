"""AI-written, ungraded. RestClient.list is every verifier's ground truth, so it must return the whole table or raise.

Offline: `_get` is replaced by an in-memory table, so no Session ever logs in.
"""
import json

import pytest

from harness.runner import persist_then_verify
from harness.verify import Verdict, VerifyContext
from prod_agent.mcp_client import RestClient


class TableRest(RestClient):
    """A REST table of `size` rows. `with_total` decides whether payloads carry "total"; `server_page` caps rows per
    page below what was asked for, as a server with a lower maximum would."""

    def __init__(self, size, *, with_total, server_page=None, max_calls=200):
        super().__init__(session=None)
        self.size, self.with_total, self.server_page, self.max_calls = size, with_total, server_page, max_calls
        self.calls = []

    def _get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if len(self.calls) > self.max_calls:  # the old client never stopped on a huge table; fail, do not hang
            raise AssertionError("paged past any sane ceiling")
        offset, limit = params["offset"], params["limit"]
        if self.server_page:
            limit = min(limit, self.server_page)
        rows = [{"id": f"r{i}"} for i in range(offset, min(offset + limit, self.size))]
        return {"data": rows, "total": self.size} if self.with_total else {"data": rows}


def test_a_table_without_total_is_read_past_its_first_page():
    rest = TableRest(450, with_total=False)
    rows = rest.list("WorkOrder")
    assert [r["id"] for r in rows] == [f"r{i}" for i in range(450)]
    assert len(rest.calls) == 3, "ends on the short page"


def test_a_table_without_total_that_fills_its_last_page_ends_on_the_empty_one():
    rest = TableRest(400, with_total=False)
    assert len(rest.list("WorkOrder")) == 400
    assert len(rest.calls) == 3


def test_total_wins_over_a_short_page_when_the_server_caps_page_size():
    rest = TableRest(250, with_total=True, server_page=100)
    assert len(rest.list("WorkOrder")) == 250


def test_filters_reach_the_server_and_cannot_override_paging():
    rest = TableRest(450, with_total=False)
    rows = rest.list("JobCard", work_order_id="wo-1", offset=999)
    assert len(rows) == 450
    assert all(p["work_order_id"] == "wo-1" for _, p in rest.calls)
    assert [p["offset"] for _, p in rest.calls] == [0, 200, 400]


@pytest.mark.parametrize("with_total", [True, False])
def test_a_table_past_the_ceiling_raises_instead_of_returning_part_of_it(monkeypatch, with_total):
    monkeypatch.setattr(RestClient, "MAX_ROWS", 500)
    rest = TableRest(10_000, with_total=with_total)
    with pytest.raises(RestClient.TooManyRows):
        rest.list("WorkOrder")
    assert len(rest.calls) <= 3


def test_a_table_exactly_at_the_ceiling_is_returned_whole(monkeypatch):
    monkeypatch.setattr(RestClient, "MAX_ROWS", 400)
    assert len(TableRest(400, with_total=False).list("WorkOrder")) == 400
    assert len(TableRest(400, with_total=True).list("WorkOrder")) == 400


def test_a_verifier_over_a_too_large_table_scores_unevaluated(tmp_path, monkeypatch):
    monkeypatch.setattr(RestClient, "MAX_ROWS", 500)
    rest = TableRest(10_000, with_total=False)

    def verify():
        ctx = VerifyContext(rest=rest, instance="suryodaya", task={}, run_dir=tmp_path, fixture=None, context={})
        late = [w for w in ctx.rest.list("WorkOrder") if w.get("id") == "r9999"]
        return (Verdict.APPROVE, "") if late else (Verdict.REVISE, "the order the agent named is not in the table")

    verdict, reason = persist_then_verify(tmp_path, {"run_id": "run-1"}, verify)
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["run_id"] == "run-1"
    assert verdict is Verdict.UNEVALUATED and "TooManyRows" in reason
