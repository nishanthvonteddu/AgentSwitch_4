"""Deterministic production logic: why a work order is late, what it blocks, what can move.

Every function re-reads live data. Nothing is cached across calls, because other teams
change the shared book while the agent runs.
"""
import datetime as dt
import json
import re

from . import config
from .mcp_client import McpClient, McpError

OPEN_WO = {"draft", "not_started", "in_progress", "stopped"}
OPEN_MR = {"draft", "submitted", "partially_ordered", "ordered"}
DONE_SCO = {"completed", "cancelled"}
def _csv(statuses: set[str]) -> str:
    """`.list` reads a comma-separated value as OR, so a status set narrows at the server.

    Confirmed over MCP on Suryodaya 2026-09-29: status=draft,not_started returned 78, exactly
    draft (40) + not_started (38). Date operators are NOT available on this interface — the tool
    schema declares planned_end_date as {"format": "date"} and rejects lt:/gte:/between:, though
    REST accepts them on the same login. The Python status checks stay regardless.
    """
    return ",".join(sorted(statuses))


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Signals whose ready date is unknown: no reschedule date can honestly be committed.
UNDATED_BLOCKERS = {
    "subcontract_not_sent", "subcontract_overdue", "material_shortage",
    "quality_rejected", "workstation_unavailable",
    "engineering_change_pending", "workstation_breakdown_active",
}
PENDING_ECO = {"draft", "submitted", "under_review"}
DOWNTIME_LOOKBACK_DAYS = 90
# finite_schedule codes that only restate lateness (renamed in Release 1, 2026-09-17).
SCHEDULE_GENERIC_CAUSES = {"work_content_exceeds_due_date", "due_date_passed"}


def _date(value) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _iso(d: dt.date | None) -> str | None:
    return d.isoformat() if d else None


def _wo_summary(wo: dict) -> dict:
    return {
        "id": wo["id"],
        "number": wo.get("number"),
        "item": wo.get("_item_id_display") or wo.get("item_id"),
        "item_id": wo.get("item_id"),
        "status": wo.get("status"),
        "priority": wo.get("priority"),
        "qty": wo.get("qty"),
        "produced_qty": wo.get("produced_qty"),
        "planned_start_date": (wo.get("planned_start_date") or "")[:10] or None,
        "planned_end_date": (wo.get("planned_end_date") or "")[:10] or None,
        "sales_order_id": wo.get("sales_order_id"),
        "production_strategy": wo.get("production_strategy"),
        "expected_cost": wo.get("expected_cost"),
        "actual_cost": wo.get("actual_cost"),
        "updated_at": wo.get("updated_at"),
    }


def _probe_denied(mcp: McpClient, tool: str) -> str | None:
    """Return a reason if this seat cannot use `tool`, else None."""
    if not mcp.has_tool(tool):
        return "tool_not_in_seat_catalogue"
    try:
        mcp.call(tool, {"limit": 1} if tool.endswith(".list") else {})
        return None
    except McpError as e:
        return e.kind if e.kind in ("permission_denied", "row_scope_denied") else None


# --------------------------------------------------------------------------- lookups

def resolve_work_order(mcp: McpClient, ref: str) -> dict | None:
    ref = (ref or "").strip()
    if UUID_RE.match(ref):
        try:
            return mcp.call("WorkOrder.get", {"id": ref})
        except McpError as e:
            if e.kind == "not_found" or "not found" in e.message.lower():
                return None
            raise
    matches = [w for w in mcp.list_all("WorkOrder", search=ref) if (w.get("number") or "").upper() == ref.upper()]
    return mcp.call("WorkOrder.get", {"id": matches[0]["id"]}) if matches else None


def finite_schedule(mcp: McpClient, horizon_days: int = 14) -> dict:
    res = mcp.call("endpoint.manufacturing.finite_schedule", {"horizon_days": horizon_days})
    return res.get("result", res)


def seat_entities(mcp: McpClient) -> list[str]:
    return sorted({name.split(".")[0] for name in mcp.tool_names() if not name.startswith("endpoint.")})


def _words(name: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Z][a-z]+|[a-z]+", name) if len(w) > 2}


def seat_capability(mcp: McpClient, tool: str) -> dict:
    present = mcp.has_tool(tool)
    denied = _probe_denied(mcp, tool) if present and tool.endswith(".list") else None
    result = {"tool": tool, "in_catalogue": present, "usable": present and not denied, "denied_reason": denied}
    if not present:
        entity = tool.split(".")[0]
        entities = seat_entities(mcp)
        if entity in entities:
            # A wrong operation name (e.g. JobCard.read) says nothing about access to the entity.
            ops = sorted(t[len(entity) + 1:] for t in mcp.tool_names() if t.startswith(entity + "."))
            result["entity_in_catalogue"] = True
            result["available_tools"] = [f"{entity}.{op}" for op in ops]
            result["note"] = f"{entity} IS visible to this seat; '{tool}' is just not an operation name. Do not list {entity} in not_visible."
        elif _rest_status(mcp, entity) in (401, 403):
            # The platform has the entity but refuses this seat: a real, reportable limit.
            result["outside_seat"] = True
            result["note"] = f"{entity} exists on the platform but is outside this seat (REST 403). Put '{entity}' in not_visible."
        else:
            # An invented name proves nothing about access; point at the real entities instead.
            result["warning"] = f"'{entity}' is not an entity on this platform seat; check a real entity before concluding"
            result["similar_entities"] = [e for e in entities if _words(e) & _words(entity)][:5]
    return result


def _rest_status(mcp: McpClient, entity: str) -> int | None:
    """403 = real entity outside the seat, 404 = no such entity (checked live on both books, 2026-09-17)."""
    from .mcp_client import _http
    session = mcp.session
    try:
        url = f"{session.base}/api/{entity}?limit=1"
        status, _ = session.with_reauth(lambda: _http(url, token=session.token))
        return status
    except Exception:
        return None


