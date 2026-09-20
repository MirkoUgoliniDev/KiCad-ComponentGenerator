"""Verifica Qt del confronto e del log; il renderer VTK non e' coinvolto."""
from copy import deepcopy
import os
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtCore import Qt, Q_ARG, QMetaObject
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication, QWidget, QDialog, QLabel, QMessageBox, QListWidgetItem
from kicadgen.gui import main_window as gui
from kicadgen.services.datasheet_extractor import ExtractedParams
from kicadgen.services.photo_inventory import draw_inventory
import numpy as np


class PreviewStub(QWidget):
    def fit_view(self): pass
    def clear_preview(self): pass
    def render_params(self, *args): pass
    def render_mesh(self, path): pass
    def set_pin_numbers(self, checked): pass


@pytest.fixture
def window(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(gui, "Preview3DVtk", PreviewStub)
    monkeypatch.setattr(gui, "load_config", lambda: {"component_type": "", "system_prompt": ""})
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
    w = gui.MainWindow()
    # Geometry/lifecycle tests inject their document selection independently of
    # the guided uploader; uploader integration has dedicated tests.
    def selected_documents():
        paths = [w._docs_dir() / w.docs_list.item(i).text() for i in range(w.docs_list.count())
                 if w.docs_list.item(i).checkState() == Qt.CheckState.Checked]
        if not paths:
            raise ValueError('Nessun documento nel test.')
        return paths, None
    monkeypatch.setattr(w, '_selected_documents', selected_documents)
    yield w
    w.close()
    app.processEvents()


def test_progress_is_visible_on_project_tab_and_literal(window):
    window._on_ai_progress("Crop 1: <b>R1</b> & C1")
    assert "<b>R1</b> & C1" in window.ai_progress_text.toPlainText()
    assert "<b>R1</b> & C1" in window.log_text.toPlainText()
    assert window.analysis_overlay.status.text() == "Crop 1: <b>R1</b> & C1"


def test_extraction_action_is_centered_below_separate_tools(window):
    assert window.group_component_analysis.isAncestorOf(window.combo_component_type)
    assert window.group_component_analysis.isAncestorOf(window.group_ai_tools)
    assert window.group_component_analysis.isAncestorOf(window.btn_extract_ai)
    assert not window.group_component_analysis.isAncestorOf(window.docs_list)
    assert window.group_ai_tools.isAncestorOf(window.combo_model)
    assert window.group_ai_tools.isAncestorOf(window.btn_pick_model)
    assert not hasattr(window, 'combo_vision_model')
    assert not hasattr(window, 'combo_verification_model')
    assert not window.group_ai_tools.isAncestorOf(window.combo_component_type)
    assert window.group_ai_tools.isAncestorOf(window.btn_ai_log)
    assert window.group_ai_tools.isAncestorOf(window.btn_component_prompt)
    assert window.group_ai_tools.isAncestorOf(window.btn_photo_comparison)
    assert window.btn_photo_comparison.text() == 'Rivedi foto / scegli modelli KiCad…'
    assert not window.btn_photo_comparison.isHidden()
    assert 'Non avvia la generazione' in window.btn_photo_comparison.toolTip()
    tools_layout = window.group_ai_tools.layout()
    assert tools_layout.contentsMargins().bottom() >= 12
    assert tools_layout.itemAt(1).layout().spacing() >= 16
    assert not window.group_ai_tools.isAncestorOf(window.btn_extract_ai)
    layout = window.btn_extract_ai.parentWidget().layout()
    tools_index = layout.indexOf(window.group_ai_tools)
    action_index = layout.indexOf(window.btn_extract_ai)
    assert tools_index < action_index
    assert layout.itemAt(action_index).alignment() & Qt.AlignmentFlag.AlignHCenter
    assert not window.btn_extract_ai.icon().isNull()
    assert window.btn_extract_ai.minimumHeight() >= 44
    window._on_extract_finished()
    assert window.btn_extract_ai.text() == "Estrai parametri con AI"


@pytest.mark.parametrize("outcome", ["finished", "error", "done"])
def test_analysis_overlay_closes_on_all_worker_outcomes(window, monkeypatch, outcome):
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    window._analysis_tabs_enabled = window.tabs.isEnabled()
    window.tabs.setEnabled(False)
    window.analysis_overlay.start()
    assert window.analysis_overlay.timer.isActive()
    if outcome == "error":
        window._on_extract_error("Errore simulato")
    elif outcome == "done":
        params = ExtractedParams(name="", description="", tags="", confidence="low", notes="",
                                 layout="castellated", castellated_sides=[])
        window._on_extracted(params)
    else:
        window._on_extract_finished()
    assert window.analysis_overlay.isHidden()
    assert not window.analysis_overlay.timer.isActive()
    assert window.tabs.isEnabled()
    window._on_extract_finished()
    assert window.btn_extract_ai.isEnabled()


def test_startup_size_fits_available_screen(window):
    available = window.screen().availableGeometry()
    assert window.width() == min(1390, max(1, available.width() - 40))
    assert window.height() == min(1050, max(1, available.height() - 40))


def test_startup_dependency_warning_is_localized_and_shown_once(
        window, monkeypatch, tmp_path):
    """@brief Explain missing applications without blocking application startup."""
    from kicadgen.services.system_dependencies import ExternalApplications

    kicad = tmp_path / "kicad-cli.exe"
    kicad.write_bytes(b"")
    monkeypatch.setattr(
        gui, "detect_external_applications",
        lambda: ExternalApplications(str(kicad), None),
    )
    captured = []

    def capture(message):
        captured.append((
            message.windowTitle(), message.text(), message.informativeText(),
            tuple(button.text() for button in message.buttons()),
        ))
        return 0

    monkeypatch.setattr(QMessageBox, "exec", capture)
    window.i18n.set_language("fr")

    assert window._warn_missing_external_applications()
    assert not window._warn_missing_external_applications()
    assert len(captured) == 1
    title, heading, explanation, buttons = captured[0]
    assert title == "Applications requises"
    assert heading == "Non détectées : FreeCAD"
    assert "KiCad détecté" in explanation
    assert "FreeCAD non détecté" in explanation
    assert "Continuer" in buttons


def test_startup_dependency_warning_is_skipped_when_both_are_found(
        window, monkeypatch, tmp_path):
    """@brief Keep startup silent when both applications are installed."""
    from kicadgen.services.system_dependencies import ExternalApplications

    kicad = tmp_path / "kicad-cli.exe"
    freecad = tmp_path / "freecadcmd.exe"
    kicad.write_bytes(b"")
    freecad.write_bytes(b"")
    monkeypatch.setattr(
        gui, "detect_external_applications",
        lambda: ExternalApplications(str(kicad), str(freecad)),
    )
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda message: shown.append(message))

    assert not window._warn_missing_external_applications()
    assert not shown


