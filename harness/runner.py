"""Run the task set.

    python -m harness.runner                         # all tasks, all instances they declare
    python -m harness.runner --instance keystone --task refuse_unknown_work_order
    python -m harness.runner --include-samples
    python -m harness.runner --workers 3             # read-only tasks three at a time; writers still run alone
    python -m harness.runner --task why_late_wo48_subcontract --runs-dir runs/demo   # kept out of the checker's view

Order per task: task.json -> fixture.json -> trace.jsonl (streamed) -> result.json, all fsync'd,
and only then the verifier runs and writes verdict.json. Every run also keeps results.json current, the
file AgentSwitch's harness runner reads (agentswitch-harness.toml, harness/results.py).

Under that runner (AGENTSWITCH_TOKEN set) a run serves the one instance it was given, with the runner's
seat token and model, inside a time budget, after a preflight that checks both doors and the model.

A local run holds runs/.console-run.lock, the same lock the console (viewer/jobs.py) takes, so two runs never
share a seat at once: verifiers attribute writes by seat and time window, and would grade each other's.
"""
import argparse
import datetime as dt
import email.utils
import http.client
import importlib
import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent import futures
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows has no flock: a local run there is unguarded, as the console is (viewer/jobs.py)
    fcntl = None

from prod_agent import config, domain
from prod_agent.agent import NO_CREDIT, ProductionAgent, default_llm, probe_reasoning_off, probe_tool_choice
from prod_agent.mcp_client import McpClient, RestClient, Session

from .fixtures import FIXTURES, WRITING_FIXTURES, concurrent_edit
from .results import RESULTS_NAME, ResultsBook, diagnostics
from .verify import Verdict, VerifyContext, normalise

TASK_DIR = Path(__file__).parent / "tasks"


COLLISION_WAIT_SECONDS = 0.5

# AgentSwitch's harness runner allows timeout_minutes (at most 30) per instance and shows nothing at all if
# results.json is missing when it stops a run, so a platform run keeps its own clock well inside that.
PLATFORM_BUDGET_MINUTES = 27
TASK_SECONDS_DEFAULT = 240    # a task's own limit unless its json sets max_seconds; applied only under a budget
MIN_TASK_START_SECONDS = 90   # a task started with less than this left would only be cut off
VERIFY_RESERVE_SECONDS = 30   # kept back from a task's limit for its verifier's reads
WATCHDOG_GRACE_SECONDS = 90   # past the budget, end the process with results.json as it stands

# The runner also gives each submission one LLM token budget shared by every instance it runs: 2026-10-12 spent
# 304,284 tokens on Suryodaya's first three tasks and Keystone's first call was refused. A run therefore gets its own
# share (--token-budget), and a task starts only when its whole limit still fits, so every listed task can finish.
TASK_TOKENS_DEFAULT = 30_000  # a task's own token limit unless its json sets max_tokens

PREFLIGHT_TITLE = "The harness reaches the instance (MCP and REST) and the model with the runner's credentials"

# One run at a time per checkout. Verifiers judge "this seat wrote nothing" by every write the team account made
# since the task started (verify.py my_writes_since_start), so a second run on the same tenant fails the first
# one's read-only tasks; it happened when a console run and a terminal run overlapped. The console takes this
# same file before it starts a child, so the path must match viewer/jobs.py's default.
RUN_LOCK_PATH = config.ROOT / "runs" / ".console-run.lock"
# flock belongs to an open file description: a console-launched runner already holds the lock through the fd it
# inherited, and a fresh open() of the file would be refused by its own parent's lock. The console names that fd.
RUN_LOCK_FD_ENV = "HARNESS_RUN_LOCK_FD"


class RunLocked(Exception):
    pass


def _inherited_lock_fd(path: Path) -> int | None:
    """The fd the console handed down, if it really is open on the lock file; a stale or stray value is ignored."""
    raw = os.environ.get(RUN_LOCK_FD_ENV, "")
    if not raw.isdigit():
        return None
    try:
        held, want = os.fstat(int(raw)), os.stat(path)
    except (OSError, OverflowError):
        return None
    return int(raw) if (held.st_dev, held.st_ino) == (want.st_dev, want.st_ino) else None


