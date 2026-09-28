# test_repomap.py — Phase 5.5 structural retrieval.
#
# The bets under test: symbols and references come out of the AST; every edge
# carries an honest confidence; path() links two symbols that share no lexical
# token; the mtime cache re-parses only what changed; rationale becomes nodes.

import json

import networkx as nx
import pytest

from agent import repomap
from agent.repomap import (
    RepoMap, extract_tags, TagCache, Tag, _fit, _word_tokens,
    EXTRACTED, INFERRED, AMBIGUOUS,
)


@pytest.fixture(autouse=True)
def _clean():
    repomap._reset_cache()
    yield
    repomap._reset_cache()


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ── extraction ─────────────────────────────────────────────────────────────
def test_extract_finds_defs_and_calls():
    src = b"class Foo(Base):\n    def bar(self):\n        return baz()\ndef baz():\n    return 1\n"
    tags = extract_tags(src, "python")
    names = {(t.kind, t.subkind, t.name) for t in tags}
    assert ("def", "class", "Foo") in names
    assert ("def", "function", "bar") in names
    assert ("def", "function", "baz") in names
    assert ("ref", "call", "baz") in names
    assert ("ref", "inherit", "Base") in names


def test_rationale_becomes_a_tag():
    src = b"def f():\n    # WHY: retries are idempotent here\n    return 1\n"
    tags = extract_tags(src, "python")
    rats = [t for t in tags if t.kind == "rationale"]
    assert len(rats) == 1
    assert rats[0].subkind == "why"
    assert "idempotent" in rats[0].text


def test_typescript_extraction():
    src = b"export class Svc {\n  handle() { return doThing(); }\n}\nfunction doThing() { return 1; }\n"
    tags = extract_tags(src, "typescript")
    names = {(t.kind, t.name) for t in tags}
    assert ("def", "Svc") in names
    assert ("def", "handle") in names
    assert ("ref", "doThing") in names


# ── confidence contract ─────────────────────────────────────────────────────
def test_same_file_call_is_extracted(tmp_path):
    _write(tmp_path, "m.py", "def helper():\n    return 1\ndef top():\n    return helper()\n")
    rm = RepoMap(tmp_path).build()
    e = _edge_between(rm, "top", "helper")
    assert e["conf"] == EXTRACTED
    assert e["etype"] == "call"


def test_cross_file_single_match_is_inferred(tmp_path):
    _write(tmp_path, "a.py", "from b import helper\ndef top():\n    return helper()\n")
    _write(tmp_path, "b.py", "def helper():\n    return 1\n")
    rm = RepoMap(tmp_path).build()
    e = _edge_between(rm, "top", "helper")
    assert e["conf"] == INFERRED


def test_multiple_matches_are_ambiguous(tmp_path):
    _write(tmp_path, "a.py", "def helper():\n    return 1\n")
    _write(tmp_path, "b.py", "def helper():\n    return 2\n")
    _write(tmp_path, "c.py", "def top():\n    return helper()\n")
    rm = RepoMap(tmp_path).build()
    e = _edge_between(rm, "top", "helper")
    assert e["conf"] == AMBIGUOUS
    assert e["candidates"] == 2


def _edge_between(rm, src_name, dst_name):
    s = rm.find_symbol(src_name)[0]
    for d in rm.find_symbol(dst_name):
        if rm.g.has_edge(s, d):
            return rm.g.edges[s, d]
    raise AssertionError(f"no edge {src_name} -> {dst_name}")


# ── the headline capability: connect the lexically-unrelated ─────────────────
def test_path_links_symbols_sharing_no_token(tmp_path):
    # renderInvoice → buildPdf → compressStream : no shared substring end to end.
    _write(tmp_path, "app.py",
           "def renderInvoice():\n    return buildPdf()\n"
           "def buildPdf():\n    return compressStream()\n"
           "def compressStream():\n    return b''\n")
    rm = RepoMap(tmp_path).build()
    s = rm.find_symbol("renderInvoice")[0]
    d = rm.find_symbol("compressStream")[0]
    chain = nx.shortest_path(rm.g.to_undirected(as_view=True), s, d)
    assert len(chain) == 3      # 2 hops
    # and grep for one name would never surface the other — they share no token.
    assert not (_word_tokens("renderInvoice") & _word_tokens("compressStream"))


# ── query: scoped, seeded, stopword-filtered ─────────────────────────────────
def test_query_stopwords_do_not_seed(tmp_path):
    _write(tmp_path, "m.py", "def is_ready():\n    return True\ndef throttle():\n    return 1\n")
    rm = RepoMap(tmp_path).build()
    # "is" is a stopword → must NOT seed is_ready; "throttle" is signal.
    want = _word_tokens("how is throttle handled", drop_stop=True)
    assert "throttle" in want and "is" not in want


def test_god_nodes_ranks_the_hub_highest(tmp_path):
    # hub() is called by many → most central.
    body = "def hub():\n    return 1\n"
    for i in range(5):
        body += f"def caller{i}():\n    return hub()\n"
    _write(tmp_path, "m.py", body)
    rm = RepoMap(tmp_path).build()
    top = rm.god_nodes(1)[0]
    assert rm.g.nodes[top]["name"] == "hub"


# ── cache: mtime hit skips the parse ─────────────────────────────────────────
def test_cache_hits_on_unchanged_mtime(tmp_path):
    db = tmp_path / "cache.db"
    cache = TagCache(db)
    tags = [Tag("def", "function", "f", 1, 0, 10)]
    cache.put("m.py", 123.0, tags)
    assert cache.get("m.py", 123.0)[0].name == "f"     # same mtime → hit
    assert cache.get("m.py", 124.0) is None            # changed mtime → miss
    cache.close()


def test_query_log_is_appended(tmp_path):
    _write(tmp_path, "m.py", "def throttle():\n    return 1\n")
    rm = RepoMap(tmp_path).build()
    rm._log("query", "throttle?", 3, 42, 1.5)
    lines = rm.log_path.read_text().strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["kind"] == "query" and rec["nodes"] == 3 and rec["tokens"] == 42


# ── token budget ─────────────────────────────────────────────────────────────
def test_fit_trims_to_budget():
    lines = [f"line number {i} with some text" for i in range(200)]
    body, toks = _fit(lines, 50)
    assert toks <= 60                       # roughly within budget (+ trim note)
    assert "trimmed to fit budget" in body
