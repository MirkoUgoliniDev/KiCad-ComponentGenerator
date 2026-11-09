"""@brief Footprint preview: back layers, «Sposta ancora», keepout zones (plan F1, F2, F4).

Plan docs/footprint-back-anchor-vertices-zones-plan.md, contracts 1-8. Each
rule has a positive and a negative case that differ for a structural reason:
a back text is mirrored and a front one is not; an applied anchor moves the
3D model offset and an unapplied one does not; a zone row hides zones and not
silkscreen; and so on.
"""
import math
from copy import deepcopy

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPointF  # noqa: E402

from kicadgen.core.footprint import ZONE_KEEPOUT_DEFAULTS, validate_footprint_drawings  # noqa: E402
from kicadgen.core.footprint_layers import COPPER_COLOURS  # noqa: E402

MM = 20.0


def record(kind="line", points=((0, 0), (5, 5)), layer="F.SilkS", **extra):
    """A drawing record before validation."""
    base = {"kind": kind, "layer": layer, "width": 0.12, "points": [list(p) for p in points]}
    base.update(extra)
    return base


def zone(points=((0, 0), (4, 0), (4, 3), (0, 3)), layers=("F.Cu",), **flags):
    """A keepout zone record before validation (no drawing layer, no width)."""
    keepout = dict(ZONE_KEEPOUT_DEFAULTS)
    keepout.update(flags)
    return {"kind": "zone", "layers": list(layers), "keepout": keepout, "points": [list(p) for p in points]}


@pytest.fixture
def preview(window, monkeypatch):
    """The window with its preview drawn; no file written, rewrites recorded."""
    generations = []
    monkeypatch.setattr(window, "_generate", lambda **kwargs: generations.append(kwargs) or True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_schedule_footprint_rewrite",
                        lambda **_kwargs: window._generate(footprint_only=True))
    window.chk_fp_object_snap.setChecked(False)
    window.chk_grid.setChecked(False)
    window._draw_preview()
    view = window.preview_view
    view.resize(800, 600)
    view.resetTransform()
    view.centerOn(0, 0)
    window.generations = generations
    return window


def _items(window, index):
    from kicadgen.gui.footprint_view import DRAWING_KEY_ROLE
    return [item for item in window.preview_scene.items() if item.data(DRAWING_KEY_ROLE) == index]


def _show(window, records):
    window.footprint_drawings = validate_footprint_drawings(records)
    window._draw_preview()


# ── F1: back layers ────────────────────────────────────────────────────

def test_a_back_text_grows_left_of_its_anchor_and_a_front_one_right(preview):
    """kicad-cli 10 svg: a left-bottom back text is reflected about its anchor along its reading direction."""
    _show(preview, [record("text", ((10, 10),), text="ABC", size=1.0, anchor="left_bottom"),
                    record("text", ((10, 20),), layer="B.SilkS", text="ABC", size=1.0, anchor="left_bottom")])
    front = _items(preview, 0)[0].sceneBoundingRect()
    back = _items(preview, 1)[0].sceneBoundingRect()
    assert front.left() >= 10 * MM - 1 and front.right() > 10 * MM + 10, "front grows right of its anchor"
    assert back.right() <= 10 * MM + 1 and back.left() < 10 * MM - 10, "back grows left of its anchor"
    assert front.width() == pytest.approx(back.width(), abs=0.5), "same text, same size"


def test_a_vertical_back_text_grows_down_and_a_front_one_up(preview):
    _show(preview, [record("text", ((0, 0),), text="ABC", size=1.0, anchor="left_bottom", angle=90),
                    record("text", ((20, 0),), layer="B.Fab", text="ABC", size=1.0, anchor="left_bottom",
                           angle=90)])
    front = _items(preview, 0)[0].sceneBoundingRect()
    back = _items(preview, 1)[0].sceneBoundingRect()
    assert front.bottom() <= 1 and front.top() < -10, "a 90-degree front text reads up from its anchor"
    assert back.top() >= -1 and back.bottom() > 10, "a 90-degree back text grows down"


def test_a_back_textbox_text_is_reflected_about_the_box_centre(preview):
    box = ((0, 0), (20, 5))
    _show(preview, [record("textbox", box, text="AB", size=1.0),
                    record("textbox", box, layer="B.SilkS", text="AB", size=1.0)])
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    front = next(i for i in _items(preview, 0) if isinstance(i, QGraphicsSimpleTextItem)).sceneBoundingRect()
    back = next(i for i in _items(preview, 1) if isinstance(i, QGraphicsSimpleTextItem)).sceneBoundingRect()
    centre = 10 * MM
    assert front.left() < centre and back.right() > centre
    assert front.left() - 0 == pytest.approx(20 * MM - back.right(), abs=0.5), "mirror image about the centre"


