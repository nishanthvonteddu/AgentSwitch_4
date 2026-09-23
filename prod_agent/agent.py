"""The agent loop. No framework: messages in, tool calls out, every event traced."""
import json
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent import futures

from . import config, domain
from .mcp_client import McpClient, McpError

SYSTEM_PROMPT = """You are the Production agent for a manufacturing company on the AgentSwitch platform.
Your seat owns work orders, BOMs, routings and job cards. You answer planners' questions from live data.

How you work:
- Use tools for every fact. Never state a number, date, record, customer or cause you did not get from a tool result in this conversation.
- Cite record numbers (WO-..., SCO-..., MR-..., SO-...) for every claim.
- Call company_context first. Use its currency and country; never assume a country, tax regime or currency.
- Other teams change this data while you run. Proposals carry a snapshot; apply_reschedule re-reads before writing.
- For "why is it late": run diagnose_work_order. Separate BLOCKING causes (no known ready date) from contributing ones. Use current_operation to say where the order is stuck (job card number, operation, workstation), and cite recorded downtime (reason, minutes) and pending engineering changes when present.
- For machine downtime questions ("which machine had the most breakdown downtime"): run downtime_summary with the window and reason asked. Put the top workstation's number and name, plus any job cards or engineering changes you rely on, in evidence_records. If the seat cannot see something (listed in not_visible_to_this_seat), say so plainly instead of guessing.
- For "what does it block": run downstream_impact. Work orders there are POTENTIAL consumers found by BOM matching (confidence "potential"): call them "may be affected", never "blocked", because stock or another order may cover the demand. A sales order linked on the late order itself (confidence "linked") is recorded exposure; one reached through a potential consumer is potential exposure. Report customer, delivery date and value. Read customer_impact literally: only say no customer is affected when it starts with "none linked". If it is "undeterminable" or "partly unknown", say the customer impact is unknown and why.
- For "reschedule": run propose_reschedule. Only proposals with writable_by_seat=true can be written, and only if apply_reschedule is available. Everything else is a recommendation that needs a person (explain why_not_writable).
- If apply_reschedule returns changed_underneath, someone else edited that order during this run. Do not retry or re-propose: every further write in this run is refused. Report the conflict, and escalate if escalate is available.
- Escalation: when something needs a person (a locked order, a blocker with no known date, data this seat cannot see, a conflicting edit) and the escalate tool is available, call escalate ONCE for the request with: the work order, records checked, what is missing, and the action requested. Record the result in escalations. If it returns raised=false (for example no assignee), say plainly that no one could be assigned and who should be contacted; never claim an escalation that was not raised.
- Do not accept a false premise. If the user says an order is late but diagnose_work_order shows is_late=false, say it is not past due (record is_late=false) and still report what is holding it.
- Costs: expected_cost and actual_cost are on the work order (diagnose_work_order). Variance = actual - expected, in the company currency. If both are 0 or missing, no cost has been recorded: say so and do not compute a variance (cost=null, outcome partial or refused).
- Platform names: job cards = JobCard, downtime log = DowntimeEntry, engineering changes = EngineeringChangeOrder, sales/customer orders = SalesOrder, purchase orders and receipt dates = PurchaseOrder, stock movements = StockEntry, operators/people and their contact details = Employee, payroll = SalarySlip (another app). For anything else call seat_entities. Before refusing for lack of access, confirm with seat_capability on the REAL entity, and put that exact entity name in not_visible. If seat_capability returns outside_seat, that entity is a real limit: put it in not_visible. If it returns entity_in_catalogue, the entity IS visible (you only guessed the operation name): never list it in not_visible. If it returns a warning, you used a wrong name: retry with a real one.
- Refuse when the data cannot support an answer or the seat is not permitted: the work order does not exist, the entity is not visible, or the requested change is outside this seat (for example changing a sales order delivery date, cancelling a work order, or reading payroll). Refusing correctly is a success, not a failure.
- Before your final answer you MUST call record_finding exactly once with the structured result, including outcome "refused" when you refuse.
Final answer: short, plain language, sections Why late / What it blocks / Rescheduling / Not visible to me."""