def hold_run_lock(path: Path | None = None) -> int | None:
    """Take the run lock (exclusive, never waiting) and return its fd, to keep open for the whole run.

    Re-locking an inherited fd succeeds because its open file description already holds the lock; if the console
    has since let go, it simply takes it again. None where there is no flock (Windows)."""
    if fcntl is None:
        return None
    path = path or RUN_LOCK_PATH
    fd = _inherited_lock_fd(path)
    if fd is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        raise RunLocked(f"another harness run holds {path} ({e.strerror}); wait for it to finish, a second run "
                        "on the same seat would make the verifiers grade each other's writes") from None
    return fd


def release_run_lock(fd: int | None) -> None:
    if fd is not None:
        os.close(fd)  # the last close of the open file description drops the flock


def _new_run_root(base: Path) -> Path:
    # Run output is grading evidence, and reusing a collision silently produces merged artifacts that still parse.
    # datetime.now() resolves to ~16ms on Windows, so %f alone does not separate two runs started in the same tick.
    # Wait for the clock to advance rather than reusing a directory or suffixing a name the checker cannot glob.
    # A clock that never advances exhausts the budget and still raises, so a genuine collision is never absorbed.
    deadline = time.monotonic() + COLLISION_WAIT_SECONDS
    while True:
        root = base / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        try:
            root.mkdir(parents=True)
            return root
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def _write(path: Path, data) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())


def load_tasks(include_samples: bool) -> list[dict]:
    tasks = []
    for path in sorted(TASK_DIR.rglob("*.json")):
        task = json.loads(path.read_text(encoding="utf-8"))
        task["_path"] = str(path.relative_to(config.ROOT))
        if task.get("sample") and not include_samples:
            continue
        tasks.append(task)
    return tasks


def _load_verifier(ref: str):
    module, _, func = ref.partition(":")
    return getattr(importlib.import_module(module), func)


def _find_by_number(rest: RestClient, entity: str, number: str) -> dict | None:
    hits = [r for r in rest.list(entity, search=number) if r.get("number") == number]
    return rest.get(entity, hits[0]["id"]) if hits else None


def _server_now_utc(base: str) -> dt.datetime:
    """The server's clock (HTTP Date header), so run-start comparisons with updated_at are not skewed by ours."""
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{base}/api/auth/me", method="HEAD"), timeout=30) as resp:
            headers = resp.headers
    except urllib.error.HTTPError as e:  # a 401 still carries the server's Date header
        headers = e.headers
    # timezone.utc, not datetime.UTC (3.11+): the platform's harness runner may run Python 3.10.
    return email.utils.parsedate_to_datetime(headers["Date"]).astimezone(dt.timezone.utc).replace(tzinfo=None)


def capture_context(rest: RestClient, task: dict) -> dict:
    """Who we are, when the run started (server clock, UTC), and rows as they were before the run."""
    try:
        started = _server_now_utc(rest.session.base)
    # A dropped or timed-out HEAD (RemoteDisconnected, ConnectionResetError, TimeoutError: all OSError, as is
    # URLError) or a garbled reply (HTTPException) costs only clock precision, never the task.
    except (OSError, http.client.HTTPException, KeyError, TypeError, ValueError):
        started = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    started -= dt.timedelta(seconds=2)
    return {
        "me": rest.raw("/api/auth/me")["id"],
        "started_at_utc": started.isoformat(),
        "snapshot": {s["number"]: _find_by_number(rest, s["entity"], s["number"]) for s in task.get("snapshot", [])},
    }


def persist_then_verify(run_dir: Path, result: dict, verify, *, catch: bool = True):
    """Write result.json (fsync'd) first, and only then run the verifier.

    `verify` is a zero-argument callable returning a Verdict or (Verdict, reason), or None.
    A verifier that raises yields UNEVALUATED unless catch=False.
    """
    _write(run_dir / "result.json", result)  # persisted before any scoring
    if verify is None:
        return Verdict.UNEVALUATED, "no verifier"
    try:
        return normalise(verify())
    except Exception:
        if not catch:
            raise
        return Verdict.UNEVALUATED, "verifier raised:\n" + traceback.format_exc()


NO_TOKENS = {"prompt": 0, "completion": 0, "total": 0}


def _prompt_for(task: dict, fixture: dict | None) -> tuple[str | None, str | None]:
    """(the prompt, None), or (None, why it cannot be asked honestly today).

    Fixtures return {} when today's data holds no honest target. Asking anyway used to raise KeyError on the
    prompt's placeholder, so the run was a harness error and the verifier's own "nothing to check" never ran."""
    if task.get("fixture") and not fixture:
        return None, "fixture found no target on this instance today"
    # Every text field of a fixture role, so a prompt can name an order by what it makes ({target_item})
    # as well as by number ({target_number}).
    values = {f"{role}_{k}": val for role, v in (fixture or {}).items()
              if isinstance(v, dict) for k, val in v.items() if isinstance(val, str)}
    try:
        return task["prompt"].format(**values), None
    except KeyError as e:
        return None, f"the prompt needs {{{e.args[0]}}}, which the fixture did not supply today"


