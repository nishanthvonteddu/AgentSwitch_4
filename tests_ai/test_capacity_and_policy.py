"""Offline tests for the capacity, bottleneck and declared-policy capabilities.

WRITTEN WITH CLAUDE (AI-assisted). These are ungraded: the team's hand-written tests live in tests/.

Everything here runs against fakes, so it needs no tenant. The point is the judgement calls, not the
plumbing: that the two load figures are both reported, that a lane the platform refused is never called
clear, and that a policy governing someone else's persona is never borrowed as this seat's own limit.
"""
import types

from prod_agent import domain


class FakeMcp:
    """Enough of McpClient for the four new functions: a tool catalogue, canned calls and canned lists."""

    def __init__(self, *, tools=(), calls=None, lists=None, rest_status=None):
        self._tools = set(tools)
        self._calls = calls or {}
        self._lists = lists or {}
        self.rest_status = rest_status or {}
        self.session = types.SimpleNamespace(instance="suryodaya")

    def has_tool(self, name):
        return name in self._tools

    def tool_names(self):
        return set(self._tools)

    def call(self, name, arguments=None):
        if name not in self._calls:
            raise AssertionError(f"unexpected call {name}")
        value = self._calls[name]
        return value(arguments) if callable(value) else value

    def list_all(self, entity, page=200, max_rows=None, **filters):
        rows = self._lists.get(entity, [])
        for key, wanted in filters.items():
            # `.list` reads a comma-separated value as OR (confirmed over MCP 2026-09-29), so the
            # fake must too, or a server-side status filter looks like a bug that is not there.
            allowed = set(str(wanted).split(",")) if isinstance(wanted, str) else {wanted}
            rows = [r for r in rows if r.get(key) in allowed]
        return rows


def _card(minutes, qty, status="open", ws="ws1", wo="wo1"):
    return {"time_in_mins": minutes, "for_qty": qty, "status": status,
            "workstation_id": ws, "work_order_id": wo, "id": f"jc{minutes}-{qty}"}


def _board(booked, available, **counts):
    return {"result": {"counts": {"booked_minutes": booked, "available_minutes": available,
                                  "workstations_overloaded": 0, "periods_overloaded": 0, **counts}}}


# ------------------------------------------------------------------ capacity_outlook

def test_capacity_outlook_reports_both_figures_and_flags_the_dispute():
    """The board's own number and the work content in the same cards both survive into the result."""
    cards = [_card(9, 250), _card(6, 250)]          # 15 booked, 3750 of work content
    mcp = FakeMcp(tools={"JobCard.list"},
                  calls={"JobCard.list": {"data": [{}]},
                         "endpoint.manufacturing.capacity_board": _board(15.0, 10000.0)},
                  lists={"JobCard": cards,
                         "Workstation": [{"id": "ws1", "number": "WS-1", "name": "Press",
                                          "working_hours_per_day": 8, "capacity": 1}]})
    out = domain.capacity_outlook(mcp, horizon_days=21)

    assert out["board_reported"]["booked_minutes"] == 15.0
    assert out["work_content"]["minutes"] == 3750.0
    assert out["board_understates_load"] is True
    assert out["confidence"] == "disputed"
    # Both magnitudes have to reach the model, or it cannot report the disagreement it is told to report.
    assert "250.0x" in out["note"]


def test_capacity_outlook_does_not_cry_wolf_when_the_board_agrees():
    """If the platform starts scaling minutes, the dispute flag must go quiet by itself."""
    cards = [_card(2250, 1), _card(1500, 1)]        # already totals, qty 1
    mcp = FakeMcp(tools={"JobCard.list"},
                  calls={"JobCard.list": {"data": [{}]},
                         "endpoint.manufacturing.capacity_board": _board(3750.0, 10000.0)},
                  lists={"JobCard": cards, "Workstation": []})
    out = domain.capacity_outlook(mcp)

    assert out["board_understates_load"] is False
    assert out["confidence"] == "board_agrees"
    assert "agree" in out["note"]


