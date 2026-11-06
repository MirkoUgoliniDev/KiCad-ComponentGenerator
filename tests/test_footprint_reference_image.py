"""@brief The reference image under the footprint drawing (2026-11-06, priority 3 C).

A picture laid under the preview: the project's calibrated photo at the
scale of the board size, or an image file scaled by the user (typed, or
from two clicks and a known distance), placed by an offset, faded by an
opacity; a view setting saved with the project, hidden by the «Immagine di
riferimento» layer box, never written to the .kicad_mod.
"""
import pytest
from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QGraphicsPixmapItem

from kicadgen.gui.footprint_view import LAYER_ROLE
from tests.test_package_project_gui import acquire
from tests.test_photo_inventory_gui import gui  # noqa: F401 - window fixture


def _png_bytes(width: int, height: int) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#336699"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def _image_item(window):
    items = [i for i in window.preview_scene.items()
             if isinstance(i, QGraphicsPixmapItem) and i.data(LAYER_ROLE) == "reference"]
    assert len(items) <= 1
    return items[0] if items else None


FILE_RECORD = {"source": "file", "path": "", "mm_per_px_x": 0.5, "mm_per_px_y": 0.25,
               "offset_x": 2.0, "offset_y": -3.0, "opacity": 0.4}


def test_the_record_validator_keeps_sane_records_and_drops_the_rest(window):  # noqa: F811
    valid = window._valid_reference_image({**FILE_RECORD, "path": "x.png"})
    assert valid == {**FILE_RECORD, "path": "x.png"}
    photo = window._valid_reference_image({"source": "photo", "mm_per_px_x": None, "mm_per_px_y": None})
    assert photo == {"source": "photo", "path": "", "mm_per_px_x": None, "mm_per_px_y": None,
                     "offset_x": 0.0, "offset_y": 0.0, "opacity": 0.5}
    for bad in ({**FILE_RECORD, "path": ""}, {**FILE_RECORD, "path": "x.png", "mm_per_px_x": 0},
                {**FILE_RECORD, "path": "x.png", "opacity": 2}, {**FILE_RECORD, "path": "x.png", "opacity": 0.0},
                {**FILE_RECORD, "path": "x.png", "offset_x": "a"}, {"source": "web"}, "junk", None):
        assert window._valid_reference_image(bad) is None, bad
    # A refused record never removes the picture in place.
    window.reference_image = window._valid_reference_image({**FILE_RECORD, "path": "x.png"})
    assert window._set_reference_image({**FILE_RECORD, "path": "x.png", "mm_per_px_x": 0}) is False
    assert window.reference_image["mm_per_px_x"] == 0.5
    window.reference_image = None


def test_a_relative_path_is_taken_from_the_project_folder(window, tmp_path):  # noqa: F811
    acquire(window)
    folder = window._project_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ref.png").write_bytes(_png_bytes(10, 10))
    assert window._set_reference_image({**FILE_RECORD, "path": "ref.png"})
    assert window._reference_file_path(window.reference_image) == folder / "ref.png"
    assert _image_item(window) is not None
    assert window._project_state()["reference_image"]["path"] == "ref.png"


def test_a_scan_proposes_the_scale_it_states(tmp_path):
    from kicadgen.gui.main_window import MainWindow
    image = QImage(10, 10, QImage.Format.Format_RGB32)
    image.fill(QColor("#000000"))
    image.setDotsPerMeterX(11811)   # 300 dpi
    image.setDotsPerMeterY(23622)   # 600 dpi
    stated = tmp_path / "scan.png"
    image.save(str(stated))
    scale = MainWindow._image_scale_from_metadata(str(stated))
    assert scale == pytest.approx((25.4 / 300, 25.4 / 600), rel=1e-3)
    plain = tmp_path / "plain.png"
    plain.write_bytes(_png_bytes(10, 10))
    assert MainWindow._image_scale_from_metadata(str(plain)) is None
    assert MainWindow._image_scale_from_metadata(str(tmp_path / "missing.png")) is None