def company_context(mcp: McpClient) -> dict:
    """Country and currency come from data, never from assumptions about which book we are in."""
    companies = mcp.call("Company.list", {"limit": 1}).get("data", [])
    wo = mcp.call("WorkOrder.list", {"limit": 1}).get("data", [])
    company_id = wo[0]["company_id"] if wo else (companies[0]["id"] if companies else None)
    c = mcp.call("Company.get", {"id": company_id}) if company_id else {}
    return {"company": c.get("_display") or c.get("name"), "country": c.get("country"),
            "currency": c.get("default_currency"), "instance": mcp.session.instance}


def downtime_summary(mcp: McpClient, days: int = 30, reason: str | None = None) -> dict:
    """Recorded downtime per workstation over a window, from DowntimeEntry records (not schedule totals)."""
    denied = _probe_denied(mcp, "DowntimeEntry.list")
    if denied:
        return {"available": False, "not_visible_to_this_seat": [f"DowntimeEntry: {denied}"]}
    today = config.today()
    since = today - dt.timedelta(days=max(int(days), 1))
    stations = {w["id"]: w for w in mcp.list_all("Workstation")}
    per_ws: dict[str, dict] = {}
    counted = 0
    # Round-trips dominate here, not rows: read newest-first and stop at the window edge rather than
    # paging the whole table. The reason filter is an equality match the server can do.
    for d in mcp.list_window("DowntimeEntry", "from_time", _iso(since), **({"reason": reason} if reason else {})):
        began = _date(d.get("from_time"))
        if not began or began < since or (reason and d.get("reason") != reason):
            continue
        counted += 1
        ws = stations.get(d.get("workstation_id"), {})
        row = per_ws.setdefault(d.get("workstation_id"), {
            "workstation": ws.get("number"), "name": ws.get("name") or d.get("_workstation_id_display"),
            "minutes": 0.0, "entries": 0, "reasons": {}, "examples": []})
        mins = d.get("downtime_mins") or 0
        row["minutes"] = round(row["minutes"] + mins, 2)
        row["entries"] += 1
        row["reasons"][d.get("reason")] = round(row["reasons"].get(d.get("reason"), 0) + mins, 2)
        if len(row["examples"]) < 3:
            row["examples"].append({"from": _iso(began), "to": _iso(_date(d.get("to_time"))), "minutes": mins,
                                    "reason": d.get("reason"), "remarks": (d.get("remarks") or "")[:120] or None})
    ranked = sorted(per_ws.values(), key=lambda r: -r["minutes"])
    return {"available": True, "since": _iso(since), "today": _iso(today), "reason_filter": reason,
            "entries_counted": counted, "workstations": ranked}


def list_late_work_orders(mcp: McpClient) -> list[dict]:
    today = config.today()
    sched = {o["work_order_id"]: o for o in finite_schedule(mcp).get("orders", [])}
    late = []
    for wo in mcp.list_all("WorkOrder", status=_csv(OPEN_WO)):
        if wo.get("status") not in OPEN_WO:
            continue
        due = _date(wo.get("planned_end_date"))
        entry = sched.get(wo["id"], {})
        if (due and due < today) or entry.get("verdict") == "late":
            s = _wo_summary(wo)
            s["days_past_due"] = (today - due).days if due else None
            s["projected_finish"] = entry.get("projected_finish")
            late.append(s)
    return sorted(late, key=lambda s: s["planned_end_date"] or "9999")


# --------------------------------------------------------------------------- diagnosis