def test_a_back_dimension_value_keeps_its_centre_and_is_mirrored(preview):
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    dim = dict(points=((0, 0), (10, 0)), offset=2.0)
    _show(preview, [record("dimension", **dim), record("dimension", layer="B.Fab", **dim)])
    labels = [next(i for i in _items(preview, n) if isinstance(i, QGraphicsSimpleTextItem)) for n in (0, 1)]
    front, back = (label.sceneBoundingRect() for label in labels)
    assert front.center().x() == pytest.approx(back.center().x(), abs=0.5)
    assert front.center().y() == pytest.approx(back.center().y(), abs=0.5)
    assert labels[0].sceneTransform().determinant() > 0, "negative: a front value is not mirrored"
    assert labels[1].sceneTransform().determinant() < 0, "a back value is reflected"


def test_back_drawings_lie_under_the_pads_and_front_ones_over(preview):
    _show(preview, [record(), record(layer="B.SilkS")])
    assert all(item.zValue() > 1 for item in _items(preview, 0)), "front over the copper (pads z 1)"
    assert all(item.zValue() < 0.5 for item in _items(preview, 1)), "back under the copper and the outlines"


def test_the_back_silkscreen_row_hides_only_back_drawings(preview):
    _show(preview, [record(), record(layer="B.SilkS")])
    preview.fp_layer_boxes["B.SilkS"].setChecked(False)
    try:
        assert all(not i.isVisible() for i in _items(preview, 1))
        assert all(i.isVisible() for i in _items(preview, 0)), "negative: the front silkscreen stays"
    finally:
        preview.fp_layer_boxes["B.SilkS"].setChecked(True)


def test_a_back_layer_can_be_the_active_layer_with_its_klc_defaults(preview):
    preview._activate_fp_draw_layer("B.Fab")
    assert preview._fp_active_layer() == "B.Fab"
    assert preview.spin_fp_draw_width.value() == pytest.approx(0.10)
    preview._toggle_footprint_draw_tool("line")
    preview._on_footprint_draw_clicked(0, 0)
    preview._on_footprint_draw_clicked(5 * MM, 0)
    preview._on_footprint_draw_finish()
    assert preview.footprint_drawings[-1]["layer"] == "B.Fab"
    assert preview.footprint_drawings[-1]["width"] == pytest.approx(0.10)


# ── F2: footprint anchor ───────────────────────────────────────────────

def test_moving_the_anchor_is_one_undo_step_and_moves_nothing_stored(preview):
    _show(preview, [record()])
    stored = deepcopy(preview.footprint_drawings)
    assert preview.set_footprint_anchor(1.0, 2.0)
    assert preview.footprint_anchor == (1.0, 2.0)
    assert preview.footprint_drawings == stored, "contract 1: stored coordinates never move"
    assert preview.has_pending_footprint_edits()
    assert not preview.set_footprint_anchor(1.0, 2.0), "negative: the same anchor is no edit"
    preview._undo_footprint_drawing()
    assert preview.footprint_anchor == (0.0, 0.0)
    preview._redo_footprint_drawing()
    assert preview.footprint_anchor == (1.0, 2.0)


def test_an_anchor_beyond_the_drawing_area_is_refused(preview):
    assert not preview.set_footprint_anchor(600.0, 0.0)
    assert preview.footprint_anchor == (0.0, 0.0)


def test_annulla_restores_the_applied_anchor_and_applica_saves_it(preview):
    assert preview.set_footprint_anchor(1.0, 0.0)
    preview.discard_footprint_draft()
    assert preview.footprint_anchor == (0.0, 0.0)
    assert preview.set_footprint_anchor(1.5, -0.5)
    preview.apply_footprint_draft()
    assert preview.applied_footprint_anchor() == (1.5, -0.5)
    assert preview._project_state()["footprint_anchor"] == [1.5, -0.5]


def test_the_model_offset_follows_the_applied_anchor_never_the_draft(preview):
    """Contract 7 / W1 fragile point: the delivery offset is the anchor the .kicad_mod was written with."""
    assert preview._kicad_model_offset() == (0.0, 0.0, 0.0)
    assert preview.set_footprint_anchor(1.0, 2.0)
    assert preview._kicad_model_offset() == (0.0, 0.0, 0.0), "negative: an unapplied anchor changes nothing"
    preview.apply_footprint_draft()
    assert preview._kicad_model_offset() == (-1.0, 2.0, 0.0), "measured sign: (-ax, ay, 0)"


