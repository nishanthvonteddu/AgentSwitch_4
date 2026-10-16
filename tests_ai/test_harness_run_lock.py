"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-10-15).

These are NOT the team's hand-written tests and must not be claimed as such: the course scores
AI-written tests at zero.

One harness run at a time per checkout. Verifiers attribute writes by team account and time window, so two
runs at once (two terminals, a terminal and the console) graded each other's writes and failed read-only
tasks in real runs. harness.runner now takes runs/.console-run.lock, the file the console already locks, and
a runner the console launched recognises the lock it inherited instead of refusing itself. No tenant is
touched: run_one is faked, and the children started here only take the lock.

Run: python -m pytest tests_ai/test_harness_run_lock.py -q
"""
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from harness import runner
from prod_agent import config
from viewer import jobs as jobs_module
from viewer.jobs import JobRunner

try:
    import fcntl
except ImportError:
    fcntl = None

pytestmark = pytest.mark.skipif(fcntl is None, reason="no flock on this platform")

LOCK_FD_ENV = "HARNESS_RUN_LOCK_FD"


def test_the_console_and_the_runner_agree_on_how_the_inherited_fd_is_named():
    assert runner.RUN_LOCK_FD_ENV == jobs_module.RUN_LOCK_FD_ENV == LOCK_FD_ENV


@pytest.fixture
def lock_path(monkeypatch, tmp_path):
    path = tmp_path / "runs" / ".console-run.lock"
    monkeypatch.setattr(runner, "RUN_LOCK_PATH", path, raising=False)
    monkeypatch.delenv(LOCK_FD_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    return path


def _held_elsewhere(path: Path) -> int:
    """Another run's hold: its own open file description, as a second terminal or console would have."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_the_runner_locks_the_file_the_console_locks():
    """The console (python -m viewer) builds JobRunner on runs/demo; its lock and the runner's must be one file."""
    runs_dir = config.ROOT / "runs"
    assert JobRunner([], runs_dir / "demo", adhoc_base=runs_dir / "adhoc").lock_path == runner.RUN_LOCK_PATH


def test_a_second_hold_is_refused_until_the_first_lets_go(lock_path):
    first = runner.hold_run_lock()
    try:
        with pytest.raises(runner.RunLocked, match="another harness run"):
            runner.hold_run_lock()
    finally:
        runner.release_run_lock(first)
    runner.release_run_lock(runner.hold_run_lock())


def _main(monkeypatch, tmp_path, *argv):
    ran = []
    monkeypatch.setattr(runner, "load_tasks", lambda include_samples: [{"id": "refuse_x", "instances": ["suryodaya"]}])
    monkeypatch.setattr(runner, "run_one", lambda task, instance, root, **kw: ran.append(task["id"]) or {
        "task_id": task["id"], "instance": instance, "verdict": "approve", "reason": "ok", "sample": False,
        "run_id": "r", "seconds": 1.0})
    monkeypatch.setattr(sys, "argv", ["runner", "--runs-dir", str(tmp_path / "out"), *argv])
    return runner.main(), ran


def test_a_run_started_while_another_holds_the_lock_refuses_and_touches_nothing(lock_path, monkeypatch, tmp_path,
                                                                                  capsys):
    other = _held_elsewhere(lock_path)
    try:
        code, ran = _main(monkeypatch, tmp_path)
    finally:
        os.close(other)

    assert code != 0 and ran == []
    assert "another harness run" in capsys.readouterr().err
    assert not (tmp_path / "out").exists(), "no run root"
    assert not (tmp_path / "results.json").exists(), "the other run's results.json is not overwritten"


def test_the_lock_is_released_when_the_run_ends(lock_path, monkeypatch, tmp_path):
    assert _main(monkeypatch, tmp_path)[0] == 0
    runner.release_run_lock(_held_elsewhere(lock_path))  # free again: a later run in this process is not refused
    assert _main(monkeypatch, tmp_path)[0] == 0


def test_an_inherited_hold_is_recognised(lock_path):
    """The console's child holds the lock through the fd it inherited. A fresh open() would be refused by it."""
    inherited = _held_elsewhere(lock_path)
    try:
        os.environ[LOCK_FD_ENV] = str(inherited)
        assert runner.hold_run_lock() == inherited
    finally:
        os.environ.pop(LOCK_FD_ENV, None)
        os.close(inherited)


