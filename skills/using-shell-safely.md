---
name: using-shell-safely
description: how to run shell commands without causing damage
---

# Running shell commands safely

The shell tool can do irreversible damage. Before every `run_shell` call:

1. **Prefer read-only.** Use `ls`, `cat`, `grep`, `git status`, `git diff` to
   understand state before you change it.
2. **Never run a destructive command speculatively.** `rm -rf`, `git reset
   --hard`, `git clean -fd`, `> file`, `dd`, and anything piped to `sh` change or
   destroy state. Confirm the exact target first, and prefer the narrowest form
   (`rm one_file.txt`, not `rm -rf .`).
3. **Stay inside the working directory.** Do not `cd /` or operate on absolute
   paths outside the project unless the task explicitly requires it.
4. **Quote paths** that may contain spaces, and avoid globbing that could match
   more than you intend.
5. **One command at a time** when the result of one decides the next — don't
   chain with `&&` across a step you haven't verified.

If a command is destructive and you are not certain, ask the user rather than
guessing.
