# Side/pinout coherence and the document check — plan (2026-11-13)

Approved by the user as points 3 and 4 of the round after commit of 2026-11-13.
Designed read-only by a Plan agent; not implemented yet.

## Failing case

`generated/ESP32-S3-DEVKIT-NXRX`, analysis 2026-11-13 12:27, `deepseek-api/deepseek-flash`:
`castellated_sides` top 22 from 44 reversed, bottom 22 from 1; `pinout` 1..44. The program kept
the sides' numbers, dropped pinout 23-43 with a log WARNING only, left pads 45-65 unnamed, and
put the bottom row names possibly on the wrong row. «Verifica componente» said «usable with
caveats» because it never sees the document pinout.

Reading each side's labels backwards gives a clean one-to-one match: top = pinout 1..22
(first_pin 1, reverse), bottom = pinout 23..44 (first_pin 23, reverse).

## General rule

1. Side-based module: pad numbers (first_pin, count, reverse) and pinout numbers are ONE
   numbering. Contradiction = a pinout entry with no pad, or pads the pinout does not name.
2. `label_relation(side_label, pinout_name)` -> `same` | `different` | `unknown`, on the primary
   name only (text before the first «(»), upper case. Classes: ground (G, GND, VSS, 0V, *GND),
   supply (`^\d+V\d*$`, `^\d+[.,]\d+V$`, compared as a number), index (bare number, or letters +
   digits). Bare index matches a prefixed one; two different prefixes are `unknown`. DECLARED
   HEURISTIC (common silkscreen abbreviations, no brand names); `unknown` never triggers anything.
   What would make it unnecessary: the model returning, per pad, the index of its pinout entry.
3. Offset/reversal: per side try every contiguous window of the sorted pinout both ways;
   coherent = no `different` and at least one `same`. RECOMMENDED only if each side has exactly
   one coherent placement, they do not overlap, cover the pinout, and numbers are consecutive.
   The option shows its support («22 etichette su 22 concordano»); no threshold.

## Work split (disjoint files)

- A `kicadgen/services/pinout_coherence.py` (new, pure) + tests: `label_relation`,
  `side_pad_labels` (moved from `main_window._side_pinout`), `document_pinout_entries`,
  `side_pinout_report` (dropped, unnamed_pads, contradictions, alignment, support, ambiguous),
  `document_findings` (typed findings for the check).
- B `datasheet_extractor.py`: `AnswerShapeError(correction=)`; `SidePinoutContradiction` raised
  in `main_call` for castellated answers -> one corrective retry with `_SIDE_PINOUT_CORRECTION`
  (structural rule, no values); second failure keeps the newer answer with its own text;
  `_ask_side_numbering` after recount/numbering repair -> question `side_numbering`: `renumber`
  (recommended only with a unique alignment) / `keep_sides` (pinout = side labels + note);
  key includes a digest of sides+pinout numbers; `QUESTION_FIELDS` + `apply_answers` branch.
- C `main_window.py`: `side_numbering` in `EXTRACTION_QUESTION_FIELDS`; open question blocks
  footprint writes like `pad_layout`; `_side_pinout` delegates to A; log line of unmatched
  entries names the remedy; `_verify_component` passes `document=` (pinout of
  `_main_answer_for_reread`, side labels, user names, decision, open question).
- D `services/component_check.py`: `Check` gains defaulted `category`
  (document/technical/final/manual), `importance` (1 blocks use, 2 fix before production,
  3 final review), `remedy`; `verify_component(document=None)`: dropped entries, side label
  `different` from pinout, unnamed pin the document names -> ERROR importance 1 (WARN 2 if the
  user chose `keep_sides`); user-typed differing name WARN 2; electrical types and ERC/DRC final.
- E `gui/component_check_dialog.py`: verdict from category/status/importance, never from
  translated titles; first line = cause + remedy; warnings listed in groups (blocks use / fix
  before production / final reviews) with compressed pin ranges; remedies deduplicated.
- F `i18n.py` + profile database, last: every new string in four languages.

Then generality-auditor on the diff and the full suite in two processes.

## Risks and open decisions

- One more PAID call when the first answer is contradictory (corrective retry), even when a
  unique alignment would settle it.
- Fragile name rule: a bare «0» vs «A0» counts as `same`; mitigated by requiring uniqueness and
  showing the support count.
- Projects that were «usable» may become «NON USARE» (pinout entries that are not pads).
- USER DECISION (2026-11-13, «ok»): a NEW verdict «DA RIVEDERE PRIMA DELL'USO» for
  importance-2 warnings; «ESITO NON CONCLUSIVO» keeps meaning only «could not be checked».
- No «Rileggi solo il pinout» exists; a partial re-read would be a paid call and needs the
  user's decision.
