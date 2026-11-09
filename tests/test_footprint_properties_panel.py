"""@brief Live «Proprietà» panel of the footprint preview (plan docs/footprint-drawing-kicad-plan.md, C2).

The panel is the shared `canvas_properties.PropertiesPanel` the symbol editor
uses. It follows the SELECTION (never the hovered drawing); one drawing shows
the fields of «Proprietà elemento…» (one shared description), several drawings
the layer, stroke width and fill, pads their number, position and size
read-only. A committed field is one undo step through the dialog's own path
(contract 1 and 11); a refresh never commits. Each rule has a positive case
and a negative one that differ structurally.
"""
from copy import deepcopy

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPointF, Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QCheckBox, QComboBox, QLabel  # noqa: E402

from kicadgen.core.footprint import USER_TEXT_ANCHOR, validate_footprint_drawings  # noqa: E402
from kicadgen.core.package_geometry import PackageGeometry, PinSpec  # noqa: E402

#: Scene px per mm of the preview.
PX = 20.0


def record(kind="line", points=((0, 0), (5, 5)), layer="F.SilkS", **extra):
    """A drawing record before validation."""
    base = {"kind": kind, "layer": layer, "width": 0.12, "points": [list(p) for p in points]}
    base.update(extra)
    return base


def set_drawings(window, raws):
    """Replace the drawings and redraw, so the scene items name the new records."""
    window.footprint_drawings = validate_footprint_drawings(raws)
    window._draw_preview()
    return window.footprint_drawings


