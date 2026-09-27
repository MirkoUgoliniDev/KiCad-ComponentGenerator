"""@brief Offline regressions for populated PCB mode and GUI edit invalidation."""
from copy import deepcopy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget

from kicadgen.gui import main_window as gui
from kicadgen.cad3d import step_generator as step, kicad_models
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.generators.pin_header import PinHeaderParams
from kicadgen.services import openrouter
from kicadgen.services.analysis_profiles import ProfileStore
from kicadgen.services.datasheet_extractor import _components_3d


class PreviewStub(QWidget):
    def clear_preview(self):
        pass

    def render_mesh(self, *args):
        pass

    def render_params(self, *args):
        pass

    def fit_view(self):
        pass

    def set_pin_numbers(self, *args):
        pass


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Board lifecycle tests must not call network or FreeCAD")
    monkeypatch.setattr(openrouter, "call_tool", forbidden)
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(step, "_run_freecad", forbidden)
    monkeypatch.setattr(kicad_models, "attach_models", Mock(return_value=0))


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    # Una sola QApplication per processo (`qapp`, tests/qt_fixtures.py): con il
    # solo riferimento locale veniva distrutta a fine test e il file moriva
    # ogni tanto di segmentation fault a meta' corsa.
    app = qapp
    monkeypatch.setattr(gui, "Preview3DVtk", PreviewStub)
    monkeypatch.setattr(gui, "load_config", lambda: {"component_type": "", "language": "en"})
    monkeypatch.setattr(gui, "save_config", lambda *args: None)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path / "output")
    # The parent may be running real analysis: never share its profile DB/logs.
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    w = gui.MainWindow(profile_store=store)
    # Il campo del nome nasce vuoto e senza nome non si genera nulla: questi
    # test riguardano il ciclo di vita della scheda, quindi partono come chi ha
    # gia' scritto il nome.
    w.field_name.setText("Componente_di_prova")
    yield w
    w.close()
    app.processEvents()


@pytest.fixture
def photo_components():
    item = dict(name="Visible body", kind="generic", x_mm=-2., y_mm=1.,
                length_mm=4., width_mm=2., height_mm=1., rotation_deg=0.,
                package="MechanicalPackage", mating="none", bbox_basis="pads",
                package_evidence="Box follows the PCB pad edges", _box=[.4, .4, .6, .6],
                terminal_geometry=dict(body_bbox=[.2, .2, .8, .8], terminals=[]),
                provenance={"view": ["detail"]})
    return [item, dict(deepcopy(item), name="Second body", x_mm=8.)]


def arm_mesh(window, monkeypatch):
    window._last_mesh_path = Path("previous.preview.json")
    revision = window._model_revision
    clear = Mock()
    monkeypatch.setattr(window.preview_3d, "clear_preview", clear)
    return revision, clear


def assert_invalidated(window, revision, clear):
    assert window._model_revision > revision
    assert window._last_mesh_path is None
    assert clear.call_count >= 1


@pytest.mark.parametrize("components", [None, []])
def test_empty_aggregate_retains_substrate_drill_and_explicit_thickness(monkeypatch, tmp_path, components):
    params = PinHeaderParams(rows=2, cols=3, pitch=2., row_spacing=10., drill=1.,
                             pad_diameter=1.8, populated_pins=[1, 4, 6])
    body = Body3DParams(length_mm=12., width_mm=16., height_mm=8.,
                        offset_x_mm=-3., offset_y_mm=-2., pcb_thickness_mm=1.,
                        populated_board=True, components=components)
    run = Mock(return_value={})
    monkeypatch.setattr(step, "_run_freecad", run)
    step.generate_3d_model(params, body, tmp_path)
    args, payload = run.call_args
    assert args[1] == [[1, 0., 0.], [4, 2., 10.], [6, 4., 10.]]
    assert args[2].height_mm == 1. and body.height_mm == 8.
    assert (args[2].offset_x_mm, args[2].offset_y_mm) == (-3., -2.)
    assert payload["extra_payload"]["substrate"]
    assert payload["extra_payload"]["components"] == []
    assert payload["notches"] == [dict(x=0., y=0., radius=.5),
                                  dict(x=2., y=10., radius=.5), dict(x=4., y=10., radius=.5)]
    assert [c["number"] for c in payload["edge_contacts"]] == ["1", "4", "6"]
    assert all(c["edge"] == "through" and c["pad_width_mm"] == 1.8
               for c in payload["edge_contacts"])


def test_plain_header_still_uses_solid_body(monkeypatch, tmp_path):
    body = Body3DParams(length_mm=12., width_mm=16., height_mm=8.,
                        pcb_thickness_mm=1., components=[])
    run = Mock(return_value={})
    monkeypatch.setattr(step, "_run_freecad", run)
    step.generate_3d_model(PinHeaderParams(), body, tmp_path)
    assert run.call_args.args[2].height_mm == 8.
    assert "notches" not in run.call_args.kwargs
    assert "extra_payload" not in run.call_args.kwargs