def test_the_kicad_artifacts_carry_the_applied_offset(preview, monkeypatch):
    from kicadgen.gui import main_window as gui
    seen = {}
    monkeypatch.setattr(gui, "discover_component_artifacts",
                        lambda *args, **kwargs: seen.update(kwargs) or "artifacts")
    assert preview.set_footprint_anchor(0.5, 0.25)
    assert preview._current_kicad_artifacts() == "artifacts"
    assert seen["model_offset"] == (0.0, 0.0, 0.0), "draft only: offset of the applied (0, 0)"
    preview.apply_footprint_draft()
    preview._current_kicad_artifacts()
    assert seen["model_offset"] == (-0.5, 0.25, 0.0)


def test_the_alignment_signature_tells_a_file_with_another_anchor(preview):
    """A .kicad_mod written with another anchor has its pads elsewhere: the gate tells it apart."""
    from kicadgen.generators.pin_header import PinHeaderParams
    from kicadgen.gui.main_window import _pad_records_differ
    params = PinHeaderParams(name="H", rows=1, cols=2, pitch=2.54, row_spacing=2.54, drill=1.0,
                             pad_diameter=1.7)
    plain, _ = preview._footprint_signature_of(params)
    moved, _ = preview._footprint_signature_of(params, (), (1.27, 0.0))
    again, _ = preview._footprint_signature_of(params, (), (1.27, 0.0))
    assert _pad_records_differ(plain, moved), "another anchor, other pad positions"
    assert not _pad_records_differ(moved, again), "negative: the same anchor matches"
    assert plain[0][2] - moved[0][2] == pytest.approx(1.27)


def test_the_worker_writes_the_file_with_the_anchor_at_the_origin(tmp_path, qapp):
    from kicadgen.generators.pin_header import PinHeaderParams
    from kicadgen.gui.main_window import read_footprint_pad_records
    from kicadgen.gui.workers import GenerateWorker
    params = PinHeaderParams(name="H", rows=1, cols=2, pitch=2.54, row_spacing=2.54, drill=1.0, pad_diameter=1.7)
    pads = {}
    for anchor in ((0.0, 0.0), (2.54, 1.0)):
        folder = tmp_path / f"a{anchor[0]}"
        worker = GenerateWorker(params, folder)
        worker.anchor = anchor
        worker.drawings = validate_footprint_drawings([record(points=((0, 0), (2.54, 0)))])
        worker.run()
        path = folder / "H.pretty" / "H.kicad_mod"
        pads[anchor] = {r[0]: (r[2], r[3]) for r in read_footprint_pad_records(path)}
        if anchor != (0.0, 0.0):
            text = path.read_text(encoding="utf-8")
            assert "(start -2.54 -1)" in text and "(end 0 -1)" in text, "the drawings move with the pads"
    for number, (x, y) in pads[(0.0, 0.0)].items():
        assert pads[(2.54, 1.0)][number] == pytest.approx((x - 2.54, y - 1.0))


def test_the_worker_without_an_anchor_writes_what_it_wrote_before(tmp_path, qapp):
    """Contract 2: anchor (0, 0) is byte-identical except the uuids."""
    import re
    from kicadgen.generators.pin_header import PinHeaderParams
    from kicadgen.gui.workers import GenerateWorker
    params = PinHeaderParams(name="H", rows=1, cols=2, pitch=2.54, row_spacing=2.54, drill=1.0, pad_diameter=1.7)
    texts = []
    for explicit in (False, True):
        folder = tmp_path / str(explicit)
        worker = GenerateWorker(params, folder)
        if explicit:
            worker.anchor = (0.0, 0.0)
        worker.run()
        text = (folder / "H.pretty" / "H.kicad_mod").read_text(encoding="utf-8")
        texts.append(re.sub(r'\(uuid "[^"]*"\)', "", text))
    assert texts[0] == texts[1]


def test_the_anchor_tool_takes_one_snapped_click_and_escape_cancels(preview):
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    preview.chk_grid.setChecked(True)
    preview._arm_footprint_anchor_tool()
    assert preview.btn_fp_anchor.isChecked()
    # The view's own key handler (the window is not shown, QTest would not deliver it).
    preview.preview_view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                                 Qt.KeyboardModifier.NoModifier))
    assert preview.footprint_anchor == (0.0, 0.0) and not preview.btn_fp_anchor.isChecked(), \
        "negative: Escape sets nothing"
    preview._arm_footprint_anchor_tool()
    preview._on_footprint_draw_clicked(1.04 * MM, 0.98 * MM)
    x, y = preview.footprint_anchor
    assert (round(x, 4), round(y, 4)) == (round(x, 1), round(y, 1)), "snapped to the drawing grid"
    assert math.hypot(x - 1.0, y - 1.0) < 0.15
    assert preview._fp_draw_tool is None and not preview.btn_fp_anchor.isChecked(), "one click, then disarmed"


