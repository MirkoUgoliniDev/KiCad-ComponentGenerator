"""@brief Modelli YOLO .pt caricati con PyTorch/Ultralytics (utente, 2026-09-25).

Un .pt passa dallo STESSO codice dell'ONNX (letterbox, soglie, NMS, audit):
cambia solo l'oggetto che esegue la rete. I test di base usano un finto
Ultralytics (girano senza PyTorch); quelli d'integrazione usano i checkpoint
veri in models/, ignorati da git, e si saltano quando mancano.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from kicadgen.services import photo_yolo as yolo

ROOT = Path(__file__).resolve().parents[1]
CLASSES = dict(enumerate(yolo._CLASSES))


class _Tensor:
    def __init__(self, array):
        self.array = array

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.array


class _NoGrad:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_ultralytics(monkeypatch, *, names=CLASSES, task="detect", end2end=False,
                      output_shape=(1, 25, 8400)):
    """Finto torch + ultralytics: una rete che restituisce zeri della forma data."""
    calls = []
    head = SimpleNamespace(end2end=end2end)

    class Network:
        model = [head]

        def float(self):
            return self

        def eval(self):
            return self

        def __call__(self, tensor):
            calls.append(tensor.array.shape)
            shape = (1, 300, 6) if head.end2end else output_shape
            return (_Tensor(np.zeros(shape, np.float32)), None)

    class YOLO:
        def __init__(self, path, task=None):
            assert task == "detect"
            self.task, self.names, self.model = task, names, Network()

    torch = SimpleNamespace(__version__="fake-torch", no_grad=_NoGrad,
                            from_numpy=lambda array: _Tensor(array))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "ultralytics",
                        SimpleNamespace(__version__="fake-ultra", YOLO=YOLO))
    return head, calls


def _weights(tmp_path, name="model.pt"):
    path = tmp_path / name
    path.write_bytes(b"checkpoint")
    return path


def test_a_pt_file_is_accepted_and_run_through_pytorch(tmp_path, monkeypatch):
    head, calls = _fake_ultralytics(monkeypatch)
    info = yolo.validate_yolo_setup(_weights(tmp_path))
    assert info["model_format"] == "pt" and info["output_shape"] == [1, 25, 8400]
    assert "fake-torch" in info["runtime"] and calls == [(1, 3, 640, 640)]
    assert info["model_metadata"]["raw_head"] == "standard"


def test_an_end_to_end_head_uses_its_official_one_to_one_inference(tmp_path, monkeypatch):
    """YOLO26: sempre la testa ufficiale uno-a-uno, forzata (il default cambia fra
    versioni di Ultralytics). Scelta rivista il 2026-09-25: il ramo uno-a-molti
    con il nostro NMS ritrovava solo 39 dei 46 corpi dell'uno-a-uno."""
    head, _ = _fake_ultralytics(monkeypatch, end2end=False)
    head.one2one_cv2 = object()  # testa end-to-end caricata «spenta»
    info = yolo.validate_yolo_setup(_weights(tmp_path))
    assert head.end2end is True
    assert info["model_metadata"]["raw_head"].startswith("one-to-one")
    assert info["output_shape"] == [1, 25, 300]


def test_one_to_one_detections_become_raw_rows_with_their_class_score():
    """[x0, y0, x1, y1, conf, classe] -> centro, dimensioni e punteggio nella sua colonna."""
    detections = np.zeros((1, 3, 6), np.float32)
    detections[0, 0] = [100, 200, 140, 260, 0.9, yolo._CLASSES.index("capacitor")]
    detections[0, 1] = [10, 10, 30, 20, 0.4, yolo._CLASSES.index("ic")]
    detections[0, 2] = [0, 0, 0, 0, 0.0, 0]  # riga vuota della testa

    class Network:
        def __call__(self, tensor):
            return (_Tensor(detections),)

    torch = SimpleNamespace(no_grad=_NoGrad, from_numpy=lambda array: _Tensor(array))
    session = yolo._TorchSession(Network(), torch, np, "images", end_to_end=True)
    raw = session.run(None, {"images": np.zeros((1, 3, 640, 640), np.float32)})[0]
    assert raw.shape == (1, 25, 3) and raw.dtype == np.float32
    cap = yolo._CLASSES.index("capacitor")
    assert list(raw[0, :4, 0]) == [120, 230, 40, 60]
    assert raw[0, 4 + cap, 0] == pytest.approx(0.9)
    assert raw[0, 4:, 0].sum() == pytest.approx(0.9)  # nessun'altra classe inventata
    assert raw[0, 4 + yolo._CLASSES.index("ic"), 1] == pytest.approx(0.4)


def test_a_pt_with_an_unknown_class_is_refused(tmp_path, monkeypatch):
    """Negativo. Contratto cambiato (utente, 2026-09-25): un ordine diverso ora si
    legge per nome (vedi il test sotto); resta rifiutata una classe sconosciuta."""
    other = dict(CLASSES)
    other[0] = "gearbox"
    _fake_ultralytics(monkeypatch, names=other)
    with pytest.raises(ValueError, match="unknown class"):
        yolo.validate_yolo_setup(_weights(tmp_path))