def test_gui_family_keeps_pcb_mode_after_last_component_removed(window, photo_components, monkeypatch, tmp_path):
    index = window.combo_component_type.findData("aggregate_tht")
    assert index >= 0
    window.combo_component_type.setCurrentIndex(index)
    window.group_3d.setChecked(True)
    # The board's 3D uses the thickness shown on the Component page (point 132).
    window.field_cast_pcb.setValue(1.2)
    window._set_photo_components(photo_components[:1])
    populated = window._current_body_params()
    window._set_photo_components([])  # l'ultimo corpo tolto dalla revisione
    empty = window._current_body_params()
    assert populated.populated_board and empty.populated_board
    assert empty.components == [] and empty.pcb_thickness_mm == 1.2
    run = Mock(return_value={})
    monkeypatch.setattr(step, "_run_freecad", run)
    step.generate_3d_model(window._current_params(), empty, tmp_path)
    assert run.call_args.args[2].height_mm == 1.2
    assert run.call_args.kwargs["extra_payload"]["substrate"]
    assert run.call_args.kwargs["notches"]


@pytest.mark.parametrize("field", ["field_body_length", "field_body_width", "field_body_height",
                                   "field_body_offset_x", "field_body_offset_y", "field_drill"])
def test_body_and_hole_edits_invalidate_mesh(window, monkeypatch, field):
    revision, clear = arm_mesh(window, monkeypatch)
    widget = getattr(window, field)
    widget.setValue(widget.value() + .1)
    assert_invalidated(window, revision, clear)


def test_explicit_mating_none_survives_decode_table_and_cad_payload(window, photo_components, monkeypatch, tmp_path):
    decoded = _components_3d(photo_components[:1])
    assert decoded[0]["mating"] == "none"
    window._set_photo_components(decoded)
    table = window._components_from_table()
    assert table[0].mating == "none"
    assert window._photo_components_from_table()[0]["mating"] == "none"
    run = Mock(return_value={})
    monkeypatch.setattr(step, "_run_freecad", run)
    body = Body3DParams(length_mm=40, width_mm=20, height_mm=5,
                        populated_board=True, components=table)
    step.generate_3d_model(PinHeaderParams(), body, tmp_path)
    component = run.call_args.kwargs["extra_payload"]["components"][0]
    assert component["mating"] == "none" and component["package"] == decoded[0]["package"]
    assert component["bbox_basis"] == "pads"
    assert component["package_evidence"] == decoded[0]["package_evidence"]


def _castellated_sides(window):
    """@brief Modulo castellated con i due lati predefiniti."""
    window.group_castellated.setChecked(True)
    assert len(window._sides) == 2
    return window._sides


def test_contact_style_from_the_3d_tab_invalidates_mesh_and_worker_revision(window, monkeypatch):
    """@brief Positivo: il tipo di contatto cambia il solido, il vecchio 3D decade.

    Correggerlo e rigenerare e' il percorso gratuito, e un'anteprima rimasta a
    video lo farebbe sembrare gia' fatto. Un risultato CAD marchiato con la
    revisione vecchia viene rifiutato.
    """
    sides = _castellated_sides(window)
    revision, clear = arm_mesh(window, monkeypatch)
    combo = window.combo_contact_style_3d
    target = next(combo.itemData(i) for i in range(3)
                  if combo.itemData(i) != sides[0]["contact_style"])
    combo.setCurrentIndex(combo.findData(target))
    assert [side["contact_style"] for side in sides] == [target, target]
    assert_invalidated(window, revision, clear)
    monkeypatch.setattr(window, "sender", lambda: SimpleNamespace(_model_revision=revision))
    assert not window._cad_result_is_current()


@pytest.mark.parametrize("field", ["field_cast_body_length", "field_cast_hole",
                                   "field_cast_thru_drill"])
def test_castellated_parameter_changes_invalidate_mesh(window, monkeypatch, field):
    _castellated_sides(window)
    revision, clear = arm_mesh(window, monkeypatch)
    value = getattr(window, field)
    value.setValue(value.value() + .5)
    assert_invalidated(window, revision, clear)


def test_unchanged_side_style_keeps_the_mesh(window, monkeypatch):
    """@brief Negativo: ripetere la scelta gia' fatta non e' una modifica."""
    _castellated_sides(window)
    revision, clear = arm_mesh(window, monkeypatch)
    combo = window.combo_contact_style_3d
    combo.setCurrentIndex(combo.currentIndex())
    assert window._model_revision == revision and clear.call_count == 0
    assert window._last_mesh_path == Path("previous.preview.json")

