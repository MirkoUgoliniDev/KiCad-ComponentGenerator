"""@brief Per-step hints and keyboard shortcuts of the drawing tools, shared by both canvases.

Plan `docs/footprint-drawing-kicad-plan.md`, A4 and A7. `step_hint` returns
the ITALIAN source text of the next action of an armed tool, given how many
points it has collected; the GUI translates it with its i18n catalogue (the
texts are catalogue keys, so they must not be reworded casually). The symbol
editor shows it in its status label, the footprint preview in the hint label
under the canvas (contract 12: refreshed after every collected point and
after Backspace).

Click orders encoded here (verified in the code on 2026-11-08):

- arc: start, end, then a point on the arc, in BOTH tabs
  (`SymbolTab._shape_outline`, `MainWindow._finish_footprint_drawing`);
- bezier: start, first control point, second control point, end
  (`SymbolTab._shape_outline` -> `symbol_glyphs.bezier_points(p0, c1, c2, p3)`;
  the footprint curve of plan D2 uses the same order).

The module is pure: it imports nothing from Qt, so it can be unit-tested and
read by the i18n catalogue checks without a display.
"""
from __future__ import annotations

from typing import Dict, Tuple

# ── Step texts (Italian source strings, translated by the GUI) ─────────────

#: First point of an open-ended tool.
_FIRST = "Clic sul primo punto"
#: Next point while the shape cannot be finished yet.
_NEXT = "Clic sul punto successivo; Backspace toglie l'ultimo punto"
#: Next point once the open shape can be finished.
_NEXT_FINISH = ("Clic sul punto successivo; doppio clic o Invio per finire, "
                "Backspace toglie l'ultimo punto")
#: Next point once a click on the first point would also close the shape.
_NEXT_CLOSE = ("Clic sul punto successivo; clic sul primo punto, doppio clic o Invio per finire, "
               "Backspace toglie l'ultimo punto")
#: Next vertex of a polygon that already has three vertices.
_POLY_CLOSE = ("Clic sul vertice successivo; clic sul primo punto, doppio clic o Invio per "
               "chiudere il poligono, Backspace toglie l'ultimo punto")

#: Hint of a tool key this module does not know.
GENERIC_HINT = "Clic sul punto successivo; Esc per annullare"

#: Fixed sequences: the hint of step i is the text at index i; an index past
#: the end repeats the last text (the tool has finished or is placing).
_FIXED_STEPS: Dict[str, Tuple[str, ...]] = {
    "rectangle": ("Clic sul primo angolo",
                  "Clic sull'angolo opposto"),
    "circle": ("Clic sul centro",
               "Clic su un punto della circonferenza"),
    # Click order start, end, mid in both tabs (see the module docstring).
    "arc": ("Clic sul punto iniziale dell'arco",
            "Clic sul punto finale dell'arco",
            "Clic su un punto intermedio dell'arco"),
    # Click order start, control 1, control 2, end (symbol_glyphs.bezier_points).
    "bezier": ("Clic sul punto iniziale della curva",
               "Clic sul primo punto di controllo",
               "Clic sul secondo punto di controllo",
               "Clic sul punto finale della curva"),
    "text": ("Clic sul punto di ancoraggio del testo",),
    "textbox": ("Clic sul primo angolo della casella di testo",
                "Clic sull'angolo opposto, poi scrivi il testo"),
    "dimension": ("Clic sul primo punto da quotare",
                  "Clic sul secondo punto da quotare",
                  "Clic dove mettere la linea di quota"),
    "dimension_radial": ("Clic sul centro del cerchio o dell'arco",
                         "Clic su un punto della circonferenza",
                         "Clic dove finisce la linea di richiamo"),
    "dimension_center": ("Clic sul centro",
                         "Clic per fissare la dimensione del segno di centro"),
    # Two clicks then the text, as KiCad's leader (arrow tip, then where the
    # text goes); a third "elbow" click is not part of this sequence.
    "dimension_leader": ("Clic sulla punta della freccia",
                         "Clic dove mettere il testo, poi scrivi il testo"),
    # Two points for a distance, a third for the placement of a linear
    # dimension (canvas_measure.MEASURE_SPEC).
    "measure": ("Clic sul primo punto da misurare",
                "Clic sul secondo punto da misurare",
                "Clic dove mettere la linea di quota"),
}

