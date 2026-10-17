"""@brief Helpers that drive the real «Estrai parametri con AI» path of the main window in tests.

The paid worker is never started: the test hands the window the result the
worker would emit (`_on_extracted`) and its `finished` signal
(`_on_extract_finished`), exactly what the signals do, so the automatic
generation, the overwrite question and every question asked while applying
the result run as in the application.
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QListWidgetItem, QMessageBox

from kicadgen.core.package_geometry import package_doubts, package_from_mapping
from kicadgen.gui import main_window as gui
from kicadgen.services.datasheet_extractor import ExtractedParams


def header_extraction(name="HDR", cols=4, **overrides):
    """@brief A 2 x `cols` pin-matrix analysis result with a documented body (20 x 10 x 3 mm).

    @param name Component name proposed by the analysis.
    @param cols Columns of the matrix.
    @param overrides ExtractedParams fields to replace (e.g. body_length_mm=None).
    @return ExtractedParams.
    """
    values = dict(name=name, description="Header test", tags="hdr", confidence="high", notes="n",
                  layout="matrix", rows=2, cols=cols, pitch_mm=2.54, row_spacing_mm=7.62,
                  pad_diameter_mm=1.7, drill_mm=1.0, body_length_mm=20, body_width_mm=10,
                  body_height_mm=3)
    values.update(overrides)
    return ExtractedParams(**values)


def soic_geometry(length=3.9, width=4.9, *, names=("",) * 8, view="top", mirrored=False,
                  clockwise=False, lead_form=None):
    """@brief An SOIC-8 package geometry mapping: rows along Y at x = ±2.7 mm, pitch 1.27 mm.

    @param length body_length_mm (along X, across the rows: 3.9 is right).
    @param width body_width_mm (along Y, along the rows: 4.9 is right).
    @param names Pad names 1..8 ('' = none).
    @param view source_view.
    @param mirrored mirrored flag.
    @param clockwise True numbers the pads clockwise seen from the top.
    @param lead_form Declared lead form ("gull_wing", "j_lead"), None = not declared.
    @return Mapping accepted by `package_from_mapping`.
    """
    pins = []
    for index in range(8):
        left = index < 4
        step = index if left else index - 4
        y = (-1.905 + 1.27 * step) if left else (1.905 - 1.27 * step)
        pins.append({"number": str(index + 1), "name": names[index], "x_mm": -2.7 if left else 2.7,
                     "y_mm": -y if clockwise else y, "pad_length_mm": 1.55, "pad_width_mm": 0.6,
                     "side": "left" if left else "right", "shape": "rect", "pad_type": "smd"})
    extra = {} if lead_form is None else {"lead_form": lead_form}
    return {**extra, "name": "SOIC", "family": "soic", "body_length_mm": length, "body_width_mm": width,
            "body_height_mm": 1.75, "origin": "body_center", "source_view": view,
            "orientation_deg": 0, "mirrored": mirrored, "confidence": "high",
            "needs_review": False, "pins": pins}


def package_extraction(geometry, *, name="SOIC", pinout=None):
    """@brief A package analysis result carrying the geometry and its structural doubts.

    @param geometry Mapping from `soic_geometry`.
    @param name Component name proposed by the analysis.
    @param pinout Explicit pinout table, or None.
    @return ExtractedParams with `geometry_doubts` computed as the extractor does.
    """
    doubts = [d.as_dict() for d in package_doubts(package_from_mapping(dict(geometry)))] or None
    return ExtractedParams(name=name, description="Package test", tags="soic", confidence="high",
                           notes="", body_length_mm=geometry["body_length_mm"],
                           body_width_mm=geometry["body_width_mm"], body_height_mm=1.75,
                           pinout=pinout, package_geometry=geometry, geometry_doubts=doubts)


class _Asked(list):
    """A list that can also carry the queued answers."""


def prepare(window, monkeypatch, tmp_path, name):
    """@brief Open a fresh project `name` in `tmp_path`, with generation run synchronously.

    @return A list the static questions (overwrite...) append their text to;
        answers queued in its `answers` attribute are used first, then Yes.
    """
    asked = _Asked()
    asked.answers = []

    def question(*args, **_kwargs):
        asked.append(args[2] if len(args) > 2 else "")
        return asked.answers.pop(0) if asked.answers else QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", question)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda w: (w.run(), w.finished.emit()))
    window.field_name.setText("")
    window.field_library_root.setText(str(tmp_path))
    window._switch_project(name, window._general_project_state)
    return asked


def run_extraction(window, params):
    """@brief Run «Estrai parametri con AI» with `params` as the worker's result."""
    window.combo_photo_detector.setCurrentIndex(window.combo_photo_detector.findData("ai"))
    if not window.docs_list.count():
        row = QListWidgetItem("photo.png")
        row.setCheckState(Qt.CheckState.Checked)
        window.docs_list.addItem(row)
    window._extract_with_ai()
    window._freecad_available = False
    window._on_extracted(params)
    window._on_extract_finished()
