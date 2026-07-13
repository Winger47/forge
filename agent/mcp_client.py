import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPClient:
    """Talks to an external MCP server over stdio.

    The server is a separate process we spawn. We speak JSON-RPC to it:
      initialize -> tools/list -> tools/call

    The MCP SDK is async but FORGE's loop is sync, so every public method
    here wraps the async work in asyncio.run() — a short-lived event loop
    per call. Slight overhead; avoids rewriting the agent as async.
    """
    

    def __init__(self, command: str, args: list):
        self.params = StdioServerParameters(command=command, args=args)

    async def _with_session(self, fn):
        """Spawn the server, initialize a session, run fn(session), tear down."""
        async with stdio_client(self.params) as (read, write):
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