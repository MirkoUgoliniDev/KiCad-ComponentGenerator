"""@brief Revisore 3, difetto 10: testi rimasti in italiano con la lingua inglese.

Il motivo del blocco senza progetto e il tooltip di «Estrai parametri» si
scrivevano in italiano a ogni cambio di nome; il tooltip della lista degli
elementi (una vista a elenco) non passava dalla ritraduzione; tre testi non
avevano voce di catalogo. Negativo: in italiano restano quelli di sempre.
"""
from PyQt6.QtWidgets import QGroupBox, QLabel

from test_photo_inventory_gui import gui


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
    # Il gruppo «Modello 3D» e le etichette dei fori stavano nel tab Footprint
    # 2D, tolto il 2026-09-26: nessuna etichetta italiana resta a vista.
    labels = {label.text() for label in window.findChildren(QLabel)}
    assert not any("(0 = auto)" in text and ("bordo" in text) for text in labels)
    _switch(window, "it")
    assert window.ai_elements_list.toolTip() == \
        "Elementi e ingombri individuati durante l'analisi fotografica"
    # Il 3D non e' piu' un gruppo di widget: e' un dato (component_state).
    assert not isinstance(window.group_3d, QGroupBox)
