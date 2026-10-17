"""@brief Characterisation of the symbol editor's camera and grid, pinned before the shared modules.

Plan `docs/canvas-camera-grid-plan.md` section 4 step 1 (2026-10-17): the
camera (zoom, wheel, pan, fit) and the background grid of `_SymbolView` will
move to `canvas_camera.py` / `canvas_grid.py`. Before anything moves, both
are recorded here as they behave TODAY.

NUMBERS: scale after `zoom` at several anchors and factors, limit hits,
wheel deltas (the +-4 notch clamp, the refusal during a pan), pan deltas and
the resulting scroll values / scene centre, the `fit` memo, the
`resizeEvent` / `showEvent` refit, `auto_fit` transitions and the
`camera_changed` emission counts. The numbers of one scripted session are
compared with `tests/data/camera_golden/symbol_camera.json`.

PIXELS: byte captures (`viewport().grab()` and the sparse-region painter of
tests/test_symbol_grid_paint_region.py) of the grid at several zooms, with the
drawing grid equal to and different from the pin grid, dots legible and not
legible, both themes. Each capture is compared (a) with its twin taken in the
same process under another route (an exposed region versus none) and (b)
with the SHA-1 stored in `tests/data/camera_golden/symbol_pixels.json`; the
PNGs of the run go to the directory named by `KICADGEN_CAMERA_PNG_DIR`
when set. Goldens are rewritten only with `KICADGEN_REGEN_CAMERA_GOLDEN=1`.

D13 (phase 2, 2026-10-17): the zoom beyond the limits is CLAMPED instead of
refused; `test_d13_zoom_beyond_the_limits_is_clamped` and the golden session
record the new contract.
"""
import hashlib
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QRegion, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from kicadgen.gui.symbol_tab import CANVAS_THEMES, FINE_GRID_MIN_PX, GRID_MM, SCENE_UNITS_PER_MM, SymbolTab

GOLDEN_DIR = Path(__file__).parent / "data" / "camera_golden"
GOLDEN_CAMERA = GOLDEN_DIR / "symbol_camera.json"
GOLDEN_PIXELS = GOLDEN_DIR / "symbol_pixels.json"
REGEN = os.environ.get("KICADGEN_REGEN_CAMERA_GOLDEN") == "1"
PNG_DIR = os.environ.get("KICADGEN_CAMERA_PNG_DIR")

ZOOM_MIN, ZOOM_MAX = 0.15, 20.0
LEFT, MIDDLE, NOMOD = Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.NoModifier


@pytest.fixture
def tab(qapp):
    """A shown tab with 40 pins on the four sides, 1200x800, fitted."""
    sides = ("left", "right", "top", "bottom")
    widget = SymbolTab()
    widget.resize(1200, 800)
    widget.set_data("Board", [{"pin": str(i), "name": f"P{i}", "symbol_side": sides[i % 4]}
                              for i in range(1, 41)], pad_ids=[str(i) for i in range(1, 41)])
    widget.show()
    qapp.processEvents()
    yield widget
    widget.deleteLater()


def _free(tab, scale=1.0):
    """Camera at a known state: auto-fit off, scaled by `scale`, centred on the scene origin."""
    view = tab.view
    view.auto_fit = False
    view.resetTransform()
    view.scale(scale, scale)
    view.centerOn(0, 0)
    QApplication.processEvents()
    return view


def _wheel(view, pos, delta):
    """Send one wheel event of `delta` units (120 per notch) at viewport point `pos`; returns the event."""
    viewport = view.viewport()
    event = QWheelEvent(QPointF(pos), QPointF(viewport.mapToGlobal(pos)), QPoint(), QPoint(0, delta),
                        Qt.MouseButton.NoButton, NOMOD, Qt.ScrollPhase.NoScrollPhase, False)
    event.setAccepted(False)
    QApplication.sendEvent(viewport, event)
    QApplication.processEvents()
    return event


