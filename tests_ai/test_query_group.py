"""query_group counts the whole matching set, because a page cannot characterise one.

Seen live on 2026-09-29: handed 50 of 1209 failed AgentJob rows with truncated=true and an explicit
instruction not to generalise, the model still reported a single error code for all 1209. The true
split was 773 / 328 / 107 / 1. Warning a model not to guess, while leaving it no way to be right,
produces a quieter guess. These cover the counting that removes the need to guess.
"""
import pytest

from prod_agent import domain
from prod_agent.mcp_client import RowPage


class GroupMcp:
    def __init__(self, rows, total=None, truncated=False, tools=("AgentJob.list",)):
        self._rows, self._tools = rows, set(tools)
        self._total = total if total is not None else len(rows)
        self._truncated = truncated
        self.calls = []
        self.session = type("S", (), {"base": "https://example.invalid", "token": None,
                                      "with_reauth": staticmethod(lambda fn: fn())})()

    def has_tool(self, name):
        return name in self._tools

    def tool_names(self):
        return set(self._tools)

    def list_all(self, entity, page=200, max_rows=None, **filters):
        self.calls.append((entity, dict(filters)))
        rows = RowPage(self._rows)
        rows.total, rows.truncated = self._total, self._truncated
        return rows


def _jobs(spec):
    return [{"error": err} for err, n in spec.items() for _ in range(n)]


# --------------------------------------------------------------- the counts are exact

def test_counts_every_matching_record_not_a_page():
    """The live case: 1209 rows, four errors, one of them 64% of the set."""
    mcp = GroupMcp(_jobs({"agent_authority_unresolved": 773, "gemini_400": 328,
                          "needs_plan": 107, "chain_depth": 1}))
    out = domain.query_group(mcp, "AgentJob", "error")
    assert out["groups"] == {"agent_authority_unresolved": 773, "gemini_400": 328,
                             "needs_plan": 107, "chain_depth": 1}
    assert out["scanned"] == 1209 and out["total"] == 1209 and out["complete"] is True
    assert sum(out["groups"].values()) == 1209, "the counts must account for every row"


def test_groups_are_ranked_most_common_first():
    out = domain.query_group(GroupMcp(_jobs({"a": 1, "b": 9, "c": 5})), "AgentJob", "error")
    assert list(out["groups"]) == ["b", "c", "a"]


def test_no_rows_are_returned_only_counts():
    """Reading 1209 rows is fine; sending 1209 rows to the model is not."""
    out = domain.query_group(GroupMcp(_jobs({"x": 500})), "AgentJob", "error")
    assert "rows" not in out


# --------------------------------------------------------------- honest about its own limits

def test_a_ceilinged_read_says_the_counts_are_a_floor():
    mcp = GroupMcp(_jobs({"a": 5000}), total=1_000_000, truncated=True)
    out = domain.query_group(mcp, "AgentJob", "error")
    assert out["complete"] is False
    assert out["scanned"] == 5000 and out["total"] == 1_000_000
    assert "floor" in out["instruction"]


def test_a_complete_read_carries_no_warning():
    out = domain.query_group(GroupMcp(_jobs({"a": 3})), "AgentJob", "error")
    assert out["complete"] is True and "instruction" not in out


def test_rows_with_no_value_are_reported_separately_not_dropped_silently():
    rows = [{"error": "a"}, {"error": None}, {"error": ""}, {}]
    out = domain.query_group(GroupMcp(rows), "AgentJob", "error")
    assert out["groups"] == {"a": 1}
    assert out["rows_with_no_value"] == 3


# --------------------------------------------------------------- payload stays small

def test_a_long_value_is_truncated_as_a_label():
    """An error message is a legitimate group key and can be thousands of characters."""
    long = "Gemini rejected the request (HTTP 400): " + "x" * 5000
    out = domain.query_group(GroupMcp([{"error": long}] * 4), "AgentJob", "error")
    label = next(iter(out["groups"]))
    assert len(label) <= domain.QUERY_GROUP_LABEL_CHARS + 1
    assert out["groups"][label] == 4


def test_a_high_cardinality_field_reports_a_top_slice_and_sums_the_tail():
    rows = [{"error": f"e{i}"} for i in range(100)]
    out = domain.query_group(GroupMcp(rows), "AgentJob", "error")
    assert len(out["groups"]) == domain.QUERY_GROUP_MAX_VALUES
    assert out["distinct_values"] == 100
    assert out["other_values_combined"] == 100 - domain.QUERY_GROUP_MAX_VALUES
    assert sum(out["groups"].values()) + out["other_values_combined"] == 100


# --------------------------------------------------------------- boundary and wiring

def test_entity_outside_the_seat_is_refused():
    out = domain.query_group(GroupMcp([], tools=("AgentJob.list",)), "SalarySlip", "status")
    assert out["error"] == "not readable by this seat"


def test_filters_are_passed_to_the_read():
    mcp = GroupMcp(_jobs({"a": 2}))
    domain.query_group(mcp, "AgentJob", "error", {"status": "failed", "ignored": None})
    assert mcp.calls[-1] == ("AgentJob", {"status": "failed"})


@pytest.mark.parametrize("control", ["limit", "offset", "sort_by", "sort_order"])
def test_paging_controls_cannot_be_passed_as_filters(control):
    mcp = GroupMcp(_jobs({"a": 2}))
    domain.query_group(mcp, "AgentJob", "error", {control: "x"})
    assert control not in mcp.calls[-1][1]


def test_the_prompt_sends_counting_questions_here_not_to_query_records():
    from prod_agent.agent import ProductionAgent, SYSTEM_PROMPT
    assert "query_group" in ProductionAgent.PARALLEL_SAFE
    assert "query_group" in ProductionAgent.REPEAT_GUARDED
    assert "run query_group, never query_records" in SYSTEM_PROMPT
    assert "characterising a set from a page" in SYSTEM_PROMPT
