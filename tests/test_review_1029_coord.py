"""@brief Coordinator fixes of the 2026-10-29 review that span two scopes.

P3: the default YOLO weights of the «YOLO + rete SMD» (YOLO + SMD network)
route are chosen in ONE place, app_paths.default_yolo_smd_model(); the
document extraction used to force the PyTorch checkpoint, so the installed
program (ONNX export only) refused the run before any photo was read.
"""

from pathlib import Path

import pytest

from kicadgen import app_paths
from kicadgen.services import datasheet_extractor as de
from kicadgen.services import photo_yolo


@pytest.fixture
def model_folders(monkeypatch, tmp_path):
    """Point the program's resources at an empty temporary tree."""
    monkeypatch.setattr(app_paths, "resource_path", lambda *parts: tmp_path.joinpath(*parts))
    folder = tmp_path / "models" / "yolo"
    folder.mkdir(parents=True)
    return folder


def _weights_checked_by_the_extraction(monkeypatch, tmp_path) -> str:
    """Run the extraction up to the YOLO setup check and return the file it checked."""
    seen = []

    def validate(path):
        seen.append(Path(path).name)
        raise ValueError("stop here: no detector in this test")
    monkeypatch.setattr(photo_yolo, "validate_yolo_setup", validate)
    document = tmp_path / "board.png"
    document.write_bytes(b"x")
    with pytest.raises(de.ExtractionError):
        de.extract_pin_header_params([document], photo_detector="yolo_smd")
    assert len(seen) == 1
    return seen[0]


def test_p3_the_extraction_uses_the_onnx_export_when_the_checkpoint_is_missing(model_folders, monkeypatch,
                                                                              tmp_path):
    """@brief Positive: export only (installed program) -> the export is checked, not a missing .pt."""
    (model_folders / app_paths.DEFAULT_YOLO_SMD_ONNX).write_bytes(b"x")
    assert _weights_checked_by_the_extraction(monkeypatch, tmp_path) == app_paths.DEFAULT_YOLO_SMD_ONNX


def test_p3_the_extraction_keeps_the_checkpoint_when_it_is_there(model_folders, monkeypatch, tmp_path):
    """@brief Negative: with the checkpoint present the route keeps the weights it was built on."""
    (model_folders / app_paths.DEFAULT_YOLO_SMD_ONNX).write_bytes(b"x")
    (model_folders / app_paths.DEFAULT_YOLO_PT).write_bytes(b"x")
    assert _weights_checked_by_the_extraction(monkeypatch, tmp_path) == app_paths.DEFAULT_YOLO_PT


# ── H5: the local classifier shares one library listing per batch ──────────

def _smd_rows(count):
    """Accepted classifier rows with a size, as _attach_kicad_results receives them."""
    return [{"accepted": True, "size_mm": (2.0, 1.0), "kind": "resistor", "package": "0805",
             "rotation_deg": 0.0, "class": "chip_R", "class_confidence": 0.9}
            for _ in range(count)]


def _record_attach(monkeypatch, rows, cancel=None):
    """Run _attach_kicad_results with fake matching; return the scope depth seen by each call."""
    import contextlib
    from kicadgen.cad3d import kicad_models
    from kicadgen.services import smd_component_classifier as smd
    depth = [0]
    seen = {"match": [], "sizes": []}

    @contextlib.contextmanager
    def scope():
        depth[0] += 1
        try:
            yield
        finally:
            depth[0] -= 1

    def candidates(component, *, library=None, log=None, cancel=None, limit=5):
        seen["match"].append(depth[0])
        return {"status": "matched", "candidates": [{"path": "/lib/R.3dshapes/R_0805.step"}],
                "selected_path": "/lib/R.3dshapes/R_0805.step"}

    def sizes(paths, library=None, log=None, *, cancel=None):
        seen["sizes"].append((depth[0], cancel))
        return {}
    monkeypatch.setattr(kicad_models, "listing_scope", scope)
    monkeypatch.setattr(kicad_models, "model_candidates", candidates)
    monkeypatch.setattr(kicad_models, "footprint_sizes", sizes)
    smd._attach_kicad_results(rows, None, None, cancel)
    return seen


def test_h5_every_body_of_a_batch_is_matched_inside_one_listing_scope(monkeypatch):
    """@brief Positive: three bodies -> three lookups and the link reading all inside the shared scope."""
    import threading
    cancel = threading.Event()
    seen = _record_attach(monkeypatch, _smd_rows(3), cancel)
    assert seen["match"] == [1, 1, 1]
    # The cancel channel reaches the footprint reading, so a cold index rebuild can stop.
    assert seen["sizes"] == [(1, cancel)]


def test_h5_a_batch_without_accepted_bodies_opens_no_scope(monkeypatch):
    """@brief Negative: nothing to match -> no library walk is opened at all."""
    rows = _smd_rows(2)
    for row in rows:
        row["accepted"] = False
    seen = _record_attach(monkeypatch, rows)
    assert seen == {"match": [], "sizes": []}