def test_one_to_one_classes_of_a_remapped_model_land_in_the_detector_column():
    """Testa end-to-end con 16 classi: la classe del modello va nella colonna interna."""
    declared = {0: "IC", 1: "LED", 2: "capacitor"}
    mapping = yolo.class_mapping(declared)
    detections = np.zeros((1, 2, 6), np.float32)
    # Riquadri DIVERSI (contratto aggiornato, fix A8): due righe con lo stesso
    # riquadro sono la stessa ancora e ora si riuniscono in una colonna sola
    # (vedi test_one_to_one_rows_of_the_same_anchor_are_merged_*).
    detections[0, 0] = [0, 0, 10, 10, 0.9, 1]   # LED del modello
    detections[0, 1] = [20, 0, 30, 10, 0.6, 0]  # IC del modello

    class Network:
        def __call__(self, tensor):
            return (_Tensor(detections),)

    torch = SimpleNamespace(no_grad=_NoGrad, from_numpy=lambda array: _Tensor(array))
    session = yolo._TorchSession(Network(), torch, np, "images", end_to_end=True,
                                 mapping=mapping)
    raw = session.run(None, {"images": np.zeros((1, 3, 640, 640), np.float32)})[0]
    assert raw[0, 4 + yolo._CLASSES.index("led"), 0] == pytest.approx(0.9)
    assert raw[0, 4 + yolo._CLASSES.index("ic"), 1] == pytest.approx(0.6)
    assert raw[0, 4:, :].sum() == pytest.approx(1.5)


def test_a_pt_with_an_unexpected_output_is_refused(tmp_path, monkeypatch):
    _fake_ultralytics(monkeypatch, output_shape=(1, 30, 8400))
    with pytest.raises(ValueError, match="raw output"):
        yolo.validate_yolo_setup(_weights(tmp_path))


def test_without_pytorch_the_message_says_what_to_install(tmp_path, monkeypatch):
    """Nel programma installato PyTorch non c'e': errore chiaro, nessuna eccezione cruda."""
    monkeypatch.setitem(sys.modules, "ultralytics", None)
    with pytest.raises(ValueError, match="requirements-yolo-pt.txt"):
        yolo.validate_yolo_setup(_weights(tmp_path))


def test_an_onnx_file_still_goes_through_onnxruntime(tmp_path, monkeypatch):
    """Negativo al ramo PyTorch: un .onnx non tocca Ultralytics."""
    def forbidden(*args, **kwargs):
        pytest.fail("un .onnx non deve caricare ultralytics")
    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=forbidden))
    path = _weights(tmp_path, "model.onnx")
    with pytest.raises(ValueError, match="compatible local YOLO model"):
        yolo.validate_yolo_setup(path)  # byte non ONNX: fallisce in onnxruntime


# ── Integrazione con i checkpoint veri (saltata se mancano) ─────────────────

REAL = [ROOT / "models" / "pcb_component_detector_yolov8m_best.pt",
        ROOT / "models" / "pcb_component_detector_yolo26_roboflow_m.pt"]


@pytest.mark.parametrize("path", REAL, ids=["yolov8m", "yolo26m"])
def test_the_real_checkpoints_load_and_detect(path):
    pytest.importorskip("ultralytics")
    if not path.is_file():
        pytest.skip(f"{path.name} non presente (ignorato da git)")
    info = yolo.validate_yolo_setup(path)
    assert info["model_format"] == "pt" and info["output_shape"][:2] == [1, 25]
    # YOLOv8m: testa standard, 8400 righe. YOLO26: testa ufficiale uno-a-uno.
    expected_head = "one-to-one" if "yolo26" in path.name else "standard"
    assert info["model_metadata"]["raw_head"].startswith(expected_head)
    photo = np.full((480, 640, 3), 40, np.uint8)
    boxes = yolo.detect_yolo_components(photo, 60.0, 45.0, model_path=path)
    assert isinstance(boxes, list)


# ── Nome del modello nella modale di avanzamento (utente, 2026-09-25) ───────

def test_the_progress_window_names_the_pt_model_and_its_format():
    label = yolo.model_label(r"C:\modelli\pcb_component_detector_yolo26_roboflow_m.pt")
    assert label == "pcb_component_detector_yolo26_roboflow_m.pt (PyTorch)"


def test_the_default_model_is_named_as_onnx(monkeypatch):
    """Negativo al caso sopra: senza scelta, il modello predefinito e il suo formato."""
    monkeypatch.delenv("KICADGEN_YOLO_MODEL", raising=False)
    assert yolo.model_label(None) == "pcb_yolov8s.onnx (ONNX)"


def test_the_label_follows_the_environment_like_the_detector(monkeypatch, tmp_path):
    monkeypatch.setenv("KICADGEN_YOLO_MODEL", str(tmp_path / "altro.pt"))
    assert yolo.model_label(None) == "altro.pt (PyTorch)"


def test_loading_a_pt_model_keeps_ultralytics_offline(monkeypatch, tmp_path):
    """Il rilevatore e' locale: prima di importare Ultralytics si imposta
    YOLO_OFFLINE, che spegne le sue ricerche DNS; un valore scelto dall'utente
    resta (negativo)."""
    monkeypatch.setenv("YOLO_OFFLINE", "x")      # recorded, so teardown restores it
    monkeypatch.delenv("YOLO_OFFLINE")
    monkeypatch.setitem(sys.modules, "ultralytics", None)      # import fails right after
    with pytest.raises(ValueError):
        yolo._setup_torch(_weights(tmp_path, "model.pt"))
    assert os.environ["YOLO_OFFLINE"] == "1"
    monkeypatch.setenv("YOLO_OFFLINE", "0")
    with pytest.raises(ValueError):
        yolo._setup_torch(_weights(tmp_path, "model.pt"))
    assert os.environ["YOLO_OFFLINE"] == "0"
