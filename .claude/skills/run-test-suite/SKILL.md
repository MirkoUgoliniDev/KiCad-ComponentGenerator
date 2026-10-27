---
name: run-test-suite
description: Run the pytest suite of this repository the way it is verified here — focused files first, then the full suite in two separate processes. Use it before reporting a change as done, before a commit and whenever CLAUDE.md or HANDOFF.md counts must be updated.
---

# Running the test suite

## 1. Focused tests first

Run the files that cover what you changed, with a fresh `--basetemp` each
time (a reused one can hold files of a previous run):

```
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/claude-test-<topic> tests/test_<area>.py
```

Always add `tests/test_documentation.py` when you touched production code,
tools, examples or `main.py`: it enforces the Doxygen `@brief` docstrings and
the English-only `#` comments.

## 2. Full suite: two halves, never one process

The single-process run can die at random with a native Qt/VTK fail-fast
(0xC0000409, no traceback; HANDOFF point 113). Use the splitter, which runs
each half in its own interpreter:

```
.venv/Scripts/python.exe tools/run_test_halves.py both -- --basetemp=.venv/claude-test-full
.venv/Scripts/python.exe tools/run_test_halves.py first --list     # see the partition
```

The split is by file name at `test_photo_detector_ui.py`, computed at run
time, so a new test file always lands in exactly one half. The halves change
size as files are added: **the sum is what counts**. A full run takes
several minutes: start it in the background and wait for the notification.

## 3. Report the numbers exactly

Report per half and in total: passed, skipped, expected failures, subtests,
failed. Write them as in HANDOFF, e.g. `A 5760/10/19, B 3601/2, 0 falliti`.
A failure is reported with its output, never retried until green. A flaky
test is a defect to diagnose (find the root cause, as for point 113), not a
reason to rerun.

## 4. Extra checks by area

- KiCad packaging: `tests/test_kicad_library.py`, the four-language catalog
  tests (`tests/test_i18n.py`), KiCad CLI read-only export when installed.
- Installer or build: follow the `installer-change` skill.
- Canvas (symbol editor, footprint preview): the `test_canvas_*.py` unit
  tests and both `test_symbol_*_parity.py` / `test_footprint_*_parity.py`.

## 5. After a full green run

When the run precedes a commit, update the counts at the top of HANDOFF.md
and the baseline sentence in `.claude/CLAUDE.md` («is N passed, M skipped
and K subtests»), keeping the two aligned.
