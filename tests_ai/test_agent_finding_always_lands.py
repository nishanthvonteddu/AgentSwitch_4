"""Every run that obeys its forced tool calls ends with a finding stored, unless wrong data was refused at the end.

AI-written regression tests (ungraded); the team's hand-written tests live in tests/.

The harness grades only the finding record_finding stores, so a run that ends without one scores zero however good
its work was. Four ways that happened on de456fe, each reproduced there with a model that did exactly what it was
told:

  H1  a needs_person found late: at two steps left record_finding was forced and the escalation guard refused it,
      then escalate was forced on the last step, and nothing was stored;
  H2  the token budget's single last call was spent on a forced escalate;
  H3  two different warn-once guards each refused one of the last two forced findings (and a hard refusal on the
      second-to-last step was followed by a soft one on the last);
  C4  the chain guard asked for apply_reschedule on a step after which record_finding was forced again, so the
      warning could never be acted on, and the half-moved chain then went through with nothing said about it.

Every test drives ProductionAgent.run with a scripted model over the in-memory book of test_behaviour_fakes; the
property test at the bottom runs a model that obeys every forced call and otherwise makes the worst legal choice
across thousands of combinations.

Run: python -m pytest tests_ai/test_agent_finding_always_lands.py -q
"""
import itertools
import json
from types import SimpleNamespace

import pytest
from test_behaviour_fakes import Book, finding, pin_today, work_order

from prod_agent.agent import ProductionAgent, _mistyped

A, B = "id-WO-2026-00169", "id-WO-2026-00170"
ESCALATION_ENDPOINTS = {"AgentSession.create": {"id": "sess-1"},
                        "endpoint.agent_governance.escalations.assignees": {"options": []}}
HARD_ERRORS = ("these fields have the wrong shape", "these fields cannot be null", "causes must be bare signal codes",
               "cost must be null")


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    pin_today(monkeypatch)  # 2026-09-16


# --------------------------------------------------------------- books

def _locked(**kwargs):
    """WO-2026-00048: draft with a subcontract never sent, so no date can be committed and a person must act."""
    wo = work_order("WO-2026-00048", "draft", start="2026-08-01", end="2026-08-20")
    return Book(tables={"WorkOrder": [wo], "SubcontractOrder": [
        {"id": "sco-30", "number": "SCO-2026-00030", "work_order_id": wo["id"], "status": "draft",
         "expected_delivery_date": None, "vendor_id": "v-1"}]}, endpoints=ESCALATION_ENDPOINTS, **kwargs)


def _chain():
    """WO-169 (draft, unblocked) and WO-170 (draft, consumes WO-169's item): both writable, both move."""
    return Book(tables={
        "WorkOrder": [work_order("WO-2026-00169", "draft", start="2026-09-01", end="2026-09-05"),
                      work_order("WO-2026-00170", "draft", start="2026-09-10", end="2026-09-13", bom_id="bom-170")],
        "BOM": [{"id": "bom-170", "number": "BOM-170", "materials": [{"item_id": "item-WO-2026-00169", "qty": 2}]}]},
        endpoints=ESCALATION_ENDPOINTS)


def _plain():
    return Book(tables={"WorkOrder": [work_order("WO-2026-00073", "in_progress", end="2026-09-01"),
                                      work_order("WO-2026-00116", "draft", end="2026-09-30")]},
                endpoints=ESCALATION_ENDPOINTS)


# --------------------------------------------------------------- the scripted model

class Call:
    def __init__(self, name, arguments, call_id):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=json.dumps(arguments))

    def model_dump(self, **kwargs):
        return {"id": self.id, "type": "function",
                "function": {"name": self.function.name, "arguments": self.function.arguments}}


class Message:
    def __init__(self, content=None, tool_calls=()):
        self.content, self.tool_calls = content, list(tool_calls)

    def model_dump(self, **kwargs):
        return {"role": "assistant", "content": self.content, "tool_calls": [c.model_dump() for c in self.tool_calls]}


def forced_tool(request: dict) -> str | None:
    """The one tool this request makes the model call, in whichever form _shape_choice expressed it."""
    choice = request.get("tool_choice")
    if isinstance(choice, dict):
        return choice["function"]["name"]
    if choice in ("required", "auto") and len(request["tools"]) == 1:
        return request["tools"][0]["function"]["name"]
    return None


