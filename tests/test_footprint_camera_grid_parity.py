"""@brief Characterisation of the footprint preview camera and background grid (plan docs/canvas-camera-grid-plan.md, §4 step 1).

Written against the code BEFORE the shared `Camera` / `paint_grid` modules
are adopted, so the migration can prove "numbers and pixels unchanged except
D13-D16". Two kinds of evidence:

- NUMBERS: scale after `zoom` and the wheel, limit hits, pan deltas, the
  framing of `MainWindow._fit_preview`, the count of `fitInView` calls per
  redraw, and the `auto_fit` transitions. Cross-phase values live in
  `tests/data/camera_golden/footprint_camera.json`.
- PIXELS: the background grid painted alone (no items, hence no fonts) into
  an image, SHA-256 of the bytes, for both step units, the fine divisors
  1/2/5/10, several zooms (dots legible and not), the origin at 0 and away
  from 0, with the full viewport and with a sparse paint region. They live in
  `tests/data/camera_golden/footprint_grid.json`.

Both files are regenerated only when `KICADGEN_REGEN_CAMERA_GOLDEN` is set.

Phase 2 (the footprint adopted `Camera` and `paint_grid`, 2026-10-17) flipped
the tests that recorded behaviour the plan deliberately changes: D14 (wheel
clamped to 4 notches), D15 (memoised fit, refit on resize), D16 (fewer lines
laid out for a sparse region, same pixels) and, with the shared camera, a
zoom that cannot move the scale or a null pan step no longer touches the
picture. The grid pixel hashes did not change at all; the camera golden
changed only in the keys listed in the phase-2 report.
"""
import hashlib
import json
import math
import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt          # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter, QRegion, QWheelEvent  # noqa: E402
from PyQt6.QtTest import QTest                                       # noqa: E402
from PyQt6.QtWidgets import QApplication                             # noqa: E402

from kicadgen.gui.footprint_view import FINE_GRID_MIN_SCREEN_PX      # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent / "data" / "camera_golden"
REGEN_ENV = "KICADGEN_REGEN_CAMERA_GOLDEN"

#: Zoom limits of the footprint camera in px per scene unit (plan §3, D13: a per-tab parameter).
LIMITS = (0.02, 40.0)


# ── Golden helpers ─────────────────────────────────────────────────────

def _golden(name, data):
    """Compare `data` with `footprint_<name>.json` byte for byte (LF); rewrite it when the env var is set."""
    path = GOLDEN_DIR / f"footprint_{name}.json"
    text = (json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8")
    if os.environ.get(REGEN_ENV):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text)
        return
    assert path.exists(), f"golden {path.name} missing: run once with {REGEN_ENV}=1"
    assert path.read_bytes().replace(b"\r\n", b"\n") == text, f"{path.name} differs from today's run"


class _Gold:
    """Collects the numbers of a test module run and compares them once with the golden file."""

    def __init__(self):
        self.data = {}

    def put(self, key, value):
        assert key not in self.data, f"{key!r} recorded twice"
        self.data[key] = value


def _r(value):
    return round(float(value), 6)


# ── Fixture: a shown window with the preview in front, camera placed by hand ──

@pytest.fixture
def fp(window, qapp, monkeypatch):
    """The preview tab shown, 1 mm main grid, fine divisor 1, camera at scale 1 centred on the origin, auto_fit off."""
    from kicadgen.gui import main_window as gui
    monkeypatch.setattr(window, "_generate", lambda **_kwargs: True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_schedule_footprint_rewrite", lambda **_kwargs: None)
    assert window._save_project_config(force=True)
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()
    window.tabs.setCurrentIndex(gui.TAB_PREVIEW)
    qapp.processEvents()
    assert window.preview_view.isVisible()
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    window.combo_fine_grid.setCurrentIndex(window.combo_fine_grid.findData(1))
    window._draw_preview()
    _place(window)
    return window


def _place(window, scale=1.0):
    view = window.preview_view
    view.resetTransform()
    if scale != 1.0:
        view.scale(scale, scale)
    view.centerOn(0, 0)
    view.auto_fit = False


def _state(view):
    """Camera numbers: scale, scrollbars, scene point at the viewport centre, auto_fit."""
    centre = view.mapToScene(view.viewport().rect().center())
    return {"scale": _r(view.transform().m11()),
            "scroll": [view.horizontalScrollBar().value(), view.verticalScrollBar().value()],
            "centre": [_r(centre.x()), _r(centre.y())], "auto_fit": bool(view.auto_fit)}


def _wheel(window, pos, steps):
    """Send a wheel event of `steps` notches (120 units each) and return it."""
    view = window.preview_view
    event = QWheelEvent(QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)), QPoint(),
                        QPoint(0, int(120 * steps)), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    event.ignore()
    QApplication.sendEvent(view.viewport(), event)
    return event


