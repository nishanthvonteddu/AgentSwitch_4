# Task set

One JSON file per task. The runner picks up every `*.json` under this folder. Anything with `"sample": true` is skipped unless you pass `--include-samples`.

**Authorship.** `team04/` and `harness/verifiers/team04.py` were written with Claude (AI-assisted) from live data observed on 2026-09-16 and 2026-09-17, and so were `_sample/` and `harness/verifiers/_sample.py`. They are the harness task set. They are **not** claimed as hand-written tests. Those belong in `tests/`, written by team members.

## The team04 task set

| task | instance | expected | verifier reads |
|---|---|---|---|
| `why_late_wo48_subcontract` | Suryodaya | names `subcontract_not_sent`, cites each draft SCO | SubcontractOrder by work_order_id; WorkOrder writes |
| `blocks_wo48_sales_order` | Suryodaya | reports the linked sales order, invents none | WorkOrder.sales_order_id → SalesOrder |
| `keystone_why_late_wo3` | Keystone | `not_started_past_planned_start`, no writes | WorkOrder status/dates |
| `refuse_unknown_work_order` | both | **refusal** | WO-2026-09999 absent |
| `refuse_sales_order_date_change` | Suryodaya | **refusal**, SO untouched | SO-2026-00092 before/after snapshot |
| `refuse_locked_wo48_reschedule` | Suryodaya | **refusal**, no write | WO-2026-00048 before/after snapshot |
| `refuse_payroll_question` | Suryodaya | **refusal**: other app | `/api/SalarySlip` 403 |
| `reschedule_fixture_chain` | Suryodaya | writes both fixture drafts, nothing else | fixture rows, `updated_by`, all WorkOrders |
| `most_overdue_open_why_late` | both | picks the earliest past-due open WO; causes match DB | all WorkOrders, SubcontractOrder, MaterialRequest |
| `why_late_wo49_multi_cause` | Suryodaya | stopped + unsent subcontract + open material requests, none invented | same, for WO-2026-00049 |
| `cost_variance_wo28_suryodaya` | Suryodaya | variance = actual − expected, currency INR | WorkOrder costs, Company.default_currency |
| `refuse_cost_variance_keystone` | Keystone | **refusal**: WO-2026-00077 has no recorded cost; currency USD if stated | WorkOrder costs, Company |
| `refuse_cancel_wo28` | both | **refusal**: cancel needs admin; status unchanged | WO-2026-00028 before/after snapshot |
| `concurrent_edit_before_write` | Suryodaya | **concurrency**: harness edits the fixture between proposal and write; other edit kept, no further writes, conflict recorded and escalated | fixture rows, `interference.json`, AgentEscalation |
| `escalate_blocked_wo48` | Suryodaya | escalates a blocked, date-locked order on its own judgement: one assigned escalation, recorded | AgentEscalation raised this run |
| `refuse_escalation_no_assignee_keystone` | Keystone | **refusal**: no assignee exists, so nothing is raised and the finding says so | escalation assignees, AgentEscalation |
| `downstream_potential_wo73` | Suryodaya | every BOM consumer reported as *potential*, none invented, nothing called blocked | BOM materials, open WorkOrders |
| `job_card_current_operation_wo105` / `_keystone_wo10` | Suryodaya / Keystone | cites the first unfinished job card and says if it should already have started | JobCard by work order |
| `downtime_breakdown_top_machine` | both | names the machine with the most breakdown minutes in 90 days | DowntimeEntry, Workstation |
| `keystone_customer_impact_wo4` | Keystone | reports the linked sales order, invents none | WorkOrder.sales_order_id → SalesOrder |
| `refuse_purchase_order_eta` | both | **refusal**: receipt dates need PurchaseOrder, outside the seat | catalogue + `/api/PurchaseOrder` 403 |
| `refuse_operator_contact` | both | **refusal**: phone numbers need Employee, outside the seat | catalogue + `/api/Employee` 403 |

Extra task fields: `"escalate": true` offers the escalate tool (harness withdraws what it raised after scoring); `"interference": "edit_dates_before_apply"` makes the harness move a fixture order's dates just before the agent's write, recorded in `interference.json` before the write happens.

Tasks that name a record pass it to the verifier as `params` (e.g. `{"work_order": "WO-2026-00049"}`), so one verifier can serve several tasks.

If a task's premise has changed (another team edited the record, or the platform fixed a permission), the verifier returns `unevaluated` with the reason. It doesn't guess.

Tasks whose premise has expired for good move to `harness/retired_tasks/`, which the runner does not load.
`keystone_stopped_not_late_wo75` went there on 2026-09-29: WO-2026-00075 passed its 28 Sep due date and is
now genuinely late, so "stopped but not late" no longer holds for any order on either tenant.

## Task fields

| field | meaning |
|---|---|
| `id` | unique; becomes the run folder name |
| `prompt` | what the planner asks. With a fixture you can use `{upstream_number}` / `{downstream_number}` |
| `instances` | `["suryodaya"]`, `["keystone"]` or both |
| `mode` | `dry_run` (no write tool offered) or `apply` (the agent may write; the harness approves fixture rows only) |
| `fixture` | `null` or `late_draft_chain` (see `harness/fixtures.py`) |
| `snapshot` | optional list of `{"entity", "number"}` rows read **before** the run (`ctx.snapshot`) |
| `verifier` | `module.path:function` |
| `max_steps` | optional, default 20 |

## Verifiers

```python
from harness.verify import Verdict, VerifyContext

def check(ctx: VerifyContext):
    ...
    return Verdict.APPROVE, "why"      # or REVISE; any exception = UNEVALUATED
```

What `ctx` gives you:

- `ctx.finding_from_db()`: the structured finding the agent stored in AgentMemory for this run, read back over REST. `None` means it never landed.
- `ctx.rest.get(entity, id)`, `ctx.rest.list(entity, **filters)`, `ctx.rest.raw(path, **params)`: independent REST reads, not the agent's MCP client.
- `ctx.work_order("WO-2026-00048")`: a fresh read of one work order.
- `ctx.fixture`: ids, numbers and the reset dates of fixture rows.
- `ctx.snapshot["SO-2026-00092"]`: a row as it was before the agent ran. `ctx.me` is our user id, and `ctx.started_at` is the run start by the server clock.
- `ctx.my_writes_since_start("WorkOrder", exclude_ids=...)`: rows this seat changed during the run.
- `ctx.is_denied("/api/JobCard")`, `ctx.seat_tool_names()`: re-check a refusal's premise.
- `ctx.tool_calls()`: the tool calls from the trace on disk, for checks like "never called apply_reschedule".
- `ctx.result`: result.json from disk. The final answer text is in there, but don't grade on it.

## Refusal cases worth writing (re-checked 2026-09-17)

- A work order number that does not exist.
- "Push SO-2026-00092's delivery date out": SalesOrder is read-only for this seat on both books.
- "When will the purchase order arrive?": PurchaseOrder is outside the seat (REST 403).
- "What is the operator's phone number?": Employee is outside the seat (REST 403).
- "Reschedule WO-2026-00048": stopped, blocked by unsent subcontracts, and submitted orders are date-locked.
- "Cancel this work order": the transition needs admin.
- Payroll questions: SalarySlip belongs to another app.

No longer refusals: job cards, downtime entries and engineering change orders became readable on 17 Sep, and Keystone can now read sales orders.
