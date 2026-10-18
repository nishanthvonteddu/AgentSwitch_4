# Architecture

How the Production agent and its harness are put together, and where to change things.

The whole system exists to answer one request well:

> "This work order is late. Find out why, tell me what it blocks downstream, and reschedule what you can."

Two halves. `prod_agent/` answers the question. `harness/` proves the answer was right, by reading the
database rather than the agent's prose.

---

## 1. The shape

```
  your question
       │
  ┌────▼─────────────────────────────────────────┐
  │  the loop            prod_agent/agent.py     │   talk to the model, run its tool calls, repeat
  │  ~20 steps max                               │   independent reads in one batch run together
  ├──────────────────────────────────────────────┤
  │  17 tools (+2)       prod_agent/agent.py     │   the ONLY things the model may ask for
  ├──────────────────────────────────────────────┤
  │  domain logic        prod_agent/domain.py    │   the real work. No LLM in this file.
  ├──────────────────────────────────────────────┤
  │  transport           prod_agent/mcp_client.py│   MCP (JSON-RPC) + an independent REST client
  └────┬─────────────────────────────────────────┘
       │
   AgentSwitch
```

**The model never touches the platform.** It cannot build a URL, call an endpoint, or invent a record
number. It can only choose one of the tools below and fill in its arguments. Everything factual happens in
`domain.py`, which is ordinary Python.

Think of the model as a shift supervisor who may only submit request slips. `domain.py` is the clerk who
walks to the filing cabinet. This is why the 39 tests in `tests/` run without a model at all, and why a
wrong answer is nearly always a `domain.py` bug rather than a prompting problem.

The model's actual job is narrow: decide which question to ask next, and write the closing summary.

---

## 2. The tools

Defined in `ProductionAgent.tool_specs()`: 17 always offered, 2 more when switched on. Each maps to a
function in `domain.py`.

| tool | what it does |
|---|---|
| `company_context` | company, country, currency — called first, so nothing is hardcoded to India |
| `list_late_work_orders` | open orders past their planned end date |
| `diagnose_work_order` | the big one: why is this order late (see §3) |
| `downstream_impact` | who consumes this order's output, and which sales orders are exposed |
| `propose_reschedule` | new dates, plus whether this seat is even allowed to write each one |
| `downtime_summary` | recorded downtime per workstation over N days, optionally one reason |
| `capacity_outlook` | shop load over a horizon: the capacity board's own figure beside the work content in the same job cards |
| `order_feasible_by` | can one order's remaining work credibly finish by a date: work content, blockers and shop load |
| `shop_floor_exceptions` | where the shop floor is stuck, by lane: shortages, quality, subcontract and automation blocks |
| `seat_entities` | the real entity names in this seat's catalogue |
| `seat_capability` | can this seat use `SalesOrder.update`? distinguishes a real limit from a typo |
| `seat_policy_conformance` | the agent policy the platform declares for this seat, and where the seat's observed reach differs |
| `find_orders_for_item` | the open work orders that make an item, late ones first, when a request names the item not the order |
| `where_is_item_used` | which BOMs consume an item and which BOM produces it |
| `query_group` | count records of one entity by a field, across every matching record |
| `query_records` | fallback read: rows of one entity this seat may see, with the total beside them |
| `record_finding` | persist the structured conclusion. Mandatory, exactly once. |

Two more appear only when switched on:

| tool | appears when |
|---|---|
| `apply_reschedule` | `--apply` / `"mode": "apply"` |
| `escalate` | `--escalate` / `"escalate": true` |

Adding a tool means adding a `_fn(...)` spec **and** a branch in `_dispatch_tool()` (called by `_dispatch()`).
Forgetting the second gives `unknown tool`.

---

## 3. How `diagnose` decides

`domain.diagnose()` gathers job cards, subcontracts, material requests, stock, quality inspections,
engineering changes, downtime and the platform's own `finite_schedule`, and emits a flat list of
**signals**. Each signal is `{code, blocking, record, ...detail}`.

The single most important line in the codebase is the blocking test:

```python
UNDATED_BLOCKERS = {
    "subcontract_not_sent", "subcontract_overdue", "material_shortage",
    "quality_rejected", "workstation_unavailable",
    "engineering_change_pending", "workstation_breakdown_active",
}
```

A cause is **blocking** if it has no knowable ready date. Everything else is **contributing**. That
distinction is what lets the agent say "I will not commit a new date" honestly, instead of inventing one.
If you add a new cause code, decide which side of this set it belongs on — that decision is the behaviour.