def _press(window, pos, button=Qt.MouseButton.LeftButton):
    QTest.mousePress(window.preview_view.viewport(), button, Qt.KeyboardModifier.NoModifier, pos)


def _move(window, pos):
    QTest.mouseMove(window.preview_view.viewport(), pos)


def _release(window, pos, button=Qt.MouseButton.LeftButton):
    QTest.mouseRelease(window.preview_view.viewport(), button, Qt.KeyboardModifier.NoModifier, pos)


#: Viewport point over empty background at the placed camera (no pad, text or drawing there).
EMPTY = QPoint(880, 690)


def _content_rect(window):
    """Union of the scene items that are not grid lines, grown by the 12 px margin of `_fit_preview`."""
    from kicadgen.gui import main_window as gui
    rect = None
    for item in window.preview_scene.items():
        if item.data(gui._GRID_ITEM):
            continue
        bounds = item.sceneBoundingRect()
        rect = bounds if rect is None else rect.united(bounds)
    return rect.adjusted(-12, -12, 12, 12)


@pytest.fixture(scope="module")
def gold():
    holder = _Gold()
    yield holder
    _golden("camera", holder.data)


# ── NUMBERS: zoom ──────────────────────────────────────────────────────

ANCHORS = {"centre": None, "upper_left": QPoint(100, 80), "lower_right": QPoint(800, 600)}


def test_zoom_scale_follows_the_factor_and_keeps_the_anchor(fp, gold):
    """@brief Positive: the new scale is scale*factor and the scene point under the anchor stays within 2.5 screen px.

    @note The scene point is kept by `centerOn`, which rounds to whole scrollbar
        steps: 2.5 px is the tolerance, the exact values are in the golden. One window serves
        every case (the fixture costs seconds).
    """
    view = fp.preview_view
    for anchor_name in sorted(ANCHORS):
        for factor in (1.2, 0.5, 2.0, 1.2 ** -3, 7.3):
            _place(fp)
            anchor = ANCHORS[anchor_name]
            point = anchor if anchor is not None else view.viewport().rect().center()
            before = view.mapToScene(point)
            view.zoom(factor, anchor)
            assert view.transform().m11() == pytest.approx(min(max(factor, LIMITS[0]), LIMITS[1]), rel=1e-12)
            assert view.transform().m22() == pytest.approx(view.transform().m11())
            assert view.auto_fit is False
            after = view.mapToScene(point)
            assert math.hypot(after.x() - before.x(), after.y() - before.y()) * view.transform().m11() <= 2.5
            gold.put(f"zoom/{anchor_name}/{factor:.6g}", _state(view))


def test_zoom_is_clamped_to_the_limits_not_refused(fp, gold):
    """@brief D13 contract (already true here): a factor past a limit lands ON the limit; at the limit the scale stays.

    @note Phase 2 (shared `Camera.zoom`): a zoom that cannot change the scale
        no longer ends `auto_fit` nor re-centres the picture (before, it did
        both, so the limit golden moved by one pixel); it returns False.
    """
    view = fp.preview_view
    assert view.zoom(1000.0) is True
    assert view.transform().m11() == pytest.approx(LIMITS[1])
    view.auto_fit = True
    assert view.zoom(2.0) is False
    assert view.transform().m11() == pytest.approx(LIMITS[1]), "at the limit nothing more"
    assert view.auto_fit is True, "a zoom that cannot move the scale is not a user camera change"
    gold.put("limit/max", _state(view))
    view.zoom(1e-9)
    assert view.transform().m11() == pytest.approx(LIMITS[0])
    view.zoom(0.5)
    assert view.transform().m11() == pytest.approx(LIMITS[0])
    gold.put("limit/min", _state(view))
    # Negative: inside the range the factor is applied exactly, no snapping to a limit.
    _place(fp, 3.0)
    view.zoom(1.5)
    assert view.transform().m11() == pytest.approx(4.5)


