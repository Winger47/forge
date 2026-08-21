# skills.py — on-demand skill loading (Phase 5).
#
# A skill is a recurring instruction extracted to a markdown file and loaded INTO
# CONTEXT ONLY WHEN NEEDED — not pinned to every request. The bet (FORGE.md
# §Phase-5 SDE-3): a good one-line trigger lets the model decide when to pull a
# skill, so we pay its token cost on the ~5% of turns that need it instead of the
# 100% an always-on system prompt would.
#
# Format: a markdown file with a small `---` frontmatter header:
#
#     ---
#     name: code-review
#     description: how to review a diff for correctness and clarity
#     ---
#     <the instructions...>
#
# The registry lists (name, description) so the model sees what it CAN load; the
# `load_skill` tool pulls the body on demand. User skills in ~/.forge/skills/
# extend the builtin set exactly like user tools extend builtin tools.

from pathlib import Path

from agent.tools import tool, ToolKind

BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"
USER_SKILLS_DIR = Path.home() / ".forge" / "skills"

_SKILLS: dict[str, dict] = {}      # name -> {"description", "path"}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split '---' frontmatter (simple key: value lines) from the markdown body.
    No YAML dependency — the header is deliberately trivial."""
    meta: dict = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            header = text[3:end].strip()
            body = text[end + 4:].lstrip("\n")
            for line in header.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
    return meta, body


def discover_skills() -> dict[str, dict]:
    """(Re)scan builtin + user skill dirs. User skills override builtin ones of
    the same name (user's machine, user's call). Returns the registry."""
    _SKILLS.clear()
    for d in (BUILTIN_SKILLS_DIR, USER_SKILLS_DIR):
        if not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            try:
                meta, _ = _parse_frontmatter(md.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — a broken skill file is skipped, not fatal
                continue
            name = meta.get("name", md.stem)
            _SKILLS[name] = {
                "description": meta.get("description", ""),
                "path": md,
            }
    return _SKILLS


def skills_catalogue() -> str:
    """A compact 'name — trigger' list for the system prompt, so the model knows
    which skills exist without paying to load any of them."""
    if not _SKILLS:
        return ""
    return "\n".join(f"- {n}: {s['description']}" for n, s in _SKILLS.items())


@tool(kind=ToolKind.META)
def load_skill(name: str):
    """Load a named skill's full instructions into context. Call this when the
    current task matches a skill's description (see the SKILLS list). Returns the
    skill's markdown body to follow."""
    skill = _SKILLS.get(name)
    if skill is None:
        available = ", ".join(_SKILLS) or "(none)"
        return f"ERROR: no skill named '{name}'. Available: {available}"
    try:
        _, body = _parse_frontmatter(skill["path"].read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return f"ERROR: could not read skill '{name}': {e}"
    return f"[SKILL: {name}]\n{body}"


discover_skills()      # populate the registry on import so the catalogue is ready
