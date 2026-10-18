"""Holes in record_finding's guards that let wrong data reach the database, or carried data across orders.

AI-written regression tests (ungraded); the team's hand-written tests live in tests/.

Each was confirmed on de456fe:

  C7  _mistyped checked a hand-picked few kinds, so outcome='conflict', is_late='yes' (declared ["boolean","null"]),
      work_order=123, a feasibility verdict of 'yes', cost figures as strings, a rescheduled entry with no number
      and an escalation with no "raised" were all persisted;
  C6  the never-substitute-an-order guard compared raw text: an answer with work_order null passed it, a request
      naming "wo-2026-09999" or "WO-2026-9999" named no order at all, and a lookup typed in another case than the
      request never matched it;
  C5  _restore_dropped treated a null like an omission, so a value the model cleared came back, and a retry about
      the right order inherited what was typed about the wrong one (and _restore_causes diagnosed the wrong one).

Run: python -m pytest tests_ai/test_agent_guard_holes.py -q
"""
import copy
import json
import re
from types import SimpleNamespace

import pytest
from test_behaviour_fakes import Book, agent_on, finding, pin_today, work_order

from prod_agent import agent as agent_module
from prod_agent import domain
from prod_agent.agent import RECORD_FINDING_SCHEMA, ProductionAgent, _mistyped

SHAPE = "these fields have the wrong shape"


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    pin_today(monkeypatch)  # 2026-09-16


# --------------------------------------------------------------- C7: every field checked against the schema

WRONG = [
    ("outcome", "conflict"), ("outcome", 5), ("is_late", "yes"), ("is_late", 1), ("work_order", 123),
    ("currency", ["INR"]), ("customer_impact", {}), ("refusal_reason", False),
    ("feasibility", {"asked_by": "2026-10-01", "verdict": "yes"}),
    ("feasibility", {"asked_by": 20261001, "verdict": "no"}),
    ("feasibility", {"verdict": "unknown", "remaining_work_content_hours": "12"}),
    ("capacity", {"board_load_pct": "117", "work_content_load_pct": 117.0, "figures_disagree": True}),
    ("capacity", {"board_load_pct": 117.0, "work_content_load_pct": 120.0, "figures_disagree": "yes"}),
    ("cost", {"expected": "100", "actual": "120", "variance": "20"}),
    ("cost", {"expected": True, "actual": 120}),
    ("rescheduled", [{"new_start": "2026-09-21", "outcome": "applied"}]),
    ("rescheduled", [{"number": "WO-2026-00170", "new_start": "2026-09-21"}]),
    ("rescheduled", [{"number": None, "outcome": "applied"}]),
    ("rescheduled", [{"number": "WO-2026-00170", "outcome": "applied", "new_end": 20260924}]),
    ("escalations", [{"number": "ESC-1"}]),
    ("escalations", [{"raised": "true"}]),
    ("escalations", [{"raised": True, "assignee": ["plant head"]}]),
]


@pytest.mark.parametrize(("field", "value"), WRONG, ids=[f"{f}={json.dumps(v)}" for f, v in WRONG])
def test_c7_a_wrong_shape_anywhere_in_the_finding_is_refused_and_never_stored(field, value):
    book = Book()
    agent = agent_on(book)

    out = agent._dispatch("record_finding", {**finding(None), field: value})

    assert out["error"] == SHAPE and any(m.startswith(field) for m in out["fields"]), out
    assert book.memories == [] and agent.finding is None


@pytest.mark.parametrize(("field", "value"), WRONG, ids=[f"{f}={json.dumps(v)}" for f, v in WRONG])
def test_c7_the_retry_does_not_inherit_the_wrong_value(field, value):
    book = Book()
    agent = agent_on(book)
    agent._dispatch("record_finding", {**finding(None), field: value})

    retry = finding(None)
    retry.pop(field, None)
    assert "agent_memory_id" in agent._dispatch("record_finding", retry)
    assert field not in book.recorded_findings()[0]


RIGHT = [
    ("is_late", None), ("is_late", False), ("outcome", "partial"), ("feasibility", None),
    ("feasibility", {"asked_by": "2026-10-01", "verdict": "unknown", "remaining_work_content_hours": None}),
    ("feasibility", {"asked_by": "2026-10-01", "verdict": "no", "remaining_work_content_hours": 12}),
    ("capacity", {"board_load_pct": None, "work_content_load_pct": 117, "figures_disagree": False}),
    ("cost", {"expected": 100, "actual": 120.5, "variance": 20.5}),
    ("rescheduled", [{"number": "WO-2026-00170", "new_start": None, "new_end": None, "outcome": "not_applied"}]),
    ("escalations", [{"raised": False, "number": None, "assignee": None, "reason_code": "no_assignee",
                      "detail": "no escalation assignee is configured"}]),
    ("escalations", [{"raised": True, "escalation_id": "esc-1", "number": "ESC-1", "status": "open",
                      "assignee": "Plant Head", "due_at": "2026-09-16T14:00:00Z"}]),
    ("customer_impact", "none linked"),
]