def test_capacity_outlook_names_workstations_over_declared_capacity():
    """8h x 1 unit x 21 days = 10080 min; 20000 of content is over it and must be named."""
    mcp = FakeMcp(tools={"JobCard.list"},
                  calls={"JobCard.list": {"data": [{}]},
                         "endpoint.manufacturing.capacity_board": _board(100.0, 10080.0)},
                  lists={"JobCard": [_card(200, 100, ws="ws1")],
                         "Workstation": [{"id": "ws1", "number": "WS-1", "name": "Assembly",
                                          "working_hours_per_day": 8, "capacity": 1}]})
    over = domain.capacity_outlook(mcp)["work_content"]["workstations_over_declared_capacity"]

    assert [o["workstation"] for o in over] == ["WS-1"]
    assert over[0]["work_content_minutes"] == 20000.0
    assert over[0]["declared_capacity_minutes"] == 10080.0
    # Reported to 2dp on purpose: a planner reads "1.98x over", not 1.9841269841269842.
    assert over[0]["times_over"] == round(20000 / 10080, 2)


def test_capacity_outlook_refuses_when_job_cards_are_not_visible():
    mcp = FakeMcp(tools=set(), lists={}, calls={})
    out = domain.capacity_outlook(mcp)

    assert out["available"] is False
    assert out["not_visible_to_this_seat"] == ["JobCard: tool_not_in_seat_catalogue"]


# ------------------------------------------------------------------ order_feasible_by

def _feasibility_mcp(cards, *, blocking=(), monkeypatch=None):
    mcp = FakeMcp(tools={"JobCard.list"},
                  calls={"JobCard.list": {"data": [{}]},
                         "WorkOrder.get": {"id": "wo1", "number": "WO-1"},
                         "endpoint.manufacturing.capacity_board": _board(10.0, 10000.0)},
                  lists={"JobCard": cards, "Workstation": [],
                         "WorkOrder": [{"id": "wo1", "number": "WO-1"}]})
    monkeypatch.setattr(domain, "resolve_work_order", lambda m, r: {"id": "wo1", "number": "WO-1"})
    monkeypatch.setattr(domain, "diagnose",
                        lambda m, r: {"blocking_causes": list(blocking), "contributing_causes": []})
    return mcp


def test_order_feasible_by_never_promises_yes(monkeypatch):
    """The platform models no calendar, so a committed date would be invented. 'yes' must be unreachable."""
    mcp = _feasibility_mcp([_card(10, 5)], monkeypatch=monkeypatch)
    out = domain.order_feasible_by(mcp, "WO-1", "2099-01-01")

    assert out["verdict"] == "unknown"
    assert out["remaining_work_content_minutes"] == 50.0
    assert "work calendar" in out["note"]


def test_order_feasible_by_says_no_behind_an_undated_blocker(monkeypatch):
    """An undated blocker stops the arithmetic; the reason names the blocker."""
    mcp = _feasibility_mcp([_card(10, 5)], blocking=["material_shortage"], monkeypatch=monkeypatch)
    out = domain.order_feasible_by(mcp, "WO-1", "2099-01-01")

    assert out["verdict"] == "no"
    assert "material_shortage" in out["reason"]


def test_order_feasible_by_says_no_for_a_date_already_past(monkeypatch):
    mcp = _feasibility_mcp([_card(10, 5)], monkeypatch=monkeypatch)
    out = domain.order_feasible_by(mcp, "WO-1", "2000-01-01")

    assert out["verdict"] == "no"
    assert "already past" in out["reason"]


def test_order_feasible_by_reports_a_missing_order_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(domain, "resolve_work_order", lambda m, r: None)
    out = domain.order_feasible_by(FakeMcp(), "WO-NOPE", "2099-01-01")

    assert out["found"] is False
    assert "no work order" in out["reason"]


# ------------------------------------------------------------------ shop_floor_exceptions

def test_shop_floor_exceptions_keeps_lane_states_so_a_denied_lane_is_not_clear():
    """Two of the four cockpit lanes are permission_denied live; losing that turns unknown into 'nothing here'."""
    cockpit = {"result": {"total_items": 2, "complete": True,
                          "lane_states": {"shortages": "permission_denied", "quality_blocks": "ready",
                                          "subcontract_blocks": "ready",
                                          "automation_failures": "permission_denied"},
                          "items": [
                              {"kind": "quality_block", "severity": "high", "state": "ready",
                               "record_label": "QI-1", "entity": "QualityInspection",
                               "diagnostic": {"code": "quality_review"}},
                              {"kind": "subcontract_block", "severity": "high", "state": "ready",
                               "record_label": "SCO-1", "entity": "SubcontractOrder",
                               "diagnostic": {"code": "subcontract_pending"}}]}}
    mcp = FakeMcp(tools={"endpoint.manufacturing.exception_cockpit"},
                  calls={"endpoint.manufacturing.exception_cockpit": cockpit})
    out = domain.shop_floor_exceptions(mcp)

    assert out["lane_states"]["shortages"] == "permission_denied"
    assert {lane["kind"] for lane in out["lanes"]} == {"quality_block", "subcontract_block"}
    assert out["truncated"] is False