def _state(view):
    """Numbers that define the camera: scale, scroll values, scene centre, auto-fit."""
    centre = view.mapToScene(view.viewport().rect().center())
    return {"scale": round(view.transform().m11(), 9),
            "h": view.horizontalScrollBar().value(), "v": view.verticalScrollBar().value(),
            "cx": round(centre.x(), 6), "cy": round(centre.y(), 6), "auto_fit": view.auto_fit}


class _Emits:
    """Counts `camera_changed` emissions."""

    def __init__(self, view):
        self.n = 0
        view.camera_changed.connect(self._hit)

    def _hit(self):
        self.n += 1

    def take(self):
        n, self.n = self.n, 0
        return n


def _empty(view):
    """A viewport point with no item under it."""
    point = QPoint(5, 5)
    assert not view.items(point)
    return point


def _drag(view, button, start, steps):
    """Press `button` at `start`, move through `steps` (offsets from start), release at the last one."""
    QTest.mousePress(view.viewport(), button, NOMOD, start)
    for step in steps:
        QTest.mouseMove(view.viewport(), start + step)
        QApplication.processEvents()
    QTest.mouseRelease(view.viewport(), button, NOMOD, start + steps[-1])
    QApplication.processEvents()


# ── NUMBERS: zoom ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("factor", [1.2, 0.5, 2.0, 1.2 ** 4])
@pytest.mark.parametrize("anchor", [None, QPoint(100, 100), QPoint(900, 650), QPoint(600, 400)])
def test_zoom_scales_by_the_factor_keeps_the_anchor_and_emits_once(tab, factor, anchor):
    """@brief Positive: scale multiplies by `factor`, the scene point under the anchor stays put, one signal, auto-fit off."""
    view = _free(tab, 1.0)
    emits = _Emits(view)
    point = anchor if anchor is not None else view.viewport().rect().center()
    under = view.mapToScene(point)
    view.auto_fit = True
    view.zoom(factor, anchor)
    assert view.transform().m11() == pytest.approx(factor)
    assert not view.auto_fit
    after = view.mapToScene(point)
    tolerance = 1.0 / min(1.0, factor) + 1.0
    assert (after.x(), after.y()) == pytest.approx((under.x(), under.y()), abs=tolerance)
    assert emits.take() == 1


def test_zoom_without_anchor_zooms_about_the_viewport_centre(tab):
    view = _free(tab, 1.0)
    centre = view.mapToScene(view.viewport().rect().center())
    view.zoom(2.0)
    after = view.mapToScene(view.viewport().rect().center())
    assert (after.x(), after.y()) == pytest.approx((centre.x(), centre.y()), abs=1.0)


def test_d13_zoom_beyond_the_limits_is_clamped(tab):
    """@brief D13 (applied 2026-10-17; before it the whole step was REFUSED): a step past [0.15, 20] lands on the limit.

    The old refusal meant a wheel step that would overshoot did nothing, so the
    user could not reach the limit. Positive: the scale reaches the limit, the
    anchor stays fixed, auto-fit goes off, one signal. Negative: already at the
    limit the same step changes nothing (no signal, auto-fit untouched).
    """
    view = _free(tab, 10.0)
    emits = _Emits(view)
    anchor = QPoint(300, 200)
    under = view.mapToScene(anchor)
    view.auto_fit = True
    view.zoom(2.5, anchor)  # would be 25 > 20
    assert view.transform().m11() == pytest.approx(ZOOM_MAX)
    assert not view.auto_fit and emits.take() == 1
    after = view.mapToScene(anchor)
    assert (after.x(), after.y()) == pytest.approx((under.x(), under.y()), abs=1.0)
    view.auto_fit = True
    view.zoom(1.01)  # at the top already: nothing to do
    assert view.transform().m11() == pytest.approx(ZOOM_MAX) and emits.take() == 0 and view.auto_fit
    view.zoom(0.0001)  # far below the bottom: clamped to it
    assert view.transform().m11() == pytest.approx(ZOOM_MIN) and emits.take() == 1
    view.auto_fit = True
    view.zoom(0.5)
    assert view.transform().m11() == pytest.approx(ZOOM_MIN) and emits.take() == 0 and view.auto_fit
    # A step that lands exactly on a limit is applied as before.
    low = _free(tab, 0.3)
    emits.take()
    low.zoom(0.5)
    assert low.transform().m11() == pytest.approx(ZOOM_MIN) and emits.take() == 1