def test_an_image_file_is_drawn_under_the_footprint_scaled_placed_and_faded(window, tmp_path):  # noqa: F811
    acquire(window)
    path = tmp_path / "ref.png"
    path.write_bytes(_png_bytes(40, 20))
    window._set_reference_image({**FILE_RECORD, "path": str(path)})
    item = _image_item(window)
    assert item is not None and item.zValue() == -10 and item.opacity() == pytest.approx(0.4)
    bounds = item.sceneBoundingRect()
    # 40 px x 0.5 mm = 20 mm wide, 20 px x 0.25 mm = 5 mm tall, centred on (2, -3) mm; 20 scene px per mm.
    assert bounds.width() == pytest.approx(20.0 * 20) and bounds.height() == pytest.approx(5.0 * 20)
    assert bounds.center().x() == pytest.approx(2.0 * 20) and bounds.center().y() == pytest.approx(-3.0 * 20)
    # A redraw keeps exactly one image; the layer box hides it; it is saved with the project.
    window._draw_preview()
    assert _image_item(window) is not None
    window.fp_layer_boxes["reference"].setChecked(False)
    assert not _image_item(window).isVisible()
    window.fp_layer_boxes["reference"].setChecked(True)
    assert window._project_state()["reference_image"] == {**FILE_RECORD, "path": str(path)}
    window._set_reference_image(None)
    assert _image_item(window) is None and window._project_state()["reference_image"] is None


def test_a_missing_file_draws_nothing_and_says_so_once(window, tmp_path, monkeypatch):  # noqa: F811
    acquire(window)
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda message, **k: logged.append(message))
    window._set_reference_image({**FILE_RECORD, "path": str(tmp_path / "missing.png")})
    window._draw_preview()
    assert _image_item(window) is None
    assert len([m for m in logged if "non leggibile" in m]) == 1, "cached: one line per source"


def test_the_calibrated_photo_takes_its_scale_from_the_board_size(window):  # noqa: F811
    acquire(window)
    window._photo_png = _png_bytes(200, 100)
    window.field_body_length.setValue(50.0)
    window.field_body_width.setValue(20.0)
    window._photo_body_fields = "matrix"
    window._set_reference_image({"source": "photo"})
    item = _image_item(window)
    assert item is not None
    bounds = item.sceneBoundingRect()
    assert bounds.width() == pytest.approx(50.0 * 20) and bounds.height() == pytest.approx(20.0 * 20)
    assert bounds.center().x() == pytest.approx(0.0) and bounds.center().y() == pytest.approx(0.0)
    # A new photo of the SAME byte length replaces the old one (the cache follows the object).
    old_key = window._reference_pixmap_cache[0]
    window._photo_png = bytes(bytearray(window._photo_png))   # equal bytes, a new object
    window._draw_preview()
    assert window._reference_pixmap_cache[0] != old_key
    # The photo's board fields may differ from the current type's: no log line per redraw.
    logged = []
    window._append_log = lambda message, **k: logged.append(message)
    window._photo_body_fields = "cast"
    window.field_cast_body_length.setValue(63.3)
    window.field_cast_body_width.setValue(25.4)
    window._draw_preview()
    window._draw_preview()
    assert not any("Revisione foto" in m for m in logged)
    # No photo: nothing drawn, no crash.
    window._photo_png = None
    window._reference_pixmap_cache = None
    window._draw_preview()
    assert _image_item(window) is None


