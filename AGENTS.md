# AGENTS.md

Instructions for AI coding agents working in this repository. Read this before
changing code or running anything.

## Live systems — ask before running

Both the agent and the harness talk to **live shared instances**. There is no local
database, no mock server, and no `--dry-run`. Ask Pravin before running:

- `python -m harness.runner` (any form) — every successful run writes an `AgentMemory`
  row, and fixtures create/update real `WorkOrder` rows on Suryodaya
- `python -m prod_agent ... --apply` — mutates real work orders
- `python -m prod_agent ... --escalate` — raises a **real escalation assigned to a real
  person**. The harness withdraws these after scoring, even when the task crashed or was
  interrupted with Ctrl-C (a killed process skips it); the CLI never does.
- `python -m viewer` is safe to start, but its **Start run** button runs `python -m harness.runner` —
  same rule, ask first. Its **Ask the agent** mode runs `python -m prod_agent` (read-only: no `--apply`,
  no `--escalate`), which still records a finding row on the tenant

Reading code, offline tests, and inspecting `runs/` output need no confirmation.

Submitting on AgentSwitch ("Our harness" → Submit for a run) runs `agentswitch-harness.toml`
against a throwaway copy of the instance, so it does not touch the shared book, but it spends
the team's **one run every 3 days**: never submit without being asked.

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env    # both passwords + OPENAI_API_KEY; .env is git-ignored

python -m pytest tests                              # graded tests — 12 of the 39 hit live tenants
AGENT_OFFLINE=1 python -m pytest tests              # the 27 that need no network
AGENT_TODAY=2026-09-16 python -m pytest tests_ai -v # pin the date or results drift
python scripts/verify_submission.py                 # full pre-submission checklist
python scripts/verify_submission.py --offline       # skip network checks
python scripts/check_results.py                     # results.json in the platform runner's format
```

A non-empty `AGENT_TODAY` line in `.env` beats the shell (see Environment), so the shell pin above
only takes effect while `.env` leaves it empty, as `.env.example` does.

`verify_submission.py` shells out to `pytest tests -q` and exits non-zero on any failed
check. Run it before considering work done. Under `--offline` the graded suite is reported as a
skip with counts rather than a `PASS`, because it no longer verifies the 12 live tests; the run
still fails on a genuine test failure, and it fails if the suite reports **no** skips at all,
since that means the gate never engaged and the tests really did run live.

What else it checks: the gap report's sections and word cap; the hand-rolled loop and no framework in
`requirements.txt`; `final_answer` absent from the verifiers; at least one `refuse_*` task; the newest
**complete** run root under `runs/2026*` (one whose `summary.json` scored every task/instance pair a full
`python -m harness.runner` plans today; newer partial roots are listed and passed over) is all approved with
none unevaluated, wrote `result.json` no later than `verdict.json` everywhere, and approved every
`refuse_*` task; no `claude` in `tests/test_*.py`; the bug reports on record (skipped offline); no key or
password in committable files; and the harness toml and `results.json` format. If that run started before
the last commit touching `prod_agent/` or `harness/` (or there are uncommitted changes there), it prints a
`WARN`, not a `FAIL`: only a new live run can clear it. Setting `AGENT_OFFLINE=1` without `--offline` is
refused.

Agent and harness invocations are documented in each module's docstring
(`prod_agent/__main__.py`, `harness/runner.py`) — this repo uses docstrings instead of a
Makefile.

## Environment

Python 3.11, but the runtime code (`prod_agent/`, `harness/`) must also run on **3.10**: the
platform's harness runner does not say which Python it uses, and `datetime.UTC` (3.11+) once
broke every finding there. `tests_ai/test_python_floor.py` guards it; tests and
`scripts/verify_submission.py` may use 3.11 (`tomllib`). Plain `pip`, no lockfile. Required in `.env`: `TEAM04_PASSWORD_SURYODAYA`,
`TEAM04_PASSWORD_KEYSTONE`, `OPENAI_API_KEY`. Optional: `OPENAI_MODEL` (default
`gpt-4.1`), `AGENT_TODAY`.

`LLM_PROVIDER` picks where the loop sends its completions. Unset or `openai` is the
default and reads `OPENAI_API_KEY`/`OPENAI_MODEL` as before. `openrouter` routes through
`https://openrouter.ai/api/v1` and requires both `OPENROUTER_API_KEY` and
`OPENROUTER_MODEL` — there is no cross-provider fallback, and no default slug, because
`gpt-4.1` is not a valid OpenRouter id (they are `vendor/model`). A model reached this way
must support tool calling and should accept a forced `tool_choice`: if it rejects one, the
loop falls back to `required` and then `auto` for the rest of the run, which makes recording a
finding likely rather than guaranteed. Each run's `start` trace event records the resolved
provider.

**`.env` overrides the process environment**, not the other way round — an exported shell
variable is silently beaten by a non-empty `.env` line (`prod_agent/config.py:58`).

