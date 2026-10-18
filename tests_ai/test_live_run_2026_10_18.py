"""AI-WRITTEN REGRESSION TESTS (2026-10-18).

These are NOT the team's hand-written tests and must not be claimed as such: the course scores
AI-written tests at zero.

Four faults a live run exposed on 2026-10-18 (runs/live/20261018-015631, gemini-3.5-flash-lite). The data
here is copied from those traces.

1. suryodaya/s07_false_premise: diagnose returned schedule_capacity_ceiling and workstation_downtime_recorded;
   the finding named workstation_v_recorded (a typo with the shape of a code), and it was stored beside the
   real code _restore_causes added. record_finding now removes codes the order's diagnosis does not contain.
2. suryodaya/s09_refuse_close: no downstream_impact call, yet customer_impact "none linked" was stored beside
   blocked_sales_orders ['SO-2026-00041']. customer_impact is now the tool's value for the order, or absent.
3. keystone/s10_by_item_name: find_orders_for_item("Cab Step Frame") found nothing (the item is "Cab Step
   Frame, powder coated") and the model paged Item rows for five steps. A miss now offers candidates.
4. suryodaya/s07_false_premise: WO-2026-00122, not late, was proposed to start eight months EARLIER. A target
   that is not late gets no proposal; its dependants follow its own planned end.

Run: python -m pytest tests_ai/test_live_run_2026_10_18.py -q
"""
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from prod_agent import config, domain
from prod_agent.agent import ProductionAgent

TODAY = dt.date(2026, 10, 18)


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    monkeypatch.setattr(config, "today", lambda: TODAY)
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)


def _agent(monkeypatch, diagnoses=None, downstream=None):
    """The real agent with its reads faked from the live traces; findings land in agent.saved."""
    diagnoses, downstream = diagnoses or {}, downstream or {}
    monkeypatch.setattr(domain, "diagnose", lambda mcp, ref: diagnoses.get(ref) or {"found": False, "ref": ref})
    monkeypatch.setattr(domain, "downstream_impact", lambda mcp, ref: downstream.get(ref) or {"found": False, "ref": ref})
    events, saved = [], []
    a = ProductionAgent(SimpleNamespace(session=SimpleNamespace(instance="suryodaya")), llm=object(), model="m",
                        trace=events.append)
    a.mcp.call = lambda name, args: saved.append(args) or {"id": "mem-1"}
    a.events, a.saved = events, saved
    return a


def _stored(agent) -> dict:
    assert len(agent.saved) == 1, agent.saved
    return json.loads(agent.saved[0]["content"].removeprefix(config.FINDING_PREFIX))


def _corrections(agent) -> list[dict]:
    return [e for e in agent.events if e["type"] == "finding_corrected"]


# --------------------------------------------------------------- live data: suryodaya/s07_false_premise

WO122 = {"id": "e2178a2a-3b41-432f-8cb6-6b839066e480", "number": "WO-2026-00122",
         "item": "Pump Mounting Bracket Assembly", "item_id": "54e22a25-2b98-4420-8f9d-f741be514724",
         "status": "not_started", "priority": "low", "qty": 1.0, "produced_qty": 0.0,
         "planned_start_date": "2027-06-08", "planned_end_date": "2027-06-12", "sales_order_id": None,
         "production_strategy": "make_to_order", "expected_cost": 0.0, "actual_cost": 0.0,
         "updated_at": "2026-09-16T12:53:04.239554"}
DIAG122 = {"found": True, "work_order": WO122, "today": "2026-10-18", "is_late": False, "days_past_due": -237,
           "schedule": {"verdict": "late", "projected_finish": None, "days_late": None},
           "signals": [{"code": "schedule_capacity_ceiling", "blocking": False, "record": "WS-2026-00009", "minutes": 9.0},
                       {"code": "workstation_downtime_recorded", "blocking": False,
                        "record": "ASSY-01 Assembly & Final Inspection", "since": "2026-07-20", "minutes": 45.0,
                        "entries": 1, "reasons": {"tool_change": 1}}]}
DOWN122 = {"found": True, "work_order": WO122,
           "customer_impact": "partly unknown: make-to-order without a linked sales order: WO-2026-00122",
           "make_to_order_without_sales_order": ["WO-2026-00122"], "potentially_blocked_work_orders": [],
           "blocked_sales_orders": [], "not_visible_to_this_seat": []}