def test_a_zoom_keeps_the_transform_a_pure_uniform_scale(fp):
    """@brief The camera never rotates or shears the view, and the scene stays y-down (m22 > 0)."""
    view = fp.preview_view
    for factor in (1.7, 0.3, 4.0):
        view.zoom(factor, QPoint(300, 200))
    t = view.transform()
    assert (t.m12(), t.m21()) == (0.0, 0.0) and t.m11() == pytest.approx(t.m22()) and t.m11() > 0


# ── NUMBERS: wheel ─────────────────────────────────────────────────────

def test_wheel_notches_scale_by_1_2_each_and_accept_the_event(fp, gold):
    """@brief Positive: |notches| <= 4 scale by 1.2**notches about the pointer; the event is always accepted.

    @note A zero-delta wheel is no zoom: since the shared camera it leaves
        `auto_fit` and the picture alone (before, `zoom(1.0)` ended auto_fit
        and shifted the picture by one pixel).
    """
    view = fp.preview_view
    point = QPoint(300, 220)
    for steps in (1, -1, 2, 3, -3, 4, -4, 0):
        _place(fp)
        view.auto_fit = True
        before = view.mapToScene(point)
        event = _wheel(fp, point, steps)
        assert event.isAccepted()
        assert view.transform().m11() == pytest.approx(1.2 ** steps, rel=1e-9)
        after = view.mapToScene(point)
        assert math.hypot(after.x() - before.x(), after.y() - before.y()) * view.transform().m11() <= 2.5
        assert view.auto_fit is (steps == 0)
        gold.put(f"wheel/{steps}", _state(view))


def test_wheel_is_clamped_to_4_notches_d14(fp, gold):
    """@brief D14: one event of more than 4 notches scales by 1.2**+-4 (before: 1.2**steps, no clamp).

    @note Negative twin: the test above (<= 4 notches unchanged).
    """
    view = fp.preview_view
    for steps in (5, 10, -10):
        _place(fp)
        _wheel(fp, QPoint(300, 220), steps)
        assert view.transform().m11() == pytest.approx(1.2 ** (4 if steps > 0 else -4), rel=1e-9)
        gold.put(f"wheel_clamped/{steps}", _state(view))


def test_wheel_is_refused_while_a_pan_runs_and_works_after_the_release(fp):
    """@brief Positive: refused (and accepted) during a pan; negative twin: zooms again after the release."""
    view = fp.preview_view
    _press(fp, EMPTY)
    assert view.camera.pan_active
    event = _wheel(fp, QPoint(300, 220), 2)
    assert event.isAccepted() and view.transform().m11() == 1.0
    _release(fp, EMPTY)
    _wheel(fp, QPoint(300, 220), 2)
    assert view.transform().m11() == pytest.approx(1.44)


def test_wheel_is_refused_during_a_drag_and_works_after_the_drop(fp):
    """@brief Positive: refused (and accepted) while a text drag runs; negative twin: zooms again after the drop."""
    from kicadgen.gui.footprint_view import TEXT_ANCHOR_ROLE, TEXT_KEY_ROLE
    view = fp.preview_view
    group = next(i for i in fp.preview_scene.items() if i.data(TEXT_KEY_ROLE) == "Reference" and i.parentItem() is None)
    glyphs = next(c for c in group.childItems() if c.data(TEXT_ANCHOR_ROLE) is None)
    centre = view.mapFromScene(glyphs.sceneBoundingRect()).boundingRect().center()
    _press(fp, centre)
    assert view._dragging()
    event = _wheel(fp, QPoint(300, 220), 2)
    assert event.isAccepted() and view.transform().m11() == 1.0
    _release(fp, centre)
    _wheel(fp, QPoint(300, 220), 2)
    assert view.transform().m11() == pytest.approx(1.44)


# ── NUMBERS: pan ───────────────────────────────────────────────────────

