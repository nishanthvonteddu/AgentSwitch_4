"""What a verifier receives and returns.

A verifier is `def check(ctx: VerifyContext) -> Verdict | tuple[Verdict, str]`.
It must decide from the database (ctx.rest / ctx.finding_from_db()), not from ctx.result["final_answer"].
Any exception inside a verifier yields UNEVALUATED, which never counts as a pass.
"""
import datetime as dt
import enum
import json
from pathlib import Path

from prod_agent import config
from prod_agent.mcp_client import LIST_ALL_MAX_ROWS, RestClient

# AgentMemory rows are large (a 200-row page was 338 KB on 2026-10-07), so the page stays at 200; the scan stops at the
# same ceiling as every other table read.
FINDING_PAGE = 200
FINDING_SCAN_MAX_ROWS = LIST_ALL_MAX_ROWS
FINDING_CLOCK_MARGIN = dt.timedelta(minutes=10)

NULL_LIKE_STRINGS = {"", "n/a", "na", "nil", "none", "null"}
TOP_LEVEL_NULLABLE_FIELDS = ("work_order", "is_late", "currency", "cost", "refusal_reason")
NESTED_NULLABLE_FIELDS = {
    "rescheduled": ("new_start", "new_end"),
    "escalations": ("number", "assignee", "reason_code"),
}


def _normalise_null_like_strings(payload: dict) -> None:
    for field in TOP_LEVEL_NULLABLE_FIELDS:
        value = payload.get(field)
        if field in payload and isinstance(value, str) and value.strip().casefold() in NULL_LIKE_STRINGS:
            payload[field] = None
    for container, fields in NESTED_NULLABLE_FIELDS.items():
        items = payload.get(container)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in fields:
                value = item.get(field)
                if field in item and isinstance(value, str) and value.strip().casefold() in NULL_LIKE_STRINGS:
                    item[field] = None


class Verdict(str, enum.Enum):
    APPROVE = "approve"
    REVISE = "revise"
    UNEVALUATED = "unevaluated"


class VerifyContext:
    def __init__(self, *, rest: RestClient, instance: str, task: dict, run_dir: Path, fixture: dict | None,
                 context: dict | None = None):
        self.rest = rest
        self.instance = instance
        self.task = task
        self.run_dir = run_dir
        self.fixture = fixture or {}
        context = context or {}
        self.me = context.get("me")                      # our user id on this instance
        self.started_at = context.get("started_at_utc")  # compare with server updated_at (UTC)
        self.snapshot = context.get("snapshot", {})      # rows by number, read before the agent ran
        # Loaded from disk on purpose: scoring only ever sees what was persisted.
        self.result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.run_id = self.result.get("run_id")

    def trace_events(self) -> list[dict]:
        path = self.run_dir / "trace.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def tool_calls(self) -> list[dict]:
        return [e for e in self.trace_events() if e.get("type") == "tool"]

    def finding_from_db(self) -> dict | None:
        """The finding this run wrote to AgentMemory, read back over REST. None if it never landed.

        AgentMemory is shared with every other run and team on the tenant and REST has no proven content filter,
        so this pages newest-first until the finding turns up. It stops early only once the rows read so far are
        observed to be newest-first and reach back before the run started (less a clock margin; the finding is written
        after the start), so a server that ignores sort_order (B14) costs a longer read, never a wrong None. A row nobody can parse is
        another run's problem and is skipped; one that names this run and still cannot be parsed raises, as does
        passing FINDING_SCAN_MAX_ROWS, because "no finding" would score REVISE where the truth is unknown.
        """
        cutoff = self._finding_cutoff()
        offset, unparsed_own, previous, descending, past_start = 0, None, None, None, False
        while not past_start:
            page = self.rest.raw("/api/AgentMemory", sort_by="created_at", sort_order="desc",
                                 limit=FINDING_PAGE, offset=offset)
            rows = page.get("data", []) if isinstance(page, dict) else []
            for row in rows:
                content = row.get("content")
                if isinstance(content, str) and content.startswith(config.FINDING_PREFIX):
                    try:
                        payload = json.loads(content[len(config.FINDING_PREFIX):])
                    except ValueError:
                        payload = None
                    if not isinstance(payload, dict):
                        if self.run_id and self.run_id in content:
                            unparsed_own = row.get("id")
                        continue
                    if payload.get("run_id") == self.run_id:
                        # Some models emit JSON null as a quoted string in otherwise valid findings.
                        _normalise_null_like_strings(payload)
                        return {**payload, "_agent_memory_id": row["id"], "_created_by": row.get("created_by")}
                created = row.get("created_at")
                if isinstance(created, str) and created:
                    # None until a strict decrease is seen: one row, or rows that all tie, prove no direction.
                    if previous is not None and created != previous and descending is not False:
                        descending = created < previous
                    previous = created
                    if descending and cutoff and created < cutoff:
                        past_start = True
                        break
            offset += len(rows)
            total = page.get("total") if isinstance(page, dict) else None
            if not rows or (offset >= total if isinstance(total, int) else len(rows) < FINDING_PAGE):
                break
            if offset >= FINDING_SCAN_MAX_ROWS and not past_start:
                raise RuntimeError(f"no finding for run {self.run_id} in the newest {offset} AgentMemory rows; "
                                   "read no further, so whether it landed is unknown")
        if unparsed_own:
            raise ValueError(f"AgentMemory row {unparsed_own} names run {self.run_id} but is not a JSON object")
        return None

    def _finding_cutoff(self) -> str | None:
        """created_at before which no row can be this run's finding, or None to read without an early stop.

        started_at_utc is normally the server's own clock, but capture_context falls back to ours when the Date
        header is unreadable, so the cutoff keeps a margin for skew rather than trusting the two clocks to agree."""
        try:
            started = dt.datetime.fromisoformat(self.started_at) if isinstance(self.started_at, str) else None
        except ValueError:
            return None
        return (started - FINDING_CLOCK_MARGIN).replace(tzinfo=None).isoformat() if started else None

    def seat_tool_names(self) -> set[str]:
        """The MCP catalogue for this seat. Seat rules are defined by it, so this one read uses MCP."""
        from prod_agent.mcp_client import McpClient
        return McpClient(self.rest.session).tool_names()

    def is_denied(self, path: str) -> bool:
        from prod_agent.mcp_client import McpError
        try:
            self.rest.raw(path, limit=1)
            return False
        except McpError as e:
            return e.code in (401, 403)

    def my_writes_since_start(self, entity: str, exclude_ids=()) -> list[str]:
        """Rows of `entity` last updated by us after the run started, excluding allowed ids."""
        return [r.get("number") or r["id"] for r in self.rest.list(entity)
                if r.get("updated_by") == self.me and (r.get("updated_at") or "") >= (self.started_at or "")
                and r["id"] not in set(exclude_ids)]

    def work_order(self, number_or_id: str) -> dict | None:
        if len(number_or_id) == 36:
            return self.rest.get("WorkOrder", number_or_id)
        hits = [w for w in self.rest.list("WorkOrder", search=number_or_id) if w.get("number") == number_or_id]
        return self.rest.get("WorkOrder", hits[0]["id"]) if hits else None


def normalise(outcome) -> tuple[Verdict, str]:
    if isinstance(outcome, tuple):
        verdict, reason = outcome
    else:
        verdict, reason = outcome, ""
    if not isinstance(verdict, Verdict):
        return Verdict.UNEVALUATED, f"verifier returned {verdict!r}, not a Verdict"
    return verdict, reason
