"""@brief The scale of the calibrated photo reaches the castellated 3D generator.

`cad3d.measurement_uncertainty` turns the photo scale (px/mm) into the
measurement uncertainty of the bodies (overhang tolerance, archive of the
accepted models). Without it the generator falls back to a declared default.
"""
import cv2
import numpy as np
import pytest

from test_photo_inventory_gui import gui
from kicadgen.cad3d import step_generator as step


def calibrated_png(width_px, height_px):
    ok, encoded = cv2.imencode('.png', np.zeros((height_px, width_px, 3), np.uint8))
    assert ok
    return encoded.tobytes()


def castellated_with_photo(window, width_px):
    window.group_castellated.setChecked(True)
    window.field_cast_body_length.setValue(40.0)
    window.field_cast_body_width.setValue(20.0)
    window._photo_body_fields = "cast"
    window._photo_png = calibrated_png(width_px, width_px // 2)
    window.group_3d.setChecked(True)


def _run_recording_errors(errors):
    """Il run vero, con gli errori del worker raccolti prima che parta."""
    original = gui.Cad3DWorker.run

    def run(worker):
        worker.error.connect(errors.append)
        original(worker)
    return run


def run_regeneration(window, monkeypatch, generator):
    started = []
    monkeypatch.setattr(gui.Cad3DWorker, "start", lambda self: started.append(self))
    monkeypatch.setattr(gui, "generate_castellated_3d_model", generator)
    monkeypatch.setattr(step, "_run_freecad", lambda *a, **k: pytest.fail("FreeCAD avviato"))
    window._regenerate_3d()
    assert len(started) == 1, window.label_3d_status.text()
    started[0].run()  # stessa chiamata del thread, qui sincrona
    return started[0]


def test_the_photo_scale_reaches_the_generator(window, monkeypatch, tmp_path):
    castellated_with_photo(window, 400)
    received = []

    def generator(params, body, output_dir, *, fillets=None, face_texts=None, progress=None,
                  model_progress=None, cancel=None, photo_scale_px_per_mm=None):
        received.append(photo_scale_px_per_mm)
        return {"step_path": tmp_path / "x.step"}

    worker = run_regeneration(window, monkeypatch, generator)
    assert worker.photo_scale_px_per_mm == pytest.approx(10.0)
    assert received == [pytest.approx(10.0)], "400 px su 40 mm di scheda"
    window._on_regen_3d_finished()


def test_no_photo_no_scale(window, monkeypatch, tmp_path):
    """Negativo: senza foto calibrata nessuna scala inventata."""
    castellated_with_photo(window, 400)
    window._photo_png = None
    received = []

    def generator(params, body, output_dir, *, photo_scale_px_per_mm="absent", **kwargs):
        received.append(photo_scale_px_per_mm)
        return {"step_path": tmp_path / "x.step"}

    run_regeneration(window, monkeypatch, generator)
    assert received == [None]
    window._on_regen_3d_finished()


def test_a_generator_without_the_keyword_is_not_broken(window, monkeypatch, tmp_path):
    """Negativo: la firma di prima non riceve la parola chiave (niente TypeError)."""
    castellated_with_photo(window, 400)
    calls = []

    def generator(params, body, output_dir, fillets=None, face_texts=None, progress=None,
                  model_progress=None, cancel=None):
        calls.append(True)
        return {"step_path": tmp_path / "x.step"}

    errors = []
    monkeypatch.setattr(gui.Cad3DWorker, "run", _run_recording_errors(errors))
    run_regeneration(window, monkeypatch, generator)
    assert calls == [True] and errors == []
    window._on_regen_3d_finished()
