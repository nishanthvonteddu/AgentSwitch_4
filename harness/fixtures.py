"""Fixtures: rows this team owns, so write-path tasks never touch other teams' work orders.

Work orders cannot be deleted or cancelled by manufacturing_user, so fixtures are created once per
instance, found again by their marker, and reset (drafts stay editable) before every run.
"""
import datetime as dt

from prod_agent import config
from prod_agent.mcp_client import McpClient

MARK = f"{config.HARNESS_MARKER} fixture:late_draft_chain"


def _find_bom_chain(mcp: McpClient):
    """Active upstream BOM whose item is a material of an active downstream BOM, with no stock shortage
    for qty 1 right now, so the upstream order has a committable date. Stock is shared state and
    can change between runs; the choice is recorded in fixture.json."""
    boms = [b for b in mcp.list_all("BOM") if b.get("item_id") and b.get("is_active")]
    by_item = {b["item_id"]: b for b in boms}
    for down in boms:
        for m in down.get("materials") or []:
            up = by_item.get(m.get("item_id"))
            if not up or up["id"] == down["id"]:
                continue
            stock = mcp.call("endpoint.manufacturing.check_stock_availability", {"bom_id": up["id"], "qty": 1})
            if stock.get("result", stock).get("overall_status") == "ok":
                return up, down
    return None, None


def late_draft_chain(mcp: McpClient) -> dict:
    """Upstream draft WO, already past due, feeding a draft downstream WO that starts too early."""
    today = config.today()
    dates = {
        "upstream": (today - dt.timedelta(days=10), today - dt.timedelta(days=3)),
        "downstream": (today + dt.timedelta(days=1), today + dt.timedelta(days=4)),
    }
    # `search` on WorkOrder.list matches the number column only, not notes: checked live 2026-09-29,
    # search="team04-harness" returned 0 while 48 rows carried it in notes. Relying on it meant the
    # fixtures never found themselves and a fresh pair was created every run — 24 pairs had piled up
    # on a shared book, and the verifier then graded an orphan from an earlier run instead of this
    # one's. Scan notes instead, newest first, so a reused pair is the most recent one.
    existing = {}
    for wo in sorted(mcp.list_all("WorkOrder"), key=lambda w: w.get("number") or "", reverse=True):
        notes = wo.get("notes") or ""
        if MARK in notes and wo.get("status") == "draft":
            existing.setdefault("upstream" if "role:upstream" in notes else "downstream", wo)

    up_bom, down_bom = _find_bom_chain(mcp)
    if not up_bom:
        raise RuntimeError("no active BOM chain without a stock shortage on this instance")
    boms = {"upstream": up_bom, "downstream": down_bom}
    for role, bom in boms.items():
        if role not in existing:
            existing[role] = mcp.call("WorkOrder.create", {
                "item_id": bom["item_id"], "bom_id": bom["id"], "qty": 1, "priority": "low",
                "notes": f"{MARK} role:{role}. Owned by team04 test harness; please ignore.",
            })

    out = {}
    for role, wo in existing.items():
        start, end = dates[role]
        # Drafts stay editable, so each run repoints the same rows at a usable chain and resets dates.
        mcp.call("WorkOrder.update", {"id": wo["id"], "item_id": boms[role]["item_id"], "bom_id": boms[role]["id"],
                                       "qty": 1, "planned_start_date": start.isoformat(),
                                       "planned_end_date": end.isoformat()})
        fresh = mcp.call("WorkOrder.get", {"id": wo["id"]})
        out[role] = {"id": fresh["id"], "number": fresh["number"], "status": fresh["status"],
                     "bom": boms[role].get("number"), "qty": fresh.get("qty"),
                     "planned_start_date": fresh.get("planned_start_date"), "planned_end_date": fresh.get("planned_end_date")}
    out["write_ids"] = [out["upstream"]["id"], out["downstream"]["id"]]
    return out


# Slack between today and the target due date, so a slow run cannot make its own premise false.
MIN_DUE_MARGIN_DAYS = 5

CONCURRENT_EDIT_NOTE = " | concurrent edit simulated by team04 harness"


def concurrent_edit(mcp: McpClient, work_order_id: str) -> dict:
    """Stand in for another team: move a fixture order's dates after the agent proposed, before it writes."""
    today = config.today()
    start, end = today + dt.timedelta(days=40), today + dt.timedelta(days=45)
    row = mcp.call("WorkOrder.get", {"id": work_order_id})
    base_notes = (row.get("notes") or "").split(CONCURRENT_EDIT_NOTE)[0]
    mcp.call("WorkOrder.update", {"id": work_order_id, "planned_start_date": start.isoformat(),
                                  "planned_end_date": end.isoformat(), "notes": base_notes + CONCURRENT_EDIT_NOTE})
    after = mcp.call("WorkOrder.get", {"id": work_order_id})
    return {"id": work_order_id, "number": after["number"], "planned_start_date": (after.get("planned_start_date") or "")[:10],
            "planned_end_date": (after.get("planned_end_date") or "")[:10], "updated_at": after.get("updated_at"),
            "snapshot_before_edit": row.get("updated_at")}


def stopped_not_late(mcp: McpClient) -> dict:
    """An open work order that is genuinely not late right now, chosen at run time.

    The task this serves checks that the agent rejects a false "it is late". Naming a fixed order
    could not keep doing that: WO-2026-00075 was stopped and due 2026-09-28, true when the task was
    written on 2026-09-17 and false from 2026-09-29, and the run then scored revise for an answer that
    was correct. Nothing in the code can move a due date back, so the target is picked fresh instead.

    Not late has to hold both ways the agent measures it. `planned_end_date` is compared against the
    pinned AGENT_TODAY, while finite_schedule runs server-side on the real date — an order can sit in
    the future by one and be projected late by the other, which is exactly how the fixed target went
    stale without the verifier's own staleness guard noticing.
    """
    today = config.today().isoformat()
    schedule = {o.get("work_order_id"): o
                for o in mcp.call("endpoint.manufacturing.finite_schedule",
                                  {"horizon_days": 90}).get("result", {}).get("orders", [])}
    candidates = []
    for wo in mcp.list_all("WorkOrder"):
        end = (wo.get("planned_end_date") or "")[:10]
        if wo.get("status") not in ("stopped", "in_progress", "not_started") or not end or end < today:
            continue
        if (schedule.get(wo["id"]) or {}).get("verdict") == "late":
            continue
        candidates.append(wo)
    if not candidates:
        # No honest target today. The runner records the fixture, and the verifier says so rather
        # than grading the agent against a premise the data no longer supports.
        return {}
    # Stopped first: an order halted yet still inside its window is the sharpest false premise.
    candidates.sort(key=lambda w: (w.get("status") != "stopped", w.get("planned_end_date") or ""))
    chosen = candidates[0]
    return {"target": {"id": chosen["id"], "number": chosen["number"], "status": chosen["status"],
                       "planned_end_date": chosen.get("planned_end_date")}}


FIXTURES = {"late_draft_chain": late_draft_chain, "stopped_not_late": stopped_not_late}