def _harness_error(error: str, agent: ProductionAgent | None) -> dict:
    result = {"run_id": None, "stop_reason": "harness_error", "error": error}
    if agent is None:  # no model call was ever made, so nothing was spent
        return {**result, "tokens": dict(NO_TOKENS)}
    # The agent may have raised mid-step, after spending tokens it never counted: "tokens" stays unknown so the
    # run's budget keeps this task's whole limit (execute), and what was counted is kept for the record.
    return {**result, "escalations": agent.escalations, "agent_session_id": agent.session_id,
            "tokens": None, "tokens_counted": dict(agent.tokens)}


def run_one(task: dict, instance: str, root: Path, deadline: float | None = None,
            forced_tool_choice: str = "named", token_budget: int | None = None,
            request_extra: dict | None = None) -> dict:
    run_dir = root / instance / task["id"]
    run_dir.mkdir(parents=True, exist_ok=False)
    _write(run_dir / "task.json", task)

    fixture, context, error, mcp, agent, trace_file, skipped = None, None, None, None, None, None, None
    try:
        try:
            session = Session(instance)
            mcp = McpClient(session)
            if task.get("fixture"):
                fixture = FIXTURES[task["fixture"]](mcp)
            _write(run_dir / "fixture.json", fixture)
            prompt, skipped = _prompt_for(task, fixture)
            if skipped is None:
                context = capture_context(RestClient(session), task)
                _write(run_dir / "context.json", context)

                trace_file = (run_dir / "trace.jsonl").open("x", encoding="utf-8")

                def trace(event):
                    trace_file.write(json.dumps(event, default=str) + "\n")
                    trace_file.flush()
                    os.fsync(trace_file.fileno())

                write_ids = set((fixture or {}).get("write_ids", []))
                interference: list[dict] = []

                def approve(p):
                    if p["work_order_id"] not in write_ids:  # harness approves fixture rows only
                        return False
                    already_edited = any(i["id"] == p["work_order_id"] for i in interference)
                    if task.get("interference") == "edit_dates_before_apply" and not already_edited:
                        edit = concurrent_edit(McpClient(Session(instance)), p["work_order_id"])
                        interference.append(edit)
                        _write(run_dir / "interference.json", interference)  # on disk before the agent's write
                        trace({"type": "interference", **edit})
                    return True

                agent = ProductionAgent(
                    mcp, apply_mode=task.get("mode") == "apply", allowed_write_ids=write_ids, approve=approve,
                    escalate_mode=bool(task.get("escalate")),
                    session_title=f"{config.HARNESS_MARKER} task {task['id']}",
                    trace=trace, max_steps=task.get("max_steps", 20), deadline=deadline,
                    forced_tool_choice=forced_tool_choice, token_budget=token_budget, request_extra=request_extra,
                    require_finding=True)
                result = agent.run(prompt)
        except Exception:
            error = traceback.format_exc()
            result = _harness_error(error, agent)
        finally:
            if trace_file is not None:
                trace_file.close()
        if skipped is not None:  # the agent never ran: nothing to grade, and no tokens spent on a false premise
            result = {"run_id": None, "stop_reason": "no_target", "tokens": dict(NO_TOKENS)}
        result["instance"], result["task_id"] = instance, task["id"]

        def verify():
            ctx = VerifyContext(rest=RestClient(Session(instance)), instance=instance, task=task,
                                run_dir=run_dir, fixture=fixture, context=context)
            return _load_verifier(task["verifier"])(ctx)

        if error:
            verdict, reason = persist_then_verify(run_dir, result, None)
            reason = "run failed before verification"
        elif skipped is not None:
            verdict, reason = persist_then_verify(run_dir, result, None)
            reason = skipped
        elif not task.get("verifier"):
            verdict, reason = persist_then_verify(run_dir, result, None)
            reason = "no verifier declared"
        else:
            verdict, reason = persist_then_verify(run_dir, result, verify)
        record = {"task_id": task["id"], "instance": instance, "verdict": verdict.value, "reason": reason,
                  "sample": bool(task.get("sample")), "run_id": result.get("run_id"),
                  "seconds": result.get("seconds"), "steps": result.get("steps"), "tokens": result.get("tokens"),
                  "stop_reason": result.get("stop_reason"), "llm_error_code": result.get("llm_error_code")}
        _write(run_dir / "verdict.json", record)
        return record
    finally:
        # Whatever ended the task (a verdict, an exception from the agent or the verifier, Ctrl-C), an escalation
        # the agent raised is a real person assigned to a test: withdraw it from the agent's own list, which holds
        # it even when no result was ever returned. Runs after verdict.json, so verifiers still see it raised.
        if agent is not None and (agent.escalations or agent.session_id):
            try:
                _write(run_dir / "cleanup.json", cleanup_escalations(
                    mcp, {"escalations": agent.escalations, "agent_session_id": agent.session_id}))
            except Exception:  # never mask the exception (or interrupt) already on its way out
                print("cleanup failed:\n" + traceback.format_exc(), file=sys.stderr, flush=True)


