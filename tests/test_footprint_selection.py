"""@brief Selection of the footprint drawings (plan docs/canvas-selection-plan.md, phase D).

First section: parity of TODAY's behaviour with nothing selected (the
keyboard and the menu act on the drawing under the pointer), which the
selection must not change. Later sections: the selection gestures, the
commands on the selection and the clipboard emptied per project.
"""
from copy import deepcopy

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QMenu  # noqa: E402

from kicadgen.core.footprint import USER_TEXT_ANCHOR, validate_footprint_drawings  # noqa: E402


def record(kind="line", points=((0, 0), (5, 5)), layer="F.SilkS", **extra):
    """A drawing record before validation."""
    base = {"kind": kind, "layer": layer, "width": 0.12, "points": [list(p) for p in points]}
    base.update(extra)
    return base


@pytest.fixture
def preview(window, monkeypatch):
    """The window with its preview drawn; no file is written."""
    monkeypatch.setattr(window, "_generate", lambda **kwargs: True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    window.chk_grid.setChecked(False)
    window._draw_preview()
    return window


def three(window):
    """Put a line, a circle and a text in the draft (indices 0, 1, 2)."""
    window.footprint_drawings = validate_footprint_drawings([
        record(), record("circle", ((10, 0), (11, 0))),
        record("text", ((20, 0),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR)])
    window._draw_preview()


def choose(monkeypatch, text):
    """Make every menu choose the entry `text` (searching submenus); record the titles shown."""
    titles = []

    def find(menu):
        for action in menu.actions():
            if action.menu() is not None:
                found = find(action.menu())
                if found is not None:
                    return found
            elif action.text() == text:
                return action
        return None

    def fake_exec(menu, *_args):
        titles.append(menu.actions()[0].text())
        return find(menu)

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    return titles


# ── Parity: nothing selected, the hovered drawing is the target ────────

def test_parity_delete_key_removes_the_hovered_drawing(preview):
    window = preview
    three(window)
    window._fp_hovered_drawing = 1
    window._delete_hovered_drawing()
    assert [r["kind"] for r in window.footprint_drawings] == ["line", "text"]
    window._fp_hovered_drawing = -1
    window._delete_hovered_drawing()
    assert len(window.footprint_drawings) == 2, "nothing hovered: nothing deleted"


def test_parity_ctrl_d_duplicates_the_hovered_drawing(preview):
    window = preview
    three(window)
    window._fp_hovered_drawing = 1
    window._duplicate_hovered_drawing()
    assert [r["kind"] for r in window.footprint_drawings] == ["line", "circle", "text", "circle"]
    # Since phase D the duplicate becomes the selection (flipped: was "no
    # selection, a second Ctrl+D with nothing hovered does nothing"); with
    # the selection cleared the old rule holds.
    assert window._fp_selected_indices() == [3]
    window._clear_footprint_selection()
    window._fp_hovered_drawing = -1
    window._duplicate_hovered_drawing()
    assert len(window.footprint_drawings) == 4


def test_parity_ctrl_c_copies_the_hovered_drawing(preview):
    window = preview
    three(window)
    window._fp_hovered_drawing = 2
    window._copy_hovered_drawing()
    assert window._paste_footprint_drawings()
    assert window.footprint_drawings[-1]["kind"] == "text"


def test_parity_menu_acts_on_the_right_clicked_drawing(preview, monkeypatch):
    window = preview
    three(window)
    titles = choose(monkeypatch, "Elimina disegno")
    window._footprint_drawing_context_menu(1)
    assert titles == ["Cerchio"]
    assert [r["kind"] for r in window.footprint_drawings] == ["line", "text"]


# ── Gestures, through real mouse and key events ────────────────────────

from PyQt6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402

#: Scene px per mm of the preview.
PX = 20.0
NO, CTRL, SHIFT = (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier,
                   Qt.KeyboardModifier.ShiftModifier)


@pytest.fixture
def shown(window, qapp, monkeypatch):
    """The preview tab shown, camera at 1 px per scene px centred on the origin; three drawings 10 mm up."""
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(window, "_generate", lambda **_kwargs: True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_schedule_footprint_rewrite", lambda **_kwargs: None)
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    qapp.processEvents()
    window.chk_grid.setChecked(False)
    # Line 0..5 mm, rectangle 8..10 mm, circle centred at 15 mm, all at y = -10 mm.
    window.footprint_drawings = validate_footprint_drawings([
        record(points=((0, -10), (5, -10))),
        record("rectangle", ((8, -11), (10, -9))),
        record("circle", ((15, -10), (16, -10)))])
    window._draw_preview()
    view = window.preview_view
    view.resetTransform()
    view.centerOn(0, 0)
    view.auto_fit = False
    return window


def vp(window, x_mm, y_mm):
    """Viewport point of a footprint point (mm)."""
    return window.preview_view.mapFromScene(QPointF(x_mm * PX, y_mm * PX))


def click(window, pos, modifier=NO, button=Qt.MouseButton.LeftButton):
    viewport = window.preview_view.viewport()
    QTest.mousePress(viewport, button, modifier, pos)
    QTest.mouseRelease(viewport, button, modifier, pos)


def drag(window, start, end, modifier=NO):
    viewport = window.preview_view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, modifier, start)
    QTest.mouseMove(viewport, (start + end) / 2)
    QTest.mouseMove(viewport, end)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, modifier, end)


def test_a_click_on_a_drawing_selects_it_and_a_background_click_clears(shown):
    window = shown
    click(window, vp(window, 2.5, -10))
    assert window._fp_selected_indices() == [0]
    assert len(window.footprint_drawings) == 3 and window.footprint_drawings[0]["points"] == [[0, -10], [5, -10]], \
        "a click selects, it moves nothing"
    click(window, vp(window, 9, -9))          # rectangle edge
    assert window._fp_selected_indices() == [1], "a plain click replaces the selection"
    click(window, vp(window, 5, 10))          # empty background
    assert window._fp_selected_indices() == []


def test_a_background_drag_pans_and_keeps_the_selection(shown):
    window = shown
    click(window, vp(window, 2.5, -10))
    before = window.preview_view.mapToScene(QPoint(0, 0))
    drag(window, vp(window, 5, 10), vp(window, 8, 12))
    assert window.preview_view.mapToScene(QPoint(0, 0)) != before, "the camera moved"
    assert window._fp_selected_indices() == [0], "a drag is not a click: the selection stays"


def test_ctrl_click_adds_and_removes(shown):
    window = shown
    click(window, vp(window, 2.5, -10))
    click(window, vp(window, 9, -9), CTRL)
    assert window._fp_selected_indices() == [0, 1]
    click(window, vp(window, 2.5, -10), CTRL)
    assert window._fp_selected_indices() == [1]
    assert window.footprint_drawings[0]["points"] == [[0, -10], [5, -10]], "Ctrl+click starts no drag"


def test_shift_band_selects_only_wholly_enclosed_drawings(shown):
    window = shown
    # From (-1, -12) to (11, -8): the line and the rectangle are inside, the circle (14..16) is not.
    drag(window, vp(window, -1, -12), vp(window, 11, -8), SHIFT)
    assert window._fp_selected_indices() == [0, 1]
    # Negative: a band cutting the circle in half selects nothing; without Ctrl it replaces.
    drag(window, vp(window, 14.5, -12), vp(window, 17, -8), SHIFT)
    assert window._fp_selected_indices() == []
    # Ctrl+Shift adds to the selection.
    click(window, vp(window, 2.5, -10))
    drag(window, vp(window, 13, -12), vp(window, 17, -8), SHIFT | CTRL)
    assert window._fp_selected_indices() == [0, 2]


def test_escape_clears_only_when_nothing_else_is_running(shown):
    window = shown
    click(window, vp(window, 2.5, -10))
    window._toggle_footprint_draw_tool("line")
    QTest.keyClick(window.preview_view, Qt.Key.Key_Escape)
    assert window._fp_draw_tool is None, "Escape left the tool"
    assert window._fp_selected_indices() == [0], "... and did not also clear the selection"
    QTest.keyClick(window.preview_view, Qt.Key.Key_Escape)
    assert window._fp_selected_indices() == []


def _colours(window):
    """{drawing index: pen colour} of the drawn shapes."""
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    return {int(i.data(DRAWING_KEY_ROLE)): i.pen().color().name()
            for i in window.preview_scene.items() if i.data(DRAWING_KEY_ROLE) is not None}


def test_selected_drawings_are_highlighted_in_the_symbols_selected_colour(shown):
    """flipped: was the theme accent BLUE (#89b4fa); now the symbol editor's `theme['selected']`
    (review M4/L1: same colour and strength as the symbol's selected elements)."""
    from kicadgen.gui.symbol_tab import CANVAS_THEMES
    window = shown
    dark = CANVAS_THEMES["dark"]["selected"]
    click(window, vp(window, 2.5, -10))
    assert _colours(window)[0] == dark and _colours(window)[1] != dark
    window._draw_preview()
    assert _colours(window)[0] == dark, "a redraw keeps the highlight"
    # The light (KiCad) theme of the symbol editor: the same colour as its selected elements.
    # No manual refresh: the symbol's `theme_changed` restyles the footprint highlight at once.
    window.symbol_tab.set_kicad_colors(True)
    assert _colours(window)[0] == CANVAS_THEMES["kicad"]["selected"] == window.symbol_tab.theme["selected"]
    window.symbol_tab.set_kicad_colors(False)
    click(window, vp(window, 5, 10))
    assert dark not in _colours(window).values()


def test_a_selected_shape_is_drawn_thicker(shown):
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    window = shown
    select(window, [0])
    widths = {int(i.data(DRAWING_KEY_ROLE)): i.pen().widthF()
              for i in window.preview_scene.items() if i.data(DRAWING_KEY_ROLE) is not None}
    assert widths[0] >= 2.5 and widths[0] > widths[1]


def test_a_selected_text_gets_the_selected_colour_and_an_outline(shown):
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    from kicadgen.gui.symbol_tab import CANVAS_THEMES
    window = shown
    window.footprint_drawings = validate_footprint_drawings([
        record("text", ((0, -10),), text="AB", size=1.0, anchor=USER_TEXT_ANCHOR),
        record("text", ((5, -10),), text="CD", size=1.0, anchor=USER_TEXT_ANCHOR)])
    window._draw_preview()
    select(window, [0])
    items = {int(i.data(DRAWING_KEY_ROLE)): i for i in window.preview_scene.items()
             if i.data(DRAWING_KEY_ROLE) is not None}
    boxes = lambda item: [c for c in item.childItems() if c.data(window._FP_SELECTION_BOX_ROLE)]  # noqa: E731
    assert items[0].brush().color().name() == CANVAS_THEMES["dark"]["selected"]
    assert len(boxes(items[0])) == 1, "an outline makes it visible whatever the layer colour"
    assert boxes(items[1]) == [] and items[1].brush().color().name() != CANVAS_THEMES["dark"]["selected"]
    window._clear_footprint_selection()
    assert boxes(items[0]) == [], "the outline goes away with the selection"


def test_ctrl_click_on_the_background_keeps_the_selection(shown):
    """Review M3: an additive click that misses every drawing must not start over."""
    window = shown
    select(window, [0, 1])
    click(window, vp(window, 5, 10), CTRL)
    assert window._fp_selected_indices() == [0, 1]
    # Twin: the same click without Ctrl clears.
    click(window, vp(window, 5, 10))
    assert window._fp_selected_indices() == []


def test_shift_click_without_drag_keeps_the_selection(shown):
    """Review L2: a Shift+click is not a band (`RubberBand.finish` returns None)."""
    window = shown
    select(window, [0, 1])
    click(window, vp(window, 5, 10), SHIFT)
    assert window._fp_selected_indices() == [0, 1]
    assert not window.preview_view.band.active and not window.preview_view.band.visible
    # Twin: a real Shift+drag over empty space replaces the selection with nothing.
    drag(window, vp(window, 4, 8), vp(window, 8, 12), SHIFT)
    assert window._fp_selected_indices() == []


def test_other_buttons_are_swallowed_while_the_band_is_drawn(shown, monkeypatch):
    """Review L3: no menu and no selection change in the middle of a band."""
    window = shown
    opened = []
    monkeypatch.setattr(QMenu, "exec", lambda menu, *_a: opened.append(menu) or None)
    viewport = window.preview_view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, SHIFT, vp(window, -1, -12))
    QTest.mouseMove(viewport, vp(window, 11, -8))
    QTest.mousePress(viewport, Qt.MouseButton.RightButton, NO, vp(window, 15, -10))   # on the circle
    QTest.mouseRelease(viewport, Qt.MouseButton.RightButton, NO, vp(window, 15, -10))
    assert opened == [] and window._fp_selected_indices() == []
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, SHIFT, vp(window, 11, -8))
    assert window._fp_selected_indices() == [0, 1], "the band still ends normally"
    # Twin: with no band, the same right press opens the menu.
    click(window, vp(window, 16, -10), button=Qt.MouseButton.RightButton)
    assert len(opened) == 1