def test_pan_moves_the_picture_by_the_pointer_delta(fp, gold):
    """@brief Positive: after press/move/release the picture followed the pointer; auto_fit is off.

    @note Since D17 the scrollbars move by exactly the pointer delta (no drift);
        the full camera is in the golden.
    """
    view = fp.preview_view
    for button in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
        for scale in (0.5, 1.0, 2.5):
            _place(fp, scale)
            view.auto_fit = True            # a pan must end it
            start = view.horizontalScrollBar().value(), view.verticalScrollBar().value()
            _press(fp, EMPTY, button)
            assert view.camera.pan_active and view.camera.pan_button == button
            _move(fp, EMPTY + QPoint(-40, -25))
            _move(fp, EMPTY + QPoint(-70, -45))
            assert view.auto_fit is False
            _release(fp, EMPTY + QPoint(-70, -45), button)
            assert not view.camera.pan_active
            moved = (view.horizontalScrollBar().value() - start[0], view.verticalScrollBar().value() - start[1])
            assert moved == (70, 45), moved
            gold.put(f"pan/{button.name}/{scale}", {"moved": list(moved), **_state(view)})


def test_a_zero_delta_move_does_not_end_auto_fit(fp):
    """@brief Negative: a press and a move to the same point leave auto_fit untouched; a real move ends it."""
    view = fp.preview_view
    view.auto_fit = True
    _press(fp, EMPTY)
    view.mouseMoveEvent(_mouse_move_event(view, EMPTY))
    assert view.auto_fit is True
    view.mouseMoveEvent(_mouse_move_event(view, EMPTY + QPoint(5, 0)))
    assert view.auto_fit is False
    _release(fp, EMPTY + QPoint(5, 0))


def _mouse_move_event(view, pos):
    from PyQt6.QtGui import QMouseEvent
    return QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)),
                       Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


def test_escape_ends_the_pan_and_the_camera_stays(fp):
    """@brief Positive: Escape ends the pan, later moves do not move the camera; negative: no pan, no effect."""
    view = fp.preview_view
    _press(fp, EMPTY)
    _move(fp, EMPTY + QPoint(-30, -20))
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert not view.camera.pan_active
    frozen = _state(view)
    _move(fp, EMPTY + QPoint(-90, -60))
    _release(fp, EMPTY + QPoint(-90, -60))
    assert _state(view) == frozen
    # Negative twin: Escape with no pan running changes no camera number.
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert _state(view) == frozen


def test_a_pan_step_does_not_drift_and_a_null_step_does_not_move_d17(fp):
    """@brief D17: the shared camera pans by scrollbars, so a step moves the picture by exactly the pointer delta.

    @note Before (phase 1, `centerOn` pan) each moving step added a (-1, -1)
        scrollbar drift because `QRect.center()` of an even viewport is half a
        pixel short of the true middle, and null steps and the release drifted
        too. Evidence on the old algorithm lives in tests/test_canvas_camera.py.
    """
    view = fp.preview_view
    _place(fp, 2.0)
    bar = (view.horizontalScrollBar(), view.verticalScrollBar())
    start = bar[0].value(), bar[1].value()
    _press(fp, EMPTY)
    view.mouseMoveEvent(_mouse_move_event(view, EMPTY))
    view.mouseMoveEvent(_mouse_move_event(view, EMPTY))
    assert (bar[0].value(), bar[1].value()) == start, "null steps do nothing"
    view.mouseMoveEvent(_mouse_move_event(view, EMPTY + QPoint(-10, 0)))
    assert (bar[0].value() - start[0], bar[1].value() - start[1]) == (10, 0)
    _release(fp, EMPTY + QPoint(-10, 0))
    assert (bar[0].value() - start[0], bar[1].value() - start[1]) == (10, 0), "the release at the last point is a null step"


@pytest.mark.parametrize("button", [Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton], ids=["middle", "left"])
def test_forty_one_pixel_moves_equal_one_forty_pixel_move(fp, button):
    """@brief User level (real events): the point under the pointer at the press stays under it; many small steps == one big one."""
    view = fp.preview_view
    _place(fp, 2.0)
    grabbed = view.mapToScene(EMPTY)
    _press(fp, EMPTY, button)
    for i in range(1, 41):
        _move(fp, EMPTY + QPoint(-i, 0))
    _release(fp, EMPTY + QPoint(-40, 0), button)
    many = _state(view)
    under = view.mapToScene(EMPTY + QPoint(-40, 0))
    assert math.hypot(under.x() - grabbed.x(), under.y() - grabbed.y()) * view.transform().m11() <= 1.0
    _place(fp, 2.0)
    _press(fp, EMPTY, button)
    _move(fp, EMPTY + QPoint(-40, 0))
    _release(fp, EMPTY + QPoint(-40, 0), button)
    assert _state(view) == many


