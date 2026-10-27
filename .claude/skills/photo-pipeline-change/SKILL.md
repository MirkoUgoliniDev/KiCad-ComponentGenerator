---
name: photo-pipeline-change
description: Change the photo-to-mechanical-envelope pipeline — PCB calibration, local detectors (YOLO, SMD network), AI inventory and crop readings, cache, reconciliation, review dialog. Use it for any change to kicadgen/services/photo_*.py, the detectors, the vision prompts or their validation.
---

# Changing the photo pipeline

Goal agreed with the user: rebuild the **mechanical envelopes** of a PCB
module from photographs, not reverse-engineer it. Geometry, position,
orientation and pinout matter; part names, values and markings do not.
Heights from a single top photo are estimates, never measures.

## 1. Read first

`docs/photo-mechanical-workflow.md` (strategy, limits, reproduction commands),
`docs/yolo-photo-workflow.md`, `docs/CLASSIFICATORE_SMD.md`, and the
«Strategia implementata» section of `.claude/CLAUDE.md`. Code map:
`photo_inventory.py` (calibration, views, matching), `photo_grounding.py`
(localisation, presence, cache), `photo_refine.py`, `datasheet_extractor.py`,
`gui/photo_review_dialog.py`.

## 2. Paid calls versus replay

New inferences cost money. Always say which one you are doing:

- **Offline replay**: answers come from `<project>/photo-cache` (examples use
  `<output>/photo-cache`). The cache key includes model, schema, prompt,
  parameters and pixels, so **changing a prompt or schema invalidates the
  cache** and the next run is paid again.
- **Real call**: only with the user's agreement for that run. Never in tests:
  `tests/conftest.py` blocks non-loopback network unless a test is marked
  `allow_network`.

Reproduction (from `docs/photo-mechanical-workflow.md`):

```
.venv/Scripts/python.exe tools/evaluate_photo_inventory.py generated/mechanical-envelope/inventory.json --photo <foto>.png --output generated/mechanical-envelope/metrics.json
```

## 3. Rules that changes must keep

- **Generalise** (binding user rule): no threshold moved until the current
  board passes, no rule on names or brands; what the data cannot tell (mating
  direction, lead attachment) is asked of the vision model or the user. Run
  the `generality-auditor` agent on the diff.
- Validate answers and cache semantically: finite non-boolean numbers, valid
  rectangles, expected IDs complete and unique. Incomplete answers are never
  cached as successes; valid parts of a partial answer are kept.
- Do not invent coordinates while verifying; unverifiable results stay as
  doubts. Exclude empty pads, vias and silkscreen.
- Estimated sizes are never used as measures; provenance and uncertainty of
  every height stay explicit.
- OpenCV prepares and calibrates; it is not a universal component detector.
  Experiments (edge snapping, detailed classification) stay out of the
  standard path until validated on more than one board.
- Do not optimise the component count at the expense of geometry: the
  reference IoU matters more than reaching the same number of boxes.
- Applying or re-extracting invalidates the old preview; late CAD results
  must not restore it.

## 4. Verify

Focused `tests/test_photo_*.py` plus a positive and a negative case that
differ for a structural reason; an offline replay on the reference photo when
the matching or geometry changed, comparing envelopes and IoU before/after.
Then the full suite (`run-test-suite`).
