"""@brief Cambio, rinomina e chiusura di un progetto: nessun dato passa da un progetto all'altro.

Difetti 5-9 del revisore 2 (verifica del 2026-09-24), riprodotti prima della
correzione. Ogni regola ha un caso positivo e uno negativo che la regola deve
distinguere per ragioni strutturali (quale cartella, quale progetto), non per
un valore numerico.
"""
import json

from PyQt6.QtWidgets import QDialog

from test_photo_inventory_gui import gui
from kicadgen.gui.generation_options_dialog import GenerationOptionsDialog
from kicadgen.gui.project_dialogs import OpenProjectDialog, ProjectNameDialog
from kicadgen.gui.project_state import (PROJECT_CONFIG_NAME, read_project_config,
                                        write_project_config)


def _open(window, monkeypatch, name):  # noqa: F811
    monkeypatch.setattr(OpenProjectDialog, "selected_name", lambda self: name)
    monkeypatch.setattr(OpenProjectDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window._open_project()


def _name_dialog_returns(monkeypatch, name):
    def accept(self):
        self.field.setText(name)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(ProjectNameDialog, "exec", accept)


def _png() -> bytes:
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), "green").save(buffer, "PNG")
    return buffer.getvalue()


# ── 5: la memoria del progetto precedente ────────────────────────────────

def test_opening_another_project_forgets_the_photo_and_the_extraction(window, monkeypatch):  # noqa: F811
    """@brief Positivo: foto, sovrapposizione e ultima estrazione di A non arrivano a B.

    Negativo: cio' che B ha salvato (i suoi campi) torna com'era.
    """
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    write_project_config(root / "B", {"widgets": {"field_description": "modulo B"}})
    (root / "B" / "docs").mkdir()
    window._photo_png = window._photo_overlay_png = _png()
    window._last_extracted = object()
    window._reviewed_this_extraction = True
    window._refresh_edit_review_buttons()
    assert window.btn_edit_component_review_top.isVisibleTo(window)

    _open(window, monkeypatch, "B")
    assert window._photo_png is None and window._photo_overlay_png is None
    assert window._last_extracted is None and window._reviewed_this_extraction is False
    assert window._last_mesh_path is None and window.package_geometry is None
    assert not window.btn_edit_component_review_top.isVisibleTo(window)
    assert window.field_description.text() == "modulo B"


