"""AI-written, ungraded verifier ground-truth tests; graded tests live in tests/."""
import datetime as dt

import pytest

from harness.adapters import make_verify_context
from harness.verify import Verdict
from harness.verifiers import team04
from prod_agent import config


class FakeRest:
    def __init__(self, *, boms=(), findings=(), sales_orders=(), work_orders=()):
        self.rows = {
            "BOM": list(boms),
            "SalesOrder": list(sales_orders),
            "WorkOrder": list(work_orders),
        }
        self.findings = list(findings)

    def raw(self, path, **params):
        return {"data": self.findings}

    def list(self, entity, **params):
        return self.rows.get(entity, [])


@pytest.fixture(autouse=True)
def pin_today(monkeypatch):
    monkeypatch.setattr(config, "today", lambda: dt.date(2026, 9, 21))


def make_context(tmp_path, finding, *, boms=(), sales_orders=(), work_orders=(), task=None, fixture=None,
                 snapshot=None):
    (tmp_path / "result.json").write_text('{"run_id": "run-1"}', encoding="utf-8")
    rest = FakeRest(boms=boms, findings=[finding], sales_orders=sales_orders, work_orders=work_orders)
    context = {
        "me": "agent-user",
        "started_at_utc": "2026-09-21T08:00:00Z",
        "snapshot": snapshot or {},
    }
    ctx = make_verify_context(tmp_path, rest, context, task=task)
    if fixture is not None:
        ctx.fixture = fixture
    return ctx


def finding(**overrides):
    return {
        "id": "memory-1",
        "created_by": "agent-user",
        "run_id": "run-1",
        "outcome": "answered",
        **overrides,
    }


def work_order(number, item_id, bom_id=None, sales_order_id=None, **overrides):
    return {
        "id": f"id-{number}",
        "number": number,
        "status": "not_started",
        "item_id": item_id,
        "bom_id": bom_id,
        "sales_order_id": sales_order_id,
        "updated_by": "another-user",
        "updated_at": "2026-09-20T00:00:00Z",
        **overrides,
    }


def bom(bom_id, *materials):
    return {"id": bom_id, "materials": [{"item_id": item_id} for item_id in materials]}


def sales_order(order_id, number):
    return {"id": order_id, "number": number}


def sales_order_graph(target_number):
    target = work_order(target_number, "ITEM-TARGET", sales_order_id="so-target")
    unrelated = work_order("WO-UNRELATED", "ITEM-OTHER", sales_order_id="so-unrelated")
    consumer = work_order("WO-CONSUMER", "ITEM-CONSUMER", bom_id="bom-consumer", sales_order_id="so-consumer")
    sales_orders = [
        sales_order("so-target", "SO-TARGET"),
        sales_order("so-unrelated", "SO-UNRELATED"),
        sales_order("so-consumer", "SO-CONSUMER"),
    ]
    return target, unrelated, consumer, sales_orders


def test_blocks_linked_sales_order_rejects_unrelated_consumer_sales_order(tmp_path):
    target, unrelated, _consumer, sales_orders = sales_order_graph("WO-2026-00048")
    ctx = make_context(
        tmp_path,
        finding(
            work_order=target["number"],
            potentially_blocked_work_orders=[unrelated["number"]],
            blocked_sales_orders=["SO-TARGET", "SO-UNRELATED"],
        ),
        sales_orders=sales_orders,
        work_orders=[target, unrelated],
    )

    verdict, reason = team04.blocks_linked_sales_order(ctx)

    assert verdict is Verdict.REVISE
    assert "SO-UNRELATED" in reason


# These findings carry work_order since the subject rule (team04 docstring, rule 1): before it, a finding with
# no subject at all was approved for a question about WO-2026-00048.
def test_blocks_linked_sales_order_accepts_target_sales_order_only(tmp_path):
    target, _unrelated, _consumer, sales_orders = sales_order_graph("WO-2026-00048")
    ctx = make_context(
        tmp_path,
        finding(work_order=target["number"], blocked_sales_orders=["SO-TARGET"]),
        sales_orders=sales_orders,
        work_orders=[target],
    )

    verdict, _reason = team04.blocks_linked_sales_order(ctx)

    assert verdict is Verdict.APPROVE


