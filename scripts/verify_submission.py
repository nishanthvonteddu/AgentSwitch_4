"""Pre-submission checklist. Not a test (it scores nothing); it only reports readiness.

    python scripts/verify_submission.py            # all checks, including live bug-report lookup
    python scripts/verify_submission.py --offline  # skip network checks

Written with Claude (AI-assisted).
"""
import ast
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
results = []
warnings = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def warn(name, detail=""):
    """Said loudly, not counted: a fact the reader must weigh, which no offline step could turn into a PASS."""
    warnings.append(name)
    print(f"WARN  {name}" + (f"  ({detail})" if detail else ""))


def required_pairs() -> set[tuple[str, str]]:
    """Every (task, instance) a full `python -m harness.runner` plans today, samples excluded."""
    from harness.runner import load_tasks
    from prod_agent.config import INSTANCES
    return {(t["id"], i) for t in load_tasks(include_samples=False) for i in t.get("instances", sorted(INSTANCES))}


def run_pairs(run: Path) -> set[tuple[str, str]]:
    """The scored (task, instance) pairs a run's summary.json records; empty if it cannot be read."""
    try:
        records = json.loads((run / "summary.json").read_text(encoding="utf-8")).get("runs") or []
        return {(r["task_id"], r["instance"]) for r in records if not r.get("sample")}
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return set()


def pick_complete_run(runs_dir: Path, required: set) -> tuple[Path | None, list[tuple[Path, set]]]:
    """The newest run root that scored every required pair, and each newer root passed over with what it lacks.

    A newer partial root (a --task or --platform-set run, or one from before tasks were added) is not evidence for
    the task set, so it never stands in for a full run; it is reported instead of hidden.
    """
    roots = sorted(p for p in runs_dir.glob("2026*") if (p / "summary.json").exists()) if runs_dir.exists() else []
    passed_over = []
    for run in reversed(roots):
        missing = required - run_pairs(run)
        if not missing:
            return run, passed_over
        passed_over.append((run, missing))
    return None, passed_over


def run_started_at(run: Path) -> float | None:
    """When a run root was created, from its name (local time, as harness.runner names it). File mtimes are no
    use here: a committed run's files carry the time of the checkout, not of the run."""
    try:
        return dt.datetime.strptime(run.name[:15], "%Y%m%d-%H%M%S").timestamp()
    except ValueError:
        return None


def code_changed_at() -> tuple[float | None, str]:
    """When prod_agent/ or harness/ last changed: now if the tree has uncommitted edits there, else the last commit
    touching them. (None, why) when git cannot say."""
    try:
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "prod_agent", "harness"], cwd=ROOT,
                               capture_output=True, text=True, timeout=30, check=True).stdout.strip()
        if dirty:
            return dt.datetime.now().timestamp(), "uncommitted changes in prod_agent/ or harness/"
        out = subprocess.run(["git", "log", "-1", "--format=%ct", "--", "prod_agent", "harness"], cwd=ROOT,
                             capture_output=True, text=True, timeout=30, check=True).stdout.strip()
        return float(out), "last commit touching prod_agent/ or harness/"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, f"git could not date the code: {exc}"