#: Wire of the symbol editor: open-ended, finished by double click or Enter.
_WIRE_STEPS = ("Clic sul punto di partenza del filo",
               "Clic sul punto successivo del filo; doppio clic o Invio per finire, "
               "Backspace toglie l'ultimo punto")

#: Every key `step_hint` knows.
HINT_KEYS: Tuple[str, ...] = ("line", "polygon", "wire") + tuple(_FIXED_STEPS)

#: Number of clicks of each fixed-sequence tool (the open-ended ones have none).
STEP_COUNTS: Dict[str, int] = {key: len(steps) for key, steps in _FIXED_STEPS.items()}


def step_hint(key: str, points: int, *, closable: bool = False) -> str:
    """@brief The Italian source text of the next action of an armed tool.

    @param key Tool key (one of `HINT_KEYS`); an unknown key gets `GENERIC_HINT`.
    @param points Points collected so far (a negative value counts as 0).
    @param closable Only for "line": True when a click on the first point
        would close the shape now (the host knows its close rule, e.g. four
        points in both tabs); ignored by the other keys.
    @return The text to translate and show. Fixed-count tools repeat their
        last step for an index past the end.
    @note The polygon is always closable from three vertices (plan D1), so it
        does not read `closable`.
    """
    count = max(0, int(points))
    if key == "line":
        if count == 0:
            return _FIRST
        if count == 1:
            return _NEXT
        return _NEXT_CLOSE if closable else _NEXT_FINISH
    if key == "polygon":
        if count == 0:
            return _FIRST
        return _NEXT if count < 3 else _POLY_CLOSE
    if key == "wire":
        return _WIRE_STEPS[min(count, 1)]
    steps = _FIXED_STEPS.get(key)
    if steps is None:
        return GENERIC_HINT
    return steps[min(count, len(steps) - 1)]


#: Every distinct source text `step_hint` can return, for the i18n catalogue checks.
ALL_HINTS: Tuple[str, ...] = tuple(dict.fromkeys(
    (_FIRST, _NEXT, _NEXT_FINISH, _NEXT_CLOSE, _POLY_CLOSE, GENERIC_HINT)
    + _WIRE_STEPS + tuple(text for steps in _FIXED_STEPS.values() for text in steps)))

# ── Shortcuts (plan §0 and A7) ─────────────────────────────────────────────

#: Tool shortcuts as written in a QKeySequence. KiCad's defaults for line,
#: arc, circle, polygon, text, dimension and measure; rectangle, Bezier and
#: text box have no KiCad default and use keys of ours (plan A7).
TOOL_SHORTCUTS: Dict[str, str] = {
    "line": "Ctrl+Shift+L",
    "arc": "Ctrl+Shift+A",
    "circle": "Ctrl+Shift+C",
    "polygon": "Ctrl+Shift+P",
    "text": "Ctrl+Shift+T",
    "dimension": "Ctrl+Shift+H",
    "measure": "Ctrl+Shift+M",
    "rectangle": "Ctrl+Shift+R",     # ours: no KiCad default
    "bezier": "Ctrl+Shift+B",        # ours: no KiCad default
    "textbox": "Ctrl+Shift+X",       # ours: no KiCad default
}
#: Back to the Select tool (disarm), as KiCad.
SELECT_SHORTCUT = "Esc"
#: Properties of the selection, as KiCad.
PROPERTIES_SHORTCUT = "E"
#: Cycle the angle mode while a tool is armed, as KiCad's «lineModeNext».
ANGLE_MODE_SHORTCUT = "Shift+Space"
#: Remove the last collected point, as KiCad.
UNDO_POINT_SHORTCUT = "Backspace"


def tooltip(hint: str, key: str) -> str:
    """@brief A tool button's tooltip: its (already translated) hint followed by its shortcut.

    @param hint The translated text to show.
    @param key Tool key of `TOOL_SHORTCUTS`.
    @return ``"hint (Ctrl+Shift+L)"``, or `hint` unchanged when the key has no shortcut.
    """
    shortcut = TOOL_SHORTCUTS.get(key)
    return f"{hint} ({shortcut})" if shortcut else hint