def cleanup_escalations(mcp: McpClient, result: dict) -> dict:
    """After scoring, withdraw escalations the harness caused so no person is left chasing a test."""
    done = {"withdrawn": [], "session_closed": None, "errors": []}
    for esc in result.get("escalations") or []:
        if esc.get("raised") and esc.get("escalation_id"):
            try:
                domain.withdraw_escalation(mcp, esc["escalation_id"], "team04 harness test run: withdrawn after scoring")
                done["withdrawn"].append(esc.get("number"))
            except Exception as e:  # cleanup must never change a verdict
                done["errors"].append(f"{esc.get('number')}: {e}")
    if result.get("agent_session_id"):
        try:
            mcp.call("AgentSession.close.active.closed", {"id": result["agent_session_id"]})
            done["session_closed"] = result["agent_session_id"]
        except Exception as e:
            done["errors"].append(f"session: {e}")
    return done


# ----------------------------------------------------------------------------- planning a run

def budget_seconds(on_platform: bool) -> float | None:
    """HARNESS_BUDGET_MINUTES if set, else the platform budget under the runner, else none: local runs are unlimited."""
    raw = os.environ.get("HARNESS_BUDGET_MINUTES")
    if raw:
        return float(raw) * 60
    return PLATFORM_BUDGET_MINUTES * 60 if on_platform else None


