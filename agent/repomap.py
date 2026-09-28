# repomap.py — Phase 5.5: Structural Retrieval (the repo-map)
#
# The agent knows the SHAPE of the whole repo without reading every file, and
# pulls that shape ON DEMAND (a tool) instead of paying for it every turn (a
# system-prompt tax the compactor would have to carry — see Phase 1).
#
# The instrument, in one breath:
#   parse files with tree-sitter  →  extract definitions + references (tags)
#   →  build a TYPED symbol graph (calls / imports / inherits / references)
#   →  every edge carries a CONFIDENCE (extracted | inferred | ambiguous) so the
#      model is never told a name-match guess in the same voice as an AST fact
#   →  rank with personalized PageRank (centrality = importance)
#   →  RATIONALE nodes (# WHY: / # NOTE: / # HACK:) surface intent grep can't
#   →  three tools interrogate it: explain(symbol), path(a,b), query(question)
#   →  a SQLite/mtime cache means a re-index re-parses only changed files
#   →  every query appends one JSONL line — Phase 9's eval dataset, for free.
#
# Deliberately NOT here (deferred, per the retrieval ladder): embeddings (P10),
# community detection, 36 grammars. This is the middle rung, not the ceiling.

import json
import re
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import networkx as nx
import tree_sitter as ts
from tree_sitter_language_pack import get_parser, get_language

from agent.tools import tool, ToolKind


# ─────────────────────────────────────────────
# Language surface — three languages, not thirty (the scope trap).
# ─────────────────────────────────────────────
LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "javascript",
}

# Each pattern is a standalone tree-sitter query capturing ONE relationship. They
# are compiled independently and a pattern that a given grammar rejects is simply
# skipped — so grammar drift degrades gracefully instead of blanking a language.
# Capture-name convention: "def.<kind>" for definitions, "ref.<kind>" for uses.
_PY = [
    "(function_definition name: (identifier) @def.function)",
    "(class_definition name: (identifier) @def.class)",
    "(call function: (identifier) @ref.call)",
    "(call function: (attribute attribute: (identifier) @ref.call))",
    "(import_from_statement name: (dotted_name (identifier) @ref.import))",
    "(import_statement name: (dotted_name (identifier) @ref.import))",
    "(class_definition superclasses: (argument_list (identifier) @ref.inherit))",
]
_TS = [
    "(function_declaration name: (identifier) @def.function)",
    "(method_definition name: (property_identifier) @def.function)",
    "(class_declaration name: (type_identifier) @def.class)",
    "(interface_declaration name: (type_identifier) @def.class)",
    "(variable_declarator name: (identifier) @def.function value: (arrow_function))",
    "(call_expression function: (identifier) @ref.call)",
    "(call_expression function: (member_expression property: (property_identifier) @ref.call))",
    "(import_specifier name: (identifier) @ref.import)",
    "(extends_clause (identifier) @ref.inherit)",
]
_JS = [
    "(function_declaration name: (identifier) @def.function)",
    "(method_definition name: (property_identifier) @def.function)",
    "(class_declaration name: (identifier) @def.class)",
    "(variable_declarator name: (identifier) @def.function value: (arrow_function))",
    "(call_expression function: (identifier) @ref.call)",
    "(call_expression function: (member_expression property: (property_identifier) @ref.call))",
    "(import_specifier name: (identifier) @ref.import)",
    "(class_heritage (identifier) @ref.inherit)",
]
PATTERNS = {"python": _PY, "typescript": _TS, "tsx": _TS, "javascript": _JS}

# `# WHY:` / `# NOTE:` / `# HACK:` in Python (#) or JS/TS (//). The one thing grep
# cannot surface structurally: intent, linked to the code it explains. ~1 regex.
RATIONALE_RE = re.compile(r"(?:#|//)\s*(WHY|NOTE|HACK)\s*:\s*(.+)", re.IGNORECASE)

_SKIP_DIRS = {".git", "venv", "venv-old", "__pycache__", ".pytest_cache",
              "node_modules", ".forge", "dist", "build", ".egg-info"}