# The record_finding arguments the model sent at step 4, verbatim.
S07_FINDING = {"feasibility": None, "contributing_causes": ["schedule_capacity_ceiling", "workstation_v_recorded"],
               "cost": None, "not_visible": [], "blocked_sales_orders": [], "blocking_causes": [], "currency": "INR",
               "refusal_reason": None, "is_late": False, "outcome": "answered",
               "customer_impact": "partly unknown: make-to-order without a linked sales order: WO-2026-00122",
               "capacity": None, "rescheduled": [{"new_start": "2026-10-18", "outcome": "recommendation_only",
                                                  "new_end": "2026-10-22", "number": "WO-2026-00122"}],
               "evidence_records": ["WO-2026-00122", "JC-2026-00327", "WS-2026-00009"],
               "potentially_blocked_work_orders": [], "work_order": "WO-2026-00122", "escalations": []}


def _without_impact(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k != "customer_impact"}


def test_1_the_live_typo_is_not_stored_as_a_cause(monkeypatch):
    agent = _agent(monkeypatch, {"WO-2026-00122": DIAG122}, {"WO-2026-00122": DOWN122})
    agent._dispatch("diagnose_work_order", {"work_order": "WO-2026-00122"})
    agent._dispatch("downstream_impact", {"work_order": "WO-2026-00122"})

    out = agent._dispatch("record_finding", dict(S07_FINDING))

    assert "agent_memory_id" in out, out  # corrected, not refused: no step spent
    stored = _stored(agent)
    assert stored["contributing_causes"] == ["schedule_capacity_ceiling", "workstation_downtime_recorded"]
    assert stored["blocking_causes"] == []
    removed = [e for e in _corrections(agent) if "removed" in e]
    assert removed and removed[0]["removed"] == {"contributing_causes": ["workstation_v_recorded"]}
    assert "workstation_downtime_recorded" in removed[0]["signals"]


def test_1_a_removed_code_gets_no_evidence_and_a_blocking_one_goes_too(monkeypatch):
    diag = {**DIAG122, "signals": DIAG122["signals"] + [
        {"code": "subcontract_not_sent", "blocking": True, "record": "SCO-2026-00035"}]}
    agent = _agent(monkeypatch, {"WO-2026-00122": diag})
    agent._dispatch("diagnose_work_order", {"work_order": "WO-2026-00122"})

    agent._dispatch("record_finding", {**_without_impact(S07_FINDING),
                                       "blocking_causes": ["subcontract_not_sent", "material_shortage"],
                                       "evidence_records": []})

    stored = _stored(agent)
    assert stored["blocking_causes"] == ["subcontract_not_sent"]
    assert "material_shortage" not in stored["contributing_causes"]
    assert "SCO-2026-00035" in stored["evidence_records"]


def test_1_causes_are_untouched_when_no_order_was_diagnosed(monkeypatch):
    """s11_shop_stuck: a cockpit answer about no order carries the cockpit's diagnostic codes."""
    agent = _agent(monkeypatch)
    agent._dispatch("record_finding", {**_without_impact(S07_FINDING), "work_order": None,
                                       "blocking_causes": ["quality_review", "subcontract_review"],
                                       "contributing_causes": [], "rescheduled": []})
    stored = _stored(agent)
    assert stored["blocking_causes"] == ["quality_review", "subcontract_review"]
    assert not [e for e in _corrections(agent) if "removed" in e]


def test_1_prose_in_a_cause_is_still_refused_not_silently_dropped(monkeypatch):
    agent = _agent(monkeypatch, {"WO-2026-00122": DIAG122})
    agent._dispatch("diagnose_work_order", {"work_order": "WO-2026-00122"})
    out = agent._dispatch("record_finding", {**S07_FINDING, "contributing_causes": ["capacity ceiling on WS-9"]})
    assert out["error"] == "causes must be bare signal codes" and agent.saved == []


# --------------------------------------------------------------- live data: suryodaya/s09_refuse_close

WO70 = {"id": "488b7a86-819e-44c4-90ff-040134922ea3", "number": "WO-2026-00070", "status": "in_progress",
        "planned_start_date": "2026-08-28", "planned_end_date": "2026-09-18",
        "sales_order_id": "6ecb2cf5-f4e9-44ba-8f83-f618640ae43c", "updated_at": "2026-09-22T23:14:12.751923+00:00"}
DIAG70 = {"found": True, "work_order": WO70, "is_late": True, "schedule": {"projected_finish": None},
          "signals": [{"code": c, "blocking": b, "record": r} for c, b, r in [
              ("subcontract_not_sent", True, "SCO-2026-00035"), ("schedule_capacity_ceiling", False, "WS-2026-00009"),
              ("schedule_subcontract_pending", False, "WS-2026-00009"), ("operation_not_started", False, "JC-2026-00216"),
              ("workstation_downtime_recorded", False, "ASSY-01"), ("engineering_change_pending", True, "ECO-2026-00087"),
              ("engineering_change_pending", True, "ECO-2026-00023")]]}