def test_the_footprint_origin_follows_the_anchor_and_the_marker_shows_it(preview):
    from kicadgen.gui.main_window import FP_ORIGIN_DEFAULT
    view = preview.preview_view
    assert not view.anchor_marker_painted(), "negative: an unmoved anchor paints nothing new"
    assert preview.set_footprint_anchor(2.0, 1.0)
    assert preview._fp_origin_points[FP_ORIGIN_DEFAULT] == (2.0, 1.0)
    assert view.drawing_origin() == QPointF(2 * MM, 1 * MM), "the default drawing origin is the anchor"
    assert view.anchor_marker() == QPointF(2 * MM, 1 * MM)
    assert not view.anchor_marker_painted(), "the drawing-origin marker already marks it"
    other = next(key for key in preview._fp_origin_points if key != FP_ORIGIN_DEFAULT)
    preview._set_footprint_drawing_origin(other)
    assert view.anchor_marker_painted(), "with another drawing origin the anchor has its own marker"


def test_a_recovered_draft_keeps_the_anchor_or_the_applied_one(preview):
    # An APPLIED anchor away from the origin, so «the applied one» and «(0, 0)» differ.
    assert preview.set_footprint_anchor(2.0, -1.0)
    preview.apply_footprint_draft()
    assert preview.set_footprint_anchor(1.0, 1.0)
    state = preview._footprint_state_json(preview._footprint_edit_state())
    assert state["anchor"] == [1.0, 1.0]
    assert preview._footprint_state_from_json(state)["anchor"] == (1.0, 1.0)
    old = {key: value for key, value in state.items() if key != "anchor"}
    assert preview._footprint_state_from_json(old)["anchor"] == (2.0, -1.0), \
        "a draft without the key keeps the applied anchor"
    # CONTRACT CHANGED 2026-11-09 (review L2): an invalid anchor keeps the
    # applied anchor too, as a missing one; it read as (0, 0) before.
    for invalid in ([True, 1], [1, float("nan")], [10 ** 400, 0], "1,2", None):
        assert preview._footprint_state_from_json(dict(state, anchor=invalid))["anchor"] == (2.0, -1.0), invalid


def test_a_huge_json_integer_anchor_is_refused_not_raised():
    """Review 2026-11-09: math.isfinite(10**400) overflows; the bound is checked first."""
    from kicadgen.gui.main_window import MainWindow
    assert MainWindow._valid_footprint_anchor([10 ** 400, 0]) == (0.0, 0.0)
    assert MainWindow._valid_footprint_anchor([0, -(10 ** 400)], default=None) is None
    assert MainWindow._valid_footprint_anchor([3, -4]) == (3.0, -4.0), "negative: plain ints still load"


def test_a_project_with_a_huge_anchor_loads_with_the_origin(preview):
    """Project load: the anchor is refused, the rest of the project is applied (no OverflowError)."""
    state = preview._project_state()
    state["footprint_anchor"] = [10 ** 400, 1]
    state["footprint_drawings"] = [record()]
    preview._apply_project_state(state)
    assert preview.footprint_anchor == (0.0, 0.0)
    assert [d["kind"] for d in preview.footprint_drawings] == ["line"], "the rest of the project is applied"
    state["footprint_anchor"] = [1.5, 2]
    preview._apply_project_state(state)
    assert preview.footprint_anchor == (1.5, 2.0), "negative: a valid anchor loads"


def test_recovering_a_draft_with_a_huge_anchor_keeps_the_applied_anchor(preview):
    """«Recupera bozza» with a 10**400 anchor: no exception out of the slot, the applied anchor stays."""
    assert preview.set_footprint_anchor(0.5, 0.5)
    preview.apply_footprint_draft()
    draft = preview._footprint_state_json(preview._footprint_edit_state())
    draft["anchor"] = [10 ** 400, 0]
    draft["drawings"] = [record()]
    preview._fp_recoverable, preview._fp_recoverable_source = draft, "main"
    preview.recover_footprint_draft()
    assert preview.footprint_anchor == (0.5, 0.5)
    assert [d["kind"] for d in preview.footprint_drawings] == ["line"], "the draft's drawings are recovered"