@pytest.mark.parametrize("accept", [True, False])
def test_generation_options_save_or_cancel(window, monkeypatch, accept):
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QDialogButtonBox
    saved = []
    window._config["include_nc_pins"] = True
    monkeypatch.setattr(gui, "save_config", lambda data: saved.append(dict(data)))
    observed = []
    def edit():
        dialog = window.findChild(gui.GenerationOptionsDialog)
        observed.append(dialog.isVisible() and dialog.isModal())
        assert dialog.include_nc_checkbox.isChecked()
        dialog.include_nc_checkbox.setChecked(False)
        assert not dialog.advanced_user_checkbox.isChecked()
        dialog.advanced_user_checkbox.setChecked(True)
        role = (QDialogButtonBox.StandardButton.Save if accept
                else QDialogButtonBox.StandardButton.Cancel)
        dialog.buttons.button(role).click()
    QTimer.singleShot(0, edit)
    window.action_generation_options.trigger()
    assert observed == [True]
    assert window._config["include_nc_pins"] is (not accept)
    assert len(saved) == int(accept)
    assert window._advanced_user() is accept
    assert window.tabs.isTabVisible(1) is accept
    assert window.tabs.isTabVisible(2) is accept


def test_output_actions_require_current_footprint(window, tmp_path, monkeypatch):
    footprint_controls = (window.btn_open_output, window.action_open_output)
    complete_controls = (
        window.btn_test_kicad, window.btn_export, window.btn_install_global,
        window.action_test_kicad, window.action_export,
        window.action_install_global,
    )
    controls = footprint_controls + complete_controls
    assert all(not control.isEnabled() for control in controls)
    window._docs_dir().mkdir(parents=True, exist_ok=True)
    window._refresh_output_actions()
    assert all(not control.isEnabled() for control in controls)
    name = window._component_name()
    footprint = window._project_dir() / f"{name}.pretty" / f"{name}.kicad_mod"
    footprint.parent.mkdir()
    footprint.write_text("(footprint test)", encoding="utf-8")
    monkeypatch.setattr(window, "_draw_preview", lambda: None)
    monkeypatch.setattr(window, "_draw_preview_3d", lambda: None)
    window._on_generated(str(footprint))
    assert all(control.isEnabled() for control in footprint_controls)
    assert all(not control.isEnabled() for control in complete_controls)
    step = window._project_dir() / f"{name}.3dshapes" / f"{name}.step"
    step.parent.mkdir()
    step.write_bytes(b"STEP")
    window._refresh_output_actions()
    assert all(control.isEnabled() for control in controls)
    window.field_name.setText("AltroComponente")
    assert all(not control.isEnabled() for control in controls)
    window.field_name.setText(name)
    assert all(control.isEnabled() for control in controls)
    window.field_library_root.setText(str(tmp_path / "altro"))
    assert all(not control.isEnabled() for control in controls)


def test_kicad_tab_is_visible_in_standard_user_mode(window):
    """@brief Keep the final review workflow available outside advanced mode."""
    assert not window._advanced_user()
    assert window.tabs.count() == 6
    assert window.tabs.isTabVisible(5)
    assert window.tabs.tabText(5) == "🔌 KiCad"
    assert window.tabs.widget(5).isAncestorOf(window.btn_test_kicad)


def test_kicad_workflow_errors_follow_selected_language(window):
    """@brief Translate structured service failures before displaying them."""
    from kicadgen.services.kicad_library import KiCadLibraryError

    window.i18n.set_language("fr")
    error = KiCadLibraryError(
        "STEP model not found: C:/Demo.step",
        code="step_missing", path="C:/Demo.step",
    )
    assert window._kicad_error_text(error) == (
        "Modèle STEP introuvable : C:/Demo.step")