# ── NUMBERS: wheel ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("delta, expected", [(120, 1.2), (-120, 1 / 1.2), (240, 1.2 ** 2), (1200, 1.2 ** 4),
                                             (-1200, 1.2 ** -4), (480, 1.2 ** 4), (60, 1.2 ** 0.5)])
def test_wheel_zooms_about_the_pointer_with_the_notches_clamped_to_four(tab, delta, expected):
    """@brief Positive: 1.2 ** clamp(delta/120, -4, 4) about the pointer; the event is accepted; one signal."""
    view = _free(tab, 1.0)
    emits = _Emits(view)
    pos = QPoint(300, 200)
    under = view.mapToScene(pos)
    event = _wheel(view, pos, delta)
    assert event.isAccepted()
    assert view.transform().m11() == pytest.approx(expected)
    after = view.mapToScene(pos)
    assert (after.x(), after.y()) == pytest.approx((under.x(), under.y()), abs=1.0 / min(1.0, expected) + 1.0)
    assert emits.take() == 1 and not view.auto_fit


def test_wheel_with_no_delta_is_accepted_and_does_nothing(tab):
    view = _free(tab, 1.0)
    emits = _Emits(view)
    event = _wheel(view, QPoint(300, 200), 0)
    assert event.isAccepted() and view.transform().m11() == 1.0 and emits.take() == 0


def test_wheel_is_refused_during_a_pan_but_still_accepted(tab):
    """@brief S11 negative: a pan in progress blocks the wheel; the event is accepted anyway. Positive twin after the release."""
    view = _free(tab, 1.0)
    start = _empty(view)
    QTest.mousePress(view.viewport(), LEFT, NOMOD, start)
    emits = _Emits(view)
    event = _wheel(view, start, 120)
    assert event.isAccepted() and view.transform().m11() == 1.0 and emits.take() == 0
    QTest.mouseRelease(view.viewport(), LEFT, NOMOD, start)
    _wheel(view, start, 120)
    assert view.transform().m11() == pytest.approx(1.2)


# ── NUMBERS: pan ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("button", [LEFT, MIDDLE])
def test_pan_moves_the_scrollbars_by_the_pointer_delta_and_the_centre_with_them(tab, button):
    """@brief Positive: middle press, or left press on the background, pans by the pixel delta; no camera_changed."""
    view = _free(tab, 1.0)
    emits = _Emits(view)
    start = _empty(view)
    before = _state(view)
    view.auto_fit = True
    QTest.mousePress(view.viewport(), button, NOMOD, start)
    assert not view.auto_fit and view.gesture is None
    QTest.mouseMove(view.viewport(), start + QPoint(30, 20))
    QApplication.processEvents()
    mid = _state(view)
    assert (mid["h"] - before["h"], mid["v"] - before["v"]) == (-30, -20)
    QTest.mouseMove(view.viewport(), start + QPoint(10, 50))
    QApplication.processEvents()
    QTest.mouseRelease(view.viewport(), button, NOMOD, start + QPoint(10, 50))
    QApplication.processEvents()
    end = _state(view)
    assert (end["h"] - before["h"], end["v"] - before["v"]) == (-10, -50)
    assert end["cx"] == pytest.approx(before["cx"] - 10, abs=0.01)
    assert end["cy"] == pytest.approx(before["cy"] - 50, abs=0.01)
    assert emits.take() == 0, "a pan emits no camera_changed today"
    assert not view.camera.pan_active