RECORD_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["answered", "partial", "refused"],
                    "description": "answered = every figure the user asked for was given from data. "
                                   "partial = some asked-for figure could not be given (not recorded, or not visible to this seat). "
                                   "refused = none of the request could be answered or done."},
        "work_order": {"type": ["string", "null"], "description": "WO number the request was about, if any"},
        "is_late": {"type": ["boolean", "null"], "description": "is_late from diagnose_work_order; null if not diagnosed"},
        "currency": {"type": ["string", "null"], "description": "currency from company_context"},
        "cost": {"type": ["object", "null"], "description": "only when cost data exists",
                 "properties": {"expected": {"type": "number"}, "actual": {"type": "number"}, "variance": {"type": "number"}}},
        "blocking_causes": {"type": "array", "items": {"type": "string"}, "description": "signal codes that block the order"},
        "contributing_causes": {"type": "array", "items": {"type": "string"}},
        "evidence_records": {"type": "array", "items": {"type": "string"}, "description": "record numbers cited"},
        "potentially_blocked_work_orders": {"type": "array", "items": {"type": "string"},
                                            "description": "possible consumers found by BOM matching; not confirmed blocks"},
        "blocked_sales_orders": {"type": "array", "items": {"type": "string"}},
        "rescheduled": {"type": "array", "items": {"type": "object", "properties": {
            "number": {"type": "string"}, "new_start": {"type": ["string", "null"]},
            "new_end": {"type": ["string", "null"]}, "outcome": {"type": "string"}}, "required": ["number", "outcome"]}},
        "customer_impact": {"type": "string", "description": "customer_impact value from downstream_impact, verbatim"},
        "not_visible": {"type": "array", "items": {"type": "string"}},
        "escalations": {"type": "array", "description": "result of each escalate call, as returned", "items": {"type": "object", "properties": {
            "raised": {"type": "boolean"}, "number": {"type": ["string", "null"]}, "assignee": {"type": ["string", "null"]},
            "reason_code": {"type": ["string", "null"]}}, "required": ["raised"]}},
        "refusal_reason": {"type": ["string", "null"]},
    },
    "required": ["outcome", "work_order", "is_late", "currency", "blocking_causes", "evidence_records",
                 "potentially_blocked_work_orders", "blocked_sales_orders", "rescheduled", "not_visible", "refusal_reason"],
}


def _nulled_required(args: dict) -> list[str]:
    """Required fields the model sent as null where its own schema does not allow null.

    Only fields it actually sent: a caller may record a partial finding (tests/test_production.py:757), so an
    absent field is the caller's choice. A field sent as null contradicts the type the model was handed, which
    is the failure this catches. Driven by RECORD_FINDING_SCHEMA rather than a second list, so a field added
    there is covered here.
    """
    properties = RECORD_FINDING_SCHEMA["properties"]
    nulled = []
    for field in RECORD_FINDING_SCHEMA["required"]:
        if args.get(field, "") is not None:
            continue
        declared = properties.get(field, {}).get("type")
        if not ("null" in declared if isinstance(declared, list) else declared == "null"):
            nulled.append(field)
    return nulled


def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": required or []}}}


WO_REF = {"work_order": {"type": "string", "description": "Work order number (WO-YYYY-NNNNN) or id"}}