# ── Commands on the selection ─────────────────────────────────────────

def select(window, indices):
    window._fp_selected_indices()
    window._fp_selection.set_many(indices)
    window._refresh_drawing_selection()


def test_delete_acts_on_the_whole_selection_in_one_undo_step(shown):
    window = shown
    select(window, [0, 2])
    window._fp_hovered_drawing = 1          # the selection wins over the pointer
    depth = len(window._fp_drawings_undo)
    window._delete_hovered_drawing()
    assert [r["kind"] for r in window.footprint_drawings] == ["rectangle"]
    assert len(window._fp_drawings_undo) == depth + 1 and window._fp_selected_indices() == []
    window._undo_footprint_drawing()
    assert len(window.footprint_drawings) == 3
    assert window._fp_selected_indices() == [], "undo replaces the list: the selection is cleared"


def test_copy_and_paste_the_selection_select_the_pasted_drawings(shown):
    window = shown
    select(window, [0, 1])
    window._copy_hovered_drawing()
    assert window._paste_footprint_drawings()
    assert window._fp_selected_indices() == [3, 4]
    assert [r["kind"] for r in window.footprint_drawings[3:]] == ["line", "rectangle"]


def test_duplicate_the_selection_is_one_step_and_selects_the_copies(shown):
    window = shown
    select(window, [1, 2])
    depth = len(window._fp_drawings_undo)
    window._duplicate_hovered_drawing()
    assert [r["kind"] for r in window.footprint_drawings] == ["line", "rectangle", "circle", "rectangle", "circle"]
    assert len(window._fp_drawings_undo) == depth + 1 and window._fp_selected_indices() == [3, 4]


