"""@brief «Scegli il modello YOLO»: files, provenance, compatibility (user, 2026-09-26)."""
import os

import pytest
from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QDialog, QDialogButtonBox

from kicadgen.gui import yolo_model_dialog as dlg
from kicadgen.services import photo_yolo, yolo_models
from tests.test_photo_inventory_gui import gui


@pytest.fixture
def models(tmp_path, monkeypatch):
    """A models folder with one ONNX and one .pt file, and a recording validator."""
    dlg._RESULTS.clear()
    folder = tmp_path / "models"
    folder.mkdir()
    (folder / "base.onnx").write_bytes(b"onnx-pesi")
    (folder / "grande.pt").write_bytes(b"pt-pesi")
    loaded = []

    def validate(path):
        loaded.append(os.path.basename(str(path)))
        if "rotto" in str(path):
            raise ValueError("classi sconosciute")
        return {"model_classes": ["ic", "resistor"], "missing_classes": ["led"],
                "class_order": "remapped by name"}

    monkeypatch.setattr(photo_yolo, "validate_yolo_setup", validate)
    yield folder, loaded
    dlg._RESULTS.clear()


def _settle(app):
    for _ in range(5):
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()


def _text(dialog):
    """Details as the user reads them (without the invisible line-break hints)."""
    return dialog.details.text().replace(dlg._BREAK, "")


def _select(dialog, name):
    for row in range(dialog.model_list.count()):
        if dialog.model_list.item(row).data(0x0100).endswith(name):
            dialog.model_list.setCurrentRow(row)
            return
    raise AssertionError(name)


def test_the_list_marks_the_model_in_use_and_the_default(app, models):
    folder, _ = models
    dialog = dlg.YoloModelDialog("", folder, default=str(folder / "base.onnx"))
    _settle(app)
    texts = [dialog.model_list.item(r).text() for r in range(dialog.model_list.count())]
    assert texts[0].startswith("base.onnx  (in uso, predefinito)") and "ONNX" in texts[0]
    assert texts[1].startswith("grande.pt\n") and "PyTorch" in texts[1]
    assert dialog.selected_path().endswith("base.onnx")


def test_an_onnx_model_is_checked_at_once_and_its_classes_shown(app, models):
    folder, loaded = models
    dialog = dlg.YoloModelDialog(str(folder / "base.onnx"), folder)
    _settle(app)
    text = _text(dialog)
    assert loaded == ["base.onnx"]
    assert "Compatibile: 2 classi, lette per nome." in text and "resistor" in text
    assert "led" in text and not dialog.check_button.isVisible()


def test_a_pt_model_is_never_loaded_just_because_it_is_selected(app, models):
    """@brief Negativo di sicurezza: un .pt esegue codice quando si carica."""
    folder, loaded = models
    dialog = dlg.YoloModelDialog(str(folder / "grande.pt"), folder)
    dialog.show()
    _settle(app)
    assert "grande.pt" not in loaded
    assert dialog.check_button.isVisible() and "si verifica solo su tua richiesta" in \
        _text(dialog)
    dialog.check_button.click()
    _settle(app)
    assert loaded.count("grande.pt") == 1 and "Compatibile" in _text(dialog)
    dialog.close()


def test_a_model_the_detector_refuses_cannot_be_chosen(app, models):
    folder, _ = models
    (folder / "rotto.onnx").write_bytes(b"x")
    dialog = dlg.YoloModelDialog(str(folder / "rotto.onnx"), folder)
    _settle(app)
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled() and "classi sconosciute" in _text(dialog)
    _select(dialog, "base.onnx")
    _settle(app)
    assert ok.isEnabled()


