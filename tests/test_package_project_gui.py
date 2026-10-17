"""Explicit package geometry survives projects and never becomes a synthetic 3D header."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import re
from unittest.mock import Mock

import pytest
from PyQt6.QtWidgets import QDialog

from kicadgen.core.package_geometry import PackageGeometry
from kicadgen.generators.package import build_package
from kicadgen.gui import main_window as gui
from kicadgen.gui.project_dialogs import ProjectNameDialog
from kicadgen.gui.project_state import (
    ProjectConfigError, read_project_config, write_project_config,
)
from kicadgen.services.datasheet_extractor import ExtractedParams
from kicadgen.services.document_roles import PHOTO_ROLE


def extracted_package():
    return ExtractedParams(
        name="SOIC", description="", tags="", confidence="high", notes="",
        body_length_mm=5, body_width_mm=4, body_height_mm=1,
        package_geometry={
            "family": "soic", "body_length_mm": 5, "body_width_mm": 4,
            "body_height_mm": 1, "origin": "pin1", "source_view": "bottom",
            "orientation_deg": 30, "mirrored": True,
            "confidence": "high", "needs_review": False,
            "pins": [
                {"number": "1", "name": "IN", "x_mm": 0, "y_mm": 0,
                 "pad_length_mm": 1.2, "pad_width_mm": .6, "side": "left",
                 "shape": "oval", "rotation_deg": 20},
                {"number": "2", "name": "OUT", "x_mm": 4, "y_mm": 0,
                 "pad_length_mm": 1.4, "pad_width_mm": .8, "side": "right",
                 "shape": "rect", "pad_type": "thru_hole", "drill_mm": .4},
            ],
        },
    )


def acquire(window):
    window._freecad_available = True
    window._on_extracted(extracted_package())
    assert isinstance(window.package_geometry, PackageGeometry)


def test_extracted_package_reopens_with_identical_pads_and_footprint(window):
    acquire(window)
    expected = asdict(window.package_geometry)
    folder = window._project_dir()
    worker = gui.GenerateWorker(window.package_geometry, folder, None)
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert not errors
    footprint = folder / f"{folder.name}.pretty" / f"{folder.name}.kicad_mod"
    original = footprint.read_text(encoding="utf-8").strip()
    assert window._save_project_config(force=True)

    window._switch_project("Another", window._general_project_state)
    assert window.package_geometry is None
    window._open_project_at(folder)

    restored = window._preview_params()
    assert isinstance(restored, PackageGeometry)
    assert asdict(restored) == expected
    # KiCad item UUIDs are regenerated; all geometry and metadata must match.
    def without_uuids(text):
        return re.sub(r'\(uuid "[^"]+"\)', '(uuid)', text)
    assert without_uuids(build_package(restored).render().strip()) == without_uuids(original)
    window._draw_preview()
    assert window.preview_scene.items()


def test_legacy_project_without_package_keeps_matrix_support(window):
    acquire(window)
    legacy = deepcopy(window._general_project_state)
    legacy.pop("package_geometry", None)
    # CONTRACT CHANGED 2026-10-15 (audit item 4): «Rigenera 3D» needs
    # documented body dimensions; the startup state only holds the
    # 10 x 10 x 5 mm placeholder. A legacy project (no `body_size_declared`
    # key) whose analysis wrote its body keeps the matrix 3D available.
    legacy.pop("body_size_declared", None)
    legacy["widgets"] = dict(legacy.get("widgets") or {}, field_body_length=12.0,
                             field_body_width=6.0, field_body_height=4.0)
    footprint = window._library_root() / "Legacy" / "Legacy.pretty" / "Legacy.kicad_mod"
    footprint.parent.mkdir(parents=True)
    footprint.write_text("(footprint)")
    window._switch_project("Legacy", legacy)
    window.group_3d.setChecked(True)
    window._refresh_output_actions()
    assert window.package_geometry is None
    assert isinstance(window._preview_params(), gui.PinHeaderParams)
    assert window.btn_regen_3d.isEnabled()
    # Negative: the same legacy project with the placeholder body only.
    legacy["widgets"].update(field_body_length=10.0, field_body_width=10.0,
                             field_body_height=5.0)
    window._apply_project_state(legacy)
    window._refresh_output_actions()
    assert not window.btn_regen_3d.isEnabled()


def test_renamed_package_reopens_under_the_current_project_name(window, monkeypatch):
    acquire(window)
    assert window._save_project_config(force=True)
    monkeypatch.setattr(ProjectNameDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ProjectNameDialog, "project_name", lambda self: "Renamed")
    window._rename_project()
    assert window._component_name() == "Renamed"
    assert window._save_project_config()
    folder = window._project_dir()
    assert read_project_config(folder)["package_geometry"]["name"] == "Renamed"
    # Also cover a saved file copied/renamed outside the application: its
    # embedded old name must not override the project folder on reopening.
    state = read_project_config(folder)
    state["package_geometry"]["name"] = "OldName"
    write_project_config(folder, state)
    window._switch_project("Another", window._general_project_state)
    window._open_project_at(folder)
    assert window.package_geometry.name == "Renamed"
    assert window._preview_params().name == "Renamed"


@pytest.mark.parametrize("invalid", [1, {"pins": [None]}, {"pins": []},
                                     {"family": "soic", "pins": [{"number": "1"}]}])
def test_invalid_saved_package_is_refused_before_application(tmp_path, invalid):
    write_project_config(tmp_path, {"package_geometry": invalid})
    with pytest.raises(ProjectConfigError, match="package_geometry"):
        read_project_config(tmp_path)


def test_invalid_package_file_is_not_rewritten_by_autosave(window, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Yes)
    folder = window._library_root() / "InvalidPackage"
    path = write_project_config(folder, {"package_geometry": {"pins": []}})
    original = path.read_bytes()
    window._open_project_at(folder)
    window.field_description.setText("Changed")
    assert not window._save_project_config(force=True)
    assert path.read_bytes() == original


def test_package_regeneration_and_preview_never_use_matrix(window, monkeypatch):
    """@brief A package with its body quotes regenerates through ITS geometry, never the pin grid.

    Until 2026-10-09 a package had no 3D path at all and the button stayed off;
    now `generate_single_package_3d_model` builds it, so the contract is: the
    worker receives the PackageGeometry, and the matrix preview is never drawn.
    """
    acquire(window)
    started = []

    class FakeWorker:
        def __init__(self, params, body, project_dir, fillets, face_texts, **options):
            started.append((params, body, options))
            self._model_revision = None
            self.photo_scale_px_per_mm = None
        def __getattr__(self, name):
            return Mock()
        def start(self):
            pass

    monkeypatch.setattr(gui, "Cad3DWorker", FakeWorker)
    render = Mock(side_effect=AssertionError("unexpected matrix preview"))
    monkeypatch.setattr(window.preview_3d, "render_params", render)
    clear = Mock()
    monkeypatch.setattr(window.preview_3d, "clear_preview", clear)

    assert window.btn_regen_3d.isEnabled(), "the package has its quotes: a 3D can be built"
    window._regenerate_3d()
    # The package path is chosen by the type of the params (PackageGeometry),
    # whatever the project kind says: that is what keeps the grid out.
    assert started and isinstance(started[0][0], PackageGeometry)
    assert started[0][1].length_mm == 5 and started[0][1].width_mm == 4
    window._cad3d_worker = None
    window._enter_3d_tab()
    assert clear.called
    render.assert_not_called()


def test_package_can_still_display_an_existing_mesh(window, monkeypatch, tmp_path):
    acquire(window)
    path = tmp_path / "existing.preview.json"
    path.write_text("{}")
    window._last_mesh_path = path
    render = Mock()
    monkeypatch.setattr(window.preview_3d, "render_mesh", render)
    window._draw_preview_3d()
    render.assert_called_once_with(path)


@pytest.mark.parametrize("roles", [None, {"photo.GIF": [PHOTO_ROLE]}])
def test_pretest_accepts_imported_gif_photographs(roles):
    photo = Path("photo.GIF")
    assert gui.MainWindow._pretest_top_views([photo], roles) == [photo]