def test_shop_floor_exceptions_flags_truncation_rather_than_implying_a_full_list():
    cockpit = {"result": {"total_items": 90, "complete": True, "lane_states": {},
                          "items": [{"kind": "quality_block", "record_label": f"QI-{i}"} for i in range(50)]}}
    mcp = FakeMcp(tools={"endpoint.manufacturing.exception_cockpit"},
                  calls={"endpoint.manufacturing.exception_cockpit": cockpit})
    out = domain.shop_floor_exceptions(mcp)

    assert out["truncated"] is True
    assert out["returned"] == 50 and out["total_items"] == 90


def test_shop_floor_exceptions_reports_absence_of_the_tool_as_not_visible():
    out = domain.shop_floor_exceptions(FakeMcp(tools=set()))

    assert out["available"] is False
    assert out["not_visible_to_this_seat"] == ["endpoint.manufacturing.exception_cockpit"]


# ------------------------------------------------------------------ seat_policy_conformance

def _policy_mcp(monkeypatch, *, readable):
    mcp = FakeMcp(tools={"AgentToolPolicy.list"},
                  lists={"AgentSeat": [{"name": "Production Agent", "module": "manufacturing",
                                        "persona_id": "p1", "charter": "Whether an order can be taken by a date.",
                                        "goal_keys": "production.capacity_answerable", "tier": "core"}],
                         "AgentPersona": [{"id": "p1", "tool_policy_id": "tp1",
                                           "require_approval": [{"action_pattern": "delete:*"},
                                                                {"action_pattern": "update:SalesOrder:delivery_date"}]}],
                         "AgentToolPolicy": [{"id": "tp1", "name": "Production Worker",
                                              "allowed_domains": "manufacturing,inventory",
                                              "denied_entities": "Coupon,AgentPersona",
                                              "read_only": 0, "max_records_per_query": 100}]})
    monkeypatch.setattr(domain, "_rest_status", lambda m, e: 200 if e in readable else 403)
    return mcp


def test_seat_policy_conformance_reports_divergence_without_calling_it_permission(monkeypatch):
    """AgentPersona is declared denied yet readable; that is a divergence to report, not a limit to obey."""
    mcp = _policy_mcp(monkeypatch, readable={"AgentPersona"})
    out = domain.seat_policy_conformance(mcp)

    assert out["divergences"] == ["AgentPersona"]
    assert out["declared_policy"]["max_records_per_query"] == 100
    assert "delete:*" in out["requires_human_approval"]
    # The caveat is the whole point: without it the model reads these rows as its own boundary.
    assert "hosted persona" in out["caveat"]


def test_seat_policy_conformance_finds_no_divergence_when_denied_means_denied(monkeypatch):
    mcp = _policy_mcp(monkeypatch, readable=set())
    out = domain.seat_policy_conformance(mcp)

    assert out["divergences"] == []
    assert all(o["this_seat_reads_it"] is False for o in out["denied_entity_observations"])


def test_seat_policy_conformance_is_unavailable_without_the_policy_tool():
    out = domain.seat_policy_conformance(FakeMcp(tools=set()))

    assert out["available"] is False
    assert out["not_visible_to_this_seat"] == ["AgentToolPolicy"]


def test_seat_policy_conformance_handles_an_instance_with_no_manufacturing_seat(monkeypatch):
    mcp = FakeMcp(tools={"AgentToolPolicy.list"},
                  lists={"AgentSeat": [{"name": "Sales Agent", "module": "crm"}]})
    out = domain.seat_policy_conformance(mcp)

    assert out["available"] is False
    assert "no manufacturing AgentSeat" in out["reason"]


# ------------------------------------------------------------------ the agent surface

