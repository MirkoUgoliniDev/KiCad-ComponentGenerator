"""@brief Window fixes of the second review (2026-11-03): A3, A6, C6.

Each rule has a positive and a negative case that differ structurally.
"""
import pytest

pytest.importorskip("PyQt6")


def test_a_failed_start_gives_back_the_declared_row_spacing(window):
    """A3: the snapshot covers every attribute the start resets."""
    window._declared_row_spacing = 2.5
    snapshot = window._photo_state_snapshot()
    window._declared_row_spacing = None
    window._restore_photo_state(snapshot)
    assert window._declared_row_spacing == 2.5


def test_a_new_project_of_an_unknown_type_creates_no_folder(window, monkeypatch, tmp_path):
    """A6: the initial state is built before the folder; a failure leaves nothing behind."""
    from PyQt6.QtWidgets import QMessageBox
    monkeypatch.setattr(window, "_library_root", lambda: tmp_path)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    from PyQt6.QtWidgets import QDialog
    from kicadgen.gui import project_dialogs
    monkeypatch.setattr(window, "_run_modal", lambda dialog: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(project_dialogs.ProjectKindDialog, "selected_type", lambda self: "x")
    monkeypatch.setattr(project_dialogs.ProjectNameDialog, "project_name", lambda self: "NUOVO")
    left = []
    monkeypatch.setattr(window, "_can_leave_project", lambda: left.append(1) or True)
    monkeypatch.setattr(window, "_state_for_project_type",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("tipo assente")))
    window._new_project()
    assert not (tmp_path / "NUOVO").exists() and left == [], "nothing left, current project kept"


def test_a_3d_interruption_is_recognised_in_any_language(window):
    """C6: the cancel is told from a fault by the flag or the current-language text, not the Italian substring only."""
    from kicadgen.cad3d.step_generator import CAD_ABORT_MESSAGE
    from kicadgen.services.messages import tx
    assert window._is_cad_cancel(tx(CAD_ABORT_MESSAGE))
    assert window._is_cad_cancel("x " + CAD_ABORT_MESSAGE)
    assert not window._is_cad_cancel("FreeCAD: errore di importazione")


def test_a_relaunch_on_a_base_rebuilt_from_the_fields_keeps_the_matrix_fields(window, monkeypatch, tmp_path):
    """A2: a synthesized base has no populated pins and no body: the fields stay; a real analysis still sets them."""
    from ui_flow_helpers import header_extraction, prepare
    prepare(window, monkeypatch, tmp_path, "HDR")
    window.field_populated_pins.setText("1,2,5")
    window._body_size_declared = True
    window._relaunch_base_synthesized = True
    window._on_extracted(header_extraction("HDR", cols=4, body_length_mm=None, body_width_mm=None,
                                           body_height_mm=None))
    assert window.field_populated_pins.text() == "1,2,5" and window._body_size_declared is True
    window._relaunch_base_synthesized = False
    window._on_extracted(header_extraction("HDR", cols=4, populated_pins=[1, 3]))
    assert window.field_populated_pins.text() == "1,3"