class Obedient:
    """Obeys every forced tool call. Otherwise plays `free`, one entry per unforced step, then `then` forever.

    `record` builds the record_finding arguments for each attempt (given the attempt's index), `escalate` the escalate
    ones; `tokens` is what each step costs; `before` runs before each reply, with the step index, so a test can change
    the book or the agent mid-run; `clock` advances by `seconds` per reply when given.
    """

    def __init__(self, record, free=(), then="read", escalate=None, tokens=None, before=None, clock=None, seconds=0):
        self.record, self.free, self.then = record, list(free), then
        self.escalate = escalate or (lambda agent: {"work_order": None, "reason": "a person must act",
                                                    "reason_code": "policy_refusal"})
        self.tokens, self.before, self.clock, self.seconds = tokens, before, clock, seconds
        self.requests, self.attempts, self.agent = [], 0, None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def _call(self, name, args):
        if name == "record_finding":
            args = self.record(self.agent, self.attempts)
            self.attempts += 1
        return Message(tool_calls=[Call(name, args, f"c{len(self.requests)}")])

    def _reply(self, request):
        forced = forced_tool(request)
        if forced == "record_finding":
            return self._call("record_finding", None)
        if forced == "escalate":
            return self._call("escalate", self.escalate(self.agent))
        if self.agent.finding is not None or request.get("tool_choice") is not None:
            return Message(content="done")  # "none" after the finding, or a forced form this model reads as done
        action = self.free.pop(0) if self.free else self.then
        if action == "prose":
            return Message(content="I think that is everything.")
        if action == "record":
            return self._call("record_finding", None)
        if action == "read":
            return self._call("company_context", {})
        name, args = action
        return self._call(name, args)

    def create(self, **request):
        step = len(self.requests)
        self.requests.append(request)
        if self.before:
            self.before(self.agent, step)
        reply = self._reply(request)
        if self.clock is not None:
            self.clock[0] += self.seconds
        total = self.tokens or 0
        usage = SimpleNamespace(prompt_tokens=total // 2, completion_tokens=total - total // 2, total_tokens=total,
                                model_dump=lambda: {"total_tokens": total})
        return SimpleNamespace(choices=[SimpleNamespace(message=reply)], usage=usage)


def run(book, llm, request, events=None, **kwargs):
    agent = ProductionAgent(book, llm=llm, model="m", require_finding=True,
                            trace=(events.append if events is not None else None), **kwargs)
    llm.agent = agent
    return agent, agent.run(request)


def forced_names(llm) -> list[str | None]:
    return [forced_tool(r) for r in llm.requests]


def same(payload):
    return lambda agent, attempt: dict(payload)


# --------------------------------------------------------------- H1: a late needs_person

def test_h1_a_late_needs_person_no_longer_ends_the_run_without_a_finding():
    """de456fe: record_finding forced at two left, refused for the missing escalation, escalate forced last."""
    book = _locked()
    llm = Obedient(same(finding("WO-2026-00048", outcome="refused", is_late=None, refusal_reason="locked")),
                   free=["read", ("propose_reschedule", {"work_order": "WO-2026-00048"})])
    agent, out = run(book, llm, "Reschedule WO-2026-00048 now.", max_steps=4, escalate_mode=True)

    assert agent.needs_person, "the premise: propose_reschedule found a locked order a person must handle"
    assert forced_names(llm)[:3] == [None, None, "record_finding"]
    assert out["finding"] is not None and len(book.memories) == 1
    assert "escalations" not in out["finding"], "no escalation was raised, and none is invented for the record"


def test_h1_escalate_is_never_forced_with_fewer_than_three_steps_left():
    agent = ProductionAgent(_locked(), llm=object(), model="m", escalate_mode=True, max_steps=10)
    agent.needs_person = ["WO-2026-00048: locked"]
    agent._escalation_warned = True
    choices = {10 - step: (agent._wrap_up_choice(step) or {}).get("function", {}).get("name") for step in range(10)}
    assert choices[3] == "escalate" and choices[2] == choices[1] == "record_finding"
    assert all(name == "escalate" for left, name in choices.items() if left >= 3), "warned: forced while it fits"


# --------------------------------------------------------------- H2: the token budget's last call

def test_h2_the_last_call_past_the_token_budget_goes_to_the_finding_not_to_escalate():
    book = _locked()
    llm = Obedient(same(finding("WO-2026-00048", outcome="refused", is_late=None, refusal_reason="locked")),
                   free=[("propose_reschedule", {"work_order": "WO-2026-00048"})], tokens=11_000)
    events = []
    agent, out = run(book, llm, "Reschedule WO-2026-00048 now.", events, escalate_mode=True, token_budget=10_000)

    assert agent.needs_person and [e["type"] for e in events].count("last_call") == 1
    assert forced_names(llm) == [None, "record_finding"]
    assert out["finding"] is not None and out["stop_reason"] == "token_budget"