def test_zoom_in_then_out_about_one_anchor_returns_within_one_pixel(fp):
    """@brief Positive: in then out about the same anchor restores scale and the scene point under it (1 px); no drift."""
    view = fp.preview_view
    anchor = QPoint(300, 220)
    _place(fp)
    start = view.mapToScene(anchor)
    for _ in range(5):
        view.zoom(1.5, anchor)
        view.zoom(1 / 1.5, anchor)
    assert view.transform().m11() == pytest.approx(1.0)
    back = view.mapToScene(anchor)
    assert math.hypot(back.x() - start.x(), back.y() - start.y()) <= 1.0


# ── NUMBERS: fit ───────────────────────────────────────────────────────

def test_fit_frames_the_non_grid_items_with_a_12px_margin_and_sets_the_scene_rect(fp, gold):
    """@brief Positive: `_fit_preview(force=True)` sets the scene rect to content + 12 px and fits it, KeepAspectRatio."""
    view = fp.preview_view
    expected = _content_rect(fp)
    fp._fit_preview(force=True)
    assert fp.preview_scene.sceneRect() == expected
    assert view.auto_fit is True
    visible = view.mapToScene(view.viewport().rect()).boundingRect()
    assert visible.contains(expected.adjusted(1, 1, -1, -1)), "the framed rect is inside the view"
    # KeepAspectRatio: the limiting axis fills the viewport, within the 2 px of margin fitInView keeps.
    fit = min(view.viewport().width() / expected.width(), view.viewport().height() / expected.height())
    assert view.transform().m11() == pytest.approx(fit, rel=0.02)
    assert (view.transform().m12(), view.transform().m21()) == (0.0, 0.0)
    gold.put("fit/forced", {**_state(view), "rect": [_r(expected.x()), _r(expected.y()),
                                                      _r(expected.width()), _r(expected.height())]})


def test_fit_ignores_the_grid_items(fp):
    """@brief Positive: toggling or changing the unit of the grid leaves the framing identical; negative: grid lines do exist."""
    from kicadgen.gui import main_window as gui
    view = fp.preview_view
    assert any(i.data(gui._GRID_ITEM) for i in fp.preview_scene.items())
    fp._fit_preview(force=True)
    reference = (_state(view), fp.preview_scene.sceneRect())
    fp.chk_grid.setChecked(False)
    fp._fit_preview(force=True)
    assert (_state(view), fp.preview_scene.sceneRect()) == reference
    fp.chk_grid.setChecked(True)
    fp.combo_grid_unit.setCurrentIndex(fp.combo_grid_unit.findData("inch"))
    fp._fit_preview(force=True)
    assert (_state(view), fp.preview_scene.sceneRect()) == reference


def test_fit_without_force_follows_auto_fit_but_always_sets_the_scene_rect(fp):
    """@brief Positive: auto_fit True refits; negative: auto_fit False keeps transform and scroll, scene rect still updated."""
    view = fp.preview_view
    view.zoom(3.0, QPoint(200, 150))
    assert view.auto_fit is False
    frozen = _state(view)
    fp.preview_scene.setSceneRect(QRectF(0, 0, 5, 5))
    fp._fit_preview()
    assert _state(view) == frozen, "the user's camera is kept"
    assert fp.preview_scene.sceneRect() == _content_rect(fp), "the scene rect is refreshed anyway"
    view.auto_fit = True
    fp._fit_preview()
    assert _state(view)["scale"] != frozen["scale"], "following the footprint again"
    assert view.auto_fit is True


def test_force_fit_goes_back_to_following_the_footprint(fp):
    """@brief «Adatta alla vista»: force fits even with auto_fit off and turns it on."""
    view = fp.preview_view
    view.zoom(3.0)
    fp._fit_preview(force=True)
    assert view.auto_fit is True


