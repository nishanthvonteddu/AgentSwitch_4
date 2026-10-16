"""One run at a time, started through the same CLI a person would type: a harness task, or a free-text question."""
import datetime as dt
import os
import subprocess
import sys
import threading
from pathlib import Path

from harness.runner import RUN_LOCK_FD_ENV
from prod_agent import config

try:
    import fcntl
except ImportError:  # Windows has no flock; there only the in-process check below guards a single console
    fcntl = None

ASK_MAX_CHARS = 1000


class JobError(Exception):
    pass


class JobRunner:
    def __init__(self, tasks: list[dict], demo_base: Path, popen=subprocess.Popen, adhoc_base: Path | None = None,
                 lock_path: Path | None = None):
        self.tasks = {t["id"]: t for t in tasks}
        self.demo_base = demo_base
        self.adhoc_base = adhoc_base or demo_base.parent / "adhoc"
        self.lock_path = lock_path or demo_base.parent / ".console-run.lock"
        self._popen = popen
        self._lock = threading.Lock()
        self._next_id = 0
        self.current: dict | None = None

    def instances_for(self, task_id: str) -> list[str]:
        return self.tasks[task_id].get("instances", sorted(config.INSTANCES))

    def start(self, task_id: str, instance: str, confirm: str) -> dict:
        if task_id not in self.tasks:
            raise JobError(f"unknown task {task_id!r}")
        if instance not in self.instances_for(task_id):
            raise JobError(f"task {task_id} does not run on {instance!r}")
        if confirm != f"{instance}/{task_id}":
            raise JobError("confirmation text does not match")
        # An argument list, never a shell string: task and instance only ever come from the allowlist above.
        cmd = [sys.executable, "-m", "harness.runner", "--task", task_id, "--instance", instance,
               "--runs-dir", str(self.demo_base)]
        return self._launch(cmd, {"kind": "task", "base": "demo", "task_id": task_id, "instance": instance},
                            self.demo_base, run_root=None)

    def ask(self, instance: str, text: str, confirm: str) -> dict:
        """A free-text question to the agent. No verifier exists for it, so its answer is never graded."""
        if instance not in config.INSTANCES:
            raise JobError(f"unknown instance {instance!r}")
        text = text.strip()
        if not text:
            raise JobError("the question is empty")
        if len(text) > ASK_MAX_CHARS:
            raise JobError(f"the question is longer than {ASK_MAX_CHARS} characters")
        if any((ord(c) < 32 and c not in "\n\t") or 127 <= ord(c) < 160 or c in "  " for c in text):
            raise JobError("the question contains control characters")
        if confirm != f"{instance}/ask":
            raise JobError("confirmation text does not match")
        root = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        run_dir = self.adhoc_base / root / instance / "ask"
        # No --apply and no --escalate: the question can read and record a finding, never change an order or page a
        # person. "--" ends the options, so a question that starts with "--apply" stays text.
        cmd = [sys.executable, "-m", "prod_agent", "--instance", instance, "--run-dir", str(run_dir), "--", text]
        return self._launch(cmd, {"kind": "ask", "base": "adhoc", "task_id": "ask", "instance": instance},
                            self.adhoc_base, run_root=root)

    def _launch(self, cmd: list[str], meta: dict, base: Path, run_root: str | None) -> dict:
        with self._lock:
            # Two runs share the fixture work orders on the tenant and would trip changed_underneath on each other.
            if self.current is not None and self.current["proc"].poll() is None:
                raise JobError("a run is already in progress")
            held = self._hold_run_lock()
            try:
                base.mkdir(parents=True, exist_ok=True)
                before = {p.name for p in base.iterdir()}
                started = dt.datetime.now()
                self._next_id += 1
                log_path = base / f"console-{started:%Y%m%d-%H%M%S}-{self._next_id}.log"
                with log_path.open("w", encoding="utf-8") as log:
                    # A new session keeps Ctrl-C on the console from killing the run before it withdraws its
                    # escalations. The child inherits the lock and holds it until it exits, even past this console.
                    # harness.runner takes the same lock itself, and a fresh open() there would be refused by this
                    # one (flock is per open file description), so it is told which fd it already holds.
                    env = None if held is None else {**os.environ, RUN_LOCK_FD_ENV: str(held)}
                    proc = self._popen(cmd, cwd=config.ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       pass_fds=() if held is None else (held,), env=env)
            finally:
                if held is not None:
                    os.close(held)
            self.current = {"job_id": self._next_id, **meta, "proc": proc, "base_dir": base, "before": before,
                            "run_root": run_root, "log": str(log_path),
                            "started": started.isoformat(timespec="seconds")}
        return self.status()

    def _hold_run_lock(self) -> int | None:
        """The run's slot across consoles. The in-memory check above forgets a run when the console restarts, while
        the run itself, in its own session, carries on."""
        if fcntl is None:
            return None
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise JobError("a run is already in progress (started from another or an earlier console)") from None
        return fd

    def status(self) -> dict | None:
        if self.current is None:
            return None
        cur = self.current
        root = cur["run_root"]
        if root is None:
            new = sorted(p.name for p in cur["base_dir"].iterdir() if p.is_dir() and p.name not in cur["before"])
            root = new[-1] if new else None
        elif not (cur["base_dir"] / root).is_dir():
            root = None  # the agent has not created its run directory yet
        code = cur["proc"].poll()
        return {"job_id": cur["job_id"], "kind": cur["kind"], "base": cur["base"], "task_id": cur["task_id"],
                "instance": cur["instance"], "started": cur["started"], "running": code is None, "exit_code": code,
                "run_root": root, "log": cur["log"]}
