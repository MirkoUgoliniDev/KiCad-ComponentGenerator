---
paths:
  - "**/*.py"
---

# Documentation standard — mandatory Doxygen-compatible comments

Moved here from `.claude/CLAUDE.md` («Documentation standard») on 2026-11-10.
`tests/test_documentation.py` enforces it.

All production Python code and maintained examples must use Doxygen-compatible
documentation. Every module, public class, public function, public method and
Qt override must have a docstring beginning with `@brief`. Document parameters
with `@param`, return values with `@return`, raised exceptions with `@throws`,
and important invariants with `@note` where applicable. Private helpers may use
short explanatory docstrings when their behavior is non-obvious.

Comments must explain intent, contracts, safety boundaries or engineering
assumptions; do not restate the implementation line by line. Keep documentation
in English in source code, use precise units for geometry, and update the
docstring whenever behavior, inputs, outputs or side effects change. New public
APIs without Doxygen-compatible documentation are not complete and must not be
merged. Run the documentation test together with the relevant functional tests.

**Comments in English — user rule, binding (2026-10-16).** Every `#` and `#:`
comment in production code, tools, examples and `main.py` is written in
English, exactly like the docstrings. Verbatim user quotes, UI labels and
literal program output stay in Italian inside «…» on ONE line, with an English
gloss next to them. `tests/test_documentation.py`
(`test_comments_are_written_in_english`) enforces it: an Italian comment fails
the suite. Write new comments in English from the start; never translate a
quote or reword English only to satisfy the checker.

New code is always commented, private helpers included; check the comments of
the diff before a commit (`pre-commit-check` skill).
