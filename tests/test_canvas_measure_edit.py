"""@brief Editing a measured value and measures that follow their targets (user, 2026-11-02).

«editare la distanza indicata nel righello per adattare lo spaziamento tra i
due oggetti in misura; se uno è un bordo deve modificare solo posizione pad»
(edit the distance shown by the ruler to adjust the spacing; with an edge, move only the pad).
Pure geometry first (`edit_displacement`, `reanchor_measurement`), then the
footprint preview: the typed value moves ONE pad, the measure stays and shows it.
"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPointF, Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QDialog, QDoubleSpinBox  # noqa: E402

from kicadgen.gui.canvas_measure import (  # noqa: E402
    KIND_CENTRE, KIND_CORNER, KIND_EDGE, KIND_FREE, MeasureAnchor, SnapTargets, edit_displacement,
    measure, measure_linear, measure_mover, reanchor_measurement,
)

K = 20.0     # scene units per mm (the footprint's)


def centre(x, y, label):
    return MeasureAnchor(x * K, y * K, KIND_CENTRE, label)


def edge(x1, y1, x2, y2, x, y):
    return MeasureAnchor(x * K, y * K, KIND_EDGE, edge=(x1 * K, y1 * K, x2 * K, y2 * K))


# ── edit_displacement ───────────────────────────────────────────────────

def test_a_true_distance_moves_the_second_pad_along_the_line():
    m = measure(centre(0, 0, "1"), centre(3, 4, "2"), K)             # 5 mm
    end, dx, dy = edit_displacement(m, 10.0, K)
    assert end == "second"
    assert (dx / K, dy / K) == pytest.approx((3.0, 4.0)), "direction kept, 5 -> 10 mm"
    # Negative: a smaller value brings it closer, along the same line.
    end, dx, dy = edit_displacement(m, 2.5, K)
    assert (dx / K, dy / K) == pytest.approx((-1.5, -2.0))


def test_a_pad_to_edge_measure_moves_only_the_pad_perpendicular_to_the_edge():
    # Edge y = -5 (body top), pad at (2, -2): 3 mm. The edge is clicked second.
    m = measure(centre(2, -2, "1"), edge(-7.5, -5, 7.5, -5, 4, -5), K)
    assert m.perpendicular and m.distance_mm == pytest.approx(3.0)
    end, dx, dy = edit_displacement(m, 1.0, K)
    assert end == "first", "the edge never moves: the pad does"
    assert (dx / K, dy / K) == pytest.approx((0.0, -2.0)), "perpendicular to the edge only"
    # Same with the edge clicked FIRST.
    m = measure(edge(-7.5, -5, 7.5, -5, -4, -5), centre(2, -2, "1"), K)
    end, dx, dy = edit_displacement(m, 1.0, K)
    assert end == "second" and (dx / K, dy / K) == pytest.approx((0.0, -2.0))


def test_a_linear_dimension_changes_only_its_axis():
    second = MeasureAnchor(12.2 * K, 1.0 * K, KIND_CENTRE, "11", axis="x")
    m = measure_linear(centre(0, 0, "12"), second, K)                # ΔX 12.2 (ΔY 1 ignored)
    end, dx, dy = edit_displacement(m, 12.0, K)
    assert end == "second" and (dx / K, dy / K) == pytest.approx((-0.2, 0.0))
    # Negative: a true distance between the same pads would also move Y.
    m = measure(centre(0, 0, "12"), MeasureAnchor(12.2 * K, 1.0 * K, KIND_CENTRE, "11"), K)
    _end, dx, dy = edit_displacement(m, 12.0, K)
    assert dy != pytest.approx(0.0)


def test_a_measure_without_a_pad_is_not_editable():
    m = measure(MeasureAnchor(0, 0, KIND_CORNER), MeasureAnchor(10 * K, 0, KIND_CORNER), K)
    assert measure_mover(m) == "" and edit_displacement(m, 5.0, K) is None
    m = measure(centre(0, 0, "1"), centre(0, 0, "2"), K)
    assert edit_displacement(m, 5.0, K) is None, "coincident ends: no direction"
    m = measure(centre(0, 0, "1"), centre(1, 0, "2"), K)
    assert edit_displacement(m, -1.0, K) is None and edit_displacement(m, float("nan"), K) is None


# ── reanchor_measurement ────────────────────────────────────────────────

def _targets(pads, corners=(), edges=()):
    return SnapTargets(centres=tuple((x * K, y * K, label) for label, x, y in pads),
                       corners=tuple((x * K, y * K) for x, y in corners),
                       edges=tuple(tuple(v * K for v in e) for e in edges))


def test_a_measure_follows_its_moved_pad_and_shows_the_new_value():
    old = _targets([("1", 0, 0), ("2", 5, 0)])
    new = _targets([("1", 0, 0), ("2", 8, 0)])
    m = measure(centre(0, 0, "1"), centre(5, 0, "2"), K)
    moved = reanchor_measurement(m, old, new, K)
    assert moved.distance_mm == pytest.approx(8.0)
    # Negative: a free end belongs to no target and the measure is dropped.
    m = measure(centre(0, 0, "1"), MeasureAnchor(5 * K, 0, KIND_FREE), K)
    assert reanchor_measurement(m, old, new, K) is None
    # Negative: other pads (another count) drop it.
    assert reanchor_measurement(measure(centre(0, 0, "1"), centre(5, 0, "2"), K), old,
                                _targets([("1", 0, 0)]), K) is None


def test_a_pad_to_edge_measure_follows_a_re_centred_frame():
    # The whole frame shifted by (1, 0.5) mm (a body centre re-estimated).
    old = _targets([("1", 2, -2)], edges=[(-7.5, -5, 7.5, -5)])
    new = _targets([("1", 3, -3.5)], edges=[(-6.5, -4.5, 8.5, -4.5)])
    m = measure(centre(2, -2, "1"), edge(-7.5, -5, 7.5, -5, 4, -5), K)
    moved = reanchor_measurement(m, old, new, K)
    assert moved.perpendicular and moved.distance_mm == pytest.approx(1.0)


def test_a_linear_dimension_keeps_its_line_offset():
    old = _targets([("1", 0, 0), ("2", 5, 0)])
    new = _targets([("1", 0, 1), ("2", 7, 1)])
    second = MeasureAnchor(5 * K, 0, KIND_CENTRE, "2", axis="x")
    m = measure_linear(centre(0, 0, "1"), second, K, MeasureAnchor(0, -3 * K, "place"))
    moved = reanchor_measurement(m, old, new, K)
    assert moved.axis == "x" and moved.distance_mm == pytest.approx(7.0)
    assert moved.start[1] / K == pytest.approx(-2.0), "the line stays 3 mm above the first pad"


# ── Footprint preview ───────────────────────────────────────────────────

from tests.test_footprint_pad_drag import PX, _show, top  # noqa: E402
from kicadgen.core.package_geometry import PackageGeometry, PinSpec  # noqa: E402

PINS = [PinSpec("1", -3.0, -2.0, 1.2, .6), PinSpec("2", 3.0, -2.0, 1.2, .6),
        PinSpec("3", 3.0, 2.0, 1.2, .6), PinSpec("4", -3.0, 2.0, 1.2, .6)]


@pytest.fixture
def fp(window, qapp, monkeypatch):
    """Four SMD pads on a 15 x 10 mm body (F.Fab outline: edges at x ±7.5, y ±5)."""
    return _show(window, qapp, monkeypatch,
                 PackageGeometry(family="soic", body_length_mm=15, body_width_mm=10, pins=list(PINS)))


def _answer(monkeypatch, value, seen=None):
    """Accept «Modifica quota» with `value` typed."""
    def exec_(dialog):
        spin = dialog.findChild(QDoubleSpinBox)
        if seen is not None:
            seen.append(spin.value())
        spin.setValue(value)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", exec_)


def _target(window, label):
    return next(c for c in window._fp_measure.targets.centres if c[2] == label)


def _edge(window, y_mm):
    return next(e for e in window._fp_measure.targets.edges
                if e[1] == pytest.approx(y_mm * PX) and e[3] == pytest.approx(y_mm * PX))


def _add(window, first, second):
    window._fp_measure.overlay.add(window.preview_scene, measure(first, second, PX))


def test_editing_a_pad_to_pad_distance_moves_the_second_pad_and_the_measure_stays(fp, monkeypatch):
    window = fp
    one, two = _target(window, "1"), _target(window, "2")
    _add(window, MeasureAnchor(one[0], one[1], KIND_CENTRE, "1", shape=one[3]),
         MeasureAnchor(two[0], two[1], KIND_CENTRE, "2", shape=two[3]))
    seen = []
    _answer(monkeypatch, 8.0, seen)
    undo = len(window._fp_drawings_undo)
    assert window._fp_measure.edit(0)
    assert seen == [pytest.approx(6.0)], "the dialog shows the measured value"
    after = top(window)
    assert after["2"] == (5.0, -2.0) and after["1"] == (-3.0, -2.0), "only the second pad moved"
    assert len(window._fp_drawings_undo) == undo + 1
    assert [m.distance_mm for m in window._fp_measure.overlay.measurements] == [pytest.approx(8.0)]


def test_editing_a_pad_to_edge_distance_moves_only_the_pad(fp, monkeypatch):
    window = fp
    one = _target(window, "1")
    top_edge = _edge(window, -5.0)
    _add(window, MeasureAnchor(one[0], one[1], KIND_CENTRE, "1", shape=one[3]),
         MeasureAnchor(one[0], top_edge[1], KIND_EDGE, edge=top_edge))
    _answer(monkeypatch, 1.0)
    assert window._fp_measure.edit(0)
    assert top(window)["1"] == (-3.0, -4.0), "the pad moved toward the edge, X unchanged"
    assert _edge(window, -5.0) == top_edge, "the body edge did not move"
    assert [m.distance_mm for m in window._fp_measure.overlay.measurements] == [pytest.approx(1.0)]


def test_a_body_width_is_not_editable_and_moves_nothing(fp, monkeypatch):
    window = fp
    top_edge, bottom_edge = _edge(window, -5.0), _edge(window, 5.0)
    _add(window, MeasureAnchor(0, top_edge[1], KIND_EDGE, edge=top_edge),
         MeasureAnchor(0, bottom_edge[1], KIND_EDGE, edge=bottom_edge))
    asked = []
    _answer(monkeypatch, 3.0, asked)
    before = top(window)
    assert not window._fp_measure.edit(0)
    assert asked == [] and top(window) == before


def test_cancelling_the_dialog_changes_nothing(fp, monkeypatch):
    window = fp
    one, two = _target(window, "1"), _target(window, "2")
    _add(window, MeasureAnchor(one[0], one[1], KIND_CENTRE, "1"), MeasureAnchor(two[0], two[1], KIND_CENTRE, "2"))
    monkeypatch.setattr(QDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)
    before = top(window)
    assert not window._fp_measure.edit(0)
    assert top(window) == before and not window.has_pending_footprint_edits()


def test_a_double_click_on_the_measure_opens_the_edit(fp, monkeypatch, qapp):
    window = fp
    one, two = _target(window, "1"), _target(window, "2")
    _add(window, MeasureAnchor(one[0], one[1], KIND_CENTRE, "1"), MeasureAnchor(two[0], two[1], KIND_CENTRE, "2"))
    _answer(monkeypatch, 7.0)
    view = window.preview_view
    on_line = view.mapFromScene(QPointF(0.0, -2.0 * PX))           # middle of the measure line
    QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, on_line)
    qapp.processEvents()
    assert top(window)["2"] == (4.0, -2.0)


def test_a_measure_follows_a_dragged_pad(fp):
    from tests.test_footprint_pad_drag import drag, vp
    window = fp
    one, two = _target(window, "1"), _target(window, "2")
    _add(window, MeasureAnchor(one[0], one[1], KIND_CENTRE, "1"), MeasureAnchor(two[0], two[1], KIND_CENTRE, "2"))
    drag(window, vp(window, 3, -2), vp(window, 4, -2))
    assert [m.distance_mm for m in window._fp_measure.overlay.measurements] == [pytest.approx(7.0, abs=0.011)]