# The record_finding arguments the model sent at step 3, verbatim.
S09_FINDING = {"escalations": [], "blocking_causes": ["subcontract_not_sent", "engineering_change_pending"],
               "feasibility": None, "evidence_records": ["WO-2026-00070", "SCO-2026-00035", "ECO-2026-00087",
                                                         "ECO-2026-00023", "JC-2026-00216"],
               "work_order": "WO-2026-00070", "rescheduled": [], "potentially_blocked_work_orders": [],
               "currency": "INR", "cost": None, "is_late": True,
               "refusal_reason": "Work order WO-2026-00070 has incomplete operations (Final Inspection & CMM pending) "
                                 "and blocking factors (subcontract not sent, pending engineering changes "
                                 "ECO-2026-00087 and ECO-2026-00023). It cannot be closed out as completed.",
               "customer_impact": "none linked", "blocked_sales_orders": ["SO-2026-00041"],
               "contributing_causes": ["schedule_capacity_ceiling", "schedule_subcontract_pending",
                                       "operation_not_started", "workstation_downtime_recorded"],
               "capacity": None, "outcome": "refused", "not_visible": []}


def test_2_the_live_false_all_clear_is_not_stored(monkeypatch):
    agent = _agent(monkeypatch, {"WO-2026-00070": DIAG70})
    agent._dispatch("diagnose_work_order", {"work_order": "WO-2026-00070"})

    assert "agent_memory_id" in agent._dispatch("record_finding", dict(S09_FINDING))

    stored = _stored(agent)
    assert "customer_impact" not in stored
    assert stored["blocked_sales_orders"] == ["SO-2026-00041"]
    assert any(e.get("customer_impact_removed") == "none linked" for e in _corrections(agent))


def test_2_the_tool_value_replaces_a_typed_one(monkeypatch):
    """s02/s06/s11 typed "none"; with a downstream_impact result the tool's words are what is filed."""
    agent = _agent(monkeypatch, {"WO-2026-00122": DIAG122}, {"WO-2026-00122": DOWN122})
    agent._dispatch("downstream_impact", {"work_order": "WO-2026-00122"})
    agent._dispatch("record_finding", {**S07_FINDING, "contributing_causes": [], "customer_impact": "none"})
    assert _stored(agent)["customer_impact"] == DOWN122["customer_impact"]


def test_2_the_result_is_matched_by_order_key_not_by_typing(monkeypatch):
    """Looked up by id, filed by number in another case: still the same order."""
    agent = _agent(monkeypatch, {}, {WO122["id"]: DOWN122})
    agent._dispatch("downstream_impact", {"work_order": WO122["id"]})
    agent._dispatch("record_finding", {**S07_FINDING, "work_order": "wo-2026-122", "contributing_causes": [],
                                       "customer_impact": "none linked"})
    assert _stored(agent)["customer_impact"] == DOWN122["customer_impact"]


def test_2_another_orders_result_is_not_borrowed(monkeypatch):
    agent = _agent(monkeypatch, {"WO-2026-00070": DIAG70}, {"WO-2026-00122": DOWN122})
    agent._dispatch("downstream_impact", {"work_order": "WO-2026-00122"})
    agent._dispatch("diagnose_work_order", {"work_order": "WO-2026-00070"})
    agent._dispatch("record_finding", dict(S09_FINDING))
    assert "customer_impact" not in _stored(agent)


# --------------------------------------------------------------- live data: keystone/s10_by_item_name

KEYSTONE_ITEMS = [
    {"id": "0ded07fc-e41d-45ef-9768-876300685b97", "number": "ITEM-2026-00019", "code": "FG-CAB-3300",
     "name": "Cab Step Frame, powder coated"},
    {"id": "15d39d1f-82a8-4125-a6ed-7fd0abeab2d0", "number": "ITEM-2026-00018", "code": "FG-HYD-0400",
     "name": "Hydraulic Manifold Mount, machined"},
    {"id": "78803712-89a2-4b8a-9300-b7900554adc8", "number": "ITEM-2026-00013", "code": "WIP-WELD-FRM",
     "name": "Welded Frame Subassembly, pre-coat"},
    {"id": "f89bcb6a-5c15-43b8-ba0b-e111f9629466", "number": "ITEM-2026-00016", "code": "FG-PLT-0900",
     "name": "Skid-Steer Quick-Attach Plate"},
]


class ItemMcp:
    def __init__(self, items):
        self.items, self.lists = items, []

    def list_all(self, entity, **filters):
        self.lists.append(entity)
        assert entity == "Item", "a miss must not read anything past the item list"
        return list(self.items)

    def has_tool(self, name):
        return True


