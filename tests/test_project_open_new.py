"""@brief Progetto nuovo o esistente, scelto esplicitamente (utente, 2026-09-24).

Decisioni dell'utente: menu File con «Nuovo progetto…», «Apri progetto…» e
«Rinomina progetto…», campo del nome in sola lettura (1a); aprendo un progetto
torna cio' che la cartella gia' contiene (2a); il nome proposto dall'analisi
non puo' essere quello di un altro progetto (3a).

Ogni regola ha il suo negativo.
"""
import os
import time

import pytest
from PyQt6.QtWidgets import QDialog, QDialogButtonBox

from test_photo_inventory_gui import window, gui  # noqa: F401 - fixture condivisa
from kicadgen.gui import project_dialogs
from kicadgen.gui.project_dialogs import (OpenProjectDialog, ProjectNameDialog, is_project_folder,
                                          list_projects, name_problem)


def _project(root, name, marker="docs"):
    folder = root / name
    (folder / marker).mkdir(parents=True)
    return folder


# ── Regole sul nome ──────────────────────────────────────────────────────

def test_a_free_name_is_accepted_and_an_existing_one_is_refused(tmp_path):
    _project(tmp_path, "Scheda")
    assert name_problem(tmp_path, "Nuova") == ""
    assert "Esiste gia'" in name_problem(tmp_path, "Scheda")
    # Windows: «scheda» e «Scheda» sono la stessa cartella, quindi lo stesso progetto.
    assert "Esiste gia'" in name_problem(tmp_path, "SCHEDA")


def test_a_rename_may_change_only_the_letter_case_of_its_own_name(tmp_path):
    _project(tmp_path, "Scheda")
    _project(tmp_path, "Altra")
    assert name_problem(tmp_path, "SCHEDA", current="Scheda") == ""
    assert "Esiste gia'" in name_problem(tmp_path, "Altra", current="Scheda")


@pytest.mark.parametrize("name", ["", "   ", "a/b", "a:b", "x?", "fine.", "CON", "com1.txt"])
def test_names_windows_cannot_use_are_refused(tmp_path, name):
    assert name_problem(tmp_path, name) != ""


def test_only_real_projects_are_listed_newest_first(tmp_path):
    """@brief Positivo: cartelle con documenti, diario, revisione o footprint. Negativo: il resto."""
    old = _project(tmp_path, "Vecchio")
    new = _project(tmp_path, "Recente", marker="photo-review")
    pretty = tmp_path / "SoloFootprint"
    (pretty / "SoloFootprint.pretty").mkdir(parents=True)
    (tmp_path / "cartella-qualsiasi").mkdir()
    (tmp_path / ".nascosta" / "docs").mkdir(parents=True)
    past = time.time() - 3600
    os.utime(old, (past, past))
    os.utime(old / "docs", (past, past))
    names = [entry.name for entry in list_projects(tmp_path)]
    assert names[0] in ("Recente", "SoloFootprint") and names[-1] == "Vecchio"
    assert set(names) == {"Vecchio", "Recente", "SoloFootprint"}
    assert not is_project_folder(tmp_path / "cartella-qualsiasi")
    assert list_projects(tmp_path / "manca") == []


# ── Dialoghi ─────────────────────────────────────────────────────────────

def test_the_new_project_dialog_keeps_ok_off_for_an_existing_name(window, tmp_path):  # noqa: F811
    _project(tmp_path, "Scheda")
    dialog = ProjectNameDialog(tmp_path, "Nuovo progetto")
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()
    dialog.field.setText("Scheda")
    assert not ok.isEnabled() and "Esiste gia'" in dialog.problem.text()
    dialog.field.setText("Scheda2")
    assert ok.isEnabled() and dialog.problem.text() == ""
    assert dialog.project_name() == "Scheda2"


def test_the_open_dialog_lists_the_projects_and_returns_the_chosen_one(window, tmp_path):  # noqa: F811
    _project(tmp_path, "Uno")
    _project(tmp_path, "Due")
    dialog = OpenProjectDialog(tmp_path)
    names = {dialog.list.item(i).data(0x0100) for i in range(dialog.list.count())}
    assert names == {"Uno", "Due"}
    assert dialog.selected_name() in names
    empty = OpenProjectDialog(tmp_path / "vuota")
    assert empty.list.count() == 0 and not empty.buttons.button(
        QDialogButtonBox.StandardButton.Open).isEnabled()


# ── Finestra principale ──────────────────────────────────────────────────

def test_the_name_field_can_no_longer_be_typed_into(window):  # noqa: F811
    """@brief Negativo: scrivere un nome non apre piu' di nascosto una cartella esistente."""
    assert window.field_name.isReadOnly()
    actions = [a.text() for a in window.menuBar().actions()[0].menu().actions()]
    assert actions[:3] == ["Nuovo progetto...", "Apri progetto...", "Rinomina progetto..."]


def _accept_with(monkeypatch, dialog_class, prepare):
    def fake_exec(self):
        prepare(self)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(dialog_class, "exec", fake_exec)


def test_new_project_creates_its_folder_and_opens_it(window, monkeypatch):  # noqa: F811
    _accept_with(monkeypatch, ProjectNameDialog, lambda d: d.field.setText("SchedaNuova"))
    window._new_project()
    assert window._component_name() == "SchedaNuova"
    assert (window._library_root() / "SchedaNuova" / "docs").is_dir()
    assert "SchedaNuova" in [e.name for e in list_projects(window._library_root())]


def test_open_project_switches_to_an_existing_folder(window, monkeypatch):  # noqa: F811
    root = window._library_root()
    _project(root, "Esistente")
    monkeypatch.setattr(OpenProjectDialog, "selected_name", lambda self: "Esistente")
    monkeypatch.setattr(OpenProjectDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window._open_project()
    assert window._component_name() == "Esistente"
    assert window._project_dir() == root / "Esistente"


def test_cancelling_leaves_the_current_project(window, monkeypatch):  # noqa: F811
    before = window._component_name()
    monkeypatch.setattr(ProjectNameDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    monkeypatch.setattr(OpenProjectDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    window._new_project()
    window._open_project()
    assert window._component_name() == before


def test_no_project_change_while_an_analysis_runs(window, monkeypatch):  # noqa: F811
    before = window._component_name()
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    opened = []
    monkeypatch.setattr(ProjectNameDialog, "exec", lambda self: opened.append(1))
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *a: None)
    window._new_project()
    window._rename_project()
    assert not opened and window._component_name() == before


def test_rename_moves_the_project_under_the_new_name(window, monkeypatch, tmp_path):  # noqa: F811
    from PIL import Image
    from kicadgen.services.document_roles import DocumentSet
    image = tmp_path / "a.png"
    Image.new("RGB", (4, 4), "blue").save(image)
    DocumentSet(window._docs_dir()).import_file("top", image)
    _accept_with(monkeypatch, ProjectNameDialog, lambda d: d.field.setText("NomeNuovo"))
    window._rename_project()
    assert window._component_name() == "NomeNuovo"
    assert (window._docs_dir() / "top.png").is_file()


def test_the_proposed_name_of_another_project_is_refused(window):  # noqa: F811
    """@brief 3a. Positivo: un nome libero si applica. Negativo: quello di un altro progetto no."""
    root = window._library_root()
    _project(root, "GiaPresente")
    current = window._component_name()
    window._set_suggested_name("GiaPresente")
    window._apply_suggested_name()
    assert window._component_name() == current
    assert "non applicato" in window.lbl_name_suggestion.text()
    window._set_suggested_name("NomeLibero")
    window._apply_suggested_name()
    assert window._component_name() == "NomeLibero"