def diagnose(mcp: McpClient, ref: str) -> dict:
    wo = resolve_work_order(mcp, ref)
    if not wo:
        return {"found": False, "ref": ref}
    today = config.today()
    wid = wo["id"]
    due = _date(wo.get("planned_end_date"))
    signals: list[dict] = []

    def sig(code, record=None, **detail):
        signals.append({"code": code, "blocking": code in UNDATED_BLOCKERS, "record": record, **detail})

    status = wo.get("status")
    if status == "draft":
        sig("not_released", wo.get("number"))
    if status == "stopped":
        sig("stopped_without_recorded_reason", wo.get("number"))
    start = _date(wo.get("planned_start_date"))
    if status == "not_started" and start and start < today:
        sig("not_started_past_planned_start", wo.get("number"), planned_start=_iso(start))

    for sco in mcp.list_all("SubcontractOrder", work_order_id=wid):
        if sco.get("work_order_id") != wid or sco.get("status") in DONE_SCO:
            continue
        expected = _date(sco.get("expected_delivery_date"))
        detail = {"status": sco.get("status"), "expected_delivery_date": _iso(expected),
                  "vendor": sco.get("_vendor_id_display") or sco.get("vendor_id")}
        if sco.get("status") == "draft":
            sig("subcontract_not_sent", sco.get("number"), **detail)
        elif expected and expected < today:
            sig("subcontract_overdue", sco.get("number"), **detail)
        else:
            sig("subcontract_pending", sco.get("number"), ready_date=_iso(expected), **detail)

    for mr in mcp.list_all("MaterialRequest", work_order_id=wid):
        if mr.get("work_order_id") != wid or mr.get("status") not in OPEN_MR:
            continue
        required_by = _date(mr.get("required_by_date"))
        sig("material_request_open", mr.get("number"), status=mr.get("status"),
            required_by_date=_iso(required_by),
            ready_date=_iso(required_by) if required_by and required_by >= today else None,
            overdue=bool(required_by and required_by < today))

    try:
        stock = mcp.call("endpoint.manufacturing.check_stock_availability", {"work_order_id": wid})
        items = stock.get("result", stock).get("items", [])
        for it in items:
            if (it.get("shortage") or 0) > 0:
                sig("material_shortage", it.get("item_id"), required=it.get("required"),
                    available=it.get("available"), shortage=it.get("shortage"), critical=it.get("is_critical"))
        if items and all((it.get("available") or 0) == 0 for it in items):
            sig("stock_check_unreliable", None, note="every component reports 0 available (known platform defect)")
    except McpError as e:
        sig("stock_check_failed", None, error=e.message)

    for qi in mcp.list_all("QualityInspection", reference_id=wid):
        if qi.get("reference_id") != wid:
            continue
        if qi.get("overall_result") == "rejected":
            sig("quality_rejected", qi.get("number") or qi["id"], rejected_qty=qi.get("rejected_qty"))
        elif qi.get("status") == "draft" and wo.get("quality_inspection_required"):
            sig("quality_inspection_pending", qi.get("number") or qi["id"])

    # Shop-floor records. Access has changed on this platform before, so each is probed, never assumed.
    not_visible: list[str] = []

    def readable(entity: str) -> bool:
        reason = _probe_denied(mcp, f"{entity}.list")
        if reason:
            not_visible.append(f"{entity}: {reason}")
        return not reason

    cards_readable, downtime_readable, eco_readable = readable("JobCard"), readable("DowntimeEntry"), readable("EngineeringChangeOrder")

    sched = finite_schedule(mcp)
    entry = next((o for o in sched.get("orders", []) if o["work_order_id"] == wid), None)
    load = {w["workstation_id"]: w for w in sched.get("workstation_load", [])}
    route_ws = set()
    if entry:
        for cause in entry.get("causes", []):
            code = cause.get("code")
            if code in SCHEDULE_GENERIC_CAUSES:
                continue  # restates "past due"; not a reason
            if code == "recorded_downtime" and downtime_readable:
                continue  # the DowntimeEntry records below are the evidence; the schedule's attribution is unreliable
            sig(f"schedule_{code}", cause.get("workstation_label"), minutes=cause.get("minutes"))
        for ws_id in {op["workstation_id"] for op in entry.get("operations", []) if op.get("workstation_id")}:
            route_ws.add(ws_id)
            ws = load.get(ws_id, {})
            if ws.get("state") == "unavailable":
                sig("workstation_unavailable", ws.get("record_label"), status=ws.get("status_code"))
            if not downtime_readable and (ws.get("downtime_minutes") or 0) > 0:
                # Fallback only: an unexplained total, used when downtime records cannot be read.
                sig("workstation_downtime", ws.get("record_label"), downtime_minutes=round(ws["downtime_minutes"], 1))

    operations, current_operation, card_ids = [], None, set()
    if cards_readable:
        cards = sorted((j for j in mcp.list_all("JobCard", work_order_id=wid) if j.get("work_order_id") == wid),
                       key=lambda j: (j.get("sequence") or 0, j.get("number") or ""))
        for j in cards:
            card_ids.add(j["id"])
            if j.get("workstation_id"):
                route_ws.add(j["workstation_id"])
            operations.append({
                "job_card": j.get("number"), "operation": j.get("operation_name"), "sequence": j.get("sequence"),
                "status": j.get("status"), "workstation": j.get("_workstation_id_display") or j.get("workstation_id"),
                "planned_start": (j.get("planned_start") or "")[:10] or None,
                "planned_end": (j.get("planned_end") or "")[:10] or None,
                "started_at": j.get("started_at"), "for_qty": j.get("for_qty"), "completed_qty": j.get("completed_qty"),
            })
        pending = [o for o in operations if o["status"] not in ("completed", "cancelled")]
        if pending:
            current_operation = pending[0]
            start, end = _date(current_operation["planned_start"]), _date(current_operation["planned_end"])
            if current_operation["status"] == "open" and start and start < today:
                sig("operation_not_started", current_operation["job_card"], operation=current_operation["operation"],
                    workstation=current_operation["workstation"], planned_start=_iso(start), days_waiting=(today - start).days)
            elif current_operation["status"] == "in_progress" and end and end < today:
                sig("operation_overrunning", current_operation["job_card"], operation=current_operation["operation"],
                    workstation=current_operation["workstation"], planned_end=_iso(end), days_over=(today - end).days)

    if downtime_readable:
        since = today - dt.timedelta(days=DOWNTIME_LOOKBACK_DAYS)
        route_totals: dict[str, dict] = {}
        # Entries on this order at any age, plus everything inside the lookback window for the route check.
        # Every job-card entry carries work_order_id too, so the first read covers both links.
        merged = {d["id"]: d for d in mcp.list_all("DowntimeEntry", work_order_id=wid)}
        merged.update({d["id"]: d for d in mcp.list_window("DowntimeEntry", "from_time", _iso(since))})
        for d in sorted(merged.values(), key=lambda r: ((r.get("from_time") or ""), r["id"])):
            on_order = d.get("work_order_id") == wid or d.get("job_card_id") in card_ids
            began, ended = _date(d.get("from_time")), _date(d.get("to_time"))
            recent_on_route = d.get("workstation_id") in route_ws and began and began >= since
            if not (on_order or recent_on_route):
                continue
            station = d.get("_workstation_id_display") or d.get("workstation_id")
            if d.get("reason") == "breakdown" and (ended is None or ended >= today):
                sig("workstation_breakdown_active", station, minutes=d.get("downtime_mins"), from_time=_iso(began),
                    remarks=(d.get("remarks") or "")[:120] or None)
            elif on_order:
                sig("downtime_on_order", station, reason=d.get("reason"), minutes=d.get("downtime_mins"),
                    from_time=_iso(began), to_time=_iso(ended), remarks=(d.get("remarks") or "")[:120] or None)
            else:  # context, not a cause: summarised per workstation so it does not drown the real signals
                t = route_totals.setdefault(station, {"minutes": 0.0, "entries": 0, "reasons": {}})
                t["minutes"] = round(t["minutes"] + (d.get("downtime_mins") or 0), 2)
                t["entries"] += 1
                t["reasons"][d.get("reason")] = t["reasons"].get(d.get("reason"), 0) + 1
        for station, t in sorted(route_totals.items(), key=lambda kv: -kv[1]["minutes"]):
            sig("workstation_downtime_recorded", station, since=_iso(since), **t)

    if eco_readable:
        for e in mcp.list_all("EngineeringChangeOrder", status=_csv(PENDING_ECO)):
            hits = [a for a in (e.get("affected_work_orders") or []) if a.get("work_order_id") == wid]
            if hits and e.get("status") in PENDING_ECO:
                sig("engineering_change_pending", e.get("number"), status=e.get("status"), action=hits[0].get("action"),
                    title=e.get("title"), priority=e.get("priority"))

    return {
        "found": True,
        "work_order": _wo_summary(wo),
        "today": _iso(today),
        "is_late": bool(status in OPEN_WO and ((due and due < today) or (entry or {}).get("verdict") == "late")),
        "days_past_due": (today - due).days if due and status in OPEN_WO else None,
        "schedule": {k: (entry or {}).get(k) for k in ("verdict", "projected_finish", "days_late")},
        "route_workstations": sorted({op.get("workstation_label") for op in (entry or {}).get("operations", [])} - {None}),
        "current_operation": current_operation,
        "operations": operations,
        "signals": signals,
        "not_visible_to_this_seat": not_visible,
    }


