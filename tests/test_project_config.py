"""@brief `project_config.json`: le impostazioni del progetto nella sua cartella.

Decisioni dell'utente del 2026-09-24 («ok proposte»): dentro il progetto vanno
campi, tabelle, modelli di sessione e opzioni di generazione; aprendo un
progetto valgono le sue impostazioni; un progetto nuovo parte dalle
impostazioni generali; si salva da solo.

Ogni regola ha il suo negativo.
"""
import json

import pytest
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                             QGroupBox, QLineEdit, QSpinBox, QTextEdit)

from test_photo_inventory_gui import window, gui  # noqa: F401 - fixture condivisa
from kicadgen.gui.project_dialogs import OpenProjectDialog, ProjectNameDialog
from kicadgen.gui.project_state import (PROJECT_CONFIG_NAME, PROJECT_CONFIG_VERSION,
                                        ProjectConfigError, read_project_config,
                                        set_widget_value, widget_value,
                                        write_project_config)


# ── Modulo: valori dei widget e file ─────────────────────────────────────

@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_every_widget_kind_goes_round_trip(app):
    line, text, spin, dspin, check = QLineEdit("a"), QTextEdit(), QSpinBox(), QDoubleSpinBox(), QCheckBox()
    group = QGroupBox()
    group.setCheckable(True)
    combo = QComboBox()
    combo.addItem("Uno", "one")
    combo.addItem("Due", "two")
    text.setPlainText("prompt")
    spin.setValue(7)
    dspin.setValue(2.54)
    check.setChecked(True)
    combo.setCurrentIndex(1)
    widgets = [line, text, spin, dspin, check, group, combo]
    saved = [json.loads(json.dumps(widget_value(w))) for w in widgets]
    line.setText("")
    text.setPlainText("")
    spin.setValue(0)
    dspin.setValue(0)
    check.setChecked(False)
    group.setChecked(False)
    combo.setCurrentIndex(0)
    assert all(set_widget_value(w, v) for w, v in zip(widgets, saved))
    assert [widget_value(w) for w in widgets] == saved


def test_a_value_that_does_not_fit_leaves_the_widget_alone(app):
    """@brief Negativo: fuori scala, tipo sbagliato o voce sparita non cambiano il campo."""
    spin = QSpinBox()
    spin.setRange(1, 10)
    spin.setValue(3)
    combo = QComboBox()
    combo.addItem("Uno", "one")
    assert not set_widget_value(spin, 99) and spin.value() == 3
    assert not set_widget_value(spin, "3") and spin.value() == 3
    assert not set_widget_value(spin, float("nan"))
    assert not set_widget_value(combo, {"data": "sparito", "text": "Sparito"})
    assert combo.currentIndex() == 0


def test_the_file_round_trips_and_a_missing_file_is_no_error(tmp_path):
    assert read_project_config(tmp_path) is None
    write_project_config(tmp_path, {"widgets": {"field_rows": 4}})
    assert read_project_config(tmp_path) == {"widgets": {"field_rows": 4}}
    assert not list(tmp_path.glob(".project_config-*.tmp"))


@pytest.mark.parametrize("content", [
    "{ non e' json",
    json.dumps({"format": "altro", "version": 1, "state": {}}),
    json.dumps({"format": "kicadgen-project", "version": PROJECT_CONFIG_VERSION + 1, "state": {}}),
    json.dumps({"format": "kicadgen-project", "version": 1}),
])
def test_a_file_that_must_not_be_applied_is_refused(tmp_path, content):
    (tmp_path / PROJECT_CONFIG_NAME).write_text(content, encoding="utf-8")
    with pytest.raises(ProjectConfigError):
        read_project_config(tmp_path)


def test_nothing_is_written_when_the_state_is_not_json(tmp_path):
    with pytest.raises(ValueError):
        write_project_config(tmp_path, {"x": float("nan")})
    assert not (tmp_path / PROJECT_CONFIG_NAME).exists()


# ── Finestra: giro completo ──────────────────────────────────────────────

