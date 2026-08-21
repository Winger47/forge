# FORGE — Master Build Plan (all phases)

The complete end-to-end build, as a sequence of gated prompt-blocks. Pairs
with `FORGE.md` (the spec) and `CLAUDE.md` (the invariants).

---

## HOW TO RUN THIS — read before anything else

1. Put `CLAUDE.md` and `FORGE.md` in the repo root.
2. Execute **one phase per Claude Code campaign**: paste that phase's block in
   **plan mode**, review the plan, approve, implement.
3. Run the phase's **GATE** checklist yourself. Do not proceed until it passes.
4. Commit as `phase-N`. Then start the next phase.

> You *may* paste this whole file at once — but every phase block ends with a
> STOP-AND-VERIFY gate, and broad-and-shallow (building all phases before any
> one is solid) is the failure mode this structure exists to prevent. Honor the
> gates whichever way you run it.

## What changed after reviewing the reference implementation

Folded in from `RivaanRanawat/ai-coding-agent`: **async-native** core, the
**`on-failure`** approval policy, a **`loop_detector`** guard, the **`ToolKind`**
enum, **TOML layered config**, and **session checkpoints**. New invariant:
**provider message-format must not leak past `LLMClient`** (the reference lets
OpenAI's shape into the loop — FORGE must not, or the Phase 3 gateway swap
breaks).

## Global invariants (enforced every phase — see CLAUDE.md)

- Async core. Agent yields `Event`s, never prints. `LLMClient` injected;
  provider shape stops at the client. Goal in via a channel. Tool failure =
  observation, not crash. Every run bounded by `max_iterations` + `max_cost`.
- **Stay in the current phase. Do not scaffold for future phases.**

---

## PHASE 0 — The spine
*(Full standalone version: `FORGE_PHASE_0_PROMPT.md`. Compact form here.)*

**Objective:** smallest working agent — async loop + 3 tools — completes one
real task end to end, then stops.

**Build:** `events.py`, `client.py` (`LLMClient` Protocol + `DirectClient`, one
provider, normalized response), `agent.py` (async loop per FORGE.md §4),
`tools/` (`read_file` auto, `write_file`/`run_shell` confirm), `cli/renderer.py`
(only printer), `main.py`.

**Acceptance task:**
`forge "find every TODO comment in this repo, group them by file, and write a prioritized summary to todos.md"`

**GATE:**
- [ ] Task runs end to end; real `todos.md` produced; agent stops on its own.
- [ ] `--max-iter 1` / tiny `--max-cost` aborts cleanly (status event, no hang).
- [ ] `grep -rn "print(" forge/` → hits ONLY in `cli/renderer.py`.
- [ ] No provider SDK import outside `client.py`.
- [ ] `pytest` + `ruff` green.

**Out of scope:** everything else. If you think you need more than 3 tools and a
loop, re-read the task.

---

## PHASE 1 — Survival + event stream
**Objective:** make the loop robust and the output fully streamable.

**Build:**
- Full event taxonomy: add `confirm_request` + finalize `status/text/tool_call/tool_result/cost`.
- `context/compactor.py` — summarize-then-drop oldest turns near the token
  ceiling; keep a synthetic summary turn so the thread survives.
- `context/loop_detector.py` — hash recent tool-call patterns; abort on
  repetition (stronger than the turn cap alone).
- Seed an eval smoke test: 3 golden tasks + a runner that scores pass/fail.

**GATE:**
- [ ] A long run (forced past the context ceiling) still finishes; compaction
      fired and is visible in logs.
- [ ] A forced repeating action trips `loop_detector` and aborts cleanly.
- [ ] All six event types emit; renderer handles each.
- [ ] The 3-task eval runs and reports scores.

**Out of scope:** gateway, registry, skills, MCP, UI beyond the TUI renderer.

---

## PHASE 2 — Seams, registry, config
**Objective:** create the slots the gateway and extensions plug into.

**Build:**
- Formalize the `LLMClient` seam; prove it with a stub client in tests.
- `tools/registry.py` — register/get/list, JSON-schema **validation** of tool
  calls, allowlist filtering. Introduce the `ToolKind` enum (READ/WRITE/SHELL/
  NETWORK/MEMORY/MCP) on each tool.
- `config/` — TOML config with priority layering: CLI → `.forge/config.toml` →
  system dir → defaults (Pydantic models).
- Hook points wired (no-op handlers): `before_run/after_run/before_tool/after_tool/on_error`.

**GATE:**
- [ ] The agent runs unchanged against a stub `LLMClient` (seam proven).
- [ ] Registry rejects a malformed tool call against its schema.
- [ ] Config loads with correct override precedence (test all four layers).
- [ ] A `before_tool` hook fires and can veto a call.

**Out of scope:** the gateway implementation itself, real hooks logic.

---

## PHASE 3 — Gateway v1 (routing + metering)
**Objective:** stand up the data plane; the agent rides it with zero loop changes.

**Build:**
- Standalone (or in-process — see FORGE.md §15) gateway exposing
  OpenAI-compatible `POST /v1/chat/completions`.
- Provider router (model/provider per request). Cost meter → append-only
  Postgres ledger (per-request tokens/cost/latency/model).
- `GatewayClient` implementing `LLMClient`; swap it in for `DirectClient`.
- **Degrade-to-passthrough:** `GatewayClient` falls back to a direct provider
  call if the gateway is unreachable.

**GATE:**
- [ ] Every model call now flows through the gateway; ledger rows appear in Postgres.
- [ ] The agent code is byte-identical to Phase 2 except the injected client.
- [ ] Kill the gateway mid-run → agent still completes via direct fallback.
- [ ] `curl` the OpenAI-compatible endpoint successfully from outside the agent.

**Out of scope:** caching, rate-limiting, failover beyond passthrough.

---

## PHASE 4 — Gateway depth
**Objective:** the resilience + cost features, on a working base.

**Build:**
- Semantic cache: embed request, similarity threshold, Redis/pgvector store.
- Exact-match cache in front of it.
- Per-key rate limiter (token bucket, Redis).
- Circuit breaker per provider + retry/backoff failover.
- Metrics: p95, error rate, $/model (read from the ledger).

**GATE:**
- [ ] A paraphrased repeat query returns a semantic cache hit (shown in logs).
- [ ] A *different-meaning* query below threshold does NOT hit (poisoning guard).
- [ ] A forced provider outage trips the breaker and fails over.
- [ ] Exceeding a key's bucket returns 429.

