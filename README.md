# FORGE

**An agentic coding CLI built on a self-hosted LLM gateway.**

FORGE takes a natural-language goal and works toward it in a loop — *think → act (use a tool) → observe → repeat* — until the task is done. Every model call is routed through a self-built **LLM gateway**: a separate HTTP service handling caching, failover, rate limiting, and streaming.

The agent is the *consumer*; the gateway is the *substrate*. Two processes, one contract (OpenAI-compatible HTTP). The agent's loop never changed as the gateway grew from a passthrough into a caching, rate-limited, streaming service beneath it.

---

## Highlights

- **Real token streaming** — responses stream character-by-character, including reassembly of tool calls that arrive as fragments across chunks.
- **Layered caching** — exact-match (Redis hash) in front, semantic (embeddings + cosine similarity) behind. Rephrased questions hit the cache; the expensive embedding only runs on an exact miss.
- **MCP client** — connects to external tool servers declared in config. Tools are discovered at runtime, merged into the local registry, and routed to whichever server owns them.
- **Permission model** — dangerous tools require approval; foreign MCP tools are default-deny unless explicitly allowlisted; a denial holds for the whole run regardless of how the model varies its arguments.
- **Context compaction** — long conversations are summarized rather than growing until they blow the context window.
- **Extensible tools** — one decorated function adds a tool; its JSON schema is generated from the signature and docstring.

---

## Architecture

```
  You type a goal
        |
        v
  +----------------------------------------------+        +----------------------+
  |  AGENT  (agent/)                              |  MCP   |  fetch server        |
  |  loop: think -> act -> observe                | <----> |  time server         |
  |  8 local tools + discovered MCP tools         | stdio  |  (separate processes)|
  |  streaming . memory . compaction . approval   |        +----------------------+
  +---------------------+------------------------+
                        |  HTTP (OpenAI-compatible)
                        |  POST /v1/chat/completions
                        v
  +----------------------------------------------+
  |  GATEWAY  (gateway/)                          |
  |  0. rate limit     (token bucket, Redis Lua)  |
  |  1. streaming?     -> SSE passthrough, no cache|
  |  2. exact cache    (Redis, hashed body)       |
  |  3. semantic cache (embeddings + cosine)      |
  |  4. failover       (model chain)              |
  +---------------------+------------------------+
                        v
                    Groq (OpenAI-compatible API)
```

---

## The gateway

**Rate limiting.** A token bucket per client, implemented as a **Redis Lua script**. The refill-check-decrement is a read-modify-write; done as separate commands it races under concurrency and two requests consume the same last token. Lua executes atomically inside Redis, so the limit actually holds. The arithmetic is factored into a pure Python function so it's unit tested without Redis. Fails *open* — if Redis is down, requests are allowed rather than blocked.

**Caching, in two tiers.** The exact cache hashes the request body (`sort_keys=True` for deterministic serialization) and does one Redis lookup — cheap, and it short-circuits before the embedder ever runs. Only on an exact miss does the semantic tier embed the prompt and compare it against stored vectors by cosine similarity above a threshold, catching rephrasings that exact matching misses.

**Cache control.** `Cache-Control: no-cache` bypasses the read path while still refreshing the stored entry. `POST /v1/cache/invalidate` removes a poisoned entry from both tiers.

**Streaming.** Requests with `stream=True` take a separate SSE path that forwards provider chunks straight through and skips caching entirely.

**Failover.** On a real miss, the gateway walks a model chain until one succeeds.

---

## The agent

**Tool registry.** A `@tool()` decorator registers a function and generates its JSON schema from the signature and type hints, with the docstring as the description. Adding a tool is one decorated block — no schema written by hand, no registry edited.

Eight local tools: `read_file`, `write_file`, `run_shell`, `list_files`, `search_files`, `edit_file`, `calculate`, `current_time`. `calculate` uses AST parsing with an operator allowlist rather than `eval`, which on model-generated input would be a remote-code-execution hole.

**Streaming with tool-call reassembly.** Text chunks display immediately, but tool calls arrive as *fragments* — the name split across chunks, the arguments as partial JSON. These are accumulated by call index and stitched back together before execution, since a partial tool call is unparseable and unrunnable.

**Multi-turn memory and compaction.** The conversation is owned by the session and passed into the loop, so it survives across goals. Past a threshold, older turns are summarized into a single message while the system prompt and recent turns stay verbatim — the context window treated as a finite cache with an eviction policy.

**Approval gate.** Tools with side effects require confirmation. `edit_file` shows a colored unified diff of the exact change before you approve it. A denial is recorded **per tool for the whole run**, not per call.

**Loop termination.** If the model requests the same call twice, the loop re-invokes it with an *empty* tool list, making a tool call physically impossible and forcing a text answer.

---

## MCP (external tools)

FORGE speaks the Model Context Protocol as a client. Servers are declared in `forge.toml`:

```toml
[mcp.fetch]
command = "python"
args = ["-m", "mcp_server_fetch"]
safe_tools = ["fetch"]     # reviewed as read-only; anything unlisted is gated

[mcp.time]
command = "python"
args = ["-m", "mcp_server_time"]
safe_tools = ["get_current_time", "convert_time"]
```

At startup FORGE spawns each server as a subprocess, speaks JSON-RPC over stdio, asks each what tools it has (`tools/list`), and merges the results into one registry that remembers which server owns each tool. The model sees a single flat list and can't tell local tools from foreign ones.

**Adding an entire external toolset is four lines of config. No Python changes.**

Collisions are handled explicitly and loudly: a foreign tool that shadows a local one is skipped with a warning (local tools are code you wrote; untrusted code never silently replaces trusted code), and between two servers the first to claim a name keeps it.

A server that fails to start is skipped — an external dependency dying costs a capability, not the agent.

---

## Quickstart

**Prerequisites**

```bash
brew install redis
brew services start redis
redis-cli ping        # PONG
```

**Install**

```bash
python3 -m venv venv          # Python 3.10+ required (MCP SDK)
source venv/bin/activate
pip install -e .
pip install mcp-server-fetch mcp-server-time    # optional MCP servers
```

**Configure**

```bash
echo "GROQ_API_KEY=your_key_here" > .env        # free key at console.groq.com
```

**Run** — two terminals:

```bash
uvicorn gateway.main:app --port 8000     # terminal 1
forge                                    # terminal 2
```

```
goal> read agent/agent.py and tell me what it does
goal> fetch https://example.com and summarize it
goal> /tools
goal> /exit
```

Watch both terminals — the gateway logs cache hits, misses, streaming, and failover as calls flow through.

**Tests**

```bash
pytest tests/ -v      # 9 tests, no network required
```

---

## Project structure

```
forge/
|-- agent/
|   |-- agent.py        # loop, clients, streaming, compaction, approval, rendering
|   |-- tools.py        # @tool registry + 8 local tools
|   \-- mcp_client.py   # MCP client: stdio transport, discovery, invocation
|-- gateway/
|   |-- main.py         # rate limit -> streaming -> cache -> semantic -> failover
|   \-- rate_limit.py   # token bucket (pure logic + Redis Lua script)
|-- tests/
|-- forge.toml          # MCP server declarations
\-- pyproject.toml      # packaging + `forge` entry point
```

---

## Design decisions

**Streaming and caching are incompatible.** A cache stores a complete response; a stream is a flow of chunks with no single response to store. Rather than force them together, streaming gets its own SSE path that bypasses the cache entirely. Two request types, two code paths.

**Not everything should be cached.** Context-dependent generations — forced final answers, conversation summaries — must bypass the cache. Their prompts are near-identical across completely different conversations, so the semantic cache matched them and served a file listing as the answer to a math question. The cache key didn't capture what actually determined the answer.

**Guards live in code, not prompts.** A determined model ignores "don't retry" instructions — verified across three prompt rewrites. Loop termination works by removing the *ability* to call a tool, not by asking nicely.

**Denials are per-action, not per-call.** The repeat guard originally keyed on name + arguments. When a denial came back, the model retried with one extra argument — a different signature, so it slipped through and prompted again. Not malice: to a model, "denied" looks like any other failed call, and varying parameters is a reasonable retry. Denials now key on the tool name and hold for the run.

**Foreign tools are default-deny, but not everything is gated.** MCP tools carry no danger classification, so unknown provenance means unknown risk. But gating *every* MCP tool would be worse than useless — approval fatigue trains you to rubber-stamp, and a gate you always approve isn't a gate. Hence an explicit per-server allowlist.

**Local tools beat a shell tool.** `run_shell` could technically do everything — `curl`, `ls`, `cat`. But its capability set is *every program on the machine*, so it must be gated on every use. A narrow tool has a bounded blast radius, can safely skip the gate, and returns structured output instead of raw text the model has to parse. The shell stays as an escape hatch for the long tail.

---

## Known limitations

- **The semantic store is in-memory and unbounded.** Vectors live in a Python list inside the gateway process — lost on restart, not shared across replicas, no eviction. Fine at this scale; the fix is Redis vector search or pgvector once the O(n) scan or persistence starts to matter.
- **The MCP client re-spawns a server per call.** Bridging a long-lived async session into a synchronous agent loop is awkward, so each call spawns, uses, and tears down. Costs process startup per tool call; the clean fix is a persistent session, which really wants an async core.
- **The agent core is synchronous.** MCP's SDK is async, bridged with `asyncio.run()` per call. A deliberate deferral, not an oversight.
- **`run_shell` executes model-generated strings** with the user's permissions. The approval gate is a *control*, not a *sandbox* — real isolation would mean containerizing tool execution.
- **No CI.** Tests are green but not enforced on push.

---

## Status

A learning build that works. Every feature was built, broken, debugged, and verified running — not scaffolded. The gateway and agent are separate processes; the agent's loop is unchanged from when the gateway was a bare passthrough.