def test_a_new_project_forgets_the_photo_too(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    window._photo_png = _png()
    _name_dialog_returns(monkeypatch, "Fresco")
    window._new_project()
    assert window._photo_png is None


# ── 6: file rifiutato e rinomina ─────────────────────────────────────────

def test_a_refused_file_stays_untouched_after_a_rename(window, monkeypatch):  # noqa: F811
    """@brief Positivo: il file di versione piu' nuova resta com'e' anche col nome nuovo.

    Negativo: un file leggibile, dopo la rinomina, si aggiorna normalmente.
    """
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    newer = json.dumps({"format": "kicadgen-project", "version": 99, "state": {}})
    (root / "Nuovo" / "docs").mkdir(parents=True)
    (root / "Nuovo" / PROJECT_CONFIG_NAME).write_text(newer, encoding="utf-8")
    _open(window, monkeypatch, "Nuovo")
    _name_dialog_returns(monkeypatch, "Nuovo2")
    window._rename_project()
    assert window._component_name() == "Nuovo2"
    window.field_description.setText("modifica")
    assert not window._save_project_config()
    assert (root / "Nuovo2" / PROJECT_CONFIG_NAME).read_text(encoding="utf-8") == newer

    _name_dialog_returns(monkeypatch, "Leggibile")
    window._new_project()
    _name_dialog_returns(monkeypatch, "Leggibile2")
    window._rename_project()
    window.field_description.setText("altra modifica")
    assert window._save_project_config()
    assert read_project_config(root / "Leggibile2")["widgets"]["field_description"] == "altra modifica"


# ── 7: cambio della cartella di lavoro ───────────────────────────────────

def test_changing_the_work_folder_saves_and_closes_the_open_project(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Positivo: l'omonimo dell'altra cartella non viene toccato; il progetto
    aperto riceve le ultime modifiche e si chiude.

    Negativo: senza progetto aperto cambiare cartella non scrive nulla.
    """
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "P" / "docs").mkdir(parents=True)
    _open(window, monkeypatch, "P")
    window.field_description.setText("ultima modifica di P")
    other = tmp_path / "altra-cartella"
    write_project_config(other / "P", {"widgets": {"field_description": "ALTRO"}})
    window.field_library_root.setText(str(other))

    assert window._component_name() == ""
    assert read_project_config(other / "P")["widgets"]["field_description"] == "ALTRO"
    assert read_project_config(root / "P")["widgets"]["field_description"] == "ultima modifica di P"
    window.field_description.setText("dopo la chiusura")
    assert not window._save_project_config()
    assert read_project_config(other / "P")["widgets"]["field_description"] == "ALTRO"

    third = tmp_path / "terza-cartella"
    window.field_library_root.setText(str(third))
    assert not third.exists()


def test_the_work_folder_does_not_change_while_work_is_running(window, monkeypatch, tmp_path):  # noqa: F811
    """@brief Un lavoro in corso scrive nel progetto: la cartella di lavoro torna com'era."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "Occupato" / "docs").mkdir(parents=True)
    _open(window, monkeypatch, "Occupato")
    monkeypatch.setattr(window, "_background_work_running", lambda: True)
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *args, **kwargs: None)
    window.field_library_root.setText(str(tmp_path / "altrove"))
    assert window._library_root() == root
    assert window._component_name() == "Occupato"


# ── 8: le impostazioni del progetto non diventano generali ───────────────

def _another_component_type(window):  # noqa: F811
    combo = window.combo_component_type
    return next(combo.itemData(i) for i in range(combo.count())
                if combo.itemData(i) and combo.itemData(i) != combo.currentData())


def test_opening_a_project_never_writes_config_json(window, monkeypatch):  # noqa: F811
    """@brief Positivo: aprire un progetto con altra tipologia e opzioni non cambia le
    impostazioni generali di config.json.

    Negativo: la stessa tipologia scelta a mano dall'utente si salva come generale.
    @note Dal 26/09 (PIANO-FIX 1) aprire un progetto aggiorna l'elenco dei
        progetti recenti, che sta in config.json: quella e' l'unica chiave che
        un'apertura puo' scrivere.
    """
    from kicadgen.services import recent_projects
    written = []
    monkeypatch.setattr(gui, "save_config", lambda config: written.append(dict(config)))
    general = {key: value for key, value in window._config.items()
               if key != recent_projects.CONFIG_KEY}
    root = window._library_root()
    other = _another_component_type(window)
    write_project_config(root / "T", {
        "widgets": {"combo_component_type": {"data": other, "text": ""}},
        "options": {"include_nc_pins": False, "silk_pin_labels": True}})
    (root / "T" / "docs").mkdir()
    _open(window, monkeypatch, "T")
    assert window.combo_component_type.currentData() == other
    assert window._include_nc_pins() is False and window._silk_pin_labels() is True
    for config in written:
        assert {key: value for key, value in config.items()
                if key != recent_projects.CONFIG_KEY} == general
    assert written and str(root / "T") in written[-1][recent_projects.CONFIG_KEY]

    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData(_another_component_type(window)))
    assert written[-1]["component_type"] == combo.currentData()


def test_project_options_do_not_leak_through_a_later_save(window, monkeypatch):  # noqa: F811
    """@brief Positivo: un save_config per altri motivi non porta con se' le opzioni del progetto.

    Negativo: un'opzione cambiata dall'utente in Opzioni di generazione vale
    per il progetto e come generale.
    """
    saved = []
    monkeypatch.setattr(gui, "save_config", lambda config: saved.append(dict(config)))
    general_nc = window._config.get("include_nc_pins", True)
    root = window._library_root()
    write_project_config(root / "T", {"options": {"include_nc_pins": not general_nc}})
    (root / "T" / "docs").mkdir()
    _open(window, monkeypatch, "T")
    window._on_grid_toggled(True)
    assert saved and saved[-1].get("include_nc_pins", True) is general_nc

    # Il dialogo si apre col valore del PROGETTO; l'utente lo riporta a
    # quello generale, come farebbe lui.
    def choose(dialog):
        assert dialog.include_nc_checkbox.isChecked() is (not general_nc)
        dialog.include_nc_checkbox.setChecked(general_nc)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(GenerationOptionsDialog, "exec", choose)
    window._edit_generation_options()
    assert saved[-1]["include_nc_pins"] is general_nc
    assert window._include_nc_pins() is general_nc
    assert window._general_project_state["options"]["include_nc_pins"] is general_nc