def test_provenance_follows_the_content_not_the_name(app, models, monkeypatch):
    folder, _ = models
    known = yolo_models.sha256_of(folder / "base.onnx")
    info = yolo_models.Provenance("Modello di prova", "YOLOv8s", "Fonte · id-123", "MIT",
                                  yolo_models.STATUS_TO_VERIFY, "Dati", yolo_models.STATUS_CONFIRMED,
                                  "")
    monkeypatch.setitem(yolo_models.KNOWN_MODELS, known, info)
    (folder / "copia-rinominata.onnx").write_bytes(b"onnx-pesi")
    dialog = dlg.YoloModelDialog(str(folder / "copia-rinominata.onnx"), folder)
    _settle(app)
    text = _text(dialog)
    assert "Modello di prova" in text and "Da verificare prima di pubblicare." in text
    _select(dialog, "grande.pt")
    _settle(app)
    assert "Non dichiarata" in _text(dialog)


def test_another_file_is_added_and_selected(app, models, monkeypatch, tmp_path):
    folder, _ = models
    outside = tmp_path / "altrove" / "mio.onnx"
    outside.parent.mkdir()
    outside.write_bytes(b"mio")
    monkeypatch.setattr(dlg.QFileDialog, "getOpenFileName", lambda *args: (str(outside), ""))
    dialog = dlg.YoloModelDialog("", folder)
    dialog.other_button.click()
    _settle(app)
    assert dialog.selected_path() == str(outside)


def test_the_main_window_uses_the_choice_only_when_confirmed(window, monkeypatch):  # noqa: F811
    chosen = []
    monkeypatch.setattr(dlg.YoloModelDialog, "exec",
                        lambda self: chosen.append(self) or QDialog.DialogCode.Rejected)
    before = window.field_yolo_model.text()
    window._pick_session_yolo_model()
    assert chosen and window.field_yolo_model.text() == before
    monkeypatch.setattr(dlg.YoloModelDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(dlg.YoloModelDialog, "selected_path", lambda self: "C:/modelli/scelto.onnx")
    window._pick_session_yolo_model()
    assert window.field_yolo_model.text() == "C:/modelli/scelto.onnx"


def test_a_shut_down_window_starts_no_more_model_checks(app, models):
    """HANDOFF 131: tenuta in vita dopo l'uso, la finestra ripartiva a caricare i
    pesi quando la sua lista cambiava (anche durante la distruzione, in un
    momento qualsiasi). Chiusa con shutdown(), non avvia piu' nulla."""
    folder, loaded = models
    dialog = dlg.YoloModelDialog(str(folder / "base.onnx"), folder)
    _settle(app)
    before = list(loaded)
    dialog.shutdown()
    dlg._RESULTS.clear()                       # nothing remembered: a live window would check
    _select(dialog, "grande.pt")
    _select(dialog, "base.onnx")
    dialog._show_selected()
    _settle(app)
    assert loaded == before and not dialog._jobs
    dialog.deleteLater()


def test_a_live_window_still_checks_the_selected_model(app, models):
    """Negativo del precedente: senza shutdown la scelta avvia il controllo."""
    folder, loaded = models
    dialog = dlg.YoloModelDialog(str(folder / "base.onnx"), folder)
    _settle(app)
    dlg._RESULTS.clear()
    loaded.clear()
    dialog._show_selected()
    _settle(app)
    assert loaded == ["base.onnx"]
    dialog.shutdown()
    dialog.deleteLater()


def test_even_a_base_exception_in_a_check_never_leaves_the_pool_thread():
    """Un'eccezione che esce da un QRunnable fa terminare il processo senza
    traceback: il lavoro la trasforma in un risultato d'errore."""
    import sys
    from types import ModuleType
    module = ModuleType("kicadgen.services.photo_yolo")

    def boom(path):
        raise KeyboardInterrupt
    module.validate_yolo_setup = boom
    saved = sys.modules.get(module.__name__)
    sys.modules[module.__name__] = module
    try:
        job = dlg._Job("check", "x.onnx")
        seen = []
        job.signals.ready.connect(lambda kind, path, result: seen.append(result))
        job.run()                                # same code the pool thread runs
    finally:
        if saved is not None:
            sys.modules[module.__name__] = saved
        else:
            sys.modules.pop(module.__name__, None)
    assert seen == [{"ok": False, "error": "KeyboardInterrupt"}]