def test_menu_on_a_member_acts_on_the_whole_selection(shown, monkeypatch):
    window = shown
    select(window, [0, 1])
    depth = len(window._fp_drawings_undo)
    titles = choose(monkeypatch, "Ruota di 90 gradi (antiorario)")
    window._footprint_drawing_context_menu(1)
    assert titles == ["2 elementi selezionati"]
    assert window.footprint_drawings[0]["points"] == [[2.5, -7.5], [2.5, -12.5]], "the line turned about its centre"
    assert window.footprint_drawings[1]["points"] == [[8, -9], [10, -11]], "the square turned about its own centre"
    assert len(window._fp_drawings_undo) == depth + 1
    assert window._fp_selected_indices() == [0, 1], "a turn keeps the selection"


def test_menu_on_a_non_member_selects_it_and_shows_the_single_menu(shown, monkeypatch):
    window = shown
    select(window, [0, 1])
    titles = choose(monkeypatch, "Specchia")
    window._footprint_drawing_context_menu(2)
    assert titles == ["Cerchio"] and window._fp_selected_indices() == [2]
    assert window.footprint_drawings[0]["points"] == [[0, -10], [5, -10]], "the old selection was not touched"


def test_a_refused_multi_turn_changes_nothing_and_says_why(shown, monkeypatch):
    window = shown
    shown_toasts = []
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda title, body: shown_toasts.append((title, body)))
    window.footprint_drawings = validate_footprint_drawings([
        record(points=((0, 0), (1, 0))), record(points=((300, -400), (300, 400)))])
    window._draw_preview()
    select(window, [0, 1])
    before = deepcopy(window.footprint_drawings)
    from kicadgen.core.footprint import turn_drawing
    assert not window._transform_drawings([0, 1], lambda r: turn_drawing(r, True))
    assert window.footprint_drawings == before
    assert shown_toasts and shown_toasts[0][0] == "Modifica scartata."


