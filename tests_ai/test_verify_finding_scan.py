"""AI-written, ungraded. VerifyContext.finding_from_db over a shared, growing, sometimes dirty AgentMemory table.

The fake honours limit/offset like the REST endpoint, so paging is exercised; nothing touches the network.
"""
import datetime as dt
import json

import pytest

from harness import verify
from harness.runner import persist_then_verify
from harness.verify import Verdict, VerifyContext
from harness.verifiers import team04
from prod_agent import config
from prod_agent.mcp_client import McpError

STARTED = "2026-09-21T08:00:00"


class MemoryRest:
    """AgentMemory rows, served newest-first in pages unless `ascending` (B14: an ignored sort_order)."""

    def __init__(self, rows, *, ascending=False, with_total=False):
        self.rows = list(rows)[::-1] if ascending else list(rows)
        self.with_total, self.calls = with_total, []

    def raw(self, path, **params):
        if path != "/api/AgentMemory":
            raise McpError(403, f"GET {path} -> HTTP 403")  # every other read is outside the seat
        self.calls.append(params)
        if len(self.calls) > 100:
            raise AssertionError("paged without end")
        offset, limit = params.get("offset", 0), params.get("limit", 200)
        page = {"data": self.rows[offset:offset + limit]}
        if self.with_total:
            page["total"] = len(self.rows)
        return page


def at(minutes_after_start: float) -> str:
    return (dt.datetime.fromisoformat(STARTED) + dt.timedelta(minutes=minutes_after_start)).isoformat()


def finding_row(row_id, run_id, created_at, **fields):
    payload = {"run_id": run_id, "outcome": "refused", "refusal_reason": "outside this seat", **fields}
    return {"id": row_id, "created_by": "agent-user", "created_at": created_at,
            "content": config.FINDING_PREFIX + json.dumps(payload, sort_keys=True)}


def other_row(i, created_at):
    return {"id": f"other-{i}", "created_by": "someone", "created_at": created_at, "content": f"note {i}"}


def ctx_for(tmp_path, rest, *, started=STARTED, run_id="run-1"):
    (tmp_path / "result.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    context = {"me": "agent-user", "started_at_utc": started, "snapshot": {}}
    return VerifyContext(rest=rest, instance="suryodaya", task={}, run_dir=tmp_path, fixture=None, context=context)


# ------------------------------------------------------------------ malformed rows from other runs

@pytest.mark.parametrize("bad_content", [
    config.FINDING_PREFIX + '{"run_id": "other-run", "outcome": ',  # cut mid-JSON
    config.FINDING_PREFIX + "not json at all",
    config.FINDING_PREFIX + '["a", "list"]',                        # JSON, but not an object
    config.FINDING_PREFIX + '"a string"',
    config.FINDING_PREFIX + "null",
])
def test_a_newer_malformed_row_from_another_run_is_skipped(tmp_path, bad_content):
    rows = [{"id": "bad", "created_at": at(5), "content": bad_content}, finding_row("mine", "run-1", at(3))]
    found = ctx_for(tmp_path, MemoryRest(rows)).finding_from_db()
    assert found["_agent_memory_id"] == "mine"


def test_a_malformed_row_naming_this_run_raises_rather_than_reading_as_no_finding(tmp_path):
    rows = [{"id": "mine-cut", "created_at": at(3), "content": config.FINDING_PREFIX + '{"run_id": "run-1", "out'}]
    with pytest.raises(ValueError, match="mine-cut"):
        ctx_for(tmp_path, MemoryRest(rows)).finding_from_db()


def test_one_malformed_row_no_longer_makes_every_verifier_unevaluated(tmp_path):
    rows = [{"id": "bad", "created_at": at(5), "content": config.FINDING_PREFIX + "{"},
            finding_row("mine", "run-1", at(3))]
    ctx = ctx_for(tmp_path, MemoryRest(rows))
    verdict, reason = persist_then_verify(tmp_path, {"run_id": "run-1"}, lambda: team04.refuse_payroll_question(ctx))
    assert verdict is Verdict.APPROVE, reason


# ------------------------------------------------------------------ how far back to look

def test_a_finding_behind_more_than_200_newer_rows_is_found(tmp_path):
    newer = [other_row(i, at(30 - i * 0.01)) for i in range(450)]
    rows = newer + [finding_row("mine", "run-1", at(2))]
    rest = MemoryRest(rows)
    found = ctx_for(tmp_path, rest).finding_from_db()
    assert found["_agent_memory_id"] == "mine"
    assert [c["offset"] for c in rest.calls] == [0, 200, 400]


def test_paging_stops_once_rows_are_older_than_the_run(tmp_path):
    margin = verify.FINDING_CLOCK_MARGIN.total_seconds() / 60
    rows = [other_row(i, at(-margin - 1 - i)) for i in range(5000)]
    rest = MemoryRest(rows)
    assert ctx_for(tmp_path, rest).finding_from_db() is None
    assert len(rest.calls) == 1, "rows from before the run cannot hold its finding"


def test_rows_inside_the_clock_margin_are_still_read(tmp_path):
    # A finding stamped slightly before started_at (our clock was the fallback and ran ahead) is still found.
    rows = [other_row(i, at(-0.5)) for i in range(250)] + [finding_row("mine", "run-1", at(-1))]
    assert ctx_for(tmp_path, MemoryRest(rows)).finding_from_db()["_agent_memory_id"] == "mine"


def test_an_ignored_sort_order_reads_on_instead_of_stopping_early(tmp_path):
    # Served oldest first: the first rows are long before the run, which must not end the read.
    rows = [finding_row("mine", "run-1", at(2))] + [other_row(i, at(-60 - i)) for i in range(450)]
    rest = MemoryRest(rows, ascending=True)
    assert ctx_for(tmp_path, rest).finding_from_db()["_agent_memory_id"] == "mine"
    assert len(rest.calls) == 3


def test_no_start_time_reads_to_the_end_of_the_table(tmp_path):
    rows = [other_row(i, at(-60 - i)) for i in range(300)] + [finding_row("mine", "run-1", at(-400))]
    assert ctx_for(tmp_path, MemoryRest(rows), started=None).finding_from_db()["_agent_memory_id"] == "mine"


@pytest.mark.parametrize("with_total", [True, False])
def test_a_table_too_large_to_rule_out_the_finding_raises(tmp_path, monkeypatch, with_total):
    monkeypatch.setattr(verify, "FINDING_SCAN_MAX_ROWS", 600)
    rows = [other_row(i, at(-i)) for i in range(2000)]
    rest = MemoryRest(rows, ascending=True, with_total=with_total)
    with pytest.raises(RuntimeError, match="unknown"):
        ctx_for(tmp_path, rest).finding_from_db()
    assert len(rest.calls) == 3


def test_a_missing_finding_in_a_small_table_is_none(tmp_path):
    rows = [finding_row("theirs", "other-run", at(1)), other_row(1, at(0.5))]
    assert ctx_for(tmp_path, MemoryRest(rows, with_total=True)).finding_from_db() is None