def test_output_handlers_do_not_open_or_export_without_footprint(window, monkeypatch):
    calls = []
    monkeypatch.setattr(gui.webbrowser, "open", lambda *args: calls.append(args))
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *args: calls.append(args))
    window._open_project_dir()
    window._export_component()
    assert calls == []


def _write_complete_kicad_output(window):
    """@brief Create the minimal footprint/STEP pair required by KiCad actions."""
    name = window._component_name()
    footprint = window._project_dir() / f"{name}.pretty" / f"{name}.kicad_mod"
    step = window._project_dir() / f"{name}.3dshapes" / f"{name}.step"
    footprint.parent.mkdir(parents=True, exist_ok=True)
    step.parent.mkdir(parents=True, exist_ok=True)
    footprint.write_text(
        f'(footprint "{name}"\n\t(version 20260206)\n'
        '\t(generator "test")\n\t(layer "F.Cu")\n)\n',
        encoding="utf-8",
    )
    step.write_bytes(b"STEP")
    window._refresh_output_actions()
    return footprint, step


def test_kicad_tab_opens_isolated_preview(window, tmp_path, monkeypatch):
    """@brief Test in KiCad creates a preview without touching a library table."""
    from kicadgen.services.system_dependencies import ExternalApplications

    _write_complete_kicad_output(window)
    pcbnew = tmp_path / "pcbnew.exe"
    pcbnew.touch()
    monkeypatch.setattr(
        gui, "detect_external_applications",
        lambda: ExternalApplications(None, None, str(pcbnew)),
    )
    launched = []
    monkeypatch.setattr(
        gui, "launch_preview",
        lambda executable, board: launched.append((Path(executable), Path(board))),
    )
    window._test_in_kicad()
    assert launched and launched[0][0] == pcbnew
    assert launched[0][1].is_file()
    assert ".kicad-preview" in launched[0][1].parts
    assert not (window._project_dir() / "fp-lib-table").exists()


def test_kicad_tab_installs_portable_project_library(window, tmp_path, monkeypatch):
    """@brief Project installation creates both libraries and fp-lib-table."""
    _write_complete_kicad_output(window)
    project = tmp_path / "DestinationProject"
    project.mkdir()
    (project / "DestinationProject.kicad_pro").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        gui.QFileDialog, "getExistingDirectory", lambda *args: str(project))
    window._install_project_library()
    name = window._component_name()
    installed = project / "KiCadGen.pretty" / f"{name}.kicad_mod"
    assert installed.is_file()
    assert f'${{KIPRJMOD}}/KiCadGen.3dshapes/{name}.step' in installed.read_text(
        encoding="utf-8")
    assert (project / "KiCadGen.3dshapes" / f"{name}.step").is_file()
    assert '(name "KiCadGen")' in (project / "fp-lib-table").read_text(
        encoding="utf-8")


def test_footprint_sections_have_independent_scroll_tabs(window):
    from PyQt6.QtWidgets import QScrollArea
    tabs = window.footprint_subtabs
    assert [tabs.tabText(i) for i in range(tabs.count())] == [
        "Generale", "Matrice THT", "Modulo castellated", "Parametri 3D"]
    for index, field in enumerate((window.field_description, window.field_pitch,
                                   window.table_sides, window.field_body_height)):
        page = tabs.widget(index)
        assert isinstance(page, QScrollArea)
        assert page.widgetResizable()
        assert page.widget().isAncestorOf(field)
    assert window.table_sides.minimumHeight() >= 150


def test_footprint_tab_navigation_does_not_change_layout_or_values(window):
    window.group_castellated.setChecked(True)
    assert window.footprint_subtabs.currentIndex() == 2
    window.field_cast_body_length.setValue(42.8)
    window.field_description.setText("Descrizione modificabile")
    for index in range(4):
        window.footprint_subtabs.setCurrentIndex(index)
        assert window._is_castellated()
        assert window.field_cast_body_length.value() == 42.8
        assert window.field_description.isEnabled()
        assert not window.field_pitch.isEnabled()
    window.group_castellated.setChecked(False)
    assert window.footprint_subtabs.currentIndex() == 1
    assert window.field_pitch.isEnabled()
    assert window.field_description.text() == "Descrizione modificabile"


def test_only_relevant_geometry_subtab_is_visible(window):
    tabs = window.footprint_subtabs
    assert tabs.isTabVisible(1)
    assert not tabs.isTabVisible(2)
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("aggregate_castellated"))
    assert not tabs.isTabVisible(1)
    assert tabs.isTabVisible(2)
    assert tabs.currentIndex() == 2
    assert tabs.isTabVisible(0) and tabs.isTabVisible(3)
    window.group_castellated.setChecked(False)
    assert tabs.isTabVisible(1)
    assert not tabs.isTabVisible(2)
    assert tabs.currentIndex() == 1


def test_saved_castellated_type_restores_matching_subtab(window, monkeypatch):
    monkeypatch.setattr(gui, "load_config", lambda: {
        "component_type": "aggregate_castellated", "system_prompt": ""})
    restored = gui.MainWindow()
    try:
        assert restored._is_castellated()
        assert restored.footprint_subtabs.isTabVisible(2)
        assert not restored.footprint_subtabs.isTabVisible(1)
    finally:
        restored.close()


