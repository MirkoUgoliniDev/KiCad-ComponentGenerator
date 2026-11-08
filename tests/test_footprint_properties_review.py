"""@brief «Proprietà» panel fixes of the 2026-11-08 review (wave F2): M10, M11, M12, B4, B5, B9.

Positions in the drawing-origin frame with Y up in the panel AND in the
drawing dialog (contract 16), the panel in the new language after a switch,
a typed value surviving a refresh, no commit while the applied footprint is
swapped in, a text the one-line field cannot give back shown read-only, and
the text length limit. Each rule has a positive and a negative case.
"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402

from kicadgen.core.package_geometry import PackageGeometry, PinSpec  # noqa: E402
from test_footprint_properties_panel import commits, panel, record, set_drawings, shown, type_into  # noqa: E402,F401


def _use_origin(window, monkeypatch, wanted_prefix):
    """Make the first origin point whose key starts with `wanted_prefix` the drawing origin.

    Set through monkeypatch, so the project state is the saved one again
    before the window closes (a changed state would ask to save it).
    """
    key = next(k for k in window._fp_origin_points if k.startswith(wanted_prefix)
               and window._fp_origin_points[k] != (0.0, 0.0))
    monkeypatch.setattr(window, "footprint_drawing_origin", key)
    window._apply_drawing_origin()
    return window._fp_origin_points[key]


# ── M10: the drawing-origin frame, Y up ────────────────────────────────

def test_the_panel_shows_positions_from_the_drawing_origin_with_y_up(shown, monkeypatch):
    window = shown
    ox, oy = _use_origin(window, monkeypatch, "corner:")
    assert (ox, oy) != (0.0, 0.0)
    window._select_footprint_drawing(1)                     # rectangle centred at (9, -10)
    assert panel(window).shown_value("x") == pytest.approx(9.0 - ox)
    assert panel(window).shown_value("y") == pytest.approx(oy + 10.0), "Y up: above the origin is positive"
    assert (panel(window).shown_value("x"), panel(window).shown_value("y")) == pytest.approx(
        window._to_drawing_mm(9.0, -10.0)), "the frame of the pad dialog and of the pointer line"
    before = [list(p) for p in window.footprint_drawings[1]["points"]]
    type_into(window, "y", panel(window).shown_value("y") + 1.0)
    after = window.footprint_drawings[1]["points"]
    assert after[0][1] == pytest.approx(before[0][1] - 1.0), "one more in Y up = one mm UP the screen"
    assert after[0][0] == pytest.approx(before[0][0]), "X untouched"


def test_the_drawing_dialog_uses_the_same_frame_as_the_panel(shown, monkeypatch):
    from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout
    window = shown
    ox, oy = _use_origin(window, monkeypatch, "corner:")
    shown_values = {}

    def fake_exec(dialog):
        form = dialog.findChild(QFormLayout)
        for row in range(form.rowCount()):
            label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if label is not None and label.widget().text() in ("X (mm)", "Y (mm)"):
                box = form.itemAt(row, QFormLayout.ItemRole.FieldRole).widget()
                assert isinstance(box, QDoubleSpinBox)
                shown_values[label.widget().text()] = box.value()
                if label.widget().text() == "X (mm)":
                    box.setValue(box.value() + 2.0)
        dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
        return dialog.result()

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    window._edit_drawing_properties(1)
    # CONTRACT CHANGED (contract 16): the dialog shows the drawing-origin frame.
    assert shown_values["X (mm)"] == pytest.approx(9.0 - ox) and shown_values["Y (mm)"] == pytest.approx(oy + 10.0)
    assert window.footprint_drawings[1]["points"] == [[10, -11], [12, -9]], "X +2 moves right, Y unchanged"


def test_a_selected_pad_shows_the_frame_of_its_dialog(shown):
    window = shown
    window.package_geometry = PackageGeometry(family="soic", body_length_mm=15, body_width_mm=10,
                                              pins=[PinSpec("1", -3.0, -2.0, 1.2, .6),
                                                    PinSpec("2", 3.0, -2.0, 1.2, .6)])
    window._draw_preview()
    window._select_footprint_pad(1)
    seen = window.package_geometry.pins_seen_from_top()[1]
    expected = window._to_drawing_mm(seen.x_mm, seen.y_mm)
    assert (panel(window).shown_value("x"), panel(window).shown_value("y")) == pytest.approx(expected)
    if window._drawing_origin_mm()[1] != seen.y_mm:
        assert panel(window).shown_value("y") != pytest.approx(seen.y_mm), "not the Y-down footprint value"


# ── M11: the panel follows the language ────────────────────────────────

def test_switching_the_language_rewrites_the_panel(shown, qapp):
    window = shown
    window._select_footprint_drawing(0)
    combo = panel(window).field_widget("layer")
    italian = [combo.itemText(i) for i in range(combo.count())]
    window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
    qapp.processEvents()
    combo = panel(window).field_widget("layer")
    english = [combo.itemText(i) for i in range(combo.count())]
    assert english != italian and english[0] == window.i18n.text("Serigrafia (stampata)")
    assert panel(window).mixed_placeholder() == window.i18n.text("(vari)") != "(vari)"
    window._toggle_footprint_drawing_selection(1)
    assert panel(window)._title.text() == window.i18n.text("{count} elementi").format(count=2)
    assert "elementi" not in panel(window)._title.text()
    window.language_combo.setCurrentIndex(window.language_combo.findData("it"))
    qapp.processEvents()
    assert panel(window)._title.text() == "2 elementi" and panel(window).mixed_placeholder() == "(vari)"


# ── M12: a typed value survives a refresh ──────────────────────────────

def test_a_typed_uncommitted_width_survives_an_undo_click_refresh(shown, qapp):
    from PyQt6.QtWidgets import QApplication
    window = shown
    window._select_footprint_drawing(0)
    window.activateWindow()
    box = panel(window).field_widget("width")
    box.setFocus()
    QApplication.processEvents()
    if not (box.hasFocus() or box.lineEdit().hasFocus()):
        pytest.skip("the platform gives no keyboard focus offscreen")
    box.lineEdit().selectAll()
    QTest.keyClicks(box.lineEdit(), box.textFromValue(0.33))      # typed, NOT committed
    typed = box.lineEdit().text()
    window._draw_preview()                                         # what an Undo click's redraw does
    window._refresh_fp_properties()
    assert box.lineEdit().text() == typed, "the typed text is kept"
    QTest.keyClick(box.lineEdit(), Qt.Key.Key_Return)
    assert window.footprint_drawings[0]["width"] == 0.33
    # Negative: without a typed edit a refresh shows the stored value.
    window._select_footprint_drawing(1)
    window._refresh_fp_properties()
    assert panel(window).shown_value("width") == pytest.approx(0.12)


# ── B4: no commit while the applied footprint is swapped in ───────────

def test_panel_commits_are_ignored_during_the_applied_swap(shown, monkeypatch):
    window = shown
    window._select_footprint_drawing(0)
    monkeypatch.setattr(window, "has_pending_footprint_edits", lambda: True)
    window._fp_applied = {"drawings": [dict(r) for r in window.footprint_drawings], "texts": {},
                          "pad_overrides": {}, "package_pins": None}
    before = [dict(r) for r in window.footprint_drawings]
    with window._applied_footprint_in_memory():
        window._fp_property_committed("width", 0.5)
    assert window.footprint_drawings == before, "nothing written to the applied copy or the draft"
    window._fp_property_committed("width", 0.5)
    assert window.footprint_drawings[0]["width"] == 0.5, "outside the swap the commit applies"


# ── B5, B9: text fields ────────────────────────────────────────────────

def test_a_text_box_with_a_literal_mark_is_read_only_in_the_panel(shown):
    window = shown
    set_drawings(window, [{"kind": "textbox", "layer": "Dwgs.User", "width": 0.1,
                           "points": [[0.0, 5.0], [10.0, 9.0]], "text": "A⏎B", "size": 1.0},
                          {"kind": "textbox", "layer": "Dwgs.User", "width": 0.1,
                           "points": [[0.0, 15.0], [10.0, 19.0]], "text": "A\nB", "size": 1.0}])
    window._select_footprint_drawing(0)
    spec = panel(window)._specs["text"]
    assert spec.kind == "readonly" and "Proprietà elemento" in spec.tooltip
    window._select_footprint_drawing(1)
    assert panel(window)._specs["text"].kind == "text", "a plain multi-line box stays editable"


def test_text_fields_hold_at_most_200_characters(shown):
    from kicadgen.core.footprint import USER_TEXT_ANCHOR
    window = shown
    set_drawings(window, [record("text", ((1, 1),), text="HI", size=1.0, anchor=USER_TEXT_ANCHOR),
                          record("text", ((1, 5),), text="X" * 250, size=1.0, anchor=USER_TEXT_ANCHOR)])
    window._select_footprint_drawing(0)
    assert panel(window)._specs["text"].max_length == 200
    assert panel(window).field_widget("text").maxLength() == 200
    window._select_footprint_drawing(1)
    assert panel(window)._specs["text"].kind == "readonly", "a longer stored text would be cut: dialog only"
