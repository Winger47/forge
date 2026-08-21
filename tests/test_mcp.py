# tests/test_mcp.py — MCP transport selection, header expansion, validation (Phase 6).

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import agent as A
from agent.mcp_client import MCPClient, to_openai_schema


def test_build_stdio_client():
    c = A._build_mcp_client({"command": "python", "args": ["-m", "mcp_server_time"]})
    assert isinstance(c, MCPClient)
    assert c.transport == "stdio" and c.command == "python"


def test_build_http_client_expands_header_secret(monkeypatch):
    monkeypatch.setenv("FORGE_TEST_TOKEN", "abc123")
    c = A._build_mcp_client({
        "url": "https://example/mcp/", "transport": "http",
        "headers": {"Authorization": "Bearer $FORGE_TEST_TOKEN"},
    })
    assert c.transport == "http" and c.url == "https://example/mcp/"
    # the token name in config becomes the real token from the environment
    assert c.headers["Authorization"] == "Bearer abc123"


def test_build_sse_is_default_for_url():
    c = A._build_mcp_client({"url": "https://example/mcp"})
    assert c.transport == "sse"


def test_to_openai_schema_wraps_mcp_tool():
    s = to_openai_schema({"name": "get_issue", "description": "read an issue",
                          "schema": {"type": "object", "properties": {}}})
    assert s["type"] == "function"
    assert s["function"]["name"] == "get_issue"


def test_mcp_call_is_schema_validated(monkeypatch):
    monkeypatch.setattr(A, "MCP_TOOLS", {
        "remote_tool": {"server": "s", "tool": {
            "name": "remote_tool", "description": "",
            "schema": {"type": "object",
                       "properties": {"q": {"type": "string"}},
                       "required": ["q"]},
        }},
    })
    ok, err = A.validate_mcp_call("remote_tool", {})          # missing required q
    assert ok is False and "q" in err
    ok, err = A.validate_mcp_call("remote_tool", {"q": "hi"})  # valid
    assert ok is True