# --------------------------------------------------------------------------- downstream

def downstream_impact(mcp: McpClient, ref: str, max_depth: int = 3) -> dict:
    wo = resolve_work_order(mcp, ref)
    if not wo:
        return {"found": False, "ref": ref}
    open_wos = [w for w in mcp.list_all("WorkOrder", status=_csv(OPEN_WO)) if w.get("status") in OPEN_WO]
    bom_inputs = {b["id"]: {m.get("item_id") for m in (b.get("materials") or [])} for b in mcp.list_all("BOM")}

    # item consumed -> the open orders whose BOM consumes it, so the walk below is a lookup per node
    # rather than a scan of every open order per node. Built from open_wos, so the order of hits is unchanged.
    consumers: dict[str, list[dict]] = {}
    for w in open_wos:
        for item_id in bom_inputs.get(w.get("bom_id"), ()):
            consumers.setdefault(item_id, []).append(w)

    blocked, visited, frontier = [], {wo["id"]}, [(wo, 0)]
    while frontier:
        parent, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        for w in consumers.get(parent.get("item_id"), ()):
            if w["id"] in visited:
                continue
            visited.add(w["id"])
            # A BOM match shows possible demand, not a confirmed supply link: stock or another order may cover it.
            blocked.append({**_wo_summary(w), "depth": depth + 1, "consumes_output_of": parent.get("number"),
                            "link": "bom_material_match", "confidence": "potential"})
            frontier.append((w, depth + 1))

    so_ids = {}
    for w in [_wo_summary(wo)] + blocked:
        if w.get("sales_order_id"):
            so_ids.setdefault(w["sales_order_id"], w["number"])
    target_number = wo.get("number")

    chain = [_wo_summary(wo)] + blocked
    unlinked_mto = [w["number"] for w in chain if w.get("production_strategy") == "make_to_order" and not w.get("sales_order_id")]
    so_visible = mcp.has_tool("SalesOrder.get")

    sales_orders, not_visible = [], []
    if not so_visible:
        not_visible.append("SalesOrder: tool_not_in_seat_catalogue (customer impact cannot be determined)")
    elif so_ids:
        for so_id, via in so_ids.items():
            try:
                so = mcp.call("SalesOrder.get", {"id": so_id})
            except McpError as e:
                not_visible.append(f"SalesOrder {so_id}: {e.kind}")
                continue
            sales_orders.append({
                "id": so_id, "number": so.get("number"), "customer": so.get("_party_id_display") or so.get("party_id"),
                "delivery_date": (so.get("delivery_date") or "")[:10] or None, "grand_total": so.get("grand_total"),
                "delivered_status": so.get("delivered_status"), "status": so.get("status"), "via_work_order": via,
                # Linked on the late order itself = recorded exposure; linked on a potential consumer = potential.
                "link": "sales_order_id", "confidence": "linked" if via == target_number else "potential",
            })

    if not so_visible:
        customer_impact = "undeterminable: sales orders are not visible to this seat"
    elif unlinked_mto:
        customer_impact = f"partly unknown: make-to-order without a linked sales order: {', '.join(unlinked_mto)}"
    elif sales_orders:
        customer_impact = "known"
    else:
        customer_impact = "none linked: no sales order on this order or its dependants"

    return {
        "found": True,
        "work_order": _wo_summary(wo),
        "customer_impact": customer_impact,
        "make_to_order_without_sales_order": unlinked_mto,
        "potentially_blocked_work_orders": blocked,
        "blocked_sales_orders": sales_orders,
        "method": "reverse walk of BOM materials over open work orders; WorkOrder has no parent/child link, "
                  "so these are potential consumers, not confirmed blocks",
        "not_visible_to_this_seat": not_visible,
    }


# --------------------------------------------------------------------------- reschedule

def propose_reschedule(mcp: McpClient, ref: str, diag: dict | None = None, down: dict | None = None) -> dict:
    """diag/down let a caller hand in results it already fetched this run; both are re-read when absent."""
    diag = diag or diagnose(mcp, ref)
    if not diag["found"]:
        return diag
    down = down or downstream_impact(mcp, ref)
    today = config.today()
    target = diag["work_order"]

    undated = [s for s in diag["signals"] if s["blocking"]]
    ready_dates = [_date(s["ready_date"]) for s in diag["signals"] if s.get("ready_date")]
    proposals = []

    def proposal(w, new_start, new_end, reason, commit_ok=True):
        writable = w["status"] == "draft"
        return {
            "work_order_id": w["id"], "number": w["number"], "status": w["status"],
            "current_start": w["planned_start_date"], "current_end": w["planned_end_date"],
            "new_start": _iso(new_start), "new_end": _iso(new_end), "reason": reason,
            "date_committable": commit_ok,
            "writable_by_seat": writable and commit_ok,
            "why_not_writable": None if writable and commit_ok else (
                "no committable date: " + ", ".join(sorted({s['code'] for s in undated})) if not commit_ok
                else f"status '{w['status']}': dates are locked after submit; changing them needs an admin cancel"),
            "snapshot_updated_at": w["updated_at"],
        }

    def duration(w):
        s, e = _date(w["planned_start_date"]), _date(w["planned_end_date"])
        return dt.timedelta(days=max((e - s).days, 1) if s and e else 1)

    target_end = None
    if target["status"] in ("in_progress", "completed", "cancelled"):
        pass  # already running or closed; nothing to move for the target itself
    elif undated:
        proposals.append(proposal(target, None, None, "blocked by an event with no known date", commit_ok=False))
    else:
        new_start = max([today] + [d for d in ready_dates if d])
        new_end = new_start + duration(target)
        projected = _date(diag["schedule"].get("projected_finish"))
        if projected and projected > new_end:
            new_end = projected
        target_end = new_end
        if (_iso(new_start), _iso(new_end)) != (target["planned_start_date"], target["planned_end_date"]):
            proposals.append(proposal(target, new_start, new_end, "earliest start after known blockers; finish from finite schedule"))

    upstream_end = {target["number"]: target_end}
    for w in down["potentially_blocked_work_orders"]:
        parent_end = upstream_end.get(w["consumes_output_of"])
        if w["status"] in ("in_progress", "stopped"):
            upstream_end[w["number"]] = None
            continue
        if parent_end is None:
            proposals.append(proposal(w, None, None, f"waits on {w['consumes_output_of']}, whose date cannot be committed", commit_ok=False))
            upstream_end[w["number"]] = None
            continue
        start = _date(w["planned_start_date"])
        if start and start > parent_end:
            upstream_end[w["number"]] = _date(w["planned_end_date"])
            continue
        new_start = parent_end + dt.timedelta(days=1)
        new_end = new_start + duration(w)
        upstream_end[w["number"]] = new_end
        proposals.append(proposal(w, new_start, new_end, f"must start after {w['consumes_output_of']} finishes"))

    return {"found": True, "work_order": target, "today": _iso(today), "proposals": proposals,
            "downstream_considered": [b["number"] for b in down["potentially_blocked_work_orders"]]}


