"""@brief La scheda «KiCad» si sceglie solo con un progetto completato (utente, 2026-09-24).

Completato = nella cartella del progetto corrente ci sono footprint E modello
STEP: cio' che serve a tutte le operazioni della scheda. Positivo e negativi
distinti per i file presenti, non per il nome del progetto.
"""
import pytest

from tests.test_photo_inventory_gui import gui

KICAD = gui.MainWindow.KICAD_TAB_INDEX


def _outputs(root, name, footprint=True, step=True):
    if footprint:
        (root / name / f"{name}.pretty").mkdir(parents=True, exist_ok=True)
        (root / name / f"{name}.pretty" / f"{name}.kicad_mod").write_text("(footprint)")
    if step:
        (root / name / f"{name}.3dshapes").mkdir(parents=True, exist_ok=True)
        (root / name / f"{name}.3dshapes" / f"{name}.step").write_text("ISO-10303-21;")


def test_without_a_project_the_kicad_tab_cannot_be_chosen(window):  # noqa: F811
    window.field_name.setText("")
    window._refresh_output_actions()
    assert window.tabs.tabText(KICAD) and not window.tabs.isTabEnabled(KICAD)
    assert window.tabs.tabToolTip(KICAD)


def test_a_project_with_footprint_and_step_opens_the_tab(window, tmp_path):  # noqa: F811
    window.field_library_root.setText(str(tmp_path))
    _outputs(tmp_path, "SCHEDA")
    window.field_name.setText("SCHEDA")
    assert window.tabs.isTabEnabled(KICAD)
    assert window.tabs.tabToolTip(KICAD) == ""


def test_footprint_without_step_can_be_tried_without_a_model(window, tmp_path, monkeypatch):  # noqa: F811
    """@brief Positive: footprint alone -> the commands are on, the status says there is no 3D model.

    CONTRACT CHANGED 2026-11-12 (user decision A: delivery without STEP): before, the commands also required the STEP, so a PC without
    FreeCAD could never deliver anything to KiCad.
    """
    from PyQt6.QtWidgets import QMessageBox
    window.field_library_root.setText(str(tmp_path))
    _outputs(tmp_path, "SCHEDA", step=False)
    window.field_name.setText("SCHEDA")
    window._refresh_output_actions()
    assert window.tabs.isTabEnabled(KICAD)
    assert window.btn_test_kicad.isEnabledTo(window.tabs.widget(KICAD))
    assert "senza modello STEP" in window.label_kicad_status.text()
    # The command asks before delivering without a model; «No» delivers nothing.
    asked, created = [], []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.No)
    monkeypatch.setattr(gui, "create_preview_project", lambda *a, **k: created.append(a))
    window._test_in_kicad()
    assert len(asked) == 1 and "vista 3D" in asked[0] and created == []


def test_footprint_with_step_is_tried_without_the_no_model_question(window, tmp_path, monkeypatch):  # noqa: F811
    """@brief Negative twin: with the STEP there is no «without STEP» question and the status says ready."""
    from PyQt6.QtWidgets import QMessageBox
    window.field_library_root.setText(str(tmp_path))
    _outputs(tmp_path, "SCHEDA")
    # A complete STEP (with its ISO 10303-21 trailer): a truncated one is refused.
    (tmp_path / "SCHEDA" / "SCHEDA.3dshapes" / "SCHEDA.step").write_text(
        "ISO-10303-21;\nEND-ISO-10303-21;\n")
    window.field_name.setText("SCHEDA")
    window._refresh_output_actions()
    assert "senza modello STEP" not in window.label_kicad_status.text()
    artifacts = window._current_kicad_artifacts()
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a) or QMessageBox.StandardButton.No)
    assert artifacts is not None and window._confirm_without_step(artifacts) is True and asked == []


def test_leaving_a_completed_project_moves_off_the_kicad_tab(window, tmp_path):  # noqa: F811
    """Mentre la si guarda, la scheda si spegne e si torna a «Progetto»."""
    window.field_library_root.setText(str(tmp_path))
    _outputs(tmp_path, "PRONTO")
    window.field_name.setText("PRONTO")
    window.tabs.setCurrentIndex(KICAD)
    assert window.tabs.currentIndex() == KICAD
    window.field_name.setText("VUOTO")
    assert window.tabs.currentIndex() == 0 and not window.tabs.isTabEnabled(KICAD)