def test_project_no_longer_has_bottom_ai_help(window):
    assert not any(label.text().startswith("Spunta uno o piu' file in lista")
                   for label in window.findChildren(QLabel))


@pytest.mark.parametrize("action", ["Salva", "Annulla", "escape", "close"])
def test_component_prompt_modal_save_and_cancel(window, action):
    from PyQt6.QtCore import QTimer
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QPushButton
    dialog = window.component_prompt_dialog
    field = window.field_component_prompt
    field.setPlainText("Originale\nNota personale")
    assert field.parentWidget() is dialog
    assert not dialog.isVisible()
    assert dialog.isModal()
    observed = []
    def edit_and_finish():
        observed.append(dialog.isVisible())
        field.setPlainText("Modificato <testo>")
        if action == "escape":
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
        elif action == "close":
            dialog.close()
        else:
            next(button for button in dialog.findChildren(QPushButton)
                 if button.text() == action).click()
    QTimer.singleShot(0, edit_and_finish)
    window.btn_component_prompt.click()
    assert observed == [True]
    expected = "Modificato <testo>" if action == "Salva" else "Originale\nNota personale"
    assert field.toPlainText() == expected
    # Reopening keeps the committed value, even after a previous rejection.
    QTimer.singleShot(0, dialog.reject)
    window.btn_component_prompt.click()
    assert field.toPlainText() == expected


def test_project_info_is_only_in_modal(window):
    from PyQt6.QtCore import QTimer
    dialog = window.project_info_dialog
    assert not dialog.isVisible()
    notes = [label for label in dialog.findChildren(QLabel)
             if "Il nome del componente" in label.text()]
    assert len(notes) == 1
    assert dialog.isModal()
    observed = []
    def close():
        observed.append(dialog.isVisible())
        dialog.accept()
    QTimer.singleShot(0, close)
    window.btn_project_info.click()
    assert observed == [True]
    assert not dialog.isVisible()


def test_generate_summary_is_in_modal_and_refreshed_on_open(window):
    from PyQt6.QtCore import QTimer
    dialog = window.generate_info_dialog
    assert window.label_summary.parentWidget() is dialog
    assert dialog.isModal() and not dialog.isVisible()
    assert not window.btn_generate_info.icon().isNull()
    window.field_name.setText("Modulo_verifica")
    observed = []
    def check_and_close():
        observed.append((dialog.isVisible(), window.label_summary.text()))
        dialog.accept()
    QTimer.singleShot(0, check_and_close)
    window.btn_generate_info.click()
    assert observed[0][0]
    assert "Modulo_verifica" in observed[0][1]
    assert not dialog.isVisible()


def test_ai_log_modal_preserves_history_and_receives_live_updates(window):
    from PyQt6.QtCore import QTimer
    dialog = window.ai_log_dialog
    # The log lives in the splitter's actions panel, inside this modal.
    assert dialog.isAncestorOf(window.ai_progress_text)
    assert window.ai_progress_text.isReadOnly()
    assert not dialog.isVisible()
    assert dialog.isModal()
    window._on_ai_progress("Prima dell'apertura")
    observed = []
    def update_and_close():
        observed.append(dialog.isVisible())
        window._on_ai_progress("Durante: <b>testo letterale</b>")
        dialog.accept()
    QTimer.singleShot(0, update_and_close)
    window.btn_ai_log.click()
    assert observed == [True]
    window._on_ai_progress("Dopo la chiusura")
    QTimer.singleShot(0, dialog.reject)
    window.btn_ai_log.click()
    text = window.ai_progress_text.toPlainText()
    assert "Prima dell'apertura" in text
    assert "Durante: <b>testo letterale</b>" in text
    assert "Dopo la chiusura" in text


def test_copy_ai_panels_copies_all_plain_text_without_modifying_logs(window, monkeypatch):
    from unittest.mock import Mock
    clipboard = Mock()
    monkeypatch.setattr(QApplication, 'clipboard', lambda: clipboard)
    assert not window.btn_copy_ai_log.isEnabled()
    assert not window.btn_copy_ai_elements.isEnabled()
    window._copy_ai_output()
    clipboard.setText.assert_not_called()
    window._on_ai_progress('Analisi: <b>testo</b> & quote')
    window._on_ai_progress('#1 Ingombro: 2 mm')
    window._on_ai_progress('#2 Componente: 3 mm')
    assert window.btn_copy_ai_log.isEnabled() and window.btn_copy_ai_elements.isEnabled()
    assert not window.btn_copy_ai_log.icon().isNull()
    assert not window.btn_copy_ai_elements.icon().isNull()
    window.ai_elements_list.setCurrentRow(0)  # Copy all rows, not just the selection.
    actions = window.ai_progress_text.toPlainText()
    window.btn_copy_ai_log.click()
    clipboard.setText.assert_called_with(actions)
    window.btn_copy_ai_elements.click()
    clipboard.setText.assert_called_with('#1 Ingombro: 2 mm\n#2 Componente: 3 mm')
    assert window.ai_progress_text.toPlainText() == actions
    assert window.ai_elements_list.count() == 2
    window.ai_progress_text.clear()
    window.ai_elements_list.clear()
    assert not window.btn_copy_ai_log.isEnabled()
    assert not window.btn_copy_ai_elements.isEnabled()


