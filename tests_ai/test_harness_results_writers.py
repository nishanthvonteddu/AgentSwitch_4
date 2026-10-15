"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-10-15).

These are NOT the team's hand-written tests and must not be claimed as such: the course scores
AI-written tests at zero.

results.json is replaced atomically through a temporary file. That file used to have one fixed name
(results.json.tmp), so two runs writing in the same directory replaced each other's temp file and the slower
one died on FileNotFoundError mid-run. Each writer now has its own temp file in the same directory.

Run: python -m pytest tests_ai/test_harness_results_writers.py -q
"""
import json
import multiprocessing

import pytest

from harness import results
from harness.results import write_atomic


def test_a_second_writer_landing_mid_write_breaks_neither(tmp_path, monkeypatch):
    """Deterministic interleaving: writer B runs a whole write while writer A is still dumping its document."""
    path = tmp_path / "results.json"
    real_dump, interleaved = results.json.dump, []

    def dump(doc, f, **kw):
        if not interleaved:
            interleaved.append(1)
            write_atomic(path, {"writer": "B"})
        real_dump(doc, f, **kw)

    monkeypatch.setattr(results.json, "dump", dump)
    write_atomic(path, {"writer": "A"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"writer": "A"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["results.json"], "no temp file left behind"


def _hammer(path: str, n: int) -> None:
    from pathlib import Path
    for i in range(n):
        write_atomic(Path(path), {"i": i, "pad": "x" * 2000})


def test_two_processes_writing_the_same_file_both_finish(tmp_path):
    path = tmp_path / "results.json"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_hammer, args=(str(path), 200)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)

    assert [p.exitcode for p in procs] == [0, 0]
    assert json.loads(path.read_text(encoding="utf-8"))["i"] == 199
    assert sorted(p.name for p in tmp_path.iterdir()) == ["results.json"]


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "results.json"
    write_atomic(path, {"v": 1})

    def broken(doc, f, **kw):
        raise ValueError("not serialisable")
    monkeypatch.setattr(results.json, "dump", broken)
    with pytest.raises(ValueError):
        write_atomic(path, {"v": 2})

    assert json.loads(path.read_text(encoding="utf-8")) == {"v": 1}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["results.json"]
