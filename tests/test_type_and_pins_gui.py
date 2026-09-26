"""@brief The component type says what the analysis will do; header pins are a 3D-only choice.

User decisions (2026-09-26): four types with clear names; «scheda con
componenti» types run the photo inventory, single components do not; the male
header pins are optional for both boards, OFF by default, and do not touch the
analysis prompt nor the footprint.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")

from kicadgen.services.analysis_profiles import ProfileStore
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
from tests.test_photo_inventory_gui import window, gui  # noqa: F401 - fixture condivisa


def _choose(window, key):  # noqa: F811
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData(key))


@pytest.mark.parametrize("key,board", [("single_tht", False), ("single_smd", False),
                                       ("aggregate_tht", True), ("aggregate_castellated", True)])
def test_the_pins_are_offered_only_for_boards(window, key, board):  # noqa: F811
    _choose(window, key)
    # La riga che descriveva l'analisi e' stata tolta dall'utente (2026-09-26).
    assert not hasattr(window, "label_type_effect")
    assert window.chk_header_pins.isHidden() is (not board)


def test_no_type_says_nothing_about_boards(window):  # noqa: F811
    """@brief Negativo: senza tipologia la disposizione rimasta non fa dire «scheda»."""
    _choose(window, "aggregate_castellated")
    _choose(window, "")
    assert window.chk_header_pins.isHidden()


def test_pins_are_off_by_default_and_follow_the_project(window, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gui, "save_config", lambda data: None)
    _choose(window, "aggregate_tht")
    assert not window.chk_header_pins.isChecked(), "spenti di default"
    assert window._pins_3d_kwargs() == {"show_pins_3d": False}
    revision = window._model_revision
    window.chk_header_pins.setChecked(True)
    assert window._show_pins_3d() is True
    assert window._pins_3d_kwargs() == {"show_pins_3d": True}
    assert window._model_revision > revision, "il 3D a video non ha i pin: e' da rigenerare"
    assert window._project_state()["options"]["show_pins_3d"] is True


def test_a_single_component_keeps_its_leads_whatever_the_choice(window, monkeypatch):  # noqa: F811
    """@brief Negativo: i piedini di un componente singolo non sono strip opzionali."""
    monkeypatch.setattr(gui, "save_config", lambda data: None)
    window._set_project_option("show_pins_3d", False)
    _choose(window, "single_tht")
    assert window._pins_3d_kwargs() == {}


def test_the_pins_choice_is_not_in_the_generation_options():
    assert not hasattr(gui.GenerationOptionsDialog, "show_pins_3d")


def test_the_new_names_reach_an_existing_database_unless_edited(tmp_path):
    """@brief Positivo: un database col nome vecchio riceve il nuovo. Negativo: un nome modificato resta."""
    database = tmp_path / "profiles.sqlite3"
    store = ProfileStore(database)
    old = "Componente aggregato a fori passanti (modulo/scheda)"
    with store._connect() as connection:
        connection.execute("UPDATE component_types SET label=? WHERE key='aggregate_tht'", (old,))
        connection.execute("UPDATE component_types SET label='La mia scheda' WHERE key='aggregate_castellated'")
        connection.execute("UPDATE translations SET text=? WHERE key='component_type.aggregate_tht' "
                           "AND language='it'", (old,))
    reopened = ProfileStore(database)
    labels = {row["key"]: row["label"] for row in reopened.list_types()}
    assert labels["aggregate_tht"] == "Scheda con componenti · connettori passanti"
    assert labels["aggregate_castellated"] == "La mia scheda"
    assert reopened.translation("component_type.aggregate_tht", "it") == \
        DEFAULT_TRANSLATIONS["component_type.aggregate_tht"]["it"]


def test_an_empty_optional_package_geometry_does_not_throw_away_the_extraction():
    """@brief Positivo: {} o «pins»: [] valgono «nessun pad esplicito», non un errore.

    Negativo: una geometria con pad non validi resta un errore (quella si'
    produrrebbe un footprint sbagliato).
    """
    from kicadgen.services import datasheet_extractor as ext
    base = dict(name="M", description="", tags="", confidence="medium", notes="",
                layout="matrix", rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=2.54,
                pad_diameter_mm=1.7, drill_mm=1.0)
    for empty in ({}, {"family": "unknown", "source_view": "top", "pins": []}):
        params = ext._params_from_tool_arguments(dict(base, package_geometry=empty))
        assert params.package_geometry is None
    with pytest.raises(ext.ExtractionError):
        ext._params_from_tool_arguments(dict(base, package_geometry={
            "family": "unknown", "pins": [{"number": "1"}]}))