def test_h2_escalate_is_forced_while_the_finding_still_fits_after_it():
    """The escalate step is the last one before the finding's own token wrap-up would begin."""
    agent = ProductionAgent(_locked(), llm=object(), model="m", escalate_mode=True, token_budget=10_000)
    agent.needs_person = ["WO-2026-00048: locked"]
    agent._last_step_tokens = 2_000
    agent.tokens["total"] = 4_500   # 4.5k + 3 x 2k >= 10k, but 4.5k + 2 x 2k < 10k: escalate now or never
    assert agent._wrap_up_choice(0)["function"]["name"] == "escalate"
    agent.tokens["total"] = 6_500   # the finding and its retry are all that fit
    assert agent._wrap_up_choice(0)["function"]["name"] == "record_finding"


def test_h2_under_a_deadline_escalate_never_takes_the_finding_window():
    clock = [0.0]
    agent = ProductionAgent(_locked(), llm=object(), model="m", escalate_mode=True, deadline=100,
                            clock=lambda: clock[0])
    agent.needs_person = ["WO-2026-00048: locked"]
    agent._last_step_seconds = 10
    clock[0] = 50   # 50s left: one 10s step and the finding's 45s still fit
    assert agent._wrap_up_choice(0)["function"]["name"] == "escalate"
    clock[0] = 60   # 40s left: inside WRAP_UP_SECONDS, the finding's
    assert agent._wrap_up_choice(0)["function"]["name"] == "record_finding"


# --------------------------------------------------------------- H3: one soft refusal per run

def test_h3_two_different_soft_guards_cannot_refuse_both_final_findings():
    """de456fe: the subject guard refused at two left, the nothing-written guard at one left."""
    book = _locked()
    book.tables["WorkOrder"].append(work_order("WO-2026-00049", "in_progress"))
    # About an order the run never looked up, and 'answered' when nothing could be written: two soft guards.
    payload = finding("WO-2026-00049", blocking_causes=["subcontract_not_sent"])
    llm = Obedient(same(payload), free=[("propose_reschedule", {"work_order": "WO-2026-00048"})])
    events = []
    _, out = run(book, llm, "Reschedule WO-2026-00048 and write it now.", events, max_steps=3, apply_mode=True,
                 approve=lambda p: True)

    assert forced_names(llm) == [None, "record_finding", "record_finding"]
    assert out["finding"] is not None and out["finding"]["work_order"] == "WO-2026-00049"
    assert out["finding"]["outcome"] == "answered", "stored as sent: the soft guards ask, they do not rewrite"
    waived = [e for e in events if e["type"] == "guard_waived"]
    assert [e["guard"] for e in waived] == ["subject", "nothing written"]
    assert all("subject guard already sent" in e["why"] for e in waived)


def test_h3_a_hard_refusal_at_two_left_still_leaves_the_last_step_to_the_finding():
    """de456fe: wrong shape refused at two left, then the escalation guard refused the last one."""
    book = _locked()
    good = finding("WO-2026-00048", outcome="refused", is_late=None, refusal_reason="locked")

    def record(agent, attempt):
        return {**good, "is_late": "yes"} if attempt == 0 else dict(good)

    llm = Obedient(record, free=["read", ("propose_reschedule", {"work_order": "WO-2026-00048"})])
    agent, out = run(book, llm, "Reschedule WO-2026-00048 now.", max_steps=4, escalate_mode=True)

    assert agent.needs_person and forced_names(llm)[2:] == ["record_finding", "record_finding"]
    assert out["finding"] is not None and out["finding"]["is_late"] is None


def test_h3_soft_guards_never_refuse_on_the_last_step():
    """de456fe: a wrong shape refused at two left, then the subject guard refused the last attempt."""
    book = _plain()

    def record(agent, attempt):
        return finding("WO-2026-00073", is_late="yes") if attempt == 0 else finding("WO-2026-00116")

    llm = Obedient(record, free=[("diagnose_work_order", {"work_order": "WO-2026-00073"})])
    events = []
    _, out = run(book, llm, "What does WO-2026-00073 block?", events, max_steps=3)

    # The forced finding at one left names an order never looked up; there is no step left to fix it in.
    assert forced_names(llm) == [None, "record_finding", "record_finding"]
    assert out["finding"]["work_order"] == "WO-2026-00116"
    assert [(e["guard"], e["why"]) for e in events if e["type"] == "guard_waived"] == \
        [("subject", "no room left for what it would ask")]


