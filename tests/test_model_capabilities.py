"""@brief Il requisito su un modello dipende da cosa gli si chiedera'.

Un modello di sola visione, senza lettura di PDF, non puo' fare l'estrazione da
datasheet ma fa benissimo il lavoro fotografico, dove i PDF non entrano mai.
Pretendere la stessa capacita' nei due casi lo nasconde e non si capisce
perche'.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from kicadgen.services.openrouter import (DATASHEET_CAPABILITIES, ModelInfo,
                                          PHOTO_CAPABILITIES)


def _model(identity, modalities, tools=True):
    return ModelInfo(id=identity, name=identity, prompt_usd_per_mtok=.3,
                     completion_usd_per_mtok=1.2, context_length=256000,
                     input_modalities=modalities, supports_tools=tools)


#: Un modello visione+testo con risposta strutturata, senza allegati: e' il
#: caso concreto che ha motivato questa distinzione.
VISION_ONLY = _model("vision-tools-no-files", ("text", "image"))
#: Uno che legge anche i documenti: va bene per entrambi i lavori.
FULL = _model("vision-tools-and-files", ("text", "image", "file"))
#: Senza risposta strutturata non serve a nulla qui, per nessuno dei due.
NO_TOOLS = _model("vision-without-tools", ("text", "image"), tools=False)


def test_a_vision_model_without_attachments_serves_the_photos_but_not_the_datasheets():
    assert VISION_ONLY.usable_for(PHOTO_CAPABILITIES)
    assert not VISION_ONLY.usable_for(DATASHEET_CAPABILITIES)
    assert not VISION_ONLY.usable_for_datasheets()
    assert FULL.usable_for(PHOTO_CAPABILITIES) and FULL.usable_for_datasheets()


def test_a_structured_answer_is_required_by_both_jobs():
    """@brief Caso negativo: la distinzione non deve far passare tutto."""
    assert not NO_TOOLS.usable_for(PHOTO_CAPABILITIES)
    assert not NO_TOOLS.usable_for(DATASHEET_CAPABILITIES)


def test_an_unknown_capability_is_an_error_and_not_a_silent_pass():
    """@brief Un requisito scritto male non deve sembrare soddisfatto."""
    with pytest.raises(ValueError, match="sconosciute"):
        FULL.usable_for(("tools", "telepatia"))


def test_the_picker_filters_by_the_job_it_was_opened_for():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from kicadgen.gui.model_dialog import ModelPickerDialog

    app = QApplication.instance() or QApplication([])
    for capabilities, expected in ((PHOTO_CAPABILITIES, [VISION_ONLY.id, FULL.id]),
                                   (DATASHEET_CAPABILITIES, [FULL.id])):
        dialog = ModelPickerDialog(None, None, capabilities=capabilities)
        try:
            dialog._on_models_loaded([VISION_ONLY, FULL, NO_TOOLS])
            app.processEvents()
            assert [model.id for model in dialog._visible_models()] == expected
            # L'etichetta deve dire quali capacita' sta filtrando: altrimenti
            # due finestre identiche mostrerebbero elenchi diversi senza motivo
            # apparente.
            assert ('PDF' in dialog.check_compatible.text()) is ('files' in capabilities)
            dialog.check_compatible.setChecked(False)
            assert len(dialog._visible_models()) == 3
        finally:
            dialog.close()