def _open(window, monkeypatch, name):  # noqa: F811
    monkeypatch.setattr(OpenProjectDialog, "selected_name", lambda self: name)
    monkeypatch.setattr(OpenProjectDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window._open_project()


def _new(window, monkeypatch, name):  # noqa: F811
    def accept(self):
        self.field.setText(name)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(ProjectNameDialog, "exec", accept)
    window._new_project()


def test_a_project_reopens_with_its_settings_and_a_new_one_with_the_general_ones(
        window, monkeypatch):  # noqa: F811
    """@brief Positivo: riaprendo A tornano i suoi valori. Negativo: B nuovo non li eredita."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    default_rows = window._general_project_state["widgets"]["field_rows"]
    _new(window, monkeypatch, "ProgettoA")
    window.field_rows.setValue(default_rows + 3)
    window.field_component_prompt.setPlainText("note del progetto A")
    window.field_description.setText("modulo di prova A")
    # Dal difetto 8 (revisore 2) le opzioni del progetto stanno in una
    # sovrapposizione propria e non in `_config`: si cambiano come l'utente,
    # dalle Opzioni di generazione (`_set_project_option`), e si leggono
    # dagli accessori.
    window._set_project_option("include_nc_pins", False)
    general_nc = window._config.get("include_nc_pins")
    # La scelta aggiorna anche la fotografia generale: per vedere che B non
    # eredita il valore di A, la generale si riporta a quella di avvio.
    window._general_project_state["options"]["include_nc_pins"] = True
    assert window._save_project_config()
    saved = json.loads((window._project_dir() / PROJECT_CONFIG_NAME).read_text(encoding="utf-8"))
    assert saved["state"]["widgets"]["field_rows"] == default_rows + 3

    _new(window, monkeypatch, "ProgettoB")
    assert window.field_rows.value() == default_rows
    assert window.field_component_prompt.toPlainText() != "note del progetto A"
    assert window.field_description.text() != "modulo di prova A"
    assert window._include_nc_pins() is True
    assert general_nc is False  # la scelta dall'interfaccia vale anche come generale

    _open(window, monkeypatch, "ProgettoA")
    assert window._component_name() == "ProgettoA"
    assert window.field_rows.value() == default_rows + 3
    assert window.field_component_prompt.toPlainText() == "note del progetto A"
    assert window.field_description.text() == "modulo di prova A"
    assert window._include_nc_pins() is False


def test_the_sides_table_and_the_contact_style_come_back(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    _new(window, monkeypatch, "Castellated")
    window.group_castellated.setChecked(True)
    window._sides.clear()
    window._add_side_row({"edge": "left", "count": 7, "pitch_mm": 1.27,
                          "contact_style": "through_hole"})
    before = window._serialised_sides()
    window._save_project_config()
    _new(window, monkeypatch, "Altro")
    assert window._serialised_sides() != before
    _open(window, monkeypatch, "Castellated")
    assert window._serialised_sides() == before


def test_it_saves_only_what_changed(window, monkeypatch):  # noqa: F811
    """@brief Il salvataggio automatico non riscrive un file uguale."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    _new(window, monkeypatch, "Stabile")
    assert not window._save_project_config()
    window.field_tags.setText("altre parole")
    assert window._save_project_config()
    assert not window._save_project_config()


def test_no_api_key_ever_reaches_the_project_file(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-segreta-di-prova")
    _new(window, monkeypatch, "Segreti")
    text = (window._project_dir() / PROJECT_CONFIG_NAME).read_text(encoding="utf-8")
    assert "sk-segreta-di-prova" not in text
    assert "api_key" not in text.lower()


def test_an_unreadable_file_is_neither_applied_nor_overwritten(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "Rotto" / "docs").mkdir(parents=True)
    broken = root / "Rotto" / PROJECT_CONFIG_NAME
    broken.write_text("{ rotto", encoding="utf-8")
    _open(window, monkeypatch, "Rotto")
    assert "non applicate" in window.ai_progress_text.toPlainText()
    window.field_tags.setText("una modifica")
    assert not window._save_project_config()
    assert broken.read_text(encoding="utf-8") == "{ rotto"


def test_the_settings_follow_a_rename(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    _new(window, monkeypatch, "VecchioNome")
    window.field_tags.setText("etichette")
    window._save_project_config()
    def accept(self):
        self.field.setText("NuovoNome")
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(ProjectNameDialog, "exec", accept)
    window._rename_project()
    assert window._component_name() == "NuovoNome"
    moved = read_project_config(window._project_dir())
    assert moved and moved["widgets"]["field_tags"] == "etichette"
    assert not (window._library_root() / "VecchioNome" / PROJECT_CONFIG_NAME).exists()


def test_a_failure_while_reading_the_state_never_reaches_the_timer(window, monkeypatch):  # noqa: F811
    """@brief Negativo: un errore nello stato non esce dallo slot del timer (chiuderebbe l'app)."""
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    _new(window, monkeypatch, "Fragile")

    def broken():
        raise KeyError("campo sparito")
    monkeypatch.setattr(window, "_project_state", broken)
    assert window._save_project_config() is False
    assert "non leggibili" in window.ai_progress_text.toPlainText()


def test_opening_a_project_without_the_file_creates_it(window, monkeypatch):  # noqa: F811
    """@brief Positivo: un progetto nato prima del file lo riceve all'apertura.

    Negativo: un file illeggibile non viene sostituito (vedi il test sopra).
    """
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    root = window._library_root()
    (root / "Vecchio" / "docs").mkdir(parents=True)
    assert not (root / "Vecchio" / PROJECT_CONFIG_NAME).exists()
    _open(window, monkeypatch, "Vecchio")
    assert read_project_config(root / "Vecchio") is not None
