"""Keep the AgentSwitch harness runner's variables out of AI-written tests unless a test sets them itself.

A shell that has just run a platform rehearsal still exports AGENTSWITCH_TOKEN; left in place it would switch every
Session and agent built here into platform mode, and HARNESS_BUDGET_MINUTES would put a clock on them.

Written with Claude (AI-assisted).
"""
import sys
from pathlib import Path

import pytest

# Several files share one fake book with `from test_behaviour_fakes import ...`. pytest's default import mode puts
# this folder on sys.path by itself; --import-mode=importlib does not, so it is added here and works in both.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

HARNESS_RUNNER_VARIABLES = ("AGENTSWITCH_TOKEN", "AGENTSWITCH_BASE_URL", "AGENTSWITCH_INSTANCE", "HARNESS_BUDGET_MINUTES")


@pytest.fixture(autouse=True)
def no_harness_runner_environment(monkeypatch):
    for name in HARNESS_RUNNER_VARIABLES:
        monkeypatch.delenv(name, raising=False)