def apply_proposal(mcp: McpClient, p: dict, allowed_ids: set[str] | None = None) -> dict:
    """Write one proposal, re-reading first. Never writes a row that changed since it was proposed."""
    out = {"work_order_id": p["work_order_id"], "number": p["number"]}
    if allowed_ids is not None and p["work_order_id"] not in allowed_ids:
        return {**out, "outcome": "refused", "detail": "work order is outside the approved set"}
    if not p.get("writable_by_seat"):
        return {**out, "outcome": "refused", "detail": p.get("why_not_writable")}
    current = mcp.call("WorkOrder.get", {"id": p["work_order_id"]})
    if current.get("updated_at") != p["snapshot_updated_at"] or current.get("status") != p["status"]:
        return {**out, "outcome": "changed_underneath",
                "detail": {"status": current.get("status"), "updated_at": current.get("updated_at")}}
    try:
        mcp.call("WorkOrder.update", {"id": p["work_order_id"],
                                       "planned_start_date": p["new_start"], "planned_end_date": p["new_end"]})
    except McpError as e:
        return {**out, "outcome": "write_rejected", "detail": e.message}
    after = mcp.call("WorkOrder.get", {"id": p["work_order_id"]})
    ok = (after.get("planned_start_date") or "")[:10] == p["new_start"] and (after.get("planned_end_date") or "")[:10] == p["new_end"]
    return {**out, "outcome": "applied" if ok else "write_not_persisted",
            "planned_start_date": after.get("planned_start_date"), "planned_end_date": after.get("planned_end_date")}


# --------------------------------------------------------------------------- escalation

ESCALATION_REASON_CODES = ("policy_refusal", "unresolved_after_retries", "other")


def escalation_assignees(mcp: McpClient) -> list[dict]:
    res = mcp.call("endpoint.agent_governance.escalations.assignees", {})
    return (res.get("result", res) or {}).get("options", [])


def open_agent_session(mcp: McpClient, title: str) -> str:
    return mcp.call("AgentSession.create", {"title": title[:120], "channel": "api"})["id"]


def raise_escalation(mcp: McpClient, session_id: str, reason: str, reason_code: str = "policy_refusal",
                     sla_minutes: int = 240) -> dict:
    """Hand work to a person. The platform names who; if nobody is assignable, say so instead of pretending."""
    if reason_code not in ESCALATION_REASON_CODES:
        reason_code = "other"
    assignees = escalation_assignees(mcp)
    if not assignees:
        return {"raised": False, "reason_code": "no_assignee",
                "detail": "no escalation assignee is configured for this company; a person must be contacted directly"}
    assignee = assignees[0]
    res = mcp.call("endpoint.agent_governance.escalations.raise", {
        "session_id": session_id, "assignee_party_id": assignee["id"], "reason": reason,
        "reason_code": reason_code, "sla_minutes": sla_minutes})
    res = res.get("result", res)
    if not res.get("ok"):  # the endpoint reports refusals inside a success envelope
        return {"raised": False, "reason_code": res.get("reason_code"), "detail": "the platform refused the escalation"}
    esc = res["escalation"]
    return {"raised": True, "escalation_id": esc["id"], "number": esc.get("number"), "status": esc.get("status"),
            "assignee": esc.get("assignee_display"), "due_at": esc.get("due_at")}


def withdraw_escalation(mcp: McpClient, escalation_id: str, note: str) -> dict:
    res = mcp.call("endpoint.agent_governance.escalations.update", {
        "escalation_id": escalation_id, "action": "withdraw", "note": note, "outcome": "withdrawn"})
    return res.get("result", res)


# --------------------------------------------------------------------------- shop floor and capacity

# The platform's own OEE basis (endpoint.manufacturing.kpis) defines a card's standard work content as
# (time_in_mins x for_qty) / actual_time_in_mins for performance, so time_in_mins is PER PIECE. capacity_board
# and finite_schedule charge it unscaled, understating load ~94x on Suryodaya and ~84x on Keystone (our report
# C1, 2026-09-24). Both figures are reported rather than silently substituting ours: the board's number is what
# the platform stands behind, and the divergence is itself the finding.
OPEN_JOB_CARD = {"open", "not_started", "in_progress", "paused"}


def _work_content_minutes(cards: list[dict]) -> float:
    """Standard work content per the platform's own OEE formula: sum(time_in_mins x for_qty)."""
    return round(sum((c.get("time_in_mins") or 0) * (c.get("for_qty") or 0) for c in cards), 2)


