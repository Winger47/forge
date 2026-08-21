# config.py — layered configuration (Phase 2).
#
# One well-defined merge, not a scatter of `if` statements. Four layers, lowest
# to highest priority:
#
#     defaults  <  system (~/.forge/config.toml)  <  project (./.forge/config.toml)  <  CLI
#
# Higher layers override lower ones KEY BY KEY (a project file that sets only
# `model` still inherits `max_iterations` from defaults). Pydantic validates the
# merged result, so a typo'd type (max_iterations = "lots") fails loudly at load,
# not deep in the loop.

import tomllib
from pathlib import Path
from pydantic import BaseModel, Field

# Kept as a literal (not imported from agent.agent) to avoid a circular import;
# agent.agent reads its default FROM here once config is the source of truth.
DEFAULT_MODEL = "openai/gpt-oss-120b"

SYSTEM_CONFIG = Path.home() / ".forge" / "config.toml"
PROJECT_CONFIG = Path(".forge") / "config.toml"


class ForgeConfig(BaseModel):
    model: str = DEFAULT_MODEL
    max_iterations: int = Field(default=10, ge=1)
    max_tokens: int = Field(default=50_000, ge=1)
    stream: bool = True
    gateway_url: str = "http://127.0.0.1:8000/v1"
    # approval policy: on-request | on-failure | auto | never | yolo
    approval_mode: str = "on-request"
    # None = expose every registered tool; a list restricts the run to those names
    allowed_tools: list[str] | None = None


def _read_toml_table(path: Path) -> dict:
    """Read the [forge] table from a TOML file. Missing file or missing table →
    empty dict (that layer simply contributes nothing)."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return {}
    table = data.get("forge", {})
    return table if isinstance(table, dict) else {}


def load_config(
    cli: dict | None = None,
    *,
    project_path: Path | None = None,
    system_path: Path | None = None,
) -> ForgeConfig:
    """Merge the four layers and validate. Paths are injectable so the precedence
    can be tested without touching the real home/project files.

    CLI overrides are filtered to non-None values, so an unset CLI flag doesn't
    clobber a file-provided value with None."""
    system_path = SYSTEM_CONFIG if system_path is None else system_path
    project_path = PROJECT_CONFIG if project_path is None else project_path

    merged: dict = {}
    merged.update(_read_toml_table(system_path))          # system beats defaults
    merged.update(_read_toml_table(project_path))         # project beats system
    if cli:
        merged.update({k: v for k, v in cli.items() if v is not None})  # CLI beats all

    return ForgeConfig(**merged)
