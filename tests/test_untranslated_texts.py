"""@brief Revisore 3, difetto 10: testi rimasti in italiano con la lingua inglese.

Il motivo del blocco senza progetto e il tooltip di «Estrai parametri» si
scrivevano in italiano a ogni cambio di nome; il tooltip della lista degli
elementi (una vista a elenco) non passava dalla ritraduzione; tre testi non
avevano voce di catalogo. Negativo: in italiano restano quelli di sempre.
"""
from PyQt6.QtWidgets import QGroupBox, QLabel

from test_photo_inventory_gui import window, gui  # noqa: F401 - fixture condivisa


def _switch(window, language):  # noqa: F811
    window.language_combo.setCurrentIndex(window.language_combo.findData(language))


def test_the_gate_reason_follows_the_language(window):  # noqa: F811
    _switch(window, "en")
    window.field_name.setText("")
    assert window.btn_extract_ai.toolTip() == "Create or open a project first (File menu)."
    assert window.docs_list.toolTip() == "Create or open a project first (File menu)."
    window.field_name.setText("Scheda")
    assert window.btn_extract_ai.toolTip().startswith("Start the extraction from the selected")
    # Un secondo cambio di lingua ritraduce dalla sorgente italiana, non dal
    # testo inglese appena mostrato.
    _switch(window, "fr")
    assert window.btn_extract_ai.toolTip().startswith("Lance l’extraction")
    _switch(window, "it")
    window.field_name.setText("")
    assert window.btn_extract_ai.toolTip() == "Crea o apri prima un progetto (menu File)."


def test_list_tooltip_group_tooltip_and_form_labels_are_translated(window):  # noqa: F811
    _switch(window, "en")
    assert window.ai_elements_list.toolTip() == "Items and outlines found during photo analysis"
    assert window.group_3d.toolTip() in (
        "FreeCAD found: the 3D model is generated together with the footprint.",
        "freecadcmd not found: install FreeCAD or set FREECAD_CMD_PATH.")
    labels = {label.text() for label in window.findChildren(QLabel)}
    assert "Half-hole inside the edge (0 = auto):" in labels
    assert "Through hole from the edge (0 = auto):" in labels
    assert not any("(0 = auto)" in text and ("bordo" in text) for text in labels)
    _switch(window, "it")
    assert window.ai_elements_list.toolTip() == \
        "Elementi e ingombri individuati durante l'analisi fotografica"
    assert isinstance(window.group_3d, QGroupBox)
