# Team 04: Production agent for AgentSwitch

> "This work order is late. Find out why, tell me what it blocks downstream, and reschedule what you can."

Our own agent loop (no framework) driving AgentSwitch over MCP, plus a harness that stores every run on disk before verifying it against the database.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in both passwords and OPENAI_API_KEY; .env is git-ignored
```

By default the loop talks to OpenAI (`OPENAI_MODEL`, default `gpt-4.1`). To run it against
a model hosted on OpenRouter instead, set all three in `.env`:

```bash
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=...
# the model is a vendor/model slug; a bare gpt-4.1 is not a valid id on OpenRouter
OPENROUTER_MODEL=anthropic/claude-sonnet-4.5
```

`.env` is parsed by splitting on the first `=` and nothing else, so a trailing `#` comment
becomes part of the value. Keep comments on their own line.

Nothing falls back across providers, so leaving `LLM_PROVIDER` unset keeps the OpenAI path
exactly as it was. Whichever model you pick must support tool calling and a forced
`tool_choice` — the loop relies on both to get a finding into the database.

## Integrating your own model

Both paths above need **your** API key. If you want to drive this agent with a model we have
no key for — AgentSwitch's own provider, your evaluation harness, a local server — hand the
agent a client instead and skip the provider logic entirely:

```python
from prod_agent.agent import ProductionAgent
from prod_agent.mcp_client import McpClient, Session

agent = ProductionAgent(McpClient(Session("suryodaya")), llm=your_client, model="your-model-id")
result = agent.run("WO-2026-00048 is late. Why, and what does it block?")
```

**Passing `llm=` needs no credentials of ours.** `OPENAI_API_KEY`, `OPENROUTER_API_KEY` and
`LLM_PROVIDER` are only read when `llm` is `None`, so an injected client bypasses all of it.
(Tenant passwords are still needed — that is AgentSwitch access, not model access.)

### What your client has to implement

One method, the OpenAI chat-completions shape:

```python
resp = llm.chat.completions.create(model=..., messages=[...], tools=[...], **sampling)
```

and the response must offer:

| what the loop reads | why |
|---|---|
| `resp.choices[0].message.content` | the final answer when no tool is called |
| `resp.choices[0].message.tool_calls[].id` / `.function.name` / `.function.arguments` | dispatching each tool call |
| `resp.choices[0].message.model_dump(exclude_none=True)` | appended to `messages` for the next turn |
| `resp.usage.model_dump()` | optional; traced when present, skipped when absent |

`sampling` carries `temperature=0` only for `gpt-4*`/`gpt-3*` ids (reasoning models reject it),
and `tool_choice` when the loop forces `record_finding` near the step budget.

**Your model must support tool calling *and* a forced `tool_choice`.** Without the second, the
loop cannot make it record a finding before the budget ends, and a run with no finding in the
database scores nothing — the verifiers read state, never the reply text.

### Running the harness against your model