def test_the_new_capabilities_are_offered_to_the_model_and_guarded_against_repeats():
    from prod_agent.agent import ProductionAgent

    agent = ProductionAgent(types.SimpleNamespace(session=types.SimpleNamespace(instance="suryodaya")),
                            llm=object(), model="test-model")
    names = [s["function"]["name"] for s in agent.tool_specs()]
    for tool in ("capacity_outlook", "order_feasible_by", "shop_floor_exceptions", "seat_policy_conformance"):
        assert tool in names, tool
        # All four are reads whose answer cannot usefully change inside one run.
        assert tool in ProductionAgent.REPEAT_GUARDED, tool


def test_dispatch_routes_each_new_tool_name_to_its_domain_function(monkeypatch):
    """Registering a tool is not the same as wiring it: this exercises the branch the model actually hits."""
    from prod_agent.agent import ProductionAgent

    agent = ProductionAgent(types.SimpleNamespace(session=types.SimpleNamespace(instance="suryodaya")),
                            llm=object(), model="test-model")
    seen = {}

    def recorder(key, reply):
        def fake(_mcp, *args):
            seen[key] = args[0] if len(args) == 1 else (args or True)
            return reply
        return fake

    monkeypatch.setattr(domain, "capacity_outlook", recorder("capacity_outlook", {"ok": "capacity"}))
    monkeypatch.setattr(domain, "order_feasible_by", recorder("order_feasible_by", {"ok": "feasible"}))
    monkeypatch.setattr(domain, "shop_floor_exceptions", recorder("shop_floor_exceptions", {"ok": "exceptions"}))
    monkeypatch.setattr(domain, "seat_policy_conformance", recorder("seat_policy_conformance", {"ok": "policy"}))

    assert agent._dispatch("capacity_outlook", {"horizon_days": 7}) == {"ok": "capacity"}
    assert agent._dispatch("order_feasible_by", {"work_order": "WO-1", "due": "2026-10-15"}) == {"ok": "feasible"}
    assert agent._dispatch("shop_floor_exceptions", {"limit": 5}) == {"ok": "exceptions"}
    assert agent._dispatch("seat_policy_conformance", {}) == {"ok": "policy"}

    assert seen["capacity_outlook"] == 7
    assert seen["order_feasible_by"] == ("WO-1", "2026-10-15")
    assert seen["shop_floor_exceptions"] == 5


def test_dispatch_applies_the_documented_defaults_when_the_model_omits_arguments(monkeypatch):
    """horizon_days and limit are optional in the schema, so the model will sometimes send neither."""
    from prod_agent.agent import ProductionAgent

    agent = ProductionAgent(types.SimpleNamespace(session=types.SimpleNamespace(instance="suryodaya")),
                            llm=object(), model="test-model")
    seen = {}

    def record(key):
        def fake(_mcp, value):
            seen[key] = value
            return {}
        return fake

    monkeypatch.setattr(domain, "capacity_outlook", record("horizon"))
    monkeypatch.setattr(domain, "shop_floor_exceptions", record("limit"))

    agent._dispatch("capacity_outlook", {})
    agent._dispatch("shop_floor_exceptions", {})

    assert seen == {"horizon": 21, "limit": 50}


def test_the_prompt_tells_the_model_to_report_both_load_figures():
    """The dispute is only useful if the model is told never to pick one figure."""
    from prod_agent.agent import SYSTEM_PROMPT

    assert "board_understates_load" in SYSTEM_PROMPT
    assert "BOTH" in SYSTEM_PROMPT
    assert "never cite them as your own permission" in SYSTEM_PROMPT


def test_the_finding_schema_can_carry_both_load_figures():
    """Verifiers grade the database, so a figure with nowhere structured to live cannot be graded at all."""
    from prod_agent.agent import RECORD_FINDING_SCHEMA

    capacity = RECORD_FINDING_SCHEMA["properties"]["capacity"]
    assert set(capacity["properties"]) == {"board_load_pct", "work_content_load_pct", "figures_disagree"}
    # Optional on purpose: the other 24 tasks have no load question and must not be forced to answer one.
    assert "capacity" not in RECORD_FINDING_SCHEMA["required"]
    assert "null" in capacity["type"]


def test_an_optional_capacity_field_left_out_is_not_treated_as_a_nulled_required_field():
    """_nulled_required drives off the schema, so a new optional field must not trip it."""
    from prod_agent.agent import _nulled_required

    assert _nulled_required({"capacity": None}) == []