def test_a_project_load_clears_the_selection(shown):
    window = shown
    select(window, [0])
    state = deepcopy(window._project_state())
    state["footprint_drawings"] = [record()]
    window._apply_project_state(state)
    assert window._fp_selected_indices() == []


# ── Group drag (as the symbol editor's) ───────────────────────────────

def test_dragging_a_member_moves_the_whole_selection_in_one_undo_step(shown):
    window = shown
    select(window, [0, 1])
    depth = len(window._fp_drawings_undo)
    viewport = window.preview_view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, NO, vp(window, 2.5, -10))
    QTest.mouseMove(viewport, vp(window, 4, -10))
    QTest.mouseMove(viewport, vp(window, 5.5, -10))
    ghosts = [i for i in window.preview_scene.items() if i.data(0x48474F) is not None]
    assert len(ghosts) >= 2, "live outlines of both members during the drag"
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, NO, vp(window, 5.5, -10))
    assert window.footprint_drawings[0]["points"] == [[3, -10], [8, -10]]
    assert window.footprint_drawings[1]["points"] == [[11, -11], [13, -9]], "the rectangle moved by the same delta"
    assert window.footprint_drawings[2]["points"] == [[15, -10], [16, -10]], "the unselected circle stayed"
    assert len(window._fp_drawings_undo) == depth + 1 and window._fp_selected_indices() == [0, 1]