#: One record per kind the panel must describe, with the field keys it shows.
KINDS = {
    "line": (record(), ["layer", "width", "x", "y"]),
    "rectangle": (record("rectangle", ((0, 0), (4, 2))),
                  ["layer", "width", "x", "y", "fill", "rect_width", "rect_height"]),
    "circle": (record("circle", ((0, 0), (2, 0))), ["layer", "width", "x", "y", "fill", "radius"]),
    "polygon": (record("polygon", ((0, 0), (4, 0), (2, 3))), ["layer", "width", "x", "y", "fill"]),
    "text": (record("text", ((1, 1),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR),
             ["layer", "x", "y", "text", "size", "angle"]),
    "textbox": ({"kind": "textbox", "layer": "Dwgs.User", "width": 0.1, "points": [[0.0, 5.0], [10.0, 9.0]],
                 "text": "Hello\nWorld", "size": 1.0},
                ["layer", "width", "x", "y", "rect_width", "rect_height", "text", "size", "angle", "border"]),
    "radial": ({"kind": "dimension", "layer": "F.Fab", "width": 0.1, "points": [[0.0, -10.0], [2.0, -12.0]],
                "dtype": "radial", "leader_length": 3.81},
               ["layer", "width", "x", "y", "radius", "leader_length"]),
    "leader": ({"kind": "dimension", "layer": "Cmts.User", "width": 0.1, "points": [[-10.0, 10.0], [-7.0, 7.0]],
                "dtype": "leader", "text": "Pin 1", "frame": "circle"},
               ["layer", "width", "x", "y", "text", "frame"]),
}


@pytest.fixture
def shown(window, qapp, monkeypatch):
    """The preview tab shown, camera at 1 px per scene px; a line, a rectangle and a circle 10 mm up."""
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
    set_drawings(window, [
        record(points=((0, -10), (5, -10))),
        record("rectangle", ((8, -11), (10, -9))),
        record("circle", ((15, -10), (16, -10)))])
    window._draw_preview()
    view = window.preview_view
    view.resetTransform()
    view.centerOn(0, 0)
    view.auto_fit = False
    qapp.processEvents()
    return window


def panel(window):
    return window.fp_properties_panel


def commits(window):
    """Record every `committed` signal of the panel."""
    seen = []
    panel(window).committed.connect(lambda key, value: seen.append((key, value)))
    return seen


def type_into(window, key, value):
    """Type `value` in a numeric field and press Enter, as a user does."""
    box = panel(window).field_widget(key)
    box.setFocus()
    box.lineEdit().selectAll()
    QTest.keyClicks(box.lineEdit(), box.textFromValue(value))
    QTest.keyClick(box.lineEdit(), Qt.Key.Key_Return)


def choose(combo, data):
    """Pick the entry holding `data` as a user does (activated)."""
    index = combo.findData(data)
    assert index >= 0
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


def placeholder_shown(window):
    return not panel(window)._placeholder.isHidden() and panel(window)._form_host.isHidden()


# ── Fields ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", list(KINDS))
def test_one_drawing_shows_the_dialog_fields_of_its_kind(shown, monkeypatch, kind):
    window = shown
    raw, keys = KINDS[kind]
    set_drawings(window, [raw])
    assert window.footprint_drawings, "the record is valid"
    window._select_footprint_drawing(0)
    assert panel(window).field_keys() == keys
    # The dialog shows the same rows (one shared description).
    from PyQt6.QtWidgets import QDialog, QFormLayout
    captions = []

    def fake_exec(dialog):
        form = dialog.findChild(QFormLayout)
        for row in range(form.rowCount()):
            for role in (QFormLayout.ItemRole.LabelRole, QFormLayout.ItemRole.SpanningRole):
                item = form.itemAt(row, role)
                widget = item.widget() if item is not None else None
                if isinstance(widget, QCheckBox) or (isinstance(widget, QLabel) and not widget.wordWrap()):
                    captions.append(widget.text())
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    window._edit_drawing_properties(0)
    assert captions == [s.label for s in window._fp_properties_fields()[1]]


def test_a_text_box_shows_its_lines_joined_by_the_mark_and_commits_them_back(shown):
    window = shown
    set_drawings(window, [KINDS["textbox"][0]])
    window._select_footprint_drawing(0)
    edit = panel(window).field_widget("text")
    assert edit.text() == "Hello⏎World"
    edit.setFocus()
    edit.selectAll()
    QTest.keyClicks(edit, "A")
    # The mark is inserted, not typed: QTest has no key for a character outside the keyboard map.
    edit.insert("⏎")
    QTest.keyClicks(edit, "B")
    QTest.keyClick(edit, Qt.Key.Key_Return)
    assert window.footprint_drawings[0]["text"] == "A\nB"


# ── Commit = one undo step, refresh never commits ─────────────────────

def test_typing_a_width_and_enter_is_one_undo_step(shown):
    window = shown
    window._select_footprint_drawing(1)
    depth = len(window._fp_drawings_undo)
    type_into(window, "width", 0.25)
    assert window.footprint_drawings[1]["width"] == 0.25
    assert len(window._fp_drawings_undo) == depth + 1
    assert window._fp_selected_indices() == [1], "the edited drawing stays selected"
    # Negative twin: Enter again on the same value commits nothing.
    QTest.keyClick(panel(window).field_widget("width").lineEdit(), Qt.Key.Key_Return)
    assert len(window._fp_drawings_undo) == depth + 1


def test_undo_shows_the_old_value_and_commits_nothing(shown):
    window = shown
    window._select_footprint_drawing(1)
    type_into(window, "width", 0.25)
    seen = commits(window)
    depth = len(window._fp_drawings_undo)
    window._undo_footprint_drawing()
    window._select_footprint_drawing(1)
    assert panel(window).shown_value("width") == pytest.approx(0.12)
    assert seen == [] and len(window._fp_drawings_undo) == depth - 1
    # Repeated refreshes never commit either (contract 11).
    window._refresh_fp_properties()
    window._draw_preview()
    assert seen == []


def test_a_panel_commit_equals_the_dialog_result(shown, monkeypatch):
    window = shown
    original = deepcopy(window.footprint_drawings)
    window._select_footprint_drawing(1)
    type_into(window, "y", panel(window).shown_value("y") + 1.0)
    by_panel = deepcopy(window.footprint_drawings[1])
    window.footprint_drawings = deepcopy(original)
    from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout

    def fake_exec(dialog):
        form = dialog.findChild(QFormLayout)
        for row in range(form.rowCount()):
            label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if label is not None and label.widget().text() == "Y (mm)":
                box = form.itemAt(row, QFormLayout.ItemRole.FieldRole).widget()
                assert isinstance(box, QDoubleSpinBox)
                box.setValue(box.value() + 1.0)
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        return dialog.result()

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    window._edit_drawing_properties(1)
    assert window.footprint_drawings[1] == by_panel != original[1]


def test_an_invalid_value_is_refused_and_the_shown_value_restored(shown, monkeypatch):
    window = shown
    toasts = []
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda title, body: toasts.append((title, body)))
    window._select_footprint_drawing(0)
    before = deepcopy(window.footprint_drawings)
    depth = len(window._fp_drawings_undo)
    x = panel(window).shown_value("x")
    type_into(window, "x", 600.0)                  # beyond the +-500 mm area
    assert window.footprint_drawings == before and len(window._fp_drawings_undo) == depth
    assert panel(window).field_widget("x").value() == pytest.approx(x)
    assert panel(window).shown_value("x") == pytest.approx(x)
    assert len(toasts) == 1 and toasts[0][0] == "Modifica scartata." and "area consentita" in toasts[0][1]
    # Negative twin: a value inside the area is applied, no toast.
    type_into(window, "x", x + 1.0)
    assert window.footprint_drawings[0]["points"] == [[1, -10], [6, -10]] and len(toasts) == 1


