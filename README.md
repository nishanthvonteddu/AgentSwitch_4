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
pass `llm=` at [harness/runner.py:159](harness/runner.py#L159) to point a full harness run at
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

## Layout

| path | what |
|---|---|
| `prod_agent/mcp_client.py` | JSON-RPC MCP client (errors arrive as HTTP 200) and an independent REST client for verifiers |
| `prod_agent/domain.py` | deterministic logic: `diagnose`, `downstream_impact`, `propose_reschedule`, `apply_proposal`, `record_finding` |
| `prod_agent/agent.py` | the loop, tool specs and system prompt |
| `harness/` | runner, verifier context, fixtures, task set |
| `tests/` | hand-written tests only |
| `GAP_REPORT.md` | week-one gap report (benchmark: Carbon) |
| `docs/ARCHITECTURE.md` | how the agent and harness fit together, and where to change things |
| `docs/BUGS_FILED.md` | platform bugs filed by the team |
| `docs/FEATURE_REQUESTS.md` | platform capabilities we asked for, for the Carbon upgrade list |

## How the agent stays safe in a shared book

- **Re-reads before writing.** Each proposal carries `updated_at`. If the row changed or its status moved, the write is skipped with `changed_underneath`.
- **Stops after a conflict.** Once any order changes underneath a run, every further write in that run is refused and the conflict goes to a person. The harness proves this live: `concurrent_edit_before_write` moves a fixture order's dates between the agent's proposal and its write.
- **Hands work to a person.** With `--escalate` (or `"escalate": true` in a task), a locked, undatable or conflicting order is escalated through the platform's escalation queue to the assignee the platform names. If no assignee exists (Keystone today), the agent says so instead of claiming a handover. `record_finding` is refused until a required escalation has been attempted. Harness-raised escalations are withdrawn after scoring.
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
- **No date or numeric range filter over MCP (29 Sep):** the MCP tool schema types these arguments (`planned_end_date` as `{"type": "string", "format": "date"}`, `qty` as `number`), so `lt:`/`gte:` are refused with `-32602` before dispatch — while the same filter over REST returns 200 and applies correctly. It is the schema, not the interface: `status` is declared as a plain string, so `ne:`/CSV pass validation and are honoured over MCP. Until this is fixed the agent reads the table and filters client-side. Filed as D1 (`7525f98c`) — see [docs/BUGS_FILED.md](docs/BUGS_FILED.md).

## Rows this team created on Suryodaya

- WO-2026-00122: probe for the write-lock test, not_started, cannot be cancelled by our role.
- WO-2026-00123 and WO-2026-00124: harness fixtures (drafts, reused and reset every run).
