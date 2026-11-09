"""@brief Editable body dimensions of the package preview (user, 2026-11-03).

Agreed behaviour (HANDOFF 03/11): the two outer dimensions of a declared body
square to the screen show the body sides as a number only and are editable
(double click, right click «Modifica quota…»); the typed value becomes that
body size, the body stays centred and the pads do not move, as one undo step
of the footprint draft. When a pad reaches past the body, the computed overall
size («totale») is drawn as well, further out and not editable; when the pads
stay inside, it would repeat the body numbers and is not drawn. Each rule has
a positive case and a negative one that differ structurally (pads inside vs
outside the body, body square to the screen vs turned by 45 degrees).
"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QGraphicsSimpleTextItem  # noqa: E402

from kicadgen.core.package_geometry import PackageGeometry, PinSpec  # noqa: E402

from test_footprint_pad_drag import _show, top  # noqa: E402

#: Through-hole pads well inside a 22.5 x 16.5 mm body (a relay-like part).
INSIDE = [PinSpec("1", -6.0, -6.0, 1.3, 1.3, pad_type="thru_hole", drill_mm=.8, shape="circle"),
          PinSpec("2", 6.0, -6.0, 1.3, 1.3, pad_type="thru_hole", drill_mm=.8, shape="circle"),
          PinSpec("3", 6.0, 6.0, 1.3, 1.3, pad_type="thru_hole", drill_mm=.8, shape="circle"),
          PinSpec("4", -6.0, 6.0, 1.3, 1.3, pad_type="thru_hole", drill_mm=.8, shape="circle")]
#: SMD leads reaching past a 4 x 4 mm body on the left and right.
OUTSIDE = [PinSpec("1", -3.0, -1.0, 1.5, .6), PinSpec("2", 3.0, -1.0, 1.5, .6),
           PinSpec("3", 3.0, 1.0, 1.5, .6), PinSpec("4", -3.0, 1.0, 1.5, .6)]


def _package(pins, length, width, angle=0.0):
    return PackageGeometry(family="soic", body_length_mm=length, body_width_mm=width,
                           orientation_deg=angle, pins=list(pins))


def _dimensions(window):
    """[(label text, edit key or None)] of the preview's dimension labels."""
    from kicadgen.gui.canvas_measure import DIMENSION_COLOR, DIMENSION_EDIT_ROLE
    from PyQt6.QtGui import QColor
    colour = QColor(DIMENSION_COLOR)
    return sorted(((item.text(), item.data(DIMENSION_EDIT_ROLE))
                   for item in window.preview_scene.items()
                   if isinstance(item, QGraphicsSimpleTextItem) and item.brush().color() == colour),
                  key=lambda entry: (entry[1] or "", entry[0]))


@pytest.fixture
def inside(window, qapp, monkeypatch):
    return _show(window, qapp, monkeypatch, _package(INSIDE, 22.5, 16.5))


def test_pads_inside_the_body_give_two_editable_body_numbers_and_no_total(inside):
    assert _dimensions(inside) == [("22.50 mm", "body_x"), ("16.50 mm", "body_y")]


def test_pads_past_the_body_add_a_computed_total_that_is_not_editable(window, qapp, monkeypatch):
    _show(window, qapp, monkeypatch, _package(OUTSIDE, 4.0, 4.0))
    found = _dimensions(window)
    assert ("4.00 mm", "body_x") in found and ("4.00 mm", "body_y") in found
    totals = [entry for entry in found if entry[1] is None]
    # CONTRACT CHANGED 2026-11-03 (second review, U6): the total is drawn per
    # AXIS where pads stick out; these leads stick out left and right only,
    # so only the horizontal total (leads from -3.75 to 3.75 mm) appears.
    assert [text for text, _key in totals] == [window._dim_label("dim.total", 7.5)]