def test_blocks_linked_sales_order_accepts_reachable_consumer_sales_order(tmp_path):
    target, _unrelated, consumer, sales_orders = sales_order_graph("WO-2026-00048")
    ctx = make_context(
        tmp_path,
        finding(
            work_order=target["number"],
            potentially_blocked_work_orders=[consumer["number"]],
            blocked_sales_orders=["SO-TARGET", "SO-CONSUMER"],
        ),
        boms=[bom("bom-consumer", "ITEM-TARGET")],
        sales_orders=sales_orders,
        work_orders=[target, consumer],
    )

    verdict, _reason = team04.blocks_linked_sales_order(ctx)

    assert verdict is Verdict.APPROVE


@pytest.mark.parametrize("item_field", ["null", "missing"])
def test_consumer_without_item_id_is_reachable_and_its_sales_order_is_linked(tmp_path, item_field):
    target = work_order("WO-2026-00048", "ITEM-TARGET", sales_order_id="so-target")
    consumer = work_order(
        "WO-CONSUMER",
        None,
        bom_id="bom-consumer",
        sales_order_id="so-consumer",
    )
    if item_field == "missing":
        consumer.pop("item_id")
    ctx = make_context(
        tmp_path,
        finding(
            work_order=target["number"],
            potentially_blocked_work_orders=[consumer["number"]],
            blocked_sales_orders=["SO-TARGET", "SO-CONSUMER"],
        ),
        boms=[bom("bom-consumer", "ITEM-TARGET")],
        sales_orders=[sales_order("so-target", "SO-TARGET"), sales_order("so-consumer", "SO-CONSUMER")],
        work_orders=[target, consumer],
        task={"params": {"work_order": target["number"]}},
    )

    downstream_verdict, _downstream_reason = team04.downstream_claims_are_potential(ctx)
    sales_order_verdict, _sales_order_reason = team04.blocks_linked_sales_order(ctx)

    assert (downstream_verdict, sales_order_verdict) == (Verdict.APPROVE, Verdict.APPROVE)


def test_linked_sales_order_reported_rejects_unrelated_consumer_sales_order(tmp_path):
    target, unrelated, _consumer, sales_orders = sales_order_graph("WO-2026-00003")
    ctx = make_context(
        tmp_path,
        finding(
            work_order=target["number"],
            potentially_blocked_work_orders=[unrelated["number"]],
            blocked_sales_orders=["SO-TARGET", "SO-UNRELATED"],
            customer_impact="SO-TARGET may be delayed",
        ),
        sales_orders=sales_orders,
        work_orders=[target, unrelated],
        task={"params": {"work_order": target["number"]}},
    )
    ctx.seat_tool_names = lambda: {"SalesOrder.get"}

    verdict, reason = team04.linked_sales_order_reported(ctx)

    assert verdict is Verdict.REVISE
    assert "SO-UNRELATED" in reason


def test_linked_sales_order_reported_accepts_target_sales_order(tmp_path):
    target, _unrelated, _consumer, sales_orders = sales_order_graph("WO-2026-00003")
    ctx = make_context(
        tmp_path,
        finding(work_order=target["number"], blocked_sales_orders=["SO-TARGET"],
                customer_impact="SO-TARGET may be delayed"),
        sales_orders=sales_orders,
        work_orders=[target],
        task={"params": {"work_order": target["number"]}},
    )
    ctx.seat_tool_names = lambda: {"SalesOrder.get"}

    verdict, _reason = team04.linked_sales_order_reported(ctx)

    assert verdict is Verdict.APPROVE


