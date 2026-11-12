---
paths:
  - "tests/**"
  - "tools/run_test_halves.py"
  - "pytest.ini"
---

# Tests

Facts that every change here must keep. How to run the suite and report the
numbers: `run-test-suite` skill; a red GitHub run: `ci-green` skill.

- **A test that passes only on the observed case is not a verification**
  (binding user rule, `.claude/CLAUDE.md` «Correzioni»): each rule gets a
  positive and a negative case that the rule must tell apart for a structural
  reason, not by numeric coincidence.
- **When a change breaks a test, update the contract it expresses**, having
  understood why it changed; never loosen the assertion until it turns green.
- **A failure is reported with its output**, never retried until green. A
  flaky test is a defect to diagnose, not a reason to rerun.
- **No network, no paid call**: `tests/conftest.py` blocks non-loopback
  traffic unless a test is marked `allow_network`; AI calls are faked through
  the `call=` parameter.
- **No assumption about the machine**: the GitHub runner has no KiCad, no
  FreeCAD and no untracked model weights; a test fakes them or uses the
  suite's own fixtures (hand-written glyph library, faked FreeCAD window
  fixture).
- **Full suite in two processes** (`tools/run_test_halves.py both`), with a
  fresh `--basetemp`: a single process can die with a native Qt/VTK
  fail-fast (HANDOFF point 113).
- **The full suite is valid only on code at rest**: nothing edited and no
  other pytest running meanwhile; a red test is reproduced alone.
- Dialogs are released after `exec()` and pooled work has a shutdown path: an
  object kept alive can crash a later test through the garbage collector.
