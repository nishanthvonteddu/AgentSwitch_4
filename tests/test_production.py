import datetime

import pytest

from prod_agent import domain
from prod_agent.mcp_client import McpClient, Session, McpError


# -----------------------------------------
# MCP CLIENT FIXTURES
# -----------------------------------------

@pytest.fixture(scope="module")
def surya():
    return McpClient(Session("suryodaya"))


@pytest.fixture(scope="module")
def keystone():
    return McpClient(Session("keystone"))


# =========================================
# OFFLINE TESTS
# =========================================

# Test 1: Permission error classification

def test_permission_error_kind():
    err = McpError(
        -32001,
        "Permission denied.",
        {"code": "permission_denied"},
    )

    assert err.kind == "permission_denied"


# Test 2: Unknown tool classification

def test_unknown_tool_error_kind():
    err = McpError(-32601, "Unknown tool.")

    assert err.kind == "unknown_tool"


# Test 3: Date parsing

def test_date_parsing():
    result = domain._date("2026-02-26T00:00:00")

    assert result == datetime.date(2026, 2, 26)


# Test 4: Empty date

def test_empty_date():
    result = domain._date("")

    assert result is None


# Test 5: Unauthorized work order update

def test_unauthorized_work_order():
    proposal = {
        "work_order_id": "abc",
        "number": "WO-X",
        "writable_by_seat": True,
    }

    result = domain.apply_proposal(
        None,
        proposal,
        allowed_ids={"other-id"},
    )

    assert result["outcome"] == "refused"


# Test 6: Non-writable proposal

def test_non_writable_proposal():
    proposal = {
        "work_order_id": "abc",
        "number": "WO-X",
        "writable_by_seat": False,
        "why_not_writable": "locked",
    }

    result = domain.apply_proposal(
        None,
        proposal,
        allowed_ids=None,
    )

    assert result["outcome"] == "refused"
    assert result["detail"] == "locked"


# =========================================
# SURYODAYA LIVE TESTS
# =========================================

# Test 7: Missing work order

def test_missing_work_order(surya):
    result = domain.diagnose(
        surya,
        "WO-2026-09999",
    )

    assert result["found"] is False


# Test 8: Existing work order

def test_existing_work_order(surya):
    result = domain.resolve_work_order(
        surya,
        "WO-2026-00048",
    )

    assert result is not None
    assert result["number"] == "WO-2026-00048"


# Test 9: Unsent subcontract

def test_unsent_subcontract(surya):
    result = domain.diagnose(
        surya,
        "WO-2026-00048",
    )

    codes = [
        signal["code"]
        for signal in result["signals"]
    ]

    assert "subcontract_not_sent" in codes


# Test 10: Blocking subcontract

def test_blocking_subcontract(surya):
    result = domain.diagnose(
        surya,
        "WO-2026-00048",
    )

    matching_signals = [
        signal
        for signal in result["signals"]
        if signal["code"] == "subcontract_not_sent"
    ]

    assert matching_signals
    assert matching_signals[0]["blocking"] is True


# Test 11: Job card visibility
# Job cards became readable for the Production seat on 2026-09-17 (bug f99d53d5 fixed).

def test_job_card_visibility(surya):
    result = domain.diagnose(
        surya,
        "WO-2026-00048",
    )

    not_visible = result["not_visible_to_this_seat"]

    assert not any(
        "JobCard" in str(item)
        for item in not_visible
    )

    assert result["current_operation"] is not None
    assert result["current_operation"]["job_card"].startswith("JC-")


# Test 12: Downstream sales order

def test_downstream_sales_order(surya):
    result = domain.downstream_impact(
        surya,
        "WO-2026-00048",
    )

    order_numbers = [
        order["number"]
        for order in result["blocked_sales_orders"]
    ]

    assert "SO-2026-00092" in order_numbers


# Test 13: Reschedule restrictions

def test_reschedule_restrictions(surya):
    result = domain.propose_reschedule(
        surya,
        "WO-2026-00048",
    )

    assert all(
        proposal["writable_by_seat"] is False
        for proposal in result["proposals"]
    )


# Test 14: Job card permission
# Readable since 2026-09-17; the tool is listed and returns rows.

def test_job_card_permission(surya):
    assert surya.has_tool("JobCard.list")

    result = surya.call(
        "JobCard.list",
        {"limit": 1},
    )

    assert len(result["data"]) == 1
    assert result["data"][0]["number"].startswith("JC-")


# Test 15: Unknown entity detection

