# runner.py — the eval harness (Phase 1 smoke test).
#
# Runs each golden task in an ISOLATED temp directory so tasks can't see each
# other's files, drives the agent headlessly (auto-approving tool confirms, since
# no human is watching), then scores the side effect. Prints a pass/fail report
# and exits non-zero if anything failed — so Phase 9 can drop this straight into
# CI as a regression gate.
#
# Run it:  python -m eval.runner            (uses the gateway if up, else direct)
#          python -m eval.runner --direct   (force direct-to-provider)

import os
import sys
import shutil
import tempfile
import argparse
from pathlib import Path

from agent.agent import run_agent, DirectClient, GatewayClient, DEFAULT_MODEL
from eval.golden_tasks import GOLDEN_TASKS

MAX_ITERATIONS = 8      # generous for these small tasks; a stuck run trips the loop detector


def run_one(task: dict, client, model: str) -> tuple[bool, str]:
    """Run a single task in a throwaway workdir and return (passed, detail)."""
    workdir = Path(tempfile.mkdtemp(prefix=f"forge-eval-{task['name']}-"))
    prev_cwd = os.getcwd()
    try:
        task["setup"](workdir)
        os.chdir(workdir)          # agent tools resolve paths against cwd

        messages = [{"role": "user", "content": task["goal"]}]
        agent = run_agent(messages, client, max_iterations=MAX_ITERATIONS)

        # Drive the generator to completion. The only input it ever asks for is a
        # confirm on a dangerous tool (write_file) — an unattended eval says yes.
        to_send = None
        while True:
            try:
                event = agent.send(to_send)
            except StopIteration:
                break
            to_send = "yes" if event.type == "confirm_request" else None

        return task["check"](workdir)
    except Exception as e:  # noqa: BLE001 — a crash is a failed task, not a failed run
        return False, f"crashed: {type(e).__name__}: {e}"
    finally:
        os.chdir(prev_cwd)
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="FORGE golden-task eval")
    ap.add_argument("--direct", action="store_true",
                    help="call the provider directly, bypassing the gateway")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args()

    if not os.getenv("GROQ_API_KEY"):
        print("GROQ_API_KEY not set — eval needs a live model. Aborting.")
        return 2

    # GatewayClient degrades to a direct call on its own if the gateway is down,
    # so the default path works whether or not the gateway is running.
    client = (DirectClient(model=args.model) if args.direct
              else GatewayClient(model=args.model))

    print(f"\n  FORGE eval · {len(GOLDEN_TASKS)} golden tasks · model {args.model}\n")
    passed = 0
    for task in GOLDEN_TASKS:
        ok, detail = run_one(task, client, args.model)
        passed += ok
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {task['name']:<16} {detail}")

    score = passed / len(GOLDEN_TASKS)
    print(f"\n  score: {passed}/{len(GOLDEN_TASKS)}  ({score:.0%})\n")
    return 0 if passed == len(GOLDEN_TASKS) else 1


if __name__ == "__main__":
    sys.exit(main())