def test_pan_total_delta_is_the_sum_of_the_steps(tab):
    view = _free(tab, 2.0)
    start = _empty(view)
    before = _state(view)
    _drag(view, MIDDLE, start, [QPoint(7, 3), QPoint(20, 9), QPoint(-4, 30)])
    end = _state(view)
    assert (end["h"] - before["h"], end["v"] - before["v"]) == (4, -30)
    assert end["cx"] == pytest.approx(before["cx"] + 4 / 2.0, abs=0.01)
    assert end["cy"] == pytest.approx(before["cy"] - 30 / 2.0, abs=0.01)


def test_escape_ends_a_running_pan_and_keeps_the_camera(tab):
    """@brief Positive: Escape ends the pan where it stands; negative: the pointer no longer pans afterwards."""
    view = _free(tab, 1.0)
    start = _empty(view)
    QTest.mousePress(view.viewport(), LEFT, NOMOD, start)
    QTest.mouseMove(view.viewport(), start + QPoint(12, 8))
    QApplication.processEvents()
    kept = _state(view)
    assert view.camera.pan_active
    QTest.keyClick(view, Qt.Key.Key_Escape)
    QApplication.processEvents()
    assert not view.camera.pan_active and view.gesture is None
    QTest.mouseMove(view.viewport(), start + QPoint(60, 60))
    QApplication.processEvents()
    assert _state(view) == kept
    QTest.mouseRelease(view.viewport(), LEFT, NOMOD, start)


def test_a_left_press_on_an_item_is_a_drag_not_a_pan(tab):
    """@brief Negative: a left press on a pin starts no pan (the gesture rules own it)."""
    view = _free(tab, 3.0)
    item_point = None
    for y in range(0, view.viewport().height(), 6):
        for x in range(0, view.viewport().width(), 6):
            if view.items(QPoint(x, y)):
                item_point = QPoint(x, y)
                break
        if item_point:
            break
    assert item_point is not None
    QTest.mousePress(view.viewport(), LEFT, NOMOD, item_point)
    assert not view.camera.pan_active
    QTest.mouseRelease(view.viewport(), LEFT, NOMOD, item_point)


# ── NUMBERS: fit ────────────────────────────────────────────────────────────

class _FitSpy:
    """Counts `fitInView` calls of a view."""

    def __init__(self, view):
        self.n = 0
        original = view.fitInView

        def spy(*args, **kwargs):
            self.n += 1
            return original(*args, **kwargs)
        view.fitInView = spy


def test_fit_frames_the_scene_rect_turns_auto_fit_on_and_memoises(tab):
    """@brief Positive: first fit refits and emits once; the second, with the camera unchanged, is skipped.

    Negative: a changed viewport size or scene rect refits; an empty scene rect fits nothing but sets auto-fit.
    """
    view = tab.view
    spy = _FitSpy(view)
    emits = _Emits(view)
    view.scene().setSceneRect(-100, -50, 200, 100)
    view.auto_fit = False
    view.fit()
    assert view.auto_fit and spy.n == 1 and emits.take() == 1
    fitted = _state(view)
    view.fit()
    assert spy.n == 1 and emits.take() == 0, "memo: same camera, no second fitInView, no signal"
    assert _state(view) == fitted
    tab.resize(900, 700)
    QApplication.processEvents()
    assert spy.n >= 2 and view.auto_fit, "a resize refits while auto-fit is on"
    emits.take()
    n = spy.n
    view.scene().setSceneRect(-200, -100, 400, 200)
    view.fit()
    assert spy.n == n + 1 and emits.take() == 1, "a changed scene rect refits"
    view.scene().setSceneRect(0, 0, 0, 0)
    view.fit()
    assert spy.n == n + 1 and view.auto_fit and emits.take() == 0


def test_fit_after_a_scroll_refits_because_the_scroll_is_part_of_the_camera_key(tab):
    view = tab.view
    view.fit()
    spy = _FitSpy(view)
    view.horizontalScrollBar().setValue(view.horizontalScrollBar().value() + 17)
    view.fit()
    assert spy.n == 1


