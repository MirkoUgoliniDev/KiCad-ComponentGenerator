"""Keep configuration and SQLite tests away from the user's live preferences."""
import sys
import types
from pathlib import Path

import pytest

# `tests` e' la cartella di questi test. Ultralytics 8.4.x (requirements-yolo-
# pt.txt, modelli .pt) installa per errore un suo pacchetto `tests` di primo
# livello in site-packages; questa cartella non ha __init__.py, quindi per
# Python e' un pacchetto «namespace» e quello di Ultralytics vince sempre,
# e `from tests.test_photo_inventory_gui import ...` falliva in 17 file
# (2026-09-25). Il nome si lega qui, prima di ogni import dei test.
_TESTS_DIR = Path(__file__).resolve().parent
_bound = sys.modules.get("tests")
if _bound is None or str(_TESTS_DIR) not in [str(Path(p).resolve()) for p in getattr(_bound, "__path__", [])]:
    _package = types.ModuleType("tests")
    _package.__path__ = [str(_TESTS_DIR)]
    sys.modules["tests"] = _package


@pytest.fixture(autouse=True)
def isolated_profile_paths(monkeypatch, tmp_path):
    monkeypatch.setenv('KICADGEN_PROFILE_DB', str(tmp_path / 'profiles.sqlite3'))
    monkeypatch.setenv('KICADGEN_CONFIG_PATH', str(tmp_path / 'config.json'))
    # L'archivio degli abbinamenti accettati cambia l'esito del matching: un
    # test non deve mai leggere (ne' scrivere) quello vero dell'utente.
    monkeypatch.setenv('KICADGEN_MATCH_DB', str(tmp_path / 'accepted_components.sqlite3'))


#: Funzioni che aprono una finestra e restano in attesa di un clic. In un test
#: nessuno clicca: la chiamata non torna piu' e la suite si ferma li'.
_BLOCKING_DIALOGS = (
    ('QMessageBox', ('information', 'warning', 'critical', 'question', 'about')),
    ('QFileDialog', ('getOpenFileName', 'getOpenFileNames', 'getSaveFileName',
                     'getExistingDirectory')),
    ('QInputDialog', ('getText', 'getItem', 'getInt', 'getDouble')),
)


@pytest.fixture(autouse=True)
def no_unattended_modal_dialog(monkeypatch):
    """@brief Fai fallire subito un test che apre una modale non prevista.

    Una modale aperta durante un test non e' mai voluta: nessuno la chiude, la
    chiamata non ritorna e la suite resta appesa a tempo indeterminato, senza
    dire dove ne' perche'. E' costato un'ora di diagnosi per scoprire che un
    ramo di validazione apriva un QMessageBox.warning.

    Un'eccezione e' diagnosticabile in un secondo; un blocco no. I test che una
    modale se la aspettano continuano a sostituirla per conto proprio: il loro
    monkeypatch viene applicato dopo questo e ha la precedenza.
    """
    try:
        from PyQt6 import QtWidgets
    except ImportError:
        return  # Suite senza Qt: non c'e' nulla da proteggere.

    def refuse(name):
        def blocked(*args, **kwargs):
            raise AssertionError(
                f"{name} aperto durante un test e mai chiuso: la suite si "
                "fermerebbe qui. Sostituiscilo nel test (monkeypatch.setattr) "
                "se la modale e' prevista, oppure evita di aprirla.")
        return blocked

    for class_name, methods in _BLOCKING_DIALOGS:
        target = getattr(QtWidgets, class_name, None)
        if target is None:
            continue
        for method in methods:
            if hasattr(target, method):
                monkeypatch.setattr(target, method, refuse(f"{class_name}.{method}"),
                                    raising=False)


@pytest.fixture(autouse=True)
def no_leftover_message_translator():
    """@brief Ogni test parte e finisce senza traduttore dei messaggi registrato.

    Una finestra registra il proprio `I18n` in `messages.tx`; se un test la
    lascia in inglese, i test successivi dei servizi (che confrontano i
    messaggi italiani) vedrebbero l'inglese per un effetto di ordine.
    """
    from kicadgen.services.messages import set_translator
    set_translator(None)
    yield
    set_translator(None)


@pytest.fixture(autouse=True)
def no_leftover_usage_listener():
    """@brief Ogni test parte e finisce senza ascoltatore delle chiamate pagate.

    Una finestra registra la riga del costo come ascoltatore globale di
    `openrouter`; lasciata attiva, una chiamata finta di un test successivo
    scriverebbe `ai-costs.json` nella cartella del progetto di quella finestra.
    """
    from kicadgen.services.openrouter import reset_usage, set_usage_listener
    set_usage_listener(None)
    # Anche il consuntivo della corsa: un test che chiama `_record_usage` non
    # deve lasciare chiamate «pagate» al successivo (audit 25/09).
    reset_usage()
    yield
    set_usage_listener(None)
    reset_usage()