class ProductionAgent:
    def __init__(self, mcp: McpClient, *, model: str | None = None, run_id: str | None = None,
                 apply_mode: bool = False, approve: Callable[[dict], bool] | None = None,
                 allowed_write_ids: set[str] | None = None, trace: Callable[[dict], None] | None = None,
                 max_steps: int = 20, llm=None, escalate_mode: bool = False, session_title: str | None = None,
                 max_escalations: int = 1):
        self.mcp = mcp
        self.provider = (config.env("LLM_PROVIDER", "openai") or "openai").strip().lower()
        if self.provider == "openai":
            self.model = model or config.env("OPENAI_MODEL", "gpt-4.1")
        elif self.provider == "openrouter":
            self.model = model or config.env("OPENROUTER_MODEL")
            if not self.model:
                raise RuntimeError("Set OPENROUTER_MODEL in .env")
        else:
            raise RuntimeError(f"Unknown LLM_PROVIDER {self.provider!r}; accepted values are openai and openrouter")
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.apply_mode = apply_mode
        self.approve = approve or (lambda proposal: False)
        self.allowed_write_ids = allowed_write_ids
        self.trace = trace or (lambda event: None)
        self.max_steps = max_steps
        self._proposals: dict[str, dict] = {}
        self.escalate_mode = escalate_mode
        self.session_title = session_title or f"team04 production agent run {self.run_id}"
        self.max_escalations = max_escalations
        self.session_id: str | None = None
        self.escalations: list[dict] = []
        self.conflicts: list[str] = []  # work orders someone else changed during this run
        self.needs_person: list[str] = []  # evidence from tool results that a person must act
        self.finding: dict | None = None
        self.finding_record: dict | None = None
        self._seen_calls: dict[tuple[str, str], int] = {}
        # Reads REPEAT_GUARDED already promises are "still current" for the rest of the run, kept so
        # propose_reschedule does not re-run a diagnosis and a BOM walk the model has already paid for.
        self._reads: dict[tuple[str, str], dict] = {}
        if llm is None:
            from openai import OpenAI
            # The SDK backs off on 429/5xx; the default 2 retries is too few for a 30k tokens-per-minute key.
            if self.provider == "openai":
                llm = OpenAI(api_key=config.env("OPENAI_API_KEY"), max_retries=8)
            else:
                api_key = config.env("OPENROUTER_API_KEY")
                if not api_key:
                    raise RuntimeError("Set OPENROUTER_API_KEY in .env")
                llm = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1", max_retries=8)
        self.llm = llm

    # ------------------------------------------------------------------ tools
    def tool_specs(self) -> list[dict]:
        specs = [
            _fn("company_context", "Company name, country and currency for this book."),
            _fn("list_late_work_orders", "Open work orders past their planned end date or projected late."),
            _fn("diagnose_work_order", "Evidence for why a work order is late: subcontracts, material requests, stock, quality, workstations, schedule.", WO_REF, ["work_order"]),
            _fn("downstream_impact", "Open work orders that consume this order's output (via BOMs) and the sales orders affected.", WO_REF, ["work_order"]),
            _fn("propose_reschedule", "Proposed new dates for the order and its dependants, with whether this seat may write each.", WO_REF, ["work_order"]),
            _fn("downtime_summary", "Recorded downtime per workstation over the last N days, from downtime entries, optionally for one reason.",
                {"days": {"type": "integer", "minimum": 1, "maximum": 366},
                 "reason": {"type": ["string", "null"], "enum": ["breakdown", "planned_maintenance", "setup_change", "material_shortage",
                                                               "power_failure", "quality_issue", "tool_change", "operator_unavailable",
                                                               "other", None]}}, ["days"]),
            _fn("seat_entities", "Exact entity names in this seat's tool catalogue. Use these names; never invent one."),
            _fn("seat_capability", "Check whether this seat can use a platform tool, e.g. 'SalesOrder.update', 'DowntimeEntry.list', 'SalarySlip.list'.",
                {"tool": {"type": "string"}}, ["tool"]),
            _fn("record_finding", "Persist the structured result. Call exactly once, before the final answer.",
                RECORD_FINDING_SCHEMA["properties"], RECORD_FINDING_SCHEMA["required"]),
        ]
        if self.apply_mode:
            specs.append(_fn("apply_reschedule", "Write one proposal returned by propose_reschedule (needs human approval).",
                             {"work_order_id": {"type": "string"}}, ["work_order_id"]))
        if self.escalate_mode:
            specs.append(_fn("escalate", "Hand this request to a person via the platform escalation queue. Call at most once.",
                             {"work_order": {"type": ["string", "null"]},
                              "reason": {"type": "string", "description": "work order, records checked, what is missing, action requested"},
                              "reason_code": {"type": "string", "enum": list(domain.ESCALATION_REASON_CODES)}},
                             ["reason", "reason_code"]))
        return specs

    # Reads whose answer cannot usefully change within one run. propose_reschedule is left out on purpose:
    # re-proposing refreshes updated_at in a shared book.
    REPEAT_GUARDED = {"company_context", "list_late_work_orders", "diagnose_work_order", "downstream_impact",
                      "downtime_summary", "seat_entities", "seat_capability"}

    def _repeat_note(self, name: str, args: dict, step: int) -> dict | None:
        """Stop the model looping on an identical read (seen live: 9 identical seat_capability calls)."""
        if name not in self.REPEAT_GUARDED:
            return None
        key = (name, json.dumps(args, sort_keys=True))
        if key not in self._seen_calls:
            self._seen_calls[key] = step
            return None
        return {"repeat": True,
                "detail": f"{name} was already called with these arguments at step {self._seen_calls[key]}; "
                          "that result above is still current. Do not call it again.",
                "instruction": "if the answer needs a record this seat cannot see, say so; "
                               "otherwise use what you have and call record_finding"}

    def _wrap_up_choice(self, step: int):
        """Force the finding into the database before the step budget runs out."""
        remaining = self.max_steps - step
        if self.finding is not None:
            return "none" if remaining == 1 else None
        escalation_due = self.escalate_mode and self.needs_person and not self.escalations
        if escalation_due and remaining == 3:
            return {"type": "function", "function": {"name": "escalate"}}
        if remaining <= 2:
            return {"type": "function", "function": {"name": "record_finding"}}
        return None

    def _dispatch(self, name: str, args: dict):
        ref = args.get("work_order")
        if name == "company_context":
            return domain.company_context(self.mcp)
        if name == "list_late_work_orders":
            rows = domain.list_late_work_orders(self.mcp)
            return {"count": len(rows), "work_orders": rows[:40], "truncated": len(rows) > 40}
        if name == "diagnose_work_order":
            self._reads["diagnose", ref] = result = domain.diagnose(self.mcp, ref)
            return result
        if name == "downstream_impact":
            self._reads["downstream", ref] = result = domain.downstream_impact(self.mcp, ref)
            return result
        if name == "propose_reschedule":
            result = domain.propose_reschedule(self.mcp, ref, diag=self._reads.get(("diagnose", ref)),
                                               down=self._reads.get(("downstream", ref)))
            for p in result.get("proposals", []):
                self._proposals[p["work_order_id"]] = p
                if not p.get("writable_by_seat") and p["number"] == (result.get("work_order") or {}).get("number"):
                    self.needs_person.append(f"{p['number']}: {p.get('why_not_writable')}")
            return result
        if name == "downtime_summary":
            return domain.downtime_summary(self.mcp, args.get("days", 30), args.get("reason"))
        if name == "seat_entities":
            return {"entities": domain.seat_entities(self.mcp)}
        if name == "seat_capability":
            return domain.seat_capability(self.mcp, args["tool"])
        if name == "apply_reschedule" and self.apply_mode:
            if self.conflicts:
                return {"outcome": "refused", "detail": f"not retrying: {', '.join(self.conflicts)} changed underneath this run; "
                        "state must be re-planned by a person"}
            p = self._proposals.get(args["work_order_id"])
            if not p:
                return {"outcome": "refused", "detail": "no proposal for that id in this run; call propose_reschedule first"}
            approved = self.approve(p)
            self.trace({"type": "approval", "work_order": p["number"], "approved": approved})
            if not approved:
                return {"outcome": "not_approved", "number": p["number"]}
            result = domain.apply_proposal(self.mcp, p, self.allowed_write_ids)
            if result.get("outcome") == "changed_underneath":
                self.conflicts.append(p["number"])
                self.needs_person.append(f"{p['number']}: changed by someone else during this run")
            return result
        if name == "escalate" and self.escalate_mode:
            if len(self.escalations) >= self.max_escalations:
                return {"raised": False, "reason_code": "limit", "detail": "already escalated in this run"}
            ref_text = f"[{args['work_order']}] " if args.get("work_order") else ""
            try:
                if self.session_id is None:
                    self.session_id = domain.open_agent_session(self.mcp, self.session_title)
                result = domain.raise_escalation(self.mcp, self.session_id, ref_text + args["reason"],
                                                 args.get("reason_code", "policy_refusal"))
            except Exception as e:
                # A throwing escalation still has to land in self.escalations. Without this the loop's own
                # handler returns the error without appending, record_finding's guard below then refuses every
                # remaining call, and the run ends with no finding in the database at all.
                self.trace({"type": "exception", "tool": "escalate", "traceback": traceback.format_exc()})
                result = {"raised": False, "reason_code": "escalation_failed", "detail": str(e)}
            self.escalations.append(result)
            return result
        if name == "record_finding":
            if self.finding is not None:
                return {"error": "finding already recorded for this run"}
            nulled = _nulled_required(args)
            if nulled:
                # The schema is sent to the model but was never checked on the way back, so a model that
                # sent null for a non-nullable field (seen live 2026-09-22: outcome=null on a refusal, which
                # the verifier scored revise) had that gap persisted and the run reported success.
                return {"error": "these fields cannot be null", "fields": nulled,
                        "instruction": "call record_finding again with a real value for each listed field"}
            cost = args.get("cost") or {}
            if cost and not (cost.get("expected") or cost.get("actual")):
                return {"error": "cost must be null when no cost is recorded (expected and actual are 0 or missing)",
                        "instruction": "record cost=null, say no cost has been recorded, and use outcome partial or refused"}
            if self.escalate_mode and self.needs_person and not self.escalations:
                # Guardrail, like record_finding itself: a handover a person must act on is not optional.
                return {"error": "escalation required before recording the finding",
                        "needs_person": self.needs_person,
                        "instruction": "call escalate once (work order, records checked, what is missing, action "
                                       "requested), then call record_finding with its result in escalations"}
            self.finding = args
            self.finding_record = domain.record_finding(self.mcp, self.run_id, args)
            return self.finding_record
        return {"error": f"unknown tool {name}"}

    # Reads that touch no run-critical state, so several of them can be in flight at once. propose_reschedule
    # qualifies: it only ever appends to _proposals and needs_person. The writes (apply_reschedule, escalate,
    # record_finding) stay serial — each gates on state the others must not race.
    PARALLEL_SAFE = {"company_context", "list_late_work_orders", "diagnose_work_order", "downstream_impact",
                     "propose_reschedule", "downtime_summary", "seat_entities", "seat_capability"}
    MAX_PARALLEL = 8

    def _invoke(self, call, note: dict | None) -> tuple[dict, str | None, float, str | None]:
        """Run one tool call. Traces and messages are left to the caller so a batch of these can run in threads."""
        t0, tb = time.time(), None
        try:
            args = json.loads(call.function.arguments or "{}")
            result, error = note or self._dispatch(call.function.name, args), None
        except McpError as e:
            result, error = {"error": e.message, "kind": e.kind}, e.kind
        except Exception as e:  # tool bugs must not kill the run; the caller traces tb
            result, error, tb = {"error": str(e)}, "exception", traceback.format_exc()
        return result, error, round(time.time() - t0, 2), tb

    def _invoke_batch(self, calls, notes) -> list[tuple]:
        """Run one batch of tool calls, overlapping adjacent independent reads. Order of results is the order asked."""
        groups: list[tuple[bool, list[int]]] = []
        for i, call in enumerate(calls):
            parallel = call.function.name in self.PARALLEL_SAFE
            if parallel and groups and groups[-1][0]:
                groups[-1][1].append(i)
            else:
                groups.append((parallel, [i]))
        results: list[tuple | None] = [None] * len(calls)
        for parallel, idxs in groups:
            if parallel and len(idxs) > 1:
                self.mcp.tool_names()  # warm the catalogue once here, not once per worker
                with futures.ThreadPoolExecutor(max_workers=min(len(idxs), self.MAX_PARALLEL)) as pool:
                    for i, out in zip(idxs, pool.map(lambda j: self._invoke(calls[j], notes[j]), idxs)):
                        results[i] = out
            else:
                for i in idxs:
                    results[i] = self._invoke(calls[i], notes[i])
        return results

    # ------------------------------------------------------------------ loop
    def run(self, request: str) -> dict:
        started = time.time()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": request}]
        self.trace({"type": "start", "run_id": self.run_id, "instance": self.mcp.session.instance,
                    "provider": self.provider, "model": self.model, "apply_mode": self.apply_mode, "request": request})
        final, stop_reason = None, "max_steps"
        llm_error = None
        for step in range(self.max_steps):
            # Strip a provider namespace so OpenRouter's OpenAI model ids retain deterministic sampling.
            sampling_model = self.model.rpartition("/")[2]
            sampling = {"temperature": 0} if sampling_model.startswith(("gpt-4", "gpt-3")) else {}
            choice = self._wrap_up_choice(step)
            if choice is not None:
                sampling["tool_choice"] = choice
                self.trace({"type": "wrap_up", "step": step, "tool_choice": choice})
            tools = self.tool_specs()
            try:
                resp = self.llm.chat.completions.create(model=self.model, messages=messages,
                                                        tools=tools, **sampling)
                msg = resp.choices[0].message
                usage = getattr(resp, "usage", None)
                llm_event = {"type": "llm", "step": step, "content": msg.content,
                             "tool_calls": [{"id": c.id, "name": c.function.name,
                                             "arguments": c.function.arguments}
                                            for c in (msg.tool_calls or [])],
                             "usage": usage.model_dump() if usage else None}
                dumped_message = msg.model_dump(exclude_none=True)
            except Exception:
                # Verifiers grade the database, so a recorded finding must still be scored after an LLM failure.
                llm_error = traceback.format_exc()
                stop_reason = "llm_error"
                break
            self.trace(llm_event)
            messages.append(dumped_message)
            if not msg.tool_calls:
                final, stop_reason = msg.content, "final_answer"
                break
            # The repeat guard stays serial and in order: its "already called at step N" answer depends on what
            # came earlier in this same batch. Bad JSON is left for _invoke, where the existing handler traces it.
            notes = []
            for call in msg.tool_calls:
                try:
                    notes.append(self._repeat_note(call.function.name,
                                                   json.loads(call.function.arguments or "{}"), step))
                except ValueError:
                    notes.append(None)
            results = self._invoke_batch(msg.tool_calls, notes)
            for call, (result, error, seconds, tb) in zip(msg.tool_calls, results):
                if tb is not None:
                    self.trace({"type": "exception", "tool": call.function.name, "traceback": tb})
                self.trace({"type": "tool", "step": step, "name": call.function.name,
                            "arguments": call.function.arguments, "error": error,
                            "seconds": seconds, "result": result})
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result, default=str)[:60000]})
        outcome = {"run_id": self.run_id, "stop_reason": stop_reason, "final_answer": final,
                   "finding": self.finding, "finding_record": self.finding_record,
                   "escalations": self.escalations, "agent_session_id": self.session_id, "conflicts": self.conflicts,
                   "seconds": round(time.time() - started, 1)}
        end_event = {"type": "end", **outcome}
        if llm_error is not None:
            end_event["error"] = llm_error
        self.trace(end_event)
        return outcome