def test_dragging_a_non_member_moves_it_alone_and_selects_it(shown):
    window = shown
    select(window, [0, 1])
    drag(window, vp(window, 16, -10), vp(window, 19, -10))      # the circle's rim
    assert window.footprint_drawings[2]["points"] == [[18, -10], [19, -10]]
    assert window.footprint_drawings[0]["points"] == [[0, -10], [5, -10]]
    assert window._fp_selected_indices() == [2]


def test_a_handle_drag_on_a_member_reshapes_that_drawing_only(shown):
    window = shown
    select(window, [0, 1])
    viewport = window.preview_view.viewport()
    QTest.mouseMove(viewport, vp(window, 2.5, -10))         # hover: the handles appear
    QTest.mouseMove(viewport, vp(window, 2.6, -10))
    drag(window, vp(window, 5, -10), vp(window, 6, -8))     # the line's end handle
    assert window.footprint_drawings[0]["points"] == [[0, -10], [6, -8]]
    assert window.footprint_drawings[1]["points"] == [[8, -11], [10, -9]], "the other member did not move"
    assert window._fp_selected_indices() == [0], "a handle press selects that drawing alone, as the symbol"


def test_escape_cancels_the_whole_group_drag(shown):
    window = shown
    select(window, [0, 1])
    before = deepcopy(window.footprint_drawings)
    viewport = window.preview_view.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, NO, vp(window, 2.5, -10))
    QTest.mouseMove(viewport, vp(window, 5.5, -10))
    QTest.keyClick(window.preview_view, Qt.Key.Key_Escape)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, NO, vp(window, 5.5, -10))
    assert window.footprint_drawings == before
    assert not [i for i in window.preview_scene.items() if i.data(0x48474F) is not None
                and i.pen().style() == Qt.PenStyle.DashLine], "no ghost left"
    assert window._fp_selected_indices() == [0, 1], "Escape cancelled the drag, not the selection"


