"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-09-30).

Ungraded. Cover the local run console (viewer/) without a network, a model or a live tenant.
The graded, hand-written tests live in tests/.
"""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from prod_agent import config
from viewer import jobs as jobs_module
from viewer import runs
from viewer.jobs import JobError, JobRunner
from viewer.server import make_server

ROOT = "20260930-101500-000001"


def _task_dir(base, root, instance, task, verdict=None, trace=()):
    d = base / root / instance / task
    d.mkdir(parents=True)
    (d / "result.json").write_text(json.dumps({"run_id": "r1"}), encoding="utf-8")
    (d / "trace.jsonl").write_text("".join(json.dumps(e) + "\n" for e in trace), encoding="utf-8")
    if verdict:
        (d / "verdict.json").write_text(json.dumps({"verdict": verdict, "reason": "because"}), encoding="utf-8")
    return d


def test_task_without_a_verdict_is_listed_as_unscored_never_approved(tmp_path):
    _task_dir(tmp_path, ROOT, "suryodaya", "refuse_x", verdict="approve")
    _task_dir(tmp_path, ROOT, "keystone", "refuse_x")

    [root] = runs.list_run_roots(tmp_path)

    assert root["complete"] is False
    assert {t["instance"]: t["verdict"] for t in root["tasks"]} == {"keystone": None, "suryodaya": "approve"}


def test_listing_skips_adhoc_demo_and_other_folders_and_reads_the_summary(tmp_path):
    for name in ("adhoc", "demo", "notes"):
        (tmp_path / name).mkdir()
    full = tmp_path / "20260917-110938"
    full.mkdir()
    (full / "summary.json").write_text(json.dumps({"approved": 30, "revise": 0, "unevaluated": 0,
                                                   "scored_total": 30}), encoding="utf-8")

    [root] = runs.list_run_roots(tmp_path)

    assert root["name"] == "20260917-110938"
    assert root["complete"] is True
    assert (root["approved"], root["scored_total"]) == (30, 30)


def test_newest_run_is_listed_first(tmp_path):
    for name in ("20260916-114054", ROOT):
        (tmp_path / name).mkdir()

    assert [r["name"] for r in runs.list_run_roots(tmp_path)] == [ROOT, "20260916-114054"]


def test_missing_base_lists_nothing(tmp_path):
    assert runs.list_run_roots(tmp_path / "demo") == []


def test_trace_read_leaves_a_half_written_line_for_the_next_read(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_bytes(b'{"type": "start"}\n{"type": "ll')

    events, offset = runs.read_trace(path)
    assert events == [{"type": "start"}]

    with path.open("ab") as f:
        f.write(b'm", "step": 0}\n')
    events, offset = runs.read_trace(path, offset)
    assert events == [{"type": "llm", "step": 0}]
    assert runs.read_trace(path, offset) == ([], offset)


def test_missing_trace_reads_as_no_events(tmp_path):
    assert runs.read_trace(tmp_path / "trace.jsonl", 5) == ([], 5)


@pytest.mark.parametrize("root,instance,task", [
    ("../etc", "suryodaya", "refuse_x"),
    (ROOT, "elsewhere", "refuse_x"),
    (ROOT, "suryodaya", "../../.env"),
    (ROOT, "suryodaya", "missing_task"),
])
def test_task_dir_refuses_anything_outside_the_run_layout(tmp_path, root, instance, task):
    _task_dir(tmp_path, ROOT, "suryodaya", "refuse_x")

    assert runs.resolve_task_dir(tmp_path, root, instance, task, {"suryodaya", "keystone"}) is None


def test_task_dir_resolves_a_real_run(tmp_path):
    d = _task_dir(tmp_path, ROOT, "suryodaya", "refuse_x")

    assert runs.resolve_task_dir(tmp_path, ROOT, "suryodaya", "refuse_x", {"suryodaya", "keystone"}) == d


def test_task_run_reports_the_same_write_ordering_the_checker_requires(tmp_path):
    d = _task_dir(tmp_path, ROOT, "suryodaya", "refuse_x", verdict="approve", trace=[{"type": "start"}])
    os.utime(d / "result.json", (1000, 1000))
    os.utime(d / "verdict.json", (2000, 2000))

    run = runs.load_task_run(d)
    assert run["persisted_before_verdict"] is True
    assert run["verdict"]["verdict"] == "approve"
    assert run["trace"] == [{"type": "start"}]
    assert run["fixture"] is None

    os.utime(d / "result.json", (3000, 3000))
    assert runs.load_task_run(d)["persisted_before_verdict"] is False


TASKS = [{"id": "refuse_x", "instances": ["keystone", "suryodaya"], "prompt": "Cancel WO-1.",
          "checks": "Refuses; no write."},
         {"id": "why_late", "instances": ["suryodaya"]}]


def fake_popen(procs):
    """Stands in for subprocess.Popen: records the call and creates the run root the real runner would."""
    def popen(cmd, **kwargs):
        proc = SimpleNamespace(cmd=cmd, kwargs=kwargs, code=None)
        proc.poll = lambda: proc.code
        if "--runs-dir" in cmd:
            Path(cmd[cmd.index("--runs-dir") + 1], f"20260930-12000{len(procs)}-000001").mkdir()
        else:  # the agent CLI creates the exact directory it is handed
            Path(cmd[cmd.index("--run-dir") + 1]).mkdir(parents=True)
        procs.append(proc)
        return proc
    return popen


def test_start_runs_the_harness_cli_into_the_demo_directory(tmp_path):
    procs, demo = [], tmp_path / "demo"
    jobs = JobRunner(TASKS, demo, popen=fake_popen(procs))

    status = jobs.start("refuse_x", "keystone", "keystone/refuse_x")

    [proc] = procs
    assert proc.cmd == [sys.executable, "-m", "harness.runner", "--task", "refuse_x", "--instance", "keystone",
                        "--runs-dir", str(demo)]
    assert proc.kwargs["cwd"] == config.ROOT
    assert proc.kwargs["start_new_session"] is True
    assert status["running"] is True
    assert status["run_root"] == "20260930-120000-000001"


@pytest.mark.skipif(jobs_module.fcntl is None, reason="no flock on this platform")
def test_the_child_is_told_which_inherited_fd_holds_the_run_lock(tmp_path):
    """harness.runner takes the run lock itself; flock is per open file description, so without the fd's number a
    console-launched runner would be refused by the very lock its parent handed it."""
    procs = []
    JobRunner(TASKS, tmp_path / "demo", popen=fake_popen(procs)).start("refuse_x", "keystone", "keystone/refuse_x")

    [proc] = procs
    [fd] = proc.kwargs["pass_fds"]
    assert proc.kwargs["env"]["HARNESS_RUN_LOCK_FD"] == str(fd)
    assert proc.kwargs["env"]["PATH"] == os.environ["PATH"], "the rest of the environment is passed on unchanged"


@pytest.mark.parametrize("task,instance,confirm", [
    ("not_a_task", "suryodaya", "suryodaya/not_a_task"),
    ("why_late", "keystone", "keystone/why_late"),
    ("refuse_x", "suryodaya", "yes"),
    ("refuse_x", "suryodaya", ""),
])
def test_start_refuses_anything_not_on_the_allowlist_or_not_confirmed(tmp_path, task, instance, confirm):
    procs = []
    jobs = JobRunner(TASKS, tmp_path / "demo", popen=fake_popen(procs))

    with pytest.raises(JobError):
        jobs.start(task, instance, confirm)
    assert procs == []


def test_only_one_run_at_a_time(tmp_path):
    procs = []
    jobs = JobRunner(TASKS, tmp_path / "demo", popen=fake_popen(procs))
    jobs.start("refuse_x", "suryodaya", "suryodaya/refuse_x")

    with pytest.raises(JobError, match="in progress"):
        jobs.start("why_late", "suryodaya", "suryodaya/why_late")

    procs[0].code = 0
    assert jobs.status()["exit_code"] == 0
    jobs.start("why_late", "suryodaya", "suryodaya/why_late")
    assert len(procs) == 2
    assert jobs.status()["run_root"] == "20260930-120001-000001"
    assert jobs.status()["job_id"] == 2


def harmless_popen(children):
    """Real child processes that only sleep, so the lock file is held the way a real run holds it."""
    def popen(cmd, **kwargs):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child
    return popen


@pytest.mark.skipif(jobs_module.fcntl is None, reason="no flock on this platform")
def test_a_restarted_or_second_console_cannot_overlap_a_run_still_going(tmp_path):
    """The run outlives the console that started it (its own session), so the in-memory check alone forgets it."""
    children = []
    try:
        first = JobRunner(TASKS, tmp_path / "demo", popen=harmless_popen(children))
        first.start("refuse_x", "suryodaya", "suryodaya/refuse_x")

        restarted = JobRunner(TASKS, tmp_path / "demo", popen=harmless_popen(children))
        with pytest.raises(JobError, match="in progress"):
            restarted.start("why_late", "suryodaya", "suryodaya/why_late")
        assert len(children) == 1

        children[0].kill()
        children[0].wait()
        restarted.start("why_late", "suryodaya", "suryodaya/why_late")
        assert len(children) == 2
    finally:
        for child in children:
            child.kill()
            child.wait()


def test_no_status_before_any_run(tmp_path):
    assert JobRunner(TASKS, tmp_path / "demo").status() is None


def test_task_without_declared_instances_runs_on_every_instance(tmp_path):
    jobs = JobRunner([{"id": "plain"}], tmp_path / "demo")
    assert jobs.instances_for("plain") == sorted(config.INSTANCES)


@pytest.fixture
def console(tmp_path):
    committed = tmp_path / "runs"
    _task_dir(committed, ROOT, "suryodaya", "refuse_x", verdict="approve", trace=[{"type": "start"}])
    procs = []
    jobs = JobRunner(TASKS, tmp_path / "demo", popen=fake_popen(procs))
    server = make_server(0, {"committed": committed, "demo": tmp_path / "demo"}, jobs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.procs = procs
    yield server
    server.shutdown()
    server.server_close()


def _call(server, path, body=None, headers=None):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_page_carries_the_session_token(console):
    status, page = _call(console, "/")
    assert status == 200
    assert console.token in page and "{{TOKEN}}" not in page


def test_past_runs_and_one_task_run_are_served(console):
    status, body = _call(console, "/api/runs?base=committed")
    assert status == 200
    assert json.loads(body)[0]["tasks"][0]["verdict"] == "approve"

    status, body = _call(console, f"/api/run?base=committed&root={ROOT}&instance=suryodaya&task=refuse_x")
    assert status == 200
    assert json.loads(body)["trace"] == [{"type": "start"}]


def test_a_path_outside_the_run_layout_is_not_found(console):
    status, _ = _call(console, "/api/run?base=committed&root=..&instance=suryodaya&task=refuse_x")
    assert status == 404


def test_task_list_carries_what_each_task_tests(console):
    [task] = [t for t in json.loads(_call(console, "/api/tasks")[1]) if t["id"] == "refuse_x"]

    # The page shows these before a live run starts; for past runs it reads them from task.json instead.
    assert (task["prompt"], task["checks"]) == ("Cancel WO-1.", "Refuses; no write.")
    assert task["instances"] == ["keystone", "suryodaya"]


def test_a_foreign_host_header_is_refused(console):
    status, _ = _call(console, "/api/tasks", headers={"Host": "evil.test"})
    assert status == 403


def test_starting_a_run_needs_the_token_and_a_local_origin(console):
    body = {"task_id": "refuse_x", "instance": "suryodaya", "confirm": "suryodaya/refuse_x"}
    assert _call(console, "/api/jobs", body)[0] == 403
    assert _call(console, "/api/jobs", body, {"X-Console-Token": console.token, "Origin": "https://evil.test"})[0] == 403
    assert console.procs == []


def test_a_confirmed_start_runs_once_and_a_mismatched_one_is_refused(console):
    headers = {"X-Console-Token": console.token, "Content-Type": "application/json"}
    bad = {"task_id": "refuse_x", "instance": "suryodaya", "confirm": "yes"}
    assert _call(console, "/api/jobs", bad, headers)[0] == 400

    good = dict(bad, confirm="suryodaya/refuse_x")
    status, body = _call(console, "/api/jobs", good, headers)
    assert status == 200
    assert json.loads(body)["running"] is True
    assert len(console.procs) == 1
    assert json.loads(_call(console, "/api/jobs/current")[1])["task_id"] == "refuse_x"


def test_a_browser_start_with_the_local_origin_is_accepted(console):
    origin = f"http://127.0.0.1:{console.server_address[1]}"
    headers = {"X-Console-Token": console.token, "Origin": origin, "Content-Type": "application/json"}
    body = {"task_id": "refuse_x", "instance": "suryodaya", "confirm": "suryodaya/refuse_x"}

    assert _call(console, "/api/jobs", body, headers)[0] == 200
    assert len(console.procs) == 1


# ------------------------------------------------------------------ ask the agent


def test_ask_runs_the_agent_cli_read_only_with_the_question_after_the_options(tmp_path):
    procs = []
    jobs = JobRunner(TASKS, tmp_path / "demo", popen=fake_popen(procs), adhoc_base=tmp_path / "adhoc")

    status = jobs.ask("keystone", "  --apply everything now  ", "keystone/ask")

    [proc] = procs
    assert proc.cmd[:4] == [sys.executable, "-m", "prod_agent", "--instance"]
    assert proc.cmd[-2:] == ["--", "--apply everything now"]
    assert "--apply" not in proc.cmd[:-1] and "--escalate" not in proc.cmd
    run_dir = Path(proc.cmd[proc.cmd.index("--run-dir") + 1])
    assert run_dir.parent.name == "keystone" and run_dir.name == "ask"
    assert proc.kwargs["start_new_session"] is True
    assert (status["kind"], status["base"], status["task_id"]) == ("ask", "adhoc", "ask")
    assert status["run_root"] == run_dir.parent.parent.name
    assert runs.ROOT_NAME.fullmatch(status["run_root"])


@pytest.mark.parametrize("instance,text,confirm", [
    ("keystone", "   ", "keystone/ask"),
    ("keystone", "x" * 1001, "keystone/ask"),
    ("keystone", "why\x00late", "keystone/ask"),
    ("keystone", "Where is the shop floor stuck?", "yes"),
    ("keystone", "Where is the shop floor stuck?", "keystone/refuse_x"),
    ("elsewhere", "Where is the shop floor stuck?", "elsewhere/ask"),
])
def test_ask_refuses_empty_long_or_unconfirmed_questions(tmp_path, instance, text, confirm):
    procs = []
    jobs = JobRunner(TASKS, tmp_path / "demo", popen=fake_popen(procs), adhoc_base=tmp_path / "adhoc")

    with pytest.raises(JobError):
        jobs.ask(instance, text, confirm)
    assert procs == []


def test_a_question_and_a_harness_run_never_overlap(tmp_path):
    procs = []
    jobs = JobRunner(TASKS, tmp_path / "demo", popen=fake_popen(procs), adhoc_base=tmp_path / "adhoc")
    jobs.start("refuse_x", "suryodaya", "suryodaya/refuse_x")

    with pytest.raises(JobError, match="in progress"):
        jobs.ask("suryodaya", "Where is the shop floor stuck?", "suryodaya/ask")

    procs[0].code = 0
    jobs.ask("suryodaya", "Where is the shop floor stuck?", "suryodaya/ask")
    with pytest.raises(JobError, match="in progress"):
        jobs.start("refuse_x", "suryodaya", "suryodaya/refuse_x")


def test_ask_run_is_served_through_the_adhoc_base(tmp_path):
    procs = []
    jobs = JobRunner(TASKS, tmp_path / "demo", popen=fake_popen(procs), adhoc_base=tmp_path / "adhoc")
    server = make_server(0, {"committed": tmp_path / "runs", "demo": tmp_path / "demo", "adhoc": tmp_path / "adhoc"}, jobs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        headers = {"X-Console-Token": server.token, "Content-Type": "application/json"}
        body = {"instance": "keystone", "text": "Can we finish WO-1 by 2027-03-31?", "confirm": "keystone/ask"}
        assert _call(server, "/api/ask", body)[0] == 403
        status, out = _call(server, "/api/ask", body, headers)
        assert status == 200
        root = json.loads(out)["run_root"]
        (tmp_path / "adhoc" / root / "keystone" / "ask" / "trace.jsonl").write_text('{"type": "start"}\n', encoding="utf-8")

        listed = json.loads(_call(server, "/api/runs?base=adhoc")[1])
        assert [(r["name"], r["tasks"][0]["verdict"]) for r in listed] == [(root, None)]
        status, run = _call(server, f"/api/run?base=adhoc&root={root}&instance=keystone&task=ask")
        assert status == 200 and json.loads(run)["verdict"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_an_oversized_request_body_is_refused_before_it_is_read(console):
    headers = {"X-Console-Token": console.token, "Content-Type": "application/json"}
    status, _ = _call(console, "/api/ask", {"instance": "keystone", "text": "x" * 40000, "confirm": "keystone/ask"}, headers)
    assert status == 413
    assert console.procs == []
