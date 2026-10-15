"""AI-written, ungraded. scripts/verify_submission.py grades only a run that covers today's whole task set.

Every run root here is built in tmp_path; the real runs/ directory is never read.
"""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path

import pytest

from harness.runner import load_tasks
from prod_agent import config

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_submission.py"
REQUIRED = {("why_late", "suryodaya"), ("why_late", "keystone"), ("refuse_payroll", "suryodaya")}


@pytest.fixture
def checker():
    spec = importlib.util.spec_from_file_location("verify_submission_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # fresh module: its results/warnings lists start empty
    return module


def make_run(runs_dir, name, pairs, *, verdict="approve", samples=()):
    root = runs_dir / name
    records = []
    for task_id, instance in [*pairs, *samples]:
        sample = (task_id, instance) in samples
        rec = {"task_id": task_id, "instance": instance, "verdict": verdict, "reason": "", "sample": sample}
        d = root / instance / task_id
        d.mkdir(parents=True)
        (d / "result.json").write_text("{}", encoding="utf-8")
        (d / "verdict.json").write_text(json.dumps(rec), encoding="utf-8")
        records.append(rec)
    scored = [r for r in records if not r["sample"]]
    (root / "summary.json").write_text(json.dumps({
        "runs": records, "approved": sum(r["verdict"] == "approve" for r in scored),
        "revise": sum(r["verdict"] == "revise" for r in scored),
        "unevaluated": sum(r["verdict"] == "unevaluated" for r in scored), "scored_total": len(scored)}),
        encoding="utf-8")
    return root


def stamp(name):
    return dt.datetime.strptime(name[:15], "%Y%m%d-%H%M%S").timestamp()


def lines(capsys):
    return capsys.readouterr().out.splitlines()


def test_a_newer_partial_run_is_passed_over_for_the_older_complete_one(checker, tmp_path, capsys):
    make_run(tmp_path, "20261001-090000", REQUIRED)
    make_run(tmp_path, "20261002-090000-123456", {("why_late", "suryodaya")})  # a one-task --task run
    checker.check_runs(tmp_path, REQUIRED, (stamp("20260930-000000"), "last commit"))
    out = lines(capsys)
    assert any(o.startswith("note  20261002-090000-123456 is partial") and "lacks 2 of 3" in o for o in out)
    assert any(o.startswith("PASS  latest complete run 20261001-090000: all approved") for o in out)
    assert all(checker.results)


def test_with_no_complete_run_nothing_is_graded_and_the_check_fails(checker, tmp_path, capsys):
    make_run(tmp_path, "20261002-090000", {("why_late", "suryodaya"), ("why_late", "keystone")})
    checker.check_runs(tmp_path, REQUIRED, (stamp("20260930-000000"), "last commit"))
    out = lines(capsys)
    assert checker.results == [False]
    assert any(o.startswith("FAIL  a complete harness run exists") and "refuse_payroll@suryodaya" in o for o in out)
    assert not any("all approved" in o for o in out)


def test_no_runs_at_all_fails(checker, tmp_path, capsys):
    checker.check_runs(tmp_path / "runs", REQUIRED, (None, "no git"))
    assert checker.results == [False]
    assert "no run with a summary.json" in capsys.readouterr().out


def test_samples_do_not_cover_a_required_pair(checker, tmp_path):
    make_run(tmp_path, "20261001-090000", REQUIRED - {("refuse_payroll", "suryodaya")},
             samples=(("refuse_payroll", "suryodaya"),))
    assert checker.pick_complete_run(tmp_path, REQUIRED)[0] is None


def test_tasks_no_longer_in_the_set_do_not_make_a_run_partial(checker, tmp_path):
    run = make_run(tmp_path, "20261001-090000", REQUIRED | {("retired_task", "keystone")})
    assert checker.pick_complete_run(tmp_path, REQUIRED)[0] == run


def test_an_unreadable_summary_is_partial_not_a_crash(checker, tmp_path):
    good = make_run(tmp_path, "20261001-090000", REQUIRED)
    bad = make_run(tmp_path, "20261002-090000", REQUIRED)
    (bad / "summary.json").write_text("{not json", encoding="utf-8")
    latest, passed_over = checker.pick_complete_run(tmp_path, REQUIRED)
    assert latest == good and [r for r, _ in passed_over] == [bad]


# ------------------------------------------------------------------ is the run evidence for this code?

def test_a_complete_run_older_than_the_code_warns_without_counting(checker, tmp_path, capsys):
    make_run(tmp_path, "20261001-090000-266195", REQUIRED)
    checker.check_runs(tmp_path, REQUIRED, (stamp("20261003-120000"), "last commit touching prod_agent/ or harness/"))
    out = lines(capsys)
    assert any(o.startswith("WARN  latest complete run 20261001-090000-266195 predates the code") for o in out)
    assert checker.warnings and all(checker.results)


def test_a_complete_run_newer_than_the_code_passes(checker, tmp_path, capsys):
    make_run(tmp_path, "20261004-090000", REQUIRED)
    checker.check_runs(tmp_path, REQUIRED, (stamp("20261003-120000"), "last commit"))
    assert "PASS  latest complete run is newer than the code" in capsys.readouterr().out
    assert not checker.warnings


def test_code_age_unknown_is_a_skip(checker, tmp_path, capsys):
    make_run(tmp_path, "20261004-090000", REQUIRED)
    checker.check_runs(tmp_path, REQUIRED, (None, "git could not date the code: no git"))
    assert "skip  run vs code age (git could not date the code" in capsys.readouterr().out


def test_code_changed_at_reads_this_checkout(checker):
    when, what = checker.code_changed_at()
    assert when is None or when <= dt.datetime.now().timestamp() + 1
    assert what


# ------------------------------------------------------------------ the checks that read the complete run

def test_a_complete_run_with_a_revise_fails_all_approved(checker, tmp_path, capsys):
    make_run(tmp_path, "20261004-090000", REQUIRED, verdict="revise")
    checker.check_runs(tmp_path, REQUIRED, (None, "no git"))
    assert any(o.startswith("FAIL  latest complete run 20261004-090000: all approved") for o in lines(capsys))


def test_result_written_after_its_verdict_fails_and_equal_mtimes_pass(checker, tmp_path, capsys):
    run = make_run(tmp_path, "20261004-090000", REQUIRED)
    for d in run.glob("*/*"):
        os.utime(d / "result.json", (1_000, 1_000))
        os.utime(d / "verdict.json", (1_000, 1_000))
    checker.check_runs(tmp_path, REQUIRED, (None, "no git"))
    assert "PASS  every run written to disk before scoring" in capsys.readouterr().out
    os.utime(run / "keystone" / "why_late" / "result.json", (2_000, 2_000))
    checker.check_runs(tmp_path, REQUIRED, (None, "no git"))
    assert "FAIL  every run written to disk before scoring" in capsys.readouterr().out


def test_run_started_at_reads_both_name_forms(checker):
    assert checker.run_started_at(Path("20261001-092435-266195")) == stamp("20261001-092435")
    assert checker.run_started_at(Path("20260916-114054")) == stamp("20260916-114054")
    assert checker.run_started_at(Path("2026-not-a-run")) is None


def test_required_pairs_are_todays_non_sample_tasks_on_their_declared_instances(checker):
    pairs = checker.required_pairs()
    tasks = {t["id"]: t for t in load_tasks(include_samples=True)}
    assert pairs and all(not tasks[t].get("sample") for t, _ in pairs)
    for task in load_tasks(include_samples=False):
        declared = set(task.get("instances", sorted(config.INSTANCES)))
        assert {i for t, i in pairs if t == task["id"]} == declared