# Confidence — an `inferred` edge rendered identically to an `extracted` one is a
# lie the model cannot detect (SDE-3 anti-pattern #3). These three strings ARE the
# epistemic contract; they are nearly free at build time and painful to retrofit.
EXTRACTED = "extracted"   # both endpoints read straight from one file's AST
INFERRED = "inferred"     # target resolved by name-match across files (a guess)
AMBIGUOUS = "ambiguous"   # the name matched several defs — we can't say which


# ─────────────────────────────────────────────
# A Tag = one thing tree-sitter found in one file. Defs and refs alike.
# ─────────────────────────────────────────────
@dataclass
class Tag:
    kind: str        # "def" | "ref" | "rationale"
    subkind: str     # function/class | call/import/inherit | why/note/hack
    name: str
    line: int        # 1-based
    start_byte: int
    end_byte: int
    text: str = ""   # rationale message (empty for code tags)


# compiled-query cache, keyed by language — building a Query is not free.
_QUERY_CACHE: dict[str, list[ts.Query]] = {}


def _queries(lang: str) -> list[ts.Query]:
    if lang not in _QUERY_CACHE:
        language = get_language(lang)
        compiled = []
        for pat in PATTERNS.get(lang, []):
            try:
                compiled.append(ts.Query(language, pat))
            except Exception:  # noqa: BLE001 — grammar rejects this pattern; skip it
                continue
        _QUERY_CACHE[lang] = compiled
    return _QUERY_CACHE[lang]


def extract_tags(source: bytes, lang: str) -> list[Tag]:
    """Parse one file's bytes and return every definition, reference, and
    rationale note as Tags. Pure function of (source, lang) — which is exactly
    why it caches cleanly by mtime."""
    parser = get_parser(lang)
    tree = parser.parse(source)
    root = tree.root_node
    tags: list[Tag] = []

    for query in _queries(lang):
        cursor = ts.QueryCursor(query)
        for cap_name, nodes in cursor.captures(root).items():
            kind, _, subkind = cap_name.partition(".")   # "def.function" -> def, function
            for node in nodes:
                name = source[node.start_byte:node.end_byte].decode("utf-8", "replace")
                # for a def, the OWNING scope is the enclosing statement (so a
                # function's byte range covers its whole body, letting refs inside
                # it resolve to it as their source symbol).
                span = node.parent if kind == "def" and node.parent else node
                tags.append(Tag(
                    kind=kind, subkind=subkind, name=name,
                    line=node.start_point[0] + 1,
                    start_byte=span.start_byte, end_byte=span.end_byte,
                ))

    # rationale nodes — a line scan, deliberately outside tree-sitter.
    for i, raw in enumerate(source.split(b"\n"), 1):
        m = RATIONALE_RE.search(raw.decode("utf-8", "replace"))
        if m:
            off = sum(len(l) + 1 for l in source.split(b"\n")[:i - 1])
            tags.append(Tag(kind="rationale", subkind=m.group(1).lower(),
                            name=f"{m.group(1).lower()}@{i}", line=i,
                            start_byte=off, end_byte=off, text=m.group(2).strip()))
    return tags


# ─────────────────────────────────────────────
# The SQLite/mtime cache — invalidation for free from the OS. Contrast the hard
# content-hash problem embeddings face (P10): here the OS hands us the signal.
# ─────────────────────────────────────────────
class TagCache:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS files "
            "(path TEXT PRIMARY KEY, mtime REAL, tags TEXT)")
        self.conn.commit()

    def get(self, relpath: str, mtime: float):
        row = self.conn.execute(
            "SELECT mtime, tags FROM files WHERE path=?", (relpath,)).fetchone()
        if row and row[0] == mtime:
            return [Tag(**d) for d in json.loads(row[1])]      # cache hit
        return None

    def put(self, relpath: str, mtime: float, tags: list[Tag]):
        self.conn.execute(
            "INSERT OR REPLACE INTO files(path, mtime, tags) VALUES (?,?,?)",
            (relpath, mtime, json.dumps([asdict(t) for t in tags])))
        self.conn.commit()

    def close(self):
        self.conn.close()