@pytest.mark.parametrize("raw, expected", [
    (None, (0.0, 0.0)), ([1, 2], (1.0, 2.0)), ([1, float("nan")], (0.0, 0.0)), ([1, 2, 3], (0.0, 0.0)),
    ([True, 0], (0.0, 0.0)), ([501, 0], (0.0, 0.0)), ("1,2", (0.0, 0.0)),
])
def test_a_saved_anchor_is_validated(raw, expected):
    from kicadgen.gui.main_window import MainWindow
    assert MainWindow._valid_footprint_anchor(raw) == expected


# ── F4: keepout zones ──────────────────────────────────────────────────

def _draw_zone(window, monkeypatch, answer, points=((0, 0), (4, 0), (4, 3))):
    monkeypatch.setattr(window, "_ask_zone_settings", lambda *a, **k: answer)
    window._toggle_footprint_draw_tool("zone")
    for x, y in points:
        window._on_footprint_draw_clicked(x * MM, y * MM)
    window._on_footprint_draw_clicked(points[0][0] * MM, points[0][1] * MM)


def test_the_zone_tool_closes_like_a_polygon_and_asks_side_and_flags(preview, monkeypatch):
    flags = dict(ZONE_KEEPOUT_DEFAULTS, footprints=True)
    _draw_zone(preview, monkeypatch, (["F.Cu", "B.Cu"], flags))
    stored = preview.footprint_drawings[-1]
    assert stored["kind"] == "zone" and stored["layers"] == ["F.Cu", "B.Cu"] and stored["keepout"] == flags
    assert "layer" not in stored and "width" not in stored
    assert len(stored["points"]) == 3, "the closing click is not stored"
    assert preview._fp_selected_indices() == [len(preview.footprint_drawings) - 1]


def test_a_cancelled_zone_dialog_stores_nothing(preview, monkeypatch):
    _draw_zone(preview, monkeypatch, None)
    assert preview.footprint_drawings == []


def test_a_zone_of_two_points_is_refused(preview, monkeypatch):
    monkeypatch.setattr(preview, "_ask_zone_settings", lambda *a, **k: pytest.fail("no dialog for 2 points"))
    preview._toggle_footprint_draw_tool("zone")
    preview._on_footprint_draw_clicked(0, 0)
    preview._on_footprint_draw_clicked(4 * MM, 0)
    preview._on_footprint_draw_finish()
    assert preview.footprint_drawings == []


def test_a_two_layer_zone_is_drawn_on_both_sides_in_copper_colours(preview):
    from kicadgen.gui.footprint_view import ZONE_COPPER_ROLE
    _show(preview, [zone(layers=("F.Cu", "B.Cu")), zone(layers=("B.Cu",))])
    both = {item.data(ZONE_COPPER_ROLE): item for item in _items(preview, 0)}
    assert set(both) == {"F.Cu", "B.Cu"}
    assert both["F.Cu"].pen().color().name() == COPPER_COLOURS["F.Cu"]
    assert both["B.Cu"].pen().color().name() == COPPER_COLOURS["B.Cu"]
    assert both["F.Cu"].zValue() > 1 > both["B.Cu"].zValue(), "front hatch over the pads, back under"
    back_only = _items(preview, 1)
    assert [item.data(ZONE_COPPER_ROLE) for item in back_only] == ["B.Cu"], "negative: one side, one item"


def test_the_zones_row_hides_zones_and_not_drawings(preview):
    _show(preview, [zone(), record()])
    box = preview.fp_layer_boxes[preview.FP_ZONES_VIEW_KEY]
    box.setChecked(False)
    try:
        assert all(not item.isVisible() for item in _items(preview, 0))
        assert all(item.isVisible() for item in _items(preview, 1)), "negative: the silkscreen stays"
        from PyQt6.QtCore import QRectF
        preview._select_footprint_drawings_in(QRectF(-1000, -1000, 2000, 2000), False)
        assert preview._fp_selected_indices() == [1], "a hidden zone is not band-selected"
    finally:
        box.setChecked(True)


def test_a_selected_zone_keeps_its_hatch_in_the_selection_colour(preview):
    _show(preview, [zone()])
    preview._select_footprint_drawing(0)
    item = _items(preview, 0)[0]
    assert item.pen().color().name() == preview._fp_selected_colour()
    preview._clear_footprint_selection()
    assert item.pen().color().name() == COPPER_COLOURS["F.Cu"]


