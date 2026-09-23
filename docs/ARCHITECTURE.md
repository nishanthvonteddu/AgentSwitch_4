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
  │  9 tools             prod_agent/agent.py     │   the ONLY things the model may ask for
  ├──────────────────────────────────────────────┤
  │  domain logic        prod_agent/domain.py    │   the real work. No LLM in this file.
  ├──────────────────────────────────────────────┤
  │  transport           prod_agent/mcp_client.py│   MCP (JSON-RPC) + an independent REST client
  └────┬─────────────────────────────────────────┘
       │
   AgentSwitch
```

**The model never touches the platform.** It cannot build a URL, call an endpoint, or invent a record
number. It can only choose one of nine tools and fill in its arguments. Everything factual happens in
`domain.py`, which is ordinary Python.

Think of the model as a shift supervisor who may only submit request slips. `domain.py` is the clerk who
walks to the filing cabinet. This is why the 40 tests in `tests/` run without a model at all, and why a
wrong answer is nearly always a `domain.py` bug rather than a prompting problem.

The model's actual job is narrow: decide which question to ask next, and write the closing summary.

---

## 2. The nine tools

Defined in `ProductionAgent.tool_specs()`. Each maps to one function in `domain.py`.

| tool | what it does |
|---|---|
| `company_context` | company, country, currency — called first, so nothing is hardcoded to India |
| `list_late_work_orders` | open orders past their planned end date |
| `diagnose_work_order` | the big one: why is this order late (see §3) |
| `downstream_impact` | who consumes this order's output, and which sales orders are exposed |
| `propose_reschedule` | new dates, plus whether this seat is even allowed to write each one |
| `downtime_summary` | recorded downtime per workstation over N days, optionally one reason |
| `seat_entities` | the real entity names in this seat's catalogue |
| `seat_capability` | can this seat use `SalesOrder.update`? distinguishes a real limit from a typo |
| `record_finding` | persist the structured conclusion. Mandatory, exactly once. |

Two more appear only when switched on:

| tool | appears when |
|---|---|
| `apply_reschedule` | `--apply` / `"mode": "apply"` |
| `escalate` | `--escalate` / `"escalate": true` |

Adding a tool means adding a `_fn(...)` spec **and** a branch in `_dispatch()`. Forgetting the second gives
`unknown tool`.

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
| forced wrap-up | `_wrap_up_choice()` | running out of steps with nothing recorded — at 2 steps left `record_finding` is forced; at 3, a due escalation |
| three locks on writes | `apply_mode` + `allowed_write_ids` + `approve()` | writing to a row we do not own |
| re-read before write | `apply_proposal` → `changed_underneath` | overwriting somebody's edit |
| conflict stops everything | `self.conflicts` | retrying into a race |
| escalation before filing | `_dispatch("record_finding")` | quietly dropping work a person must pick up |
| a failed escalation is still an escalation | `except` in `_dispatch("escalate")` | the rule below refusing every later call, so the run ends with nothing filed |
| costs must be real | cost check in `record_finding` | computing a variance when no cost was recorded |
| no null where the schema forbids null | `_nulled_required()` in `record_finding` | filing a finding the model left half-built, and calling the run a success |

The escalation rule is worth reading twice: if `needs_person` is non-empty and nothing was escalated,
`record_finding` is **refused** and the model is told to escalate first. A handover is not optional.

That rule is also why a throwing `escalate` has to leave a record behind. If `open_agent_session` or
`raise_escalation` raises, the failure is appended to `self.escalations` as `reason_code:
escalation_failed`, exactly as a platform refusal would be. Without it the guard above refuses every
remaining `record_finding`, the forced wrap-up has nothing left to force, and the run ends with no
finding in the database — breaking the first guarantee in this table.

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
Check `result.json` → `stop_reason`. `harness_error` means we never even reached the agent.

---

## 8. The task set

24 tasks in `harness/tasks/team04/`, **9 of them refusals**, scored across both instances (30 runs).
`harness/verifiers/team04.py` holds 23 verifiers plus 9 shared helpers.

A task is one JSON file:

```json
{
  "id": "why_late_wo48_subcontract",
  "instances": ["suryodaya"],
  "mode": "dry_run",
  "prompt": "This work order WO-2026-00048 is late. Find out why...",
  "verifier": "harness.verifiers.team04:why_late_subcontract_not_sent",
  "checks": "Every draft SubcontractOrder on WO-2026-00048 (DB) is a blocking cause and cited.",
  "fixture": null
}
```

The refusal tasks matter most for grading: the correct answer is "no". They cover payroll (another app),
purchase-order ETAs, operator contact details, cancelling a work order, changing a sales order date, an
invented work order number, and a cost variance where no cost exists.

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
| add a fixture | add to `FIXTURES` in `harness/fixtures.py` (only `late_draft_chain` today) |
| add a tool | `_fn(...)` spec **and** a `_dispatch()` branch — plus `PARALLEL_SAFE` if it is a read that touches no run state |
| write a graded test | `tests/` only, by hand — AI-written tests score zero. AI-assisted work goes in `tests_ai/`, labelled |

`tests/` (40 tests) is hand-written and graded. `harness/` tasks and verifiers are AI-assisted and labelled
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