def check_runs(runs_dir: Path, required: set, code_changed: tuple[float | None, str]) -> None:
    latest, passed_over = pick_complete_run(runs_dir, required)
    for run, missing in passed_over:
        print(f"note  {run.name} is partial, not graded: lacks {len(missing)} of {len(required)} task/instance pairs")
    if latest is None:
        newest = passed_over[0] if passed_over else None
        detail = "no run with a summary.json" if newest is None else \
            f"newest {newest[0].name} lacks " + ", ".join(f"{t}@{i}" for t, i in sorted(newest[1]))
        check("a complete harness run exists (python -m harness.runner, every task on every instance)", False, detail)
        print("skip  run verdict, ordering and refusal checks (no complete run to read)")
        return
    summary = json.loads((latest / "summary.json").read_text(encoding="utf-8"))
    check(f"latest complete run {latest.name}: all approved",
          summary["approved"] == summary["scored_total"] and summary["unevaluated"] == 0,
          f"{summary['approved']}/{summary['scored_total']} approved, {summary['unevaluated']} unevaluated, "
          f"covers all {len(required)} task/instance pairs")
    changed, what = code_changed
    started = run_started_at(latest)
    if changed is None or started is None:
        print(f"skip  run vs code age ({what if changed is None else f'cannot date {latest.name} from its name'})")
    elif started < changed:
        # A WARN, not a FAIL: only a live run (shared tenant, LLM spend, ask first) can clear it, and a checker that
        # stays red after every commit gets ignored. The verdicts above are true of the code as it was then.
        warn(f"latest complete run {latest.name} predates the code it would vouch for",
             f"run started {dt.datetime.fromtimestamp(started):%Y-%m-%d %H:%M}, "
             f"{what} {dt.datetime.fromtimestamp(changed):%Y-%m-%d %H:%M}; its verdicts are not evidence for this code")
    else:
        check("latest complete run is newer than the code", True, f"newer than the {what}")
    order_ok, missing = True, []
    for d in latest.glob("*/*"):
        r, v = d / "result.json", d / "verdict.json"
        if not (r.exists() and v.exists()):
            missing.append(d.name)
        # Equal passes on purpose: a filesystem with 1-2 s mtimes (FAT, some network shares) stamps both files
        # alike whenever the verifier is quick, and that is not evidence the order was broken.
        elif r.stat().st_mtime > v.stat().st_mtime:
            order_ok = False
    check("every run written to disk before scoring", order_ok and not missing,
          f"missing files in {missing}" if missing else "result.json older than verdict.json everywhere")
    refusal_verdicts = [json.loads(p.read_text(encoding="utf-8"))["verdict"]
                        for p in latest.glob("*/refuse_*/verdict.json")]
    check("refusal tasks approved in latest run",
          bool(refusal_verdicts) and all(v == "approve" for v in refusal_verdicts),
          f"{refusal_verdicts.count('approve')}/{len(refusal_verdicts)}")


