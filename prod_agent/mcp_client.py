"""Minimal MCP (JSON-RPC over HTTP POST) and REST clients for AgentSwitch.

A JSON-RPC error still arrives as HTTP 200, so every call inspects the envelope.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

PROTOCOL_VERSION = "2025-11-25"
TOKEN_DIR = config.ROOT / ".tokens"


class McpError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data or {}

    @property
    def kind(self) -> str:
        if isinstance(self.data, dict) and self.data.get("code"):
            return self.data["code"]
        if "permission" in self.message.lower() or "cannot perform" in self.message.lower():
            return "permission_denied"
        if self.code == -32601 or "unknown tool" in self.message.lower():
            return "unknown_tool"
        return "error"


class AuthExpired(Exception):
    pass


CONNECT_RETRIES = 3
CONNECT_BACKOFF_SECONDS = 2.0


GATEWAY_ERRORS = {502, 503, 504}


def _http(url, body=None, token=None, method=None, timeout=120, retries=CONNECT_RETRIES, read_only=None):
    """read_only: retry gateway errors too. Defaults to True for bodiless requests (GET). A gateway error on a
    write may come after the server applied it, so writes are never retried on 502/503/504."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    if read_only is None:
        read_only = data is None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise AuthExpired(url) from e
            if read_only and e.code in GATEWAY_ERRORS and attempt < retries:
                time.sleep(CONNECT_BACKOFF_SECONDS * (attempt + 1))
                continue
            try:
                payload = json.loads(e.read() or b"null")
            except ValueError:
                payload = None
            return e.code, payload
        except urllib.error.URLError:
            # urllib raises URLError only while connecting or sending, before any response exists,
            # so a retry cannot repeat a write the server processed. A timeout while waiting for the
            # response is a plain TimeoutError and is deliberately not retried.
            if attempt == retries:
                raise
            time.sleep(CONNECT_BACKOFF_SECONDS * (attempt + 1))


class Session:
    """Login for one instance. Tokens are cached under .tokens/ namespaced by team and instance."""

    def __init__(self, instance: str):
        if instance not in config.INSTANCES:
            raise ValueError(f"unknown instance {instance!r}")
        self.instance = instance
        self.base = config.INSTANCES[instance]
        self._lock = threading.Lock()
        self._token_file = TOKEN_DIR / f"team04-{instance}.token"
        self.token = self._token_file.read_text().strip() if self._token_file.exists() else None
        if not self.token:
            self.login()

    def login(self):
        email, password = config.credentials(self.instance)
        status, payload = _http(f"{self.base}/api/auth/login", {"email": email, "password": password})
        if status != 200 or not payload or "token" not in payload:
            raise RuntimeError(f"login failed on {self.instance}: HTTP {status}")
        self.token = payload["token"]
        TOKEN_DIR.mkdir(exist_ok=True)
        self._token_file.write_text(self.token)
        return payload

    def with_reauth(self, fn):
        try:
            return fn()
        except AuthExpired:
            stale = self.token
            with self._lock:
                # Concurrent tool calls share one session: whoever gets here first re-logs in, the rest reuse it.
                if self.token == stale:
                    self.login()
            return fn()


def _is_descending(batch: list[dict], field: str) -> bool | None:
    """True/False if `batch` proves the sort direction, None if it is too short or too uniform to tell."""
    values = [(r.get(field) or "")[:10] for r in batch]
    values = [v for v in values if v]
    if len(values) < 2 or values[0] == values[-1]:
        return None
    return values[0] > values[-1]


