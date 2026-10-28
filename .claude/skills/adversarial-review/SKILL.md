---
name: adversarial-review
description: Run a full or partial adversarial review of KiCadGen with read-only agents on disjoint file scopes, consolidate their findings, verify the severe ones and report before fixing anything. Use it when the user asks for a review, an audit, a «verifica avversariale» or «scatena agenti esperti», and before a release.
---

# Adversarial review with agents

The user's expectation (many rounds in HANDOFF.md): a review finds defects
with proof, ranked by severity; fixes come **after** the user has seen the
list and said which ones to fix. Agents work in parallel on **disjoint**
files; the coordinator integrates. Never let two agents modify the same
file; reviewers modify nothing at all.

## 1. Before launching

- Write the plan in HANDOFF.md (`update-handoff` skill): base commit, scopes,
  «phase 1 = report only».
- Inventory the code: `git ls-files 'kicadgen/**/*.py' | xargs wc -l | sort -rn`.
  A 20 000-line file is split by **line ranges** between two reviewers.

## 2. Scopes (10 for the whole codebase, fewer for a partial one)

| Scope | Agent | Files |
|---|---|---|
| GUI A / GUI B | `adversarial-reviewer` (lens gui) | `kicadgen/gui/main_window.py` split by line range |
| GUI other | `adversarial-reviewer` (lens gui) | the rest of `kicadgen/gui/` except canvas, symbol tab, footprint preview |
| Canvas | `canvas-parity-reviewer` | `canvas_*.py`, `drawing_transform.py`, `symbol_tab.py`, footprint preview, parity tests |
| Services | `adversarial-reviewer` (lens services) | `kicadgen/services/` except photo, kicad_library, component_check |
| Photo | `generality-auditor` | `kicadgen/services/photo_*.py`, SMD classifier, YOLO models |
| Generators + core | `adversarial-reviewer` (lens generators) | `kicadgen/generators/`, `kicadgen/core/` (kicad-cli read-only checks) |
| cad3d | `adversarial-reviewer` (lens cad3d) | `kicadgen/cad3d/` |
| KiCad delivery | `kicad-workflow-reviewer` | `kicad_library.py`, `component_check.py`, their tests |
| Packaging / CI / harness | `adversarial-reviewer` (lens packaging) | `main.py`, `app_paths.py`, `crash_report.py`, `packaging/`, `scripts/`, `tools/`, `.github/`, `tests/conftest.py`, `tests/qt_fixtures.py`, requirements, notices, READMEs |

Launch them in **one** message so they run concurrently. Each brief names
the exact files, the lens, the forbidden actions, the output format and the
limit (15 findings). Brief them as peers: what is already known (open items
in HANDOFF), what to ignore.

## 3. While they run

Do not read or edit files inside their scopes. Work on `.claude/`, docs
outside the scopes, or wait. Never predict their results.

## 4. Consolidation

1. Collect the reports; merge duplicates; keep the stricter severity.
2. **Verify every critical/high finding yourself** (run the test or snippet
   the agent gave; if it gave none, build the reproduction). An agent handed
   a hypothesis tends to confirm it: a finding without a reproduction is
   SUSPECTED, say so.
3. Drop findings that contradict a user decision recorded in HANDOFF.md
   (quote the decision) or that are style only.
4. Write the consolidated list in HANDOFF.md: per finding severity, file,
   scenario, status (confirmed/suspected), and whether it is in the user's
   request or «reported, not authorised».

## 5. Report to the user (Italian)

Ranked table, confirmed first, with counts per severity, the categories that
came out clean, and the tests run. Then **one** question: which findings to
fix (all, by severity, or a list). Fixes start only after that answer, with
the `update-handoff` and `pre-commit-check` skills and, for each fix, a
positive and a negative test; agents fixing in parallel get disjoint files.