def test_the_default_value_text_never_covers_a_body_number(window, qapp, monkeypatch):
    """U3: the body dimensions are laid out beyond the footprint texts facing them."""
    from kicadgen.gui.canvas_measure import DIMENSION_EDIT_ROLE
    from kicadgen.gui.footprint_view import TEXT_KEY_ROLE
    _show(window, qapp, monkeypatch, _package(INSIDE, 22.5, 16.5))
    labels = [item.sceneBoundingRect() for item in window.preview_scene.items()
              if isinstance(item, QGraphicsSimpleTextItem) and item.data(DIMENSION_EDIT_ROLE)]
    texts = [item.sceneBoundingRect() for item in window.preview_scene.items()
             if item.data(TEXT_KEY_ROLE) is not None and item.parentItem() is None]
    assert labels and texts
    assert not any(label.intersects(text) for label in labels for text in texts)


def test_a_body_turned_by_45_degrees_keeps_only_the_computed_total(window, qapp, monkeypatch):
    _show(window, qapp, monkeypatch, _package(INSIDE, 22.5, 16.5, angle=45.0))
    found = _dimensions(window)
    assert found and all(key is None for _text, key in found)


def test_editing_the_horizontal_side_changes_the_body_length_and_no_pad(inside, monkeypatch):
    window = inside
    pads = top(window)
    undo = len(window._fp_drawings_undo)
    monkeypatch.setattr(window, "_ask_measure_value", lambda current: 25.0)
    assert window._edit_body_dimension("body_x")
    assert (window.package_geometry.body_length_mm, window.package_geometry.body_width_mm) == (25.0, 16.5)
    assert top(window) == pads, "the pads keep their positions"
    assert len(window._fp_drawings_undo) == undo + 1 and window.has_pending_footprint_edits()
    assert ("25.00 mm", "body_x") in _dimensions(window)
    window._undo_footprint_drawing()
    assert window.package_geometry.body_length_mm == 22.5


def test_on_a_body_turned_by_90_degrees_the_horizontal_side_is_the_width(window, qapp, monkeypatch):
    _show(window, qapp, monkeypatch, _package(INSIDE, 22.5, 16.5, angle=90.0))
    assert _dimensions(window) == [("16.50 mm", "body_x"), ("22.50 mm", "body_y")]
    monkeypatch.setattr(window, "_ask_measure_value", lambda current: 18.0)
    assert window._edit_body_dimension("body_x")
    assert (window.package_geometry.body_length_mm, window.package_geometry.body_width_mm) == (22.5, 18.0)


def test_a_size_out_of_range_is_refused_and_changes_nothing(inside, monkeypatch):
    window = inside
    shown = []
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda title, text: shown.append(title))
    monkeypatch.setattr(window, "_ask_measure_value", lambda current: 0.0)
    assert not window._edit_body_dimension("body_y")
    assert window.package_geometry.body_width_mm == 16.5 and not window.has_pending_footprint_edits()
    assert shown, "the refusal is said"