def test_a_text_member_of_a_group_drag_shows_its_moved_box(shown):
    """Review L4: a text has no outline path, so its drawn box is the ghost."""
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    window = shown
    window.footprint_drawings = validate_footprint_drawings([
        record(points=((0, -10), (5, -10))),
        record("text", ((8, -10),), text="AB", size=1.0, anchor=USER_TEXT_ANCHOR)])
    window._draw_preview()
    select(window, [0, 1])
    text_box = next(i for i in window.preview_scene.items() if i.data(DRAWING_KEY_ROLE) == 1).sceneBoundingRect()
    window._on_footprint_handle_pressed(0, -1, 2.5 * PX, -10 * PX)
    window._on_footprint_handle_dragged(0, -1, 5.5 * PX, -10 * PX)     # +3 mm
    ghosts = [i.path().boundingRect() for i in window.preview_scene.items() if i.data(0x48474F) is not None
              and hasattr(i, "path")]
    moved = text_box.translated(3 * PX, 0)
    assert any(abs(g.left() - moved.left()) < 0.5 and abs(g.width() - moved.width()) < 0.5 for g in ghosts)
    # Twin: a lone text drag (not in a group) still shows its box too, and nothing for the line.
    window._fp_handle_drag = None
    window._clear_footprint_selection()
    window._on_footprint_handle_pressed(1, -1, 8.5 * PX, -10.2 * PX)
    window._on_footprint_handle_dragged(1, -1, 11.5 * PX, -10.2 * PX)
    ghosts = [i.path().boundingRect() for i in window.preview_scene.items() if i.data(0x48474F) is not None
              and hasattr(i, "path")]
    assert len(ghosts) == 1


def test_a_group_drop_refused_for_one_member_changes_nothing(shown, monkeypatch):
    window = shown
    toasts = []
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda title, body: toasts.append((title, body)))
    window.footprint_drawings = validate_footprint_drawings([
        record(points=((0, -10), (5, -10))), record(points=((498, -10), (499.5, -10)))])
    window._draw_preview()
    select(window, [0, 1])
    before = deepcopy(window.footprint_drawings)
    window._on_footprint_handle_pressed(0, -1, 2.5 * PX, -10 * PX)
    window._on_footprint_handle_dropped(0, -1, 5.5 * PX, -10 * PX)   # +3 mm: 502.5 is out
    assert window.footprint_drawings == before
    assert toasts and toasts[0][0] == "Spostamento scartato." and "area consentita" in toasts[0][1]


# ── A new drawing becomes the selection ───────────────────────────────

def test_a_new_drawing_becomes_the_selection(shown):
    window = shown
    select(window, [0])
    window._toggle_footprint_draw_tool("rectangle")
    window._on_footprint_draw_clicked(20 * PX, -12 * PX)
    window._on_footprint_draw_clicked(22 * PX, -10 * PX)
    assert len(window.footprint_drawings) == 4 and window._fp_selected_indices() == [3]