def test_unknown_entity_detection(surya):
    result = domain.seat_capability(
        surya,
        "MachineDowntimeLog.list",
    )

    assert "warning" in result
    assert "DowntimeEntry" in result["similar_entities"]


# Test 16: Indian currency

def test_suryodaya_currency(surya):
    result = domain.company_context(surya)

    assert result["currency"] == "INR"


# =========================================
# KEYSTONE LIVE TESTS
# =========================================

# Test 17: US currency

def test_keystone_currency(keystone):
    result = domain.company_context(keystone)

    assert result["currency"] == "USD"


# Test 18: Sales order permission
# Keystone gained the sales_viewer role on 2026-09-17: read, but still no update.

def test_keystone_sales_order_permission(keystone):
    assert keystone.has_tool("SalesOrder.get") is True
    assert keystone.has_tool("SalesOrder.update") is False


# Test 19: Customer impact uncertainty
# No live order lacks a readable sales order any more, so a fake seat without
# SalesOrder tools checks that the agent still refuses to invent customer impact.

class NoSalesOrderMCP:
    def __init__(self):
        self.order = {
            "id": "wo-1",
            "number": "WO-FAKE-1",
            "item_id": "item-1",
            "bom_id": "bom-1",
            "status": "not_started",
            "sales_order_id": "so-1",
            "production_strategy": "make_to_order",
        }

    def list_all(self, entity, **filters):
        return [self.order] if entity == "WorkOrder" else []

    def call(self, name, args):
        assert name == "WorkOrder.get", f"unexpected call {name}"
        return dict(self.order)

    def has_tool(self, name):
        return not name.startswith("SalesOrder.")


def test_customer_impact_uncertainty():
    result = domain.downstream_impact(
        NoSalesOrderMCP(),
        "WO-FAKE-1",
    )

    assert result["customer_impact"].startswith(
        "undeterminable"
    )
    assert result["blocked_sales_orders"] == []


# Test 20: Stopped but not late
# Keystone was reseeded on 2026-09-17; WO-2026-00075 is stopped and due 2026-09-28.
# Disabled 2026-09-29: the premise expired. WO-2026-00075 passed its due date and the
# platform schedule now marks it late, and no stopped order on either tenant is on time.
# Re-enable once rewritten against an order that is stopped but not yet due.

# def test_stopped_order_not_late(keystone):
#     result = domain.diagnose(
#         keystone,
#         "WO-2026-00075",
#     )
#
#     assert result["work_order"]["status"] == "stopped"
#     assert result["is_late"] is False


import copy

import pytest

from prod_agent import domain
from prod_agent.mcp_client import McpClient, McpError


@pytest.fixture
def proposal():
    return {
        "work_order_id": "w1",
        "number": "WO-T",
        "status": "draft",
        "writable_by_seat": True,
        "snapshot_updated_at": "t1",
        "new_start": "2026-09-20",
        "new_end": "2026-09-25",
    }


class FakeWorkOrderMCP:
    def __init__(
        self,
        updated_at="t1",
        status="draft",
        reject=False,
        persist=True,
    ):
        self.calls = []
        self.reject = reject
        self.persist = persist

        self.row = {
            "id": "w1",
            "number": "WO-T",
            "status": status,
            "updated_at": updated_at,
            "planned_start_date": "2026-09-01",
            "planned_end_date": "2026-09-05",
        }

    def call(self, name, args):
        self.calls.append(name)

        if name == "WorkOrder.get":
            return copy.deepcopy(self.row)

        if name == "WorkOrder.update":
            if self.reject:
                raise McpError(-32602, "Cannot modify")

            if self.persist:
                self.row["planned_start_date"] = "2026-09-20"
                self.row["planned_end_date"] = "2026-09-25"
                self.row["updated_at"] = "t3"

            return {"id": "w1", "success": True}

        raise AssertionError(f"Unexpected MCP tool: {name}")


# Test 21: Detect a changed timestamp.

def test_21_changed_timestamp(proposal):
    mcp = FakeWorkOrderMCP(updated_at="t2")

    result = domain.apply_proposal(mcp, proposal)

    assert result["outcome"] == "changed_underneath"
    assert "WorkOrder.update" not in mcp.calls


# Test 22: Detect a changed workflow status.

def test_22_changed_status(proposal):
    mcp = FakeWorkOrderMCP(status="not_started")

    result = domain.apply_proposal(mcp, proposal)

    assert result["outcome"] == "changed_underneath"
    assert "WorkOrder.update" not in mcp.calls


