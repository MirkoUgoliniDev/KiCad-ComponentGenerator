"""@brief Coherent product identity (PIANO-FIX item 3, 2026-09-26)."""
from kicadgen import __version__
from tests.test_photo_inventory_gui import gui


def test_the_title_shows_project_and_version(window):  # noqa: F811
    window.field_name.setText("")
    assert window.windowTitle() == f"KiCadGen {__version__}"
    window.field_name.setText("YOL")
    assert window.windowTitle() == f"YOL — KiCadGen {__version__}"
    assert "KiCadGen-Tool" not in window.windowTitle()


def test_the_about_entry_is_italian_in_the_italian_menu(window):  # noqa: F811
    """@brief Negativo: nessuna parola inglese nel menu italiano."""
    assert window.action_about.text() == "Informazioni su KiCadGen"
    assert window.tabs.tabText(gui.TAB_PREVIEW) == "Anteprima footprint"


def test_an_old_database_gets_the_corrected_texts(tmp_path):
    """@brief Positivo: i testi vecchi rimasti uguali all'originale vengono sostituiti."""
    from kicadgen.services.analysis_profiles import ProfileStore
    database = tmp_path / "profiles.sqlite3"
    store = ProfileStore(database)
    with store._connect() as connection:
        connection.execute("UPDATE translations SET text='Anteprima FootPrint' "
                           "WHERE key='tab.preview.name' AND language='it'")
    assert ProfileStore(database).translation("tab.preview.name", "it") == "Anteprima footprint"