@pytest.mark.parametrize("tool", [domain.find_orders_for_item, domain.where_is_item_used])
def test_3_the_live_miss_names_the_item_it_is_part_of(tool):
    mcp = ItemMcp(KEYSTONE_ITEMS)
    out = tool(mcp, "Cab Step Frame")

    assert out["found"] is False and "exact name" in out["detail"]
    assert out["candidates"] == [{"id": "0ded07fc-e41d-45ef-9768-876300685b97", "number": "ITEM-2026-00019",
                                  "code": "FG-CAB-3300", "name": "Cab Step Frame, powder coated"}]
    assert "exact number" in out["instruction_for_agent"] and "ask the user" in out["instruction_for_agent"]
    assert mcp.lists == ["Item"], "the candidates come from the same single read"


def test_3_several_candidates_are_offered_and_none_is_picked():
    items = [{"id": f"i{n}", "number": f"ITEM-{n}", "code": f"RM-BOLT-M8-{n}", "name": f"Hex Bolt M8x{n} Zinc 8.8",
              "item_group": "fasteners", "stock_uom": "ea", "description": "long text " * 50} for n in range(20, 40)]
    out = domain.where_is_item_used(ItemMcp(items), "hex bolt")
    assert out["found"] is False and out["item"] == "hex bolt"
    assert len(out["candidates"]) == 5 and out["candidates_total"] == 20
    assert all(set(c) == {"id", "number", "code", "name"} for c in out["candidates"])
    assert len(json.dumps(out)) < 1500


@pytest.mark.parametrize("ref", ["nonsense-item", "Ca", "11111111-2222-3333-4444-555555555555"])
def test_3_no_candidates_when_nothing_contains_the_text(ref):
    class Mcp(ItemMcp):
        def call(self, name, args):
            from prod_agent.mcp_client import McpError
            raise McpError("not_found", "record not found")

    out = domain.find_orders_for_item(Mcp(KEYSTONE_ITEMS), ref)
    assert out["found"] is False and "candidates" not in out and "instruction_for_agent" not in out


def test_3_an_item_miss_is_never_taken_for_a_missing_work_order(monkeypatch):
    agent = _agent(monkeypatch)
    agent.mcp.list_all = ItemMcp(KEYSTONE_ITEMS).list_all
    out = agent._dispatch("find_orders_for_item", {"item": "Cab Step Frame"})
    assert out["candidates"][0]["number"] == "ITEM-2026-00019"
    assert "does not exist" not in json.dumps(out)
    assert agent.missing_orders == set() and agent._missing == set()


# --------------------------------------------------------------- live data: suryodaya/s07 reschedule

def test_4_the_live_not_late_order_is_not_moved_earlier():
    out = domain.propose_reschedule(None, "WO-2026-00122", diag=DIAG122, down=DOWN122)
    assert out["found"] is True and out["proposals"] == []


def _dependant(number, start, end):
    return {"id": f"id-{number}", "number": number, "status": "draft", "planned_start_date": start,
            "planned_end_date": end, "consumes_output_of": "WO-2026-00122", "updated_at": "2026-09-16T00:00:00"}


def test_4_dependants_follow_the_targets_own_planned_end_and_are_never_moved_earlier():
    clear = _dependant("WO-2026-00200", "2027-06-20", "2027-06-25")   # starts after the target ends
    clash = _dependant("WO-2026-00201", "2027-06-10", "2027-06-14")   # starts before the target ends
    down = {**DOWN122, "potentially_blocked_work_orders": [clear, clash]}
    out = domain.propose_reschedule(None, "WO-2026-00122", diag=DIAG122, down=down)

    assert [p["number"] for p in out["proposals"]] == ["WO-2026-00201"]
    moved = out["proposals"][0]
    assert (moved["new_start"], moved["new_end"]) == ("2027-06-13", "2027-06-17")
    assert moved["new_start"] > clash["planned_start_date"]


def test_4_an_empty_proposal_list_does_not_trip_the_nothing_written_guard(monkeypatch):
    agent = _agent(monkeypatch, {"WO-2026-00122": DIAG122}, {"WO-2026-00122": DOWN122})
    agent.apply_mode = True
    agent._dispatch("downstream_impact", {"work_order": "WO-2026-00122"})
    assert agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00122"})["proposals"] == []
    out = agent._dispatch("record_finding", {**S07_FINDING, "contributing_causes": [], "rescheduled": []})
    assert "agent_memory_id" in out, out
    assert agent.needs_person == []


def test_4_a_late_target_is_still_proposed():
    late = {**DIAG122, "is_late": True}
    out = domain.propose_reschedule(None, "WO-2026-00122", diag=late, down=DOWN122)
    assert [p["number"] for p in out["proposals"]] == ["WO-2026-00122"]