def test_h3_the_one_soft_refusal_is_still_given_while_there_is_room():
    book = _plain()
    good = finding("WO-2026-00073")
    llm = Obedient(lambda agent, attempt: finding("WO-2026-00116") if attempt == 0 else dict(good),
                   free=[("diagnose_work_order", {"work_order": "WO-2026-00073"}), "record"])
    _, out = run(book, llm, "What does WO-2026-00073 block?", max_steps=6)

    assert llm.attempts == 2 and out["finding"]["work_order"] == "WO-2026-00073"


# --------------------------------------------------------------- C4: the chain guard asks only what can be done

def _chain_run(max_steps, record, free_after=(), **kwargs):
    book = _chain()
    llm = Obedient(record, free=[("propose_reschedule", {"work_order": "WO-2026-00169"}),
                                 ("apply_reschedule", {"work_order_id": A}), *free_after])
    events = []
    agent, out = run(book, llm, "Reschedule WO-2026-00169 and everything after it, and write it.", events,
                     max_steps=max_steps, apply_mode=True, approve=lambda p: True, allowed_write_ids={A, B}, **kwargs)
    return book, llm, events, out


def test_c4_on_a_forced_step_the_chain_guard_records_the_unapplied_write_instead_of_asking_for_it():
    """de456fe refused the forced finding at two left with "call apply_reschedule", then forced record_finding."""
    book, llm, events, out = _chain_run(4, same(finding("WO-2026-00169")))

    assert forced_names(llm)[:3] == [None, None, "record_finding"]
    assert llm.attempts == 1, "nothing was asked that the next, forced, step could not do"
    assert [(r["number"], r["outcome"]) for r in out["finding"]["rescheduled"]] == \
        [("WO-2026-00169", "applied"), ("WO-2026-00170", "not_applied")]
    assert book.recorded_findings()[0]["rescheduled"][1] == {
        "number": "WO-2026-00170", "new_start": "2026-09-21", "new_end": "2026-09-24", "outcome": "not_applied"}


@pytest.mark.parametrize("mode", ["named", "required", "auto"])
def test_c4_with_room_the_chain_guard_asks_and_the_next_step_can_apply(mode):
    book, llm, events, out = _chain_run(8, same(finding("WO-2026-00169")),
                                        free_after=["record", ("apply_reschedule", {"work_order_id": B}), "record"],
                                        forced_tool_choice=mode)

    assert forced_tool(llm.requests[3]) is None
    assert "apply_reschedule" in [t["function"]["name"] for t in llm.requests[3]["tools"]]
    assert [(r["number"], r["outcome"]) for r in out["finding"]["rescheduled"]] == \
        [("WO-2026-00169", "applied"), ("WO-2026-00170", "applied")]


def test_c4_a_due_escalation_needs_one_more_free_step_before_the_chain_guard_asks():
    """With an escalation due, wrap-up forces escalate at three left; asking for apply at four left would lose it."""
    agent = ProductionAgent(_chain(), llm=object(), model="m", apply_mode=True, escalate_mode=True, max_steps=8)
    agent._step = 4   # three after this one: the next has three left, where a due escalation is forced
    assert agent._free_steps_fit(1) and not agent._free_steps_fit(2)
    agent._step = 3   # four after this one: free, escalate, record_finding, retry
    assert agent._free_steps_fit(2)


# --------------------------------------------------------------- the property

def _scenario(name):
    """(book, request, setup steps, apply-mode kwargs, the order the finding is about)."""
    if name == "chain":
        return (_chain(), "Reschedule WO-2026-00169 and its dependants and write it now.",
                [("propose_reschedule", {"work_order": "WO-2026-00169"}), ("apply_reschedule", {"work_order_id": A})],
                {"apply_mode": True, "approve": lambda p: True, "allowed_write_ids": {A, B}}, "WO-2026-00169")
    if name == "locked":
        return (_locked(), "Reschedule WO-2026-00048 and write it now.",
                [("propose_reschedule", {"work_order": "WO-2026-00048"})],
                {"apply_mode": True, "approve": lambda p: True}, "WO-2026-00048")
    return (_plain(), "What does WO-2026-00073 block?", [("diagnose_work_order", {"work_order": "WO-2026-00073"})],
            {}, "WO-2026-00073")


