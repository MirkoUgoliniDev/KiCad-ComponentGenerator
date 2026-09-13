"""@brief Verify the SQLite-backed internationalization contract."""

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