def test_3d_viewport_is_not_inside_scrolling_settings(window):
    from PyQt6.QtWidgets import QScrollArea, QSplitter
    parent = window.preview_3d.parentWidget()
    splitter = None
    while parent is not None:
        assert not isinstance(parent, QScrollArea)
        if isinstance(parent, QSplitter):
            splitter = parent
            break
        parent = parent.parentWidget()
    assert splitter is not None
    assert isinstance(splitter.widget(1), QScrollArea)
    assert splitter.widget(1).widget() is not splitter.widget(0)


def test_3d_actions_are_centered_outside_scroll_and_splitter(window):
    from PyQt6.QtWidgets import QScrollArea, QSplitter
    bar = window.model_3d_action_bar
    assert bar.isAncestorOf(window.btn_regen_3d)
    layout = bar.layout()
    assert layout.itemAt(0).spacerItem() is not None
    assert layout.itemAt(layout.count() - 1).spacerItem() is not None
    parent = bar.parentWidget()
    while parent is not None:
        assert not isinstance(parent, (QScrollArea, QSplitter))
        parent = parent.parentWidget()
    container_layout = bar.parentWidget().layout()
    assert isinstance(container_layout.itemAt(0).widget(), QSplitter)
    assert container_layout.itemAt(1).widget() is bar


def test_comparison_button_opens_valid_image_and_resets_on_next_result(window,monkeypatch):
    png = draw_inventory(np.full((80,160,3),40,np.uint8), [])
    params = ExtractedParams(name="",description="",tags="",confidence="medium",notes="",
                             layout="castellated",castellated_sides=[],photo_overlay_png=png,
                             photo_source="top.png")
    window._on_extracted(params)
    assert window.btn_photo_comparison.isEnabled()
    def inspect(dialog):
        assert "top.png" in dialog.windowTitle()
        assert any(not label.pixmap().isNull() for label in dialog.findChildren(QLabel))
        return 0
    monkeypatch.setattr(QDialog,"exec",inspect)
    window.btn_photo_comparison.click()
    params.photo_overlay_png = None
    window._on_extracted(params)
    assert not window.btn_photo_comparison.isEnabled()


@pytest.fixture
def raw_params():
    params = ExtractedParams(
        name="", description="", tags="", confidence="medium", notes="",
        layout="castellated", castellated_sides=[], body_length_mm=40, body_width_mm=20,
        rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=2.54,
        pad_diameter_mm=1.7, drill_mm=1.0,
        photo_source="raw-top.png", components_3d=[
            dict(name="U1", kind="ic", x_mm=-4, y_mm=2, length_mm=6, width_mm=2,
                 height_mm=1.5, rotation_deg=90, confidence="low", _uncertain=True,
                 vendor_data={"pins": [1, 2]}, _box=(.2, .3, .5, .6)),
            dict(name="R1", kind="resistor", x_mm=8, y_mm=0, length_mm=2, width_mm=1,
                 height_mm=.5, rotation_deg=0, vendor_data={"pins": [3, 4]}),
        ],
    )
    # Assignment also supports the parent landing the dataclass field separately.
    params.photo_png = draw_inventory(np.full((100, 400, 3), 40, np.uint8), [])
    return params


def test_raw_photo_opens_with_current_table_dimensions_and_cancel_is_unchanged(window, raw_params, monkeypatch):
    window._on_extracted(raw_params)
    window.table_components.cellWidget(0, 2).setValue(-7.5)
    window.field_cast_body_length.setValue(50)
    window._last_mesh_path = Path("previous.preview.json")
    before = window._photo_components_from_table()
    before_status = window.label_3d_status.text()
    assert window._photo_png == raw_params.photo_png
    assert window.btn_photo_comparison.isEnabled()

    def inspect(dialog):
        assert isinstance(dialog, gui.PhotoReviewDialog)
        assert "raw-top.png" in dialog.windowTitle()
        assert dialog.photo_transform.body_length_mm == 50
        assert dialog.photo_transform.body_width_mm == 20
        assert dialog.get_components() == before
        assert dialog.get_components()[0]["x_mm"] == -7.5
        assert dialog._items[0].pen().color().name() == "#ffad33"
        dialog.fields["x_mm"].setValue(4)
        dialog.delete_selected()
        dialog.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(gui.PhotoReviewDialog, "exec", inspect)
    window.btn_photo_comparison.click()
    assert window._photo_components_from_table() == before
    assert window._last_mesh_path == Path("previous.preview.json")
    assert window.label_3d_status.text() == before_status


