"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-09-30).

Ungraded. Prove --runs-dir moves a run root without changing the default the submission checker reads.
The graded, hand-written tests live in tests/.
"""
import sys

import pytest

from harness import runner
from prod_agent import config

LOCK_FD_ENV = "HARNESS_RUN_LOCK_FD"  # runner.RUN_LOCK_FD_ENV; a console-launched run's inherited lock


def _record(task, instance, root, **kw):
    return {"task_id": task["id"], "instance": instance, "verdict": "approve", "reason": "ok",
            "sample": False, "run_id": "r1", "seconds": 1.0}


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch, tmp_path):
    """main() writes results.json into the working directory and takes the checkout's run lock: keep both in
    tmp_path, so the suite neither overwrites the repo's results.json nor blocks (or is blocked by) a real run."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "RUN_LOCK_PATH", tmp_path / ".console-run.lock", raising=False)
    monkeypatch.delenv(LOCK_FD_ENV, raising=False)


def test_runs_dir_puts_the_run_root_and_summary_under_the_given_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "load_tasks", lambda include_samples: [{"id": "refuse_x", "instances": ["suryodaya"]}])
    monkeypatch.setattr(runner, "run_one", _record)
    demo = tmp_path / "demo"
    monkeypatch.setattr(sys, "argv", ["runner", "--task", "refuse_x", "--runs-dir", str(demo)])

    assert runner.main() == 0

    roots = [p for p in demo.iterdir() if p.is_dir()]
    assert len(roots) == 1
    assert (roots[0] / "summary.json").exists()
    assert (tmp_path / "results.json").exists(), "results.json lands in the working directory, here tmp_path"


def test_default_runs_dir_is_still_the_directory_the_checker_reads(monkeypatch, tmp_path):
    # A real plan: an empty one is now refused before any run root is made (test_harness_cli_validation.py).
    seen = []
    monkeypatch.setattr(runner, "load_tasks", lambda include_samples: [{"id": "refuse_x", "instances": ["suryodaya"]}])
    monkeypatch.setattr(runner, "run_one", _record)
    monkeypatch.setattr(runner, "_new_run_root", lambda base: seen.append(base) or tmp_path)
    monkeypatch.setattr(sys, "argv", ["runner"])

    runner.main()

    assert seen == [config.ROOT / "runs"]