def test_a_stray_fd_number_does_not_stand_in_for_the_lock(lock_path, tmp_path):
    """An fd open on some other file (or not open at all) proves nothing: the lock is taken for real."""
    other = os.open(tmp_path / "unrelated", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        for value in (str(other), "987654", "not-a-number"):
            os.environ[LOCK_FD_ENV] = value
            fd = runner.hold_run_lock()
            assert fd != other
            runner.release_run_lock(fd)
    finally:
        os.environ.pop(LOCK_FD_ENV, None)
        os.close(other)


CHILD = textwrap.dedent("""
    import sys
    from pathlib import Path
    from harness import runner
    try:
        runner.hold_run_lock(Path(sys.argv[1]))
        print("HELD", flush=True)
    except runner.RunLocked as e:
        print("REFUSED", e, flush=True)
""")


def test_a_runner_the_console_launches_holds_the_consoles_lock(tmp_path):
    """End to end through JobRunner: a real child, launched the way the console launches harness.runner, takes the
    lock it inherited instead of refusing, while a second console is still refused."""
    lock = tmp_path / ".console-run.lock"
    children = []

    def popen(cmd, **kwargs):
        child = subprocess.Popen([sys.executable, "-c", CHILD + "import time; time.sleep(30)\n", str(lock)], **kwargs)
        children.append(child)
        return child

    try:
        jobs = JobRunner([{"id": "refuse_x", "instances": ["suryodaya"]}], tmp_path / "demo", popen=popen,
                         lock_path=lock)
        status = jobs.start("refuse_x", "suryodaya", "suryodaya/refuse_x")
        log, deadline = Path(status["log"]), time.monotonic() + 30
        while not log.read_text(encoding="utf-8").strip() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert log.read_text(encoding="utf-8").startswith("HELD"), log.read_text(encoding="utf-8")

        second = JobRunner([{"id": "refuse_x", "instances": ["suryodaya"]}], tmp_path / "demo2", popen=popen,
                           lock_path=lock)
        with pytest.raises(jobs_module.JobError, match="in progress"):
            second.start("refuse_x", "suryodaya", "suryodaya/refuse_x")
    finally:
        for child in children:
            child.kill()
            child.wait()


def test_without_the_fd_name_a_child_of_the_console_would_refuse_itself(tmp_path):
    """Why the console passes the fd's number: flock is per open file description, so the child's own open()
    of the same file is refused by the lock it is itself holding through the inherited fd."""
    lock = tmp_path / ".console-run.lock"
    held = _held_elsewhere(lock)
    try:
        env = {k: v for k, v in os.environ.items() if k != LOCK_FD_ENV}
        out = subprocess.run([sys.executable, "-c", CHILD, str(lock)], pass_fds=(held,), env=env, cwd=config.ROOT,
                             capture_output=True, text=True, timeout=60).stdout
        assert out.startswith("REFUSED")
        out = subprocess.run([sys.executable, "-c", CHILD, str(lock)], pass_fds=(held,), cwd=config.ROOT,
                             env={**env, LOCK_FD_ENV: str(held)}, capture_output=True, text=True,
                             timeout=60).stdout
        assert out.startswith("HELD")
    finally:
        os.close(held)


def test_platform_mode_takes_no_lock(lock_path, monkeypatch, tmp_path):
    """The runner's copy of the instance is private to the submission; a held local lock must not cost the run."""
    monkeypatch.setenv("AGENTSWITCH_TOKEN", "seat-token")
    monkeypatch.setenv("AGENTSWITCH_BASE_URL", "https://copy.example.invalid")
    monkeypatch.setenv("AGENTSWITCH_INSTANCE", "suryodaya")
    monkeypatch.setattr(runner, "preflight", lambda instance: (_ for _ in ()).throw(RuntimeError("offline test")))
    monkeypatch.setattr(runner, "watchdog_for", lambda *a: None)
    other = _held_elsewhere(lock_path)
    try:
        code, _ = _main(monkeypatch, tmp_path, "--platform-set")
    finally:
        os.close(other)

    assert code == 0
    assert (tmp_path / "results.json").exists()


def test_without_flock_the_runner_runs_unguarded(lock_path, monkeypatch, tmp_path):
    """Windows has no fcntl: viewer/jobs.py already copes, and the runner must still start there."""
    monkeypatch.setattr(runner, "fcntl", None)
    assert runner.hold_run_lock() is None
    assert _main(monkeypatch, tmp_path)[0] == 0