Two things `diagnose` deliberately throws away:

- `SCHEDULE_GENERIC_CAUSES` — the platform's own scheduler returns `due_date_passed` for every late order,
  which only restates the question. Filtered out so it cannot drown a real signal.
- Unrelated downtime is summarised per workstation as context, not promoted to a cause.

`downstream_impact` walks BOMs in reverse. Anything it finds is reported as
`potentially_blocked_work_orders` with `confidence: potential` — **never** "blocked" — because stock or
another order may already cover that demand. A sales order counts as `linked` only when it sits on the
late order itself.

---

## 4. The guardrails

These are why the agent is trustworthy in a book other teams are editing. Each one exists because
something went wrong live.

| guardrail | where | what it prevents |
|---|---|---|
| must file a finding | `record_finding` required before the final answer | a run that concludes only in prose |
| repeat guard | `REPEAT_GUARDED` | the loop burning steps; we saw 9 identical `seat_capability` calls |
| forced wrap-up | `_wrap_up_choice()` | running out of steps with nothing recorded — at 2 steps left `record_finding` is forced; at 3, a due escalation (never later: the last steps, the token budget's last call and the deadline's wrap-up window belong to the finding) |
| three locks on writes | `apply_mode` + `allowed_write_ids` + `approve()` | writing to a row we do not own |
| re-read before write | `apply_proposal` → `changed_underneath` | overwriting somebody's edit |
| conflict stops everything | `self.conflicts` | retrying into a race |
| escalation before filing | `_dispatch("record_finding")` | quietly dropping work a person must pick up |
| a failed escalation is still an escalation | `except` in `_dispatch("escalate")` | the rule below refusing every later call, so the run ends with nothing filed |
| costs must be real | cost check in `record_finding` | computing a variance when no cost was recorded |
| no null where the schema forbids null | `_nulled_required()` in `record_finding` | filing a finding the model left half-built, and calling the run a success |
| every field in its declared shape | `_mistyped()` in `record_finding` | a value of the wrong type, outside its enum, or an entry missing a required key reaching the verifier |
| never a substituted order | `requested_orders` / `missing_orders` in `record_finding` | answering about a "closest" order (or about none) when the order asked for does not exist |

The escalation rule is worth reading twice: if `needs_person` is non-empty and nothing was escalated,
the first `record_finding` is **refused** and the model is told to escalate first; once that refusal has been
seen, `_wrap_up_choice()` forces the `escalate` call on the next step. A handover matters, but not more than
the finding: escalate is only forced while three steps remain (escalate, `record_finding`, one retry) and
outside the finding's own time and token wrap-up, so a `needs_person` found late can no longer push the
finding off the end of the run.

`record_finding`'s guards come in two kinds. **Hard** guards keep wrong data out — the wrong shape, a null
the schema forbids, a substituted order, prose for a cause code, an unrecorded cost — and refuse every time,
because a wrong row is graded as an answer. **Soft** guards ask for a better answer — the subject the run
looked up, a refusal said out loud when nothing could be written, a reschedule chain finished, the
escalation raised — and each sends a finding back at most **once**, and only while there is room to act on it
(`_take_soft_refusal()`): a step for the retry, and for the chain and escalation guards the free steps their
instruction needs. Room is what keeps the finding safe: on the last step no soft guard has room, so it always
stores. Without the room check, two of them used to refuse the last two forced findings and leave nothing
filed. (One send-back shared by all four was tried and dropped: an early subject refusal then waived a needed
escalation with many steps still left.) A soft guard that is waived lets the finding through as sent and traces `guard_waived`;
nothing is invented for it (an escalation never raised stays absent from `escalations`), except that a
writable reschedule left unapplied is put on record as `not_applied` with the proposal's dates. Only a hard
refusal on the very last step can still end a run with no finding.

That rule is also why a throwing `escalate` has to leave a record behind. If `open_agent_session` or
`raise_escalation` raises, the failure is appended to `self.escalations` as `reason_code:
escalation_failed`, exactly as a platform refusal would be. Without it the escalation stays due, the forced
wrap-up keeps forcing `escalate` instead of `record_finding`, and the run can end with no finding in the
database — breaking the first guarantee in this table.

The last row is the newest, and the cheapest to get wrong. `RECORD_FINDING_SCHEMA` is handed to the model
as the tool definition, but nothing used to check the answer against it on the way back. On 2026-09-22 a
model returned `outcome: null` on a refusal that was otherwise correct; it was persisted, the run reported
`final_answer`, and the verifier scored it `revise`. `_nulled_required()` now rejects any *required* field
sent as `null` that the schema does not declare nullable, and names them so the model can retry — there is
almost always budget left. An **absent** field is not an error: a caller may record a partial finding
(`tests/test_production.py` files `{"outcome": "refused"}` alone), so only a sent-but-null value counts.