# Test 23: Apply an unchanged proposal and verify it.

def test_23_successful_update(proposal):
    mcp = FakeWorkOrderMCP()

    result = domain.apply_proposal(mcp, proposal)

    assert result["outcome"] == "applied"

    assert mcp.calls == [
        "WorkOrder.get",
        "WorkOrder.update",
        "WorkOrder.get",
    ]


# Test 24: Handle a rejected platform update.

def test_24_platform_rejects_update(proposal):
    mcp = FakeWorkOrderMCP(reject=True)

    result = domain.apply_proposal(mcp, proposal)

    assert result["outcome"] == "write_rejected"
    assert result["detail"] == "Cannot modify"


# Test 25: Detect a write that was not persisted.

def test_25_write_not_persisted(proposal):
    mcp = FakeWorkOrderMCP(persist=False)

    result = domain.apply_proposal(mcp, proposal)

    assert result["outcome"] == "write_not_persisted"

    assert mcp.calls == [
        "WorkOrder.get",
        "WorkOrder.update",
        "WorkOrder.get",
    ]


from types import SimpleNamespace


@pytest.fixture
def fake_mcp():
    client = McpClient.__new__(McpClient)

    client._tools = {"A.list": {}}

    client.session = SimpleNamespace(
        with_reauth=lambda fn, *args, **kwargs: fn(*args, **kwargs)
    )

    return client


# Test 26: Parse JSON returned inside text content.

def test_26_text_content_parsing(fake_mcp):
    fake_mcp._rpc = lambda *args, **kwargs: {
        "content": [
            {
                "type": "text",
                "text": '{"a": 1}',
            }
        ]
    }

    result = fake_mcp.call("A.list", {})

    assert result == {"a": 1}


# Test 27: Prefer structured content.

def test_27_structured_content(fake_mcp):
    fake_mcp._rpc = lambda *args, **kwargs: {
        "structuredContent": {"s": 2},
        "content": [
            {
                "type": "text",
                "text": '{"s": 99}',
            }
        ],
    }

    result = fake_mcp.call("A.list", {})

    assert result == {"s": 2}


# Test 28: Handle an MCP tool error.

def test_28_tool_error(fake_mcp):
    fake_mcp._rpc = lambda *args, **kwargs: {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": "boom",
            }
        ],
    }

    with pytest.raises(McpError) as exc:
        fake_mcp.call("A.list", {})

    assert exc.value.code == "tool_error"


# Test 29: Combine paginated results.

def test_29_pagination(fake_mcp):
    def fake_call(name, args):
        page = args.get("offset", 0) // 2 + 1

        if page == 1:
            return {
                "data": [1, 2],
                "total": 3,
            }

        if page == 2:
            return {
                "data": [3],
                "total": 3,
            }

        return {
            "data": [],
            "total": 3,
        }

    fake_mcp.call = fake_call

    result = fake_mcp.list_all("A.list", page=2)

    assert result == [1, 2, 3]


import json
from types import SimpleNamespace


class FakeToolCall:
    def __init__(self, name, arguments="{}"):
        self.id = "call-1"
        self.type = "function"
        self.function = SimpleNamespace(
            name=name,
            arguments=arguments,
        )

    def model_dump(self, **kwargs):
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, **kwargs):
        return {
            "role": "assistant",
            "content": self.content,
            "tool_calls": [
                call.model_dump()
                for call in self.tool_calls
            ],
        }


class FakeLLM:
    def __init__(self, messages):
        self.messages = messages
        self.index = 0

        self.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=self.create,
            )
        )

    def create(self, *args, **kwargs):
        message = self.messages[
            min(self.index, len(self.messages) - 1)
        ]

        self.index += 1

        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=message)
            ]
        )


@pytest.fixture
def fake_agent_mcp():
    return SimpleNamespace(
        session=SimpleNamespace(instance="suryodaya"),
        call=lambda name, args: {},
        has_tool=lambda name: True,
    )


@pytest.fixture
def agent_adapter():
    # Project-specific adapter required.
    #
    # Implement run_agent(llm, mcp, max_steps=...)
    # returning a dict with stop_reason and trace.
    #
    # Implement dispatch_tool(name, arguments, mcp, run_id)
    # returning the tool result.
    #
    # Connect both to your existing agent code.

    from harness import adapters

    return adapters


# Test 30: Stop when the model produces a final answer.

