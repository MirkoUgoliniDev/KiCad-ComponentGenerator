---
paths:
  - "kicadgen/services/datasheet_extractor.py"
  - "kicadgen/services/drawing_holes.py"
  - "kicadgen/services/library_footprints.py"
  - "kicadgen/services/openrouter.py"
  - "kicadgen/services/providers.py"
  - "kicadgen/services/analysis_profiles.py"
  - "kicadgen/generators/symbol_pins.py"
---

# Document extraction (datasheets and dimensioned drawings)

Facts that every change here must keep. Procedure, files to read first and the
focused tests: `document-extraction-change` skill. Run the `generality-auditor`
agent on the diff before a commit.

- **A model reading is not a measure.** Pad positions are adopted only with an
  independent agreeing reading or a geometry witness (holes drawn in the
  document, KiCad library footprint). Unconfirmed or disagreeing readings block
  generation with a remedy; they are never averaged into a result.
- **One reading's mistake must not judge another reading.** Whatever a witness
  compares a reading with (body size, view, unit) is either a printed fact or
  that reading's own claim, never a value taken from a competing reading.
- **The drawn holes see geometry, never numbering.** Two pad numbers exchanged
  on the same positions pass the hole check: the note says the numbering is
  unverified.
- **Pin numbers are text** («1», «A1», «EP»): compared as stripped strings,
  never as integers; a schema never declares `pin` as `integer`.
- **Pitch is not row spacing**, body height is not row spacing: each value is
  read separately, with its unit (`drawing_unit`), and converted by the program.
- **Validate every answer semantically**: finite non-boolean numbers, valid
  rectangles, expected IDs complete and unique, declared views. An incomplete
  answer is never stored as a success; valid parts of a partial answer are kept.
- **Prompts and schemas are product decisions**: changed only when the task is
  about extraction. A new profile field bumps `SCHEMA_VERSION` with a
  migration, and `data/analysis_profiles.sqlite3` is committed with the change.
- **Every analysis call costs money** and document answers are not cached.
  Tests pass a fake `call=` and never reach the network; a real call needs the
  user's agreement for that run.
- **No rule on part names, brands or package names; no threshold moved until
  the part on screen passes** (binding user rule, `.claude/CLAUDE.md`
  «Correzioni»). What the documents cannot tell is asked of the model (a
  schema field) or of the user.
