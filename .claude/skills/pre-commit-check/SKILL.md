---
name: pre-commit-check
description: Checks to run before every commit in this repository — explicit user OK, secrets, profile database included, English comments in the diff, docs and handoff aligned. Use it each time a commit is about to be made.
---

# Before committing

## 1. Authorisation

Commit, push and merge only after the user's explicit OK **for this round**.
An OK covers the work present when it was given, not later changes, and an
OK to commit is not an OK to push or merge. Work on the current feature
branch (`ADVANCED`, `FIX`), never directly on `main`.

## 2. What goes in, what stays out

- Never: `.env`, `config.json`, Python caches, `generated/`, `photo-cache`,
  private photos, local KiCad state, `.claude/settings.local.json`,
  `.claude/worktrees/`, scratch files (`*.tmp`, `*.pkl`, `tmp/`, `work/`).
  `git status --short` must show only intended paths.
- Always: `data/analysis_profiles.sqlite3` when it changed, after checking it
  holds no secret:

```
.venv/Scripts/python.exe -c "import sqlite3,re;c=sqlite3.connect('data/analysis_profiles.sqlite3');print([(t,r) for t in [x[0] for x in c.execute(\"select name from sqlite_master where type='table'\")] for r in c.execute(f'select * from {t}') if re.search(r'sk-[A-Za-z0-9-]{16,}|api[_-]?key\W*[:=]', str(r), re.I)])"
```

  The output must be `[]`.
- Scan the staged diff for keys and tokens:
  `git diff --cached | grep -i -E "sk-[a-z0-9-]{16,}|api[_-]?key *[:=]|password *[:=]"`.

## 3. Code quality in the diff

- Every `#` and `#:` comment in production code, tools, examples and
  `main.py` is in English; Italian only for verbatim quotes and UI labels in
  «…» on one line with an English gloss. New code is commented, private
  helpers included when non-obvious.
- Every new public module, class, function, method and Qt override has a
  docstring starting with `@brief`, with `@param` / `@return` / `@throws`.
- `tests/test_documentation.py` passes.

## 4. Documentation aligned

- HANDOFF.md section marked done (`update-handoff` skill); after the commit,
  a second commit records the hash.
- If the full suite ran, the counts at the top of HANDOFF.md and the
  baseline in `.claude/CLAUDE.md` match (`run-test-suite` skill).

## 5. Message

Conventional prefix (`feat:`, `fix:`, `chore:`, `docs:`), an English subject,
a body that says what changed and why, and the attribution line required by
the session.