@pytest.mark.parametrize(("field", "value"), RIGHT, ids=[f"{f}={json.dumps(v)}" for f, v in RIGHT])
def test_c7_values_the_schema_allows_still_pass(field, value):
    """Extra keys on an entry are allowed: escalations are filed as escalate returned them."""
    assert _mistyped({**finding(None), field: value}) == []


def test_c7_a_field_added_to_the_schema_is_checked_without_touching_the_guard(monkeypatch):
    schema = copy.deepcopy(RECORD_FINDING_SCHEMA)
    schema["properties"]["handover"] = {"type": ["object", "null"], "required": ["team"], "properties": {
        "team": {"type": "string"}, "priority": {"type": "string", "enum": ["low", "high"]}}}
    monkeypatch.setattr(agent_module, "RECORD_FINDING_SCHEMA", schema)

    assert _mistyped({"handover": {"team": "planning", "priority": "high"}}) == []
    assert _mistyped({"handover": {"team": "planning", "priority": "urgent"}})
    assert _mistyped({"handover": {"priority": "low"}})
    assert _mistyped({"handover": {"team": 7}})


def test_c7_a_partial_finding_is_still_the_callers_choice():
    """tests/test_production.py records {"outcome": "refused"} alone."""
    book = Book()
    assert "agent_memory_id" in agent_on(book)._dispatch("record_finding", {"outcome": "refused"})


def _full_run_book():
    """Every fixer has something to do: a chain to write (one write stale), causes and evidence to restore."""
    wo49 = work_order("WO-2026-00049", "stopped", start="2026-08-01", end="2026-08-30")
    return Book(tables={
        "WorkOrder": [work_order("WO-2026-00169", "draft", start="2026-09-01", end="2026-09-05"),
                      work_order("WO-2026-00170", "draft", start="2026-09-10", end="2026-09-13", bom_id="bom-170"),
                      wo49],
        "BOM": [{"id": "bom-170", "number": "BOM-170", "materials": [{"item_id": "item-WO-2026-00169", "qty": 2}]}],
        "SubcontractOrder": [{"id": "sco-24", "number": "SCO-2026-00024", "work_order_id": wo49["id"],
                              "status": "draft", "expected_delivery_date": None, "vendor_id": "v-1"}]})


def test_c7_what_the_fixers_produce_always_passes_the_shape_check():
    """The loop's own corrections are stored as they are, so each must be in the declared shape: a confirmed write,
    a stale write, causes and evidence restored, unread lanes named, an id filed as its number."""
    book = _full_run_book()
    agent = agent_on(book, apply_mode=True, approve=lambda p: True,
                     allowed_write_ids={"id-WO-2026-00169", "id-WO-2026-00170"})
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00169"})
    agent._dispatch("apply_reschedule", {"work_order_id": "id-WO-2026-00169"})
    book.tables["WorkOrder"][1]["updated_at"] = "2026-09-16T09:30:00Z"  # another team edits WO-170
    assert agent._dispatch("apply_reschedule", {"work_order_id": "id-WO-2026-00170"})["outcome"] == "changed_underneath"
    agent._dispatch("diagnose_work_order", {"work_order": "WO-2026-00049"})
    agent._unread_lanes = {"shortages": "permission_denied"}

    agent._dispatch("record_finding", finding("id-WO-2026-00049", rescheduled=[{"number": "WO-2026-00170",
                                                                                "outcome": "conflict"}]))

    [stored] = book.recorded_findings()
    assert stored["work_order"] == "WO-2026-00049"
    assert [(r["number"], r["outcome"]) for r in stored["rescheduled"]] == \
        [("WO-2026-00169", "applied"), ("WO-2026-00170", "changed_underneath")]
    assert "subcontract_not_sent" in stored["blocking_causes"] and "SCO-2026-00024" in stored["evidence_records"]
    assert any(e.startswith("shortages") for e in stored["not_visible"])
    assert _mistyped({k: v for k, v in stored.items() if k in RECORD_FINDING_SCHEMA["properties"]}) == []


def test_c7_a_missing_order_refusal_the_loop_files_passes_the_shape_check():
    book = Book(tables={"WorkOrder": []})
    agent = agent_on(book)
    agent._dispatch("diagnose_work_order", {"work_order": "WO-2026-09999"})
    agent._dispatch("record_finding", finding("WO-2026-09999", blocking_causes=["not_released"]))
    [stored] = book.recorded_findings()
    assert stored["outcome"] == "refused" and _mistyped(
        {k: v for k, v in stored.items() if k in RECORD_FINDING_SCHEMA["properties"]}) == []