def main():
    offline = "--offline" in sys.argv
    if os.environ.get("AGENT_OFFLINE") == "1" and not offline:
        print("ERROR: AGENT_OFFLINE=1 requires --offline; pass --offline or unset AGENT_OFFLINE")
        return 1

    print("\n== A. Gap report")
    gap = ROOT / "GAP_REPORT.md"
    text = gap.read_text(encoding="utf-8") if gap.exists() else ""
    check("GAP_REPORT.md exists", bool(text))
    for needle, label in [("Carbon", "names a modern competitor"),
                          ("## 1.", "Q1: what they do that we do not"),
                          ("## 2.", "Q2: gaps an agent can close today"),
                          ("platform work", "gaps that need platform work"),
                          ("## 3.", "Q3: what our agent can do that they cannot")]:
        check(label, needle in text)
    words = len(text.split())
    check("about one page", words <= 1100, f"{words} words")

    print("\n== B. Agent")
    agent_src = (ROOT / "prod_agent" / "agent.py").read_text(encoding="utf-8")
    check("own loop (no framework)", "for step in range(self.max_steps)" in agent_src)
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    check("no agent framework in requirements", not any(f in reqs for f in ("langchain", "llama", "autogen", "crewai")))
    domain_src = (ROOT / "prod_agent" / "domain.py").read_text(encoding="utf-8")
    check("re-reads before writing", "changed_underneath" in domain_src)
    check("currency read from Company, not hardcoded", "default_currency" in domain_src)

    print("\n== C. Harness")
    verifier_src = (ROOT / "harness" / "verifiers" / "team04.py").read_text(encoding="utf-8")
    check("verifiers never read the reply text", "final_answer" not in verifier_src)
    check("verifiers read the database", "finding_from_db" in verifier_src and "ctx.rest" in verifier_src)

    tasks = [json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "harness" / "tasks" / "team04").glob("*.json")]
    refusals = [t["id"] for t in tasks if t["id"].startswith("refuse_")]
    check("task set present", len(tasks) > 0, f"{len(tasks)} tasks")
    check("at least one refusal task", len(refusals) >= 1, f"{len(refusals)} refusal tasks")

    check_runs(ROOT / "runs", required_pairs(), code_changed_at())

    print("\n== D. Hand-written tests")
    test_files = list((ROOT / "tests").glob("test_*.py"))
    n_tests = sum(1 for f in test_files for node in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
                  if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"))
    check("hand-written tests exist in tests/", n_tests > 0, f"{n_tests} test functions (10 points each if hand-written)")
    labelled = [f.name for f in test_files if "claude" in f.read_text(encoding="utf-8").lower()]
    check("no AI-authorship label inside tests/", not labelled, ", ".join(labelled))
    offline_skipped = None
    if n_tests:
        env = os.environ.copy()
        if offline:
            env["AGENT_OFFLINE"] = "1"
        with tempfile.TemporaryDirectory() as report_dir:
            report = Path(report_dir) / "report.xml"
            rc = subprocess.run(
                [sys.executable, "-m", "pytest", "tests", "-q", f"--junitxml={report}"],
                cwd=ROOT,
                env=env,
            ).returncode
            if rc != 0:
                check("pytest passes", False, f"pytest exited with code {rc}")
            elif offline:
                try:
                    suite = ET.parse(report).getroot().find("testsuite")
                    if suite is None:
                        raise ValueError("testsuite element missing")
                    tests = int(suite.attrib["tests"])
                    skipped = int(suite.attrib["skipped"])
                except (OSError, ET.ParseError, KeyError, TypeError, ValueError) as exc:
                    check("pytest report readable", False, f"pytest report could not be read: {exc}")
                else:
                    if skipped > 0:
                        offline_skipped = skipped
                        print(
                            f"skip  pytest: {tests - skipped} ran, {skipped} skipped "
                            "(live tenant fixtures; AGENT_OFFLINE=1)"
                        )
                    else:
                        check("offline gate engaged", False, "pytest report recorded zero skipped tests")
            else:
                check("pytest passes", True)

    print("\n== E. Bugs")
    if offline:
        print("skip  bug reports (offline)")
    else:
        from prod_agent.mcp_client import RestClient, Session
        for inst, expected in (("suryodaya", 4), ("keystone", 1)):
            rows = RestClient(Session(inst)).raw("/api/bug-report/mine").get("data", [])
            check(f"bug reports on record: {inst}", len(rows) >= expected, f"{len(rows)} filed")

    print("\n== F. Safe to publish")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8") if (ROOT / ".gitignore").exists() else ""
    check(".env and .tokens/ are git-ignored", ".env" in gitignore and ".tokens" in gitignore)
    leaked = []
    # Built at runtime so this file does not match its own search.
    key_marker, password_marker = "sk-" + "proj-", "!aA" + "1"
    for p in ROOT.rglob("*"):
        if p.is_file() and not any(part in (".git", "runs", ".tokens", "__pycache__") for part in p.parts) \
                and p.name != ".env" and p.suffix in (".py", ".md", ".json", ".txt", ".example", ".toml"):
            body = p.read_text(encoding="utf-8", errors="ignore")
            if key_marker in body or password_marker in body:
                leaked.append(str(p.relative_to(ROOT)))
    check("no API key or password in committable files", not leaked, ", ".join(leaked))

    print("\n== G. AgentSwitch harness runner (Our harness -> Submit for a run)")
    import tomllib

    from harness.results import RESULTS_NAME, problems
    from prod_agent.config import INSTANCES
    try:
        spec = tomllib.loads((ROOT / "agentswitch-harness.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # TOMLDecodeError is a ValueError
        check("agentswitch-harness.toml readable", False, str(exc))
    else:
        missing = [k for k in ("install", "run", "results", "instances", "timeout_minutes") if k not in spec]
        check("harness toml has every key the runner reads", not missing, ", ".join(missing))
        instances = spec.get("instances")
        check("harness toml lists only our instances", isinstance(instances, list) and bool(instances)
              and set(instances) <= set(INSTANCES), str(instances))
        timeout = spec.get("timeout_minutes")
        check("harness toml timeout is 1-30 minutes", type(timeout) is int and 1 <= timeout <= 30, str(timeout))
        check("harness toml reads the file our runner writes", spec.get("results") == RESULTS_NAME, str(spec.get("results")))
        check("harness toml runs our harness", "harness.runner" in str(spec.get("run")), str(spec.get("run")))
    check("results.json is git-ignored", RESULTS_NAME in gitignore)
    results_file = ROOT / RESULTS_NAME
    if results_file.exists():
        try:
            found = problems(json.loads(results_file.read_text(encoding="utf-8")))
        except ValueError as exc:
            found = [f"not JSON: {exc}"]
        check(f"latest {RESULTS_NAME} in the runner's format", not found, "; ".join(found[:3]))

    if offline:
        if offline_skipped:
            print(f"\nThis is not full submission verification: {offline_skipped} live graded tests and "
                  "section E bug reports were skipped.")
        else:
            print("\nThis is not full submission verification: section E bug reports were skipped.")
    print(f"\n{sum(results)}/{len(results)} checks passed" + (f", {len(warnings)} warning(s)" if warnings else ""))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