---

## 5. The harness: save first, judge second

Every task writes files in this exact order, each `fsync`'d:

```
task.json → fixture.json → context.json → trace.jsonl → result.json
                                                             │ (fsync)
                                                        verifier runs
                                                             │
                                                        verdict.json
```

The evidence is on disk **before** anything is scored. `VerifyContext` even re-loads `result.json` from the
file rather than reusing the in-memory object, so scoring can only ever see what was genuinely persisted.
No run can be graded on something you cannot go back and read.

`context.json` is captured before the agent starts and holds three things the verifier needs later:

- `me` — our user id, so writes can be attributed
- `started_at_utc` — from the **server's** HTTP `Date` header, minus 2s, so clock skew doesn't hide a write
- `snapshot` — the rows named in the task, as they were before the run

---

## 6. Verifiers read the database, not the reply

This is the part that makes the harness worth anything. A verifier never looks at `final_answer`. Take
`why_late_subcontract_not_sent`:

1. read WO-2026-00048 from REST;
2. compute the right answer **live** — which of its subcontracts are in `draft` right now;
3. read back the finding the agent stored in AgentMemory, matched on `run_id`;
4. compare, and separately assert no work orders were written.

The agent cannot pass by writing a persuasive paragraph. It passes only when what it *filed* matches what
is *in the database*. Expected answers are recomputed at scoring time, never hardcoded, because other
teams edit the same rows.

The finding round-trips through `AgentMemory` with the prefix `TEAM04_FINDING ` and is located by
`run_id` — that is the contract between agent and verifier.

---

## 7. Three verdicts

| verdict | meaning |
|---|---|
| `approve` | the filed answer matches the database |
| `revise` | it got it wrong |
| `unevaluated` | **could not judge** — never counts as a pass |

`unevaluated` is the honest option and it earns its place. When a verifier finds its premise gone (the
order was completed by someone else, an entity stopped being readable), it returns `UNEVALUATED` with a
reason instead of passing or failing on a stale premise. A verifier that raises does the same.

A harness without that third verdict quietly converts broken premises into passes.

**Reading a failure:** an `unevaluated` usually means the world changed, not that the agent is broken.
Check `result.json` → `stop_reason`. `harness_error` means the run failed before or inside the agent (the
traceback is in `error`); `no_target` means the task's fixture found no honest target today, so the agent was
never run. A verifier read that is too large to trust also scores `unevaluated`: `RestClient.list` raises
`TooManyRows` past 5000 rows, and `finding_from_db()` raises when it cannot tell whether the finding landed.

---

## 8. The task set

33 tasks in `harness/tasks/team04/`, **8 of them refusals** (9 are named `refuse_*`, but
`refuse_escalation_no_assignee_keystone` is an answering task); a full run is 44 runs across both instances
(the newest committed run, `runs/20261001-092435-266195`, predates the six `complex_*`/`stopped_not_late`
tasks and has 36). `harness/verifiers/team04.py` holds 32 verifiers (`current_operation_cited` serves both job-card tasks
through `params`) plus 30 private helpers.

A task is one JSON file:

```json
{
  "id": "why_late_wo48_subcontract",
  "instances": ["suryodaya"],
  "mode": "dry_run",
  "prompt": "This work order WO-2026-00048 is late. Find out why...",
  "verifier": "harness.verifiers.team04:why_late_subcontract_not_sent",
  "checks": "About WO-2026-00048, answered or partial. Every draft SubcontractOrder on WO-2026-00048 (DB) is a blocking cause and cited; ...",
  "fixture": null
}
```

The refusal tasks matter most for grading: the correct answer is "no". They cover payroll (another app),
purchase-order ETAs, operator contact details, cancelling a work order, rescheduling a date-locked work
order, changing a sales order date, an invented work order number, and a cost variance where no cost exists.

### One task attacks the agent on purpose

`concurrent_edit_before_write` is the best piece of the design. Between the agent proposing dates and
writing them, **the harness reaches in on a second connection and edits that row**, writing
`interference.json` to disk before the agent's write lands. It then checks the agent noticed, kept the
other edit, and stopped writing. The shared-book hazard is reproduced on demand rather than hoped for.

It hangs off `approve()` in `runner.py`, which is the last moment before a write.

---