def test_resize_and_show_refit_only_while_auto_fit_is_on(tab):
    """@brief Positive: resize refits while auto-fit is on; negative: with it off neither resize nor show touches the camera."""
    view = tab.view
    view.fit()
    before = _state(view)
    tab.resize(1000, 640)
    QApplication.processEvents()
    assert _state(view)["scale"] != before["scale"] and view.auto_fit
    view.auto_fit = False
    kept = _state(view)
    spy = _FitSpy(view)
    tab.resize(1200, 800)
    QApplication.processEvents()
    assert spy.n == 0 and view.transform().m11() == pytest.approx(kept["scale"])
    tab.hide()
    tab.show()
    QApplication.processEvents()
    assert spy.n == 0 and not view.auto_fit
    view.auto_fit = True
    tab.hide()
    QApplication.processEvents()
    tab.show()
    QApplication.processEvents()
    assert view.auto_fit


def test_auto_fit_transitions(tab):
    """@brief zoom, a wheel step and a pan press turn auto-fit off; `fit()` turns it on; a zoom that changes nothing leaves it."""
    view = tab.view
    view.fit()
    assert view.auto_fit
    view.zoom(1.1)
    assert not view.auto_fit
    view.fit()
    assert view.auto_fit
    _wheel(view, QPoint(100, 100), 120)
    assert not view.auto_fit
    view.fit()
    start = _empty(view)
    QTest.mousePress(view.viewport(), LEFT, NOMOD, start)
    assert not view.auto_fit
    QTest.mouseRelease(view.viewport(), LEFT, NOMOD, start)
    view.fit()
    scale = view.transform().m11()
    view.zoom(1000.0)
    assert not view.auto_fit and view.transform().m11() == pytest.approx(ZOOM_MAX), "D13: clamped, so it is a zoom"
    view.fit()
    view.zoom(1.0)  # factor 1: the scale does not change, so auto-fit stays
    assert view.auto_fit and view.transform().m11() == scale


# ── Golden of one scripted session ──────────────────────────────────────────

def _session(tab):
    """A fixed script over the camera; returns the state and signal count after each step."""
    view = tab.view
    emits = _Emits(view)
    out = []

    def mark(label):
        entry = _state(view)
        entry["label"] = label
        entry["emits"] = emits.take()
        out.append(entry)

    view.fit()
    mark("fit")
    view.fit()
    mark("fit again")
    view.zoom(1.5, QPoint(200, 150))
    mark("zoom 1.5 at 200,150")
    view.zoom(0.7, QPoint(900, 600))
    mark("zoom 0.7 at 900,600")
    _wheel(view, QPoint(640, 120), 120 * 7)
    mark("wheel +7 clamped to 4")
    _wheel(view, QPoint(50, 700), -120 * 3)
    mark("wheel -3")
    view.zoom(1000.0)
    mark("zoom 1000 clamped")
    _drag(view, MIDDLE, _empty(view), [QPoint(40, 10), QPoint(55, -22)])
    mark("middle pan")
    view.fit()
    mark("fit after pan")
    tab.resize(1000, 700)
    QApplication.processEvents()
    mark("resize")
    return out


def test_golden_camera_session(tab):
    """@brief The numbers of the scripted session equal `symbol_camera.json` (rewritten with KICADGEN_REGEN_CAMERA_GOLDEN=1).

    D18 (2026-10-17): the zoom re-centres on the exact viewport middle, so the
    picture no longer creeps by one pixel per zoom step. The golden was
    regenerated once for that: only the centre and scroll values after zoom
    steps moved (cumulatively, hence tens of pixels at scale 20); the pan
    delta (-55 px / +22 px scroll) is unchanged.
    """
    result = _session(tab)
    if REGEN:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        GOLDEN_CAMERA.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    golden = json.loads(GOLDEN_CAMERA.read_text(encoding="utf-8"))
    assert len(golden) == len(result)
    for want, got in zip(golden, result):
        assert got["label"] == want["label"]
        assert got["emits"] == want["emits"], got["label"]
        assert got["auto_fit"] == want["auto_fit"], got["label"]
        assert got["scale"] == pytest.approx(want["scale"], rel=1e-7), got["label"]
        assert (got["h"], got["v"]) == (want["h"], want["v"]), got["label"]
        assert (got["cx"], got["cy"]) == pytest.approx((want["cx"], want["cy"]), abs=1e-4), got["label"]