def test_fit_with_an_empty_scene_changes_nothing(fp):
    """@brief Negative: no items (nor a bounding rect) means no rect, no scene-rect change, no camera change."""
    view = fp.preview_view
    fp.preview_scene.clear()
    fp.preview_scene.setSceneRect(QRectF(1, 2, 30, 40))
    view.auto_fit = True
    before = _state(view)
    fp._fit_preview(force=True)
    assert _state(view) == before and fp.preview_scene.sceneRect() == QRectF(1, 2, 30, 40)


def test_fit_resets_a_zoomed_transform_before_fitting(fp):
    """@brief `reset=True` of the plan: the result does not depend on the zoom reached before the fit."""
    view = fp.preview_view
    fp._fit_preview(force=True)
    reference = _state(view)
    for factor in (5.0, 0.2):
        view.zoom(factor, QPoint(200, 150))
        fp._fit_preview(force=True)
        assert _state(view) == reference


# ── NUMBERS: fitInView calls, resize, auto_fit ─────────────────────────

class _FitCounter:
    def __init__(self, view, monkeypatch):
        self.count = 0
        original = view.fitInView

        def counting(*args, **kwargs):
            self.count += 1
            return original(*args, **kwargs)
        monkeypatch.setattr(view, "fitInView", counting)


def test_a_redraw_that_leaves_the_camera_unchanged_does_not_refit_d15(fp, monkeypatch):
    """@brief D15: the memo skips `fitInView` for a redraw that changes neither the rectangle nor the camera.

    @note Before: every redraw while following called `fitInView` (1, 1, 1).
        Negative twins: a camera moved from outside, a different framing rect
        and `force` all refit; a user camera (auto_fit off) never does.
    """
    from PyQt6.QtWidgets import QGraphicsRectItem
    view = fp.preview_view
    counter = _FitCounter(view, monkeypatch)
    view.auto_fit = True
    fp._draw_preview()
    first = counter.count
    fp._draw_preview()
    fp._draw_preview()
    assert (first, counter.count) == (1, 1)
    view.scale(1.3, 1.3)                       # moved from outside while auto_fit is still on
    fp._draw_preview()
    assert counter.count == 2, "a changed camera is refitted"
    fp._fit_preview(force=True)
    assert counter.count == 3, "force always fits"
    item = QGraphicsRectItem(500, 500, 40, 40)
    fp.preview_scene.addItem(item)
    fp._fit_preview()
    assert counter.count == 4, "a different framing rectangle is refitted"
    fp.preview_scene.removeItem(item)
    counter.count = 0
    view.zoom(2.0)
    fp._draw_preview()
    fp._draw_preview()
    assert counter.count == 0, "a user camera is never refitted"


def test_resize_refits_while_following_and_never_a_user_camera_d15(fp, monkeypatch, qapp):
    """@brief D15: while auto_fit is on a resize re-frames the footprint (the provider's rectangle); a user camera is left alone.

    @note Before: a resize never refitted (the transform stayed, 0 calls).
    """
    view = fp.preview_view
    fp._fit_preview(force=True)
    qapp.processEvents()
    counter = _FitCounter(view, monkeypatch)
    before = view.transform().m11()
    fp.resize(1100, 760)
    qapp.processEvents()
    qapp.processEvents()
    assert counter.count >= 1 and view.auto_fit is True
    assert view.transform().m11() != before
    expected = fp._preview_framing_rect()
    assert view.mapToScene(view.viewport().rect()).boundingRect().contains(expected.adjusted(1, 1, -1, -1))
    view.zoom(1.5)
    counter.count = 0
    kept = _state(view)
    fp.resize(1400, 900)
    qapp.processEvents()
    assert counter.count == 0 and view.auto_fit is False
    assert _state(view)["scale"] == kept["scale"]


def test_auto_fit_transitions(fp):
    """@brief auto_fit is True at start, False after zoom, wheel or a moving pan; True after a forced fit; untouched by a redraw."""
    view = fp.preview_view
    assert view.auto_fit in (True, False)
    for how in ("zoom", "wheel", "pan"):
        view.auto_fit = True
        if how == "zoom":
            view.zoom(1.1)
        elif how == "wheel":
            _wheel(fp, QPoint(300, 220), 1)
        else:
            _press(fp, EMPTY)
            _move(fp, EMPTY + QPoint(-20, -10))
            _release(fp, EMPTY + QPoint(-20, -10))
        assert view.auto_fit is False, how
    fp._fit_preview(force=True)
    assert view.auto_fit is True
    fp._draw_preview()
    assert view.auto_fit is True
    view.auto_fit = False
    fp._draw_preview()
    assert view.auto_fit is False


