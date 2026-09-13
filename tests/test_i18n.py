"""@brief Verify the SQLite-backed internationalization contract."""

import pytest

from kicadgen.services.analysis_profiles import ProfileStore
from kicadgen.services.i18n import I18n


def test_translation_catalog_seeds_all_supported_languages(tmp_path):
    """@brief Ensure a new profile database contains the navigation catalog."""
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    i18n = I18n(store, "fr")
    assert i18n.tr("tab.project") == "📁 Projet"
    assert i18n.tr("tab.model3d") == "🧊 Modèle 3D"


def test_translation_falls_back_to_english_and_persists_edits(tmp_path):
    """@brief Ensure missing locales fall back and edited text survives reopen."""
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    store.save_translation("custom.help", "de", "Benutzerdefinierte Hilfe")
    assert I18n(store, "de").tr("custom.help") == "Benutzerdefinierte Hilfe"
    assert I18n(store, "fr").tr("app.file") == "Fichier"
    assert I18n(store, "fr").tr("custom.help") == "custom.help"
    assert any(row["key"] == "custom.help" for row in store.list_translations("de"))


def test_existing_database_receives_new_bundled_translation_keys(tmp_path):
    """@brief Seed new UI keys without overwriting a user's existing edit."""
    database = tmp_path / "profiles.sqlite3"
    store = ProfileStore(database)
    store.save_translation("ui:Non disponibile", "fr", "Indisponible personnalisé")
    with store._connect() as connection:
        connection.execute(
            "DELETE FROM translations WHERE key=? AND language=?",
            ("ui:Vista superiore", "fr"),
        )

    reopened = ProfileStore(database)

    assert reopened.translation("ui:Vista superiore", "fr") == "Vue de dessus"
    assert reopened.translation("ui:Non disponibile", "fr") == "Indisponible personnalisé"


@pytest.mark.parametrize(
    ("language", "title", "compatible", "header", "no_limit", "selected", "cancel"),
    (
        ("it", "Modello solo per questa sessione", "Solo modelli compatibili",
         "Modello (id)", "nessun limite", "Modello scelto: anthropic/claude-sonnet-5", "Annulla"),
        ("en", "Model for this session only", "Compatible models only",
         "Model (ID)", "no limit", "Selected model: anthropic/claude-sonnet-5", "Cancel"),
        ("fr", "Modèle pour cette session uniquement", "Modèles compatibles",
         "Modèle (ID)", "aucune limite", "Modèle sélectionné : anthropic/claude-sonnet-5", "Annuler"),
        ("de", "Modell nur für diese Sitzung", "Nur kompatible Modelle",
         "Modell (ID)", "keine Begrenzung", "Ausgewähltes Modell: anthropic/claude-sonnet-5", "Abbrechen"),
    ),
)
def test_model_picker_uses_parent_language_without_touching_model_data(
        tmp_path, monkeypatch, language, title, compatible, header, no_limit,
        selected, cancel):
    """@brief Translate the nested model picker that executes inside its helper."""
    from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QWidget
    from kicadgen.gui.model_dialog import ModelPickerDialog

    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.i18n = I18n(ProfileStore(tmp_path / "models.sqlite3"), language)
    monkeypatch.setattr(ModelPickerDialog, "_load_models", lambda self, force_refresh=False: None)

    dialog = ModelPickerDialog("anthropic/claude-sonnet-5", parent,
                               title="Modello solo per questa sessione")

    assert dialog.windowTitle() == title
    assert dialog.check_compatible.text().startswith(compatible)
    assert dialog.table.horizontalHeaderItem(0).text() == header
    assert dialog.spin_max_cost.specialValueText() == no_limit
    assert dialog.label_selected.text() == selected
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).text() == cancel
    dialog.close()
    parent.close()
    app.processEvents()
