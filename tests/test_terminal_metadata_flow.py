"""Regression coverage for row-owned terminal metadata and the STEP payload.

FreeCAD and AI boundaries are stubbed; table and payload handling are real.
"""
from copy import deepcopy
import json
import os
from types import SimpleNamespace

import pytest

from kicadgen.cad3d import step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.generators.castellated_module import (
    CastellatedModuleParams, CastellatedSide, ComponentBox,
)


@pytest.fixture
def geometry():
    # Deliberately asymmetric: loss, replacement or premature rotation is visible.
    return {
        "body_bbox": [0.2, 0.15, 0.8, 0.85],
        "terminals": [
            {"bbox": [0.0, 0.2, 0.2, 0.3]},
            {"bbox": [0.8, 0.55, 1.0, 0.7]},
        ],
        "confidence": "high",
    }


@pytest.fixture
def window(monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    pytest.importorskip("pyvistaqt")
    from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget
    from kicadgen.gui import main_window as gui

    class PreviewStub(QWidget):
        def fit_view(self): pass
        def clear_preview(self): pass
        def render_params(self, *args): pass
        def render_mesh(self, path): pass
        def set_pin_numbers(self, checked): pass

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(gui, "Preview3DVtk", PreviewStub)
    monkeypatch.setattr(gui, "load_config", lambda: {"component_type": "", "system_prompt": ""})
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    widget = gui.MainWindow()
    yield widget
    widget.close()
    app.processEvents()


def step_components(components, monkeypatch, tmp_path):
    captured = []

    def capture(*args, **kwargs):
        # Also exercise JSON compatibility at the subprocess boundary.
        captured.append(json.loads(json.dumps(kwargs["extra_payload"])))
        return {"warnings": []}

    monkeypatch.setattr(step_generator, "_run_freecad", capture)
    params = CastellatedModuleParams(
        name="TerminalMetadata", body_length=40, body_width=20,
        sides=[CastellatedSide(edge="left", count=2, pitch=2.54)],
        components=components,
    )
    body = Body3DParams(length_mm=40, width_mm=20, height_mm=5)
    step_generator.generate_castellated_3d_model(params, body, tmp_path)
    assert len(captured) == 1
    return captured[0]["components"]


def test_deleting_first_row_keeps_survivor_metadata_through_step(
    window, geometry, monkeypatch, tmp_path,
):
    removed_geometry = {
        "body_bbox": [0.1, 0.3, 0.9, 0.7],
        "terminals": [{"bbox": [0.2, 0.0, 0.3, 0.3]}],
        "confidence": "medium",
    }
    common = dict(kind="generic", y_mm=0, length_mm=4, width_mm=2, height_mm=1)
    entries = [
        dict(common, name="Removed", x_mm=-8, pin_count=1,
             pins_by_side={"top": 1}, _detected_kind="ic_qfn",
             terminal_geometry=removed_geometry),
        dict(common, name="Survivor", x_mm=4, pin_count=2,
             pins_by_side={"left": 1, "right": 1}, _detected_kind="ic_soic",
             terminal_geometry=deepcopy(geometry)),
    ]
    before = deepcopy(entries)
    window._set_photo_components(entries)
    window.table_components.setCurrentCell(0, 0)
    window._remove_component_row()
    window._add_component_row()
    window.table_components.cellWidget(0, 0).setText("Edited survivor")
    rotation = window.table_components.cellWidget(0, 7)
    rotation.setCurrentIndex(rotation.findData(90.0))

    survivor, added = window._components_from_table()
    assert survivor.name == "Edited survivor"
    assert survivor.rotation == 90
    assert survivor.pin_count == 2
    assert survivor.pins_by_side == {"left": 1, "right": 1}
    assert survivor.detected_kind == "ic_soic"
    assert survivor.terminal_geometry == geometry
    assert added.pin_count is None
    assert added.pins_by_side is None
    assert added.detected_kind is None
    assert added.terminal_geometry is None

    payload = step_components([survivor, added], monkeypatch, tmp_path)
    assert payload[0]["name"] == "Edited survivor"
    assert payload[0]["kind"] == "ic_soic"
    assert payload[0]["rotation_deg"] == 90
    assert payload[0]["terminal_geometry"] == geometry
    assert payload[0]["pin_count"] == 2
    assert payload[0]["pins_by_side"] == {"left": 1, "right": 1}
    assert payload[1].get("terminal_geometry") is None
    assert entries == before


def test_direct_row_addition_preserves_metadata_without_photo_dict(window, geometry):
    component = ComponentBox(
        x=0, y=0, length=4, width=2, height=1,
        pin_count=2, pins_by_side={"left": 1, "right": 1},
        detected_kind="ic_soic", terminal_geometry=deepcopy(geometry),
    )
    window.table_components.setRowCount(0)
    window._add_component_row(component)
    actual, = window._components_from_table()
    assert actual.pin_count == component.pin_count
    assert actual.pins_by_side == component.pins_by_side
    assert actual.detected_kind == component.detected_kind
    assert actual.terminal_geometry == geometry
    # Reading a row must not expose its nested metadata for accidental mutation.
    actual.terminal_geometry["terminals"][0]["bbox"][0] = 0.1
    reread, = window._components_from_table()
    assert reread.terminal_geometry == geometry
    assert component.terminal_geometry == geometry


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_step_payload_preserves_local_terminal_geometry(rotation, geometry, monkeypatch, tmp_path):
    component = ComponentBox(
        x=3, y=-2, length=4, width=2, height=1, rotation=rotation,
        kind="generic", detected_kind="ic_soic", terminal_geometry=deepcopy(geometry),
    )
    before = deepcopy(component)
    payload, = step_components([component], monkeypatch, tmp_path)
    assert payload["terminal_geometry"] == geometry
    assert payload["rotation_deg"] == rotation
    assert (payload["x"], payload["y"]) == (3, -2)
    assert (payload["length_mm"], payload["width_mm"]) == (4, 2)
    assert payload["kind"] == "ic_soic"
    assert component == before


@pytest.fixture
def terminal_snapshot(window, tmp_path):
    window._set_photo_components([
        dict(name="U1", kind="generic", _detected_kind="ic_soic",
             x_mm=-5, y_mm=2, length_mm=4, width_mm=2, height_mm=1,
             rotation_deg=90),
        dict(name="R1", kind="resistor", x_mm=5, y_mm=-3,
             length_mm=2, width_mm=1, height_mm=0.6, rotation_deg=180),
    ])
    window._photo_png = b"snapshot-photo"
    window._last_mesh_path = tmp_path / "previous-mesh.json"
    worker = SimpleNamespace(
        _model_revision=window._model_revision,
        _photo_snapshot=window._photo_png,
        components=deepcopy(window._photo_components_from_table()),
    )
    window._terminal_worker = worker
    yield worker
    window._terminal_worker = None


@pytest.mark.parametrize("change", ["revision", "row", "photo"])
def test_obsolete_terminal_result_does_not_overwrite_table_or_mesh(
    window, terminal_snapshot, geometry, monkeypatch, change,
):
    result = deepcopy(terminal_snapshot.components)
    result[0]["terminal_geometry"] = deepcopy(geometry)
    if change == "revision":
        window._model_revision += 1
    elif change == "row":
        window.table_components.cellWidget(0, 2).setValue(-7)
        # Exercise the row-snapshot guard independently of the revision guard.
        terminal_snapshot._model_revision = window._model_revision
    else:
        window._photo_png = b"replacement-photo"
    before = deepcopy(window._photo_components_from_table())
    previous_mesh = window._last_mesh_path
    previous_revision = window._model_revision
    cleared = []
    monkeypatch.setattr(window.preview_3d, "clear_preview", lambda: cleared.append(True))

    window._on_terminal_detail_done(result)

    assert window._photo_components_from_table() == before
    assert window._last_mesh_path == previous_mesh
    assert window._model_revision == previous_revision
    assert not cleared
    assert "scartato" in window.log_text.toPlainText()


def test_valid_terminal_result_preserves_geometry_then_invalidates_mesh(
    window, terminal_snapshot, geometry, monkeypatch,
):
    result = deepcopy(terminal_snapshot.components)
    result[0]["terminal_geometry"] = deepcopy(geometry)
    before = deepcopy(result)
    revision = window._model_revision
    cleared_with_components = []
    monkeypatch.setattr(
        window.preview_3d, "clear_preview",
        lambda: cleared_with_components.append(window._photo_components_from_table()),
    )

    window._on_terminal_detail_done(result)

    actual = window._photo_components_from_table()
    assert actual == before
    assert len(cleared_with_components) == 1
    assert cleared_with_components[0] == actual  # New data was applied before clearing.
    assert window._last_mesh_path is None
    assert window._model_revision == revision + 1
    assert "1 componenti" in window.label_3d_status.text()
    assert "Rigenera modello 3D" in window.label_3d_status.text()
    assert result == before


@pytest.mark.parametrize("fails", [False, True])
def test_terminal_worker_run_uses_snapshot_and_emits_result_or_error(
    window, geometry, monkeypatch, tmp_path, fails,
):
    from kicadgen.gui.main_window import TerminalDetailWorker
    from kicadgen.services import openrouter, photo_terminals

    source = [dict(name="U1", x_mm=2, terminal_geometry=deepcopy(geometry))]
    expected_snapshot = deepcopy(source)
    calls, observations, done, errors, progress = [], [], [], [], []

    def fake_call(**kwargs):
        calls.append(kwargs)
        if fails:
            raise RuntimeError("simulated AI failure")
        return {"components": deepcopy(expected_snapshot)}

    def fake_observe(photo, components, model, call, log, cache_dir):
        observations.append((photo, deepcopy(components), model, cache_dir))
        log("mock terminal progress")
        response = call(model=model)
        return response["components"]

    monkeypatch.setattr(openrouter, "call_tool", fake_call)
    monkeypatch.setattr(photo_terminals, "observe_terminals", fake_observe)
    worker = TerminalDetailWorker(b"photo-bytes", source, "mock-model")
    source[0]["terminal_geometry"]["body_bbox"][0] = 0.05
    source[0]["name"] = "Changed after launch"
    worker.done.connect(done.append)
    worker.error.connect(errors.append)
    worker.progress.connect(progress.append)

    worker.run()  # Synchronous signal verification: no network or thread scheduling.

    assert len(observations) == 1
    assert observations[0][:3] == (b"photo-bytes", expected_snapshot, "mock-model")
    assert calls == [{"model": "mock-model"}]
    assert progress == ["mock terminal progress"]
    assert worker.components == expected_snapshot
    if fails:
        assert not done
        assert len(errors) == 1
        assert "simulated AI failure" in errors[0]
    else:
        assert done == [expected_snapshot]
        assert not errors


def test_close_event_waits_for_terminal_worker_then_accepts(window):
    from PyQt6.QtGui import QCloseEvent

    state = SimpleNamespace(running=True)
    worker = SimpleNamespace(isRunning=lambda: state.running)
    window._terminal_worker = worker
    try:
        active_event = QCloseEvent()
        active_event.accept()
        window.closeEvent(active_event)
        assert not active_event.isAccepted()
        assert window._terminal_worker is worker
        assert "Attendi" in window.label_3d_status.text()

        state.running = False
        finished_event = QCloseEvent()
        finished_event.ignore()
        window.closeEvent(finished_event)
        assert finished_event.isAccepted()
    finally:
        window._terminal_worker = None


@pytest.mark.parametrize("detected_kind", ["ic_soic", "ic_qfn"])
def test_step_uses_generic_ic_for_photo_hint_without_terminal_geometry(
    detected_kind, geometry, monkeypatch, tmp_path,
):
    components = [
        ComponentBox(
            name="Photo hint only", x=-10, y=0, length=4, width=2, height=1,
            kind="generic", detected_kind=detected_kind,
        ),
        ComponentBox(
            name="Manual package", x=0, y=0, length=4, width=2, height=1,
            kind=detected_kind,
        ),
        ComponentBox(
            name="Observed terminals", x=10, y=0, length=4, width=2, height=1,
            kind="generic", detected_kind=detected_kind,
            terminal_geometry=deepcopy(geometry),
        ),
    ]
    before = deepcopy(components)

    hinted, manual, observed = step_components(components, monkeypatch, tmp_path)

    assert hinted["kind"] == "ic"
    assert hinted.get("terminal_geometry") is None
    assert hinted.get("pin_count") is None
    assert hinted.get("pins_by_side") is None
    assert manual["kind"] == detected_kind
    assert observed["kind"] == detected_kind
    assert observed["terminal_geometry"] == geometry
    assert components == before