def test_the_applied_body_reaches_the_project_and_rewrites_the_step_too(inside, monkeypatch):
    window = inside
    calls = []
    monkeypatch.setattr(window, "_schedule_footprint_rewrite", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(window, "_ask_measure_value", lambda current: 20.0)
    window._edit_body_dimension("body_y")
    assert window.apply_footprint_draft()
    assert calls and calls[-1].get("body_edit") is True
    state = window._footprint_edit_state()
    assert state["package_body"] == (22.5, 20.0)
    assert window._footprint_state_from_json(window._footprint_state_json(state))["package_body"] == (22.5, 20.0)


def test_a_flushed_body_rewrite_is_a_full_generation(inside, monkeypatch):
    window = inside
    started = []
    monkeypatch.setattr(window, "_pipeline_owns_output", lambda: False)
    monkeypatch.setattr(window, "_generate", lambda **kwargs: started.append(kwargs) or True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_background_work_running", lambda: False)
    window._fp_rewrite_dirty, window._fp_rewrite_pad_edit, window._fp_rewrite_body_edit = True, True, True
    window._flush_footprint_rewrite()
    assert started and started[-1].get("footprint_only") is False
    window._fp_rewrite_dirty, window._fp_rewrite_pad_edit, window._fp_rewrite_body_edit = True, True, False
    window._flush_footprint_rewrite()
    assert started[-1].get("footprint_only") is True, "a pad-only edit stays footprint-only"


def test_a_double_click_on_a_body_number_asks_its_value(inside, qapp, monkeypatch):
    window = inside
    asked = []
    monkeypatch.setattr(window, "_edit_body_dimension", lambda key: asked.append(key) or False)
    window.preview_view.dimension_edit_requested.disconnect()
    window.preview_view.dimension_edit_requested.connect(window._edit_body_dimension)
    from kicadgen.gui.canvas_measure import DIMENSION_EDIT_ROLE
    del DIMENSION_EDIT_ROLE
    pos = _line_pos(window, "body_x")
    QTest.mouseDClick(window.preview_view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    assert asked == ["body_x"]
    assert window.preview_view._dimension_at(window.preview_view.mapFromScene(0.0, 0.0)) is None, \
        "the body centre is no dimension"


def test_a_recovery_file_of_the_earlier_version_is_still_offered(tmp_path):
    """A draft written before `package_body` existed is bound to the applied state without that key."""
    from kicadgen.services.footprint_draft import load_footprint_draft, save_footprint_draft
    old_baseline = {"drawings": [], "texts": {}, "pad_overrides": {}, "package_pins": None}
    old_draft = dict(old_baseline, drawings=[{"kind": "line"}])
    path = tmp_path / "draft.json"
    save_footprint_draft(path, old_baseline, old_draft)
    baseline = dict(old_baseline, package_body=[22.5, 16.5])
    assert load_footprint_draft(path, baseline) == dict(old_draft, package_body=[22.5, 16.5])
    other = dict(baseline, drawings=[{"kind": "circle"}])
    assert load_footprint_draft(path, other) is None, "negative: bound to another applied state"


def test_a_right_click_on_a_body_number_asks_for_its_menu(inside, qapp):
    window = inside
    seen = []
    window.preview_view.dimension_context_requested.disconnect()
    window.preview_view.dimension_context_requested.connect(lambda key, _pos: seen.append(key))
    from kicadgen.gui.canvas_measure import DIMENSION_EDIT_ROLE
    label = next(item for item in window.preview_scene.items()
                 if isinstance(item, QGraphicsSimpleTextItem) and item.data(DIMENSION_EDIT_ROLE) == "body_y")
    pos = window.preview_view.mapFromScene(label.sceneBoundingRect().center())
    QTest.mousePress(window.preview_view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseRelease(window.preview_view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    assert seen == ["body_y"]


# ── Review 2026-11-03 fixes ────────────────────────────────────────────

def _label_pos(window, key):
    from kicadgen.gui.canvas_measure import DIMENSION_EDIT_ROLE
    label = next(item for item in window.preview_scene.items()
                 if isinstance(item, QGraphicsSimpleTextItem) and item.data(DIMENSION_EDIT_ROLE) == key)
    return window.preview_view.mapFromScene(label.sceneBoundingRect().center())


def _line_pos(window, key):
    """A point on the dimension LINE near its first end: away from the Value text, which may cover the label."""
    from PyQt6.QtWidgets import QGraphicsLineItem
    from kicadgen.gui.canvas_measure import DIMENSION_EDIT_ROLE
    line = max((item for item in window.preview_scene.items()
                if isinstance(item, QGraphicsLineItem) and item.data(DIMENSION_EDIT_ROLE) == key),
               key=lambda item: item.line().length())
    point = line.line().pointAt(0.1)
    return window.preview_view.mapFromScene(line.mapToScene(point))


def _double_click(pos):
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent
    return QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(pos), QPointF(pos),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


def test_a_text_over_a_body_number_keeps_its_menu_and_its_double_click(inside, monkeypatch):
    """C1: on the same pixel the text wins with every button, as on a left press."""
    window = inside
    view = window.preview_view
    pos = _label_pos(window, "body_x")
    seen = []
    view.dimension_context_requested.disconnect()
    view.dimension_context_requested.connect(lambda key, _pos: seen.append(("dim", key)))
    view.dimension_edit_requested.disconnect()
    view.dimension_edit_requested.connect(lambda key: seen.append(("edit", key)))
    view.text_context_requested.disconnect()
    view.text_context_requested.connect(lambda key: seen.append(("text", key)))
    # A Value text item over the number: a real scene item with the text role, so the view's
    # shared precedence (`_pick`, texts above dimensions) is what decides, not a patched helper.
    from PyQt6.QtCore import QRectF
    from kicadgen.gui.footprint_view import TEXT_KEY_ROLE
    centre = view.mapToScene(pos)
    cover = window.preview_scene.addRect(QRectF(centre.x() - 6, centre.y() - 6, 12, 12))
    cover.setData(TEXT_KEY_ROLE, "Value")
    cover.setZValue(1000)
    QTest.mousePress(view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    assert seen == [("text", "Value")]
    seen.clear()
    view.mouseDoubleClickEvent(_double_click(pos))
    assert ("edit", "body_x") not in seen, "negative: the text above the number takes the double click"


def test_a_double_click_during_a_pan_edits_nothing(inside, monkeypatch):
    """C2: a running pan swallows the double click; without it the number is edited."""
    window = inside
    view = window.preview_view
    asked = []
    view.dimension_edit_requested.disconnect()
    view.dimension_edit_requested.connect(asked.append)
    pos = _line_pos(window, "body_x")
    monkeypatch.setattr(type(view.camera), "pan_active", property(lambda _self: True))
    view.mouseDoubleClickEvent(_double_click(pos))
    assert asked == []
    monkeypatch.undo()
    view.mouseDoubleClickEvent(_double_click(pos))
    assert asked == ["body_x"]


def test_the_hovered_body_number_is_highlighted(inside):
    """C4: hovering an editable number brightens it; leaving it restores the teal."""
    from PyQt6.QtGui import QColor
    from kicadgen.gui.canvas_measure import DIMENSION_COLOR, DIMENSION_EDIT_ROLE, MEASURE_HIGHLIGHT_COLOR
    window = inside
    view = window.preview_view
    label = next(item for item in window.preview_scene.items()
                 if isinstance(item, QGraphicsSimpleTextItem) and item.data(DIMENSION_EDIT_ROLE) == "body_y")
    view._hover_dimension(_label_pos(window, "body_y"))
    assert label.brush().color() == QColor(MEASURE_HIGHLIGHT_COLOR)
    view._hover_dimension(view.mapFromScene(0.0, 0.0))
    assert label.brush().color() == QColor(DIMENSION_COLOR)


def test_a_recovered_body_is_refused_on_a_package_without_body(window, qapp, monkeypatch):
    """M1/L9: no body on a body-less package, nothing out of range or overflowing; a valid one is kept."""
    _show(window, qapp, monkeypatch, PackageGeometry(family="soic", pins=list(OUTSIDE)))
    state = window._footprint_state_json(window._footprint_edit_state())
    assert window._footprint_state_from_json(dict(state, package_body=[30, 20]))["package_body"] is None
    _show(window, qapp, monkeypatch, _package(INSIDE, 22.5, 16.5))
    state = window._footprint_state_json(window._footprint_edit_state())
    restored = window._footprint_state_from_json
    assert restored(dict(state, package_body=[10 ** 400, 5]))["package_body"] == (22.5, 16.5)
    assert restored(dict(state, package_body=[0.0, 5]))["package_body"] == (22.5, 16.5)
    assert restored(dict(state, package_body=[25, 18]))["package_body"] == (25.0, 18.0)


def test_a_body_rewrite_without_analysis_or_footprint_is_refused(inside, monkeypatch):
    """M3: the rewrite of a body edit has the pad edit's precondition; with a footprint it starts."""
    window = inside
    monkeypatch.setattr(window, "_project_has_footprint", lambda: False)
    window._acquisition_completed = False
    window._fp_rewrite_flushing = True
    try:
        assert window._generate_applied(footprint_only=False) is False
    finally:
        window._fp_rewrite_flushing = False


def test_leaving_after_a_body_apply_rewrites_only_the_footprint(inside, monkeypatch):
    """M2: the leave path never waits for a STEP build; the flag is dropped first."""
    window = inside
    calls = []
    monkeypatch.setattr(window, "_flush_footprint_rewrite",
                        lambda: calls.append(window._fp_rewrite_body_edit))
    window._fp_rewrite_dirty, window._fp_rewrite_body_edit = True, True
    window._finish_footprint_rewrite_now()
    window._fp_rewrite_dirty, window._fp_rewrite_body_edit = True, False
    window._finish_footprint_rewrite_now()
    assert calls == [False, False]


def test_the_pinout_table_lists_every_pad_and_names_from_the_drawing(window, qapp, monkeypatch):
    """A1/A2 of the relay: unnamed pads appear, prefilled from the pad drawing; a pinout name wins."""
    pins = [PinSpec("11", -6.0, 0.0, 1.3, 1.3, name="NO", pad_type="thru_hole", drill_mm=.8, shape="circle"),
            PinSpec("A1", 6.0, -6.0, 1.3, 1.3, name="Coil+", pad_type="thru_hole", drill_mm=.8, shape="circle"),
            PinSpec("A2", 6.0, 6.0, 1.3, 1.3, name="", pad_type="thru_hole", drill_mm=.8, shape="circle")]
    _show(window, qapp, monkeypatch, _package(pins, 22.5, 16.5))
    monkeypatch.setattr(window, "_settle_pad_ids", lambda: ["11", "A1", "A2"])
    window.pinout = [{"pin": "11", "name": "Normally open"}]
    rows = {row["pin"]: row["name"] for row in window._pinout_rows_for_dialog()}
    assert rows == {"11": "Normally open", "A1": "Coil+", "A2": ""}


def test_applying_a_body_edit_updates_the_body_fields(inside, monkeypatch):
    """U5: after «Applica» the body fields show the applied body; before it they keep the applied one."""
    window = inside
    monkeypatch.setattr(window, "_ask_measure_value", lambda current: 25.0)
    window.field_body_length.setValue(22.5)
    window._edit_body_dimension("body_x")
    assert window.field_body_length.value() == 22.5, "a draft never reaches the fields"
    assert window.apply_footprint_draft()
    assert window.field_body_length.value() == 25.0


def test_an_untouched_pinout_ok_never_records_drawing_names_as_the_users(window, qapp, monkeypatch):
    """B3/U2: names prefilled from the pad drawing are applied only when the user changes them."""
    from PyQt6.QtWidgets import QDialog
    pins = [PinSpec("11", -6.0, 0.0, 1.3, 1.3, name="NO", pad_type="thru_hole", drill_mm=.8, shape="circle"),
            PinSpec("A1", 6.0, -6.0, 1.3, 1.3, name="Coil+", pad_type="thru_hole", drill_mm=.8, shape="circle")]
    _show(window, qapp, monkeypatch, _package(pins, 22.5, 16.5))
    monkeypatch.setattr(window, "_settle_pad_ids", lambda: ["11", "A1"])
    window.pinout = [{"pin": "11", "name": "NO"}]
    window._user_pin_names = {}
    monkeypatch.setattr(window, "_run_modal", lambda dialog: QDialog.DialogCode.Accepted)
    window._show_pinout_dialog()
    assert window._user_pin_names == {} and window.pinout == [{"pin": "11", "name": "NO"}]
    # Positive twin: a changed proposal is the user's.
    def edit_and_accept(dialog):
        row = next(r for r in range(dialog.table.rowCount()) if dialog.table.item(r, 0).text() == "A1")
        dialog.table.item(row, 1).setText("COIL_P")
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(window, "_run_modal", edit_and_accept)
    window._show_pinout_dialog()
    assert window._user_pin_names.get("A1") == "COIL_P"
