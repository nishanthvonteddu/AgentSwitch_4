"""Reads are retried through a gateway error; writes never are.

AI-written regression tests (ungraded); the team's hand-written tests live in tests/.

A 502/503/504 on a write may come after the server applied it, so repeating WorkOrder.update or
AgentMemory.create could write twice — a second finding row, or a date moved again over someone else's edit.
These drive the real McpClient over a fake socket (urllib.request.urlopen) and count what reached the server.

Run: python -m pytest tests_ai/test_behaviour_mcp_client.py -q
"""
import http.client
import io
import json
import threading
import urllib.error

import pytest

from prod_agent import mcp_client
from prod_agent.mcp_client import CONNECT_RETRIES, McpClient, McpError, Session


class Server:
    """Answers initialize, then fails every tools/call with `failure` and counts the attempts per tool."""

    def __init__(self, failure):
        self.failure, self.attempts = failure, {}

    def urlopen(self, req, timeout=None):
        body = json.loads(req.data)
        if body["method"] != "tools/call":
            return _Response({"jsonrpc": "2.0", "id": body["id"], "result": {}})
        name = body["params"]["name"]
        self.attempts[name] = self.attempts.get(name, 0) + 1
        raise self.failure(req.full_url)


class _Response(io.BytesIO):
    status = 200

    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())


def _gateway(code):
    return lambda url: urllib.error.HTTPError(url, code, "gateway", {}, io.BytesIO(b"null"))


@pytest.fixture
def client_against(monkeypatch):
    monkeypatch.setattr(mcp_client.time, "sleep", lambda seconds: None)

    def build(failure):
        server = Server(failure)
        monkeypatch.setattr(mcp_client.urllib.request, "urlopen", server.urlopen)
        session = Session.__new__(Session)
        session.base, session.token, session.instance, session.platform = "http://book.invalid", "t", "suryodaya", None
        session._lock = threading.Lock()
        return McpClient(session), server
    return build


READS = ["WorkOrder.list", "WorkOrder.get", "endpoint.manufacturing.finite_schedule",
         "endpoint.manufacturing.check_stock_availability", "endpoint.agent_governance.escalations.assignees"]
WRITES = ["WorkOrder.update", "AgentMemory.create", "AgentSession.create",
          "endpoint.agent_governance.escalations.raise"]


@pytest.mark.parametrize("code", [502, 503, 504])
@pytest.mark.parametrize("tool", READS)
def test_a_read_is_retried_through_a_gateway_error(client_against, tool, code):
    client, server = client_against(_gateway(code))
    with pytest.raises(McpError):
        client.call(tool, {"id": "x"})
    assert server.attempts[tool] == CONNECT_RETRIES + 1


@pytest.mark.parametrize("code", [502, 503, 504])
@pytest.mark.parametrize("tool", WRITES)
def test_a_write_is_sent_once_whatever_the_gateway_says(client_against, tool, code):
    client, server = client_against(_gateway(code))
    with pytest.raises(McpError) as exc:
        client.call(tool, {"id": "x"})
    assert server.attempts[tool] == 1, f"{tool} was repeated after a {code}: it may already have been applied"
    assert exc.value.code == code


@pytest.mark.parametrize("tool", WRITES)
def test_a_write_whose_response_was_cut_off_is_not_repeated(client_against, tool):
    client, server = client_against(lambda url: http.client.IncompleteRead(b"{\"jsonrpc\""))
    with pytest.raises(http.client.IncompleteRead):
        client.call(tool, {"id": "x"})
    assert server.attempts[tool] == 1


def test_a_read_whose_response_was_cut_off_is_read_again(client_against):
    client, server = client_against(lambda url: http.client.IncompleteRead(b"{\"jsonrpc\""))
    with pytest.raises(http.client.IncompleteRead):
        client.call("AgentMemory.list", {"limit": 200})
    assert server.attempts["AgentMemory.list"] == CONNECT_RETRIES + 1


@pytest.mark.parametrize("method,params,read_only", [
    ("initialize", {}, True),
    ("tools/list", {}, True),
    ("tools/call", {"name": "JobCard.list"}, True),
    ("tools/call", {"name": "SalesOrder.get"}, True),
    ("tools/call", {"name": "endpoint.manufacturing.finite_schedule"}, True),
    ("tools/call", {"name": "WorkOrder.update"}, False),
    ("tools/call", {"name": "AgentMemory.create"}, False),
    ("tools/call", {"name": "WorkOrder.cancel.not_started.cancelled"}, False),
    ("tools/call", {"name": "endpoint.agent_governance.escalations.update"}, False),
])
def test_the_classification_each_rpc_gets(method, params, read_only):
    assert McpClient._is_read_only(method, params) is read_only