# ── PIXELS ─────────────────────────────────────────────────────────────

STEP_MM = {"mm": 20.0, "inch": 2.54 * 20.0}
SCALES = (0.3, 1.0, 2.0, 5.0)
DIVISORS = (1, 2, 5, 10)
#: Origin at the scene origin and away from it (the first pad of a castellated module sits off the origin).
ORIGINS = {"origin0": (0.0, 0.0), "pad1": (-37.0, 18.0)}


def _sparse_region(view):
    """Two far-apart rectangles of the viewport, as two dirty items leave them."""
    w, h = view.viewport().width(), view.viewport().height()
    rects = [QRect(w // 10, h // 10, 60, 40), QRect(w * 3 // 4, h * 3 // 4, 80, 50)]
    return QRegion(rects[0]) | QRegion(rects[1])


class _Counting(QPainter):
    """A painter that counts the lines and dots the grid asks for."""

    def __init__(self, image):
        super().__init__(image)
        self.lines = 0
        self.dots = 0

    def drawLine(self, *args):
        self.lines += 1
        super().drawLine(*args)

    def drawPoints(self, polygon):
        self.dots += len(polygon) if isinstance(polygon, (list, tuple)) else polygon.count()
        super().drawPoints(polygon)


def _paint_background(view, region=None):
    """Paint only `drawBackground` into an image, as Qt does: scene transform, view hints, clip and rect of the region.

    @return (image, painter) -- the painter carries the line and dot counts.
    @note Phase 2 installs the paint-event region the grid lays out against;
        `_expose` below is the single place to adapt then.
    """
    viewport = view.viewport()
    image = QImage(viewport.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.black)
    painter = _Counting(image)
    painter.setRenderHints(view.renderHints())
    rect_px = viewport.rect() if region is None else region.boundingRect()
    if region is not None:
        painter.setClipRegion(region)
    painter.setWorldTransform(view.viewportTransform())
    exposed = _expose(view, region)
    try:
        view.drawBackground(painter, view.mapToScene(rect_px).boundingRect())
    finally:
        exposed()
        painter.end()
    return image, painter


def _expose(view, region):
    """Tell the view which region is being painted (the shared ExposedRegion hook); return the function that clears it."""
    if region is None:
        return lambda: None
    view._exposed.region = region

    def clear():
        view._exposed.region = None
    return clear


def _digest(image):
    return hashlib.sha256(image.constBits().asstring(image.sizeInBytes())).hexdigest()


@pytest.fixture(scope="module")
def grid_gold():
    holder = _Gold()
    yield holder
    _golden("grid", holder.data)


def test_the_grid_pixels_for_each_unit_divisor_zoom_and_origin(fp, grid_gold):
    """@brief SHA-256 of the background alone at four zooms; dots appear exactly when fine step * scale >= the legibility limit.

    @note Positive: dots legible -> the image differs from the lines-only one;
        negative: not legible, or divisor 1 -> identical to the lines-only picture.
    """
    view = fp.preview_view
    for unit in sorted(STEP_MM):
        for divisor in DIVISORS:
            for origin_name in sorted(ORIGINS):
                step = STEP_MM[unit]
                view.set_grid(step, ORIGINS[origin_name], True)
                for scale in SCALES:
                    _place(fp, scale)
                    view.set_fine_divisor(divisor)
                    image, painter = _paint_background(view)
                    legible = divisor > 1 and (step / divisor) * scale >= FINE_GRID_MIN_SCREEN_PX
                    assert view.fine_dots_visible() is legible
                    assert (painter.dots > 0) is legible
                    view.set_fine_divisor(1)
                    lines_only, _ = _paint_background(view)
                    assert (_digest(image) != _digest(lines_only)) is legible
                    grid_gold.put(f"full/{unit}/{divisor}/{origin_name}/{scale}",
                                  {"sha": _digest(image), "lines": painter.lines, "dots": painter.dots})


def test_the_grid_pixels_with_a_sparse_paint_region(fp, grid_gold):
    """@brief A sparse clip paints exactly the pixels the full viewport paints inside that clip (golden: hash + counts).

    @note D16: with the paint region known only the lines and dots reaching it
        are laid out (fewer calls, the same pixels; the hash is the one of the
        whole-rectangle layout recorded before the shared grid). Negative twin:
        a region covering the whole viewport lays out everything.
    """
    view = fp.preview_view
    view.set_grid(20.0, ORIGINS["pad1"], True)
    for divisor, scale in ((1, 1.0), (1, 2.0), (5, 1.0), (5, 2.0)):
        _place(fp, scale)
        view.set_fine_divisor(divisor)
        region = _sparse_region(view)
        sparse, painter = _paint_background(view, region)
        full, _ = _paint_background(view)
        clipped = QImage(full.size(), QImage.Format.Format_ARGB32_Premultiplied)
        clipped.fill(Qt.GlobalColor.black)
        keep = QPainter(clipped)
        keep.setClipRegion(region)
        keep.drawImage(0, 0, full)
        keep.end()
        assert _digest(sparse) == _digest(clipped)
        _, whole = _paint_background(view, QRegion(view.viewport().rect()))
        assert 0 < painter.lines < whole.lines, "D16: fewer lines laid out than for the whole viewport"
        if divisor > 1 and view.fine_dots_visible():
            assert 0 < painter.dots < whole.dots
        grid_gold.put(f"sparse/{divisor}/{scale}", {"sha": _digest(sparse), "lines": painter.lines, "dots": painter.dots})


def test_a_hidden_or_zero_step_grid_paints_no_lines(fp):
    """@brief Negative: hidden or non-positive step -> only the background fill, no lines, no dots."""
    view = fp.preview_view
    view.set_grid(20.0, (0.0, 0.0), False)
    blank, _ = _paint_background(view)
    for args in ((20.0, (0.0, 0.0), False), (0.0, (0.0, 0.0), True), (-5.0, (0.0, 0.0), True)):
        view.set_grid(*args)
        image, painter = _paint_background(view)
        assert (painter.lines, painter.dots) == (0, 0), args
    view.set_grid(20.0, (0.0, 0.0), True)
    image, painter = _paint_background(view)
    assert painter.lines > 0 and _digest(image) != _digest(blank), "negative twin: a shown grid does paint"


def test_lines_and_dots_use_the_module_colours(fp):
    """@brief Lines come out in (about) GRID_LINE_COLOR, the dots only add brighter pixels the lines-only picture lacks.

    @note The view's own render hints decide whether the strokes are blended
        (the footprint leaves the painter's antialiasing as it is), so the
        colours are compared with a tolerance; exact pixels are in the hashes.
    """
    from kicadgen.gui.footprint_view import GRID_LINE_COLOR
    view = fp.preview_view
    _place(fp, 5.0)
    view.set_grid(20.0, (0.0, 0.0), True)
    view.set_fine_divisor(5)
    with_dots, painter = _paint_background(view)
    view.set_fine_divisor(1)
    lines_only, _ = _paint_background(view)
    assert painter.dots > 0
    wanted = QColor(GRID_LINE_COLOR)

    canvas = QColor(lines_only.pixel(0, 0))
    row = [QColor(lines_only.pixel(x, 5)) for x in range(lines_only.width())]
    brightest = max(row, key=lambda c: c.lightness())
    # Blended or not, a line pixel lies on the segment canvas -> line colour.
    for get in ("red", "green", "blue"):
        low, high = sorted((getattr(canvas, get)(), getattr(wanted, get)()))
        assert low <= getattr(brightest, get)() <= high and brightest.lightness() > canvas.lightness(), get
    changed = [(x, y) for x in range(300) for y in range(300) if with_dots.pixel(x, y) != lines_only.pixel(x, y)]
    assert changed, "the dots add pixels"
    assert all(QColor(with_dots.pixel(x, y)).lightness() > canvas.lightness() for x, y in changed)



def test_the_grab_of_the_whole_viewport_is_stable_in_one_process(fp):
    """@brief Two grabs of the same state are byte-identical (the in-run reference used before and after the swap)."""
    view = fp.preview_view
    _place(fp, 2.0)
    view.set_fine_divisor(5)
    first = view.viewport().grab().toImage()
    second = view.viewport().grab().toImage()
    assert _digest(first) == _digest(second)