`harness/adapters.py:run_agent(llm, mcp, ...)` already takes the client as its first argument.
`harness/runner.py` constructs `ProductionAgent` without `llm`, so it uses the `.env` provider;
pass `llm=` at [harness/runner.py:280](harness/runner.py#L280) to point a full harness run at
your own client.

### Using the model AgentSwitch already hosts

`AgentProvider.list` shows what the platform has configured — on both tenants the default is
`AgentSwitch AI` (`fireworks`, `accounts/fireworks/models/deepseek-v4p1-flash`,
`supports_tools: 1`), alongside GPT-4o, Claude Sonnet and Gemini entries. The API keys are
held platform-side under `api_key_ref`, so that model cannot be called directly from here.
The route that does work is to let the platform run it: `AgentTask.create` with a `persona_id`
and `provider_id`, then `AgentTask.run_now`, then read the trace back from `AgentMessage`
(`role`, `tool_name`, `tool_status`, token counts). That trace is database state, which is
what our verifiers already grade. **We have not run that path yet** — it creates a real agent
job against a shared tenant and spends its daily budget, so it needs the owners' go-ahead.

## Run the agent

```bash
python -m prod_agent --instance suryodaya "WO-2026-00048 is late. Why, what does it block, and reschedule what you can."
python -m prod_agent --instance keystone  "..." --apply    # offers writes; each needs y at the prompt
python -m prod_agent --instance suryodaya "..." --escalate # may raise one real escalation to a person
```

Each run writes `runs/adhoc/<timestamp>-<instance>/trace.jsonl` and `result.json`.

## Run the harness

```bash
python -m harness.runner                          # every task, every instance it declares
python -m harness.runner --task <id> --instance keystone
python -m harness.runner --include-samples
```

Output goes to `runs/<timestamp>/<instance>/<task>/`: `task.json`, `fixture.json`, `trace.jsonl` (written event by event), and `result.json`, all fsync'd **before** the verifier writes `verdict.json`. Verdicts are `approve`, `revise` or `unevaluated`, and unevaluated never counts as a pass. See [harness/tasks/README.md](harness/tasks/README.md) for the task and verifier format.

Every run also keeps `results.json` (git-ignored) current in the format AgentSwitch's harness runner reads, so `python scripts/check_results.py` can check it.

What a run refuses, skips or cleans up:

- **One local run at a time.** A local run holds `runs/.console-run.lock` for its whole length, the same lock the console takes, because verifiers attribute writes by seat and time window and two runs would grade each other's. While another run or the console holds it, the runner prints `not started: ...` and exits 3. A console-started run inherits the console's lock (`HARNESS_RUN_LOCK_FD`). Platform mode takes no lock (its instance copy is private to the submission). On Windows there is no `flock`, so nothing guards it.
- **Bad arguments (local runs).** An unknown `--task` id is an argparse error before anything is created. A plan with nothing in it (a task on an instance it does not declare, a sample without `--include-samples`) prints why and exits 2.
- **No honest target.** A task whose fixture finds nothing today is recorded `unevaluated` (`stop_reason: no_target`) without running the agent.
- **Escalations are always withdrawn.** Whatever ends a task (a verdict, a crash in the agent or the verifier, Ctrl-C), escalations the agent raised are withdrawn afterwards and written to `cleanup.json`, after `verdict.json`. A killed process skips this, and the CLI (`python -m prod_agent --escalate`) never withdraws.
- **Reads too big to trust.** `RestClient.list` raises `TooManyRows` past 5000 rows, and `ctx.finding_from_db()` raises when it cannot tell whether the finding landed; a verifier that raises scores `unevaluated`, never a pass.

## Run on AgentSwitch ("Our harness" → Submit for a run)

Since Release 8.1 the platform runs this harness on its own server with its own model. [agentswitch-harness.toml](agentswitch-harness.toml) tells it how: `pip install -r requirements.txt`, then `python -m harness.runner` once per instance in `instances`, each against a fresh copy of that instance whose writes are thrown away, with no internet, for at most `timeout_minutes` (30). It reads `results.json` and shows pass/fail per task and a score. **One run per team every 3 days**, on the exact commit of the branch saved in the panel.

The runner gives us `AGENTSWITCH_BASE_URL`, `AGENTSWITCH_TOKEN`, `AGENTSWITCH_INSTANCE` and `OPENAI_BASE_URL`/`OPENAI_API_KEY`/`OPENAI_MODEL`. When `AGENTSWITCH_TOKEN` is set, the harness runs in platform mode:

- **Credentials.** The runner's token and base URL are used as given: no password, no login, no `.tokens/` file, and `.env` is not read at all, so its model settings cannot override the runner's. The model client is the plain `openai` package, which picks up `OPENAI_BASE_URL` itself.
- **One instance.** Only `AGENTSWITCH_INSTANCE`'s tasks run; a `Session` for the other instance is refused.
- **Preflight.** Before any task: MCP `tools/list` and REST `/api/auth/me` with the token (the agent uses MCP, verifiers read REST), and one tiny call per form of forced tool call (`named`, then `required`, then `auto`) to find which the model accepts. A failure is written to `results.json` as a failed `preflight` entry with every task marked not run.
- **Results.** Every planned task is in `results.json` from the start as not run, and is replaced by its verdict as it lands: `passed` is true for `approve` only. The file is rewritten whole after every task, so a run stopped at any moment still leaves a readable file.
- **Tokens.** Each submission has one LLM token budget, shared by both instances and not in the announcement: on 2026-10-12 Suryodaya's first three tasks spent 304,284 tokens on the platform's `deepseek-v4p1-flash`, and every later call, Keystone's included, came back 429 `budget_exhausted`. So the toml runs `--platform-set --token-budget 130000`: only the tasks marked `"platform": true` (4 per instance), with a 130k share per instance. A task starts only if its whole `max_tokens` (default 30k) still fits, the agent forces its finding before that limit, and the run stops at the first `budget_exhausted`. DeepSeek V4.1 Flash thinks by default, and the thinking is billed, so the preflight tries four ways to switch it off (`reasoning_effort` `none`, then `low`, DeepSeek's `thinking: {type: disabled}`, the chat template's `thinking: false`). It measures each by the completion tokens a one-word reply takes, and keeps the first that comes back quiet, else the quietest accepted. A host may accept a field and ignore it, which is why it measures rather than trusts. The run log lists what each attempt cost. A reasoning model's thinking text is never sent back on the next step. Every task's evidence starts with its steps and tokens, e.g. `approve [3 steps, 14.2k tokens]: ...`.
- **Time.** A 27-minute budget (`HARNESS_BUDGET_MINUTES` overrides). No task starts with under 90 seconds left, and the rest stay "not run (time budget)", which counts as failed. Each task gets 240 seconds, or its `max_seconds`; the agent forces its finding 45 seconds before its limit and stops at it. Tasks run cheapest first. At 28.5 minutes a watchdog exits with `results.json` as it stands.

Local runs are unchanged: no budget, no preflight, both instances, password login from `.env`.

`--workers N` runs the read-only tasks N at a time before the rest. A task that may write a work order or raise an escalation, or whose fixture writes, always runs alone, because verifiers count everything this seat did since a task started. The committed toml runs one task at a time: with the platform set, time is not the constraint, tokens are.

Rehearse a platform run locally before spending a submission. This calls the live tenant and writes an AgentMemory row per task, so the AGENTS.md rule on harness runs applies:

```bash
AGENTSWITCH_BASE_URL=https://agentswitch.theschoolofai.in AGENTSWITCH_INSTANCE=suryodaya \
AGENTSWITCH_TOKEN=$(cat .tokens/team04-suryodaya.token) OPENAI_API_KEY=... OPENAI_MODEL=gpt-4.1 \
python -m harness.runner --task refuse_unknown_work_order
python scripts/check_results.py
```

## Live run console

```bash
python -m viewer            # http://127.0.0.1:8765
```

A local page for the screen-shared live demo. Pick a harness task and an instance, type the confirmation it
asks for, and watch the agent's steps arrive, then the verdict and the evidence behind it (the finding in the
database, the write ordering, interference and cleanup). It also browses past runs.