def capacity_outlook(mcp: McpClient, horizon_days: int = 21) -> dict:
    """Load against declared capacity over a horizon, with the board's figure and the work content it omits."""
    denied = _probe_denied(mcp, "JobCard.list")
    if denied:
        return {"available": False, "not_visible_to_this_seat": [f"JobCard: {denied}"]}
    res = mcp.call("endpoint.manufacturing.capacity_board", {"horizon_days": int(horizon_days)})
    board = res.get("result", res) or {}
    counts = board.get("counts") or {}
    cards = [c for c in mcp.list_all("JobCard", status=_csv(OPEN_JOB_CARD)) if c.get("status") in OPEN_JOB_CARD]
    stations = {w["id"]: w for w in mcp.list_all("Workstation")}

    booked = counts.get("booked_minutes")
    available = counts.get("available_minutes")
    content = _work_content_minutes(cards)
    per_ws: dict[str, float] = {}
    for c in cards:
        minutes = (c.get("time_in_mins") or 0) * (c.get("for_qty") or 0)
        per_ws[c.get("workstation_id")] = per_ws.get(c.get("workstation_id"), 0.0) + minutes
    overloaded = []
    for ws_id, minutes in sorted(per_ws.items(), key=lambda kv: -kv[1]):
        ws = stations.get(ws_id) or {}
        capacity = (ws.get("working_hours_per_day") or 0) * 60.0 * (ws.get("capacity") or 1) * int(horizon_days)
        if capacity and minutes > capacity:
            overloaded.append({"workstation": ws.get("number"), "name": ws.get("name"),
                               "work_content_minutes": round(minutes, 1),
                               "declared_capacity_minutes": round(capacity, 1),
                               "times_over": round(minutes / capacity, 2)})

    understated = bool(booked) and content > booked * 1.5
    return {
        "available": True, "horizon_days": int(horizon_days), "open_job_cards": len(cards),
        "board_reported": {"booked_minutes": booked, "available_minutes": available,
                           "workstations_overloaded": counts.get("workstations_overloaded"),
                           "periods_overloaded": counts.get("periods_overloaded"),
                           "load_pct": round(100 * booked / available, 2) if booked and available else None},
        "work_content": {"minutes": content,
                         "basis": "sum(JobCard.time_in_mins x for_qty), the platform's own OEE basis",
                         "load_pct": round(100 * content / available, 1) if available else None,
                         "workstations_over_declared_capacity": overloaded},
        "board_understates_load": understated,
        "confidence": "disputed" if understated else "board_agrees",
        "note": ("capacity_board charges time_in_mins unscaled while the platform's OEE basis defines work "
                 f"content as time_in_mins x for_qty; the two disagree by {round(content / booked, 1)}x here. "
                 "Report both figures and do not present either as settled."
                 if understated else "the board's booked minutes agree with the work content in the same cards"),
    }


def order_feasible_by(mcp: McpClient, ref: str, due: str) -> dict:
    """Whether an order's remaining work can credibly finish by a date. The seat charter's first question."""
    wo = resolve_work_order(mcp, ref)
    if not wo:
        return {"found": False, "work_order": ref, "reason": "no work order with that number or id"}
    target = _date(due)
    if not target:
        return {"found": True, "work_order": wo.get("number"), "verdict": "unknown",
                "reason": f"could not read {due!r} as a date"}
    today = config.today()
    cards = [c for c in mcp.list_all("JobCard", work_order_id=wo["id"], status=_csv(OPEN_JOB_CARD))
             if c.get("status") in OPEN_JOB_CARD]
    remaining = _work_content_minutes(cards)
    diag = diagnose(mcp, wo.get("number") or ref)
    blocking = list(diag.get("blocking_causes") or [])
    outlook = capacity_outlook(mcp)
    days = (target - today).days

    if blocking:
        # An undated blocker is the honest stop: no arithmetic can promise a date behind it.
        verdict = "no"
        reason = f"blocked with no known ready date: {', '.join(blocking)}"
    elif days < 0:
        verdict, reason = "no", f"the date asked for ({due}) is already past"
    elif not cards:
        verdict, reason = "unknown", "no open job cards on this order, so there is no work content to size"
    else:
        verdict = "unknown"
        reason = ("remaining work content is known but the platform cannot place it: finite_schedule models no "
                  "work calendar and no labour capacity, and capacity_board's own load figure is disputed "
                  "(capacity_outlook.board_understates_load)")
    return {
        "found": True, "work_order": wo.get("number"), "asked_by": due, "today": _iso(today),
        "days_available": days, "open_job_cards": len(cards),
        "remaining_work_content_minutes": remaining,
        "remaining_work_content_hours": round(remaining / 60.0, 1),
        "blocking_causes": blocking, "contributing_causes": diag.get("contributing_causes") or [],
        "shop_load": {"board_load_pct": (outlook.get("board_reported") or {}).get("load_pct"),
                      "work_content_load_pct": (outlook.get("work_content") or {}).get("load_pct"),
                      "disputed": outlook.get("board_understates_load")},
        "verdict": verdict, "reason": reason,
        "note": "a yes verdict is deliberately not offered: the platform models no work calendar or labour "
                "capacity, so a committed date would be a number the platform itself would not stand behind",
    }


def shop_floor_exceptions(mcp: McpClient, limit: int = 50) -> dict:
    """Where the shop floor is stuck, from the platform's exception cockpit. The charter's second question."""
    if not mcp.has_tool("endpoint.manufacturing.exception_cockpit"):
        return {"available": False, "not_visible_to_this_seat": ["endpoint.manufacturing.exception_cockpit"]}
    res = mcp.call("endpoint.manufacturing.exception_cockpit", {"limit": int(limit), "offset": 0})
    cockpit = res.get("result", res) or {}
    items = cockpit.get("items") or []
    lanes: dict[str, dict] = {}
    for it in items:
        lane = lanes.setdefault(it.get("kind") or "unknown",
                                {"kind": it.get("kind"), "count": 0, "severities": {}, "examples": []})
        lane["count"] += 1
        sev = it.get("severity") or "unknown"
        lane["severities"][sev] = lane["severities"].get(sev, 0) + 1
        if len(lane["examples"]) < 3:
            lane["examples"].append({"record": it.get("record_label"), "entity": it.get("entity"),
                                     "severity": sev, "state": it.get("state"),
                                     "diagnostic": (it.get("diagnostic") or {}).get("code")})
    total = cockpit.get("total_items")
    return {
        "available": True, "total_items": total, "returned": len(items),
        "truncated": bool(total is not None and len(items) < total),
        "complete": cockpit.get("complete"),
        # lane_states says which lanes the platform could not fill; a lane missing for that reason is not "clear".
        "lane_states": cockpit.get("lane_states"),
        "lanes": sorted(lanes.values(), key=lambda r: -r["count"]),
    }