def test_photo_apply_updates_table_metadata_arbitrary_rotation_and_invalidates_mesh(window, raw_params, monkeypatch):
    original = deepcopy(raw_params.components_3d)
    window._on_extracted(raw_params)
    window._last_mesh_path = Path("old.preview.json")
    cleared = []
    monkeypatch.setattr(window.preview_3d, "clear_preview", lambda: cleared.append(True))

    def edit(dialog):
        dialog.fields["x_mm"].setValue(1.234567)
        dialog.fields["rotation_deg"].setValue(37)
        dialog.fields["width_mm"].setValue(.025)
        dialog.component_list.setCurrentRow(1)
        dialog.delete_selected()
        dialog.add_component()
        dialog.text_fields["name"].setText("Manual part")
        dialog.text_fields["kind"].setText("custom-package")
        dialog.accept()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(gui.PhotoReviewDialog, "exec", edit)
    window.btn_photo_comparison.click()
    actual = window._photo_components_from_table()
    assert len(actual) == 2
    assert actual[0]["x_mm"] == pytest.approx(1.234567)
    assert actual[0]["width_mm"] == .025
    assert actual[0]["rotation_deg"] == 37
    assert actual[0]["vendor_data"] == original[0]["vendor_data"]
    assert actual[1]["name"] == "Manual part"
    assert actual[1]["kind"] == "custom-package"
    assert window.castellated_components[0].rotation == 37
    assert window._last_mesh_path is None
    assert cleared == [True]
    assert "da rigenerare" in window.label_3d_status.text()
    assert raw_params.components_3d == original
    actual[0]["vendor_data"]["pins"].append(99)
    assert window._photo_components_from_table()[0]["vendor_data"] == original[0]["vendor_data"]


def test_row_metadata_survives_deleting_earlier_row_and_adding_table_row(window, raw_params):
    window._on_extracted(raw_params)
    window.table_components.setCurrentCell(0, 0)
    window._remove_component_row()
    window._add_component_row()
    actual = window._photo_components_from_table()
    assert actual[0]["name"] == "R1"
    assert actual[0]["vendor_data"] == {"pins": [3, 4]}
    assert "vendor_data" not in actual[1]


def test_no_photo_next_extraction_clears_raw_and_disables_editor(window, raw_params):
    window._on_extracted(raw_params)
    raw_params.photo_png = None
    raw_params.photo_overlay_png = None
    raw_params.photo_source = ""
    window._on_extracted(raw_params)
    assert window._photo_png is None
    assert window._photo_overlay_png is None
    assert window._photo_source == ""
    assert not window.btn_photo_comparison.isEnabled()


def test_table_changes_refresh_normalized_geometry_cache(window, raw_params):
    window._on_extracted(raw_params)
    window.table_components.cellWidget(0, 2).setValue(0)
    window.table_components.cellWidget(0, 3).setValue(0)
    # Local 6 x 2 at 90 degrees has board-aligned span 2 x 6 mm.
    assert window._photo_components_from_table()[0]["_box"] == pytest.approx((.475, .35, .525, .65))


def test_invalid_raw_image_warns_without_modifying_table(window, raw_params, monkeypatch):
    window._on_extracted(raw_params)
    original = window._photo_components_from_table()
    window._photo_png = b"invalid"
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args))
    window.btn_photo_comparison.click()
    assert len(messages) == 1
    assert window._photo_components_from_table() == original


def test_worker_forwards_separate_photo_and_datasheet_models(monkeypatch):
    calls = []
    expected = object()
    def extract(paths, **kwargs):
        calls.append((paths, kwargs))
        return expected
    monkeypatch.setattr(gui, "extract_pin_header_params", extract)
    worker = gui.ExtractWorker([Path("board.png")], model="vendor/datasheet", vision_model="vendor/photo",
                               grounding_prompt="localize", verification_prompt="verify",
                               verification_model="vendor/verifier", terminal_prompt="terminals")
    spy = QSignalSpy(worker.done)
    worker.run()
    assert spy[0][0] is expected
    assert calls[0][1]["model"] == "vendor/datasheet"
    assert calls[0][1]["vision_model"] == "vendor/photo"
    assert calls[0][1]["grounding_prompt"] == "localize"
    assert calls[0][1]["verification_prompt"] == "verify"
    assert calls[0][1]["verification_model"] == "vendor/verifier"
    assert calls[0][1]["terminal_prompt"] == "terminals"


def test_settings_preserve_legacy_defaults_but_runtime_uses_one_model(window, monkeypatch):
    window.profile_store.save_defaults({'vision_model': 'vendor/old-photo'})
    window._session_model = "vendor/session-datasheet"
    saved = []
    class SettingsStub:
        def __init__(self, prompt, model, parent, *, current_vision_model,
                     current_grounding_prompt, current_verification_prompt, current_verification_model):
            assert current_vision_model == "vendor/old-photo"
            assert parent is window
        def exec(self): return QDialog.DialogCode.Accepted
        def prompt(self): return "prompt"
        def model(self): return "vendor/new-datasheet"
        def vision_model(self): return "vendor/new-photo"
        def verification_model(self): return "vendor/new-verify"
        def grounding_prompt(self): return "localize"
        def verification_prompt(self): return "verify"
    monkeypatch.setattr(gui, "LlmSettingsDialog", SettingsStub)
    monkeypatch.setattr(gui, "save_config", lambda config: saved.append(deepcopy(config)))
    window._edit_llm_settings()
    assert saved == []  # No AI profile writes to legacy JSON.
    defaults = window.profile_store.defaults()
    assert defaults["vision_model"] == "vendor/new-photo"
    assert defaults["verification_model"] == "vendor/new-verify"
    assert defaults["grounding_prompt"] == "localize"
    assert defaults["verification_prompt"] == "verify"
    assert defaults["model"] == "vendor/new-datasheet"
    assert window._effective_model() == "vendor/session-datasheet"
    assert window._vision_model() == "vendor/session-datasheet"
    assert window._verification_model() == "vendor/session-datasheet"