# ── «Esportazioni» (user, 2026-10-11): the history table in a tab of its own ──

def test_the_export_history_lives_in_its_own_always_enabled_tab(window, monkeypatch):  # noqa: F811
    """@brief Positive: the table is in «Esportazioni», enabled without a project
    and showing every record. Negative: the KiCad tab no longer holds it."""
    exports = gui.TAB_EXPORTS
    assert exports == window.tabs.count() - 1
    assert window.tabs.tabText(exports) == window.i18n.tr("tab.exports.name")
    assert window.tabs.widget(exports).isAncestorOf(window.kicad_export_table)
    assert not window.tabs.widget(KICAD).isAncestorOf(window.kicad_export_table)
    window.field_name.setText("")
    window._refresh_output_actions()
    assert window.tabs.isTabEnabled(exports) and not window.tabs.isTabEnabled(KICAD)
    records = [{"component": f"C{i}", "kind": "project", "destination": f"d{i}",
                "timestamp": "2026-10-11T10:00:00"} for i in range(15)]
    monkeypatch.setattr(gui, "load_kicad_export_history", lambda: records)
    window._refresh_kicad_export_history()
    assert window.kicad_export_table.rowCount() == 15


def test_without_step_a_refused_install_never_asks_about_the_model(window, tmp_path, monkeypatch):  # noqa: F811
    """@brief Negative: metadata missing -> the install is refused first, the «without STEP» question never comes."""
    from PyQt6.QtWidgets import QMessageBox
    window.field_library_root.setText(str(tmp_path))
    _outputs(tmp_path, "SCHEDA", step=False)
    window.field_name.setText("SCHEDA")
    asked, warned = [], []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a) or QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))
    monkeypatch.setattr(window, "_refuse_install_for_metadata", lambda: True)
    window._install_project_library()
    assert asked == []


@pytest.mark.parametrize("command", ["_install_project_library", "_test_in_kicad"])
def test_without_step_yes_delivers_and_no_delivers_nothing(window, tmp_path, monkeypatch, command):  # noqa: F811
    """@brief Positive «Sì» -> the delivery runs; negative «No» -> nothing is written or opened."""
    from PyQt6.QtWidgets import QFileDialog, QMessageBox
    from kicadgen.services.system_dependencies import ExternalApplications
    window.field_library_root.setText(str(tmp_path))
    _outputs(tmp_path, "SCHEDA", step=False)
    window.field_name.setText("SCHEDA")
    project = tmp_path / "kicad"
    project.mkdir()
    (project / "demo.kicad_pro").write_text("{}")
    monkeypatch.setattr(window, "_refuse_install_for_metadata", lambda: False)
    monkeypatch.setattr(window, "_refuse_kicad_command", lambda: False)
    monkeypatch.setattr(gui, "detect_external_applications",
                        lambda: ExternalApplications("kicad-cli", None, "pcbnew"))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(project))
    delivered = []
    monkeypatch.setattr(window, "_install_kicad_library", lambda *a, **k: delivered.append("install"))
    monkeypatch.setattr(gui, "create_preview_project", lambda *a, **k: delivered.append("preview") or
                        __import__("types").SimpleNamespace(board_path=tmp_path / "b.kicad_pcb"))
    # CONTRACT CHANGED 2026-11-13 (point 8): the preview opens through
    # `launch_preview_editors` and renders its 3D image off the GUI thread.
    monkeypatch.setattr(gui, "launch_preview_editors", lambda *a, **k: __import__("types").SimpleNamespace(
        schematic_skipped=None, schematic_error=None))
    monkeypatch.setattr(gui.MainWindow, "_start_preview_render", lambda *a, **k: None)
    for answer, expected in ((QMessageBox.StandardButton.No, []),
                             (QMessageBox.StandardButton.Yes, ["install" if "install" in command else "preview"])):
        delivered.clear()
        monkeypatch.setattr(QMessageBox, "question", lambda *a, answer=answer, **k: answer)
        getattr(window, command)()
        assert delivered == expected, (answer, delivered)

