---
name: document-extraction-change
description: Change how KiCadGen reads datasheets and mechanical drawings — the AI tool schemas and prompts of kicadgen/services/datasheet_extractor.py, pinout and pin numbering, single-part pad positions and dimension chains, the drill holes found in the drawing (drawing_holes.py), the provider calls (openrouter.py, providers.py) and the analysis profiles. Use it for any change to what the analysis asks, accepts or derives from documents.
---

# Changing the document extraction

The analysis turns datasheet pages and dimensioned drawings into the
parameters the generators use: body sizes, pads, pinout. A mistake here
becomes a wrong footprint that still looks plausible, so every value keeps
its evidence and nothing estimated is passed off as measured.

## 1. Read first

- `kicadgen/services/datasheet_extractor.py`: tool schemas
  (`_single_component_tool`, `_pad_positions_tool`, `_pin_numbering_tool`, the
  main extraction schema around the `pinout` property), answer conversion
  (`single_answer_to_data`), chain sums (`pads_from_chains`), the
  readings settled against the drawing holes (`_settle_by_holes`,
  `apply_dedicated_pads`, `apply_dedicated_numbering`).
- `kicadgen/services/drawing_holes.py`: hole GEOMETRY found in the drawing
  image, never numbering.
- `kicadgen/generators/symbol_pins.py` (`pinout_on_pads`): the symbol pins
  are exactly the footprint pads; the pinout only NAMES them.
- `kicadgen/services/openrouter.py` (DeepSeek strict mode, `strict_tool`,
  `_STRICT_REFUSED_STATUSES`), `kicadgen/services/providers.py`.
- `kicadgen/services/analysis_profiles.py` (`SCHEMA_VERSION`): the profile
  fields (models, prompts) stored in `data/analysis_profiles.sqlite3`.

## 2. Rules that changes must keep

- **Generalise** (binding user rule, see `.claude/CLAUDE.md`): no rule on part
  names, brands or package names; no threshold moved until the current part
  passes. What the documents cannot tell is asked of the model (a schema
  field) or of the user. Run the `generality-auditor` agent on the diff.
- **Pin numbers are text.** Pads are numbered «1», «11», «A1», «B12», «EP»:
  a schema that declares `pin` as `integer` makes a compliant model drop or
  renumber alphanumeric pins (T7NS5D4-12 relay, 2026-11-03: A1/A2 lost).
  Compare numbers as stripped strings, never as ints.
- **Pitch is not row spacing**, body height is not row spacing; each value is
  read separately, with its unit (`drawing_unit`), and converted by the program.
- **A model reading is not a measure.** Pad positions summed from transcribed
  dimension chains are adopted only with an independent agreeing reading or
  holes found in the drawing; unconfirmed or disagreeing readings block
  generation with a remedy, they are never averaged into a result.
- **Validate every answer semantically**: finite non-boolean numbers, valid
  rectangles, expected IDs complete and unique, declared views. An incomplete
  answer is never stored as a success; valid parts of a partial answer are kept.
- **Strict mode**: a DeepSeek tool schema is sent strict only when it converts
  losslessly (every field nullable); a 400/404/422 resends it normally. A new
  schema keyword must be checked against that conversion
  (`tests/test_openrouter.py`).
- **Prompts and schemas are product decisions**: change them only when the
  task is about extraction, never as a side effect of packaging or GUI work.
  A new profile field bumps `SCHEMA_VERSION` with a migration, and the profile
  database is committed with the change (`pre-commit-check`).

## 3. Paid calls versus offline tests

Every analysis call costs money; document answers are not cached. Tests pass
a fake `call=` (see `read_pad_positions`, `read_pin_numbering`) and never
reach the network: `tests/conftest.py` blocks non-loopback traffic unless a
test is marked `allow_network`. A real call needs the user's agreement for
that run; say which one you ran.

## 4. Verify

- Focused: `tests/test_datasheet_extractor.py`, `tests/test_extraction_*.py`,
  `tests/test_drawing_holes*.py`, `tests/test_symbol_pins_from_pads.py`,
  `tests/test_pinout_edit.py`, `tests/test_openrouter.py`,
  `tests/test_analysis_profiles.py`, plus `tests/test_documentation.py`.
- Each rule gets a positive and a negative case that differ for a
  structural reason (e.g. an alphanumeric pad set vs a numeric one), not a
  number tuned to the part on screen.
- When a user-visible message changes, follow `add-ui-text`.
- Then the full suite (`run-test-suite`) and `update-handoff`.