def test_a_refused_or_cancelled_drawing_leaves_the_selection_unchanged(shown, monkeypatch):
    """Negative twin, as the symbol: a refused or cancelled shape does not touch the selection."""
    window = shown
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda *_a: None)
    select(window, [0, 1])
    window._toggle_footprint_draw_tool("circle")
    window._on_footprint_draw_clicked(20 * PX, -12 * PX)
    window._on_footprint_draw_clicked(20.001 * PX, -12 * PX)     # zero radius once snapped: refused
    window._on_footprint_draw_finish()
    assert len(window.footprint_drawings) == 3 and window._fp_selected_indices() == [0, 1]
    window._toggle_footprint_draw_tool("rectangle")
    window._on_footprint_draw_clicked(20 * PX, -12 * PX)
    window._cancel_footprint_drawing()
    assert window._fp_selected_indices() == [0, 1]


# ── Selection validity: the drawings revision (review L7) ─────────────

def test_every_replacement_of_the_drawings_bumps_the_revision(preview):
    window = preview
    three(window)
    revision = window._fp_drawings_revision
    window._delete_drawings([0])
    assert window._fp_drawings_revision > revision, "a commit"
    revision = window._fp_drawings_revision
    window._undo_footprint_drawing()
    assert window._fp_drawings_revision > revision, "an undo"
    window._delete_drawings([0])                # pending again: the discard has work to do
    revision = window._fp_drawings_revision
    window.discard_footprint_draft()
    assert window._fp_drawings_revision > revision, "a discard"
    revision = window._fp_drawings_revision
    window._apply_project_state(deepcopy(window._project_state()))
    assert window._fp_drawings_revision > revision, "a project load"


def test_an_edit_in_place_invalidates_the_selection_once_bumped(preview):
    window = preview
    three(window)
    select(window, [2])
    window.footprint_drawings.pop(0)            # in place: the list object is the same
    window._bump_fp_drawings_revision()
    assert window._fp_selected_indices() == [], "index 2 no longer names the same drawing"
    # Twin: without a change the selection stays.
    select(window, [0])
    assert window._fp_selected_indices() == [0]


def test_the_applied_state_swap_keeps_a_valid_selection(preview):
    """Generating with the applied footprint swaps the drawings and back: the draft's selection stays."""
    window = preview
    three(window)
    window.apply_footprint_draft()
    window._delete_drawings([0])                # a pending draft: the swap really happens
    assert window.has_pending_footprint_edits()
    select(window, [1])
    with window._applied_footprint_in_memory():
        assert len(window.footprint_drawings) == 3, "the applied list is in place"
    assert window._fp_selected_indices() == [1]
    # Twin: a real replacement after the swap clears it.
    window.footprint_drawings = deepcopy(window.footprint_drawings)
    assert window._fp_selected_indices() == []


# ── Clipboard per project ─────────────────────────────────────────────

def _open(window, monkeypatch, name):
    from PyQt6.QtWidgets import QDialog
    from kicadgen.gui.project_dialogs import OpenProjectDialog
    monkeypatch.setattr(OpenProjectDialog, "selected_name", lambda self: name)
    monkeypatch.setattr(OpenProjectDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window._open_project()


def test_opening_another_project_empties_the_footprint_clipboard(window, monkeypatch):
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "B" / "docs").mkdir(parents=True)
    window.footprint_drawings = validate_footprint_drawings([record()])
    window._copy_drawings([0])
    assert window._fp_clipboard
    _open(window, monkeypatch, "B")
    assert not window._fp_clipboard
    assert not window._paste_footprint_drawings()


def test_the_clipboard_survives_within_the_same_project(window):
    """Negative twin: no project change, the paste still works."""
    window.footprint_drawings = validate_footprint_drawings([record()])
    window._copy_drawings([0])
    window._draw_preview()
    assert window._paste_footprint_drawings()


def test_opening_another_project_empties_the_symbol_clipboard_too(window, monkeypatch):
    from kicadgen.gui import main_window as gui
    if not hasattr(window.symbol_tab, "clear_clipboard"):
        pytest.skip("SymbolTab.clear_clipboard not available yet (agent S)")
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    (window._library_root() / "B" / "docs").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(window.symbol_tab, "clear_clipboard", lambda: calls.append(True))
    _open(window, monkeypatch, "B")
    assert calls == [True]
