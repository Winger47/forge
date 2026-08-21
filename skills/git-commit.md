---
name: git-commit
description: how to write a clear git commit message
---

# Writing a commit message

Format:

```
<subject: imperative, <=50 chars, no trailing period>

<body: wrap at 72 cols; explain WHY, not what the diff already shows>
```

Rules:

- Subject in the imperative mood: "Add retry to gateway", not "Added" or "Adds".
- The diff shows *what* changed; the body explains *why* — the problem it solves,
  the constraint that forced this approach, anything a future reader would wonder.
- One logical change per commit. If the subject needs an "and", it's two commits.
- Reference an issue if there is one; don't invent one.

Before committing: run the tests and the linter, and read your own diff once.