def test_an_option_the_project_does_not_declare_falls_back_to_the_general_one(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    write_project_config(root / "SenzaOpzioni", {"widgets": {}})
    (root / "SenzaOpzioni" / "docs").mkdir()
    window._config["silk_pin_labels"] = True
    _open(window, monkeypatch, "SenzaOpzioni")
    assert window._silk_pin_labels() is True
    window._config["silk_pin_labels"] = False
    assert window._silk_pin_labels() is False


# ── 9: rinomina che cambia solo le maiuscole ─────────────────────────────

def test_a_rename_that_changes_only_the_case_really_renames(window, monkeypatch):  # noqa: F811
    """@brief Positivo: «Scheda» diventa «SCHEDA», cartella compresa, contenuto intatto.

    Negativo: lo stesso nome identico non tocca nulla.
    """
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "Scheda" / "docs").mkdir(parents=True)
    (root / "Scheda" / "docs" / "foto.jpg").write_bytes(b"x")
    _open(window, monkeypatch, "Scheda")
    _name_dialog_returns(monkeypatch, "SCHEDA")
    window._rename_project()
    assert window._component_name() == "SCHEDA"
    assert _named_like(root, "scheda") == ["SCHEDA"]
    assert (root / "SCHEDA" / "docs" / "foto.jpg").read_bytes() == b"x"
    assert read_project_config(root / "SCHEDA") is not None

    assert window._migrate_docs_to_new_name("SCHEDA") == "SCHEDA"
    assert _named_like(root, "scheda") == ["SCHEDA"]


def _named_like(root, name):
    """Cartelle di `root` con quel nome a meno delle maiuscole, piu' le temporanee rimaste."""
    return sorted(p.name for p in root.iterdir()
                  if p.name.casefold() == name or "rinomina" in p.name)


# ── Rinomina durante un lavoro in corso ──────────────────────────────────

def test_a_rename_waits_for_the_running_work(window, monkeypatch):  # noqa: F811
    """@brief Positivo: con un lavoro in corso la rinomina non parte. Negativo: a lavoro finito si'.

    Prima valeva per «Usa questo nome», tolto il 2026-09-24 (utente): il nome
    si cambia solo da «Rinomina progetto…», con la stessa guardia.
    """
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "Vecchio" / "docs").mkdir(parents=True)
    _open(window, monkeypatch, "Vecchio")
    busy = {"value": True}
    monkeypatch.setattr(window, "_background_work_running", lambda: busy["value"])
    monkeypatch.setattr(gui.QMessageBox, "information", lambda *args, **kwargs: None)
    _name_dialog_returns(monkeypatch, "Proposto")
    window._rename_project()
    assert window._component_name() == "Vecchio"
    busy["value"] = False
    window._rename_project()
    assert window._component_name() == "Proposto"

# ── ramo «documenti in conflitto» di `_migrate_docs_to_new_name` ─────────

def test_a_rename_onto_documents_with_the_same_names_moves_nothing(window, monkeypatch):  # noqa: F811
    """@brief Ramo difensivo: la cartella di destinazione ha gia' documenti omonimi.

    Il dialogo rifiuta i nomi di progetti esistenti (`name_problem`), quindi
    qui si arriva solo se la cartella nasce fra il dialogo e lo spostamento:
    si chiama il metodo direttamente (revisore 4, difetto 14). Positivo: resta
    il nome vecchio, nessun file toccato, il motivo nel log. Negativo: senza
    omonimi gli stessi documenti si spostano.
    """
    from kicadgen.services.document_roles import MANIFEST
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "Origine" / "docs").mkdir(parents=True)
    _open(window, monkeypatch, "Origine")
    source = root / "Origine" / "docs"
    (source / MANIFEST).write_text("{}", encoding="utf-8")
    (source / "foto.jpg").write_bytes(b"mia")
    (root / "Occupata" / "docs").mkdir(parents=True)
    (root / "Occupata" / "docs" / "foto.jpg").write_bytes(b"altrui")

    assert window._migrate_docs_to_new_name("Occupata") == "Origine"
    assert (source / "foto.jpg").read_bytes() == b"mia"
    assert (root / "Occupata" / "docs" / "foto.jpg").read_bytes() == b"altrui"
    assert "Occupata" in window.ai_progress_text.toPlainText()

    assert window._migrate_docs_to_new_name("Libera") == "Libera"
    assert (root / "Libera" / "docs" / "foto.jpg").read_bytes() == b"mia"
    assert not (source / "foto.jpg").exists()