def test_zone_properties_edit_side_and_flags_in_one_step(preview):
    _show(preview, [zone()])
    values = preview._drawing_properties_values(preview.footprint_drawings[0])
    assert values["zone_layers"] == "F.Cu" and "layer" not in values and "width" not in values
    keys = [spec.key for spec in preview._drawing_property_specs(preview.footprint_drawings[0])]
    assert keys[0] == "zone_layers" and "width" not in keys
    assert [k for k in keys if k.startswith("keepout_")] == [
        "keepout_tracks", "keepout_vias", "keepout_pads", "keepout_copperpour", "keepout_footprints"]
    undo = len(preview._fp_drawings_undo)
    assert preview.set_drawing_properties(0, {"zone_layers": "B.Cu", "keepout_pads": False})
    stored = preview.footprint_drawings[0]
    assert stored["layers"] == ["B.Cu"] and stored["keepout"]["pads"] is False
    assert len(preview._fp_drawings_undo) == undo + 1
    assert preview.set_drawing_properties(0, {"zone_layers": "B.Cu"}), "unchanged values are accepted"
    assert len(preview._fp_drawings_undo) == undo + 1, "negative: no undo step for a no-op"


def test_the_panel_of_several_zones_offers_side_and_flags_only(preview):
    _show(preview, [zone(), zone(layers=("B.Cu",))])
    preview._fp_selection.set_many([0, 1])
    _subject, specs, _title = preview._fp_properties_fields()
    side = next(spec for spec in specs if spec.key == "zone_layers")
    assert side.mixed and {"layer", "width", "fill"}.isdisjoint(spec.key for spec in specs)
    _show(preview, [zone(), record()])
    preview._fp_selection.set_many([0, 1])
    _subject, specs, _title = preview._fp_properties_fields()
    assert not specs, "negative: a zone and a line share no field"


def test_the_zone_menu_offers_copper_sides_and_no_stroke(preview, monkeypatch):
    from PyQt6.QtWidgets import QMenu
    _show(preview, [zone()])
    seen = []

    def fake_exec(menu, *_args):
        seen.append([a.text() for a in menu.actions()])
        layer = next(a for a in menu.actions() if a.menu() is not None)
        seen.append([a.text() for a in layer.menu().actions()])
        return next(a for a in layer.menu().actions() if a.text() == "Rame inferiore (B.Cu)")

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    global_pos = preview.preview_view.viewport().mapToGlobal(
        preview.preview_view.mapFromScene(QPointF(2 * MM, 1.5 * MM)))
    preview._footprint_drawing_context_menu(0, global_pos)
    assert "Spessore del tratto…" not in seen[0] and "Aggiungi vertice" in seen[0]
    assert seen[1] == ["Rame superiore (F.Cu)", "Rame inferiore (B.Cu)", "Entrambi i lati (F.Cu e B.Cu)"]
    assert preview.footprint_drawings[0]["layers"] == ["B.Cu"]


def test_a_zone_turns_mirrors_copies_and_deletes_like_a_drawing(preview):
    _show(preview, [zone(points=((0, 0), (4, 0), (0, 2)))])
    assert preview._turn_drawing(0, True)
    assert preview.footprint_drawings[0]["kind"] == "zone"
    assert preview._mirror_drawing(0)
    assert preview._duplicate_drawing(0) and len(preview.footprint_drawings) == 2
    assert preview.footprint_drawings[1]["layers"] == preview.footprint_drawings[0]["layers"]
    assert preview._delete_drawings([0, 1]) and preview.footprint_drawings == []


def test_a_zone_is_written_by_the_worker(tmp_path, qapp):
    from kicadgen.generators.pin_header import PinHeaderParams
    from kicadgen.gui.workers import GenerateWorker
    params = PinHeaderParams(name="H", rows=1, cols=2, pitch=2.54, row_spacing=2.54, drill=1.0, pad_diameter=1.7)
    worker = GenerateWorker(params, tmp_path)
    worker.drawings = validate_footprint_drawings([zone(layers=("F.Cu", "B.Cu"))])
    worker.anchor = (1.0, 0.0)
    worker.run()
    text = (tmp_path / "H.pretty" / "H.kicad_mod").read_text(encoding="utf-8")
    assert '(layers "F.Cu" "B.Cu")' in text and "(keepout" in text
    assert "(xy -1 0)" in text and "(xy 3 0)" in text, "the zone moves with the anchor"


def test_a_moved_anchor_survives_a_crash_through_the_recovery_file(preview, tmp_path):
    """Recovery of a MOVED anchor: the reader must keep the key (positive); the GUI's own completion
    (`_with_draft_anchor`) only covers drafts without it."""
    from kicadgen.services.footprint_draft import load_footprint_draft, save_footprint_draft
    baseline = preview._footprint_state_json(preview._applied_footprint_state())
    assert preview.set_footprint_anchor(2.0, 3.0)
    draft = preview._footprint_state_json(preview._footprint_edit_state())
    path = tmp_path / "draft.json"
    save_footprint_draft(path, baseline, draft)
    assert load_footprint_draft(path, baseline)["anchor"] == [2.0, 3.0]