**Out of scope:** difficulty-based routing (that's Phase 9), UI for any of this.

---

## PHASE 5 — Agent depth
**Objective:** the control-plane differentiators.

**Build:**
- Skills: markdown files loaded **on demand** into context (domain / workflow /
  tool-usage types).
- Approval-policy model (the mode switch over `ToolKind`): `on-request` /
  `on-failure` / `auto` / `never` / `yolo`, plus always-on dangerous-command
  detection + path-escape checks.
- Subagents: parent spawns a scoped child with isolated context; only the
  result returns. Start with `codebase-investigator`, `code-reviewer`.
- `persistence.py` — session checkpoints: save/restore/list.

**GATE:**
- [ ] A skill loads on demand and demonstrably changes behavior.
- [ ] Each approval mode behaves per the table (verify `on-failure` specifically).
- [ ] A subagent's verbose exploration stays out of the parent transcript;
      only its answer returns.
- [ ] Checkpoint save → restart → restore round-trips a session.

**Out of scope:** planner/reflection (Phase 9), MCP, connectors.

---

## PHASE 6 — MCP client + connectors
**Objective:** consume external tools; recognizable interop signal.

**Read this first — connectors are MCP, not hand-built clients.** A connector is
an integration with a third-party service. Maintaining auth, API versioning, and
rate limits per service is a burden MCP exists to remove. So "add a connector"
means **connect a maintained MCP server**, not write an API client. Build the
client machinery ONCE; the entire ecosystem below becomes available by config.
Hand-build a connector only if no server exists — and then build it AS an MCP
server so it plugs into the same machinery.

**Connector catalogue (the runtime menu — do NOT pre-build these):**
GitHub*, GitLab/Bitbucket, Slack*, Linear/Jira*, Notion/Confluence, Google
Drive, Sentry, Datadog/Grafana, Postgres/MySQL (read-only), Stripe, AWS/GCP
(prefer CLI via `run_shell`), Browser/Playwright, Gmail, Figma, PagerDuty.
(`*` = natural first picks for a coding agent. Filesystem servers are REDUNDANT
— local FS tools already cover that.)

**Build:**
- `tools/mcp/` — MCP client manager: connect via stdio + HTTP/SSE, auto-discover
  and register server tools into the registry alongside local tools, namespaced
  to avoid collisions with local tool names.
- Connection config in TOML (server name, transport, command/url, args).
- Connect EXACTLY ONE server with real capability you lack locally — GitHub
  first choice (read issue / open PR). Prove the round-trip, then stop.

**GATE:**
- [ ] The agent discovers and successfully calls a tool from one real external
      MCP server.
- [ ] MCP tools appear in the registry beside local tools, schema-validated,
      namespaced.
- [ ] The GitHub server performs one real action (issue read or PR open).
- [ ] No connected server merely duplicates an existing local tool (no FS server).

**Out of scope:** building your own MCP *server*; connecting more than one server;
hand-writing any service API client. The catalogue is a menu for later, not a
build list.

---

## PHASE 7 — Surfaces (TUI → Chat → Dashboard)
**Objective:** a face for both planes, all on the one event stream.

**Build:**
- TUI (Rich/Textual): streamed text, tool-call panels, live cost, y/n confirms,
  slash commands (`/help /config /tools /mcp /stats /save /resume`).
- Chat UI (browser): same conversation as bubbles + tool cards, streaming the
  SAME agent over SSE/WS. (Needs the Phase 8 server if not already built —
  build a thin server stub here or pull Phase 8 forward.)
- Dashboard: $/model over time, cache-hit %, p95, request log — reads Postgres.

**GATE:**
- [ ] TUI renders all six event types; confirm round-trip works.
- [ ] Chat UI drives the **same** `agent.run` with **zero** agent changes.
- [ ] Dashboard shows live metrics from the ledger.
- [ ] The print-discipline grep STILL passes (renderers only).

**Out of scope:** auth, multi-tenant concerns (Phase 8).

---

## PHASE 8 — Server (agent-as-a-service)
**Objective:** the deployment surface, only now that something needs it.

**Build:**
- Wrap `agent.run` in a service: session state per connection, SSE/WebSocket
  event fan-out, the goal/confirm input channel over the wire, basic auth,
  concurrency.

**GATE:**
- [ ] 2+ concurrent sessions run with fully isolated state.
- [ ] A browser client streams events live and can send confirms back.
- [ ] Reconnect resumes a session.
- [ ] The agent core is unchanged from Phase 7 — the server only wraps it.

**Out of scope:** horizontal scaling, queues — note as `TODO(post-v1)`.

---

## PHASE 9 — Eval + LLM-engineering extensions (the differentiator)
**Objective:** prove it works, and add the depth recruiters don't see in clones.

**Build (eval is mandatory; pick extensions as a menu, not a checklist):**
- **Eval harness** — grow the Phase-1 smoke test into a golden-task suite +
  regression runner (wire it into CI to block regressions) + LLM-as-judge for
  fuzzy outputs. Capture via the `after_run` hook.
- **Tracing** — per-request span (model/tokens/latency/cost/tool-path/prompt
  version), replayable; feed the dashboard.
- **Structured-output repair** — on malformed tool-call JSON, feed the
  validation error back and retry.
- *Then pick from:* RAG-as-a-tool · planner/reflection · guardrails (PII +
  injection on tool output) · difficulty-based model routing.

**GATE:**
- [ ] The eval suite runs in CI and a deliberately broken change fails it.
- [ ] Tracing shows complete per-request spans.
- [ ] A forced malformed tool-call is recovered by repair-retry.
- [ ] One chosen Tier-2 extension works end to end.

---

## APPENDIX — Catalogues (reference menus, NOT build lists)

These are the search spaces you draw from as phases land. **Building the
*framework* (Tool interface + registry + ToolKind approval; the skill loader;
the MCP client) is the work. Once those exist, each item below is plug-in.**
Do not pre-build the catalogues. Build the framework; add items reactively.

### A. Tools

Priority: `[0]` Phase 0 · `[C]` core, by Phase 5 · `[X]` extension · `[M]` better as an MCP server.

| Tool | Kind | Pri |
|---|---|---|
| read_file | READ | 0 |
| write_file | WRITE | 0 |
| run_shell | SHELL | 0 |
| edit_file (surgical + diff) | WRITE | C |
| list_dir / glob | READ | C |
| search_code / grep | READ | C |
| http_request | NETWORK | C |
| web_search | NETWORK | C |
| web_fetch | NETWORK | C |
| memory (key-value) | MEMORY | C |
| todo (self task-list) | MEMORY | C |
| spawn_subagent | meta | C |
| load_skill | meta | C |
| read_many_files / file_stat / tree | READ | X |
| apply_patch (multi-file diff) | WRITE | X |
| create_dir / move / delete (delete=dangerous) | WRITE | X |
| run_python (sandboxed) | SHELL | X |
| run_tests / run_linter / format | SHELL | X |
| package_install (dangerous) | SHELL | X |
| download_file | NETWORK | X |
| query_kb (RAG) / embed_text | RETRIEVAL | X |
| query_db (read) | DATA | M |
| read_pdf / read_docx | READ | M |
| git_status/diff/log/commit/branch | SHELL | X |
| ask_user / finish | meta | X |

**Actually hand-build (~12):** the `[0]` + `[C]` rows. Everything `[X]` SHELL is
`run_shell` + output parsing; everything `[M]`/`[X] DATA` is an MCP server. Do
not multiply tools that are `run_shell` in a costume.

### B. Skills

A skill = a recurring instruction extracted to markdown, loaded **on demand**.
You cannot author the complete set up front — a skill earns existence only after
the agent needs the same correction ~3 times. **Ship 2–3 seeds; grow by
extraction.** Types: `D` domain · `W` workflow/format · `T` tool-usage · `G` guard.

| Skill | Type | Seed? |
|---|---|---|
| code-review.md | D | seed |
| git-commit.md | W | seed |
| using-shell-safely.md | T | seed |
| writing-tests.md / debugging.md / refactoring.md | D | grow |
| reading-a-codebase.md / api-design.md | D | grow |
| sql-and-migrations.md / security-review.md | D | grow |
| performance-profiling.md | D | grow |
| pr-description.md / changelog.md / docstrings.md | W | grow |
| readme.md / bug-report.md / adr.md / release-notes.md | W | grow |
| using-edit-file.md / using-search.md / using-git.md | T | grow |
| using-mcp-tools.md / using-subagents.md | T | grow |
| when-to-ask.md / cost-awareness.md / stop-conditions.md | G | grow |
| untrusted-input.md (injection guard as a skill) | G | grow |

**Build the loader + skill-metadata format (crisp one-line trigger per skill).
Seed 3. Add one only when you watch a mistake recur.** A repo of 27 guessed
skills with an agent that has run twice is the skills version of the over-spec
stall.

### C. Connectors (all via MCP — see Phase 6)

Connect maintained MCP servers; do not hand-write API clients. `*` = first picks.

| Connector | Category |
|---|---|
| GitHub* | code host |
| GitLab / Bitbucket | code host |
| Slack* | comms |
| Linear / Jira* | issue tracking |
| Notion / Confluence | docs |
| Google Drive / Gmail | docs / comms |
| Sentry* | observability (errors → fixes) |
| Datadog / Grafana / PagerDuty | observability / on-call |
| Postgres / MySQL (read-only) | database |
| Stripe | payments |
| AWS / GCP (prefer CLI via run_shell) | cloud |
| Browser / Playwright | automation |
| Figma | design |

**Connect ONE (GitHub) in Phase 6. The rest is a runtime menu.** A filesystem
MCP server is redundant — local FS tools already cover it.

---

## The discipline that decides whether this ships

This document is a sequence, not a pile. Phases 0–3 (~22% of the effort) hold
the spine, the seams, and the first working agent-on-gateway — and ~60–70% of
the learning. Everything after is breadth on a base that already runs.

**Do not move to phase N+1 until phase N's gate is green.** That single rule is
the difference between this and the four specs that came before it.



# FORGE — Specification & Learning Curriculum

FORGE is an agentic CLI coding assistant (Claude Code / Aider category) built on a
self-made LLM Gateway. It is built **to learn** — the product is real, but the
primary deliverable is the set of concepts each phase forces into your hands.

This document owns the **why, what, and what-you-learn**. Its sibling,
`FORGE_BUILD_PLAN.md`, owns the **how and the gates**. `CLAUDE.md` owns the
**invariants Claude Code enforces during review**. When they disagree, this file
defines intent; the build plan defines acceptance.

---

## 0. THE LEARNING CONTRACT (read before anything else)

This is a curriculum, not a roadmap. Three rules make that distinction real
instead of decorative:

1. **Every phase ships running code.** A phase is complete only when its
   artifact runs and its gate is green. A phase you have *specced but not built*
   is a failure, not progress. This is the single rule that separates FORGE from
   a design document.
2. **Concepts are just-in-time, not up-front.** You do not need to understand
   PageRank before Phase 5.5 or circuit breakers before Phase 4. Learn the
   concept when the phase demands it. The "Concepts you'll learn" block per phase
   is your reading list *for that phase*, not a prerequisite wall.
3. **Stay in the current phase.** Do not scaffold for future phases. The
   temptation to "just add the hook for later" is how the spine rots before it's
   solid. Build the phase in front of you; trust the seams to hold.

> The honest risk for this project is not too many phases. It is planning all of
> them and building three. If you feel more energy writing Phase 18's spec than
> building Phase 3, that is the tell. Close the doc. Build.

---

## 1. WHAT FORGE IS

Give it a goal. It loops — model call → tool calls → observations → repeat —
until the task is done or a bound trips. It reads and writes your files, runs
shell commands, calls external tools over MCP, and rides a gateway that meters
cost and adds resilience. It presents through a TUI, a browser chat, and a
metrics dashboard — all subscribing to one event stream.

**Category peers:** Claude Code, Aider, Cursor's agent, OpenCode.
**What makes FORGE a *learning* build:** you build the gateway, the eval harness,
the retrieval layer, and the multi-agent orchestration yourself, rather than
importing a framework. The framework *is* the syllabus.

---

## 2. THE THREE-PLANE ARCHITECTURE

Every component belongs to exactly one plane. This is the spatial model that
keeps the system honest as it grows.

```
┌─────────────────────────────────────────────────────────────┐
│ PRESENTATION PLANE   — owns ALL I/O                          │
│   renderer · approver · TUI · Chat UI · Dashboard            │
│   "the only place that talks to a human or a screen"         │
└───────────────▲───────────────────────────┬─────────────────┘
                │ Events (out)              │ Decisions (in)
┌───────────────┴───────────────────────────▼─────────────────┐
│ CONTROL PLANE        — the agent core, NO I/O                │
│   agent loop · tools · registry · skills · subagents         │
│   planner · guardrails · compaction · loop detection         │
│   "reasons and acts; yields Events; never prints"            │
└───────────────▲───────────────────────────┬─────────────────┘
                │ NormalizedResponse        │ create(messages…)
┌───────────────┴───────────────────────────▼─────────────────┐
│ DATA PLANE           — the LLM Gateway                       │
│   LLMClient · router · ledger · cache · rate-limit · breaker │
│   "provider shape stops HERE; the loop never sees raw JSON"  │
└─────────────────────────────────────────────────────────────┘
```

**Why planes and not layers?** Layers imply a strict call stack. Planes imply
*independent concerns that communicate through defined seams*. The agent (Control)
must be swappable onto a real gateway (Data) with zero code change, and drivable
by a TUI or a browser (Presentation) with zero code change. That is only possible
if the seams — `LLMClient` and the `Event` stream — are the *only* contact points.

---

## 3. GLOBAL INVARIANTS (enforced every phase)

These do not change. Violating one is a bug regardless of what phase you're in.

| # | Invariant | The concept behind it |
|---|---|---|
| 1 | The agent core does **no I/O**. It yields `Event`s. The presentation layer (renderer + approver) owns every read/print. | Dependency inversion; separation of policy from mechanism |
| 2 | The agent depends on the `LLMClient` **Protocol**, never a provider SDK. | Interface segregation; the D in SOLID |
| 3 | **Provider message-shape stops at the client.** The loop never sees provider JSON. | Anti-corruption layer (DDD) |
| 4 | The goal enters via a **channel/iterator**, never `input()` inside the loop. | Inversion of control |
| 5 | A tool failure is **data, not a crash** — caught, returned as an observation. | Errors-as-values; the model self-corrects |
| 6 | Every run is bounded by `max_iterations` **and** `max_cost`. | Liveness + resource bounding |
| 7 | **Extension contracts are public and versioned.** The `Tool` interface and skill format are consumed by user code — additive changes only. | Open/Closed principle at the system boundary |

---

## 4. GOVERNING MENTAL MODELS

Four models recur across phases. Internalize these and most design decisions
answer themselves.

**M1 — Framework, not catalogue.** The work is the *plugin host* (registry +
loader + MCP client). The tools/skills/connectors themselves are *data* that plug
in. If adding a tool means editing `agent.py`, the host isn't a host — it's a
hardcode.

**M2 — A phase boundary is a seam, not a wall.** You don't "not build" a future
capability — you *expose where it will attach* without implementing its policy.
The Phase-1 confirmation stub is the canonical example: the return-path *shape*
exists in Phase 1; the policy fills in at Phase 5, changing no control flow.

**M3 — Index type is a function of process lifetime, not sophistication.** A
long-running editor can afford a background embedding index; an ephemeral CLI
cannot. Reach for the cheapest retrieval that clears the bar: agentic grep →
structural repo-map → semantic embeddings, in that order. (See the Retrieval
Ladder, §6.)

**M4 — One event per concept, discriminated by a field.** "The run ended" is one
concept with many reasons — model it as `TerminalEvent(reason)`, not five event
types. Splitting a concept across types forces every consumer to reassemble it.

---

## 5. THE CURRICULUM AT A GLANCE

Seven movements, ~21 phases. Phases 0–3 hold ~22% of the effort and ~60–70% of
the learning — they are the spine. Everything after is breadth on a base that
runs.

| Movement | Phases | Theme | Primary domain learned |
|---|---|---|---|
| I — Agent Core | 0–2 | the loop, survival, the plugin host | Agent engineering; SOLID; plugin architecture |
| II — Data Plane | 3–4 | gateway: routing, resilience, cost | Distributed systems; service design |
| III — Agent Intelligence | 5–6 | skills, approval, subagents, retrieval, MCP | Context engineering; protocols |
| IV — Surfaces & Serving | 7–8 | TUI, dashboard, server, chat | Event-driven UI; wire protocols |
| V — Correctness | 9 | eval, tracing, repair | Testing LLMs; observability |
| VI — Advanced AI Eng | 10–13 | RAG, reasoning, guardrails, routing | AI engineering depth |
| VII — Production | 14–20 | metrics, orchestration, state, security, perf, deploy, model layer | Production ops; MLOps |

---

## 6. THE RETRIEVAL LADDER (governs Phases 5.5, 10)

"Index the codebase like other tools" collapses three distinct strategies. Build
up the ladder; stop at the first rung that's good enough.

| Rung | Strategy | Builds | Invalidation | CLI-viable? |
|---|---|---|---|---|
| Floor | Agentic search (`grep`/`glob`) | nothing | n/a | yes — free, exact |
| Middle | Structural repo-map (tree-sitter + PageRank) | symbol graph, mtime-cached | filesystem mtime (free) | yes — the sweet spot |
| Ceiling | Semantic index (embeddings/RAG) | vector DB | content-hash / Merkle diff (you build it) | only at 1000+ files |

**Decision:** grep is the floor (Phase 5). The repo-map is the highest
value-per-LOC upgrade for a CLI and is CLI-native (Phase 5.5). Embeddings are the
ceiling — gated behind a repo-size threshold (Phase 10), because their marginal
accuracy gain is real but modest and scales with repo size.

---

# THE PHASES

Each phase carries: **Spine** (the promise), **Scope in/out**, **Features**,
**Concepts you'll learn**, and the **SDE-3 lens** (anti-pattern + tradeoff).
Acceptance gates live in `FORGE_BUILD_PLAN.md`.

---

## MOVEMENT I — THE AGENT CORE

### Phase 0 — The Spine  ✅ built
**Spine:** give it a goal; it loops and completes one real task, then stops.
**Plane:** Control + Data (minimal).

**Scope (in):** async agent loop; `LLMClient` Protocol + one direct client;
normalized response; 3 tools (read/write/shell); renderer; `main.py`.
**Scope (out):** everything else. If you think you need >3 tools, re-read the task.

**Features & functionality:**
- `agent.run(goal)` async iterator yielding `Event`s.
- Tool dispatch; tool failure captured as observation.
- Provider response normalized to `NormalizedResponse` inside the client.

**Concepts you'll learn:**
- **The ReAct pattern** — reason → act → observe as the atomic agent cycle. Why
  it exists: LLMs can't act on the world; the loop gives them hands and eyes.
- **Dependency inversion via Protocol** — the loop names *what* it needs
  (`create()`), not *who* provides it.
- **Async iterators / generators** — `yield` as a cooperative control-transfer
  point; the driver pulls events.
- **The normalization boundary (anti-corruption layer)** — provider JSON is
  translated once, at the edge, so its shape never contaminates the core.

**SDE-3 lens:** The anti-pattern is letting OpenAI's `tool_calls` shape leak into
the loop "just for now" — it makes the Phase-3 gateway swap a rewrite. Tradeoff:
normalization costs you a translation layer per provider, and buys you a loop
that outlives any single provider.

---

### Phase 1 — Survival + Event Stream  ✅ built
**Spine:** the loop is bounded, observable, and won't spiral.
**Plane:** Control + Presentation (taxonomy).

**Scope (in):** full event taxonomy; context compaction; loop detection; cost
metering; eval smoke test.
**Scope (out):** gateway, registry, skills, MCP, UI beyond the renderer.

**Features & functionality:**
- Events: `status/text/tool_call/tool_result/cost/confirm_request/terminal`.
- `confirm_request` emits **one-way** with a stubbed decision point
  (`_resolve_confirmation` returns a constant) — Model M2 in action.
- Compaction: summarize-then-drop oldest turns near the token ceiling; keep
  tool-call/result **pairs** intact; preserve the goal.
- Loop detection: cycle detection over a `(tool, args, result)` fingerprint.

**Concepts you'll learn:**
- **Discriminated unions + exhaustiveness** — the `Event` union + `match` +
  `assert_never`; the type checker proves you handled every case.
- **Control theory over a loop** — bounds and detectors as governors preventing
  runaway. Why: an autonomous loop with a paying meter is a liability without them.
- **Cycle detection** — fingerprinting + period detection; the *result* is in the
  fingerprint so genuine progress isn't flagged.
- **Token budgeting & context compaction** — the context window as a finite
  resource with an eviction policy (this is caching theory in disguise).

**SDE-3 lens:** Anti-pattern is compacting mid-pair and orphaning a `tool_result`
— the provider API rejects the whole request. Tradeoff: `input_tokens` as your
gauge lags by one turn; you absorb it with headroom (compact at 80%).

---

### Phase 2 — The Plugin Host (registry, config, hooks) + user tools
**Spine:** the seams exist; I can add my own tools without touching the core.
**Plane:** Control.

**Scope (in):** `LLMClient` seam proven with a stub; tool registry as a **plugin
host**; JSON-schema validation; `ToolKind` enum; TOML layered config; no-op hooks;
user-tool discovery.
**Scope (out):** the gateway itself; approval *modes* (P5); sandboxing beyond
ToolKind tagging.

**Features & functionality:**
- Registry: register/get/list; schema-validate every tool call; allowlist filter.
- Public `Tool` contract (name, description, schema, `ToolKind`, async `run`).
- User tools loaded from `~/.forge/tools/` (and/or entry-points), namespaced.
- Fail-load isolation: a broken user tool is quarantined + reported, never fatal.
- Config precedence: CLI → `.forge/config.toml` → system → defaults (Pydantic).
- Hook points wired as no-ops: `before/after_run`, `before/after_tool`, `on_error`.

**Concepts you'll learn:**
- **Open/Closed principle at a boundary** — the host is closed for modification,
  open for extension. Adding capability = adding data, not editing code.
- **Plugin architecture** — discovery, registration, namespacing, load-time
  isolation. Real-world analogy: VSCode extensions, pytest plugins.
- **Schema validation as a contract** — JSON Schema as the machine-checkable
  boundary between untrusted (model/user) input and your executor.
- **Config precedence / layered configuration** — the override chain as a
  well-defined merge, not ad-hoc `if` statements.
- **The hook/middleware pattern** — lifecycle interception points; the same idea
  as Express middleware or Django signals.
- **Trust boundaries** — a user tool is code you didn't write in your process;
  classify its danger (`ToolKind`) at the door.

**SDE-3 lens:** Anti-pattern is a registry where a user tool named `read_file`
silently shadows the builtin — namespace or reject, loudly. Tradeoff: fail-load
isolation costs you a try/except per plugin and buys you a startup that survives
one bad extension. This phase is where FORGE stops being a script.

---

## MOVEMENT II — THE DATA PLANE (GATEWAY)

### Phase 3 — Gateway v1 (routing + metering)
**Spine:** the client is a real service; the agent rides it unchanged.
**Plane:** Data.

**Scope (in):** OpenAI-compatible `POST /v1/chat/completions`; provider router;
append-only Postgres ledger; `GatewayClient` implementing `LLMClient`;
degrade-to-passthrough fallback.
**Scope (out):** caching, rate-limiting, failover beyond passthrough (P4).

**Features & functionality:**
- Router selects provider/model per request.
- Ledger row per request: tokens, cost, latency, model (append-only).
- `GatewayClient` swaps in for the direct client — the loop is byte-identical.
- If the gateway is unreachable, `GatewayClient` falls back to a direct call.

**Concepts you'll learn:**
- **Dependency inversion paying off** — you swap the implementation behind a
  Protocol and the consumer never knows. This is the moment SOLID becomes visceral.
- **API compatibility as a contract** — implementing an existing wire spec
  (OpenAI's) so any client can talk to you; interface as a market standard.
- **Append-only ledgers** — immutable event logs; why financial/audit systems
  never UPDATE. Introduces event-sourcing thinking.
- **Graceful degradation** — degrade-to-passthrough as a fallback strategy; the
  system loses a feature (metering) but not availability.

**SDE-3 lens:** Anti-pattern is the agent knowing it's on a gateway (leaked
abstraction). Verify byte-identical loop code. Tradeoff (CAP preview): the ledger
write is on the request path — do you block on it (consistency) or fire-and-forget
(availability)? Decide and know why.

---

### Phase 4 — Gateway Depth (resilience + cost)
**Spine:** the gateway is resilient and cost-aware under failure and load.
**Plane:** Data.

**Scope (in):** exact-match + semantic cache; per-key rate limiter; circuit
breaker + retry/backoff failover; metrics from the ledger.
**Scope (out):** difficulty routing (P13); UI (P7).

**Features & functionality:**
- Exact cache in front; semantic cache (embed request → similarity threshold →
  vector store) behind it.
- Token-bucket rate limiter per key (Redis).
- Circuit breaker per provider; retry with backoff on transient failure.
- Metrics: p95 latency, error rate, $/model.

**Concepts you'll learn:**
- **Cache invalidation & poisoning** — the semantic cache's threshold is a
  precision/recall dial; too loose and a different-meaning query gets a wrong
  cached answer. "Two hard things in CS" made concrete.
- **Token bucket** — rate limiting as a refilling-bucket state machine; smooths
  bursts vs. a fixed window.
- **Circuit breaker** — closed/open/half-open state machine; stops hammering a
  dead dependency. Analogy: an electrical breaker.
- **Exponential backoff + jitter** — why naive retry causes thundering herds.
- **Embeddings (first contact)** — text → vector; cosine similarity as semantic
  distance.

**SDE-3 lens:** Anti-pattern is a semantic *response* cache for a coding agent —
near-identical prompts often need *different* edits; usually skip it. Tradeoff:
every resilience feature adds a failure mode of its own (a breaker stuck open is
an outage you caused). Instrument each.

---

## MOVEMENT III — AGENT INTELLIGENCE

### Phase 5 — Agent Depth (skills, approval, subagents) + user skills
**Spine:** it's a real coding agent — it has skills, asks before dangerous acts,
and delegates. I can add my own skills.
**Plane:** Control + Presentation (approver).

**Scope (in):** skill loader (on-demand markdown) as a plugin host; **Approver**
replacing the P1 stub; approval-policy modes over `ToolKind`; diff preview;
subagents with context isolation; session checkpoints; user skills.
**Scope (out):** planner/reflection (P11); MCP (P6); RAG (P10); output guardrails
(P12).

**Features & functionality:**
- Skills: markdown loaded on demand; public metadata format; user skills from
  `~/.forge/skills/`. Seed 3; grow by extraction.
- Approval: inject an `Approver` (mirrors `LLMClient`); modes `on-request` /
  `on-failure` / `auto` / `never` / `yolo`; always-on dangerous-command + path-
  escape checks. **User tools inherit this automatically.**
- Diff preview before `write_file`/`edit_file`, routed through the approver.
- Subagents: parent spawns a scoped child; only the result returns.
- `persistence.py`: checkpoint save/restore/list.

**Concepts you'll learn:**
- **Capability injection** — the `Approver` as an injected policy; the seam from
  M2 finally filled. The return-path (Decision *into* the loop) is your first
  bidirectional flow — study it.
- **Context isolation** — a subagent's noisy exploration stays out of the
  parent's transcript; only the distilled answer returns. This is how you scale
  agents without blowing the window.
- **Policy engines** — decoupling "what's allowed" (policy) from "how it runs"
  (mechanism); the approval modes are a tiny policy language.
- **Serialization & checkpointing** — snapshotting mutable session state to disk
  and restoring it; introduces the state/identity problem.

**SDE-3 lens:** Anti-pattern is putting the approver's I/O back "in the renderer"
— it's a distinct presentation capability (Invariant 1 matured). Tradeoff: skills
loaded on demand cost a retrieval decision; loaded always, they cost context. The
on-demand choice is a bet the trigger matcher is good.

---

### Phase 5.5 — Structural Retrieval (the repo-map)
**Spine:** the agent knows the shape of the whole repo without reading every file
— and pulls that shape on demand instead of paying for it every turn.
**Plane:** Control.

**Scope (in):** a structural index over the repo — tree-sitter symbol extraction,
a typed reference graph, personalized-PageRank ranking, SQLite/mtime cache — plus
the **three tools** the agent uses to interrogate it: `explain`, `path`, `query`.
Upgrade `codebase-investigator` to reach for them above a size threshold.
**Scope (out):** embeddings (P10). This is the *middle* rung, not the ceiling.
Community detection (Leiden), multi-language breadth beyond 3 grammars, and the
work-memory overlay (P9) are deliberately deferred.

**Features & functionality:**
- Parse files → extract definitions/references (tags) via tree-sitter. **Three
  languages, not thirty.** Python + TypeScript + one more you actually use.
- Build a **typed** symbol graph — edges are `calls` / `imports` / `inherits` /
  `references`, not a flat adjacency. `[NEW]`
- Every edge carries `confidence: Literal["extracted", "inferred", "ambiguous"]`.
  `extracted` = read out of the AST. `inferred` = resolved by name-matching
  heuristic across files. The agent is told which is which. `[NEW — locked]`
- Rank with personalized PageRank (bias toward chat/mentioned files). Top-degree
  nodes fall out for free as **god nodes** — the hubs everything flows through.
- **Rationale nodes:** `# WHY:` / `# NOTE:` / `# HACK:` comments and ADR/RFC
  citations become first-class nodes linked to the code they explain. ~50 LOC of
  regex; surfaces the one thing grep structurally cannot — intent. `[NEW]`
- **Tool surface — the index is a tool, not context:** `[NEW — locked]`
    - `explain(symbol) -> NodeCard`   — def site, callers, callees, rationale, rank
    - `path(a, b) -> list[Edge]`      — how do these two things connect?
    - `query(question) -> Subgraph`   — scoped, token-budgeted traversal
- Token-budgeted rendering applies **per query response**, not to a map injected
  into every request.
- Cache by file mtime; `--update` re-parses only changed files. A post-commit git
  hook covers the "someone else's commit landed" case that mtime alone misses.
- Every `explain`/`path`/`query` call appends one JSONL line (question, nodes
  returned, token cost, duration) to a query log. 20 LOC. This is the raw dataset
  Phase 9's eval harness would otherwise have to invent. `[NEW]`

**Concepts you'll learn:**
- **AST parsing** — tree-sitter turns source into a syntax tree; language-agnostic
  structure extraction. The same tech LSPs and IDEs use.
- **Graph ranking (PageRank)** — a function referenced by 20 others matters more
  than a private helper called once; centrality as importance. The web-search
  algorithm, applied to code.
- **Typed edges and graph traversal** — `path(A, B)` answers a question class grep
  cannot express *at all*: two symbols three hops apart that share no lexical
  token. This is where a graph stops being a nicer grep and becomes a different
  instrument.
- **Epistemic honesty in an index** — resolution across files is heuristic. An
  index that reports guesses and facts in the same voice will make the model act
  confidently on a guess. Confidence tags are the fix, and they are nearly free at
  build time and painful to retrofit.
- **mtime-based cache invalidation** — the OS gives you the invalidation signal
  for free; contrast with the hard content-hash problem embeddings face.
- **Retrieval as a pull, not a push** — the alternative design (Aider's) injects a
  ranked symbol block into the system prompt every turn. Note what that does to
  Phase 1: a fixed multi-kilotoken tax that the compactor must carry, evict, and
  watch get re-fetched. A queryable index is compaction-friendly; an injected map
  fights the compactor by construction.
- **Token-budgeted rendering** — binary-search the response size to fit the budget.

**SDE-3 lens:**
Three anti-patterns, in order of how often engineers hit them:
1. **Reaching for embeddings here.** The CLI-native tool (Aider) deliberately chose
   the cheaper structural map, and so did the graph-native tool (graphify, which
   states `no embeddings, no vector store` as a design principle). Two independent
   teams, same call. That's not a coincidence — it's the shape of the problem.
2. **Shipping the map as context instead of as a tool.** Cheaper to build, and it
   silently taxes every request in the system forever. The cost lands in a phase
   you already shipped, which is why it's easy to miss.
3. **Flattening confidence.** An `inferred` edge rendered identically to an
   `extracted` one is a lie the model cannot detect.

Tradeoff: the repo-map is symbol-level, not semantic — it finds *what's central*
and *what's connected*, not *what's about topic X*. That's Phase 10's job, and only
if you need it. The gate for building P10 is a query log (above) showing structural
retrieval actually missing.

Scope trap: tree-sitter has ~40 grammars available and adding one is easy. Adding
all of them is a week of work that improves nothing on the repos you use.

Prior art: `Graphify-Labs/graphify` (MIT) — same rung, productized. Read
`ARCHITECTURE.md` / `docs/how-it-works.md`. Steal the confidence tags and the
query surface; skip the 36 grammars, the HTML viz, the Obsidian/Neo4j exports, and
the LLM semantic pass over docs. **Reference, not dependency** — this phase's gate
is "I built a structural index," and `pip install` does not clear it.

**Gate:** on a repo you did not write (pick one ~500 files), `path()` connects two
symbols you know are related but that share no lexical token, and it does so
faster and in fewer tokens than the Phase 5 grep loop finds the same link. Query
log is non-empty. If `path()` cannot beat grep here, the phase failed and the
retrieval ladder is wrong — say so out loud rather than shipping it anyway.

---

### Phase 6 — MCP Client + Connectors
**Spine:** it consumes external tools without me hand-writing API clients.
**Plane:** Control.

**Scope (in):** MCP client manager (stdio + HTTP/SSE); auto-discover + register
server tools into the same registry, namespaced; connect exactly one server
(GitHub).
**Scope (out):** building your own MCP *server*; >1 connector; any hand-written
service client.

**Features & functionality:**
- Transport-abstracted client: stdio and SSE.
- Discovered MCP tools land in the registry beside local + user tools,
  schema-validated identically, namespaced against collisions.
- One real round-trip: read a GitHub issue or open a PR.

**Concepts you'll learn:**
- **Protocol clients** — implementing a spec (MCP) to consume a maintained
  ecosystem instead of N bespoke integrations.
- **Transport abstraction** — the same tool-calling logic over stdio or HTTP/SSE;
  the transport is a swappable detail.
- **Tool federation & namespacing** — merging tool sources without collision; the
  registry (P2) pays off again.

**SDE-3 lens:** Anti-pattern is hand-writing a GitHub API client — that's the
maintenance burden MCP exists to remove. Tradeoff: MCP adds a dependency on
someone else's server uptime and versioning; you trade build cost for operational
coupling.

---

## MOVEMENT IV — SURFACES & SERVING

### Phase 7 — Surfaces: TUI + Dashboard
**Spine:** it has a face — a rich terminal UI and a live metrics dashboard.
**Plane:** Presentation.

**Scope (in):** TUI (Rich/Textual) on the event stream; slash commands; Dashboard
reading Postgres directly.
**Scope (out):** anything needing the wire — Chat UI, auth, sessions (P8).

**Features & functionality:**
- TUI: streamed text, tool-call panels, live cost, confirm round-trip via the
  approver, `/help /config /tools /mcp /stats /save /resume`.
- Dashboard (read-only browser): $/model over time, cache-hit %, p95, request log.

**Concepts you'll learn:**
- **Event-stream subscribers** — the TUI is *a* subscriber to `agent.run`, not the
  owner. Two surfaces, one stream, zero core changes: this is why Invariant 1
  exists.
- **Terminal rendering** — a render loop over an event feed; diffing screen state.
- **Read models (CQRS-lite)** — the dashboard reads a projection (the ledger),
  fully decoupled from the write path.

**SDE-3 lens:** Anti-pattern is the TUI reaching into agent internals for state —
it must consume only events. Tradeoff: the print-discipline grep must still pass;
renderers are the only writers. If the TUI needs data the events don't carry, add
a *field*, don't add a backchannel.

---

### Phase 8 — Server + Chat UI (agent-as-a-service)
**Spine:** it's a service — multiple browser sessions drive the same agent over
the wire.
**Plane:** Presentation + serving.

**Scope (in):** wrap `agent.run` in a service; per-connection session state;
SSE/WebSocket event fan-out; goal/confirm channel over the wire; basic auth;
concurrency; the browser Chat UI.
**Scope (out):** horizontal scaling, queues (`TODO(post-v1)`).

**Features & functionality:**
- Service wraps the *unchanged* agent core; state isolated per connection.
- Chat UI: bubbles + tool cards, streaming the same agent over SSE/WS; confirms
  sent back over the wire.
- Reconnect resumes a session (checkpoints from P5).

**Concepts you'll learn:**
- **Event fan-out** — one producer, many subscribers over SSE/WebSocket; the
  pub/sub pattern on the wire.
- **Stateful connections & session isolation** — per-connection state without
  cross-talk; the concurrency correctness problem.
- **Wire protocols (SSE vs WebSocket)** — one-way stream vs full duplex; picking
  by need (events out = SSE; confirms back = WS or a second channel).
- **Backpressure** — what happens when the client consumes slower than the agent
  produces.

**SDE-3 lens:** Anti-pattern is state bleeding across sessions (a global that
should've been per-connection) — the classic concurrency bug. Tradeoff: "trust
user tools in-process" (fine for a solo CLI) becomes a *security posture* the
moment there's a server. Name it here, or inherit a vulnerability.

---

## MOVEMENT V — CORRECTNESS

### Phase 9 — Eval + Observability Core
**Spine:** it's provably correct under change and observable from the inside.
**Plane:** cross-cutting.

**Scope (in):** golden-task eval suite + regression runner in CI; LLM-as-judge;
per-request tracing spans; structured-output repair; mypy/pyright in CI.
**Scope (out):** the depth extensions (each is its own phase below).

**Features & functionality:**
- Eval grows the P1 smoke test into a suite; CI blocks regressions.
- Tracing: span per request (model/tokens/latency/cost/tool-path/prompt version),
  replayable, feeds the dashboard.
- Repair: on malformed tool-call JSON, feed the validation error back and retry.
- Type gate: `assert_never` exhaustiveness becomes CI-enforced, not runtime-only.

**Concepts you'll learn:**
- **Golden-task / regression testing for non-determinism** — how you test a
  system whose output isn't byte-stable.
- **LLM-as-judge** — using a model to grade fuzzy outputs; its own bias/reliability
  tradeoffs.
- **Distributed tracing (spans)** — the request as a causal tree; the foundation
  of debugging a system you can't step through.
- **Self-healing / repair loops** — feeding validation errors back as observations
  so the model corrects its own malformed output.

**SDE-3 lens:** Anti-pattern is asserting on human-readable strings (grepping
"loop detected") instead of the `TerminalReason` enum — refactor-fragile. This is
why M4 (structured events) was built in P1. Tradeoff: LLM-as-judge is cheap to add
and hard to trust; pair it with a few deterministic golden checks.

---

## MOVEMENT VI — ADVANCED AI ENGINEERING

### Phase 10 — Semantic Retrieval (RAG over code)
**Spine:** it recalls the right code semantically, even in large repos.
**Plane:** Control + Data (index store).

**Scope (in):** `query_kb`/`embed_text`; chunk → embed → vector store; invalidation
driven by the agent's own mutation stream (and/or content hashing); upgrade the
investigator above a size threshold.
**Scope (out):** semantic *response* cache (that's the gateway, P4, and usually an
anti-pattern).

**Concepts you'll learn:**
- **RAG (retrieval-augmented generation)** — retrieve-then-generate; the dominant
  pattern for grounding models in private data.
- **Chunking strategies** — function/class/logical-block chunking vs naive
  line windows; chunk boundaries decide retrieval quality.
- **Vector similarity search** — nearest-neighbor in embedding space; the vector
  DB (pgvector/Turbopuffer-style).
- **The hard invalidation problem** — an embedding is stale the instant you edit
  its chunk; content-hash / Merkle-diff to detect change. Contrast the *free*
  mtime signal of the repo-map — this is why this rung is last.
- **Hybrid retrieval** — semantic + grep beats either alone; nobody drops exact
  search for embeddings.

**SDE-3 lens:** Anti-pattern is an index your own `write_file` silently
staleness-poisons. Tradeoff: the accuracy gain is real but modest and scales with
repo size — for personal-scale repos, Phase 10 may be a *tutorial* (learn how RAG
works) more than a *need*. Build it knowing which it is.

---

### Phase 11 — Reasoning (planner + reflection)
**Spine:** it plans before acting and critiques its own results.
**Plane:** Control.

**Scope (in):** planner (decompose goal → checkable steps, surfaced as events);
reflection loop (evaluate result vs plan, self-correct, bounded).
**Scope (out):** multi-agent teams beyond the P5 subagent primitive (P15).

**Concepts you'll learn:**
- **Task decomposition** — turning a fuzzy goal into an ordered, checkable plan;
  plan-execute-observe.
- **Reflection / critic loops** — the agent grades its own step and retries;
  self-correction as a bounded loop (reuse the P1 iteration discipline so it can't
  spiral).
- **Plan-as-data** — the plan surfaced as events so the UI can show it and the
  eval can check it.

**SDE-3 lens:** Anti-pattern is unbounded reflection — a critic that always finds
one more thing to fix burns budget forever. Bound it with the same governor as the
main loop. Tradeoff: planning adds latency and cost per task for correctness on
hard tasks; make it conditional on task complexity.

---

### Phase 12 — Guardrails (defend inputs and outputs)
**Spine:** it treats tool output and external content as untrusted.
**Plane:** Control.

**Scope (in):** PII detection/redaction on tool output; prompt-injection detection
on untrusted content (fetched pages, MCP results, file contents); hardened path-
escape/dangerous-command checks.
**Scope (out):** full container sandboxing of tool execution (`TODO(post-v1)`).

**Concepts you'll learn:**
- **Prompt injection** — the #1 LLM-security class; untrusted content carrying
  instructions the model obeys. Why the trust boundary (P2) matters.
- **Input/output filtering** — sanitizing what enters and leaves the model context.
- **PII detection/redaction** — pattern + model-based detection; data-governance
  basics.
- **Defense in depth** — layered checks (P5 danger checks + P12 filters) rather
  than one gate.

**SDE-3 lens:** Anti-pattern is trusting MCP/tool output because "it's from a
tool" — a fetched web page is attacker-controlled. Tradeoff: every filter has
false positives that block legitimate work; tune conservatively and log what you
block for eval.

---

### Phase 13 — Adaptive Routing (cost-optimal model selection)
**Spine:** easy work goes to cheap models, hard work to strong ones.
**Plane:** Data (gateway extension).

**Scope (in):** difficulty signal (heuristic → refined against trace/eval data);
router in the gateway selecting model tier per request; routing decisions logged.
**Scope (out):** learned/ML routing models — heuristic + policy is v1.

**Concepts you'll learn:**
- **Routing heuristics** — cheap signals (prompt size, tool depth, retry count) as
  a difficulty proxy.
- **Cost/quality optimization** — the core AI-eng tradeoff; measure it against the
  eval suite so you don't trade cost for silent regression.
- **Multi-armed bandit (intro)** — the explore/exploit framing for routing; a
  door into learned routing later.

**SDE-3 lens:** Depends on P9 tracing/eval to *measure* difficulty and verify no
regression — hence it's late. Anti-pattern is routing by vibes with no A/B against
a fixed-model baseline. Tradeoff: routing saves cost and adds a decision that can
be wrong; log every decision for post-hoc eval.

---

## MOVEMENT VII — PRODUCTION HARDENING (the 20+ stretch)

> These phases turn FORGE from "works on my machine" into "operable service." They
> are where the *distributed-systems and MLOps* learning lives. Build them only
> after Movements I–VI run — but they are legitimate curriculum, not scope-creep,
> given the learning goal.

### Phase 14 — Deep Observability
**Spine:** I can see p95/p99, error budgets, and cost trends in real dashboards.
**Concepts:** OpenTelemetry; RED (Rate/Errors/Duration) & USE (Utilization/
Saturation/Errors) metrics; histograms & percentiles (why averages lie); metric
cardinality (the label-explosion trap). **SDE-3 lens:** high-cardinality labels
(per-user-id) blow up your metrics store — a classic production incident.

### Phase 15 — Multi-Agent Orchestration
**Spine:** a planner delegates to specialized worker + critic agents.
**Concepts:** the actor model; orchestration topologies (supervisor/worker,
pipeline, blackboard); context isolation at scale; inter-agent message passing.
**SDE-3 lens:** the anti-pattern is agents that share context and drift into
groupthink; isolation is the feature. Tradeoff: orchestration multiplies cost and
latency — justify each agent.

### Phase 16 — Durable State & Event Sourcing
**Spine:** sessions are durable; the full agent history is replayable.
**Concepts:** event sourcing (state as a fold over an event log); CQRS
(command/query separation); snapshotting for replay performance. Ties directly to
the append-only ledger (P3) — now applied to agent state. **SDE-3 lens:** the
Hungerly concept you designed but never built — now you *build* it. Tradeoff:
event sourcing is powerful and complex; replay bugs are subtle.

### Phase 17 — Security & Multi-Tenancy
**Spine:** multiple users, isolated, authenticated, with managed secrets.
**Concepts:** authN vs authZ; RBAC; secret management (vaulting, rotation);
tenant isolation (the noisy-neighbor problem). **SDE-3 lens:** the P8 "trust user
tools in-process" posture now becomes a hard boundary — in-process user code in a
multi-tenant server is a breach waiting to happen. This phase forces the
sandboxing you deferred.

### Phase 18 — Performance & Efficiency
**Spine:** low latency and cost under real load.
**Concepts:** latency budgets; provider prompt caching (stable-prefix reuse);
request batching; connection pooling; concurrency tuning; streaming
optimization. **SDE-3 lens:** measure before optimizing (P9/P14 give you the
data); the anti-pattern is guessing. Tradeoff: caching and batching add
correctness edge cases (stale prefixes, partial batches).

### Phase 19 — Deployment & Delivery
**Spine:** ship safely and roll back fast.
**Concepts:** containerization; 12-factor app; CI/CD pipelines; progressive
delivery (feature flags, blue-green, canary); health checks & readiness probes.
**SDE-3 lens:** the operability traits that separate SDE-3 from SDE-1 — you own
the deploy, not just the code. Tradeoff: flags add config surface and dead code if
never cleaned up.

### Phase 20 — The Model Layer (stretch)
**Spine:** eval-driven model selection, and optionally a fine-tuned or local model.
**Concepts:** eval-driven development (the eval suite as the fitness function);
PEFT/LoRA (parameter-efficient fine-tuning); when fine-tuning beats prompting (and
when it doesn't — usually it doesn't); local inference tradeoffs. **SDE-3 lens:**
the anti-pattern is fine-tuning to fix a prompt problem. Fine-tune only when eval
data proves prompting has plateaued. Tradeoff: a fine-tuned model is a maintenance
liability (drift, re-training) vs. a prompt you can change in seconds.

---

## APPENDIX A — CONCEPT INDEX (what you'll own by the end)

Grouped by domain, so you can see the curriculum's coverage.

**Software design & SOLID:** dependency inversion (P0/P3), Open/Closed (P2),
interface segregation (P0), anti-corruption layer (P0), policy/mechanism
separation (P5).

**Agent engineering:** ReAct loop (P0), context compaction (P1), loop detection
(P1), context isolation (P5/P15), skills/on-demand context (P5), planning &
reflection (P11), multi-agent orchestration (P15).

**Distributed systems:** service contracts & API compatibility (P3), append-only
ledgers / event sourcing (P3/P16), caching & invalidation (P4/P10), token-bucket
rate limiting (P4), circuit breakers (P4), backoff+jitter (P4), graceful
degradation (P3), CAP tradeoffs (P3/P8), event fan-out & pub/sub (P8),
backpressure (P8).

**AI engineering:** embeddings (P4), RAG & chunking (P10), hybrid retrieval (P10),
LLM-as-judge (P9), prompt injection & guardrails (P12), adaptive routing &
bandits (P13), eval-driven development (P9/P20), PEFT/LoRA (P20).

**Retrieval/PL:** AST parsing via tree-sitter (P5.5), PageRank/graph centrality
(P5.5), vector similarity search (P10).

**Observability & ops:** discriminated unions & exhaustiveness (P1), distributed
tracing (P9), RED/USE metrics & percentiles (P14), OpenTelemetry (P14), CI/CD &
progressive delivery (P19), secret management & RBAC (P17).

---

## APPENDIX B — HOW THE DOCS RELATE

- **FORGE.md** (this file) — intent, architecture, per-phase scope + concepts.
  *The syllabus.*
- **FORGE_BUILD_PLAN.md** — per-phase prompt blocks + acceptance gates.
  *The lab manual.*
- **CLAUDE.md** — invariants Claude Code enforces in reviewer/mentor mode.
  *The examiner.*

Read this to know *what and why*. Execute the build plan to *do it*. Let
`CLAUDE.md` keep you honest while you write the code yourself.

---

## THE ONE RULE, RESTATED

Every phase ends in running code, or it didn't happen. A 21-phase syllabus you
*build* is a staff-level education. A 21-phase syllabus you *write* is the fourth
spec that never shipped. The difference is entirely in whether you close this
document and go implement the phase in front of you.






# FORGE — Movement VII: Production Hardening (Phases 14–20)

In-depth companion to `FORGE.md`. Where the main spec compresses these phases to a
paragraph, this document gives them the full system-design treatment.

**Why these phases matter more than they look.** Movements I–VI make FORGE *work*.
Movement VII makes it *operable* — and operability is the single trait that
separates an SDE-1 from an SDE-3. An SDE-1 asks "does it work on my machine?" An
SDE-3 asks "how do I see it when it breaks at 3am, isolate the blast radius, roll
back in 90 seconds, and prove it won't regress?" Every phase here is a rep on that
muscle. The concepts are deliberately transferable: you will use OpenTelemetry,
event sourcing, RBAC, and canary deploys in *every* backend system you ever own,
not just FORGE.

**The through-line:** you already emit structured events (P1), keep an append-only
ledger (P3), and produce tracing spans (P9). Movement VII is largely about taking
those existing signals and building the *operational surface* on top of them —
dashboards, replay, isolation, safe delivery. You're not starting cold; you're
industrializing what you built.

---

## Phase 14 — Deep Observability

**Spine:** when FORGE misbehaves, I can see *why* from the outside, in seconds, on
a dashboard — without adding print statements.

**What changes about FORGE:** P9 gave you tracing spans and a ledger. Right now
they're data at rest. P14 turns them into a live operational surface: metrics
pipelines, dashboards, and alerts. FORGE stops being a black box you inspect by
reading logs and becomes a system you *observe*.

### Scope
- **In:** OpenTelemetry instrumentation; metrics export (Prometheus/OTLP); a
  Grafana (or equivalent) dashboard; structured JSON logging with correlation
  IDs; SLI/SLO definitions + error budgets; a first real alert.
- **Out:** distributed tracing across *multiple services* beyond gateway+agent
  (you only have two); ML-based anomaly detection.

### Features & functionality
- Instrument the gateway and agent with OTel: counters, gauges, histograms.
- Export metrics; stand up a dashboard reading them.
- Correlation ID threaded from request → agent run → each tool call → gateway
  call, so one ID reconstructs the whole causal chain.
- Define SLIs (e.g. p95 turn latency, tool-error rate, $/task) and SLOs
  (targets), compute an error budget.
- One symptom-based alert (e.g. "tool-error rate > 5% for 5m").

### Concepts you'll learn

**The three pillars of observability.**
- *Core + analogy:* Metrics (aggregate numbers — the car's dashboard gauges),
  Logs (discrete events — the car's diagnostic trouble codes), Traces (causal
  request path — the GPS breadcrumb trail of one trip). You need all three;
  each answers a different question. Metrics: "is something wrong?" Traces:
  "*where* is it wrong?" Logs: "*what exactly* happened there?"
- *Under the hood:* A metric is a time-series: a name + labels + a value over
  time, stored in a TSDB. A **counter** only goes up (total requests). A
  **gauge** goes up/down (in-flight requests). A **histogram** buckets
  observations (latency 0–10ms, 10–50ms, …) so you can compute percentiles.
  Prometheus *pulls* metrics by scraping a `/metrics` endpoint; push-based
  systems (StatsD/OTLP) send them. The pull model gives you free liveness
  detection — if the scrape fails, the target is down.
- *SDE-3:* **Averages lie.** A 200ms average latency can hide that 1% of users
  wait 8s. You operate on percentiles — p50, p95, p99, p99.9 — because the tail
  is where users churn and incidents live. This is why you emit histograms, not
  averages.

**Cardinality — the trap that OOMs your metrics store.**
- *Core:* Every unique combination of label values creates a separate time-series.
  `http_requests{route="/chat"}` is one series. `http_requests{user_id="..."}`
  with 10k users is 10k series. Add another high-cardinality label and you
  multiply.
- *Under the hood:* TSDBs index every series in memory. High cardinality
  (user IDs, request IDs, full URLs as labels) causes a **cardinality explosion**
  that exhausts memory and takes the monitoring system down — often mid-incident,
  when you need it most.
- *SDE-3:* IDs belong in *traces and logs* (high-cardinality, sampled), never in
  *metric labels* (low-cardinality, always-on). This one rule prevents a whole
  class of production outages.

**SLI / SLO / error budget.**
- *Core:* An SLI is a measured signal (p95 latency). An SLO is the target
  (p95 < 2s, 99% of the time). The **error budget** is `1 − SLO` — the amount of
  failure you're *allowed*. If your SLO is 99.9%, you have 0.1% budget to spend on
  risky deploys.
- *SDE-3:* The error budget turns reliability from an argument into a number. Out
  of budget → freeze features, fix reliability. In budget → ship faster, spend it.
  It aligns product and ops on one metric.

### Failure modes
| Failure | Cause | Mitigation |
|---|---|---|
| Monitoring is blind | Metrics pipeline down | Alert on *absence* of metrics (dead-man's switch) |
| TSDB OOM | Cardinality explosion | Label hygiene; IDs in traces not metrics |
| Alert fatigue | Alerting on causes, not symptoms | Alert on user-facing SLIs; page only on symptoms |
| PII leak | User content in logs | Scrub before logging; structured fields only |

### SDE-3 lens
- **Anti-pattern:** logging your way to observability. Unstructured `print`s don't
  aggregate; you can't graph a log line. Emit structured events and metrics.
- **Tradeoff:** instrumentation has overhead (histogram buckets, span creation).
  Sample traces (e.g. 100% of errors, 1% of successes) to bound cost.

### Definition of done
A dashboard shows live p95 latency, tool-error rate, and $/task from real runs;
one correlation ID reconstructs a full request in the trace view; one alert fires
on a deliberately induced error spike.

---

## Phase 15 — Multi-Agent Orchestration

**Spine:** a planner agent decomposes a hard task and delegates to specialized
worker and critic agents, each with an isolated context.

**What changes about FORGE:** P5 gave you *one* subagent primitive (parent spawns
a scoped child, only the result returns). P15 generalizes that into a *team* with
a coordination layer. This is where single-agent limits (context window, focus
drift on long tasks) get broken by division of labor.

### Scope
- **In:** an orchestrator/planner; ≥2 specialized agents (e.g.
  `codebase-investigator`, `code-reviewer`, `implementer`); a message-passing
  contract between them; result aggregation; per-child timeouts + cost budgets.
- **Out:** dynamic agent spawning of arbitrary depth (bound it); learned
  orchestration policies.

### Features & functionality
- Planner decomposes the goal into sub-tasks and assigns each to an agent role.
- Each child runs with isolated context; returns a structured result, not its
  transcript.
- Orchestrator aggregates results, handles a child failure/timeout, and decides
  next steps.
- Every child inherits the run's cost/iteration budget (children can't escape the
  global governor).

### Concepts you'll learn

**Orchestration topologies.**
- *Core + analogy:* How you wire agents together, like org structures.
  - **Supervisor/worker:** one planner delegates to workers (a manager + team).
  - **Pipeline:** output of A feeds B feeds C (an assembly line).
  - **Blackboard:** agents read/write a shared workspace (a war room whiteboard).
  - **Hierarchical:** supervisors of supervisors (a company org chart).
- *SDE-3:* Topology is a latency/cost/quality tradeoff. Pipelines are simple but
  serial (slow). Supervisor/worker parallelizes but adds coordination overhead.
  Start with supervisor/worker; reach for others only when a real bottleneck
  demands it.

**Context isolation as the core value.**
- *Core:* A child agent exploring a codebase generates thousands of tokens of
  noisy searching. If that pollutes the parent's context, you blow the window and
  degrade the parent's reasoning. Isolation means the child does the messy work
  and returns *only the distilled answer*.
- *Under the hood:* Each child gets a fresh message history seeded with just its
  sub-task. Its `agent.run` is independent; the parent receives a single
  synthetic observation. This is `map` (spawn children) + `reduce` (aggregate
  results) applied to agents.
- *SDE-3:* Isolation is also a *failure boundary* — a child that spirals or errors
  is contained; the parent gets a clean "child failed" observation and continues.

**The coordination overhead problem.**
- *Core:* N agents don't give N× throughput. Coordination (planning, message
  passing, aggregation, waiting on the slowest child) is pure overhead. Amdahl's
  law for agents.
- *SDE-3:* Every agent you add multiplies cost (N agents × M turns each) and adds
  a failure mode. Justify each role. Two well-scoped agents beat five vague ones.

### Under the hood — a minimal topology
```
                 ┌──────────────┐
   goal ───────► │  Orchestrator │  (plans, assigns, aggregates)
                 └──────┬───────┘
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
   ┌────────────┐ ┌────────────┐ ┌────────────┐
   │investigator│ │implementer │ │  reviewer  │   isolated contexts
   └─────┬──────┘ └─────┬──────┘ └─────┬──────┘
         └── result ────┼──── result ──┘
                        ▼
                 aggregate → next step or done
```

### Failure modes
| Failure | Cause | Mitigation |
|---|---|---|
| Orchestrator hangs | A child never returns | Per-child timeout + budget; treat timeout as a failed observation |
| Cost explosion | N agents × M turns unbounded | Global budget shared across the tree; children can't exceed it |
| Groupthink | Shared context across agents | Enforce isolation; agents disagree by design |
| Deadlock/livelock | Agents wait on each other (blackboard) | Acyclic dependencies; a coordinator owns sequencing |

### SDE-3 lens
- **Anti-pattern:** spawning agents for tasks a single agent handles fine.
  Multi-agent is for *genuinely separable* concerns, not everything.
- **Tradeoff:** parallelism (spawn workers concurrently) cuts latency but
  complicates aggregation and error handling. Serial is simpler and often enough.

### Definition of done
A multi-part goal produces a visible plan, spawns ≥2 isolated child agents, and
aggregates their results into a correct outcome; a deliberately failed child is
contained and the orchestrator recovers.

---

## Phase 16 — Durable State & Event Sourcing

**Spine:** a session survives a restart, and the *entire* agent history is
replayable and auditable — state is derived from an immutable event log.

**What changes about FORGE:** P5 gave you checkpoints (snapshot save/restore). P16
reframes persistence: instead of saving *current state*, you save the *sequence of
events that produced it*, and rebuild state by replaying them. This is the
Hungerly event-sourcing concept you designed but never shipped — now it's real.

### Scope
- **In:** an append-only event store for agent history (each message, tool call,
  decision as an event); state reconstruction by replay; snapshots for replay
  performance; event schema versioning.
- **Out:** distributed consensus across event-store replicas; full CQRS read-model
  infrastructure beyond the P7 dashboard.

### Features & functionality
- Every state transition (user turn, model response, tool call, tool result,
  approval decision) is appended as an immutable event.
- Current session state = fold over the event log from the last snapshot.
- Periodic snapshots so replay doesn't start from event zero.
- Event versioning so old logs still replay after the schema evolves.

### Concepts you'll learn

**Event sourcing — the core inversion.**
- *Core + analogy:* Most systems store *state* and mutate it (UPDATE the row).
  Event sourcing stores the *events* and derives state by replaying them — like a
  bank ledger. Your account balance isn't stored; it's the sum of every
  transaction. The transactions are the truth; the balance is a derived view.
- *Under the hood:* State is `reduce(events, apply, initial_state)`. To get the
  current session, you replay every event through a reducer. Because replay of a
  long log is slow, you periodically write a **snapshot** (state at event N) and
  replay only events after N.
- *SDE-3:* You get a full audit log and time-travel debugging *for free* — you can
  replay to any point and see exactly what the agent knew. This is enormously
  powerful for debugging non-deterministic agents ("replay to turn 12, what was in
  context?").

**CQRS (Command Query Responsibility Segregation).**
- *Core:* Separate the write model (append events) from the read model (query a
  projection). Writes go to the event log; reads come from a derived view
  optimized for querying (your P7 dashboard reads a projection of the ledger).
- *SDE-3:* CQRS lets write and read scale independently, but introduces
  **eventual consistency** — the read model lags the write model. You must decide
  whether that lag is acceptable per query.

**Idempotency & determinism in replay.**
- *Core:* Replay must be deterministic — replaying the same events must produce the
  same state. This breaks if events contain *side effects* (a "call the LLM" event
  that re-calls on replay).
- *SDE-3:* Store the *result* of a side effect as its own event, not the
  *intent*. Replay reads the stored result; it never re-executes. This is the
  single most important event-sourcing discipline and the one most people get
  wrong.

### Under the hood — replay with snapshots
```
event log:  e0 e1 e2 ... e99 [SNAP@100] e101 e102 e103 (now)
                                  │
replay:  load SNAP@100  ──►  apply e101,e102,e103  ──►  current state
         (skip e0..e99)      (only 3 events, not 103)
```

### Failure modes
| Failure | Cause | Mitigation |
|---|---|---|
| Replay produces wrong state | Side effects in events | Store results, never intents; deterministic reducers |
| Old logs won't replay | Schema evolved | Version events; upcasters transform old → new |
| Replay too slow | No snapshots | Periodic snapshots; replay from latest |
| Dual-write inconsistency | Event + external write not atomic | Outbox pattern; event is the source of truth |

### SDE-3 lens
- **Anti-pattern:** event sourcing *everything*. It's complex; use it where audit,
  replay, or time-travel add real value (agent history: yes; a config flag: no).
- **Tradeoff:** immense debuggability and auditability vs. real complexity — schema
  evolution, replay performance, and eventual consistency are ongoing costs.

### Definition of done
Kill FORGE mid-session; restart; the session resumes exactly where it left off by
replaying events from the last snapshot. Replay to an arbitrary past turn and
inspect the agent's context at that point.

---

## Phase 17 — Security & Multi-Tenancy

**Spine:** multiple users share one FORGE deployment, fully isolated,
authenticated, authorized, with secrets managed and user tool code sandboxed.

**What changes about FORGE:** P8 made FORGE a server; P2 lets users add tools (=
untrusted code) that P8 runs *in-process*. That's a convenience in a solo CLI and
a **vulnerability** in a shared server. P17 pays that debt: this is where you name
and enforce the security posture you deferred.

### Scope
- **In:** authentication (who are you); authorization (what may you do, RBAC);
  secret management (vaulting, rotation, no secrets in code/logs); tenant
  isolation (data + cost + policy per tenant); sandboxing of user tool execution.
- **Out:** full SOC2/compliance tooling; hardware-level isolation.

### Features & functionality
- AuthN: sessions/JWT/OAuth on the P8 server.
- AuthZ: RBAC — roles gate which tools, models, and cost limits a user gets.
- Secrets: pulled from a vault, injected at runtime, rotated; never committed,
  never logged.
- Tenant isolation: each tenant's ledger, event log, and sessions are isolated;
  one tenant can't see or exhaust another.
- Sandboxing: user tool code runs in a subprocess/container/WASM boundary, not the
  main process.

### Concepts you'll learn

**AuthN vs AuthZ — the distinction people conflate.**
- *Core + analogy:* AuthN = *who are you* (showing your passport at the airport).
  AuthZ = *what are you allowed to do* (your boarding pass says seat 14C, not the
  cockpit). Separate concerns, separate failures.
- *Under the hood:* A JWT is `header.payload.signature`, base64-encoded. The
  signature (signed with a server secret) makes it tamper-evident — change the
  payload and the signature breaks. The server verifies the signature without a DB
  lookup (stateless auth), which is the tradeoff vs. server-side sessions
  (stateful, revocable instantly).
- *SDE-3:* JWTs are hard to revoke before expiry (they're stateless). Short
  expiry + refresh tokens is the standard mitigation. Know why you chose stateless
  vs. session.

**Tenant isolation — the catastrophic-failure domain.**
- *Core:* Three levels: **row-level** (tenant_id column, shared tables — cheap,
  leak-prone), **schema-level** (schema per tenant — middle), **database-level**
  (DB per tenant — strong isolation, expensive). A cross-tenant data leak is the
  single worst bug class in a SaaS.
- *SDE-3:* Isolation must be enforced at the lowest layer possible (DB row-level
  security), not in application `if` statements — one missed `WHERE tenant_id=?`
  and you've leaked. Defense in depth: enforce in the query layer *and* the DB.

**The confused deputy problem.**
- *Core:* FORGE acts with *its own* credentials (a GitHub token, a DB connection)
  on behalf of a *user*. A malicious user can trick FORGE into using its elevated
  privileges to do something the user couldn't. The agent is the "confused
  deputy" wielding authority it shouldn't for that user.
- *SDE-3:* Scope every action to the *user's* permissions, not FORGE's. Pass the
  user's identity down; check authZ at the point of action, not just at the door.

**Sandboxing untrusted code.**
- *Core:* A user tool's `run()` is arbitrary code in your process — it can read
  your secrets, exhaust memory, or make network calls. Sandbox it: subprocess with
  dropped privileges, a container, or a WASM runtime.
- *SDE-3:* Sandboxing is a spectrum of cost vs. safety: subprocess (weak, cheap) →
  container/gVisor (strong, heavier) → WASM (strong, restrictive). Pick per threat
  model. In-process (P2/P8) is only acceptable single-tenant.

### Failure modes
| Failure | Cause | Mitigation |
|---|---|---|
| Cross-tenant data leak | Missing tenant filter | DB row-level security + query-layer enforcement |
| Secret in logs | Logging full request/env | Scrub secrets; structured logging with denylist |
| Privilege escalation | User tool reads process secrets | Sandbox execution; least privilege |
| Confused deputy | Agent uses its creds for user actions | Scope actions to user identity + permissions |
| Noisy neighbor | One tenant exhausts resources | Per-tenant rate limits + cost budgets |

### SDE-3 lens
- **Anti-pattern:** authZ checks scattered as ad-hoc `if` statements. Centralize
  policy (a policy engine, extending the P5 approval model to a full authZ layer).
- **Tradeoff:** stronger isolation (DB-per-tenant, WASM sandbox) costs money and
  latency. Match isolation strength to the sensitivity of what you're protecting.

### Definition of done
Two users run concurrent sessions with provably isolated data and cost; a user
tool attempting to read a server secret is blocked by the sandbox; secrets are
vault-sourced and absent from all logs.

---

## Phase 17.S — The Security Scanner Suite (Grype + ClamAV via MCP)

This is a sub-phase of P17 because it completes the **defense-in-depth** picture.
P17's core is *dynamic* runtime containment (eBPF/Tetragon watching what executes).
The scanners are the *static* half — analysis of artifacts at rest. Together they
form three complementary layers:

```
STATIC (at rest)                          DYNAMIC (at runtime)
├─ Grype     → supply chain (deps/CVEs)   └─ eBPF/Tetragon → syscall/exec/net
└─ ClamAV    → content (malware sigs)         (P17 core, observe-mode)
```

None of these is a single feature you drop in one place. Each carries a **motive**,
and the motive decides its home. Naming the motive is the whole discipline
(you've applied it to eBPF and to the scanners already — here it's codified).

### The motive gate — where each scanner actually lives

| Scanner | Analysis | Motive | Primary home | This section covers |
|---|---|---|---|---|
| **Grype** | static, supply-chain | **A** — agent capability (help user fix *their* deps) | Tool / MCP (~P6) | its security engineering + its P19 dogfooding role |
| **ClamAV** | static, content | **B** — guardrail (protect user *from* ingested content) | Guardrail (P12) | its P17 security engineering |
| **eBPF** | dynamic, runtime | **B** — containment (watch untrusted execution) | P17 core (above) | — |

Grype has a **dual role**, and this is the useful teaching point: the *same binary*
serves two motives at two layers.
- **Motive A (agent-facing, ~P6):** the agent calls Grype on the *user's* repo to
  find and fix vulnerable dependencies (scan → identify → bump → test → PR).
- **FORGE's own posture (P17/P19):** Grype scans *FORGE's own* dependency tree in
  CI (P19) — FORGE is software with a supply chain too. Dogfooding.

ClamAV's motive is narrower and it constrains the tool's *signature*: it scans
**untrusted content FORGE ingests** (`web_fetch`/`download_file` output) before it
enters context or executes — **not the user's working directory.** A coding
agent's own repo is not the threat. The tool must be named `scan_ingested_artifact`,
not `scan_directory`, so the wrong use is hard to reach for.

### Requirements

**Functional:** expose both as MCP tools; agent invokes with a target; receives a
*structured, ranked, truncated* result; results land in the P6 registry (namespaced,
schema-validated) beside local + user tools.

**Non-functional:**
- **Latency:** ClamAV's signature DB (~200MB+) cold-loads in seconds; Grype's DB
  refresh hits the network. Unmanaged, both break the "a tool call feels instant"
  expectation.
- **Reproducibility:** same input → same findings *modulo DB version* — so the DB
  version must be **in the output**, or two scans aren't comparable.
- **Bounded output:** the scale point that reframes the whole design (below).

**Scale estimation — the number that dictates the contract:** Grype on a mature
repo returns **hundreds to thousands** of findings. At ~50 tokens/finding, 2,000
findings ≈ **100k tokens**. That cannot go in an agent's context. Therefore the
tool **must summarize + rank + cap server-side, never return raw.** This single
number is why "wrap the CLI" is not the job — the projection is.

### Core design — the contract IS the engineering

The wrapper (invoke CLI → parse) is ~30 lines and worthless alone. The value is the
projection into an agent-actionable shape:

```python
from typing import TypedDict, Literal

class Vuln(TypedDict):
    package: str
    installed: str
    fixed_in: str | None      # None → agent CANNOT act; deprioritize, don't rank high
    severity: Literal["critical", "high", "medium", "low", "negligible"]
    cve: str

class GrypeResult(TypedDict):
    db_version: str           # reproducibility — WITHOUT this, re-scans aren't comparable
    total_findings: int       # "showing 20 of 1,847"
    actionable: list[Vuln]    # fixed_in != None, severity-desc, CAPPED (top-N)
    summary: dict[str, int]   # {"critical": 3, "high": 12, ...} — at-a-glance
    truncated: bool
    status: Literal["ok", "stale_db", "db_fetch_failed"]   # stale MUST NOT read as pass

class ClamResult(TypedDict):
    clean: bool
    infections: list[dict]    # [{"path": str, "signature": str}] — capped
    files_scanned: int
    db_version: str
    status: Literal["ok", "clamd_unavailable", "timeout"]  # fail-safe, never silent-clean
```

Three decisions baked into `GrypeResult`, each a tradeoff:
- **Rank by *actionability*, not raw severity.** A `critical` with `fixed_in: None`
  is noise to a *coding* agent — it can't bump a version that doesn't exist. This
  is motive-A thinking (help the agent *act*), not scanner thinking (report all).
- **Cap + summarize; paginate via tool args.** Return top-N + a histogram +
  `total_findings`. The agent sees the shape without eating 100k tokens; it
  re-queries with a filter (`severity="critical"`) if it needs more.
- **`db_version` in the output.** Without it the agent can't tell "my fix worked"
  from "the DB changed under me." This is the append-only-ledger instinct (P3, P16)
  applied to scan results.

### Transport — stdio + `clamd`, not SSE, not `clamscan`

| Decision | Grype | ClamAV |
|---|---|---|
| Transport | **stdio** (stateless-ish; DB cached on disk) | **stdio wrapper → `clamd` daemon** |
| DB lifecycle | `update_db` exposed; check freshness on invoke | **`clamdscan` against resident `clamd`** — NOT `clamscan` |
| Why | simple lifecycle, no service to run | `clamscan` reloads the 200MB DB *every call* — the classic footgun |

HTTP/SSE only if the scanner is shared infra behind the P8 server. For a CLI,
stdio + a warm `clamd` gives simple lifecycle *and* fast scans.

### Build-vs-buy — decided against the evidence

Two existing servers were evaluated. Both fail the test "adopt only if maintained
AND the contract fits an agent consumer":

| | `anchore/grype-mcp` | `a2amarket/mcp-clamav` |
|---|---|---|
| Health | **archived Mar 2026, read-only**; last release Aug 2025 | 3★, 0 releases, 4 commits — toy |
| Output contract | thin passthrough ("zero modifications to Grype") | `result: raw clamscan output` |
| Transport | stdio (ok) | **SSE-only + base64 the file over the wire** |
| DB lifecycle | `update_db`/`get_db_info` (good ideas) | `clamscan` (cold reload every call) |
| Verdict | **fork the *interface design*, rewrite the contract** | **build fresh — architecture is wrong** |

The lesson generalized: **a generic scanner MCP is built for a human reading output
in an IDE; your consumer is an agent with a finite context window.** Same output,
wrong audience. That mismatch — not quality — is when "build it yourself" is
correct, and it's the explicit exception to the P6 MCP-first rule.

- **ClamAV → build.** SSE + base64 + `clamscan` + raw output is wrong on every
  axis for a local CLI. ~40 lines, you own every decision.
- **Grype → build, but steal the homework.** The archived Anchore repo has a *good*
  tool surface — copy `scan_dir` / `scan_purl` / `scan_image` / `get_db_info` /
  `update_db` (note `scan_purl`: scan one `pkg:npm/lodash@4.17.20` without a full
  rescan — a smart agent affordance). Apache-2.0, so the *ideas* are free; check
  LICENSE before lifting code. Add the layer it skipped: rank, cap, `db_version`,
  fail-safe.

**The reusable rule (state it in one line):** *adopt when the server is maintained
AND its contract fits an agent consumer; build when either fails.*

### Under the hood — the DB-lifecycle failure that reads as a pass

```
Grype:  invoke ─► check DB freshness ─► [stale] network fetch ─► scan
                                            └─ FAILS offline → naive wrapper
                                               returns error the agent reads
                                               as "no vulns" = FALSE NEGATIVE
ClamAV: invoke ─► clamd up? ─► [no] cold-load 200MB sigs (seconds)
                     └─ [yes] scan warm (fast)
```

The dangerous direction for a security tool is the **false negative that reads as a
pass.** A stale/failed scan must surface `status: "stale_db"` — never an empty
`findings: []` the agent interprets as clean.

### Failure modes
| Failure | Naive wrapper | SDE-3 contract |
|---|---|---|
| Grype DB fetch fails (offline) | error → agent reads as "clean" | `status: "stale_db"`, `db_age` surfaced — **never silent pass** |
| ClamAV `clamd` not running | timeout → agent hangs | health-check on init; fail fast `clamd_unavailable` |
| Scan of huge tree | 100k-token dump → context blown | server-side cap + `summary` + `truncated: true` |
| Scan times out (giant image) | hangs the turn | bounded timeout → partial + `incomplete: true` |
| ClamAV finds malware | path returned as plain string → agent may `read_file` it | quarantine metadata; P12 guardrail **hard-blocks ingestion** of flagged paths |

That last row is motive-B-critical: a scanner that hands the agent a *path to
malware* must ensure the agent does not then read that path into context. A ClamAV
hit is a **hard block on ingestion**, not a finding to reason about.

### Operability
- **Metrics → P14:** `scan_duration`, `db_version`, `findings_count`, `db_staleness`.
  **DB staleness is a security SLI** — a scanner on a 30-day-old DB is a silent
  liability; alert on it.
- **Trace → P9:** each scan is a span in the agent run (see "agent spent 8s in Grype").
- **The scanners' own supply chain:** Grype/ClamAV signature DBs are dependencies
  *you don't control*. Log versions; treat DB updates as a change event.

### SDE-3 lens
- **Anti-pattern — let the model sort findings.** Returning raw findings and asking
  the model to rank them pays tokens + a round-trip for what `sorted(key=severity)`
  does perfectly and deterministically. The model is a *bad* sorter. Rank
  server-side.
- **Anti-pattern — ClamAV on the repo.** Pointing the malware scanner at the user's
  working directory is a slow, pointless linter. Scan *ingested untrusted content*
  only; encode that in the tool signature.
- **Tradeoff — server-side projection.** More wrapper code (rank/cap/summary) vs.
  deterministic, token-cheap, model-independent results. For 2,000 findings this is
  not close: build the projection.

### Reproducibility note (the re-scan workflow)
The agent's core loop here is: scan → fix a vuln → **re-scan to confirm.** If
Grype's DB updates *between* the two scans, the delta conflates "my fix" with "DB
drift." The `db_version` field makes drift *detectable*; the workflow must **pin
the DB version across a fix-verify cycle** (scan with a fixed DB, or compare
`db_version` and flag if it changed) so the agent can attribute the delta correctly.

### Definition of done
Grype and ClamAV run as stdio MCP servers returning the `GrypeResult`/`ClamResult`
contracts (not raw output); a scan of a repo with 1,000+ findings returns a
ranked, capped, summarized result under a token budget; an offline/stale scan
surfaces `stale_db` and is **never** read as clean; a ClamAV hit hard-blocks
ingestion of the flagged path.

---

## Phase 18 — Performance & Efficiency

**Spine:** low latency and low cost under real concurrent load — driven by
measurement, not guesswork.

**What changes about FORGE:** P4 gave you gateway caching; P14 gave you the metrics
to *see* latency and cost. Now you optimize with data. The rule that governs this
entire phase: **measure first.** You already have the instrumentation; use it to
find the real bottleneck before touching anything.

### Scope
- **In:** provider prompt caching (stable-prefix reuse); request batching (esp.
  embeddings for P10); connection pooling; concurrency tuning; streaming
  optimization; tail-latency reduction.
- **Out:** custom inference kernels; model-level optimization (that's P20).

### Features & functionality
- Prompt-cache the stable prefix (system + tools + early turns) so repeat calls
  skip re-processing it.
- Batch embedding requests when indexing (P10) instead of one-per-chunk.
- Pool and reuse provider HTTP connections.
- Tune the concurrency of parallel tool calls / subagents.

### Concepts you'll learn

**Prompt caching — the biggest cheap win.**
- *Core + analogy:* The system prompt + tool definitions are identical across
  every turn. Re-sending and re-processing them each time is waste. Providers
  cache the KV representation of a stable prefix so subsequent calls reuse it —
  like a compiler caching a parsed header instead of re-parsing it every build.
- *Under the hood:* Transformers compute a key/value cache over the prompt tokens.
  If the prefix is unchanged, that computation is reused (provider-side, via
  cache markers). You pay full price once, reduced price thereafter, for the
  cached span.
- *SDE-3:* Cache the *stable* prefix; put *volatile* content (the latest turn)
  after it. Ordering matters — a change early in the prompt invalidates the whole
  cache downstream.

**Latency vs. throughput, and Little's Law.**
- *Core:* Latency = time for one request. Throughput = requests per second.
  Batching *increases throughput* (amortize fixed costs) but *increases latency*
  for the individual request (it waits for the batch). They trade off.
- *Under the hood:* Little's Law: `concurrency = throughput × latency`. To handle
  more concurrent load, either go faster (lower latency) or process more in
  parallel (higher concurrency) — and each has a ceiling (connection pool size,
  provider rate limits).
- *SDE-3:* Know which you're optimizing. A batch job wants throughput; an
  interactive agent turn wants latency. Don't batch the interactive path.

**Tail latency amplification.**
- *Core:* In fan-out (P15 multi-agent), the parent waits for the *slowest* child.
  If each child has a 1% chance of being slow (p99), and you fan out to 10
  children, the chance *at least one* is slow is ~10%. Your p99 children make your
  p50 parent slow.
- *SDE-3:* This is why tail latency dominates distributed systems. Mitigations:
  hedged requests (send a duplicate to a second provider, take the first
  response), timeouts, and reducing fan-out width.

**Connection pooling.**
- *Core:* Opening a TCP+TLS connection per request is expensive (handshake RTTs).
  A pool keeps warm connections and reuses them.
- *SDE-3:* Pool size is a tuning knob: too small → requests queue (head-of-line
  blocking); too large → resource exhaustion. Size it against measured
  concurrency, not a guess.

### Failure modes
| Failure | Cause | Mitigation |
|---|---|---|
| Cache stampede | Many concurrent misses on the same key | Single-flight / request coalescing |
| Pool exhaustion | Pool too small under load | Size against measured concurrency; queue with timeout |
| Head-of-line blocking | Serial processing of a slow request | Bounded concurrency; timeouts |
| Over-batching | Batch too large; latency spikes | Cap batch size + max wait time |

### SDE-3 lens
- **Anti-pattern:** optimizing without profiling. You will optimize the wrong
  thing. Use P14 metrics to find the actual hotspot first — usually it's provider
  latency, not your code.
- **Tradeoff:** every optimization adds complexity and an edge case (stale cache,
  partial batch, drained pool). Only optimize a *measured* bottleneck.

### Definition of done
Metrics show a measurable latency/cost reduction (e.g. prompt caching cuts $/task
by X%) attributable to a specific change, verified against a pre-optimization
baseline — not a guess.

---

## Phase 19 — Deployment & Delivery

**Spine:** ship a new version safely, with zero downtime, and roll back in under a
minute if the eval suite or live metrics say it's bad.

**What changes about FORGE:** FORGE becomes a *deployable artifact* with a delivery
pipeline. This is the phase that makes you *own* the software end-to-end — not just
write it, but ship it responsibly. Your P9 eval suite becomes the CI gate; your
P14 metrics gate the rollout.

### Scope
- **In:** containerization (Docker); a CI/CD pipeline running the P9 eval suite as
  a gate; progressive delivery (feature flags + canary or blue-green);
  liveness/readiness probes; graceful shutdown; a database migration strategy.
- **Out:** multi-region deploys; full IaC (Terraform) beyond a basic setup.

### Features & functionality
- Containerize gateway + server as immutable images.
- CI: on every commit, run tests + eval suite; block merge on regression.
- CD: deploy via canary (route small % of traffic, watch metrics, promote or roll
  back) or blue-green (stand up new, swap, keep old for instant rollback).
- Feature-flag new phases so they ship dark and enable gradually.
- Health checks so the load balancer never routes to a cold/unhealthy instance.

### Concepts you'll learn

**Progressive delivery — decoupling deploy from release.**
- *Core + analogy:* **Deploy** = the code is on the server. **Release** = users
  see it. Progressive delivery separates them so you can deploy safely and release
  gradually — like a store stocking a product in the back before putting it on
  shelves.
  - **Blue-green:** two identical environments; deploy to green, swap traffic
    from blue to green atomically; roll back = swap back (instant).
  - **Canary:** route a small % to the new version, watch metrics, promote if
    healthy, roll back if not.
  - **Feature flags:** ship code disabled, enable per-user/percentage at runtime,
    no redeploy to toggle.
- *SDE-3:* Blue-green gives instant rollback but doubles infra cost during the
  swap. Canary limits blast radius but needs good metrics (P14) to gate on.
  Feature flags decouple release from deploy but accumulate **flag debt** if never
  cleaned up.

**Health checks — liveness vs readiness.**
- *Core:* **Liveness:** is the process alive? (Fail → restart it.) **Readiness:**
  is it ready to serve? (Fail → stop routing to it, don't restart.) A cold
  instance warming its connection pool is *live but not ready*.
- *SDE-3:* Conflating them causes outages — a slow-to-warm instance gets
  restart-looped (liveness kills it before it's ready) or gets traffic before it
  can handle it (no readiness gate).

**Graceful shutdown.**
- *Core:* On deploy, the old instance must finish in-flight requests before dying,
  not drop them. It stops accepting new work, drains existing, then exits.
- *SDE-3:* For FORGE, an agent run is long — graceful shutdown must checkpoint
  (P16) or drain a running agent, or you kill a user's task mid-flight.

**Database migration strategy (expand/contract).**
- *Core:* You can't change a schema and deploy code atomically across instances —
  old and new code run simultaneously during rollout. **Expand/contract:** add the
  new column (both versions work), deploy code using it, then remove the old
  column later. Never rename in one step.
- *SDE-3:* A migration that breaks the currently-running version = self-inflicted
  outage. Migrations must be backward-compatible for the duration of a rollout.

### Under the hood — canary flow
```
   100% traffic ─► v1 (stable)
                    │  deploy v2 to 5% of instances
   95% ─► v1        │
    5% ─► v2 ───────┘  watch P14 metrics (error rate, latency)
                    ├─ healthy? ─► promote: 25% → 50% → 100%
                    └─ degraded? ─► roll back to 100% v1 (auto)
```

### Failure modes
| Failure | Cause | Mitigation |
|---|---|---|
| Bad deploy, no rollback | Mutable deploy, no prior version | Immutable images; blue-green for instant rollback |
| Migration breaks prod | Non-backward-compatible schema change | Expand/contract; never rename in one step |
| Traffic to cold instance | No readiness gate | Readiness probe before routing |
| Dropped requests on deploy | No graceful shutdown | Drain in-flight; checkpoint long runs |
| Flag debt | Flags never removed | Track + expire flags; clean up post-rollout |

### SDE-3 lens
- **Anti-pattern:** a canary with no metric gate — that's just a slow full rollout
  that still ships the bug, only slower. The gate (P14 metrics) *is* the point.
- **Tradeoff:** progressive delivery adds pipeline complexity and infra cost for
  safety. For a solo project it may feel heavy — but the *concepts* are the
  deliverable; implement a minimal blue-green to learn the shape.

### Definition of done
A commit triggers CI (tests + eval gate); a deploy rolls out via canary/blue-green
with a metric gate; a deliberately bad version is auto-rolled-back or swapped out
in under a minute with zero dropped requests.

---

## Phase 20 — The Model Layer (stretch)

**Spine:** model choice is driven by the eval suite as a fitness function; optional
fine-tuning or local inference, justified by data, not vibes.

**What changes about FORGE:** every prior phase treated the model as a fixed
input. P20 makes the model itself a *tunable component* — but under strict
eval-driven discipline. Your P9 eval suite becomes the objective function that
decides whether any model change is an improvement.

### Scope
- **In:** eval-driven model selection (compare models/prompts on the golden
  suite); optionally PEFT/LoRA fine-tuning or local inference (Ollama/llama.cpp);
  a distilled router model (ties to P13).
- **Out:** pretraining anything; large-scale training infrastructure.

### Features & functionality
- Run the eval suite across candidate models; pick by measured score + cost, not
  reputation.
- (Optional) LoRA-fine-tune a small model on FORGE-specific tasks where eval
  proves prompting has plateaued.
- (Optional) local inference for cost/privacy, measured against hosted quality.

### Concepts you'll learn

**Eval-driven development.**
- *Core + analogy:* The eval suite is the fitness function — the objective you
  optimize. Every model/prompt/fine-tune change is a hypothesis tested against it,
  like TDD but for model behavior: no green eval, no ship.
- *SDE-3:* Without an eval baseline, "the new model feels better" is unfalsifiable.
  The eval turns model selection from taste into measurement. This is the single
  most important AI-engineering discipline.

**PEFT / LoRA — what fine-tuning actually does.**
- *Core + analogy:* Full fine-tuning updates all model weights (expensive, huge).
  **LoRA** freezes the base model and trains tiny **low-rank adapter matrices**
  injected alongside the weights — like adding sticky notes to a textbook instead
  of rewriting it. You train ~0.1–1% of the parameters.
- *Under the hood:* A weight update ΔW is approximated as the product of two small
  matrices `A·B` (rank r ≪ dimension). You train A and B; the base weights never
  move. At inference, `W + A·B` is used. Tiny to store, fast to train, swappable.
- *SDE-3:* LoRA adapters are composable and cheap to store, but fine-tuning risks
  **catastrophic forgetting** (the model loses general ability) and **data
  leakage** (training on your eval set inflates scores fraudulently). Keep train
  and eval sets strictly separate.

**When fine-tuning beats prompting (rarely).**
- *Core:* Fine-tune for *form/behavior* the model can't be prompted into
  reliably (a specific output format, a domain style, latency by using a smaller
  model). Do *not* fine-tune to add *knowledge* (that's RAG, P10) or to fix a
  prompt you haven't optimized.
- *SDE-3:* The decision tree is: prompt → few-shot → RAG → *then* fine-tune, only
  if eval proves the earlier rungs plateaued. Fine-tuning is a maintenance
  liability (drift, re-training on model updates); prompting changes in seconds.

**Quantization (if going local).**
- *Core:* Quantization stores weights in lower precision (int8/int4 vs fp16),
  shrinking memory and speeding inference at some quality cost.
- *SDE-3:* It's a quality/resource dial — measure the eval-score drop against the
  cost/latency win. Don't assume int4 is "free."

### Failure modes
| Failure | Cause | Mitigation |
|---|---|---|
| Fraudulent eval gains | Trained on eval set | Strict train/eval separation; held-out test set |
| Catastrophic forgetting | Over-fine-tuning | Small LR, few epochs, eval general ability too |
| Fine-tuned to fix a prompt bug | Skipped prompt optimization | Exhaust prompt → few-shot → RAG first |
| Local model quality cliff | Aggressive quantization | Measure eval-score drop; pick precision by data |

### SDE-3 lens
- **Anti-pattern:** fine-tune-first. It's the most expensive, least reversible
  lever, reached for before cheaper ones are exhausted. The eval suite exists to
  stop you doing this.
- **Tradeoff:** a fine-tuned or local model can cut cost and add privacy, but adds
  a maintenance burden (drift, re-training, ops) a hosted model doesn't have.

### Definition of done
The eval suite ranks ≥2 candidate models by measured score and cost, and the
choice is made from that data. If you fine-tune: a LoRA adapter beats the base
model on a held-out test set (not the training set), with general ability
verified intact.

---

## Movement VII — the ownership arc, summarized

| Phase | The SDE-3 capability it forges |
|---|---|
| 14 Observability | You can *see* the system from outside and operate on symptoms |
| 15 Orchestration | You can decompose work and reason about coordination cost |
| 16 Event Sourcing | You can make state durable, auditable, and replayable |
| 17 Security | You can reason about trust boundaries and blast radius |
| 17.S Scanners | You design tool *contracts* for an agent consumer, not a human reader |
| 18 Performance | You optimize from measurement, understanding latency vs throughput |
| 19 Deployment | You *own* delivery — ship safely, roll back fast |
| 20 Model Layer | You treat the model as a measured, tunable component |

Every one of these transfers directly to any backend or platform role. FORGE is
the vehicle; operability is the skill.

---

## The build-first rule still applies — doubly, here

These phases are the most tempting to *read about* and never build, because the
concepts (event sourcing, RBAC, canary deploys) feel like knowledge you can
acquire by understanding. You can't. Understanding blue-green and *running* a
blue-green swap that drops zero requests are different skills, and only the second
is what an SDE-3 has. Each phase's "Definition of done" is a running artifact, not
a comprehension check. Build the minimal real version — one dashboard, one canary,
one LoRA adapter — and you own the concept. Read it and you own a summary.