`AGENTSWITCH_TOKEN`, `AGENTSWITCH_BASE_URL` and `AGENTSWITCH_INSTANCE` belong to the platform's
harness runner and are shell-only, read from `os.environ` like `AGENT_OFFLINE` below. With
`AGENTSWITCH_TOKEN` set the code is in **platform mode**: `.env` is not read at all (the
runner's `OPENAI_*` win), `Session` uses the given token and base URL for that one instance
(no login, no `.tokens/`), `LLM_PROVIDER` is ignored, and `harness.runner` runs a preflight,
keeps a 27-minute budget (`HARNESS_BUDGET_MINUTES` overrides) and rewrites `results.json`
after every task. Each submission also has one LLM token budget (~300k) shared by both
instances, so the toml runs `--platform-set --token-budget 130000`: only tasks marked
`"platform": true`, each started only while its `max_tokens` fits. Adding a platform task
must keep `tests_ai/test_harness_toml.py` green, which checks the set still fits. Unset them before running tests; `tests_ai/conftest.py` clears them for
the AI-written tests.

`AGENT_OFFLINE` is the other exception, and it is **shell-only**: putting it in `.env` does nothing.
The root `conftest.py` reads it straight from `os.environ` during collection, which happens before
anything calls `load_dotenv`, precisely so a stray `.env` line cannot silently switch the gate on
or off. Set to exactly `1`, it skips every test that depends on a live-tenant fixture — `surya`,
`suryodaya` or `keystone` — before the fixture can log in. Any other value, including empty, means
"not offline". The gate only applies when pytest actually loads the root `conftest.py`, so
`cd tests && pytest .`, an absolute tests path from an unrelated directory, and `--noconftest` all
bypass it: invoke pytest from the repository root.

## Hard constraints (enforced by scripts/verify_submission.py)

These fail the submission check, not just review:

- **No agent framework.** The loop is hand-rolled; the checker greps `agent.py` for
  `for step in range(self.max_steps)` and fails if langchain/llama/autogen/crewai appears
  in `requirements.txt`.
- **Verifiers grade the database, never the reply text.** The string `final_answer` must
  not appear in `harness/verifiers/team04.py`.
- **Never write to `tests/`.** Those tests must stay human-authored — they are worth 10
  points each, and AI-authored ones score zero. The checker enforces this by failing if
  the string "claude" appears in any `tests/test_*.py` file, so do not add AI-authorship
  labels, attribution, or tool names there either. AI-written tests belong in `tests_ai/`,
  which is ungraded.
- **`GAP_REPORT.md` has a 1100-word cap.**
- Nothing may be hardcoded to one country or currency — read it from `Company`.

## Domain rules that are easy to get wrong

- Downstream impact from a BOM match is **potential**, never confirmed:
  `potentially_blocked_work_orders` with `confidence: potential`. Only a sales order on
  the late order itself is `linked`.
- **Refusing correctly is a success**, not a failure — about a quarter of the task set is
  refusal cases (8 of the 33 tasks expect `refused`; `refuse_escalation_no_assignee_keystone`
  is named like one but is an answering task).
- Seat limits are **probed, not assumed**: 403 means a real limit, 404 means an invented
  capability name.
- Harness verdicts are `approve` / `revise` / `unevaluated`. **`unevaluated` never counts
  as a pass** — a verifier returns it when a task's premise has changed, rather than
  guessing.
- **Persist, then verify**: `result.json` is fsync'd to disk before any verifier runs.
  `verify_submission.py` checks mtimes to prove the ordering held.

## Platform quirks

- JSON-RPC errors arrive as **HTTP 200** — every MCP call must inspect the envelope.
  Some endpoints report refusals inside a success envelope.
- Reads are retried on 502/503/504; **writes are never retried**.
- Once any write reports `changed_underneath`, every further write in that run is refused.
- `temperature=0` is only sent for `gpt-4*`/`gpt-3*` models, matched after any `vendor/`
  namespace is stripped so `openai/gpt-4.1` still counts; reasoning models reject it. This
  is an allow-list on purpose: a miss only costs determinism, whereas sending the parameter
  to a model that rejects it raises from an LLM call that has no `try` around it.

## Code style

`ruff.toml` configures `ruff check` for correctness rules only (pyflakes, bugbear, pyupgrade and the
E4/E7/E9 errors; line length and style are off). Ruff is not in `requirements.txt` and the checker does
not run it. No formatter is used: running `black`/`ruff format` would reformat the whole repo — don't.
Match what's there: double quotes, ~120-char lines, PEP 604 unions and
builtin generics (`dict | None`, `list[dict]`, never `typing.Optional`/`List`), plain dicts
as the data interchange format (no dataclasses or pydantic), stdlib → third-party → local
imports. Comments explain *why the platform behaves oddly*, not what the code does.

## Run output

Agent runs land in `runs/adhoc/<timestamp>-<instance>/` (console questions in
`runs/adhoc/<timestamp>/<instance>/ask/`), harness runs in
`runs/<timestamp>/<instance>/<task_id>/`. `runs/demo/` holds console runs; the checker never reads it.
A local `python -m harness.runner` holds `runs/.console-run.lock` (the console takes the same lock), so a
second local run, from a terminal or the console, refuses to start while one is in progress.
`runs/` is git-ignored, but a few run directories are **deliberately committed as grading evidence** — never
delete or regenerate those.