# --------------------------------------------------------------------------- declared policy

def seat_policy_conformance(mcp: McpClient) -> dict:
    """Compare the platform's DECLARED agent policy with what this seat can observably reach.

    AgentToolPolicy and AgentPersona.require_approval configure the platform's own hosted persona, not this
    API user, so a divergence is not automatically a defect. It is reported as a divergence, never as a
    permission: what this seat may actually do is decided by seat_capability probes, not by these rows.
    """
    if not mcp.has_tool("AgentToolPolicy.list"):
        return {"available": False, "not_visible_to_this_seat": ["AgentToolPolicy"]}
    seats = [s for s in mcp.list_all("AgentSeat") if (s.get("module") or "").lower() == "manufacturing"]
    if not seats:
        return {"available": False, "reason": "no manufacturing AgentSeat declared on this instance"}
    seat = seats[0]
    personas = {p["id"]: p for p in mcp.list_all("AgentPersona")}
    policies = {p["id"]: p for p in mcp.list_all("AgentToolPolicy")}
    persona = personas.get(seat.get("persona_id")) or {}
    policy = policies.get(persona.get("tool_policy_id")) or {}

    denied_entities = [e.strip() for e in (policy.get("denied_entities") or "").split(",") if e.strip()]
    allowed_domains = [d.strip() for d in (policy.get("allowed_domains") or "").split(",") if d.strip()]
    observed = []
    for entity in denied_entities:
        reachable = _rest_status(mcp, entity) == 200
        observed.append({"entity": entity, "declared": "denied", "this_seat_reads_it": reachable,
                         "divergence": reachable})
    return {
        "available": True,
        "seat": {"name": seat.get("name"), "charter": seat.get("charter"),
                 "goal_keys": seat.get("goal_keys"), "tier": seat.get("tier")},
        "declared_policy": {"name": policy.get("name"), "allowed_domains": allowed_domains,
                            "denied_entities": denied_entities, "read_only": policy.get("read_only"),
                            "max_records_per_query": policy.get("max_records_per_query")},
        "requires_human_approval": [r.get("action_pattern") for r in (persona.get("require_approval") or [])],
        "denied_entity_observations": observed,
        "divergences": [o["entity"] for o in observed if o["divergence"]],
        "caveat": "these rows configure the platform's hosted persona, not this API user. A divergence is a "
                  "question for the platform owners, not permission for this seat to act: confirm every real "
                  "limit with seat_capability before reporting it.",
    }


# --------------------------------------------------------------------------- findings

def record_finding(mcp: McpClient, run_id: str, finding: dict) -> dict:
    """Persist the agent's conclusion so verifiers read the database, not the reply text."""
    payload = {"run_id": run_id, "recorded_at": dt.datetime.now(dt.UTC).isoformat(), **finding}
    row = mcp.call("AgentMemory.create", {
        "content": config.FINDING_PREFIX + json.dumps(payload, sort_keys=True),
        "category": "fact", "source": "system", "importance": 0.5, "is_active": True,
    })
    return {"agent_memory_id": row.get("id"), "run_id": run_id}

# --------------------------------------------------------------------------- generic reads

# The specific tools above encode judgement a raw row does not carry (blocking vs contributing,
# "potential" never "confirmed", a verdict that may not say yes). query_records exists only for
# questions none of them answers; the prompt keeps it a fallback. Everything here is read-only.
QUERY_MAX_ROWS = 200            # never hand the model more than this in one call
QUERY_NARROW_ABOVE = 2000       # above this, return the count and refuse to page

# Rows are the wrong unit to cap on: a BOM row carrying its materials and operations is ~3,300
# characters, a Workstation row ~300. Seen live 2026-09-29: 100 BOMs came back as 100 of 100 with
# truncated false, serialised to 327k characters, and the 60k tool-content limit then cut it to about
# 16 rows — the model was told it held every BOM while holding a sixth of them. Budget by size, below
# the transport limit, so the envelope's own count is the number that actually arrives.
QUERY_MAX_CHARS = 40000

# `.list` accepts these alongside real field names; they shape the page rather than filter it.
_QUERY_CONTROLS = {"limit", "offset", "sort_by", "sort_order", "search"}


QUERY_GROUP_MAX_VALUES = 40      # distinct values reported; the tail is summed into "other"
QUERY_GROUP_LABEL_CHARS = 80     # an error message is a legitimate group key and can be long