# ── PIXELS ──────────────────────────────────────────────────────────────────

def _arrange(tab, scale, drawing_mm, theme):
    """The view at `scale`, drawing grid `drawing_mm`, canvas theme `theme`, the scene emptied so only the grid is captured."""
    view = tab.view
    view.theme = CANVAS_THEMES[theme]
    view.setBackgroundBrush(QColor(view.theme["canvas"]))
    view.set_drawing_grid(drawing_mm)
    for item in list(view.scene().items()):
        view.scene().removeItem(item)
    view.auto_fit = False
    view.resetTransform()
    view.scale(scale, scale)
    view.centerOn(0, 0)
    QApplication.processEvents()
    return view


def _bytes(image):
    return image.constBits().asstring(image.sizeInBytes())


def _grab(view):
    return view.viewport().grab().toImage().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def _sparse(view):
    """Two far-apart rectangles of the viewport, as two dirty items leave them."""
    w, h = view.viewport().width(), view.viewport().height()
    return QRegion(QRect(w // 10, h // 10, 60, 40)) | QRegion(QRect(w * 3 // 4, h * 3 // 4, 80, 50))


def _paint(view, region, exposed):
    """Paint the background clipped to `region`, with or without the region known to the view."""
    image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(view.theme["canvas"]))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setClipRegion(region)
    painter.setWorldTransform(view.viewportTransform())
    view.exposed.region = region if exposed else None
    try:
        view.drawBackground(painter, view.mapToScene(region.boundingRect()).boundingRect())
    finally:
        view.exposed.region = None
        painter.end()
    return image


def _count_color(image, color):
    """Pixels of `image` whose RGB equals `color` exactly."""
    target = QColor(color).rgb()
    return sum(1 for y in range(image.height()) for x in range(image.width()) if image.pixel(x, y) == target)


#: (label, scale, drawing grid mm, theme): dots legible, not legible, equal grids, both themes, the zoom limits.
PIXEL_CASES = [
    ("equal_dark_x1", 1.0, GRID_MM, "dark"),
    ("equal_light_x3", 3.0, GRID_MM, "kicad"),
    ("fine_dark_legible_x6", 6.0, 0.635, "dark"),
    ("fine_light_legible_x6", 6.0, 0.635, "kicad"),
    ("fine_dark_legible_x3", 3.0, 0.635, "dark"),
    ("fine_dark_legible_x1", 1.0, 0.635, "dark"),
    ("fine_dark_illegible_x0_5", 0.5, 0.635, "dark"),
    ("fine_light_illegible_x0_5", 0.5, 0.635, "kicad"),
    ("coarse_dark_x2", 2.0, 2.54, "dark"),
    ("limit_low_dark_x0_15", 0.15, GRID_MM, "dark"),
    ("limit_high_light_x20", 20.0, 0.635, "kicad"),
]


def _pixel_record(tab):
    """SHA-1 of every capture: the full grab and the sparse paint, which must equal its twin without a region."""
    record = {}
    for label, scale, step, theme in PIXEL_CASES:
        view = _arrange(tab, scale, step, theme)
        full = _grab(view)
        region = _sparse(view)
        with_region = _paint(view, region, exposed=True)
        without = _paint(view, region, exposed=False)
        assert _bytes(with_region) == _bytes(without), f"{label}: the sparse region changes pixels"
        record[label] = {"grab": hashlib.sha1(_bytes(full)).hexdigest(),
                         "sparse": hashlib.sha1(_bytes(with_region)).hexdigest(),
                         "legible": view.fine_grid_visible()}
        if PNG_DIR:
            Path(PNG_DIR).mkdir(parents=True, exist_ok=True)
            full.save(str(Path(PNG_DIR) / f"before_symbol_cam_{label}.png"))
            with_region.save(str(Path(PNG_DIR) / f"before_symbol_cam_{label}_sparse.png"))
    return record


def test_golden_grid_pixels(tab):
    """@brief Every capture hashes to the stored value (a byte-identical grid before and after the shared module)."""
    record = _pixel_record(tab)
    if REGEN:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        GOLDEN_PIXELS.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    assert record == json.loads(GOLDEN_PIXELS.read_text(encoding="utf-8"))


def test_grab_is_repeatable_in_one_process(tab):
    """@brief Twin captures of the same state are byte-equal, so the goldens measure the code and not the noise."""
    view = _arrange(tab, 6.0, 0.635, "dark")
    assert _bytes(_grab(view)) == _bytes(_grab(view))


@pytest.mark.parametrize("theme", ["dark", "kicad"])
def test_dots_appear_only_when_legible_and_distinct_from_the_pin_grid(tab, theme):
    """@brief Positive: the fine grid at x6 paints dot-coloured pixels; negatives: equal grid, or dots closer than the minimum, paint none."""
    colors = CANVAS_THEMES[theme]
    view = _arrange(tab, 6.0, 0.635, theme)
    assert view.fine_grid_visible()
    assert _count_color(_grab(view), colors["grid_dots"]) > 0
    view = _arrange(tab, 6.0, GRID_MM, theme)
    assert not view.fine_grid_visible()
    assert _count_color(_grab(view), colors["grid_dots"]) == 0
    view = _arrange(tab, 0.5, 0.635, theme)
    assert 0.635 * SCENE_UNITS_PER_MM * 0.5 < FINE_GRID_MIN_PX and not view.fine_grid_visible()
    image = _grab(view)
    assert _count_color(image, colors["grid_dots"]) == 0
    assert _count_color(image, colors["grid"]) > 0, "the pin-grid lines are still drawn"


def test_fine_grid_legibility_threshold_is_the_minimum_pixel_spacing(tab):
    """@brief The rule `drawing_mm * 10 * scale >= FINE_GRID_MIN_PX`, both sides of the boundary; hidden grid paints nothing."""
    just_in = FINE_GRID_MIN_PX / (0.635 * SCENE_UNITS_PER_MM)
    assert _arrange(tab, just_in * 1.001, 0.635, "dark").fine_grid_visible()
    assert not _arrange(tab, just_in * 0.999, 0.635, "dark").fine_grid_visible()
    view = _arrange(tab, 6.0, 0.635, "dark")
    view.show_grid = False
    assert not view.fine_grid_visible()
    image = _grab(view)
    assert _count_color(image, CANVAS_THEMES["dark"]["grid"]) == 0
    assert _count_color(image, CANVAS_THEMES["dark"]["grid_dots"]) == 0


def test_a_pin_grid_line_passes_through_the_scene_origin(tab):
    """@brief Origin 0 for the pin grid: the line pixels sit within 1.5 px of the mapped scene x=0 and of x=k*step.

    Negative: columns 4 px away from every line are canvas coloured.
    """
    scale = 3.0
    view = _arrange(tab, scale, GRID_MM, "dark")
    image = _grab(view)
    canvas = QColor(CANVAS_THEMES["dark"]["canvas"]).rgb()
    origin = view.viewportTransform().map(QPointF(0, 0))
    y = int(origin.y()) + int(GRID_MM * SCENE_UNITS_PER_MM * scale / 2)  # away from any row line
    step_px = GRID_MM * SCENE_UNITS_PER_MM * scale
    for k in (-3, -1, 0, 1, 2):
        at = origin.x() + k * step_px
        near = [x for x in range(int(at) - 3, int(at) + 4) if image.pixel(x, y) != canvas]
        assert near and all(abs(x + 0.5 - at) <= 1.5 for x in near), (k, at, near)
        assert image.pixel(int(at) - 4, y) == canvas and image.pixel(int(at) + 4, y) == canvas


def test_camera_goldens_are_read_as_parsed_json_whatever_the_line_endings(tmp_path):
    """@brief Positive: a CRLF copy of a golden parses to the same data; negative: a changed value parses differently."""
    for golden in (GOLDEN_CAMERA, GOLDEN_PIXELS):
        data = json.loads(golden.read_text(encoding="utf-8"))
        text = json.dumps(data, indent=1, sort_keys=True)
        crlf = tmp_path / golden.name
        crlf.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        assert json.loads(crlf.read_text(encoding="utf-8")) == data
        changed = json.loads(text)
        entry = changed[0] if isinstance(changed, list) else next(iter(changed.values()))
        entry[next(iter(entry))] = "changed"
        crlf.write_bytes(json.dumps(changed, indent=1, sort_keys=True).replace("\n", "\r\n").encode("utf-8"))
        assert json.loads(crlf.read_text(encoding="utf-8")) != data


def test_a_pan_of_forty_one_pixel_moves_keeps_the_pressed_point_under_the_pointer(tab):
    """@brief User level: middle press, 40 one-pixel moves, release; the pressed scene point follows the pointer.

    Positive: the scene point under the pointer at the press is under it at
    the end (within 1 px) and the camera equals a single 40 px move.
    Negative twin: the same press with no move leaves the camera alone.
    """
    view = _free(tab, 2.0)
    start = _empty(view)
    before = _state(view)
    under = view.mapToScene(start)
    QTest.mousePress(view.viewport(), MIDDLE, NOMOD, start)
    for i in range(1, 41):
        QTest.mouseMove(view.viewport(), start + QPoint(i, 0))
        QApplication.processEvents()
    QTest.mouseRelease(view.viewport(), MIDDLE, NOMOD, start + QPoint(40, 0))
    QApplication.processEvents()
    after = view.mapToScene(start + QPoint(40, 0))
    assert (after.x(), after.y()) == pytest.approx((under.x(), under.y()), abs=1.0 / 2.0 + 1e-6)
    stepped = _state(view)
    single = _free(tab, 2.0)
    QTest.mousePress(single.viewport(), MIDDLE, NOMOD, start)
    QTest.mouseMove(single.viewport(), start + QPoint(40, 0))
    QApplication.processEvents()
    QTest.mouseRelease(single.viewport(), MIDDLE, NOMOD, start + QPoint(40, 0))
    QApplication.processEvents()
    assert _state(single) == stepped
    assert stepped["h"] - before["h"] == -40 and stepped["v"] == before["v"]
    still = _free(tab, 2.0)
    kept = _state(still)
    QTest.mousePress(still.viewport(), MIDDLE, NOMOD, start)
    QTest.mouseRelease(still.viewport(), MIDDLE, NOMOD, start)
    QApplication.processEvents()
    assert (_state(still)["h"], _state(still)["v"]) == (kept["h"], kept["v"])


@pytest.mark.parametrize("anchor", [QPoint(200, 150), QPoint(900, 600), QPoint(600, 400)])
def test_zoom_in_then_out_about_the_same_anchor_returns_within_one_pixel(tab, anchor):
    """@brief D18: the picture no longer creeps by one pixel per zoom; in then out returns to the start (within 1 px).

    Negative twin: zooming in only moves the scroll values far more than 1 px.
    """
    view = _free(tab, 1.0)
    before = _state(view)
    view.zoom(1.2, anchor)
    moved = _state(view)
    assert abs(moved["h"] - before["h"]) + abs(moved["v"] - before["v"]) > 2
    view.zoom(1 / 1.2, anchor)
    after = _state(view)
    assert after["scale"] == pytest.approx(before["scale"])
    assert abs(after["h"] - before["h"]) <= 1 and abs(after["v"] - before["v"]) <= 1
