# AI-written regression tests

Everything in this folder was written by an AI assistant — most of it by Claude, some by Codex, as each file's docstring records. It is **not** part of the team's hand-written tests and scores nothing under the course rules. The graded tests are in `tests/`.

Purpose: catch regressions in the agent and changes in the platform. Two tests in `test_ai_regression.py`, named for platform bugs B1 and B3, now assert those bugs stay fixed and fail if either comes back.

Everything runs without the live tenants except the 21 tests in `test_ai_regression.py` that use a live-tenant fixture; `AGENT_OFFLINE=1` (from the repository root) skips those. The domain and agent-guard behaviour tests pin their own date (`pin_today` in `test_behaviour_fakes.py`, which also stops `.env` being read). For the rest, a non-empty `AGENT_TODAY` in `.env` overrides the shell value below.

The `xfail(strict=True)` tests at the end of `test_behaviour_domain.py` record open production defects, not fixed here: each fails today and turns into an XPASS failure the day the code changes. They cover customer impact reported as "none linked" when every SalesOrder read fails; a shortage from a stock check flagged `stock_check_unreliable` still blocking; a received or in-quality-check subcontract flagged `subcontract_overdue`; a dependant of an in-progress order whose `why_not_writable` names no cause; and a past-due draft counted late.

`test_behaviour_fakes.py` holds the shared fake book (`Book`, `pin_today`, ...), imported as `from test_behaviour_fakes import ...`. `tests_ai/conftest.py` puts this folder on `sys.path`, so that import works under pytest's default import mode and under `--import-mode=importlib` alike. Keep the folder free of an `__init__.py`, which would turn it into a package and change the module name.

```powershell
$env:AGENT_TODAY = "2026-09-16"
python -m pytest tests_ai -v
```