def query_group(mcp: McpClient, entity: str, field: str, filters: dict | None = None) -> dict:
    """Count matching records by one field, over the whole matching set rather than a page.

    A page cannot answer "what were they doing". Seen live 2026-09-29: handed 50 of 1209 failed
    AgentJob rows, the model reported one error code for all 1209 — the true split was 773/328/107/1.
    Warning it not to generalise leaves it no way to be right; counting does. MCP exposes no aggregate
    tool (REST has one), so the rows are walked here and only the counts are returned, which keeps the
    payload small no matter how many rows were read.
    """
    filters = {k: v for k, v in (filters or {}).items() if v is not None and k not in _QUERY_CONTROLS}
    if not mcp.has_tool(f"{entity}.list"):
        return {"entity": entity, "error": "not readable by this seat", **seat_capability(mcp, f"{entity}.list")}
    try:
        rows = mcp.list_all(entity, **filters)
    except McpError as e:
        return {"entity": entity, "error": e.message, "filters_sent": sorted(filters)}

    counts: dict[str, int] = {}
    missing = 0
    for r in rows:
        value = r.get(field)
        if value is None or value == "":
            missing += 1
            continue
        label = str(value)
        if len(label) > QUERY_GROUP_LABEL_CHARS:
            label = label[:QUERY_GROUP_LABEL_CHARS] + "…"
        counts[label] = counts.get(label, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    groups = dict(ranked[:QUERY_GROUP_MAX_VALUES])
    tail = sum(n for _, n in ranked[QUERY_GROUP_MAX_VALUES:])
    out = {"entity": entity, "filters": filters, "group_by": field,
           "scanned": len(rows), "total": rows.total, "groups": groups,
           "distinct_values": len(ranked), "complete": not rows.truncated}
    if tail:
        out["other_values_combined"] = tail
    if missing:
        out["rows_with_no_value"] = missing
    if rows.truncated:
        out["instruction"] = (f"counted {len(rows)} of {rows.total} matching records — the read stopped at its "
                              "ceiling, so these counts are a floor, not the full split. Say so, and narrow "
                              "the filters if an exact split is needed.")
    return out


def query_records(mcp: McpClient, entity: str, filters: dict | None = None,
                  limit: int = 50, sort_by: str | None = None, newest_first: bool = True) -> dict:
    """Read rows of one entity this seat may see, always bounded and always saying what was not returned.

    Counts before it fetches: a counting question costs one call and no rows, and a million matches
    costs the same. `total` is returned beside every page so the model cannot mistake a page for the
    whole table — the failure this exists to prevent.
    """
    filters = {k: v for k, v in (filters or {}).items() if v is not None and k not in _QUERY_CONTROLS}
    # limit=0 is the platform's own count-only idiom, so keep it meaning that rather than "unset".
    limit = 0 if limit == 0 else max(1, min(int(limit or 50), QUERY_MAX_ROWS))

    if not mcp.has_tool(f"{entity}.list"):
        # Reuse the probe that already distinguishes a wrong name from a real seat limit.
        return {"entity": entity, "error": "not readable by this seat", **seat_capability(mcp, f"{entity}.list")}

    page = {"limit": 1, **filters}
    if sort_by:
        page.update({"sort_by": sort_by, "sort_order": "desc" if newest_first else "asc"})
    try:
        probe = mcp.call(f"{entity}.list", page)
    except McpError as e:
        # The platform names the offending filter ("Unknown filter 'x' for Y"), which is worth passing through:
        # closed schemas mean a wrong field name fails loudly rather than returning every row.
        return {"entity": entity, "error": e.message, "filters_sent": sorted(filters),
                "instruction": "check the field name against a row you have already seen, then call again"}

    total = probe.get("total") if isinstance(probe, dict) else None
    if total is None:
        total = len(probe.get("data", []) if isinstance(probe, dict) else probe or [])

    result = {"entity": entity, "filters": filters, "total": total}
    if total == 0:
        return {**result, "returned": 0, "rows": [], "truncated": False}
    if total > QUERY_NARROW_ABOVE:
        # Paging this would cost total/200 sequential calls and overflow the context either way.
        return {**result, "returned": 0, "rows": [], "truncated": True, "too_many": True,
                "instruction": f"{total} records match. Add filters (status, a date with lt:/gte:/between:, an id) "
                               "and call again, or tell the user the set is too large to read and what would narrow it."}

    if limit == 0:
        return {**result, "returned": 0, "rows": [], "truncated": total > 0, "count_only": True}

    rows = mcp.call(f"{entity}.list", {**page, "limit": min(limit, total)}).get("data", [])

    # Trim to what will survive serialisation, so returned/truncated describe what the model receives
    # rather than what was fetched. Fencing inflates this further upstream, hence the margin.
    kept, budget = [], QUERY_MAX_CHARS
    for row in rows:
        cost = len(json.dumps(row, default=str)) + 2
        if kept and cost > budget:
            break
        kept.append(row)
        budget -= cost
    dropped_for_size = len(rows) - len(kept)
    rows = kept
    truncated = total > len(rows)
    out = {**result, "returned": len(rows), "rows": rows, "truncated": truncated}
    if dropped_for_size:
        out["dropped_for_size"] = dropped_for_size
        out["size_limited"] = True
    if truncated:
        # Naming the move matters: seen live 2026-09-29, the model was told it held 50 of 100 BOMs and
        # moved on rather than asking for the rest, so a question answerable from the full set was
        # answered from half of it. Rows carry their child arrays, so "fetch it all and scan" is often
        # the whole answer — say so when the set actually fits under the cap.
        fits = total <= QUERY_MAX_ROWS and not dropped_for_size
        if dropped_for_size:
            nextstep = (f"These rows are large, so only {len(rows)} of {total} fit in one reply. Asking for a "
                        "higher limit will NOT return more. Narrow the filters so fewer records match, or use "
                        "query_group if you only need counts across the whole set.")
        elif fits:
            nextstep = f"All {total} fit in one call: repeat this query with limit={total} to get them all."
        else:
            nextstep = (f"{total} will not fit in one call (cap {QUERY_MAX_ROWS}): narrow the filters, "
                        "or use query_group if you only need counts.")
        out["next_step"] = nextstep
        out["instruction"] = (f"these are {len(rows)} of {total} matching records, ordered by "
                              f"{sort_by or 'server default'}. Say so rather than presenting them as all of them. "
                              "Never total or average over them, and never say what these records have in common "
                              "as though it held for all {total}: if you are about to describe the set — a shared "
                              "error, status, reason, owner or kind — call query_group on that field instead, which "
                              "counts every matching record. " + nextstep).format(total=total)
    return out


# --- Stopped/finished orders are never late ---------------------------------
NOT_LATE_STATUSES = {"stopped", "cancelled", "canceled", "completed", "closed"}

_diagnose_original = diagnose


def diagnose(mcp, ref, *args, **kwargs):
    result = _diagnose_original(mcp, ref, *args, **kwargs)
    if isinstance(result, dict):
        wo = result.get("work_order") or {}
        status = str(wo.get("status") or "").strip().lower()
        if status in NOT_LATE_STATUSES:
            result["is_late"] = False
    return result