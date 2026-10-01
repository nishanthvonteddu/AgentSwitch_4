# Bugs filed by Team 04

All reports were filed through `POST /api/bug-report` as team04 (Production seat). Full reproduction steps are in each report (`GET /api/bug-report/mine`).

| # | Instance | Report id | Summary | Board | Severity | Status (17 Sep) |
|---|---|---|---|---|---|---|
| B1 | Suryodaya (also Keystone) | f99d53d5-a527-4884-9d91-2407107f0b95 | JobCard and DowntimeEntry unreadable for `manufacturing_user`, although the schema grants read | M1 | Critical | Live, Release 1 |
| B1a | Suryodaya (also Keystone) | 92bb5235-35ed-4ce2-bea2-52acc3b4fc08 | EngineeringChangeOrder unreadable too; same root cause as B1 | S20 | High | Live, Release 1 |
| B2 | Suryodaya (also Keystone) | cda6df5e-d47e-4e5e-9d4f-55a7a88bf1da | `finite_schedule` returns JobCard ids that `JobCard.get` reports as not found | M2 | High | Live, Release 1 |
| B2a | Suryodaya | 0c52d179-4916-4eda-864e-51ae842448a3 | `finite_schedule` exposes downtime and job-card data the entity tools refuse; follow-up to B2 and B4 | S21 | Medium | Live, Release 1 |
| B3 | Suryodaya | 9dd39dd6-69b4-4683-ad62-5302273e6a5d | Admin-only transitions (e.g. WorkOrder cancel) listed in `tools/list` for `manufacturing_user` | M3 | Medium | Live, Release 1 |
| B4 | Suryodaya | 293321e3-7f66-4d13-a645-872ba6cb7052 | `finite_schedule` never attributes downtime or blocking to late orders | M4 | Medium | Live, Release 1 |
| B5 | Keystone | a45d393a-da35-46e5-b0a9-659f38e88141 | Workstation numbers contain the literal format token (`WS-.#####-2026-00001`) | M5 | Medium | Live, Release 1 |
| B5a | Keystone | 32c5715c-4879-4bf1-b881-6ff65d5231c2 | Duplicate of B5; adds serial numbers with an empty `number` | S22 | Medium | Live, Release 1 |
| B6 | Keystone | 4c26c220-0513-4423-8b5c-76eb28c5bbc6 | Cost Analysis page shows ₹ for a US company whose currency is USD | S30 | Medium | Fixed, next release |
| B9 | Suryodaya (also Keystone) | a77321bc-5a2d-4a3d-8106-3f05a7146afc | `check_stock_availability` returns "not found" inside a success response instead of an error | S29 | Medium | Fixed, next release |