def downstream_context(tmp_path, reported, extra_wos=(), extra_boms=()):
    target = work_order("WO-TARGET", "ITEM-A")
    direct = work_order("WO-DIRECT", "ITEM-B", bom_id="bom-direct")
    ctx = make_context(
        tmp_path,
        finding(work_order=target["number"], potentially_blocked_work_orders=reported),
        boms=[bom("bom-direct", "ITEM-A"), *extra_boms],
        work_orders=[target, direct, *extra_wos],
        task={"params": {"work_order": target["number"]}},
    )
    return ctx


def test_downstream_claims_rejects_unreachable_self_consumer(tmp_path):
    self_consumer = work_order("WO-SELF", "ITEM-SELF", bom_id="bom-self")
    ctx = downstream_context(
        tmp_path,
        ["WO-DIRECT", "WO-SELF"],
        extra_wos=[self_consumer],
        extra_boms=[bom("bom-self", "ITEM-SELF")],
    )

    verdict, _reason = team04.downstream_claims_are_potential(ctx)

    assert verdict is Verdict.REVISE


def test_downstream_claims_rejects_unreachable_mutual_consumers(tmp_path):
    first = work_order("WO-CYCLE-A", "ITEM-X", bom_id="bom-cycle-a")
    second = work_order("WO-CYCLE-B", "ITEM-Y", bom_id="bom-cycle-b")
    ctx = downstream_context(
        tmp_path,
        ["WO-DIRECT", "WO-CYCLE-A", "WO-CYCLE-B"],
        extra_wos=[first, second],
        extra_boms=[bom("bom-cycle-a", "ITEM-Y"), bom("bom-cycle-b", "ITEM-X")],
    )

    verdict, _reason = team04.downstream_claims_are_potential(ctx)

    assert verdict is Verdict.REVISE


def test_downstream_claims_accepts_direct_and_depth_two_consumers(tmp_path):
    depth_two = work_order("WO-DEPTH-2", "ITEM-C", bom_id="bom-depth-2")
    ctx = downstream_context(
        tmp_path,
        ["WO-DIRECT", "WO-DEPTH-2"],
        extra_wos=[depth_two],
        extra_boms=[bom("bom-depth-2", "ITEM-B")],
    )

    verdict, _reason = team04.downstream_claims_are_potential(ctx)

    assert verdict is Verdict.APPROVE


def test_downstream_claims_accepts_cycle_reachable_from_target(tmp_path, monkeypatch):
    cycle_b = work_order("WO-CYCLE-B", "ITEM-C", bom_id="bom-cycle-b")
    ctx = downstream_context(
        tmp_path,
        ["WO-DIRECT", "WO-CYCLE-B"],
        extra_wos=[cycle_b],
        extra_boms=[bom("bom-direct", "ITEM-A", "ITEM-C"), bom("bom-cycle-b", "ITEM-B")],
    )

    verdict, _reason = team04.downstream_claims_are_potential(ctx)

    assert "WO-TARGET" not in ctx.finding_from_db()["potentially_blocked_work_orders"]
    assert verdict is Verdict.APPROVE

    def direct_consumers_only(_ctx, _wo, open_wos=None, inputs=None):
        return {"WO-DIRECT": open_wos["WO-DIRECT"]}

    monkeypatch.setattr(team04, "_bom_consumers", direct_consumers_only)
    narrowed_verdict, _narrowed_reason = team04.downstream_claims_are_potential(ctx)
    assert narrowed_verdict is Verdict.REVISE


def reschedule_context(tmp_path, *, down_end="2026-09-26", down_qty=1.0):
    upstream = work_order(
        "WO-UPSTREAM",
        "ITEM-UP",
        status="draft",
        qty=1.0,
        planned_start_date="2026-09-21",
        planned_end_date="2026-09-23",
        updated_by="agent-user",
        updated_at="2026-09-21T08:01:00Z",
    )
    downstream = work_order(
        "WO-DOWNSTREAM",
        "ITEM-DOWN",
        status="draft",
        qty=down_qty,
        planned_start_date="2026-09-24",
        planned_end_date=down_end,
        updated_by="agent-user",
        updated_at="2026-09-21T08:02:00Z",
    )
    fixture = {
        "upstream": {"id": upstream["id"], "qty": 1},
        "downstream": {"id": downstream["id"], "qty": 1},
    }
    applied = [
        {"number": upstream["number"], "outcome": "applied"},
        {"number": downstream["number"], "outcome": "applied"},
    ]
    return make_context(
        tmp_path,
        finding(work_order=upstream["number"], rescheduled=applied),
        work_orders=[upstream, downstream],
        fixture=fixture,
    )