# ── Review 2026-11-09: a zone is hit on its outline only (as KiCad) ─────

def _zone_over_pads(window, qapp, monkeypatch, layers):
    """Four pads at (±3, ±2) mm on a shown preview, a keepout zone (-5, -4)-(5, 4) mm over them."""
    from test_footprint_pad_drag import PINS, _show
    from kicadgen.core.package_geometry import PackageGeometry
    _show(window, qapp, monkeypatch, PackageGeometry(family="soic", body_length_mm=15.0, body_width_mm=10.0,
                                                     pins=list(PINS)))
    window.footprint_drawings = validate_footprint_drawings(
        [zone(points=((-5, -4), (5, -4), (5, 4), (-5, 4)), layers=layers)])
    window._draw_preview()
    qapp.processEvents()
    return window


def _right_press(window, x_mm, y_mm, monkeypatch):
    """A real right press (and release) at (x, y) mm; returns the signals the view emitted."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QMenu
    monkeypatch.setattr(QMenu, "exec", lambda *a, **k: None)
    view = window.preview_view
    heard = []
    slots = [(view.drawing_context_requested, lambda index, _pos: heard.append(("drawing", index))),
             (view.pad_context_requested, lambda key: heard.append(("pad", key))),
             (view.background_context_requested, lambda *_a: heard.append(("background", None))),
             (view.body_context_requested, lambda *_a: heard.append(("body", None))),
             (view.dimension_context_requested, lambda *_a: heard.append(("dimension", None)))]
    for signal, slot in slots:
        signal.connect(slot)
    # The window's pad slot opens the modal pad dialog: only the view's choice is under test.
    view.pad_context_requested.disconnect(window._edit_preview_pad)
    try:
        pos = view.mapFromScene(QPointF(x_mm * MM, y_mm * MM))
        QTest.mouseMove(view.viewport(), pos)
        QTest.mousePress(view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
    finally:
        for signal, slot in slots:
            signal.disconnect(slot)
        view.pad_context_requested.connect(window._edit_preview_pad)
    return heard


@pytest.mark.parametrize("layers", [("F.Cu",), ("B.Cu",)])
def test_a_right_click_on_the_zone_outline_opens_the_zone_menu(window, qapp, monkeypatch, layers):
    """Positive: the border of the zone (front hatch over the pads, or back under them) is the zone."""
    _zone_over_pads(window, qapp, monkeypatch, layers)
    assert _right_press(window, 0.0, -4.0, monkeypatch)[:1] == [("drawing", 0)]


@pytest.mark.parametrize("layers", [("F.Cu",), ("B.Cu",)])
def test_a_right_click_on_a_pad_inside_a_zone_reaches_the_pad(window, qapp, monkeypatch, layers):
    """Negative twin: the zone's AREA is not its hit; the pad under the hatch keeps its menu."""
    _zone_over_pads(window, qapp, monkeypatch, layers)
    heard = _right_press(window, -3.0, -2.0, monkeypatch)
    assert heard and heard[0][0] == "pad", heard


def test_a_right_click_inside_a_zone_off_everything_opens_the_background_menu(window, qapp, monkeypatch):
    _zone_over_pads(window, qapp, monkeypatch, ("F.Cu",))
    assert _right_press(window, 0.0, 0.5, monkeypatch) == [("background", None)]


def test_a_drag_inside_a_zone_pans_and_moves_no_zone(window, qapp, monkeypatch):
    """A left drag inside the area is the background's pan; on the outline it moves the zone."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    _zone_over_pads(window, qapp, monkeypatch, ("F.Cu",))
    view = window.preview_view
    stored = deepcopy(window.footprint_drawings)

    def drag(start_mm, end_mm):
        start = view.mapFromScene(QPointF(start_mm[0] * MM, start_mm[1] * MM))
        end = view.mapFromScene(QPointF(end_mm[0] * MM, end_mm[1] * MM))
        QTest.mouseMove(view.viewport(), start)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        QTest.mouseMove(view.viewport(), (start + end) / 2)
        QTest.mouseMove(view.viewport(), end)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)

    drag((0.0, 0.5), (1.0, 1.5))
    assert window.footprint_drawings == stored, "inside: nothing moved"
    drag((0.0, -4.0), (1.0, -3.0))
    assert window.footprint_drawings != stored, "positive: the outline drags the zone"
    assert window.footprint_drawings[0]["kind"] == "zone"


def test_the_zone_hit_shape_is_its_outline(qapp):
    """Unit: the hatched item's Qt shape holds the border, not the interior."""
    from PyQt6.QtGui import QPen, QPolygonF
    from kicadgen.gui.footprint_view import ZoneOutlineItem
    item = ZoneOutlineItem(QPolygonF([QPointF(0, 0), QPointF(100, 0), QPointF(100, 80), QPointF(0, 80)]))
    item.setPen(QPen())
    assert item.shape().contains(QPointF(50, 0)) and item.shape().contains(QPointF(0, 40)), "every side"
    assert not item.shape().contains(QPointF(50, 40)), "negative: the interior is not hit"