def _combinations():
    for max_steps in range(3, 9):
        for escalate_mode in (False, True):
            for needs_at in ((None,) + tuple(range(max_steps)) if escalate_mode else (None,)):
                for scenario, free, wrong_subject, hard, mode in itertools.product(
                        ("chain", "locked", "plain"), ("read", "record", "prose"), (False, True),
                        ("never", "first", "always"), ("named", "required")):
                    yield max_steps, escalate_mode, needs_at, scenario, free, wrong_subject, hard, mode


def _one(max_steps, escalate_mode, needs_at, scenario, free, wrong_subject, hard, mode, **budget):
    book, request, setup, kwargs, subject = _scenario(scenario)
    # Worst legal choices: the finding names an order never looked up (when wrong_subject), says 'answered' however
    # little was written, never mentions the chain or the escalation, and is otherwise the same on every retry.
    payload = finding("WO-2026-00116" if wrong_subject else subject)

    def record(agent, attempt):
        body = {**payload, "escalations": list(agent.escalations)}
        wrong = hard == "always" or (hard == "first" and attempt == 0)
        return {**body, "is_late": "yes"} if wrong else body  # wrong data: a hard guard must refuse it

    def before(agent, step):
        if step == needs_at:
            agent.needs_person.append("a person must act")

    llm = Obedient(record, free=setup, then=free, before=before, **budget.pop("llm", {}))
    events = []
    agent, out = run(book, llm, request, events, max_steps=max_steps, escalate_mode=escalate_mode,
                     forced_tool_choice=mode, **kwargs, **budget)
    return agent, out, llm, events


def _check(agent, out, llm, events, label):
    tools = [e for e in events if e["type"] == "tool"]
    for e in events:
        if e["type"] == "wrap_up" and forced_tool({"tool_choice": e["tool_choice"], "tools": []}) == "escalate":
            assert agent.max_steps - e["step"] >= 3, f"{label}: escalate forced with fewer than three steps left"
    if out["finding"] is not None:
        assert _mistyped(out["finding"]) == [], f"{label}: stored in the wrong shape: {out['finding']}"
        return
    attempts = [e for e in tools if e["name"] == "record_finding"]
    assert attempts, f"{label}: the run ended without a single record_finding attempt"
    last = attempts[-1]
    assert isinstance(last["result"], dict) and last["result"].get("error") in HARD_ERRORS, \
        f"{label}: no finding, and the last attempt was not refused for wrong data: {last['result']}"
    assert last["step"] == max(e["step"] for e in events if e["type"] == "llm"), \
        f"{label}: wrong data was refused before the final step and no retry followed"


def test_property_an_obedient_model_always_ends_with_a_finding():
    seen = 0
    for combo in _combinations():
        agent, out, llm, events = _one(*combo)
        _check(agent, out, llm, events, combo)
        seen += 1
        hard = combo[6]
        # An obedient model makes its first attempt by two steps left at the latest, so wrong data sent once is
        # always followed by a forced retry; only wrong data on every attempt may leave nothing stored (a fixer can
        # still mend it: an answer about an order that proves not to exist is filed as a refusal, is_late null).
        assert out["finding"] is not None or hard == "always", combo
    assert seen == 45 * 108


@pytest.mark.parametrize("budget", [6_000, 9_000, 12_000, 15_000, 20_000])
def test_property_under_a_token_budget(budget):
    for needs_at, scenario, free, hard in itertools.product((None, 0, 1, 2, 3), ("chain", "locked", "plain"),
                                                            ("read", "record", "prose"), ("never", "first", "always")):
        combo = (20, True, needs_at, scenario, free, True, hard, "named")
        agent, out, llm, events = _one(*combo, token_budget=budget, llm={"tokens": 3_000})
        _check(agent, out, llm, events, (budget, *combo))
        assert out["finding"] is not None or hard == "always", (budget, combo)


@pytest.mark.parametrize("deadline", [40, 60, 80, 100, 130])
def test_property_under_a_deadline(deadline):
    for needs_at, scenario, free, hard in itertools.product((None, 0, 1, 2, 3), ("chain", "locked", "plain"),
                                                            ("read", "record", "prose"), ("never", "first", "always")):
        clock = [0.0]
        combo = (20, True, needs_at, scenario, free, True, hard, "named")
        agent, out, llm, events = _one(*combo, deadline=deadline, clock=lambda c=clock: c[0],
                                       llm={"clock": clock, "seconds": 10})
        _check(agent, out, llm, events, (deadline, *combo))
        assert out["finding"] is not None or hard == "always", (deadline, combo)
