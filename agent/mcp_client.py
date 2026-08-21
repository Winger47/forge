import asyncio
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPClient:
    """Talks to an external MCP server over a swappable TRANSPORT.

    The tool-calling logic (initialize -> tools/list -> tools/call) is identical
    regardless of how the bytes travel; only the connection differs. That's the
    point of transport abstraction — one client, three wires:

        stdio  — spawn a local process, speak over its stdin/stdout
        sse    — connect to a server-sent-events HTTP endpoint
        http   — connect to a streamable-HTTP endpoint

    The MCP SDK is async but FORGE's loop is sync, so every public method wraps
    the async work in asyncio.run() — a short-lived event loop per call.
    """

    def __init__(self, transport: str = "stdio", *, command: str = None,
                 args: list = None, url: str = None, env: dict = None,
                 headers: dict = None):
        self.transport = transport
        self.command = command
        self.args = args or []
        self.url = url
        self.env = env                 # stdio: subprocess environment
        self.headers = headers         # sse/http: HTTP headers (auth lives here)

    @asynccontextmanager
    async def _connect(self):
        """Open the right transport and yield (read, write). Each SDK transport
        is an async context manager; we normalize their differing return shapes
        (streamable-http yields a third element) down to the (read, write) pair
        the session needs."""
        if self.transport == "stdio":
            params = StdioServerParameters(command=self.command, args=self.args,
                                           env=self.env)
            async with stdio_client(params) as (read, write):
                yield read, write
        elif self.transport == "sse":
            from mcp.client.sse import sse_client
            async with sse_client(self.url, headers=self.headers) as (read, write):
                yield read, write
        elif self.transport == "http":
            from mcp.client.streamable_http import streamablehttp_client
            async with streamablehttp_client(self.url, headers=self.headers) as (read, write, *_):
                yield read, write
        else:
            raise ValueError(f"unknown MCP transport '{self.transport}'")

    async def _with_session(self, fn):
        """Connect, initialize a session, run fn(session), tear down."""
        async with self._connect() as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()      # MCP handshake
                return await fn(session)

    def list_tools(self):
        """Ask the server what tools it has."""
        async def _go(session):
            result = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "schema": t.inputSchema,      # JSON Schema for the args
                }
                for t in result.tools
            ]
        return asyncio.run(self._with_session(_go))

    def call_tool(self, name: str, arguments: dict):
        """Run one of the server's tools and return its result as text."""
        async def _go(session):
            result = await session.call_tool(name, arguments)
            parts = []
            for block in result.content:
                parts.append(getattr(block, "text", str(block)))
            return "\n".join(parts)
        return asyncio.run(self._with_session(_go))


def to_openai_schema(mcp_tool: dict) -> dict:
    """Wrap an MCP tool's JSON Schema in the OpenAI function-tool envelope,
    so the model sees MCP tools and local tools as one uniform list."""
    return {
        "type": "function",
        "function": {
            "name": mcp_tool["name"],
            "description": mcp_tool["description"],
            "parameters": mcp_tool["schema"],   # MCP inputSchema IS JSON Schema
        },
    }