# ── Review 2026-11-09: arming the zone tool shows the zones ────────────

def test_arming_the_zone_tool_ticks_the_hidden_zones_row(preview, monkeypatch):
    box = preview.fp_layer_boxes[preview.FP_ZONES_VIEW_KEY]
    box.setChecked(False)
    try:
        preview._toggle_footprint_draw_tool("line")
        assert not box.isChecked(), "negative: another tool leaves the zones row as it is"
        preview._cancel_footprint_drawing()
        _draw_zone(preview, monkeypatch, (["F.Cu"], dict(ZONE_KEEPOUT_DEFAULTS)))
        assert box.isChecked(), "the zone tool shows the zones before the first click"
        assert all(item.isVisible() for item in _items(preview, len(preview.footprint_drawings) - 1)), \
            "the new, selected zone is visible"
    finally:
        box.setChecked(True)


# ── Review 2026-11-09, L1 and L3: offsets read with the gate ──────────

def test_the_install_reads_the_model_offset_after_the_questions(window, monkeypatch, tmp_path):
    """L1: the offset given to the install is the one valid when the gate is re-checked, not before the questions.

    Positive: an offset that changes while the question is open reaches the
    install. Negative twin: with nothing changed the install gets the offset
    the artifacts were discovered with.
    """
    from types import SimpleNamespace
    from PyQt6.QtWidgets import QMessageBox
    from kicadgen.gui import main_window as gui
    from tests.test_kicad_gate_remedies import NAME, _ready
    _ready(window, monkeypatch, tmp_path, with_body=False)
    for name in ("warning", "information"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    installed = []
    monkeypatch.setattr(gui, "install_component_library", lambda artifacts, *a, **k: installed.append(
        artifacts.model_offset) or SimpleNamespace(identifier="KiCadGen:" + NAME, footprint_path="fp",
                                                   table_path="t"))
    monkeypatch.setattr(gui, "record_kicad_export", lambda **k: True)
    artifacts = gui.discover_component_artifacts(window._project_dir(), window._component_name(), require_3d=True)
    offset = {"value": (0.0, 0.0, 0.0)}
    monkeypatch.setattr(window, "_kicad_model_offset", lambda: offset["value"])

    def question(*args, **kwargs):
        offset["value"] = (-1.0, 2.0, 0.0)
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", question)
    # A global install always asks: the question runs between discovery and copy.
    window._install_kicad_library(tmp_path / "dest", project_library=False,
                                  table_path=tmp_path / "fp-lib-table", table_version="10.0", artifacts=artifacts)
    assert installed == [(-1.0, 2.0, 0.0)]
    offset["value"] = (0.0, 0.0, 0.0)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window._install_kicad_library(tmp_path / "dest2", project_library=False,
                                  table_path=tmp_path / "fp-lib-table", table_version="10.0", artifacts=artifacts)
    assert installed[-1] == (0.0, 0.0, 0.0)


def test_the_component_check_is_refused_while_the_footprint_file_is_stale(window, monkeypatch):
    """L3: the 3D test places the model with the APPLIED anchor; a stale .kicad_mod refuses the check.

    Positive: a pending footprint write -> warning with the gate's reason, no
    dialog. Negative twin: an aligned file opens the dialog with the applied offset.
    """
    from PyQt6.QtWidgets import QMessageBox
    from kicadgen.gui import component_check_dialog
    opened, warned = [], []

    class Dialog:
        def __init__(self, args, parent, tx):
            opened.append(args)

        def exec(self):
            return 0

        def deleteLater(self):
            pass
    monkeypatch.setattr(component_check_dialog, "ComponentCheckDialog", Dialog)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    monkeypatch.setattr(window, "_footprint_alignment_block", lambda: None)
    window._fp_rewrite_dirty = True
    try:
        window._verify_component()
    finally:
        window._fp_rewrite_dirty = False
    assert opened == [] and warned and "Applica ai file" in warned[-1]
    window._verify_component()
    assert len(opened) == 1 and opened[0]["model_offset"] == window._kicad_model_offset()