## 8a. What makes a step slow, and what was done about it

Measured on Suryodaya, 2026-09-22: a `.list` call returning 200 rows takes ~2.3s; the same call returning
**one** row takes ~2-3.5s. Latency is per round-trip and barely moves with payload size. So the thing to
cut is the number of calls, not the number of rows — narrowing a read into several targeted queries makes
a step *slower*, which is why `diagnose` still fetches broadly and filters in Python.

Three changes follow from that, and together they took one real step from 112s to 36s with byte-identical
output (`WO-2026-00047`, compared against the previous implementation on live data):

| change | where | effect |
|---|---|---|
| independent reads in one batch overlap | `_invoke_batch()` | the model usually asks for several reads at once; they now run together |
| reads are reused within a run | `self._reads` → `propose_reschedule(diag=, down=)` | `propose_reschedule` re-ran a whole diagnosis and BOM walk the model had already paid for |
| windowed reads stop at the window edge | `McpClient.list_window()` | a 30-day downtime question no longer pages the whole table |

`_invoke_batch` splits a batch into **contiguous runs** of parallel-safe calls, so the model's ordering is
preserved and a write never jumps ahead of a read. `PARALLEL_SAFE` is the reads only; `apply_reschedule`,
`escalate` and `record_finding` stay serial because each gates on state the others must not race.
`propose_reschedule` is in the set: it only ever *appends* to `_proposals` and `needs_person`. The repeat
guard runs serially before dispatch, because "already called at step N" depends on batch order. One
`McpClient` is shared by the batch, so its request counter and re-login are locked.

Reusing a read is safe for exactly the reason `REPEAT_GUARDED` already tells the model a repeated read is
"still current": within one run these answers are not expected to change. The cache is keyed on the work
order reference, so a different order re-reads.

`list_window` exists because `.list` filters are **equality-only** — no date ranges — but `sort_by` /
`sort_order` work. It pages newest-first and stops at the first row older than the window, then returns
oldest-first so callers aggregate in a stable order. At today's table sizes (~100 downtime rows) this is
one call either way; it is what stops a 30-day question reading three years of history later.

---

## 9. Where to change things

| you want to | do this |
|---|---|
| add a delay cause | emit a new signal in `domain.diagnose`, then decide if it belongs in `UNDATED_BLOCKERS` |
| add a harness task | drop a JSON in `harness/tasks/team04/` + a verifier function; no registration needed |
| add a fixture | add to `FIXTURES` in `harness/fixtures.py` (six today: `late_draft_chain`, `stopped_not_late`, `late_with_open_material_request`, `late_with_unrelated_order`, `late_by_item`, `injected_late_order`); one that writes also goes in `WRITING_FIXTURES` |
| add a tool | `_fn(...)` spec **and** a `_dispatch_tool()` branch — plus `PARALLEL_SAFE` if it is a read that touches no run state |
| write a graded test | `tests/` only, by hand — AI-written tests score zero. AI-assisted work goes in `tests_ai/`, labelled |

`tests/` (39 tests) is hand-written and graded. `harness/` tasks and verifiers are AI-assisted and labelled
as such in every file header. Keep that line clean.

---

## 10. Constraints as of 2026-09-22

- **SalesOrder read is available again.** The 2026-09-20 release revoked it (bug N140); the fix is live
  and re-verified on both instances on 2026-09-22 — `SalesOrder.list`, `SalesOrder.get` and
  `SalesOrder.make.WorkOrder` are back in the catalogue and readable. Customer-impact answers are
  reachable again. `SalesOrder.update` remains absent, which is what makes
  `refuse_sales_order_date_change` a refusal case rather than an unreachable one.
- **Submitted work orders are date-locked** for `manufacturing_user`, so the agent writes dates on drafts
  only and proposes the rest. Cancel is admin-only.
- **Work orders carry no parent/child link *in this data***, which is why downstream impact is a reverse BOM
  walk and its results are "potential", not confirmed. `WorkOrder.supplies_work_order_id` exists and is
  writable on create/update, but is empty on all 133 orders (Suryodaya) and all 77 (Keystone), as of
  2026-09-22 — so there is nothing to walk. If it is ever populated, downstream impact could report
  confirmed links instead of inferring potential ones, and this tool should be revisited.
- **Outside the seat:** PurchaseOrder, StockEntry, Employee, SalarySlip (REST 403, absent from the
  catalogue).
- Seat limits have already moved twice (17 and 20 September). Re-probe rather than trusting this list;
  `seat_capability` is how the agent does it at runtime.
