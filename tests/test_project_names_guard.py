"""@brief Rinomina e nuovo progetto: il costo AI segue il nome, un nome occupato mai (audit 2026-09-25).

Prima: il file dei costi restava nella vecchia cartella; il nome di un altro
progetto era rifiutato solo dal pulsante OK spento della finestra, quindi un
«accetta» arrivato per un'altra via (Invio, un chiamante) fondeva due progetti.
"""
from PyQt6.QtWidgets import QDialog, QMessageBox

from kicadgen.gui.project_dialogs import ProjectNameDialog
from kicadgen.services.project_costs import COSTS_FILE, add_call, load_project_costs, start_run
from tests.test_photo_inventory_gui import gui


def _project(root, name):
    (root / name / "docs").mkdir(parents=True)
    return root / name


def _accept_with(monkeypatch, name, *, respect_ok):
    def run(dialog):
        dialog.field.setText(name)
        ok = dialog.buttons.button(dialog.buttons.StandardButton.Ok)
        if respect_ok and not ok.isEnabled():
            return QDialog.DialogCode.Rejected
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(ProjectNameDialog, "exec", run)


def test_renaming_moves_the_ai_cost_with_the_project(window, tmp_path, monkeypatch):  # noqa: F811
    window.field_library_root.setText(str(tmp_path))
    old = _project(tmp_path, "VECCHIO")
    start_run(old)
    add_call(old, {"cost_usd": 3.0})
    window._switch_project("VECCHIO", window._general_project_state)
    _accept_with(monkeypatch, "NUOVO", respect_ok=True)
    window._rename_project()
    assert window._component_name() == "NUOVO"
    assert load_project_costs(tmp_path / "NUOVO").total_usd == 3.0
    assert not (tmp_path / "VECCHIO" / COSTS_FILE).exists()
    assert window.ai_account.text().endswith("Costo dell'analisi: 3.00 USD")


def test_the_application_refuses_another_project_name_even_if_accepted(window, tmp_path,  # noqa: F811
                                                                       monkeypatch):
    """Negativo: «accetta» forzato con il nome di un progetto esistente -> nulla si fonde."""
    window.field_library_root.setText(str(tmp_path))
    _project(tmp_path, "A")
    other = _project(tmp_path, "B")
    add_call(other, {"cost_usd": 1.0})
    window._switch_project("A", window._general_project_state)
    _accept_with(monkeypatch, "B", respect_ok=False)
    window._rename_project()
    assert window._component_name() == "A"
    assert (tmp_path / "A").is_dir() and load_project_costs(other).total_usd == 1.0
    assert "Rinomina non applicata" in window._log_plain_text()


def test_the_dialog_itself_refuses_enter_on_a_taken_name(window, tmp_path):  # noqa: F811
    _project(tmp_path, "PRESO")
    dialog = ProjectNameDialog(tmp_path, "Nuovo progetto")
    dialog.field.setText("PRESO")
    dialog.accept()  # come Invio nel campo
    assert dialog.result() != QDialog.DialogCode.Accepted and dialog.isVisible() is False
    dialog.field.setText("LIBERO")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.deleteLater()


def test_a_new_project_never_reuses_a_folder_without_docs(window, tmp_path, monkeypatch):  # noqa: F811
    """Negativo: cartella di progetto esistente senza docs/ -> rifiutata, non riusata."""
    window.field_library_root.setText(str(tmp_path))
    (tmp_path / "ESISTENTE" / "ESISTENTE.pretty").mkdir(parents=True)
    _accept_with(monkeypatch, "ESISTENTE", respect_ok=False)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: warnings.append(a))
    window._new_project()
    assert warnings and not (tmp_path / "ESISTENTE" / "docs").exists()
    assert window._component_name() != "ESISTENTE"
