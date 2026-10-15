"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-10-15).

These are NOT the team's hand-written tests and must not be claimed as such: the course scores
AI-written tests at zero.

A typo in --task, or a task on an instance it does not declare, used to exit 0 and leave an empty
runs/<timestamp>/ behind. Locally that is now a non-zero exit before anything is created. Under the platform
runner nothing changes: an empty plan still writes results.json with its failed no_tasks entry, which is what
the runner's panel shows. run_one is faked; no tenant is touched.

Run: python -m pytest tests_ai/test_harness_cli_validation.py -q
"""
import json
import sys

import pytest

from harness import runner
from harness.results import problems

LOCK_FD_ENV = "HARNESS_RUN_LOCK_FD"  # runner.RUN_LOCK_FD_ENV; a console-launched run's inherited lock

TASKS = [{"id": "refuse_x", "instances": ["suryodaya"]},
         {"id": "sample_x", "instances": ["suryodaya"], "sample": True}]


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "RUN_LOCK_PATH", tmp_path / ".console-run.lock", raising=False)
    monkeypatch.delenv(LOCK_FD_ENV, raising=False)
    monkeypatch.setattr(runner, "load_tasks",
                        lambda include_samples: [t for t in TASKS if include_samples or not t.get("sample")])
    ran = []
    monkeypatch.setattr(runner, "run_one", lambda task, instance, root, **kw: ran.append((task["id"], instance)) or {
        "task_id": task["id"], "instance": instance, "verdict": "approve", "reason": "ok", "sample": False,
        "run_id": "r", "seconds": 1.0})
    out = tmp_path / "out"

    def main(*argv):
        monkeypatch.setattr(sys, "argv", ["runner", "--runs-dir", str(out), *argv])
        return runner.main()

    main.ran, main.out, main.results = ran, out, tmp_path / "results.json"
    return main


def test_an_unknown_task_id_is_a_usage_error_before_anything_is_created(cli, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli("--task", "refuse_y")

    assert exit_.value.code == 2
    assert "unknown task id(s): refuse_y" in capsys.readouterr().err
    assert not cli.out.exists() and not cli.results.exists() and cli.ran == []


def test_a_task_on_an_instance_it_does_not_declare_exits_non_zero_without_a_run_root(cli, capsys):
    code = cli("--task", "refuse_x", "--instance", "keystone")

    assert code != 0
    assert "no task to run on keystone" in capsys.readouterr().err
    assert not cli.out.exists() and not cli.results.exists() and cli.ran == []


def test_a_sample_named_without_include_samples_says_how_to_run_it(cli, capsys):
    assert cli("--task", "sample_x") != 0
    assert "--include-samples" in capsys.readouterr().err
    assert cli("--task", "sample_x", "--include-samples") == 0
    assert cli.ran == [("sample_x", "suryodaya")]


def test_a_valid_task_still_runs(cli):
    assert cli("--task", "refuse_x") == 0
    assert cli.ran == [("refuse_x", "suryodaya")]
    assert len([p for p in cli.out.iterdir() if p.is_dir()]) == 1


def test_under_the_platform_runner_an_empty_plan_still_writes_the_no_tasks_entry(cli, monkeypatch):
    """Unchanged platform contract: the runner shows nothing for a missing results.json, so it is never refused."""
    monkeypatch.setenv("AGENTSWITCH_TOKEN", "seat-token")
    monkeypatch.setenv("AGENTSWITCH_BASE_URL", "https://copy.example.invalid")
    monkeypatch.setenv("AGENTSWITCH_INSTANCE", "keystone")
    monkeypatch.setattr(runner, "watchdog_for", lambda *a: None)
    monkeypatch.setattr(runner, "preflight", lambda instance: {
        "tools": 1, "me": "u", "model": "m", "mode": "named", "tool_called": True,
        "thinking": {"setting": None, "request_extra": {}, "tried": [], "tokens": {"completion": 0}}})

    assert cli("--platform-set", "--token-budget", "130000") == 0

    doc = json.loads(cli.results.read_text(encoding="utf-8"))
    assert [t["id"] for t in doc["tasks"]] == ["keystone:no_tasks"] and problems(doc) == []
    assert cli.ran == []
