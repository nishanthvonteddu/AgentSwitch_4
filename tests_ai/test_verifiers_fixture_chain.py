"""AI-WRITTEN TESTS (written by Claude, 2026-10-15) for the late_draft_chain fixture. Ungraded; the graded
tests live in tests/.

The write tasks (reschedule_fixture_chain, concurrent_edit_before_write) assume the fixture's upstream order
has a committable date. The product refuses to commit one while domain.diagnose reports any blocking signal,
and the fixture used to screen stock only, so a chain whose upstream carried, say, an active breakdown was
handed to the agent and its correct "nothing written" scored revise. The fixture now checks each candidate
chain with the product's own rule and records the result.
"""
import datetime as dt

import pytest

from harness import fixtures
from prod_agent import config


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(config, "today", lambda: dt.date(2026, 10, 1))


class FakeMcp:
    """Two candidate chains: BOM-UP1 -> BOM-DOWN1 and BOM-UP2 -> BOM-DOWN2, both with stock."""

    def __init__(self, short=()):
        self.boms = [
            {"id": "up1", "number": "BOM-UP1", "item_id": "i-up1", "is_active": True, "materials": []},
            {"id": "down1", "number": "BOM-DOWN1", "item_id": "i-d1", "is_active": True, "materials": [{"item_id": "i-up1"}]},
            {"id": "up2", "number": "BOM-UP2", "item_id": "i-up2", "is_active": True, "materials": []},
            {"id": "down2", "number": "BOM-DOWN2", "item_id": "i-d2", "is_active": True, "materials": [{"item_id": "i-up2"}]},
        ]
        self.short = set(short)
        self.orders: dict[str, dict] = {}
        self.writes = []

    def list_all(self, entity, **filters):
        return list(self.boms) if entity == "BOM" else [dict(w) for w in self.orders.values()]

    def call(self, name, args):
        if name == "endpoint.manufacturing.check_stock_availability":
            return {"result": {"overall_status": "short" if args["bom_id"] in self.short else "ok"}}
        if name == "WorkOrder.create":
            number = f"WO-FX-{len(self.orders) + 1}"
            self.orders[number] = {"id": f"id-{number}", "number": number, "status": "draft", **args}
            return dict(self.orders[number])
        if name == "WorkOrder.update":
            self.writes.append(args)
            row = next(w for w in self.orders.values() if w["id"] == args["id"])
            row.update({k: val for k, val in args.items() if k != "id"})
            return dict(row)
        if name == "WorkOrder.get":
            return dict(next(w for w in self.orders.values() if w["id"] == args["id"]))
        raise AssertionError(f"unexpected call {name}")


def diagnose_blocking(blocked_boms):
    """A stand-in for domain.diagnose: blocking signals for orders currently pointed at the listed BOMs."""
    def diagnose(mcp, work_order_id):
        row = next(w for w in mcp.orders.values() if w["id"] == work_order_id)
        signals = [{"code": "not_released", "blocking": False}]
        if row["bom_id"] in blocked_boms:
            signals.append({"code": "workstation_breakdown_active", "blocking": True})
        return {"found": True, "signals": signals}
    return diagnose


def test_fixture_skips_a_chain_whose_upstream_carries_an_undated_blocker(monkeypatch):
    monkeypatch.setattr(fixtures.domain, "diagnose", diagnose_blocking({"up1"}))
    mcp = FakeMcp()

    out = fixtures.late_draft_chain(mcp)

    assert (out["upstream"]["bom"], out["downstream"]["bom"]) == ("BOM-UP2", "BOM-DOWN2")
    assert out["upstream"]["undated_blockers"] == []
    assert out["write_ids"] == [out["upstream"]["id"], out["downstream"]["id"]]
    assert out["upstream"]["planned_end_date"] == "2026-09-28"


def test_fixture_reuses_its_rows_while_trying_chains(monkeypatch):
    monkeypatch.setattr(fixtures.domain, "diagnose", diagnose_blocking({"up1"}))
    mcp = FakeMcp()

    fixtures.late_draft_chain(mcp)

    assert len(mcp.orders) == 2  # one pair, repointed, never a second pair per candidate


def test_fixture_takes_the_first_chain_when_it_is_free(monkeypatch):
    monkeypatch.setattr(fixtures.domain, "diagnose", diagnose_blocking(set()))
    out = fixtures.late_draft_chain(FakeMcp())
    assert out["upstream"]["bom"] == "BOM-UP1"


def test_fixture_refuses_to_set_up_a_premise_no_chain_can_meet(monkeypatch):
    monkeypatch.setattr(fixtures.domain, "diagnose", diagnose_blocking({"up1", "up2"}))
    with pytest.raises(RuntimeError, match="undated blocker"):
        fixtures.late_draft_chain(FakeMcp())


def test_fixture_still_screens_stock_first(monkeypatch):
    def never(*_args):
        raise AssertionError("no chain passed the stock screen, so nothing should be diagnosed")

    monkeypatch.setattr(fixtures.domain, "diagnose", never)
    with pytest.raises(RuntimeError, match="stock shortage"):
        fixtures.late_draft_chain(FakeMcp(short={"up1", "up2"}))