# ─────────────────────────────────────────────
# THE REPO MAP — the typed graph plus the ranking over it.
# ─────────────────────────────────────────────
# English filler + generic code words that would otherwise seed half the repo
# (every `test_is_*` matches "is"). A question's SIGNAL is its rare words.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "of", "to", "in", "on",
    "for", "and", "or", "how", "where", "what", "which", "does", "do", "did",
    "this", "that", "with", "by", "at", "from", "handled", "handle", "used",
    "use", "get", "set", "run", "call", "called", "code", "file", "files",
}


def _word_tokens(s: str, drop_stop: bool = False) -> set[str]:
    """Split camelCase / snake_case / kebab into lowercase word tokens, so a
    question phrased in English can seed a symbol named `parseConfigFile`.
    With drop_stop, filler words are removed — used on the QUESTION side so a
    query's rare words drive the match, not 'is'/'where'."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    toks = {w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if len(w) > 1}
    return toks - _STOPWORDS if drop_stop else toks


class RepoMap:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.g = nx.DiGraph()
        self.ranks: dict[str, float] = {}
        self.defs_by_name: dict[str, list[str]] = {}   # symbol name -> [node ids]
        self.n_files = 0
        self.log_path = self.root / ".forge" / "repomap_query.jsonl"

    # ---- build -------------------------------------------------------------
    def build(self):
        cache = TagCache(self.root / ".forge" / "repomap.db")
        per_file: dict[str, list[Tag]] = {}
        try:
            for path in self._source_files():
                rel = str(path.relative_to(self.root))
                mtime = path.stat().st_mtime
                tags = cache.get(rel, mtime)
                if tags is None:                       # miss → parse only this file
                    lang = LANG_BY_EXT[path.suffix]
                    try:
                        tags = extract_tags(path.read_bytes(), lang)
                    except Exception:  # noqa: BLE001 — a file that won't parse is skipped
                        tags = []
                    cache.put(rel, mtime, tags)
                per_file[rel] = tags
                self.n_files += 1
        finally:
            cache.close()

        self._build_graph(per_file)
        self._rank()
        return self

    def _source_files(self):
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix not in LANG_BY_EXT:
                continue
            if any(part in _SKIP_DIRS or part.endswith(".egg-info")
                   for part in path.relative_to(self.root).parts):
                continue
            yield path

    def _build_graph(self, per_file: dict[str, list[Tag]]):
        # pass 1 — every definition becomes a node, indexed by name.
        for rel, tags in per_file.items():
            self.g.add_node(rel, ntype="file", rel=rel, line=1)
            for t in tags:
                if t.kind == "def":
                    nid = f"{rel}:{t.name}:{t.line}"
                    self.g.add_node(nid, ntype="symbol", rel=rel, name=t.name,
                                    subkind=t.subkind, line=t.line,
                                    span=(t.start_byte, t.end_byte))
                    self.defs_by_name.setdefault(t.name, []).append(nid)

        # pass 2 — resolve references to definitions, tagging each edge's
        # confidence. This is the whole epistemic game.
        for rel, tags in per_file.items():
            defs = [t for t in tags if t.kind == "def"]
            for t in tags:
                if t.kind == "ref":
                    src = self._enclosing(rel, defs, t.start_byte)
                    self._resolve_edge(src, rel, t)
                elif t.kind == "rationale":
                    src = self._enclosing(rel, defs, t.start_byte)
                    rid = f"{rel}#{t.name}"
                    self.g.add_node(rid, ntype="rationale", rel=rel,
                                    line=t.line, text=t.text, subkind=t.subkind)
                    self.g.add_edge(src, rid, etype="rationale", conf=EXTRACTED)

    def _enclosing(self, rel: str, defs: list[Tag], byte: int) -> str:
        """The innermost definition whose byte-span contains `byte` — the symbol a
        reference lives inside. Falls back to the file node for module-level code."""
        best, best_size = None, None
        for d in defs:
            if d.start_byte <= byte < d.end_byte:
                size = d.end_byte - d.start_byte
                if best_size is None or size < best_size:
                    best, best_size = d, size
        return f"{rel}:{best.name}:{best.line}" if best else rel

    def _resolve_edge(self, src: str, src_rel: str, ref: Tag):
        candidates = self.defs_by_name.get(ref.name, [])
        if not candidates:
            return                                     # external / unresolved symbol
        same_file = [c for c in candidates if self.g.nodes[c]["rel"] == src_rel]
        if same_file:
            # visible in the same translation unit — structural, not a guess.
            for tgt in same_file:
                if tgt != src:
                    self.g.add_edge(src, tgt, etype=ref.subkind, conf=EXTRACTED)
        elif len(candidates) == 1:
            self.g.add_edge(src, candidates[0], etype=ref.subkind, conf=INFERRED)
        else:
            # the name matched several defs across files — say so, link all.
            for tgt in candidates:
                self.g.add_edge(src, tgt, etype=ref.subkind, conf=AMBIGUOUS,
                                candidates=len(candidates))

    def _rank(self, personalization: dict | None = None):
        if self.g.number_of_nodes() == 0:
            self.ranks = {}
            return
        try:
            self.ranks = nx.pagerank(self.g, personalization=personalization,
                                     max_iter=200)
        except (nx.PowerIterationFailedConvergence, ZeroDivisionError):
            # disconnected/degenerate graph — fall back to degree centrality.
            self.ranks = {n: self.g.degree(n) for n in self.g.nodes}

    # ---- query log (Phase 9's dataset, written from day one) ---------------
    def _log(self, kind: str, question: str, nodes: int, tokens: int, dur_ms: float):
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(), "kind": kind, "question": question,
                    "nodes": nodes, "tokens": tokens, "dur_ms": round(dur_ms, 1),
                }) + "\n")
        except OSError:
            pass   # a query must never fail because its log line couldn't be written

    # ---- lookups -----------------------------------------------------------
    def find_symbol(self, name: str) -> list[str]:
        """Node ids for a symbol: exact name first, then case-insensitive
        substring, each ordered by rank (most central first)."""
        exact = self.defs_by_name.get(name, [])
        if not exact:
            low = name.lower()
            exact = [nid for n, ids in self.defs_by_name.items()
                     if low in n.lower() for nid in ids]
        return sorted(exact, key=lambda n: self.ranks.get(n, 0), reverse=True)

    def god_nodes(self, k: int = 5) -> list[str]:
        syms = [n for n, d in self.g.nodes(data=True) if d.get("ntype") == "symbol"]
        return sorted(syms, key=lambda n: self.ranks.get(n, 0), reverse=True)[:k]


# ─────────────────────────────────────────────
# Token-budgeted rendering — applied PER QUERY RESPONSE, never to a map injected
# into every request. Greedy line fit against an approx char/4 token estimate.
# ─────────────────────────────────────────────
def _approx_tokens(text: str) -> int:
    return len(text) // 4 + 1


def _fit(lines: list[str], budget_tokens: int) -> tuple[str, int]:
    out, used = [], 0
    for ln in lines:
        cost = _approx_tokens(ln)
        if used + cost > budget_tokens and out:
            out.append(f"… (+{len(lines) - len(out)} more lines trimmed to fit budget)")
            break
        out.append(ln)
        used += cost
    body = "\n".join(out)
    return body, _approx_tokens(body)


def _card(rm: RepoMap, nid: str) -> list[str]:
    d = rm.g.nodes[nid]
    pct = 0
    if rm.ranks:
        order = sorted(rm.ranks.values(), reverse=True)
        pct = int(100 * (1 - order.index(rm.ranks.get(nid, 0)) / max(len(order), 1)))
    lines = [f"{d.get('name', nid)}  ({d.get('subkind','?')})  "
             f"{d['rel']}:{d['line']}   rank≈{pct}th pct"]
    callers = [(p, rm.g.edges[p, nid]) for p in rm.g.predecessors(nid)]
    callees = [(s, rm.g.edges[nid, s]) for s in rm.g.successors(nid)]
    rats = [rm.g.nodes[s] for s in rm.g.successors(nid)
            if rm.g.nodes[s].get("ntype") == "rationale"]
    if callers:
        lines.append("  callers:")
        for p, e in callers[:8]:
            pd = rm.g.nodes[p]
            lines.append(f"    ← {pd.get('name', p)} ({pd['rel']}:{pd.get('line','?')}) "
                         f"[{e.get('etype','?')}/{e.get('conf','?')}]")
    if [c for c in callees if rm.g.nodes[c[0]].get('ntype') == 'symbol']:
        lines.append("  calls:")
        for s, e in callees[:8]:
            sd = rm.g.nodes[s]
            if sd.get("ntype") != "symbol":
                continue
            lines.append(f"    → {sd.get('name', s)} ({sd['rel']}:{sd.get('line','?')}) "
                         f"[{e.get('etype','?')}/{e.get('conf','?')}]")
    if rats:
        lines.append("  rationale:")
        for r in rats:
            lines.append(f"    {r['subkind'].upper()} (line {r['line']}): {r['text']}")
    return lines


# ─────────────────────────────────────────────
# The process-level index cache — one RepoMap per repo root, built lazily on the
# first tool call and reused. A fresh process re-parses only changed files (the
# mtime cache), which is the `--update` story without a flag.
# ─────────────────────────────────────────────
_MAPS: dict[str, RepoMap] = {}


def get_map(root: str = ".") -> RepoMap:
    key = str(Path(root).resolve())
    if key not in _MAPS:
        _MAPS[key] = RepoMap(Path(root)).build()
    return _MAPS[key]


def _reset_cache():          # test seam
    _MAPS.clear()
    _QUERY_CACHE.clear()


# ─────────────────────────────────────────────
# THE THREE TOOLS — the index is a tool, not context (SDE-3 anti-pattern #2:
# shipping the map as context silently taxes every request forever).
# ─────────────────────────────────────────────
@tool(kind=ToolKind.READ)
def explain(symbol: str):
    """Structural card for a symbol: where it's defined, who calls it, what it
    calls, any WHY/NOTE/HACK rationale attached, and how central it is. Prefer
    this over reading a whole file when you only need a symbol's shape and
    connections."""
    t0 = time.perf_counter()
    rm = get_map()
    hits = rm.find_symbol(symbol)
    if not hits:
        rm._log("explain", symbol, 0, 0, (time.perf_counter() - t0) * 1000)
        return f"No symbol matching '{symbol}' in the repo map ({rm.n_files} files indexed)."
    lines = _card(rm, hits[0])
    if len(hits) > 1:
        lines.append(f"  (+{len(hits) - 1} other definitions share this name — "
                     f"ambiguous; showing the most central)")
    body, toks = _fit(lines, 700)
    rm._log("explain", symbol, len(hits), toks, (time.perf_counter() - t0) * 1000)
    return body


@tool(kind=ToolKind.READ)
def path(a: str, b: str):
    """How are two symbols connected? Returns the chain of typed edges linking
    them (with confidence tags). Answers a question grep cannot express at all:
    two symbols several hops apart that share no lexical token."""
    t0 = time.perf_counter()
    rm = get_map()
    src, dst = rm.find_symbol(a), rm.find_symbol(b)
    if not src or not dst:
        missing = a if not src else b
        rm._log("path", f"{a}~{b}", 0, 0, (time.perf_counter() - t0) * 1000)
        return f"Can't route: no symbol matching '{missing}' in the repo map."
    s, d = src[0], dst[0]
    undirected = rm.g.to_undirected(as_view=True)
    try:
        nodes = nx.shortest_path(undirected, s, d)
    except nx.NetworkXNoPath:
        rm._log("path", f"{a}~{b}", 0, 0, (time.perf_counter() - t0) * 1000)
        return (f"No structural path between '{a}' and '{b}' — they are in "
                f"disconnected parts of the graph.")
    lines = [f"path: {a} → {b}  ({len(nodes) - 1} hops)"]
    for u, v in zip(nodes, nodes[1:]):
        e = rm.g.edges.get((u, v)) or rm.g.edges.get((v, u)) or {}
        un, vn = rm.g.nodes[u], rm.g.nodes[v]
        lines.append(f"  {un.get('name', u)} ({un['rel']}:{un.get('line','?')}) "
                     f"--{e.get('etype','?')}/{e.get('conf','?')}--> "
                     f"{vn.get('name', v)} ({vn['rel']}:{vn.get('line','?')})")
    body, toks = _fit(lines, 700)
    rm._log("path", f"{a}~{b}", len(nodes), toks, (time.perf_counter() - t0) * 1000)
    return body


@tool(kind=ToolKind.READ)
def query(question: str):
    """Ask the repo map a scoped question in English (e.g. 'where is rate
    limiting handled?'). Seeds PageRank on symbols whose names overlap your words
    and returns the most central matching region — a token-budgeted subgraph, not
    the whole map."""
    t0 = time.perf_counter()
    rm = get_map()
    want = _word_tokens(question, drop_stop=True)
    seeds = {}
    for name, ids in rm.defs_by_name.items():
        overlap = len(want & _word_tokens(name))
        if overlap:
            for nid in ids:
                seeds[nid] = seeds.get(nid, 0) + overlap
    if not seeds:
        rm._log("query", question, 0, 0, (time.perf_counter() - t0) * 1000)
        return (f"No symbols matched '{question}'. Try explain(<symbol>) or a "
                f"grep — this repo map indexes {rm.n_files} files by symbol name.")
    # personalized PageRank biased toward the seed symbols — importance AS SEEN
    # from the question, not globally.
    pers = {n: 0.0 for n in rm.g.nodes}
    for nid, w in seeds.items():
        pers[nid] = float(w)
    try:
        ranked = nx.pagerank(rm.g, personalization=pers, max_iter=200)
    except (nx.PowerIterationFailedConvergence, ZeroDivisionError):
        ranked = {n: float(seeds.get(n, 0)) for n in rm.g.nodes}
    # SCOPE to the seeds and their immediate neighbourhood — a query should
    # return the region around the match, not the repo's global god-nodes that
    # personalized rank still leaks mass into.
    scope = set(seeds)
    for nid in seeds:
        scope.update(rm.g.predecessors(nid))
        scope.update(rm.g.successors(nid))
    top = sorted((n for n in scope
                  if rm.g.nodes[n].get("ntype") == "symbol"),
                 key=lambda n: (n in seeds, ranked.get(n, 0)), reverse=True)[:10]
    lines = [f"query: {question}  ({len(seeds)} seed symbols)"]
    for nid in top:
        d = rm.g.nodes[nid]
        star = " ◀ match" if nid in seeds else "   (neighbour)"
        lines.append(f"  {d['name']} ({d.get('subkind','?')})  "
                     f"{d['rel']}:{d['line']}{star}")
    body, toks = _fit(lines, 800)
    rm._log("query", question, len(top), toks, (time.perf_counter() - t0) * 1000)
    return body


# ─────────────────────────────────────────────
# CLI — `python -m agent.repomap [root] [--stats] [--update]`. --update just
# rebuilds; the mtime cache means only changed files are re-parsed. Lets the
# Phase 5.5 gate be run by hand: path() vs the grep loop on a foreign repo.
# ─────────────────────────────────────────────
def _main(argv):
    import sys
    args = [a for a in argv if not a.startswith("--")]
    root = args[0] if args else "."
    _reset_cache()
    t0 = time.perf_counter()
    rm = get_map(root)
    dur = (time.perf_counter() - t0) * 1000
    print(f"repo map: {rm.n_files} files · {rm.g.number_of_nodes()} nodes · "
          f"{rm.g.number_of_edges()} edges · built in {dur:.0f}ms")
    if "--stats" in argv:
        confs: dict[str, int] = {}
        for *_e, data in rm.g.edges(data=True):
            confs[data.get("conf", "?")] = confs.get(data.get("conf", "?"), 0) + 1
        print("edge confidence:", confs)
        print("god nodes (most central symbols):")
        for nid in rm.god_nodes(8):
            d = rm.g.nodes[nid]
            print(f"  {d['name']:30s} {d['rel']}:{d['line']}")
    return rm


if __name__ == "__main__":
    import sys
    _main(sys.argv[1:])