def test_30_final_answer(agent_adapter, fake_agent_mcp):
    llm = FakeLLM([
        FakeMessage(content="hi"),
    ])

    result = agent_adapter.run_agent(
        llm=llm,
        mcp=fake_agent_mcp,
        max_steps=3,
    )

    assert result["stop_reason"] == "final_answer"

    event_types = [
        event["type"]
        for event in result["trace"]
    ]

    assert event_types == ["start", "llm", "end"]


# Test 31: Respect the maximum step count.

def test_31_max_steps(agent_adapter, fake_agent_mcp):
    llm = FakeLLM([
        FakeMessage(
            tool_calls=[
                FakeToolCall("list_late_work_orders")
            ]
        ),
    ])

    result = agent_adapter.run_agent(
        llm=llm,
        mcp=fake_agent_mcp,
        max_steps=2,
    )

    assert result["stop_reason"] == "max_steps"


# Test 32: Recover from a failing domain tool.

def test_32_tool_exception(
    agent_adapter,
    fake_agent_mcp,
    monkeypatch,
):
    def broken_tool(*args, **kwargs):
        raise RuntimeError("Simulated tool failure")

    monkeypatch.setattr(
        domain,
        "list_late_work_orders",
        broken_tool,
    )

    llm = FakeLLM([
        FakeMessage(
            tool_calls=[
                FakeToolCall("list_late_work_orders")
            ]
        ),
        FakeMessage(content="Unable to retrieve work orders."),
    ])

    result = agent_adapter.run_agent(
        llm=llm,
        mcp=fake_agent_mcp,
        max_steps=3,
    )

    assert result["stop_reason"] == "final_answer"

    event_types = [
        event["type"]
        for event in result["trace"]
    ]

    assert "exception" in event_types

    assert any(
        event["type"] == "tool"
        and "error" in str(event).lower()
        for event in result["trace"]
    )


# Test 33: Prevent overwriting an existing finding.

def test_33_finding_cannot_be_overwritten(
    agent_adapter,
    fake_agent_mcp,
    monkeypatch,
):
    saved = {}

    def fake_record_finding(*args, **kwargs):
        outcome = kwargs.get("outcome")

        if outcome is None:
            for arg in args:
                if isinstance(arg, dict) and "outcome" in arg:
                    outcome = arg["outcome"]
                    break

        if saved:
            return {"error": "Finding already recorded"}

        saved["outcome"] = outcome
        return {"outcome": outcome}

    monkeypatch.setattr(
        domain,
        "record_finding",
        fake_record_finding,
    )

    first = agent_adapter.dispatch_tool(
        name="record_finding",
        arguments={"outcome": "refused"},
        mcp=fake_agent_mcp,
        run_id="R1",
    )

    second = agent_adapter.dispatch_tool(
        name="record_finding",
        arguments={"outcome": "answered"},
        mcp=fake_agent_mcp,
        run_id="R1",
    )

    assert "error" not in first
    assert "error" in second
    assert saved["outcome"] == "refused"


import json
from pathlib import Path


class FakeRest:
    def __init__(self, findings=None, work_orders=None):
        self.findings = findings or []
        self.work_orders = work_orders or []

    def raw(self, *args, **kwargs):
        return {"data": copy.deepcopy(self.findings)}

    def list(self, *args, **kwargs):
        return {"data": copy.deepcopy(self.work_orders)}


@pytest.fixture
def harness_adapter():
    # Project-specific adapter required.
    #
    # Connect these functions to your harness:
    #
    # make_verify_context(run_dir, rest, context)
    # find_run_finding(verify_context)
    # changed_work_orders(verify_context, exclude_ids=None)
    # verify_unknown_order(verify_context, order_number)
    # run_task(run_dir, agent, verifier)
    #
    # Normalize returned values to the formats
    # expected in the tests below.

    from harness import adapters

    return adapters


def make_test_context(
    harness_adapter,
    tmp_path,
    findings=None,
    work_orders=None,
):
    result_file = tmp_path / "result.json"

    result_file.write_text(
        json.dumps({"run_id": "R1"}),
        encoding="utf-8",
    )

    rest = FakeRest(
        findings=findings,
        work_orders=work_orders,
    )

    context = {
        "me": "me",
        "started_at_utc": "2026-09-16T10:00:00",
        "snapshot": {},
    }

    return harness_adapter.make_verify_context(
        run_dir=tmp_path,
        rest=rest,
        context=context,
    )


# Test 34: Select only the current run's finding.