def order_plan(plan: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    """Cheapest first, by each task's own limit, so a slow day costs the expensive tasks at the end rather than a
    string of quick ones. Stable, so tasks with equal limits keep their file order."""
    return sorted(plan, key=lambda p: p[0].get("max_seconds", TASK_SECONDS_DEFAULT))


def isolated(task: dict) -> bool:
    """Safe to run beside other tasks. Verifiers count every work order write and every escalation this seat made
    since a task started, so a task that may write or escalate, or whose fixture writes, always runs alone."""
    return (task.get("mode") == "dry_run" and not task.get("escalate") and not task.get("interference")
            and task.get("fixture") not in WRITING_FIXTURES)


def task_deadline(task: dict, started: float, budget: float | None, now: float) -> tuple[bool, float | None]:
    """(start it?, deadline on the monotonic clock). No budget, no deadline: local runs keep the step budget only."""
    if budget is None:
        return True, None
    left = budget - (now - started)
    if left < MIN_TASK_START_SECONDS:
        return False, None
    return True, now + min(task.get("max_seconds", TASK_SECONDS_DEFAULT), left - VERIFY_RESERVE_SECONDS)


def preflight(instance: str) -> dict:
    """Before any task spends budget: the runner's token opens both doors this harness uses (the agent calls MCP,
    verifiers read REST), and the model takes a forced tool call in some form. Raises with the reason if not."""
    session = Session(instance)
    tools = McpClient(session).tool_names()
    if not tools:
        raise RuntimeError("tools/list returned no tools for this seat")
    me = RestClient(session).raw("/api/auth/me")["id"]
    model = config.env("OPENAI_MODEL", "gpt-4.1")
    llm = default_llm("openai")
    thinking = probe_reasoning_off(llm, model)
    return {"tools": len(tools), "me": me, "model": model, "thinking": thinking,
            **probe_tool_choice(llm, model, **thinking["request_extra"])}


def execute(plan: list[tuple[dict, str]], root: Path, book: ResultsBook, *, budget: float | None,
            workers: int = 1, forced_tool_choice: str = "named", clock=time.monotonic,
            token_budget: int | None = None, request_extra: dict | None = None) -> list[dict]:
    """Run the plan, keeping results.json current after every task. With workers > 1 the isolated tasks run that
    many at a time first, then the rest one by one. Under a token budget a task starts only if its whole limit is
    still free; its limit is held while it runs and what it did not spend is handed back, if that is known."""
    started, records, lock = clock(), [], threading.Lock()
    held = [0]          # tokens spent by finished tasks plus the limits of running ones
    spent_out = [None]  # set once the platform says the run's budget is gone: no later task can succeed

    def one(task: dict, instance: str) -> None:
        go, deadline = task_deadline(task, started, budget, clock())
        cap = None
        with lock:
            if spent_out[0]:
                go, why = False, spent_out[0]
            elif not go:
                why = "time budget"
            elif token_budget is not None:
                cap = task.get("max_tokens", TASK_TOKENS_DEFAULT)
                go, why = token_budget - held[0] >= cap, "token budget"
                if go:
                    held[0] += cap
        if not go:
            book.not_run(task, instance, why)
            book.write()
            return
        book.started(task, instance)
        book.write()
        try:
            rec = run_one(task, instance, root, deadline=deadline, forced_tool_choice=forced_tool_choice,
                          token_budget=cap, request_extra=request_extra)
        except Exception:
            # A task that cannot even start is unevaluated, and must not take the rest of the run with it.
            tb = traceback.format_exc()
            print(tb, file=sys.stderr)
            rec = {"task_id": task["id"], "instance": instance, "verdict": Verdict.UNEVALUATED.value,
                   "reason": "harness error: " + tb.strip().splitlines()[-1], "sample": bool(task.get("sample")),
                   "run_id": None, "seconds": None}
        with lock:
            records.append(rec)
            # Hand back only what a task is known not to have spent. A crash after the agent started leaves its
            # spend unknown (no "tokens"), and handing the whole limit back could overspend the platform's budget.
            if cap is not None and rec.get("tokens") is not None:
                held[0] += (rec["tokens"].get("total") or 0) - cap
            if rec.get("llm_error_code") in NO_CREDIT:
                spent_out[0] = f"the platform's LLM budget is used up ({rec['llm_error_code']})"
        book.record(task, instance, rec)
        book.write()
        print(f"{rec['verdict']:12} {instance:10} {task['id']}  {diagnostics(rec)}  {rec['reason'][:100]}", flush=True)

    serial = plan
    if workers > 1:
        together = [p for p in plan if isolated(p[0])]
        serial = [p for p in plan if not isolated(p[0])]
        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda p: one(*p), together))
    for task, instance in serial:
        one(task, instance)
    return records


def _start_watchdog(seconds: float, book: ResultsBook) -> threading.Timer:
    """End the process before the runner's own timeout can. results.json is current after every task, so what it
    holds at that moment is the run's result; one stuck call (a propose_reschedule took 73 minutes on 2026-10-07)
    would otherwise take every finished task down with it."""
    def expire():
        book.unfinished("the run's time budget ran out")
        book.write()
        print(f"time budget exhausted after {seconds / 60:.1f} min; results.json written, exiting", flush=True)
        os._exit(0)

    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    timer.start()
    return timer


