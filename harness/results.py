"""results.json for AgentSwitch's harness runner ("Our harness" -> Submit for a run, Release 8.1).

The runner reads only this file:

    {"tasks": [{"id": "t1", "title": "What it checks", "passed": true, "score": 1.0, "evidence": "short proof"}],
     "summary": "one line"}

passed strictly true or false, score 0-1, 1 to 200 tasks. If the file is missing or malformed when the run ends,
the panel shows no results at all, so every planned task is listed before the first one starts and the file is
rewritten, whole, after every verdict.

Written with Claude (AI-assisted).
"""
import collections
import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path

RESULTS_NAME = "results.json"  # agentswitch-harness.toml: results = "results.json"
MAX_TASKS = 200
EVIDENCE_LIMIT = 500
TITLE_LIMIT = 140
NOT_RUN = "not run"


def title_of(task: dict) -> str:
    """What the task checks: the first sentence of its own `checks` text, else of the request it makes."""
    text = " ".join((task.get("checks") or task.get("prompt") or task["id"]).split())
    first = text.split(". ")[0].rstrip(".")
    return first if len(first) <= TITLE_LIMIT else first[:TITLE_LIMIT - 3].rstrip() + "..."


def diagnostics(record: dict) -> str:
    """Steps, tokens and how the loop ended: the panel shows nothing else of a run, and on 2026-10-12 every
    failure read "no finding recorded" whether the model was refused or simply never recorded one."""
    if record.get("steps") is None:
        return ""
    parts = [f"{record['steps']} steps", f"{((record.get('tokens') or {}).get('total') or 0) / 1000:.1f}k tokens"]
    stop = record.get("stop_reason")
    if stop and stop != "final_answer":
        parts.append(f"stop {stop}" + (f" {record['llm_error_code']}" if record.get("llm_error_code") else ""))
    return "[" + ", ".join(parts) + "]"


def problems(doc) -> list[str]:
    """Everything that would make the runner show "no results" for this file. Empty means it will be read."""
    if not isinstance(doc, dict):
        return ["the top level must be an object"]
    found = []
    tasks = doc.get("tasks")
    if not isinstance(tasks, list):
        return ["tasks must be a list"]
    if not 1 <= len(tasks) <= MAX_TASKS:
        found.append(f"tasks must hold 1 to {MAX_TASKS} entries, not {len(tasks)}")
    ids = collections.Counter()
    for i, task in enumerate(tasks):
        where = f"tasks[{i}]"
        if not isinstance(task, dict):
            found.append(f"{where} must be an object")
            continue
        for key in ("id", "title"):
            if not isinstance(task.get(key), str) or not task[key].strip():
                found.append(f"{where}.{key} must be a non-empty string")
        if not isinstance(task.get("evidence"), str):
            found.append(f"{where}.evidence must be a string")
        if type(task.get("passed")) is not bool:  # not 0/1, not "true": the runner takes true or false only
            found.append(f"{where}.passed must be true or false, not {task.get('passed')!r}")
        if "score" in task:
            score = task["score"]
            if type(score) not in (int, float) or not 0 <= score <= 1:
                found.append(f"{where}.score must be a number from 0 to 1, not {score!r}")
        ids[task.get("id")] += 1
    found += [f"id {key!r} appears {n} times" for key, n in ids.items() if n > 1 and isinstance(key, str)]
    if not isinstance(doc.get("summary"), str):
        found.append("summary must be a string")
    return found


def write_atomic(path: Path, doc: dict) -> None:
    """Replace the file in one step, so the runner never reads a half-written one if it stops us mid-write.

    The temporary file is this writer's own: with one fixed name, two runs in the same directory replaced each
    other's temp file and the slower one died on FileNotFoundError. Same directory, so the replace stays atomic."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.chmod(tmp, 0o644)  # mkstemp makes it 0600; the file the runner reads stays readable as before
            json.dump(doc, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class ResultsBook:
    """One entry per planned task, in plan order. Thread-safe: read-only tasks may run several at a time."""

    def __init__(self, path: Path, label: str):
        self.path = path
        self.label = label
        self.note = ""
        self._entries: dict[str, dict] = {}
        self._status: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(task: dict, instance: str) -> str:
        return f"{instance}:{task['id']}"

    def _set(self, key: str, title: str, status: str, passed: bool, evidence: str) -> None:
        with self._lock:
            self._entries[key] = {"id": key, "title": title, "passed": passed, "score": 1.0 if passed else 0.0,
                                  "evidence": evidence[:EVIDENCE_LIMIT]}
            self._status[key] = status

    def plan(self, task: dict, instance: str) -> None:
        self._set(self.key(task, instance), title_of(task), "not_run", False, NOT_RUN)

    def started(self, task: dict, instance: str) -> None:
        self._set(self.key(task, instance), title_of(task), "running", False, f"{NOT_RUN}: started, no verdict yet")

    def record(self, task: dict, instance: str, record: dict) -> None:
        verdict = record["verdict"]
        seen = diagnostics(record)
        # unevaluated never counts as a pass: only approve is passed=true.
        self._set(self.key(task, instance), title_of(task), verdict, verdict == "approve",
                  f"{verdict}{' ' + seen if seen else ''}: {record.get('reason') or ''}")

    def not_run(self, task: dict, instance: str, why: str) -> None:
        self._set(self.key(task, instance), title_of(task), "not_run", False, f"{NOT_RUN}: {why}")

    def fail(self, name: str, title: str, evidence: str) -> None:
        """A failure that is not a task, such as a preflight that found the run could not work."""
        self._set(f"{self.label}:{name}", title, "failed", False, evidence)

    def unfinished(self, why: str) -> None:
        """Mark every task still without a verdict: the run is ending before they got one."""
        with self._lock:
            pending = [(k, self._entries[k]["title"], s) for k, s in self._status.items() if s in ("not_run", "running")]
        for key, title, status in pending:
            evidence = f"{NOT_RUN}: {why}" if status == "not_run" else f"stopped before its verdict: {why}"
            self._set(key, title, "not_run", False, evidence)

    def doc(self) -> dict:
        with self._lock:
            tasks = list(self._entries.values())
            counts = collections.Counter(self._status.values())
        if not tasks:  # the runner refuses an empty list, and an empty run is a failure worth showing
            tasks = [{"id": f"{self.label}:no_tasks", "title": "The harness has tasks for this instance",
                      "passed": False, "score": 0.0, "evidence": "no task matched this instance"}]
            counts["failed"] += 1
        summary = (f"{self.label}: {counts['approve']}/{len(tasks)} approved, {counts['revise']} revise, "
                   f"{counts['unevaluated']} unevaluated (never a pass), {counts['not_run'] + counts['running']} not run")
        if counts["failed"]:
            summary += f", {counts['failed']} failed before any task"
        if self.note:
            summary += f"; {self.note}"
        return {"tasks": tasks, "summary": summary}

    def write(self) -> list[str]:
        """Write the file as it stands and return its problems, so a malformed file is caught by the run itself."""
        doc = self.doc()
        with self._lock:
            write_atomic(self.path, doc)
        return problems(doc)