class McpClient:
    def __init__(self, session: Session):
        self.session = session
        self._id = 0
        self._lock = threading.Lock()
        self._tools = None
        self.session.with_reauth(self._initialize)

    READ_ONLY_ENDPOINTS = {
        "endpoint.manufacturing.finite_schedule", "endpoint.manufacturing.check_stock_availability",
        "endpoint.manufacturing.genealogy", "endpoint.agent_governance.escalations",
        "endpoint.agent_governance.escalations.assignees",
    }

    @classmethod
    def _is_read_only(cls, method, params) -> bool:
        if method in ("initialize", "tools/list"):
            return True
        name = (params or {}).get("name", "") if method == "tools/call" else ""
        return name.endswith((".list", ".get")) or name in cls.READ_ONLY_ENDPOINTS

    def _rpc(self, method, params=None):
        with self._lock:
            self._id += 1
            request_id = self._id
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        status, payload = _http(f"{self.session.base}/api/mcp", body, self.session.token,
                                read_only=self._is_read_only(method, params))
        if status != 200 or payload is None:
            raise McpError(status, f"HTTP {status}", payload)
        if "error" in payload:
            err = payload["error"]
            raise McpError(err.get("code"), err.get("message", ""), err.get("data"))
        return payload.get("result")

    def _initialize(self):
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "team04-production-agent", "version": "0.1"},
        })

    def tool_names(self) -> set[str]:
        if self._tools is None:
            result = self.session.with_reauth(lambda: self._rpc("tools/list"))
            self._tools = {t["name"]: t for t in result.get("tools", [])}
        return set(self._tools)

    def has_tool(self, name: str) -> bool:
        return name in self.tool_names()

    def call(self, name: str, arguments: dict | None = None):
        result = self.session.with_reauth(
            lambda: self._rpc("tools/call", {"name": name, "arguments": arguments or {}}))
        blocks = result.get("content")
        if result.get("structuredContent") is not None:
            content = result["structuredContent"]
        elif not (isinstance(blocks, list) and all(isinstance(b, dict) for b in blocks)):
            content = result  # some tools return the record itself as the result
        else:
            text = "".join(c.get("text", "") for c in result.get("content", []))
            try:
                content = json.loads(text)
            except ValueError:
                content = {"text": text}
        if result.get("isError"):
            raise McpError("tool_error", json.dumps(content)[:500], content)
        return content

    def list_window(self, entity: str, field: str, since: str, page: int = 200, **filters) -> list[dict]:
        """Rows of `entity` from `since` onwards, oldest first.

        `.list` filters are equality-only, so a date range cannot be pushed to the server, but the sort can:
        paging newest-first and stopping at the window edge bounds the read by the window rather than by the
        size of the table. Returned oldest first so callers aggregate in a stable order — the unsorted scan
        this replaces was in whatever order the server happened to return.
        """
        rows, offset, descending = [], 0, None
        while True:
            res = self.call(f"{entity}.list", {"limit": page, "offset": offset,
                                               "sort_by": field, "sort_order": "desc", **filters})
            batch = res.get("data", []) if isinstance(res, dict) else res
            if descending is None:
                descending = _is_descending(batch, field)
                if descending is False:
                    # B14: this platform accepts an invalid sort_order silently and serves ascending. Stopping
                    # early on ascending rows would return nothing at all, so fall back to reading the table.
                    return [r for r in self.list_all(entity, page=page, **filters)
                            if ((r.get(field) or "")[:10] or since) >= since]
            for row in batch:
                value = (row.get(field) or "")[:10]
                if value and value < since:
                    return rows[::-1]
                rows.append(row)
            total = res.get("total") if isinstance(res, dict) else None
            offset += len(batch)
            if not batch or len(batch) < page or (total is not None and offset >= total):
                return rows[::-1]

    def list_all(self, entity: str, page: int = 200, **filters) -> list[dict]:
        rows, offset = [], 0
        while True:
            res = self.call(f"{entity}.list", {"limit": page, "offset": offset, **filters})
            batch = res.get("data", []) if isinstance(res, dict) else res
            rows.extend(batch)
            total = res.get("total") if isinstance(res, dict) else None
            offset += len(batch)
            if not batch or len(batch) < page or (total is not None and offset >= total):
                return rows


class RestClient:
    """Read-only REST access. Verifiers use this so they never share a code path with the agent."""

    def __init__(self, session: Session):
        self.session = session

    def _get(self, path, params=None):
        url = f"{self.session.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, payload = self.session.with_reauth(lambda: _http(url, token=self.session.token))
        if status != 200:
            raise McpError(status, f"GET {path} -> HTTP {status}", payload)
        return payload

    def get(self, entity: str, record_id: str) -> dict:
        return self._get(f"/api/{entity}/{urllib.parse.quote(record_id)}")

    def list(self, entity: str, **params) -> list[dict]:
        rows, offset = [], 0
        while True:
            payload = self._get(f"/api/{entity}", {"limit": 200, "offset": offset, **params})
            batch = payload.get("data", [])
            rows.extend(batch)
            offset += len(batch)
            if not batch or offset >= payload.get("total", 0):
                return rows

    def raw(self, path: str, **params):
        return self._get(path, params or None)