**Ask the agent** takes a free-text question instead ("Can we finish WO-2026-00048 by 2027-03-31?", "Where is
the shop floor stuck right now?"). It runs `python -m prod_agent --instance <name> --run-dir … -- "<question>"`
with no `--apply` and no `--escalate`, so it can read and record a finding but never change an order or page a
person. No verifier exists for a free-text question, so its tag reads **Not graded**; for a graded feasibility
answer run the `feasible_by_wo48` task. Questions land in `runs/adhoc/` and show under **Questions** in the QC log.

- It starts the same command you would type: `python -m harness.runner --task <id> --instance <name>
  --runs-dir runs/demo`. Every click is a live run against a shared tenant.
- Demo runs land in `runs/demo/`, never `runs/2026*`, which is the only place `scripts/verify_submission.py`
  looks (it grades the newest run there that covers every task on every instance).
- One run at a time, only tasks from `harness/tasks/team04/`, no `--apply`/`--escalate` outside what a task
  declares.
- There is no stop button: a killed run skips the escalation withdrawal. Ctrl-C stops the page, not the run.
- Localhost only. It is not built to be shared or hosted.

**Before the demo**

- The console and `python -m harness.runner` share one lock file, `runs/.console-run.lock`, so a second run from
  another console or a terminal is refused while one is in progress (not on Windows, which has no `flock`).
- Never restart the console while a run is in progress. The run keeps going and still holds the lock, so the new
  console refuses to start another, but it does not show the running one; wait for the log in `runs/demo/` to end.
- If you refresh the page just as a run finishes, the live view is not re-attached; the run is under **Demo runs** in the QC log.
- Check provider, model and date without showing `.env` on screen (`.env` beats exported variables):
  `grep -E '^(LLM_PROVIDER|OPENAI_MODEL|OPENROUTER_MODEL|AGENT_TODAY)=' .env`. The trace's `start` event shows
  the provider and model actually used.
- After an escalation task, read the `cleanup` block. A failed withdrawal does not change the verdict, so the
  page flags it; withdraw that escalation by hand.

**Demo order** (agent time from the 17 Sep run): `refuse_unknown_work_order` on keystone (~15 s) →
`why_late_wo48_subcontract` (~70 s) → `concurrent_edit_before_write` (~100 s; writes fixture rows, escalation
withdrawn after scoring) → `feasible_by_wo48` ("can we finish WO-48 by 2027-03-31?", graded) → one question in
**Ask the agent** from a grader. Show `escalate_blocked_wo48` (~190 s) from past runs instead of live. These times
are from `gpt-4.1`; slower models take longer (the OpenRouter rehearsals took about 80 s for the refusal). If a
tenant is down, walk through `runs/20260917-110938` in the same page.

## Layout

| path | what |
|---|---|
| `prod_agent/mcp_client.py` | JSON-RPC MCP client (errors arrive as HTTP 200) and an independent REST client for verifiers |
| `prod_agent/domain.py` | deterministic logic: `diagnose`, `downstream_impact`, `propose_reschedule`, `apply_proposal`, `record_finding` |
| `prod_agent/agent.py` | the loop, tool specs and system prompt |
| `harness/` | runner, verifier context, fixtures, task set, `results.json` writer |
| `agentswitch-harness.toml` | how AgentSwitch's harness runner installs, runs and reads this repo |
| `viewer/` | local live-run console (python -m viewer) |
| `tests/` | hand-written tests only |
| `GAP_REPORT.md` | week-one gap report (benchmark: Carbon) |
| `docs/ARCHITECTURE.md` | how the agent and harness fit together, and where to change things |
| `docs/BUGS_FILED.md` | platform bugs filed by the team |
| `docs/FEATURE_REQUESTS.md` | platform capabilities we asked for, for the Carbon upgrade list |

## How the agent stays safe in a shared book

- **Re-reads before writing.** Each proposal carries `updated_at`. If the row changed or its status moved, the write is skipped with `changed_underneath`.
- **Stops after a conflict.** Once any order changes underneath a run, every further write in that run is refused and the conflict goes to a person. The harness proves this live: `concurrent_edit_before_write` moves a fixture order's dates between the agent's proposal and its write.
- **Hands work to a person.** With `--escalate` (or `"escalate": true` in a task), a locked, undatable or conflicting order is escalated through the platform's escalation queue to the assignee the platform names. If no assignee exists (Keystone today), the agent says so instead of claiming a handover. The first `record_finding` before a required escalation is refused, and the next step forces the `escalate` call, as long as three steps remain for it, the finding and a retry. Harness-raised escalations are withdrawn after scoring.
- **Potential, not confirmed, downstream.** Orders found by BOM matching are recorded as `potentially_blocked_work_orders` with `confidence: potential`, because stock or another order may cover the demand. A sales order is `linked` only when it is on the late order itself.
- **Writes only what the seat can write.** Submitted work orders are date-locked for `manufacturing_user` (verified live), so the agent writes dates on drafts only and proposes the rest.
- **Two gates on writes.** Every write needs approval (a human prompt in the CLI; in the harness, only the team's own fixture rows) and must be in the allowed-id set.
- **The finding goes into the database.** `record_finding` stores the structured conclusion in AgentMemory (private to our team), so verifiers read state, not prose.
- **Cannot loop past its budget.** A repeated read with identical arguments returns a note instead of running again, and in the last steps the loop forces `record_finding` (after a required escalation), so a run always leaves its finding in the database — an escalation that *fails* is recorded as one too, or that required-escalation rule would refuse every remaining call and leave nothing filed.
- **Tells a real access limit from a guessed name.** `seat_capability` asks REST: 403 means the entity exists but is outside the seat (reported in `not_visible`), 404 means the name was invented, and a wrong operation name on a visible entity (e.g. `JobCard.read`) returns the real operations.
- **Nothing is hardcoded to one country.** Country and currency come from `Company`, and missing tools (e.g. SalesOrder on Keystone) are detected, not assumed.

## Seat limits the agent reports rather than works around (2026-09-17)

- **Readable since 17 Sep (after our bug reports):** JobCard, DowntimeEntry and EngineeringChangeOrder on both instances. The agent probes access on every run instead of assuming it.
- **SalesOrder:** read-only on both instances (Keystone gained `sales_viewer` on 17 Sep). Read was revoked by the 20 Sep release and restored on 22 Sep — `.list`/`.get` verified on both, `.update` still absent.
- **Outside the seat:** PurchaseOrder, StockEntry, Employee and payroll (REST 403, not in the catalogue).
- **Links and locks:** WorkOrder has no parent/child link, so downstream impact comes from a reverse walk of BOM materials. Submitted work orders are date-locked, and cancel is admin-only (no longer listed for this seat).
- **No date or numeric range filter over MCP (29 Sep):** `lt:`/`gte:` are refused with `-32602` on date and number arguments, though they work over REST — the MCP schema types the field. Plain-string fields like `status` do accept them. The agent filters client-side until this is fixed; filed as D1 in [docs/BUGS_FILED.md](docs/BUGS_FILED.md).

## Rows this team created on Suryodaya

- WO-2026-00122: probe for the write-lock test, not_started, cannot be cancelled by our role.
- Harness fixtures: drafts the `late_draft_chain` and `injected_late_order` fixtures own. They are found again each run by the `team04-harness fixture:<name>` marker in their notes, not by number, and only created when none is found, so their numbers are not fixed. Every run resets their dates; `late_draft_chain` also repoints its pair at a BOM chain whose upstream has no undated blocker, trying up to 5 chains.