# --------------------------------------------------------------- C6: one normalisation for WO numbers

def test_c6_wo_numbers_compare_by_year_and_sequence_whatever_the_typing():
    _wo_key = agent_module._wo_key
    assert _wo_key("wo-2026-9999") == _wo_key(" WO-2026-09999 ") == "WO-2026-09999"
    assert _wo_key("WO-2026-00048") != _wo_key("WO-2025-00048")
    assert _wo_key("SO-2026-00048") is None and _wo_key(None) is None and _wo_key("WO-26-1") is None


@pytest.fixture
def persisted(monkeypatch):
    def diagnose(mcp, ref):
        if re.search(r"-0*9999$", ref):
            return {"found": False, "ref": ref}
        return {"found": True, "work_order": {"number": ref.upper(), "id": f"id-{ref.upper()}"}, "signals": []}

    monkeypatch.setattr(domain, "diagnose", diagnose)
    rows = []
    monkeypatch.setattr(domain, "record_finding", lambda mcp, run_id, f: rows.append(f) or {"run_id": run_id})
    return rows


def _call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


def _message(content=None, tool_calls=()):
    tool_calls = list(tool_calls)
    return SimpleNamespace(content=content, tool_calls=tool_calls, model_dump=lambda **_: {
        "role": "assistant", "content": content, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in tool_calls]})


class Scripted:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.sent.append(kwargs["messages"])
        reply = self.replies.pop(0) if self.replies else _message(content="done")
        return SimpleNamespace(choices=[SimpleNamespace(message=reply)])


def _answer(work_order):
    return finding(work_order, blocking_causes=["engineering_change_pending"], evidence_records=["WO-2026-00099"])


def _refusal(work_order="WO-2026-09999"):
    return finding(work_order, outcome="refused", is_late=None, refusal_reason="that work order does not exist")


def _run(request, lookups, record_first, record_then=None):
    replies = [_message(tool_calls=[_call(f"d{i}", "diagnose_work_order", {"work_order": ref})])
               for i, ref in enumerate(lookups)]
    replies.append(_message(tool_calls=[_call("r1", "record_finding", record_first)]))
    if record_then is not None:
        replies.append(_message(tool_calls=[_call("r2", "record_finding", record_then)]))
    llm = Scripted(replies)
    agent = ProductionAgent(SimpleNamespace(session=SimpleNamespace(instance="suryodaya")), llm=llm, model="m")
    agent.run(request)
    first = next((json.loads(m["content"]) for m in llm.sent[-1] if m.get("tool_call_id") == "r1"), None)
    return agent, first


@pytest.mark.parametrize("placeholder", [None, "null", "n/a"])
def test_c6a_an_answer_that_names_no_order_is_refused_when_the_asked_order_is_missing(persisted, placeholder):
    _, first = _run("Why is WO-2026-09999 late?", ["WO-2026-09999", "WO-2026-00099"], _answer(placeholder),
                    _refusal())
    assert "WO-2026-09999 was asked about and does not exist" in first["error"]
    assert persisted == [_refusal()]


@pytest.mark.parametrize("request_text", ["Why is wo-2026-09999 late?", "Why is WO-2026-9999 late?",
                                          "why is Wo-2026-009999 late"])
def test_c6b_the_asked_order_is_found_in_the_request_however_it_is_typed(persisted, request_text):
    _, first = _run(request_text, ["WO-2026-09999", "WO-2026-00099"], _answer("WO-2026-00099"), _refusal())
    assert "was asked about and does not exist" in first["error"]
    assert persisted == [_refusal()]


def test_c6c_a_lookup_typed_in_another_case_still_counts_as_the_asked_order(persisted):
    _, first = _run("Why is WO-2026-09999 late?", ["wo-2026-09999", "WO-2026-00099"], _answer("WO-2026-00099"),
                    _refusal())
    assert "WO-2026-09999 was asked about" in first["error"]
    assert persisted == [_refusal()]


@pytest.mark.parametrize("refusal", [_refusal(), _refusal(None), _refusal("wo-2026-9999"), {"outcome": "refused"}])
def test_c6_a_refusal_about_the_missing_order_is_still_accepted(persisted, refusal):
    """Named as asked, in any typing, or with no order at all: the graded suite records a bare refusal."""
    _, first = _run("Why is WO-2026-9999 late?", ["wo-2026-09999"], refusal)
    assert "error" not in first and len(persisted) == 1


