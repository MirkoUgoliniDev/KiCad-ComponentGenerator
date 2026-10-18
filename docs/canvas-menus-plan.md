# Shared element menu, clipboard and transforms — plan (phase C)

User request, 2026-10-18: the right-click menu on a footprint drawing
(Duplica, Layer, Modifica testo / Spessore, Elimina) is poorer than the one on
a symbol element (title, Ruota ±90°, Specchia, Elimina, Copia, Duplica,
Proprietà elemento…, Modifica testo / style entries). Align them, and «devi
usare le librerie comuni» (you must use the shared libraries, the ones made
yesterday): the menu is built by ONE shared module, as gestures, camera,
grid and click tools already are.

## 1. What exists today

| | Symbol editor (`symbol_tab.py`) | Footprint preview (`main_window.py`) |
|---|---|---|
| Builder | `_graphic_context_menu(index, global_pos)` | `_footprint_drawing_context_menu(index)` |
| Title | disabled label (element label, or «N elementi selezionati») | none |
| Rotate ±90 | yes (not on a single wire) | **no** |
| Mirror | yes (not on a single wire) | **no** |
| Delete | «Elimina elemento» | «Elimina disegno» |
| Copy / paste | «Copia (Ctrl+C)», Ctrl+V; next paste one grid step further | **no** |
| Duplicate | «Duplica (Ctrl+D)» | «Duplica (Ctrl+D)» |
| Properties | «Proprietà elemento…» (X, Y, rotation, mirror, size) | **no** |
| Layer | n/a | submenu with colour swatches, checkable |
| Text | «Modifica testo…» | «Modifica testo…» |
| Style | width / fill / stroke type entries | «Spessore del tratto…» |
| Icons | `assets/rotate-ccw.svg`, `rotate-cw.svg`, `format-painter.svg`, `delete.svg` | none |

Data models differ and STAY different:
- Symbol element: `{x, y, rotation, mirror, primitives}`; shapes have their
  origin at the centre of their bounds, so rotate/mirror act about the shape;
  a text's origin is its bottom-left anchor; rotation of a text is taken
  modulo 180 (horizontal or vertical, `symbol_glyphs.placed_primitives`); a
  mirror of a text only moves its anchor, which is its origin: no visible
  effect (KiCad never mirrors text glyphs).
- Footprint drawing: `{kind, layer, width, points (absolute mm, Y down)[,
  text, size, anchor]}`; no rotation or mirror field.

## 2. Target behaviour (both tabs)

Menu order, built by the shared module from a list of entries:

1. Title (disabled): the element's label / drawing kind name, or the selection count.
2. Ruota di 90 gradi (antiorario) — icon rotate-ccw
3. Ruota di 90 gradi (orario) — icon rotate-cw
4. Specchia — icon format-painter
5. Elimina elemento / Elimina disegno — icon delete (labels stay per tab: existing translations)
6. Copia (Ctrl+C)
7. Duplica (Ctrl+D)
8. Tab-specific entries: footprint «Layer ▸» submenu (as today).
9. Proprietà elemento…
10. separator, then Modifica testo… (text) or the style entries (symbol) /
    «Spessore del tratto…» (footprint).

Footprint semantics, mirroring the symbol's:
- **Rotate a shape**: every point turns ±90° about the centre of the
  drawing's bounding box (line: all vertices; rectangle: both corners, still
  axis aligned; circle: centre and rim point; arc: start, mid, end). Screen
  sense: Y grows DOWN, so «antiorario» (counter-clockwise ON SCREEN) maps
  (x, y) → (cx + (y − cy), cy − (x − cx)).