def test_single_model_selection_drives_all_ai_stages_and_can_return_to_default(window, monkeypatch):
    window.profile_store.save_defaults({'model': 'vendor/default'})
    window._refresh_model_combo()
    monkeypatch.setattr(gui, "pick_model", lambda *args, **kwargs: "vendor/session")
    window.btn_pick_model.click()
    assert window._vision_model() == "vendor/session"
    assert window._verification_model() == "vendor/session"
    assert window._effective_model() == "vendor/session"
    assert window.profile_store.defaults()["model"] == "vendor/default"
    assert window.combo_model.currentData() == "vendor/session"
    monkeypatch.setattr(gui, "pick_model", lambda *args, **kwargs: None)
    window.btn_pick_model.click()
    assert window._vision_model() == "vendor/session"
    window.combo_model.setCurrentIndex(0)
    assert window._vision_model() == "vendor/default"
    assert window._effective_model() == "vendor/default"


def test_single_default_model_is_used_for_vision(window):
    assert window._vision_model() == window._default_model()
    settings = gui.LlmSettingsDialog("prompt", "vendor/datasheet", window,
                                     current_vision_model=window._vision_model())
    try:
        assert settings.vision_model() == window._default_model()
    finally:
        settings.close()


def test_custom_profile_drives_geometry_family_and_worker_snapshot(window, monkeypatch):
    store = window.profile_store
    key = store.save_type(None, label='Modulo personale', family='aggregate_castellated',
                         instructions='istruzioni specifiche', overrides={
                             'model': 'type/data', 'vision_model': 'type/photo',
                             'verification_model': 'type/verify', 'system_prompt': 'type prompt',
                             'grounding_prompt': 'ground', 'verification_prompt': 'verify',
                             'terminal_prompt': 'terminals', 'image_instructions': 'extra foto'})
    window._reload_profile_types()
    window.field_component_prompt.setPlainText('note libere')
    window.combo_component_type.setCurrentIndex(window.combo_component_type.findData(key))
    assert window.group_castellated.isChecked()
    assert 'istruzioni specifiche' in window.field_component_prompt.toPlainText()
    assert 'note libere' in window.field_component_prompt.toPlainText()
    assert window.combo_model.currentText() == 'Predefinito: type/data'
    monkeypatch.setattr(gui.ExtractWorker, 'start', lambda self: None)
    item = QListWidgetItem('board.png')
    item.setCheckState(Qt.CheckState.Checked)
    window.docs_list.addItem(item)
    # Il rilevatore parte forzato su YOLO, che pretende pesi ONNX e runtime
    # installati: senza, l'estrazione si ferma su un avviso e questo test
    # fallirebbe per un motivo che col suo oggetto non c'entra. La scelta
    # predefinita resta verificata in tests/test_photo_detector_ui.py.
    window.combo_photo_detector.setCurrentIndex(
        window.combo_photo_detector.findData('ai'))
    window._extract_with_ai()
    worker = window._extract_worker
    assert (worker.model, worker.vision_model, worker.verification_model) == ('type/data', 'type/data', 'type/data')
    assert worker.system_prompt == 'type prompt'
    assert worker.grounding_prompt.startswith('ground') and 'extra foto' in worker.grounding_prompt
    assert worker.verification_prompt.startswith('verify') and 'extra foto' in worker.verification_prompt
    assert worker.terminal_prompt.startswith('terminals') and 'extra foto' in worker.terminal_prompt
    assert 'istruzioni specifiche' in worker.extra_instructions
    store.save_type(key, label='changed', family='single_smd', overrides={'model': 'next'})
    assert worker.model == 'type/data'  # Running request is isolated from later edits.
    window._on_extract_finished()


def test_single_session_model_does_not_persist(window, monkeypatch):
    before = window.profile_store.defaults()
    monkeypatch.setattr(gui, 'pick_model', lambda *a, **kw: 'session/all')
    window.btn_pick_model.click()
    assert window._verification_model() == 'session/all'
    assert window._effective_model() == 'session/all'
    assert window._vision_model() == 'session/all'
    assert window.profile_store.defaults() == before
    window.combo_model.setCurrentIndex(0)
    assert window._verification_model() == before['model']
    assert window._effective_model() == before['model']


def test_removed_type_refresh_preserves_free_notes_and_falls_back(window):
    window.combo_component_type.setCurrentIndex(window.combo_component_type.findData('single_smd'))
    window.field_component_prompt.append('note da conservare')
    window.profile_store.delete_type('single_smd')
    window._reload_profile_types()
    assert window.combo_component_type.currentData() == ''
    assert 'note da conservare' in window.field_component_prompt.toPlainText()
    assert window.combo_component_type.findData('single_smd') == -1


def test_only_defaults_refresh_does_not_replace_session_prompt(window):
    window.combo_component_type.setCurrentIndex(window.combo_component_type.findData('single_smd'))
    window.field_component_prompt.setPlainText('custom edited session instructions')
    window.profile_store.save_defaults({'model': 'new/default'})
    window._reload_profile_types(update_prompt=False)
    assert window.field_component_prompt.toPlainText() == 'custom edited session instructions'
    assert window._effective_model() == 'new/default'