def test_the_photo_is_centred_on_the_drawn_body_not_on_the_origin(window, monkeypatch):  # noqa: F811
    """A pin-header footprint has its origin on pin 1: the board photo must sit on the body (audit 2026-11-06)."""
    from kicadgen.generators.pin_header import PinHeaderParams
    acquire(window)
    params = PinHeaderParams(rows=1, cols=4, body_length=25.4, body_width=15.2, body_offset_x=-3.0,
                             body_offset_y=-1.27)
    monkeypatch.setattr(window, "_preview_params", lambda: params)
    window.field_body_length.setValue(25.4)
    window.field_body_width.setValue(15.2)
    window._photo_body_fields = "matrix"
    window._photo_png = _png_bytes(254, 152)
    window._set_reference_image({"source": "photo"})
    window._draw_preview()          # the header drawer declares its body centre
    item = _image_item(window)
    assert item is not None
    centre = item.sceneBoundingRect().center()
    assert centre.x() == pytest.approx((-3.0 + 12.7) * 20) and centre.y() == pytest.approx((-1.27 + 7.6) * 20)
    assert item.sceneBoundingRect().width() == pytest.approx(25.4 * 20)


def test_two_clicks_and_a_known_distance_set_the_scale(window, tmp_path, monkeypatch):  # noqa: F811
    from PyQt6.QtWidgets import QInputDialog
    acquire(window)
    path = tmp_path / "ref.png"
    path.write_bytes(_png_bytes(40, 20))
    window._set_reference_image({**FILE_RECORD, "path": str(path), "mm_per_px_x": 1.0, "mm_per_px_y": 1.0})
    window._start_reference_calibration()
    spec = window.preview_view.click_collector.spec
    assert spec.key == window._REFERENCE_SCALE_TOOL and spec.count == 2
    monkeypatch.setattr(QInputDialog, "getDouble", lambda *a, **k: (5.0, True))
    # The clicked points are not snapped to the grid: 2.5 mm apart, said to be 5 mm.
    window._finish_footprint_drawing(window._REFERENCE_SCALE_TOOL, [(0.0, 0.0), (2.5, 0.0)])
    record = window.reference_image
    assert record["mm_per_px_x"] == pytest.approx(2.0) and record["mm_per_px_y"] == pytest.approx(2.0)
    assert window._fp_draw_tool is None
    # Cancelled dialog: unchanged.
    monkeypatch.setattr(QInputDialog, "getDouble", lambda *a, **k: (5.0, False))
    window._finish_footprint_drawing(window._REFERENCE_SCALE_TOOL, [(0.0, 0.0), (2.5, 0.0)])
    assert window.reference_image["mm_per_px_x"] == pytest.approx(2.0)
    # A value that pushes the scale out of bounds keeps the picture and says so.
    toasts = []
    monkeypatch.setattr(window, "_draw_feedback_toast", lambda title, body: toasts.append(title))
    monkeypatch.setattr(QInputDialog, "getDouble", lambda *a, **k: (10000.0, True))
    window._finish_footprint_drawing(window._REFERENCE_SCALE_TOOL, [(0.0, 0.0), (0.5, 0.0)])
    assert window.reference_image["mm_per_px_x"] == pytest.approx(2.0) and toasts == ["Calibrazione dell'immagine"]
    # The tool's clicks are not snapped to the grid, and its ghost is a line.
    from PyQt6.QtCore import QPointF
    from kicadgen.gui.canvas_clicktools import ToolSpec
    spec = ToolSpec(window._REFERENCE_SCALE_TOOL, count=2)
    assert window.tool_snap(spec, QPointF(13.0, 7.0), False, ()) == [(0.65, 0.35)]
    assert not window._ghost_path([(0.0, 0.0), (1.0, 1.0)], window._REFERENCE_SCALE_TOOL).isEmpty()


def test_the_record_comes_back_with_the_project_and_a_corrupt_one_is_dropped(window, tmp_path):  # noqa: F811
    acquire(window)
    record = {**FILE_RECORD, "path": str(tmp_path / "ref.png")}
    window._set_reference_image(record)
    state = window._project_state()
    window._switch_project("ALTRO", window._general_project_state)
    assert window.reference_image is None
    window._switch_project(state["project_name"] if "project_name" in state else "SCHEDA", state)
    assert window.reference_image == record
    state["reference_image"] = {"source": "file", "path": "x", "mm_per_px_x": -1}
    window._switch_project("SCHEDA", state)
    assert window.reference_image is None