# ── Several drawings ───────────────────────────────────────────────────

def test_several_drawings_mixed_width_is_untouched_by_a_fill_commit(shown):
    window = shown
    set_drawings(window, [
        record("rectangle", ((0, 0), (4, 2))),
        record("circle", ((10, 0), (12, 0)), width=0.2)])
    window._fp_selection.set_many([0, 1], False)
    window._refresh_drawing_selection()
    assert panel(window).field_keys() == ["layer", "width", "fill"]
    width_box = panel(window).field_widget("width")
    assert width_box.is_mixed() and panel(window).shown_value("width") is None
    depth = len(window._fp_drawings_undo)
    panel(window).field_widget("fill").click()
    assert [d.get("fill") for d in window.footprint_drawings] == [True, True]
    assert [d["width"] for d in window.footprint_drawings] == [0.12, 0.2], "the mixed width is not written"
    assert len(window._fp_drawings_undo) == depth + 1, "one undo step for both"
    assert window._fp_selected_indices() == [0, 1]


def test_several_drawings_offer_fill_only_when_all_are_fillable(shown):
    window = shown
    # The fixture's line, rectangle and circle: the line has no fill.
    window._fp_selection.set_many([0, 1], False)
    window._refresh_drawing_selection()
    assert panel(window).field_keys() == ["layer", "width"]
    # A text among them drops the stroke width too (a text has no stroke row).
    set_drawings(window, [
        record(), record("text", ((1, 1),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR)])
    window._fp_selection.set_many([0, 1], False)
    window._refresh_drawing_selection()
    assert panel(window).field_keys() == ["layer"]


def test_a_layer_change_from_the_panel_moves_the_drawing(shown):
    window = shown
    window._select_footprint_drawing(0)
    depth = len(window._fp_drawings_undo)
    combo = panel(window).field_widget("layer")
    assert isinstance(combo, QComboBox) and combo.currentData() == "F.SilkS"
    choose(combo, "F.Fab")
    assert window.footprint_drawings[0]["layer"] == "F.Fab"
    assert window.footprint_drawings[1]["layer"] == "F.SilkS", "only the selected drawing"
    assert len(window._fp_drawings_undo) == depth + 1
    # Several drawings: one step moves them all.
    window._fp_selection.set_many([1, 2], False)
    window._refresh_drawing_selection()
    choose(panel(window).field_widget("layer"), "Cmts.User")
    assert [d["layer"] for d in window.footprint_drawings] == ["F.Fab", "Cmts.User", "Cmts.User"]
    assert len(window._fp_drawings_undo) == depth + 2


# ── Pads, empty selection ──────────────────────────────────────────────

def test_selected_pads_are_shown_read_only(shown):
    window = shown
    window.package_geometry = PackageGeometry(
        family="soic", body_length_mm=15, body_width_mm=10,
        pins=[PinSpec("1", -3.0, -2.0, 1.2, .6), PinSpec("2", 3.0, -2.0, 1.2, .6)])
    window._draw_preview()
    window._select_footprint_pad(1)
    assert panel(window).field_keys() == ["number", "x", "y", "size"]
    assert all(isinstance(panel(window).field_widget(k), QLabel) for k in panel(window).field_keys())
    assert panel(window).field_widget("number").text() == "2"
    assert panel(window).shown_value("x") == pytest.approx(3.0)
    assert panel(window).field_widget("size").text() == "1.2 × 0.6 mm"
    window._select_footprint_pad(0, additive=True)
    assert panel(window).field_keys() == ["numbers"]
    assert panel(window).field_widget("numbers").text() == "1, 2"


def test_no_selection_shows_the_placeholder(shown):
    window = shown
    assert placeholder_shown(window)
    window._select_footprint_drawing(0)
    assert not placeholder_shown(window)
    window._clear_footprint_selection()
    assert placeholder_shown(window)


def test_the_panel_follows_the_selection_not_the_hover(shown):
    window = shown
    window._fp_hovered_drawing = 1
    window._refresh_fp_properties()
    assert placeholder_shown(window), "a hovered drawing alone fills nothing"


# ── E shortcut ─────────────────────────────────────────────────────────

def test_e_focuses_the_panel_with_a_selection(shown, qapp, monkeypatch):
    window = shown
    opened = []
    monkeypatch.setattr(window, "_edit_drawing_properties", lambda index: opened.append(index))
    window._select_footprint_drawing(0)
    window.fp_properties_group.setExpanded(False)
    # CONTRACT CHANGED (verification 2026-11-09, D4): the REAL pointer rests on drawing 2
    # (the press target E reads), not an assigned `_fp_hovered_drawing`.
    from test_footprint_outline_hit import pointer_on_drawing
    pointer_on_drawing(window, qapp, 2)
    assert window._fp_selected_indices() == [0], "hovering does not change the selection"
    assert window._edit_footprint_target_properties()
    qapp.processEvents()
    assert window.fp_properties_group.isExpanded()
    assert QApplication.focusWidget() is panel(window).field_widget("layer")
    assert opened == [], "the selection wins over the hovered drawing"
    # Negative twin: nothing selected, the drawing under the pointer opens its dialog.
    # CONTRACT CHANGED (verification 2026-11-09, D4): "under the pointer" is the drawing a
    # press there takes (`FootprintView.drawing_under_pointer`), so the pointer is moved onto
    # the circle's rim instead of setting the hovered index (which only reveals the handles).
    window._clear_footprint_selection()
    view = window.preview_view
    QTest.mouseMove(view.viewport(), view.mapFromScene(QPointF(16 * PX, -10 * PX)))
    qapp.processEvents()
    assert window._edit_footprint_target_properties()
    assert opened == [2]


# ── Drag ───────────────────────────────────────────────────────────────

def test_a_drag_does_not_refresh_the_panel_at_each_move(shown, monkeypatch):
    window = shown
    window._select_footprint_drawing(0)
    calls = []
    real = window._fp_properties_fields
    monkeypatch.setattr(window, "_fp_properties_fields", lambda: calls.append(1) or real())
    viewport = window.preview_view.viewport()

    def vp(x_mm, y_mm):
        return window.preview_view.mapFromScene(QPointF(x_mm * PX, y_mm * PX))
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(2.5, -10))
    calls.clear()
    for x in (3.0, 3.5, 4.0, 4.5, 5.0, 5.5):
        QTest.mouseMove(viewport, vp(x, -10))
    assert window.preview_view.gestures.active
    # The hooks a move may reach (a pad or body drag redraws the preview at
    # each step, a group drag restyles the selection) are skipped too.
    window._refresh_drawing_selection()
    window._after_preview_drawn(None)
    assert calls == [], "no refresh per pointer move"
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(5.5, -10))
    assert window.footprint_drawings[0]["points"] == [[3, -10], [8, -10]]
    assert calls, "the panel catches up at the drop"
    assert panel(window).shown_value("x") == pytest.approx(5.5)