def test_start_extraction_uses_single_model_and_resets_photo_state(window, raw_params, monkeypatch):
    window._on_extracted(raw_params)
    window.profile_store.save_defaults({'vision_model': 'vendor/photo'})
    window._session_model = "vendor/datasheet"
    # Suppress only thread execution: inspect the real constructed worker.
    monkeypatch.setattr(gui.ExtractWorker, "start", lambda self: None)
    row = QListWidgetItem("board.png")
    row.setCheckState(Qt.CheckState.Checked)
    window.docs_list.addItem(row)
    window.combo_photo_detector.setCurrentIndex(
        window.combo_photo_detector.findData('ai'))
    window._extract_with_ai()
    assert window._extract_worker.model == "vendor/datasheet"
    assert window._extract_worker.vision_model == "vendor/datasheet"
    assert window._extract_worker.verification_model == "vendor/datasheet"
    assert window._photo_png is None
    assert window._photo_overlay_png is None
    assert not window.btn_photo_comparison.isEnabled()
    assert "Modello AI unico: vendor/datasheet" in window.ai_progress_text.toPlainText()
    assert "modello AI vendor/datasheet" in window.log_text.toPlainText()


@pytest.mark.parametrize("layout", ["castellated", "matrix"])
def test_extraction_discards_previous_mesh(window, raw_params, monkeypatch, tmp_path, layout):
    raw_params.layout = layout
    old_mesh = tmp_path / "old.preview.json"
    old_mesh.touch()
    window._last_mesh_path = old_mesh
    cleared, rendered = [], []
    monkeypatch.setattr(window.preview_3d, "clear_preview", lambda: cleared.append(True))
    monkeypatch.setattr(window.preview_3d, "render_mesh", rendered.append)

    window._on_extracted(raw_params)

    assert window._last_mesh_path is None
    assert cleared
    assert "da rigenerare" in window.label_3d_status.text()
    window._draw_preview_3d()
    window._on_pin_numbers_toggled(True)
    assert rendered == []


@pytest.mark.parametrize("layout", ["castellated", "matrix"])
@pytest.mark.parametrize("regenerate", [False, True], ids=["initial", "regen"])
@pytest.mark.parametrize("change", ["apply", "extraction", "cancel"])
def test_queued_cad_result_respects_photo_revision(
    window, raw_params, monkeypatch, tmp_path, layout, regenerate, change,
):
    raw_params.layout = layout
    raw_params.body_height_mm = 2
    raw_params.castellated_sides = [dict(edge="left", count=4, pitch_mm=2.54, first_pin=1)]
    window._on_extracted(raw_params)
    window.group_3d.setChecked(True)
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda self: None)
    monkeypatch.setattr(gui.Cad3DWorker, "start", lambda self: None)
    rendered = []
    monkeypatch.setattr(window.preview_3d, "render_mesh", rendered.append)
    step = tmp_path / "board.step"
    mesh = tmp_path / "board.preview.json"
    mesh.touch()

    def start():
        if regenerate:
            window._regenerate_3d()
            worker = window._cad3d_worker
        else:
            window._generate()
            worker = window._worker
        assert worker is not None, window.log_text.toPlainText()
        return worker

    worker = start()
    # Deliver real Qt signals only after the user action, without running CAD.
    if not regenerate:
        QMetaObject.invokeMethod(
            worker, "done", Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, str(tmp_path / "board.kicad_mod")),
        )
    QMetaObject.invokeMethod(
        worker, "done" if regenerate else "cad3d_done",
        Qt.ConnectionType.QueuedConnection, Q_ARG(str, str(step)),
    )
    if change == "apply":
        components = window._photo_components_from_table()
        if components:
            components[0]["x_mm"] += 1
        window._apply_photo_review(components)
    elif change == "extraction":
        raw_params.body_width_mm = 25
        window._on_extracted(raw_params)
    else:
        def cancel(dialog):
            dialog.add_component()
            dialog.reject()
            return QDialog.DialogCode.Rejected
        monkeypatch.setattr(gui.PhotoReviewDialog, "exec", cancel)
        window._show_photo_comparison()
    status = window.label_3d_status.text()
    QApplication.processEvents()

    if change == "cancel":
        assert rendered == [mesh]
        assert window._last_mesh_path == mesh
        assert "Modello STEP aggiornato" in window.label_3d_status.text()
    else:
        assert rendered == []
        assert window._last_mesh_path is None
        assert window.label_3d_status.text() == status
        assert "OK (3D)" not in window.log_text.toPlainText()
        # Stale failures/warnings must not overwrite the pending revision either.
        (worker.error if regenerate else worker.cad3d_error).emit("obsolete error")
        worker.warning.emit("obsolete warning")
        assert window.label_3d_status.text() == status
        assert "obsolete" not in window.log_text.toPlainText()

    worker.finished.emit()
    assert (window.btn_regen_3d if regenerate else window.btn_generate).isEnabled()
    if change != "cancel":
        current_worker = start()
        (current_worker.done if regenerate else current_worker.cad3d_done).emit(str(step))
        assert rendered == [mesh]
        assert window._last_mesh_path == mesh
        assert "Modello STEP aggiornato" in window.label_3d_status.text()
        current_worker.finished.emit()
