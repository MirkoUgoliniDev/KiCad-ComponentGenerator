"""@brief Il requisito su un modello dipende da cosa gli si chiedera'.

Un modello di sola visione, senza lettura di PDF, non puo' fare l'estrazione da
datasheet ma fa benissimo il lavoro fotografico, dove i PDF non entrano mai.
Pretendere la stessa capacita' nei due casi lo nasconde e non si capisce
perche'.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import subprocess
import sys

import pytest

from kicadgen.services.openrouter import (DATASHEET_CAPABILITIES, ModelInfo,
                                          PHOTO_CAPABILITIES)

#: Radice del repository, per far trovare il pacchetto al processo separato.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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


def test_the_picker_filters_by_the_job_it_was_opened_for(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from kicadgen.gui import model_dialog
    from kicadgen.gui.model_dialog import ModelPickerDialog

    app = QApplication.instance() or QApplication([])
    # Il filtro si verifica sui modelli sintetici sopra: l'elenco vero non
    # serve, e scaricarlo qui aprirebbe un socket verso OpenRouter dentro un
    # test unitario. Sostituito il caricamento, come in test_i18n; in piu' la
    # funzione di rete e' resa fatale, cosi' un percorso non sostituito si
    # distingue subito da un replay offline invece di partire in silenzio.
    monkeypatch.setattr(ModelPickerDialog, "_load_models",
                        lambda self, force_refresh=False: None)
    monkeypatch.setattr(model_dialog, "list_models", lambda *args, **kwargs: pytest.fail(
        "Un test unitario non deve chiamare OpenRouter"))
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


#: Sonda eseguita in un processo separato: se la finestra distrugge il proprio
#: thread di download, Qt chiama qFatal e il processo termina con abort, che nel
#: processo di pytest ucciderebbe la sessione invece di segnare un test rosso.
_LIFETIME_PROBE = '''
import gc
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication, QWidget

from kicadgen.gui import model_dialog

started, release = threading.Event(), threading.Event()


def offline_list_models(force_refresh=False):
    """Replay offline: nessun socket e nessuna chiamata a pagamento.

    Riproduce solo la durata della richiesta vera, che ha 30 s di timeout ed e'
    percio' quasi sempre ancora in volo quando l'utente chiude la finestra.
    """
    started.set()
    assert release.wait(20), "il download finto non e' stato rilasciato"
    return []


model_dialog.list_models = offline_list_models

app = QApplication([])
parent = QWidget()
dialog = model_dialog.ModelPickerDialog(None, parent)
assert started.wait(5), "il worker non e' partito"
worker = dialog._worker
assert worker is not None and worker.isRunning()

# Caso positivo e caso negativo della proprieta' del thread: quello del dialogo
# appartiene a Qt tramite QApplication, quindi nessun riferimento Python che
# muore lo distrugge; un QThread senza genitore, come quello che il dialogo
# creava prima, resta invece proprieta' di PyQt e muore col suo involucro.
assert worker.parent() is app, worker.parent()
assert not sip.ispyowned(worker)
assert sip.ispyowned(QThread())

began = time.monotonic()
dialog.close()
closing = time.monotonic() - began
# Chiudere non deve attendere il megabyte dell'elenco: la finestra sparisce
# subito, altrimenti l'utente resterebbe fermo fino al timeout.
assert closing < 1.0, closing
assert worker.isRunning()

# La finestra muore col genitore mentre il download e' ancora in volo: e' il
# caso dell'utente che chiude l'applicazione subito dopo Annulla.
sip.delete(parent)
del parent, dialog
gc.collect()
assert not sip.isdeleted(worker), "thread distrutto mentre era in corsa"
assert worker.isRunning()

# Il risultato tardivo arriva a finestra sepolta: non deve toccare i widget.
release.set()
assert worker.wait(10000)
deadline = time.monotonic() + 2
while time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
print("PROBE-OK")
'''


def test_closing_the_picker_never_destroys_its_running_download(tmp_path):
    """@brief Regressione: chiudere il dialogo con il download in volo non
    deve far terminare l'applicazione.

    @note Il caso va eseguito fuori da pytest: la rottura non e' un'asserzione
    fallita ma un abort del processo (exit 134), che porterebbe via la sessione
    di test. Il processo separato usa un `list_models` offline, quindi la prova
    non dipende dalla rete ne' costa nulla.
    """
    pytest.importorskip("PyQt6")
    script = tmp_path / "picker_lifetime_probe.py"
    script.write_text("import sys\nsys.path.insert(0, %r)\n" % str(_REPOSITORY_ROOT)
                      + _LIFETIME_PROBE, encoding="utf-8")
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                            timeout=180, cwd=str(tmp_path),
                            env=dict(os.environ, QT_QPA_PLATFORM="offscreen",
                                     PYTHONPATH=str(_REPOSITORY_ROOT)))
    assert result.returncode == 0, (result.returncode, result.stdout[-2000:],
                                   result.stderr[-4000:])
    assert "PROBE-OK" in result.stdout
    # Le due diagnosi conosciute di questo difetto: il thread distrutto in corsa
    # e lo slot tardivo che tocca widget C++ gia' eliminati. Entrambe finiscono
    # in abort, quindi vanno escluse anche quando il codice di uscita e' 0.
    assert "Destroyed while thread" not in result.stderr
    assert "has been deleted" not in result.stderr