def test_34_finding_belongs_to_current_run(
    harness_adapter,
    tmp_path,
):
    findings = [
        {
            "id": "m0",
            "run_id": "R0",
            "outcome": "answered",
        },
        {
            "id": "m1",
            "run_id": "R1",
            "outcome": "refused",
        },
        {
            "id": "m2",
            "run_id": "unrelated",
            "outcome": "answered",
        },
    ]

    ctx = make_test_context(
        harness_adapter,
        tmp_path,
        findings=findings,
    )

    finding, memory_id = (
        harness_adapter.find_run_finding(ctx)
    )

    assert memory_id == "m1"
    assert finding["run_id"] == "R1"
    assert finding["outcome"] == "refused"


# Test 35: Count only this team's writes during the run.

def test_35_only_current_run_writes(
    harness_adapter,
    tmp_path,
):
    work_orders = [
        {
            "id": "w1",
            "number": "WO-1",
            "updated_by": "me",
            "updated_at": "2026-09-16T10:05:00",
        },
        {
            "id": "w2",
            "number": "WO-2",
            "updated_by": "me",
            "updated_at": "2026-09-16T09:00:00",
        },
        {
            "id": "w3",
            "number": "WO-3",
            "updated_by": "another-team",
            "updated_at": "2026-09-16T10:06:00",
        },
    ]

    ctx = make_test_context(
        harness_adapter,
        tmp_path,
        work_orders=work_orders,
    )

    result = harness_adapter.changed_work_orders(ctx)

    assert result == ["WO-1"]

    excluded = harness_adapter.changed_work_orders(
        ctx,
        exclude_ids={"w1"},
    )

    assert excluded == []


# Test 36: Approve a correct refusal.

def test_36_correct_refusal(
    harness_adapter,
    tmp_path,
):
    findings = [
        {
            "id": "m1",
            "run_id": "R1",
            "outcome": "refused",
        }
    ]

    ctx = make_test_context(
        harness_adapter,
        tmp_path,
        findings=findings,
    )

    verdict = harness_adapter.verify_unknown_order(
        ctx,
        "WO-2026-09999",
    )

    assert verdict == "APPROVE"


# Test 37: Reject an unsupported answer.

def test_37_incorrect_answer(
    harness_adapter,
    tmp_path,
):
    findings = [
        {
            "id": "m1",
            "run_id": "R1",
            "outcome": "answered",
        }
    ]

    ctx = make_test_context(
        harness_adapter,
        tmp_path,
        findings=findings,
    )

    verdict = harness_adapter.verify_unknown_order(
        ctx,
        "WO-2026-09999",
    )

    assert verdict == "REVISE"


# Test 38: Reject a missing finding.

def test_38_missing_finding(
    harness_adapter,
    tmp_path,
):
    ctx = make_test_context(
        harness_adapter,
        tmp_path,
        findings=[],
    )

    verdict = harness_adapter.verify_unknown_order(
        ctx,
        "WO-2026-09999",
    )

    assert verdict == "REVISE"


# Test 39: Mark a changed test premise as unevaluated.

def test_39_work_order_created_during_run(
    harness_adapter,
    tmp_path,
):
    findings = [
        {
            "id": "m1",
            "run_id": "R1",
            "outcome": "refused",
        }
    ]

    work_orders = [
        {
            "id": "w-new",
            "number": "WO-2026-09999",
            "updated_at": "2026-09-16T10:05:00",
        }
    ]

    ctx = make_test_context(
        harness_adapter,
        tmp_path,
        findings=findings,
        work_orders=work_orders,
    )

    verdict = harness_adapter.verify_unknown_order(
        ctx,
        "WO-2026-09999",
    )

    assert verdict == "UNEVALUATED"


# Test 40: Save results before scoring.

def test_40_result_saved_before_verdict(
    harness_adapter,
    tmp_path,
):
    def fake_agent(*args, **kwargs):
        return {
            "run_id": "R1",
            "stop_reason": "final_answer",
        }

    def fake_verifier(*args, **kwargs):
        result_file = tmp_path / "result.json"

        # The result must already be saved
        # before verification begins.
        assert result_file.exists()

        saved = json.loads(
            result_file.read_text(encoding="utf-8")
        )

        assert saved["run_id"] == "R1"

        return "APPROVE"

    harness_adapter.run_task(
        run_dir=tmp_path,
        agent=fake_agent,
        verifier=fake_verifier,
    )

    result_file = tmp_path / "result.json"
    verdict_file = tmp_path / "verdict.json"

    assert result_file.exists()
    assert verdict_file.exists()

    assert (
        result_file.stat().st_mtime_ns
        <= verdict_file.stat().st_mtime_ns
    )