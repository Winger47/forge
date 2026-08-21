# hooks.py — lifecycle interception points (Phase 2).
#
# The middleware pattern: named points in the run where user code can observe or
# intervene, without editing the loop. Phase 2 wires the POINTS (as no-ops);
# later phases and user config hang real behavior off them — an audit log on
# `after_tool`, a policy veto on `before_tool`, a metrics flush on `after_run`.
#
# The one hook that can change control flow is `before_tool`: a handler returning
# False VETOES the call. Everything else is observe-only. This mirrors Express
# middleware / Django signals: additive, ordered, and default-transparent.

from typing import Callable

# The five points. before_tool is the only vetoing one.
HOOK_POINTS = ("before_run", "after_run", "before_tool", "after_tool", "on_error")


class Hooks:
    """A registry of callbacks per lifecycle point. Empty by default — an
    unconfigured agent behaves exactly as if hooks didn't exist (no-op)."""

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = {p: [] for p in HOOK_POINTS}

    def register(self, point: str, fn: Callable) -> None:
        if point not in self._handlers:
            raise ValueError(f"unknown hook point '{point}' (valid: {HOOK_POINTS})")
        self._handlers[point].append(fn)

    # --- observe-only points: run every handler, swallow handler errors so a
    #     buggy hook can never take down the run it was only watching. ---
    def fire(self, point: str, **payload) -> None:
        for fn in self._handlers.get(point, []):
            try:
                fn(**payload)
            except Exception as e:  # noqa: BLE001 — a hook's crash is not the run's crash
                print(f"   [hook {point} raised: {type(e).__name__}: {e}]")

    # --- the vetoing point: any handler returning False blocks the tool call. ---
    def allow_tool(self, name: str, args: dict) -> bool:
        for fn in self._handlers.get("before_tool", []):
            try:
                if fn(name=name, args=args) is False:
                    return False
            except Exception as e:  # noqa: BLE001
                print(f"   [hook before_tool raised: {type(e).__name__}: {e}]")
        return True