- **Rotate a text**: the anchor stays; a new optional field `angle` ∈ {0, 90}
  (modulo 180, as the symbol's text) toggles; written as `(at x y 90)` on the
  `fp_text`; drawn rotated in the preview about the anchor (90 = reading
  bottom-to-top, KiCad's convention). Records without `angle` are angle 0 and
  are written byte-identical to today.
- **Mirror a shape**: left-right about the bounding-box centre X (arc order
  start/mid/end is kept: the mirrored three points define the mirrored arc).
- **Mirror a text**: no visible effect, exactly as in the symbol (decision
  C-M1 below).
- **Copy / paste**: Ctrl+C copies the selected drawing(s), Ctrl+V pastes them
  one drawing-grid step right and down, each further paste one step further
  (as the symbol); the pasted drawings become the selection. Duplicate =
  copy + paste keeping the clipboard (as the symbol).
- **Properties**: dialog with layer, stroke width; for a shape the position
  of its bounding-box centre X/Y and its size (rectangle width/height,
  circle radius); for a text the anchor X/Y, the text, its size and the
  angle (0/90). One undo step per accepted dialog; unchanged values change
  nothing.
- Every edit is one `_apply_footprint_drawings` commit (one undo step, one
  recovery checkpoint), refused geometry changes nothing.

## 3. Shared code (the "librerie comuni")

### 3.1 `kicadgen/gui/canvas_menus.py` (Qt-only, no model imports)

- `MenuEntry` (frozen dataclass): `key: str`, `label: str` (already
  translated by the caller), `icon: str | None` (asset file name under
  `kicadgen/gui/assets`), `enabled: bool = True`, `separator_before: bool =
  False`, `tooltip: str = ""`, `checkable: bool = False`, `checked: bool =
  False`, `children: tuple[MenuEntry, ...] = ()` (a submenu when non-empty),
  `icon_object: QIcon | None = None` (a ready icon, e.g. a layer swatch).
- `STANDARD_ENTRIES` helpers: `rotate_entries(tx)`, `mirror_entry(tx)`,
  `delete_entry(tx, label)`, `copy_entry(tx)`, `duplicate_entry(tx)`,
  `properties_entry(tx)` returning `MenuEntry` with the shared keys
  `rotate_ccw`, `rotate_cw`, `mirror`, `delete`, `copy`, `duplicate`,
  `properties` and the shared icons — so both tabs show the same words, icons
  and order.
- `build_element_menu(parent, title, entries) -> (QMenu, dict[QAction, str])`:
  disabled title + separator, tooltips visible, icons visible in menu, one
  action per entry (submenus recursively), map action → key.
- `exec_element_menu(menu, global_pos) -> str | None`: exec, return the
  chosen key (None when dismissed), `deleteLater` the menu in a `finally`.
- `ElementClipboard`: holds deep copies of records and a cumulative paste
  offset; `copy(records)`, `bool(clipboard)`, `paste_batch(step) -> list`
  (records translated by the accumulated offset, via a caller-supplied
  `translate(record, dx, dy) -> record` given at construction), `advance()`
  after a successful paste, `snapshot()/restore()` for Duplicate (which must
  keep the previous clipboard). No model knowledge.

### 3.2 `kicadgen/core/drawing_transform.py` (pure, no Qt)

- `bounds_centre(points) -> (cx, cy)`.
- `rotate_points_90(points, centre, counter_clockwise_on_screen: bool) ->
  list` (Y down convention documented; results rounded to 1e-4 mm).
- `mirror_points_x(points, centre_x) -> list`.

The symbol keeps its own `x/y/rotation/mirror` model; it uses the shared
menu, entries and clipboard; the footprint uses the shared menu, entries,
clipboard and transforms.

## 4. Decisions to confirm at commit (positive and negative test each)

- C-M1: «Specchia» on a TEXT is shown DISABLED in both tabs (it has never had
  a visible effect in the symbol; offering a no-op is misleading). Symbol
  behaviour change: the entry greys out for a single text; a multi-selection
  keeps it (it acts on the other elements).
- C-M2: footprint text angle limited to 0/90 like the symbol's text (KiCad's
  "keep upright" shows 180/270 as 0/90 anyway).
  Amended 2026-10-18 (user: «fixa tutto»): a saved angle that is any other
  multiple of 90 (180, 270, -90, a hand-edited file) is normalised modulo 180
  instead of dropping the whole text; 45, strings, bools and NaN still drop it.
- C-M5 (user, 2026-10-18): a right click on a member of the symbol selection
  keeps the whole selection (the «N elementi selezionati» menu is reachable
  from the canvas), as the pin menu does; on any other element that element
  becomes the selection.
- C-M3: footprint gets Ctrl+C / Ctrl+V with the symbol's paste offset rule.
- C-M4: the footprint menu gets a title line (kind name: «Linea»,
  «Rettangolo», «Cerchio», «Arco», «Testo» — existing translation keys of the
  palette when present).

## 5. Method

1. Parity tests of the CURRENT symbol menu (entries, order, enabled state,
   icons, the action each key triggers, multi-selection variant) and of the
   current footprint menu, before touching code.
2. Shared modules with unit tests (`tests/test_canvas_menus.py`,
   `tests/test_drawing_transform.py`).
3. Adoption: symbol (agent S), footprint (agent F: core schema/writer,
   preview drawing of rotated text, menu, clipboard, properties).
4. New translations: every new visible string in all four languages of the
   SQLite catalogue (`data/`), as the other phases.
5. Read-only adversarial review, full suite in two halves, commit only with
   the user's OK.