def test_c6_an_answer_about_the_asked_order_itself_is_filed_as_a_refusal_not_refused(persisted):
    _, first = _run("Why is wo-2026-09999 late?", ["WO-2026-09999"], _answer("WO-2026-09999"))
    assert "error" not in first and persisted[0]["outcome"] == "refused"


# --------------------------------------------------------------- C5: restore only what was left out

def _escalating_book():
    return Book(tables={"WorkOrder": [work_order("WO-2026-00048", "draft")]},
                endpoints={"AgentSession.create": {"id": "sess-1"},
                           "endpoint.agent_governance.escalations.assignees": {"options": []}})


def test_c5a_a_value_the_retry_clears_is_not_put_back():
    book = _escalating_book()
    agent = agent_on(book, escalate_mode=True)
    agent.needs_person = ["WO-2026-00048: locked"]
    refused = agent._dispatch("record_finding", finding("WO-2026-00048", outcome="refused", is_late=None,
                                                        refusal_reason="locked, a person must act"))
    assert refused["error"] == "escalation required before recording the finding"
    escalation = agent._dispatch("escalate", {"work_order": "WO-2026-00048", "reason": "locked",
                                              "reason_code": "policy_refusal"})

    # Escalated, the model now answers instead, and clears the refusal reason on purpose.
    agent._dispatch("record_finding", finding("WO-2026-00048", outcome="partial", is_late=None, refusal_reason=None,
                                              escalations=[escalation]))

    [stored] = book.recorded_findings()
    assert stored["outcome"] == "partial" and stored["refusal_reason"] is None


def test_c5a_a_field_left_out_is_still_restored():
    book = _escalating_book()
    agent = agent_on(book, escalate_mode=True)
    agent.needs_person = ["WO-2026-00048: locked"]
    agent._dispatch("record_finding", finding("WO-2026-00048", outcome="refused", is_late=None,
                                              refusal_reason="locked", evidence_records=["SCO-2026-00030"]))
    retry = finding("WO-2026-00048", outcome="refused", is_late=None, refusal_reason="locked")
    del retry["evidence_records"]
    agent._dispatch("record_finding", retry)
    assert "SCO-2026-00030" in book.recorded_findings()[0]["evidence_records"]


def _two_orders():
    return Book(tables={"WorkOrder": [work_order("WO-2026-00073", "in_progress", end="2026-09-01"),
                                      work_order("WO-2026-00116", "stopped", end="2026-09-30")]})


def _looked_up(book, number):
    return [args for name, args in book.calls if name == "WorkOrder.list" and args.get("search") == number]


def test_c5b_nothing_typed_about_the_wrong_order_reaches_the_retry_or_the_database():
    book = _two_orders()
    agent = agent_on(book)
    agent._dispatch("diagnose_work_order", {"work_order": "WO-2026-00073"})

    refused = agent._dispatch("record_finding", finding("WO-2026-00116", evidence_records=["SO-2026-00116"],
                                                        blocked_sales_orders=["SO-2026-00116"]))
    assert refused["error"] == "the finding is about a work order this run never looked up"
    assert _looked_up(book, "WO-2026-00116") == [], "the order about to be refused was diagnosed for its causes"

    retry = finding("WO-2026-00073")
    del retry["evidence_records"], retry["blocked_sales_orders"]
    agent._dispatch("record_finding", retry)

    [stored] = book.recorded_findings()
    assert stored["work_order"] == "WO-2026-00073"
    assert "SO-2026-00116" not in (stored.get("evidence_records") or []) and "blocked_sales_orders" not in stored
    assert "stopped_without_recorded_reason" not in stored["contributing_causes"], "WO-116's cause, not WO-73's"


def test_c5b_a_wrong_subject_let_through_is_still_not_diagnosed_for_causes():
    """With the run's one soft refusal spent, a finding about an order never looked up is stored as sent: the loop
    does not add that order's causes from a read nobody asked for."""
    book = _two_orders()
    agent = agent_on(book)
    agent._dispatch("diagnose_work_order", {"work_order": "WO-2026-00073"})
    agent._dispatch("record_finding", finding("WO-2026-00116"))
    agent._dispatch("record_finding", finding("WO-2026-00116"))

    [stored] = book.recorded_findings()
    assert stored["work_order"] == "WO-2026-00116" and stored["contributing_causes"] == []
    assert _looked_up(book, "WO-2026-00116") == []


def test_c5b_an_order_the_request_named_is_still_diagnosed_for_causes():
    book = _two_orders()
    agent = agent_on(book)
    agent.requested_orders = {"WO-2026-00116"}
    agent._examined = {"WO-2026-00116"}
    agent._dispatch("record_finding", finding("wo-2026-00116"))
    assert "stopped_without_recorded_reason" in book.recorded_findings()[0]["contributing_causes"]