def test_reschedule_fixture_chain_rejects_downstream_end_before_start(tmp_path):
    ctx = reschedule_context(tmp_path, down_end="1900-01-01")

    verdict, _reason = team04.reschedule_fixture_chain(ctx)

    assert verdict is Verdict.REVISE


def test_reschedule_fixture_chain_rejects_changed_quantity(tmp_path):
    ctx = reschedule_context(tmp_path, down_qty=-99)

    verdict, _reason = team04.reschedule_fixture_chain(ctx)

    assert verdict is Verdict.REVISE


def test_reschedule_fixture_chain_accepts_valid_dates_and_baseline_quantities(tmp_path):
    ctx = reschedule_context(tmp_path)

    verdict, _reason = team04.reschedule_fixture_chain(ctx)

    assert verdict is Verdict.APPROVE


def test_reschedule_fixture_chain_skips_missing_or_null_quantity_baseline(tmp_path, monkeypatch):
    missing_ctx = reschedule_context(tmp_path, down_qty=-99)
    missing_ctx.fixture["upstream"].pop("qty")
    missing_ctx.fixture["downstream"].pop("qty")

    missing_verdict, _missing_reason = team04.reschedule_fixture_chain(missing_ctx)

    assert missing_verdict is Verdict.APPROVE
    monkeypatch.setitem(missing_ctx.fixture["downstream"], "qty", 1)
    compared_verdict, _compared_reason = team04.reschedule_fixture_chain(missing_ctx)
    assert compared_verdict is Verdict.REVISE

    null_ctx = reschedule_context(tmp_path, down_qty=-99)
    null_ctx.fixture["upstream"]["qty"] = None
    null_ctx.fixture["downstream"]["qty"] = None

    null_verdict, _null_reason = team04.reschedule_fixture_chain(null_ctx)

    assert null_verdict is Verdict.APPROVE
    monkeypatch.setitem(null_ctx.fixture["downstream"], "qty", 1)
    compared_verdict, _compared_reason = team04.reschedule_fixture_chain(null_ctx)
    assert compared_verdict is Verdict.REVISE


def locked_context(tmp_path, outcome, refusal_reason=...):
    locked = work_order(
        "WO-2026-00048",
        "ITEM-LOCKED",
        status="submitted",
        updated_by="another-user",
        updated_at="2026-09-20T00:00:00Z",
    )
    fields = {"outcome": outcome}
    if refusal_reason is not ...:
        fields["refusal_reason"] = refusal_reason
    return make_context(
        tmp_path,
        finding(**fields),
        work_orders=[locked],
        snapshot={locked["number"]: locked.copy()},
    )


# partial with a reason used to be approved here. The request is one write, so nothing in it is answerable and
# partial ("some asked-for figure could not be given") misdescribes it: team04 docstring, rule 3.
@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("partial", ...),
        ("partial", "The submitted order is date-locked"),
        ("answered", "The submitted order is date-locked"),
        ("unexpected", "The submitted order is date-locked"),
    ],
)
def test_refuse_locked_reschedule_rejects_non_refusal_shapes(tmp_path, outcome, reason):
    ctx = locked_context(tmp_path, outcome, reason)

    verdict, _message = team04.refuse_locked_work_order_reschedule(ctx)

    assert verdict is Verdict.REVISE


@pytest.mark.parametrize("outcome", ["refused"])
def test_refuse_locked_reschedule_accepts_refusal_with_reason(tmp_path, outcome):
    ctx = locked_context(tmp_path, outcome, "The submitted order is date-locked")

    verdict, _reason = team04.refuse_locked_work_order_reschedule(ctx)

    assert verdict is Verdict.APPROVE
