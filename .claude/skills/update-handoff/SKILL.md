---
name: update-handoff
description: Keep HANDOFF.md aligned with the work in progress — write the plan before touching code, record each substantial state change, and realign hashes and test counts after a commit. Use it at the start of every change the user authorises and whenever the state changes.
---

# Keeping HANDOFF.md current

User rule, binding (`.claude/CLAUDE.md`, «Handoff sempre aggiornato»):
whoever resumes must not have to rebuild anything from the conversation.
HANDOFF.md is written in Italian; the newest section goes **at the top**,
right under `# STATO ATTUALE (aggiornare a ogni commit)`.

## 1. Before touching code

When the user says to apply changes, first add a section:

```
## GG/MM — IN CORSO: <what, in a few words>

Utente: «<their words>» -> <decision>. Branch `<branch>`, base `<hash>`.
Da fare, in quest'ordine: ...
Interpretazioni e dubbi: ...
```

Quote the user verbatim in «…». List what will be done, the decisions and
interpretations, the open doubts. Then change the code.

## 2. While working

Update the section as soon as the state changes in a substantial way:
a piece completed, a diagnosis that overturns the previous one, a decision
by the user, a defect found and not fixed. Change the title marker:
`IN CORSO` -> `FATTO, NON committato (attende OK)` -> `FATTO e committato
\`<hash>\` su <branch> (NON pushato, NON mergiato)`.

A wrong diagnosis is corrected in place, saying that it was wrong and why:
a wrong diagnosis left there is worse than none.

## 3. Minimum content

- branch and last commit;
- suite result (see the `run-test-suite` skill for the format);
- the chain of causes of the problems faced;
- open work split into **requested by the user** and **reported but never
  authorised**;
- decisions taken by the user;
- operational traps that would waste the next person's time.

## 4. After a commit

The commit hash is known only after committing: record it in a second
commit, `docs: handoff with the commit hash`. If the suite was run, realign
the counts at the top and the baseline in `.claude/CLAUDE.md`. Never rewrite
historical sections to follow a later rename; only the current state moves.

## 5. Before a foreseeable interruption

End of credit, end of session, change of topic: the handoff must already be
valid. Write it before answering the user's last question, not after.