| B10 | Suryodaya (also Keystone) | af8c5d99-1408-48bb-b3f2-e1274d1d6a18 | Production seat can create, update and delete email `TrustedSender` rows (another app's security control) | — | High | Filed 17 Sep, after cutoff |
| B11 | Suryodaya (also Keystone) | 64772b8a-4d5e-4cbb-b245-a45b7c995f21 | Every `.delete` tool returns `{"deleted": true}` for an id that does not exist | — | Medium | Filed 17 Sep, after cutoff |
| B12 | Suryodaya (also Keystone) | 4a41704c-7308-4611-bf8b-da08a2446fee | `finite_schedule` `recorded_downtime` cites downtime linked to neither the order nor the cause's workstation (follow-up to M4) | — | Medium | Filed 17 Sep, after cutoff |

| B13 = board **N140** | Suryodaya | d7263aae-f93e-4501-b55d-8d62ce126703 | SalesOrder read lost by the Production seat on 20 Sep; `sales_viewer` still granted in `/api/auth/me` but absent from every permission map | — | High | Filed 21 Sep |
| B13a | Keystone | b961b3a9-036f-4617-9793-2c3575588f5f | Same defect, filed separately on Keystone | — | High | Filed 21 Sep |

**B13 detail.** Between 07:52 and 08:35 IST on 2026-09-20, mid-harness-run, `SalesOrder` stopped being
readable from the Production seat on both instances: REST 403 on the collection *and* on 49 distinct ids
taken from `WorkOrder.sales_order_id`, and every `SalesOrder.*` tool disappeared from `tools/list`. The
entity is still in `/api/schemas`, and `/api/auth/me` still grants `sales_viewer` — but no entity's
permission map references that role, so it now grants nothing anywhere. Same class as B1. In the same
window the seat's tool count went 301 -> 310 and the schema entity count 424 -> 428, so a release landed.
Cost: 3 of 30 harness tasks unevaluated and 2 of 40 hand-written tests failing, all customer-impact.

| B14 = board **N144** | Suryodaya + Keystone | a89f99b5-9496-4510-9d96-280409dcafb3 / 7eb3b883-6fd4-4156-aabf-a35a1e8c6480 | Invalid `sort_order` silently accepted and treated as `asc`, while invalid `sort_by` correctly 400s | — | Medium | Filed 21 Sep |
| B15 = board **N145** | Suryodaya | 40e36812-246a-4920-abe5-bf14d76d516d | `BugReport.created_by` is always `"system"`; every other entity records the real user id | — | Medium | Filed 21 Sep |

**B14 detail.** `sort_order=sideways` returns 200 and ascending rows, identical to `sort_order=asc`, on both
instances. `sort_by=nonexistent_field` correctly returns 400, and `limit=-1 / 99999 / abc` and `offset=-5`
all return 422, so this one value is the gap. It matters because "most recent N" on an agent-driven platform
is `sort_by=created_at&sort_order=desc&limit=N`: a wrong enum silently returns the OLDEST rows with no error
the caller can detect.

**B15 detail.** Rows we create on `WorkOrder` (9) and `AgentMemory` (192) carry our real user id in
`created_by`. All 13 of our `BugReport` rows carry `"system"`, with `updated_by` null. Attribution survives
only in the free-text `reporter` column, so anything joining on `created_by` attributes every report in the
tenant to one pseudo-user.

**Closed without filing.** B7 (every team could read every other team's bug reports via the generic
`BugReport` API) was re-tested on 21 Sep and is **fixed**: `/api/BugReport` now returns only our own 13 rows.
B8 (whether `BugReport.create` honours a spoofed `reporter`) remains unconfirmed and is not filed, because
confirming it would write a row attributed to another team into the shared triage queue.

| B16 = board **N141** | Suryodaya | ae363fd5-de93-466e-9cdb-c4503c6f2617 | M3 incomplete: SubcontractOrder page still offers Approve, Reject, Cancel and delete to the Production seat | — | Medium | Filed 21 Sep |
| B17 = board **N143** | Suryodaya | de7b45d3-8b00-4b12-9253-db33df2c1fe3 | SubcontractOrder detail labels `vendor_id` as "Customer", and labels both warehouse fields "Store" | — | Medium | Filed 21 Sep |

**B16 detail.** Filed as a follow-up to M3, not a new defect. M3's fix removed the admin-only transitions
from `tools/list` and that part holds, but the web UI was not covered, so the same permission is still
offered through the other door. Verified unavailable on three doors: `SubcontractOrder.permissions` gives
`manufacturing_user` only read/create/write/submit; our MCP catalogue has only
`SubcontractOrder.approval.submit` (submit *for* approval); and `ApprovalRequest.permissions` omits
`manufacturing_user` entirely, with `/api/ApprovalRequest` and `/api/ApprovalPolicy` both 403.

**B17 detail.** `SubcontractOrder` has no customer field — the value shown as "Customer" is `vendor_id`,
which names the wrong side of the transaction on an order whose whole point is sending material out. The
two fields both rendered "Store" are `vendor_warehouse_id` and `target_warehouse_id`.

**Considered and not filed.** The Keystone agent dashboard tile reads "SCHEDULED TASKS 0" beside a list of
2 (both `AgentTask` rows are `paused`, confirmed over REST) — real but cosmetic, and likely to be triaged
into the known Keystone AgentTask item. The Keystone dashboard's "TOOL CALLS 96" happens to equal
Suryodaya's `AgentTask` row count; almost certainly coincidence, and we will not file a cross-tenant claim
we cannot substantiate.

Distinct defects: about 22 (B1a, B2a and B5a are follow-ups or duplicates).

Status from the class Bug Board, 17 Sep: the first 10 reports were all accepted; 8 live in Release 1 (17 Sep 2026, 18:40 IST), 2 fixed and shipping in the next release. Re-tested live on both instances the same day. B10-B12 were filed after the 14:35 cutoff, so they are queued for the next release and carry no board id yet.

| B18 | Suryodaya | 06a8c178-e905-4ec5-8727-a6744bbbe8a6 | Dashboard reports ON-TIME 100% while 65 of 83 open work orders are past due; Schedule page says "Late 57" | — | High | Filed 21 Sep |
| B19 | Suryodaya | 5b24a45e-2804-469c-9c56-4460dc8891ab | `WS-2026-00010` declares a 44-hour working day; 5 of 14 workstations are item rows with item prices as hour rates | — | High | Filed 21 Sep |

**B18 detail.** The tile's own denominator, "46 of 46", is the completed set; all 83 open orders are excluded,
65 of them already overdue (worst: WO-2026-00047 due 2026-02-25). Counted over REST on 21 Sep: 129 total,
46 completed (none finished late), 83 open, 65 open and past `planned_end_date`. The Schedule page reports
"Late 57" on the same login and data, so the app contradicts itself on its headline KPI.

**B19 detail.** `working_hours_per_day = 44` is accepted with no validation, and the Schedule page states it
places job cards on "its workstation's declared daily minutes" — so the field feeds the finite scheduler,
making 2,640 min/day of capacity possible against a 1,440-minute ceiling. Separately, WS-2026-00010..00014
are measuring-tool item rows (names carrying units of issue — Pair/Set/Kg/Mtr, hour rates 22,638–90,932
against 900–2,400 for the nine real machines, assets including a Dell server and an air compressor).

**Considered and not filed from the same sweep.** All 100 Suryodaya routings are mis-seeded (names are item
names with units; descriptions hold quality-disposition text from another entity) — real, but likely to be
absorbed into the known "odd demo names" item. Keystone BOM names say `rev A`/`rev B` while every `revision`
field is 1.0 — Low. KPI tiles on Subcontracting and Cost Analysis aggregate only the visible page
("OVERDUE 25 of 25 shown" beside "TOTAL 100 all 100") — misleading but self-disclosed. Overhead Rates shows
₹0.00 for every workstation with no explanation while the dashboard's OEE tile says "Not available in your
permission scope"; possibly the same class as S29, but we did not confirm the cause and will not file a guess.

| B20 | Suryodaya | 4f949f4d-68c1-4ee3-86ac-3d9d1c820979 | `make.orders` flags 4 of 29 "late" lines that are fully delivered; 3 have a promise date still in the future | - | High | Filed 23 Sep |
| B21 | Suryodaya + Keystone | c5b54e81-2f90-47c6-85c5-c1e7da033875 / 12c9b6c0-2990-45d9-97c9-8bec42913193 | OEE returns null with `source_state: "complete"`; the dashboard explains it as a permission limit the API never reports | - | High | Filed 23 Sep |
| B22 | Suryodaya | 0e2a7f2d-f195-4e2d-9813-848c9d15baae | `make.orders` returns 200 of 542 rows with no pagination argument and no truncation flag | - | Medium | Filed 23 Sep |

**B20 detail.** Audited all 29 `late=true` rows: 25 genuinely late, 4 not. SO-2026-00049:1 (43/43 delivered,
promised 2026-10-06), SO-2026-00056:2 (270/270, promised 2026-10-06), SO-2026-00052:2 (64/64, promised
2026-09-25) and SO-2026-00050:1 (216/216, promised 2026-09-16) are all fully delivered, and the first three
have a promise date *after* their projected date. Root cause traced: each sits on a sales order whose linked
work order is past `planned_end` with `produced_qty` 0, and the line inherits that work order's lateness and
blocker instead of using its own delivery state. One of those work orders, WO-2026-00113, is still a draft.

**B21 detail.** `oee_pct` returns `value/numerator/denominator: null` with `source_state: "complete"` and all
five sources `complete`, `reason: null`, on both instances - while the UI tile reads "Not available in your
permission scope", a cause the payload contradicts. OEE is computable from readable data: 180 completed job
cards, all non-backflushed, all carrying `actual_time_in_mins`, and the sibling metrics using the same inputs
all compute (`utilisation_pct` 1.3 from 41490/3114000, `scrap_rate_pct` 0.0 from 4845, `first_pass_yield_pct`
99.4 from 3334/3354). Same family as S29, plus a fabricated explanation on top.

**B22 detail.** `total: 542`, rows 200; `stage=invoiced` gives `total: 433`, rows 200. No `truncated`/
`has_more`/`next_offset` key, and the closed inputSchema declares only `stage` and `late` - `offset`, `limit`
and `page` each return `-32602`. So 342 of 542 lines are unreachable, and a partial answer is
indistinguishable from a complete one.

**Verified fixed on 23 Sep (Release 6).** N140 (SalesOrder read restored - the agent, the 2 graded tests and
the 3 harness tasks all recovered with no code change), N150 (on-time now reads 44.2% and the payload
documents its basis as "late open orders count against"), N151 (no working day longer than 24 hours), and
N145 (the 4 reports filed today carry our real user id in `created_by`; the 19 earlier ones still read
`system` and were not backfilled). Team 04 stands at 19 Live on server.

**Checked and not filed from this sweep.** Invoiced lines carrying a live blocker looked wrong but is
correct - those 433 are billed-but-undelivered (`delivered_qty` 0), so a blocker belongs there. The
dashboard's draft-count drift was our own harness fixtures moving between two reads. `endpoint.people_directory`
serving names the `Employee` entity refuses is deliberate and already tracked as N146 ("a names-only people
directory any member may read ... and NOTHING else"), with no pivot: the ids it returns give 403 on Employee
and 404 elsewhere.

| B23 | Suryodaya (both verified) | ac17e40d-6297-4765-af01-0d563115e3b2 | SPA catch-all returns 200 + index.html for static-file paths, incl. `/service-worker.js` and `/robots.txt` | - | Low-Medium | Filed 23 Sep |
| B24 | Suryodaya (both verified) | 92fd2536-c8d2-473b-8127-ed321af943ed | Manufacturing UI calls `/api/accounting/locale` and `/api/payroll/locale` on every page load and swallows two 403s | - | Low | Filed 23 Sep |

**B23 detail.** Any path outside `/assets/` and `/api/` is absorbed by the catch-all and answered with the
SPA shell under HTTP 200 regardless of file extension: `/service-worker.js`, `/robots.txt`, `/foo/bar.js`
and `/totally-random-xyz.js` all return `200 text/html`. `/manifest.webmanifest` and
`/assets/does-not-exist.js` correctly return 404, so the right behaviour already exists in the routing.
The real service worker at `/sw.js` is served correctly as `application/javascript`. Not a security issue -
`nosniff` prevents execution - filed as correctness and hygiene.

**B24 detail.** Every Manufacturing page load fires `GET /api/accounting/locale` and `GET /api/payroll/locale`
from bundle `api-CNm7t7R0.js`, both 403 for a seat holding neither app, both discarded silently. Filed
leading with that (the frontend requesting endpoints its own seat cannot use), with the secondary
observation that a Production seat has no seat-neutral way to read the tax or accounting regime: those two
endpoints are 403 and `/api/locale`, `/api/manufacturing/locale` and `/api/company/locale` are all 404,
while `/api/Company` gives country, `default_currency` and `fiscal_year_start` but no regime or standard.
The report explicitly concedes the section 3 seat boundary rather than disputing it.

**Security sweep, 23 Sep - nothing to report.** Verified hardened on both instances: HSTS 2yr + preload,
nonce CSP with `strict-dynamic` and `frame-ancestors 'none'`, session cookie `HttpOnly; Secure; SameSite=lax`,
opaque (non-JWT) bearer token, cross-instance token reuse 401 both directions, cross-tenant IDOR 404,
anonymous 401 on `/api/auth/me`, `/api/WorkOrder`, `/api/schemas` and `/api/mcp`, no stack traces and no
enumeration oracle in errors, `_redacted_fields` working, TRACE/OPTIONS 405, `/api/Company` scoped to our
own tenant, no CORS wildcard, no version disclosure. We did not test login user-enumeration: two failed
logins risk a lockout before submission for a Low-severity finding.

**Brief-conformance audit, 23 Sep - platform matches its documentation.** All 365 tools carry a closed
JSON Schema (0 missing, 0 with `additionalProperties` other than false); malformed JSON, missing method,
unknown method, wrong jsonrpc version, non-object params and batching all return HTTP 200 with correct
JSON-RPC error codes, and only auth answers at the HTTP layer, exactly as section 6 states; batching is
refused by name; `notifications/initialized` correctly returns 202 with no body as a notification and
-32601 when sent with an id; `/api/agent/tools` returns exactly 13 tools, all with schemas; `SalarySlip`,
`Contract` and `EsignDocument` are 403 on both instances; `/docs`, `/redoc` and `/openapi.json` are live
when authenticated; `GET /api/mcp` returns 405 with `Allow: POST`. The eight new manufacturing entities
(`QualityIssue`, `EightDReport`, `SupplierCorrectiveActionRequest`, `AQLAcceptancePlan`, `AQLSampleSizeCode`,
`ChangeoverTime`, `ManufacturingPreferences`, `QualityPreferences`) all read cleanly with no truncation and
sane permissions.

## Board acceptance, 21 September

All five reports filed on 21 Sep were accepted and are Open on the class Bug Board, queued for the next
release. Team 04 now has 16 board entries: 10 Live on server, 6 Open.

| Ours | Board | Area | Severity | Owner's note |
|---|---|---|---|---|
| B13 | N140 | Access | High | "We are restoring it, because that access was given on purpose." |
| B16 | N141 | Manufacturing | High | Accepted as a follow-up to M3, not a duplicate. "We will check whether the server refuses the action if clicked." |
| B15 | N145 | Bug reports | Medium | "The team who filed each report is still recorded separately... no team has lost credit - but the audit trail should agree." |
| B17 | N143 | Manufacturing | Low | "Checked against the schema." |
| B14 | N144 | API | Low | "An invalid value should be refused the same way." |

**N140 unblocks our red tests.** Once SalesOrder read is restored, re-run `tests/test_downstream_sales_order`
and `tests/test_keystone_sales_order_permission` (both currently failing), and the three harness tasks that
are `unevaluated` (`blocks_wo48_sales_order`, `keystone_customer_impact_wo4`,
`refuse_sales_order_date_change`). Also revisit the worked example in GAP_REPORT.md, which cites
SO-2026-00092.

**Severity calibration.** We pitched B17 as a material-movement hazard and it was rated Low; B16 we expected
Medium and it was rated High. The board weights *whether the platform lets you do the wrong thing* above
*whether a label reads wrongly*.

**N142 (downtime entries that end before they start, Medium, Open)** is also credited to Team 04 but was not
filed from this session - another team04 session filed it. Not one of B13-B17.

## 24 September — the capacity layer

Release 6/7 added a finite-capacity read model (`capacity_board`, and a rebuilt `finite_schedule`
returning per-order verdicts and causes). Auditing it produced one defect that invalidates its output.

| C1 | Suryodaya + Keystone | 290c63d5-a44e-41c1-a282-bd7bc7cce7df / fc632303-c70e-4f45-be12-d3fe8edccfbb | `capacity_board` and `finite_schedule` charge a job card's per-piece minutes as the whole job, understating load 94x (Suryodaya) / 84x (Keystone) | - | Critical-High | Filed 24 Sep |

**C1 detail.** The platform's own OEE basis string defines a card's standard work content as
`(time_in_mins x for_qty) / actual_time_in_mins` for performance — so `time_in_mins` is **per piece**.
Both capacity endpoints charge it raw: `capacity_board`'s `booked_minutes` of 1381.0 equals
`sum(JobCard.time_in_mins)` over the 149 open cards to the decimal, and `finite_schedule` charges the
unscaled value on 149 of 149 scheduled operations. Consequences: the board reports **1.25% load and 0
workstations overloaded** where the same cards are **117% of its own declared `available_minutes`** with
ASSY-01 at 2.4x; `work_content_exceeds_due_date` fires on 1 of 61 orders while `due_date_passed` fires on
57; and every operation returns `scheduled_start == scheduled_finish == 2026-09-24`.

**C1 supersedes part of N263.** The identical-`projected_finish` symptom we filed as FR1 and the board
queued as N263 ("projected finish dates that respect capacity, downtime and operators", Release 7, a work
calendar plus a labour model) has this as its root cause. A work calendar will not separate the dates while
work content is understated ~94x. Flagged inside the report so N263 does not ship and miss.

**What we deliberately did not claim.** `actual_time_in_mins == time_in_mins x for_qty` holds on 180/180
(Suryodaya) and 240/240 (Keystone) completed cards, but that implies OEE performance is exactly 1.0 on every
card, which points at the actuals being seeded from the standard. The report says so itself and rests the
argument on the OEE formula and the schema text (`actual_time_in_mins` is "the minutes the shop floor
ACTUALLY booked ... a completion path never writes a standard-time estimate here"), not on the data. An
earlier draft leant on the WIP posting as corroboration; that was dropped, because costing reads
`actual_time_in_mins` and so sits downstream of the same values.

**Open question carried in the report.** BOM and Routing operation lines carry `batch_size` (default 1)
next to `time_in_mins`. If work content is meant to be `time_in_mins x for_qty / batch_size` the magnitudes
change. The OEE basis string does not divide by it, so that is what we used.

### Drafted, not filed

- **C2** — Suryodaya `Routing` operation lines carry seeded noise: `sequence` non-integer on 187/224 and
  above 100 on 175/224 (range 1 … 1991.59), `batch_size` fractional on 185/224, `time_in_mins` over 24h on
  42/224, and 42/100 routings return operations out of sequence order. Keystone's 2 routings are clean.
  Latent: all 329 job cards have `instruction_source: bom_operation` and clean sequences 1–4, so no routing
  currently reaches a job card — but 74 of 143 work orders are for items that have a Routing row.
- **C4** — two `AgentProvider` rows hold impossible settings (`temperature` 78.39 / 39.75, `max_tokens`
  102.75 / 16.75, model strings "Surface Plate 342" / "Feeler Gauge Set 341", `api_key_ref` not matching the
  provider), and two rows carry `is_default = 1` on both instances. Low: the malformed rows are
  `is_active = 0` and the duplicate default is masked because only one row is active.
- **C5** — 35 of 102 `AgentSkill` rows instruct the agent to use `delegate_to_agent` and `ask_human`;
  neither appears in the seat's 365-tool catalogue. Probably injected into hosted agent runs rather than
  exposed over MCP, so likely correct behaviour — worth confirming, not yet worth filing.

### Negative results, 24 September

Swept and clean, recorded so we do not re-check: `planned_end < planned_start` (0 of 143 / 77),
`produced_qty > qty` (0), JobCard actual end-before-start (0), negative durations (0), QualityInspection
`accepted + rejected != inspected` (0 of 157), and `exception_cockpit` truncation behaviour (correct
`total_items` / `limit` / `offset` / `complete` at limit 1, 50 and 500). The new endpoints report their own
limits honestly — `capacity_board` publishes `malformed_workstation_ids`, `finite_schedule` publishes
`uncosted_item_change_pairs` and a `generic_late_cause_rate` — which is the discipline S29 and N233 asked for.

## 29 September — the MCP filter gate

| D1 | Suryodaya (both verified) | 7525f98c-89cd-445c-9908-8cc21e32a068 | Date and numeric range filters (`lt:` `gte:` …) are rejected by the MCP tool schema but work over REST, so no agent can filter by date | - | High | Filed 29 Sep |

**D1 detail.** `WorkOrder.list` over MCP with `planned_end_date: "lt:2026-09-29"` returns `-32602`
(`/planned_end_date must be a valid date.`); the same filter over REST returns 200 and `total: 124` of 147,
correctly applied (`lt:` 124 + `gte:` 23 = 147, no nulls). Cause, from `tools/list`:
`{"type": "string", "format": "date"}` — the operator prefix fails JSON-Schema validation before dispatch.
Numeric args fail the same way (`{"qty": "gt:100"}` → `/qty must be number.`, REST returns 46). Every
date-typed argument we tried is refused, across `WorkOrder`, `JobCard`, `DowntimeEntry` and `MaterialRequest`;
`created_at` is not an MCP argument at all, though REST filters on it.

The gate is the schema, not the interface: `status` is declared as a plain string, so `{"status":
"ne:completed"}` passes and is honoured over MCP (`total: 101` = 147 − 46 completed) — the same reason CSV
works (`a6804fa`). Confirms the syntax `8b8d0e0` backed out as "not confirmed"; that description can be
sharpened once D1 is triaged. Reproduced identically on Keystone, recorded in the report, no twin filed.

| D2 | Suryodaya (both verified) | b9b72008-3dd1-4128-8ca6-2a0b5bc8efe9 | `search` is advertised as full-text but covers a per-entity column list; WorkOrder's omits `notes` while MaterialRequest's includes it | - | Medium | Filed 30 Sep |

**D2 detail.** On WorkOrder, `search` matches `number` only: `search="team04-harness"` returns 0 while 60 rows
carry it in `notes` (`search="WO-2026-001"` → 70). `notes` is the entity's only free-text column and the only
one this seat writes. Not a rule about identifiers — `MaterialRequest.notes` (30/30), `DowntimeEntry.remarks`,
`QualityInspection.remarks` and `Workstation.description` are all searched, while `WorkOrder.notes` and
`EngineeringChangeOrder.reason`/`description` are not. Same on MCP and REST, both instances.
`/api/agent/tools` calls it "Full-text search across all entities or a specific entity."

The failure is a silent `total: 0`, which reads as "no such records". Our harness trusted it to find its own
fixture rows and created a new pair per run instead of reusing one — 60 marker drafts on Suryodaya where 2
should be, fixed our side in `9929235`. The report states that as the cost of the silent zero, not as a
platform action.
