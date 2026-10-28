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
from PyQt6.QtWidgets import QApplication, QDialog

from kicadgen.cad3d import kicad_models as km
from kicadgen.gui import main_window as gui
from kicadgen.gui.accepted_models_dialog import AcceptedModelsDialog, ConsolidateDialog
from kicadgen.services import accepted_models as am


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
    remembered = dialog.table.item(0, 1)
    assert dialog.table.rowCount() == 1
    assert remembered.checkState() == Qt.CheckState.Checked
    assert "Senza modello da ricordare: 1" in dialog.summary.text()
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

    # CONTRACT CHANGED 2026-10-27 (review B6): the window releases every modal
    # it opens (`MainWindow._run_modal` calls `deleteLater`), so a fake dialog
    # must answer it like the QDialog it stands for.
    def deleteLater(self):
        pass

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


def test_accepting_a_verified_component_saves_its_calibrated_photo_crop(
        window, library, monkeypatch):
    import cv2
    import numpy as np

    component = dict(_board(library)[0], x_mm=0.0, y_mm=0.0, rotation_deg=0.0)
    monkeypatch.setattr(window, "_photo_components_from_table", lambda: [component])
    monkeypatch.setattr(window, "_decoded_component_photo",
                        lambda: np.full((400, 400, 3), 40, dtype=np.uint8))
    monkeypatch.setattr(window, "_component_photo_board_size", lambda: (10.0, 10.0))
    monkeypatch.setattr(gui, "ConsolidateDialog", _AcceptingDialog)
    monkeypatch.setattr(km, "_search_roots", lambda library_root=None: [library])

    window._accept_models()

    [entry] = am.AcceptedModelStore().entries()
    assert entry["image_count"] == 1
    [example] = am.AcceptedModelStore().images(entry["id"])
    decoded = cv2.imdecode(np.frombuffer(example["image_png"], np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == (96, 96, 3)
    archive = AcceptedModelsDialog(am.AcceptedModelStore())
    assert archive.table.item(0, 7).text() == "1"
    archive.table.selectRow(0)
    assert archive.btn_preview.isEnabled()
    archive.close()


def test_crop_preview_can_browse_all_examples_and_shows_component_id(
        window, library, monkeypatch):
    import numpy as np

    store = am.AcceptedModelStore()
    crop1 = np.zeros((96, 96, 3), dtype=np.uint8)
    crop1[:, :40] = 80
    crop2 = np.zeros((96, 96, 3), dtype=np.uint8)
    crop2[:, 50:] = 180
    first = dict(_board(library)[0], name="C-10", _accepted_image_png=b"", _accepted_image_descriptor=b"")
    second = dict(first, name="C-11")
    for component, crop in ((first, crop1), (second, crop2)):
        import cv2
        from kicadgen.services.component_image_match import describe_crop, descriptor_bytes
        ok, encoded = cv2.imencode(".png", crop)
        assert ok
        component["_accepted_image_png"] = encoded.tobytes()
        component["_accepted_image_descriptor"] = descriptor_bytes(describe_crop(crop))
    store.consolidate([first, second], [library])
    [entry] = store.entries()
    archive = AcceptedModelsDialog(store)
    seen = {}

    def inspect_preview(dialog):
        from PyQt6.QtWidgets import QLabel, QPushButton
        legend = dialog.findChild(QLabel, "cropLegend")
        assert "C-10" in legend.text() and "ritaglio 1 di 2" in legend.text()
        dialog.findChild(QPushButton, "cropNext").click()
        assert "C-11" in legend.text() and "ritaglio 2 di 2" in legend.text()
        seen["navigated"] = True
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", inspect_preview)
    archive._show_entry_crop(entry["id"])
    assert seen["navigated"]
    archive.close()


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


# ── Icona «archivio» nella scheda 3D (utente, 2026-09-24) ───────────────────

C0805 = "Capacitor_SMD.3dshapes/C_0805_2012Metric.step"


def _two_entries(library):
    """Archivio con due voci: un resistore 0603 e un condensatore 0805."""
    (library / "Capacitor_SMD.3dshapes").mkdir(parents=True, exist_ok=True)
    (library / C0805).write_text("ISO-10303-21;\n", encoding="utf-8")
    store = am.AcceptedModelStore()
    store.consolidate(_board(library), [library])
    store.consolidate([{"name": "C9", "kind": "capacitor", "length_mm": 2.0, "width_mm": 1.25,
                        "bbox_basis": "body", "model_path": str(library / C0805),
                        "model_match": {"status": "auto_matched"}}], [library])
    return store


def test_the_entries_this_board_uses_come_first_and_are_highlighted(window, library):
    """Positivo: il report finale di R7 usa la voce 0603: in cima, con nome nel tooltip.

    Negativo: la voce 0805 viene da un'altra scheda e nessun corpo di questa la
    riceverebbe (categoria diversa): resta sotto, senza evidenziazione.
    """
    store = _two_entries(library)
    board = [{"name": "R7", "kind": "resistor", "length_mm": 1.62, "width_mm": 0.82,
              "bbox_basis": "body"},
             {"name": "U1", "kind": "ic", "length_mm": 5, "width_mm": 4}]
    entry = next(e for e in store.entries() if e["model_name"] == "R_0603_1608Metric")
    board[0]["model_match"] = {"status": "library_matched", "attachment": "accepted_library",
                               "accepted_entry": {"id": entry["id"]}}
    dialog = AcceptedModelsDialog(store, components=board, roots=[library])
    assert dialog.table.rowCount() == 2
    first, second = dialog.table.item(0, 4), dialog.table.item(1, 4)
    assert first.text() == "R_0603_1608Metric" and second.text() == "C_0805_2012Metric"
    assert first.background().color().alpha() > 0
    assert "R7" in first.toolTip()
    assert second.background().style() == Qt.BrushStyle.NoBrush
    assert "Usate da questa scheda: 1" in dialog.status_label.text()
    _dispose(dialog)


def test_from_the_menu_nothing_is_highlighted(window, library):
    store = _two_entries(library)
    dialog = AcceptedModelsDialog(store)
    assert all(dialog.table.item(row, 0).background().style() == Qt.BrushStyle.NoBrush
               for row in range(dialog.table.rowCount()))
    assert "questa scheda" not in dialog.status_label.text()
    _dispose(dialog)


def test_an_entry_whose_file_vanished_is_not_claimed_for_the_board(window, library):
    """Negativo: stessa regola della generazione, che salta i file spariti."""
    store = _two_entries(library)
    (library / R0603).unlink()
    board = [{"name": "R7", "kind": "resistor", "length_mm": 1.62, "width_mm": 0.82,
              "bbox_basis": "body"}]
    dialog = AcceptedModelsDialog(store, components=board, roots=[library])
    assert "Usate da questa scheda: 0" in dialog.status_label.text()
    _dispose(dialog)


def test_the_3d_tab_icon_is_always_on_and_opens_the_board_view(window, monkeypatch):
    button = window.btn_accepted_archive
    assert button.isEnabledTo(window.tabs.widget(gui.TAB_MODEL_3D))
    first, _, rest = button.toolTip().partition("\n")
    assert first and rest
    opened = []

    def fake_exec(dialog):
        opened.append(dialog)
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(AcceptedModelsDialog, "exec", fake_exec)
    monkeypatch.setattr(window, "_photo_components_from_table",
                        lambda: [{"name": "R7", "kind": "resistor"}])
    # Stato che lascia un'estrazione vera (2026-09-25): le schede di anteprima
    # ora seguono i dati a ogni cambio di scheda, non la sola chiamata interna.
    window._acquisition_completed = True
    window._set_acquisition_tabs_enabled(True)  # la scheda 3D, come dopo un'estrazione
    button.click()
    assert len(opened) == 1 and opened[0]._components == [{"name": "R7", "kind": "resistor"}]
    _dispose(opened[0])


def _dispose(widget):
    """Distruzione ordinata: un widget Qt lasciato al garbage collector puo'
    essere distrutto dopo la QApplication e far cadere il processo all'uscita
    (codice 139, visto il 2026-09-24)."""
    widget.close()
    widget.setParent(None)
    widget.deleteLater()
    QApplication.processEvents()


def test_a_model_confirmed_by_hand_is_not_claimed_from_the_archive(window, library):
    """Negativo (audit 2026-09-25): la generazione non consulta l'archivio per un
    modello confermato o rifiutato a mano; la finestra non deve dire il contrario."""
    store = _two_entries(library)
    board = [{"name": "R7", "kind": "resistor", "length_mm": 1.62, "width_mm": 0.82,
              "bbox_basis": "body", "model_selection": {"status": "confirmed"}}]
    dialog = AcceptedModelsDialog(store, components=board, roots=[library])
    assert "Usate da questa scheda: 0" in dialog.status_label.text()
    _dispose(dialog)


def test_the_model_path_tooltip_survives_the_highlight(window, library):
    store = _two_entries(library)
    board = [{"name": "R7", "kind": "resistor", "length_mm": 1.62, "width_mm": 0.82,
              "bbox_basis": "body"}]
    entry = next(e for e in store.entries() if e["model_name"] == "R_0603_1608Metric")
    board[0]["model_match"] = {"status": "library_matched", "attachment": "accepted_library",
                               "accepted_entry": {"id": entry["id"]}}
    dialog = AcceptedModelsDialog(store, components=board, roots=[library])
    tip = dialog.table.item(0, 4).toolTip()
    assert R0603 in tip.replace("\\", "/") and "R7" in tip
    _dispose(dialog)


def test_excluded_rows_are_counted_but_not_listed(window, library):
    dialog = ConsolidateDialog(_board(library))
    assert dialog.table.rowCount() == 1
    assert 'Senza modello da ricordare: 1' in dialog.summary.text()
    _dispose(dialog)


def test_geometric_compatibility_alone_does_not_claim_archive_use(window, library):
    store = _two_entries(library)
    board = [{"name": "R7", "kind": "resistor", "length_mm": 1.62, "width_mm": .82,
              "bbox_basis": "body"}]
    dialog = AcceptedModelsDialog(store, components=board, roots=[library])
    assert "Usate da questa scheda: 0" in dialog.status_label.text()
    _dispose(dialog)


def test_failed_import_does_not_claim_archive_use(window, library):
    store = _two_entries(library)
    entry = store.entries()[0]
    board = [{"name": "R7", "model_match": {"status": "import_failed",
              "attachment": "accepted_library", "accepted_entry": {"id": entry["id"]}}}]
    dialog = AcceptedModelsDialog(store, components=board, roots=[library])
    assert "Usate da questa scheda: 0" in dialog.status_label.text()
    _dispose(dialog)


def test_corrupt_crop_clears_previous_image_and_navigation_still_works(window, library, monkeypatch):
    import cv2
    import numpy as np
    from PyQt6.QtWidgets import QLabel, QPushButton
    from kicadgen.services.accepted_models import IMAGE_CROP_VERSION
    store = _two_entries(library)
    entry = store.entries()[0]
    ok, png = cv2.imencode(".png", np.zeros((24, 24, 3), dtype=np.uint8))
    assert ok
    examples = [dict(id=1, image_png=png.tobytes(), crop_version=IMAGE_CROP_VERSION),
                dict(id=2, image_png=b"invalid", crop_version=IMAGE_CROP_VERSION)]
    monkeypatch.setattr(store, "images", lambda _: examples)
    archive = AcceptedModelsDialog(store)

    def inspect(dialog):
        previous = dialog.findChild(QPushButton, "cropPrevious")
        next_image = dialog.findChild(QPushButton, "cropNext")
        labels = [label for label in dialog.findChildren(QLabel)
                  if label.objectName() != "cropLegend"]
        assert any(label.pixmap() is not None and not label.pixmap().isNull() for label in labels)
        next_image.click()
        assert previous.isEnabled() and not next_image.isEnabled()
        assert all(label.pixmap() is None or label.pixmap().isNull() for label in labels)
        assert "non leggibile" in dialog.findChild(QLabel, "cropLegend").text()
        previous.click()
        assert next_image.isEnabled() and not previous.isEnabled()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", inspect)
    archive._show_entry_crop(entry["id"])
    _dispose(archive)


def test_single_crop_hides_navigation(window, library, monkeypatch):
    import cv2
    import numpy as np
    from PyQt6.QtWidgets import QPushButton
    from kicadgen.services.accepted_models import IMAGE_CROP_VERSION
    store = _two_entries(library)
    entry = store.entries()[0]
    png = cv2.imencode('.png', np.zeros((24, 24, 3), dtype=np.uint8))[1].tobytes()
    monkeypatch.setattr(store, 'images', lambda _: [dict(id=1, image_png=png, crop_version=IMAGE_CROP_VERSION)])
    archive = AcceptedModelsDialog(store)
    def inspect(dialog):
        assert dialog.findChild(QPushButton, 'cropPrevious').isHidden()
        assert dialog.findChild(QPushButton, 'cropNext').isHidden()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, 'exec', inspect)
    archive._show_entry_crop(entry['id'])
    _dispose(archive)


@pytest.mark.parametrize('status', ['unresolved', 'import_failed', 'review_required'])
def test_unattached_candidates_cannot_be_consolidated_even_with_stale_path(window, library, status):
    board = _board(library)
    board[0]['model_match']['status'] = status
    dialog = ConsolidateDialog(board)
    assert dialog.table.rowCount() == 0
    assert dialog.selected_components() == []
    assert not dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()
    store = am.AcceptedModelStore()
    assert store.consolidate(board, [library]).stored == 0
    _dispose(dialog)
