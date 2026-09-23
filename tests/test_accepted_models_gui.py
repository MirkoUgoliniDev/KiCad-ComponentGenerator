"""@brief «Accetto, mi piace»: pulsante, dialogo di conferma e archivio nella GUI.

Scelte dell'utente: tutta la scheda in un clic (spuntati tutti quelli con un
modello da ricordare, con la possibilita' di toglierne), preselezione da
confermare sulle schede successive, archivio in un file SQLite separato.
Il pulsante si accende solo su un solido generato dai dati correnti: si
accetta cio' che si e' visto.
"""
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog

from kicadgen.cad3d import kicad_models as km
from kicadgen.gui import main_window as gui
from kicadgen.gui.accepted_models_dialog import AcceptedModelsDialog, ConsolidateDialog
from kicadgen.services import accepted_models as am
from tests.test_photo_inventory_gui import window  # noqa: F401 - fixture condivisa


R0603 = "Resistor_SMD.3dshapes/R_0603_1608Metric.step"


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "3dmodels"
    (root / "Resistor_SMD.3dshapes").mkdir(parents=True)
    (root / R0603).write_text("ISO-10303-21;\n", encoding="utf-8")
    return root


def _board(library):
    """Due corpi: uno con modello agganciato, uno rimasto segnaposto."""
    return [
        {"name": "R1", "kind": "resistor", "length_mm": 1.6, "width_mm": 0.8,
         "bbox_basis": "body", "model_path": str(library / R0603),
         "model_match": {"status": "auto_matched"}},
        {"name": "U1", "kind": "ic", "length_mm": 5, "width_mm": 4,
         "model_match": {"status": "unresolved"}},
    ]


def test_consolidate_dialog_checks_only_rows_with_a_model(window, library):
    dialog = ConsolidateDialog(_board(library))
    remembered = dialog.table.item(0, 0)
    placeholder = dialog.table.item(1, 0)
    assert remembered.checkState() == Qt.CheckState.Checked
    assert not placeholder.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert "nessun modello KiCad" in dialog.table.item(1, 4).text()
    assert [c["name"] for c in dialog.selected_components()] == ["R1"]
    # L'utente toglie la spunta: niente da ricordare, il pulsante si spegne.
    remembered.setCheckState(Qt.CheckState.Unchecked)
    assert dialog.selected_components() == []
    ok = dialog.buttons.button(dialog.buttons.StandardButton.Ok)
    assert not ok.isEnabled()
    dialog.close()


def test_archive_dialog_lists_and_deletes_entries(window, library):
    store = am.AcceptedModelStore()
    store.consolidate(_board(library), [library])
    dialog = AcceptedModelsDialog(store)
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 4).text() == "R_0603_1608Metric"
    assert not dialog.btn_delete.isEnabled()
    dialog.table.selectRow(0)
    assert dialog.btn_delete.isEnabled()
    dialog.btn_delete.click()
    assert dialog.table.rowCount() == 0
    assert store.entries() == []
    dialog.close()


def test_archive_dialog_reports_an_unreadable_file_instead_of_hiding_it(window, library):
    store = am.AcceptedModelStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b"non e' sqlite" * 20)
    dialog = AcceptedModelsDialog(store)
    assert dialog.table.rowCount() == 0
    assert "Archivio abbinamenti" in dialog.status_label.text()
    dialog.close()


def _enabled(button):
    """Stato proprio del pulsante: la scheda che lo contiene puo' essere spenta."""
    return button.isEnabledTo(button.parentWidget())


def test_accept_button_follows_the_generated_solid(window, monkeypatch):
    assert not _enabled(window.btn_accept_models)
    monkeypatch.setattr(window, "_cad_result_is_current", lambda: True)
    monkeypatch.setattr(window, "_show_generated_mesh", lambda path: None)
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: True)
    window._on_regen_3d_done("modello.step")
    assert _enabled(window.btn_accept_models)
    # Dati cambiati dopo la generazione: il solido visto non e' piu' quello.
    window._invalidate_generated_mesh()
    assert not _enabled(window.btn_accept_models)


def test_a_stale_generation_does_not_enable_the_button(window, monkeypatch):
    """Negativo: un risultato CAD tardivo descrive dati vecchi."""
    monkeypatch.setattr(window, "_cad_result_is_current", lambda: False)
    window._on_regen_3d_done("vecchio.step")
    assert not _enabled(window.btn_accept_models)


class _AcceptingDialog:
    """Sostituto del dialogo: conferma tutti i componenti spuntabili."""

    def __init__(self, components, parent=None, i18n=None):
        self.components = components

    def exec(self):
        return QDialog.DialogCode.Accepted

    def selected_components(self):
        return [c for c in self.components if am.eligibility(c)[0]]

    # retranslate_widgets riceve un oggetto senza figli: nulla da fare.
    def findChildren(self, *args):
        return []


def test_accepting_stores_the_board_and_says_so(window, library, monkeypatch):
    monkeypatch.setattr(window, "_photo_components_from_table", lambda: _board(library))
    monkeypatch.setattr(gui, "ConsolidateDialog", _AcceptingDialog)
    monkeypatch.setattr(km, "_search_roots", lambda library_root=None: [library])
    logged = []
    monkeypatch.setattr(window, "_append_log", logged.append)
    window._accept_models()
    [entry] = am.AcceptedModelStore().entries()
    assert entry["model_ref"] == R0603
    assert entry["source"] == window._component_name()
    # Il segnaposto U1 non arriva nemmeno all'archivio: il dialogo lo esclude.
    assert any("1 voci nuove, 0 conferme" in line for line in logged)
    assert "1" in window.label_3d_status.text()


def test_cancelling_the_dialog_stores_nothing(window, library, monkeypatch):
    class Cancelled(_AcceptingDialog):
        def exec(self):
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(window, "_photo_components_from_table", lambda: _board(library))
    monkeypatch.setattr(gui, "ConsolidateDialog", Cancelled)
    window._accept_models()
    assert am.AcceptedModelStore().entries() == []
    assert not am.AcceptedModelStore().path.exists()


def test_a_write_failure_is_reported_on_the_status_bar(window, library, monkeypatch):
    def fail(self, *args, **kwargs):
        raise am.AcceptedModelsError("disco pieno")

    monkeypatch.setattr(window, "_photo_components_from_table", lambda: _board(library))
    monkeypatch.setattr(gui, "ConsolidateDialog", _AcceptingDialog)
    monkeypatch.setattr(am.AcceptedModelStore, "consolidate", fail)
    window._accept_models()
    assert "disco pieno" in window.label_3d_status.text()


def test_the_archive_is_reachable_from_the_configuration_menu(window):
    assert window.action_accepted_models.isEnabled()
