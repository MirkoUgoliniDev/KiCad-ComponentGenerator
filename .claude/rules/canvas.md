---
paths:
  - "kicadgen/gui/canvas_*.py"
  - "kicadgen/gui/footprint_view.py"
  - "kicadgen/gui/symbol_tab.py"
  - "kicadgen/gui/kicad_text_item.py"
  - "kicadgen/core/drawing_transform.py"
  - "kicadgen/core/vertex_edit.py"
  - "kicadgen/core/object_snap.py"
  - "tests/test_canvas_*.py"
  - "tests/*_parity.py"
---

# Canvases: symbol editor and footprint preview

Facts that every change here must keep. Procedure and traps:
`canvas-shared-change` skill; review with the `canvas-parity-reviewer` agent.

- **One implementation of every interaction, shared by both tabs** (user
  rule): what works well in one tab works the same in the other, reusing the
  code, without the user having to ask. A shared behaviour is never fixed
  inside one view only.
- **Find the owner first**: gestures, camera, grid, click tools, menus,
  selection, hit testing, measure, origin, properties and snap markers each
  live in one `kicadgen/gui/canvas_*.py` module; point transforms are in
  `kicadgen/core/drawing_transform.py` (Qt-free).
- **A difference between the tabs that must stay is written in the plan**
  (`docs/canvas-*-plan.md`) with its reason; anything else that differs is a
  defect.
- **Never trust the Python type of an item from `scene.items()`** (crash of
  2026-11-09 and 2026-11-12, `tests/test_preview_stale_wrappers.py`): an item
  Qt builds itself (`scene.addLine`, `addEllipse`, `addRect`) has a wrapper
  Qt does not tell when `scene.clear()` deletes it; a wrapper still referenced
  is handed back for a NEW item at the same address with the OLD type, and a
  type-specific call (`setPen`, `setBrush`, `setText`) writes through the
  wrong layout (Windows access violation). Restyle or edit the items from the
  lists the drawer filled (emptied before every clear), or ask the real type
  with the virtual `item.type()`; base `QGraphicsItem` calls (`data`,
  `setData`, `setVisible`) are safe. The symbol editor builds its items in
  Python (`_add_kept_item`), which avoids the class altogether.
- **Parity tests first**: the current behaviour of both tabs is recorded in
  `tests/test_symbol_<topic>_parity.py` and
  `tests/test_footprint_<topic>_parity.py` before it is changed.
- **One user action = one undo step.** Selection clears on undo, redo and
  delete; clipboards on project switch.
- **Pixel goldens** under `tests/data/camera_golden/` are SHA hashes that
  depend on the Qt build: regenerated only with their environment switch, only
  when the change is intended, and said in the handoff.
- **Measure before and after** when touching paint or drag paths: performance
  regressions here are visible to the user.