def watchdog_for(platform: dict | None, budget: float | None, book: ResultsBook) -> threading.Timer | None:
    """Armed only on the runner's throwaway copy. os._exit skips cleanup_escalations, so on a shared tenant it
    would leave a real person assigned to a test escalation; a local budget keeps its per-task deadlines only."""
    if budget is None or not platform:
        return None
    return _start_watchdog(budget + WATCHDOG_GRACE_SECONDS, book)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", choices=sorted(config.INSTANCES) + ["all"], default="all")
    ap.add_argument("--task", action="append", help="task id (repeatable)")
    ap.add_argument("--include-samples", action="store_true")
    ap.add_argument("--workers", type=int, default=1,
                    help="run read-only tasks this many at a time; tasks that write or escalate always run alone")
    ap.add_argument("--platform-set", action="store_true",
                    help='only tasks marked "platform": true, the set sized for the harness runner\'s token budget')
    ap.add_argument("--token-budget", type=int, default=None,
                    help="LLM tokens this run may spend; a task starts only if its max_tokens still fits")
    # Partial runs (a live demo of a few tasks) go elsewhere: the checker grades the newest complete runs/2026* root.
    ap.add_argument("--runs-dir", type=Path, default=config.ROOT / "runs")
    args = ap.parse_args()

    book = ResultsBook(Path.cwd() / RESULTS_NAME, label=args.instance)
    try:
        platform = config.platform()
    except RuntimeError as e:  # a half-set runner environment: say so in the one file the runner reads
        book.label = os.environ.get("AGENTSWITCH_INSTANCE") or "platform"
        book.fail("preflight", PREFLIGHT_TITLE, str(e))
        book.write()
        print(f"preflight failed: {e}")
        return 0
    if platform and args.instance not in ("all", platform["instance"]):
        ap.error(f"this run serves {platform['instance']} only (AGENTSWITCH_INSTANCE)")
    if platform:
        allowed = {platform["instance"]}
    else:
        allowed = set(config.INSTANCES) if args.instance == "all" else {args.instance}
    book.label = ", ".join(sorted(allowed))

    loaded = load_tasks(args.include_samples)
    if not platform and args.task:
        # A typo used to exit 0 with an empty run root; refuse before anything is created. Sample ids are known,
        # so naming one without --include-samples is caught by the empty-plan check below instead.
        known = {t["id"] for t in load_tasks(True)}
        if unknown := sorted(set(args.task) - known):
            ap.error(f"unknown task id(s): {', '.join(unknown)}")
    tasks = [t for t in loaded if not args.task or t["id"] in args.task]
    if args.platform_set:
        tasks = [t for t in tasks if t.get("platform")]
    plan = order_plan([(t, i) for t in tasks for i in t.get("instances", sorted(config.INSTANCES)) if i in allowed])
    # Locally an empty plan is a mistake (a task on an instance it does not declare, a sample without
    # --include-samples) and would leave an empty run root. Under the platform runner it is not refused: results.json
    # then carries the failed no_tasks entry, which is what the runner's panel shows (results.py doc()).
    if not plan and not platform:
        print(f"no task to run on {book.label}: check --task, --instance, --platform-set and --include-samples "
              "(a task runs only on the instances its json declares)", file=sys.stderr)
        return 2
    try:
        # The platform runner's copy of the instance is private to this one submission, so nothing else can share
        # its seat, and a lock problem there would cost the team's one run in three days: only local runs lock.
        run_lock = None if platform else hold_run_lock()
    except (RunLocked, OSError) as e:
        print(f"not started: {e}", file=sys.stderr)
        return 3
    try:
        return _run(args, plan, book, platform)
    finally:
        release_run_lock(run_lock)


def _run(args, plan: list[tuple[dict, str]], book: ResultsBook, platform: dict | None) -> int:
    for task, instance in plan:
        book.plan(task, instance)
    book.write()  # every planned task is on file before the first one starts

    choice, request_extra = "named", {}
    if platform:
        try:
            pre = preflight(platform["instance"])
        except Exception as e:
            book.fail("preflight", PREFLIGHT_TITLE, f"{type(e).__name__}: {e}")
            book.unfinished("preflight failed")
            book.write()
            print(f"preflight failed: {type(e).__name__}: {e}")
            return 0
        choice, request_extra = pre["mode"], pre["thinking"]["request_extra"]
        honoured = "" if pre["tool_called"] else " (accepted, not honoured)"
        probe = pre["thinking"]
        thinking = "thinking: no off switch accepted" if not probe["setting"] else \
            f"thinking: {probe['setting']} ({probe['tokens']['completion']} completion tokens to say ok)"
        book.note = f"model {pre['model']}, tool_choice {pre['mode']}{honoured}, {thinking}, {pre['tools']} MCP tools"
        print("preflight thinking probe: " + "; ".join(probe["tried"]), flush=True)
        print(f"preflight: {book.note}", flush=True)

    budget = budget_seconds(bool(platform))
    watchdog_for(platform, budget, book)

    root = _new_run_root(args.runs_dir)
    records = execute(plan, root, book, budget=budget, workers=args.workers, forced_tool_choice=choice,
                      token_budget=args.token_budget, request_extra=request_extra)

    scored = [r for r in records if not r["sample"]]
    summary = {"runs": records, "approved": sum(r["verdict"] == "approve" for r in scored),
               "revise": sum(r["verdict"] == "revise" for r in scored),
               "unevaluated": sum(r["verdict"] == "unevaluated" for r in scored), "scored_total": len(scored)}
    if records:
        _write(root / "summary.json", summary)
    found = book.write()
    print(f"\n{summary['approved']}/{summary['scored_total']} approved "
          f"({summary['unevaluated']} unevaluated, never a pass). Runs in {root}")
    print(f"{RESULTS_NAME}: {book.doc()['summary']}")
    if found:
        print(f"{RESULTS_NAME} would not be read: " + "; ".join(found))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
