"""@brief Notes saved before 2026-11-12 carry the Italian type-block markers: every reader still finds them.

The type instructions became English with English markers; a project saved
before keeps «--- Tipologia componente (generato, modificabile) ---» in its
notes. The photo inventory must strip that block and the window must replace
it on a type change, as it does with the new markers.
"""
from kicadgen.services import component_types
from kicadgen.services.photo_global_inventory import _inventory_context
from tests.test_photo_inventory_gui import gui  # noqa: F401 - window fixture

OLD_START, OLD_END = component_types.LEGACY_BLOCK_MARKERS[0]


def test_the_inventory_context_strips_an_italian_block_and_keeps_the_user_text():
    """@brief Positive (old markers) and negative (the user's own words stay)."""
    notes = f"Nota mia prima.\n{OLD_START}\nTIPOLOGIA: vecchio blocco\n{OLD_END}\nNota mia dopo."
    context = _inventory_context(notes)
    assert OLD_START not in context and "vecchio blocco" not in context
    assert "Nota mia prima." in context and "Nota mia dopo." in context
    new = f"{component_types.BLOCK_START}\nTYPE: new block\n{component_types.BLOCK_END}\nmine"
    assert _inventory_context(new) == "mine"


def test_a_type_change_replaces_an_italian_block_saved_in_an_old_project(window):  # noqa: F811
    """@brief Positive: the old block goes, the English one comes, the user's note stays."""
    combo = window.combo_component_type
    window.field_component_prompt.setPlainText(
        f"Nota dell'utente.\n{OLD_START}\nTIPOLOGIA: testo italiano\n{OLD_END}")
    combo.setCurrentIndex(combo.findData("single_tht"))
    text = window.field_component_prompt.toPlainText()
    assert OLD_START not in text and "testo italiano" not in text
    assert component_types.BLOCK_START in text and "Nota dell'utente." in text
