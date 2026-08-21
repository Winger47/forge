---
name: code-review
description: how to review a diff or file for correctness, clarity, and risk
---

# Reviewing code

Review in this order and stop early if a blocker appears:

1. **Correctness first.** Does it do what it claims? Look for off-by-one errors,
   unhandled `None`/empty cases, wrong boolean logic, and resource leaks (files,
   connections) that aren't closed on the error path.
2. **Failure paths.** What happens on bad input, a network error, a missing file?
   An error should become a handled result, not an uncaught crash.
3. **Clarity.** Names say what they hold; functions do one thing; no dead code.
   Prefer deleting code to commenting it out.
4. **Tests.** Is the new behavior covered? A bug fix without a regression test
   will come back.

Report findings **most-severe first**. For each: the file:line, what breaks, and
the concrete input that triggers it. Do not pad the review with style nits when a
correctness bug is present — lead with the bug.
