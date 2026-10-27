---
name: canvas-shared-change
description: Change how the symbol editor or the footprint preview behaves — drawing, selection, menus, drag, zoom, grid, click tools, undo — through the shared canvas components, with parity tests in both tabs. Use it for any interaction change in either canvas.
---

# Changing canvas behaviour

The symbol editor and the footprint preview share one implementation of
every interaction. User rule: what works well in one tab works the same in
the other, reusing the code, without the user having to ask.

## 1. Find the owner

```
kicadgen/gui/canvas_gestures.py    drags, hover handles, key repeat, deferred recovery writes, Escape/Hide
kicadgen/gui/canvas_camera.py      zoom clamped per tab, wheel ±4 notches, scrollbar pan, memoised fit
kicadgen/gui/canvas_grid.py        ExposedRegion + paint_grid (only where Qt paints)
kicadgen/gui/canvas_clicktools.py  ToolSpec, ClickToolHost, PointCollector, ClickToolRouter
kicadgen/gui/canvas_menus.py       element context menu and clipboard
kicadgen/gui/canvas_selection.py   IndexSelection, RubberBand, encloses, is_click
kicadgen/core/drawing_transform.py turn and mirror of points (Qt-free)
```

The plans with the contract and the deliberate per-tab differences:
`docs/canvas-gestures-plan.md`, `canvas-camera-grid-plan.md`,
`canvas-clicktools-plan.md`, `canvas-menus-plan.md`, `canvas-selection-plan.md`.
Read the relevant plan before changing behaviour.

## 2. Method

1. Write the plan in HANDOFF.md (`update-handoff` skill). For a new shared
   capability, write a plan in `docs/canvas-<topic>-plan.md` first: inventory
   of both tabs with line numbers, the component API, the contract, the
   differences and who decides them.
2. **Parity tests first**: record the current behaviour of both tabs in
   `tests/test_symbol_<topic>_parity.py` and
   `tests/test_footprint_<topic>_parity.py` before changing it, so a
   regression in the tab you did not mean to change shows.
3. Change the shared component with unit tests in
   `tests/test_canvas_<topic>.py`; adopt it in both views. Never fix a shared
   behaviour inside one view only.
4. A difference between tabs that must stay is recorded in the plan with its
   reason; anything else that differs is a defect.
5. Review with the `canvas-parity-reviewer` agent.

## 3. Traps

- Pixel goldens under `tests/data/camera_golden/` are SHA hashes that depend
  on the Qt build. Regenerate goldens only with their environment switch
  (e.g. `KICADGEN_REGEN_CLICKTOOL_GOLDEN=1`) and only when the change is
  intended; say so in the handoff.
- One user action = one undo step. Selection clears on undo/redo/delete,
  clipboards on project switch.
- A dialog or `QRunnable` kept alive after it closes can crash a later test
  through the garbage collector (HANDOFF points 113 and 131, block A): release
  dialogs after `exec()` and give pooled work a shutdown path.
- Measure before/after when touching paint or drag paths (refresh counts,
  timings): performance regressions here are visible to the user.

## 4. Verify

All `tests/test_canvas_*.py`, all `*_parity.py`, the tab's own tests and
`tests/test_documentation.py`; then the full suite (`run-test-suite`).
