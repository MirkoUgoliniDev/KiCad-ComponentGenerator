# Shared element selection, trembling clicks, clipboard per project — plan (phase D)

User request, 2026-10-18 (behaviour restated and confirmed «si»):

1. A trembling click must not show «Punto coincidente» nor close a tiny shape.
2. The footprint preview gets the symbol editor's element selection, built on
   a SHARED module (as gestures, camera, grid, click tools and menus).
3. The element clipboard is emptied when another project is opened or created,
   in both tabs.

## 1. Trembling click — DONE by the coordinator

`ClickToolRouter.release` adds the release point of a drag as a QUIET click:
a release that snaps back onto the last point is dropped without
`tool_refused`, so no «Punto coincidente». A real drag still adds its point.
Test: `tests/test_canvas_clicktools.py::test_a_drag_release_snapping_onto_the_last_point_is_dropped_silently`.

## 2. Selection — gestures (identical in both tabs)

| Gesture | Effect |
|---|---|
| click on an element | it becomes the selection (as today in the symbol, via the drag press) |
| Ctrl+click on an element | adds / removes it |
| Shift+drag, or the tab's selection mode | rubber band; elements whose WHOLE bounds lie inside are selected; Ctrl held: added |
| click on the empty background (press+release without drag) | clears the selection (NEW in the symbol too) |
| Escape with nothing else to cancel (no tool, pan, band, drag, placement) | clears the selection (NEW in the symbol too) |
| background drag | pans, as today |

Commands on the selection (one undo step for the whole selection): Copia,
Duplica, Canc, Ruota ±90°, Specchia; the right-click menu on a member shows
«{count} elementi selezionati» (symbol C-M5 rule); the pasted elements become
the selection. With nothing selected, the footprint commands act on the
drawing under the pointer as today.

The symbol keeps its pin selection (pins and elements are exclusive, as
today); "clear" clears both. A plain (non-additive) rubber band is a NEW
selection: when it encloses nothing it clears the pins too, as the
footprint and KiCad do (review 2026-10-27, D5: `_select_region` kept the
pins when the band chose no element); only an additive (Ctrl) band keeps
them. The pin table is cleared with the pins. Tests in
`tests/test_review_1027_canvas.py`, with the additive negative twin.

## 3. Shared code — `kicadgen/gui/canvas_selection.py` (DONE, tests in `tests/test_canvas_selection.py`)

- `IndexSelection`: `primary`, `members`, `indices(count)`, `in`, `bool`,
  `select(index, additive)`, `toggle(index)`, `set_many(indices, additive)`, `clear()`.
- `RubberBand(view)`: `begin(pos, additive)`, `move(pos)`, `finish(pos) ->
  QRectF | None`, `cancel()`, `active`, `additive`.
- `is_click(press, release)`: below `startDragDistance` (at least 1 px).
- `canvas_menus.ElementClipboard.clear()` for item 3.

## 4. Adoption

- Agent S (`symbol_tab.py`): the view's own rubber band becomes `RubberBand`;
  `_selected_graphic` / `_selected_graphics` are backed by one
  `IndexSelection` (properties keep the names used across the file and the
  tests); background click and Escape clear the selection; public
  `SymbolTab.clear_clipboard()`.
- Agent F (`main_window.py`, `footprint_view.py`): footprint selection with
  `IndexSelection` + `RubberBand` + `is_click`, highlight of the selected
  drawings, commands on the selection, menu, paste selects; selection cleared
  or remapped when the drawing list changes under it (undo/redo, project
  load, delete); clipboard of BOTH tabs cleared on project open/new
  (`self._fp_clipboard.clear()` and `self.symbol_tab.clear_clipboard()`).
- Parity tests first for every existing behaviour that must not change;
  each new behaviour with a positive and a negative test.
