"""@brief Detect generic PCB envelopes with a strictly local, optional YOLO model.

@note The caller supplies an already calibrated BGR board crop. This module does
not calibrate, download weights, infer package identity, or measure height: the
height it writes is an estimate from the detector class and the plan
(photo_heights), always marked `_height_estimated`.
Weights are a local .onnx file (onnxruntime, CPU) or a local .pt checkpoint
(PyTorch/Ultralytics, optional install); both go through the same letterbox,
thresholds, NMS and audit code.
@note The recipe is chosen per run through yolo_settings (defaults in
YOLO_SETTING_DEFAULTS): one overview plus magnified 640 and/or 1280 px tiles
with a chosen overlap (tile_sizes may be empty: overview only), an operating
confidence threshold, and class-agnostic IoU NMS. A tile whose bounds repeat
the overview or another view is not inferred again. The view count depends
on the image size; no components are silently capped.
"""
from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json
import math
import os
from numbers import Real
from pathlib import Path
import tempfile
try:
    from .messages import tx
    from .photo_heights import estimate_body_height
except ImportError:  # caricato come file a se' (test di isolamento degli import)
    from kicadgen.services.messages import tx
    from kicadgen.services.photo_heights import estimate_body_height


_CLASSES = (
    "battery", "button", "buzzer", "capacitor", "clock", "connector", "diode",
    "display", "fuse", "heatsink", "ic", "inductor", "led", "pads", "pins",
    "potentiometer", "relay", "resistor", "switch", "transformer", "transistor",
)
#: Soglia operativa PREDEFINITA. E' una proprieta' del modello (quanto e'
#: calibrata la sua confidenza), non della scheda: 0.25 va bene per l'ONNX
#: yolov8s di base; un YOLO26 addestrato su Roboflow rende meglio piu' in
#: alto (utente, 2026-09-25). Per questo chi usa il rilevatore la puo'
#: scegliere (detect_yolo_components(confidence=...)), come il cursore di
#: Roboflow; qui resta solo il valore di partenza.
_CONFIDENCE = 0.25
_AUDIT_CONFIDENCE = 0.05


def operating_confidence(value=None) -> float:
    """@brief Validate the operating confidence threshold chosen for a run.

    @param value Threshold in (0.05, 1), or None for the default _CONFIDENCE.
    @return The threshold as a float.
    @throws ValueError For a non-number, a boolean, a non-finite value or a
        value outside (audit floor, 1): below the audit floor the weak
        proposals would not even be recorded.
    """
    if value is None:
        return _CONFIDENCE
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError("YOLO confidence threshold must be a finite number")
    if not _AUDIT_CONFIDENCE < float(value) < 1:
        raise ValueError(f"YOLO confidence threshold must be between {_AUDIT_CONFIDENCE} and 1")
    return float(value)
_NMS_IOU = 0.5
_EXCLUDED = {"pads", "pins"}
#: Lati delle tessere ingrandite, in pixel della foto calibrata.
_TILE_SIZES = (640, 1280)
#: Frazione di tessera che si ripete nella successiva. Il passo e' il
#: complemento: e' la stessa costante usata da _views, tenuta qui perche' da
#: essa dipende quale corpo una tessera puo' misurare e quale no.
_TILE_OVERLAP_FRACTION = 0.25

#: Parametri del rilevatore che l'utente puo' scegliere (utente, 2026-09-25:
#: modale «Parametri YOLO»). I valori sono quelli usati finora: scegliere
#: nulla lascia il comportamento identico. Restano fuori di proposito l'unione
#: dei frammenti (_FRAGMENT_COVERED, interna e dichiarata fragile), la soglia
#: d'audit (non cambia il risultato) e l'ingresso a 640 px (lo fissa
#: l'addestramento del modello).
YOLO_SETTING_DEFAULTS = {"confidence": _CONFIDENCE, "nms_iou": _NMS_IOU,
                         "tile_sizes": list(_TILE_SIZES),
                         "tile_overlap": _TILE_OVERLAP_FRACTION}


def yolo_settings(values=None) -> dict:
    """@brief Validate the detector settings chosen by the user and fill in the defaults.

    @param values Mapping with any of the YOLO_SETTING_DEFAULTS keys, or None.
    @return A new dict with every key: confidence (operating threshold),
        nms_iou (overlap above which two boxes are the same body), tile_sizes
        (magnified tile sides in px, subset of 640 and 1280; empty = the whole
        photo only, as Roboflow does) and tile_overlap (fraction of a tile
        repeated in the next one).
    @throws ValueError For an unknown key or a value outside its range:
        confidence as operating_confidence; nms_iou in [0.1, 0.95];
        tile_overlap in [0.1, 0.5]; tile_sizes a list of distinct 640/1280.
    """
    values = {} if values is None else dict(values)
    unknown = set(values) - set(YOLO_SETTING_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown YOLO settings: {', '.join(sorted(unknown))}")
    result = {key: (list(value) if isinstance(value, list) else value)
              for key, value in YOLO_SETTING_DEFAULTS.items()}
    result.update(values)
    result["confidence"] = operating_confidence(result["confidence"])

    def bounded(key, low, high):
        value = result[key]
        if (isinstance(value, bool) or not isinstance(value, Real)
                or not math.isfinite(value) or not low <= float(value) <= high):
            raise ValueError(f"YOLO setting {key} must be between {low} and {high}")
        result[key] = float(value)

    bounded("nms_iou", 0.1, 0.95)
    bounded("tile_overlap", 0.1, 0.5)
    sizes = result["tile_sizes"]
    if (not isinstance(sizes, (list, tuple)) or len(set(sizes)) != len(sizes)
            or any(isinstance(size, bool) or size not in _TILE_SIZES for size in sizes)):
        raise ValueError("YOLO setting tile_sizes must list distinct values among "
                         + ", ".join(map(str, _TILE_SIZES)))
    result["tile_sizes"] = sorted(int(size) for size in sizes)
    return result


try:
    from ..app_paths import resource_path
except ImportError:  # modulo caricato da solo (test): stessa regola del sorgente
    def resource_path(*parts):
        """@brief Resolve a resource path inside the source repository when app_paths is unavailable.

        Fallback: the repository layout.
        """
        return Path(__file__).resolve().parents[2].joinpath(*parts)

def detector_classes() -> tuple:
    """@brief Categorie che il rilevatore locale sa assegnare.

    @return Le classi note, escluse quelle che non sono corpi montati.
    @note Le piazzole nude e le file di pin non sono componenti: restano fuori
        anche dall'elenco che si propone a chi rivede, perche' sceglierle
        sarebbe un errore che nessuno a valle saprebbe correggere.
    """
    return tuple(name for name in _CLASSES if name not in _EXCLUDED)


#: Weights formats the detector loads: ONNX with onnxruntime, PyTorch
#: checkpoints with Ultralytics (user's request, 2026-09-25).
_MODEL_SUFFIXES = (".onnx", ".pt")


def resolve_yolo_model(model_path=None) -> Path:
    """@brief Resolve and check a readable, nonempty local weights file.

    @param model_path Optional local path overriding KICADGEN_YOLO_MODEL.
    @return Absolute Path, defaulting to repository models/pcb_yolov8s.onnx.
    @throws ValueError If the path is invalid, missing, unreadable, or neither
        .onnx nor .pt.
    @note No downloads, deserialization or optional imports occur here. A .pt
        file is a Python pickle: LOADING it (in `_setup`) runs code from the
        file, so only files the user chose themselves are ever loaded.
    """
    selected = model_path if model_path is not None else os.environ.get(
        "KICADGEN_YOLO_MODEL", resource_path("models", "pcb_yolov8s.onnx")
    )
    try:
        if not os.fspath(selected).strip():
            raise ValueError("empty model path")
        path = Path(selected).expanduser().resolve()
        if path.suffix.lower() not in _MODEL_SUFFIXES:
            raise ValueError("expected a .onnx or .pt weights file")
        with path.open("rb") as handle:
            if not handle.read(1):
                raise ValueError("weights file is empty")
        return path
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ValueError(
            f"Cannot read local YOLO weights {selected!r}: {exc}. Supply model_path "
            "or set KICADGEN_YOLO_MODEL to an existing PCB .onnx or .pt detection model."
        ) from exc


def _dependencies():
    # Optional packages must never prevent importing the standard application.
    try:
        import numpy as np
        import cv2
        import onnxruntime as ort
    except (ImportError, OSError) as exc:
        raise ValueError(
            "Local YOLO runtime is unavailable. Install requirements-yolo.txt "
            "in the application's Python environment (numpy, opencv-python and "
            f"CPU onnxruntime), then retry. Details: {exc}"
        ) from exc
    return np, cv2, ort


#: Name of each weights format as the user reads it.
_FORMAT_NAMES = {".onnx": "ONNX", ".pt": "PyTorch"}


def model_label(model_path=None) -> str:
    """@brief Short description of the model the detector is going to use.

    @param model_path The same argument given to detect_yolo_components.
    @return File name and format, e.g. «pcb_yolov8s.onnx (ONNX)» or
        «pcb_component_detector_yolo26_roboflow_m.pt (PyTorch)».
    @note Same choice as resolve_yolo_model (argument, then
        KICADGEN_YOLO_MODEL, then the default), but nothing is opened or
        loaded: it only names what the progress window is about to run.
    """
    selected = model_path if model_path is not None else os.environ.get(
        "KICADGEN_YOLO_MODEL", resource_path("models", "pcb_yolov8s.onnx"))
    path = Path(os.fspath(selected))
    return f"{path.name} ({_FORMAT_NAMES.get(path.suffix.lower(), path.suffix or '?')})"


def _image_dependencies():
    """numpy and OpenCV only: what every weights format needs."""
    try:
        import numpy as np
        import cv2
    except (ImportError, OSError) as exc:
        raise ValueError(
            "Local YOLO runtime is unavailable. Install requirements-yolo.txt "
            f"(numpy, opencv-python), then retry. Details: {exc}") from exc
    return np, cv2


def class_mapping(names) -> list:
    """@brief Map the classes a model declares onto the detector's own classes, by name.

    @param names The model's class table {index: name}, indexes 0..n-1.
    @return For each model class index, the index of the same class in
        _CLASSES.
    @throws ValueError For a malformed table, a duplicated name or a name
        the detector does not know.
    @note Names match ignoring case and surrounding spaces («IC» is «ic»).
        A model may declare fewer classes than the detector: the missing
        ones simply never score. It may not declare an unknown class: what
        that class means cannot be known, and reading it as another one
        would label bodies wrongly without any error (user, 2026-09-25: a
        16-class YOLO26 model in a different order was the case).
    """
    if (not isinstance(names, dict) or not names
            or sorted(names) != list(range(len(names)))
            or any(type(key) is not int for key in names)):
        raise ValueError("class metadata must be a table {0: name, 1: name, ...}")
    known = {name: index for index, name in enumerate(_CLASSES)}
    mapping = []
    for index in range(len(names)):
        name = str(names[index]).strip().casefold()
        if name not in known:
            raise ValueError(f"unknown class {names[index]!r}; the detector knows: "
                             + ", ".join(_CLASSES))
        mapping.append(known[name])
    if len(set(mapping)) != len(mapping):
        raise ValueError("class metadata declares the same class twice")
    return mapping


def _mapping_info(names, mapping) -> dict:
    """Audit fields describing how the model's classes were read."""
    return {"model_classes": [str(names[i]) for i in range(len(names))],
            "missing_classes": [c for i, c in enumerate(_CLASSES) if i not in mapping],
            "class_order": "detector order" if mapping == list(range(len(_CLASSES)))
            else "remapped by name"}


def _remap_scores(raw, mapping, np):
    """[1, 4 + model classes, rows] -> [1, 4 + len(_CLASSES), rows], zero where absent."""
    rows = np.zeros((1, 4 + len(_CLASSES), raw.shape[2]), np.float32)
    rows[0, :4] = raw[0, :4]
    rows[0, [4 + target for target in mapping]] = raw[0, 4:]
    return rows


class _RemappedSession:
    """@brief ONNX session whose class scores come back in the detector's order.

    Used only when the model's class table differs from _CLASSES; a model
    with the detector's own table keeps its plain session.
    """

    def __init__(self, session, mapping, np):
        self._session, self._mapping, self._np = session, list(mapping), np

    def run(self, output_names, feeds):
        """@brief Run the model, then reorder its scores; see _remap_scores."""
        return [_remap_scores(raw, self._mapping, self._np) if isinstance(raw, self._np.ndarray)
                and raw.ndim == 3 and raw.shape[1] == 4 + len(self._mapping) else raw
                for raw in self._session.run(output_names, feeds)]


class _TorchSession:
    """A PyTorch detection network behind the `run` interface of onnxruntime.

    The detector code (letterbox, thresholds, NMS, audit, review candidates)
    stays the same for .onnx and .pt: only this object differs. `run` gets the
    same [1, 3, 640, 640] float32 tensor and returns a raw
    [1, 4 + classes, rows] float32 array: centre x, centre y, width, height in
    letterbox pixels, then one score per class, like the ONNX export.
    """

    def __init__(self, network, torch, np, input_name, end_to_end=False, mapping=None):
        self._network, self._torch, self._np = network, torch, np
        self._input = input_name
        self._end_to_end = end_to_end
        # Indice di classe del modello -> indice in _CLASSES (class_mapping).
        self._mapping = list(mapping) if mapping is not None else list(range(len(_CLASSES)))

    def raw_output(self, feeds):
        """@brief Run the network and return its first output as float32, unconverted."""
        np = self._np
        tensor = self._torch.from_numpy(feeds[self._input])
        with self._torch.no_grad():
            output = self._network(tensor)
        raw = output[0] if isinstance(output, (list, tuple)) else output
        return np.ascontiguousarray(raw.detach().cpu().numpy(), dtype=np.float32)

    def run(self, output_names, feeds):
        """@brief Run the network on one letterboxed tensor; return [raw output]."""
        np = self._np
        raw = self.raw_output(feeds)
        if not self._end_to_end:
            if self._mapping == list(range(len(_CLASSES))):
                return [raw]
            return [_remap_scores(raw, self._mapping, np)]
        return [_one_to_one_rows(raw, self._mapping, np)]


def _one_to_one_rows(raw, mapping, np):
    """@brief Convert end-to-end detections [1, K, 6] into raw rows [1, 4 + classes, K].

    @param raw Detections x0, y0, x1, y1, confidence, class (letterbox pixels).
    @param mapping Model class index -> index in _CLASSES (class_mapping).
    @param np NumPy module.
    @return Rows in the layout of the standard head, K columns (the shape the
        probe measured, so every view keeps the same output shape).
    @note Class ids must be finite, integral and within the model's table; a
        row whose id is not (NaN, 2.7, -1, 99) carries no class and is left
        with zero scores, i.e. below the audit floor. Truncating 2.7 to 2
        would label a body with a class the model never gave.
    @note The one-to-one head ranks anchors x classes jointly (Ultralytics
        get_topk_index flattens them), so ONE anchor may come back as several
        rows with the same box and different classes. Those rows are merged
        into the first one, keeping every class score, exactly like a
        standard-head row: _proposals then applies the same rule to both
        formats (pads/pins winning over a body class that reaches the
        operating threshold -> class_ambiguous uncertain body). The same box
        is recognized by bitwise-equal coordinates, which is what gathering
        the same anchor produces; two different anchors with identical boxes
        would be one body for NMS anyway.
    """
    detections = raw[0]
    count = len(detections)
    rows = np.zeros((4 + len(_CLASSES), count), np.float32)
    ids = detections[:, 5].astype(np.float64)
    with np.errstate(invalid="ignore"):
        valid = (np.isfinite(ids) & (ids == np.round(ids))
                 & (ids >= 0) & (ids < len(mapping)))
    targets = np.asarray(mapping, int)
    first_of = {}
    for index in np.flatnonzero(valid):
        box = tuple(float(value) for value in detections[index, :4])
        column = first_of.setdefault(box, int(index))
        if column == index:
            x0, y0, x1, y1 = box
            rows[:4, column] = [(x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0]
        target = 4 + targets[int(ids[index])]
        rows[target, column] = max(rows[target, column], detections[index, 4])
    return rows[None]


def _force_one_to_one(head, probe) -> bool:
    """@brief Switch an end-to-end head to its one-to-one inference, only if it really has one.

    @param head Last module of the network (the detection head).
    @param probe Callable running the network on a blank input and returning
        its raw first output as a NumPy array (_TorchSession.raw_output).
    @return True when the head now produces end-to-end detections [1, K, 6];
        False, with the head left as found, otherwise.
    @note The attribute's mere presence is not evidence: a FUSED checkpoint of
        an end-to-end model keeps `one2one_cv2` set to None (Ultralytics
        Detect.fuse removes the unused branch), and forcing end2end on it has
        no effect -- the output stays [1, 4 + classes, 8400]. Reading that as
        [K, 6] would turn class scores into boxes. So the switch is kept only
        if the head reports end2end True afterwards AND a probe returns a 3-D
        array whose last dimension is 6.
    """
    declared = (getattr(head, "one2one_cv2", None) is not None
                or getattr(head, "end2end", False) is True)
    if not declared:
        return False
    previous = getattr(head, "end2end", None)
    try:
        head.end2end = True
    except (AttributeError, TypeError):
        return False
    if getattr(head, "end2end", None) is True:
        shape = tuple(probe().shape)
        if len(shape) == 3 and shape[0] == 1 and shape[-1] == 6:
            return True
    try:
        head.end2end = previous
    except (AttributeError, TypeError):
        pass
    return False


def _setup_torch(path):
    """Load a .pt checkpoint with Ultralytics, checked like the ONNX model."""
    np, cv2 = _image_dependencies()
    try:
        import torch
        import ultralytics
        from ultralytics import YOLO
    except (ImportError, OSError) as exc:
        raise ValueError(
            "PyTorch support for .pt models is not installed. Install "
            "requirements-yolo-pt.txt in the application's Python environment, or "
            "use an .onnx export of the same model. The Windows installer does not "
            f"include PyTorch. Details: {exc}") from exc
    try:
        weights = path.read_bytes()
        model = YOLO(str(path), task="detect")
        if model.task != "detect":
            raise ValueError("model task must be 'detect'")
        names = dict(model.names)
        mapping = class_mapping(names)
        network = model.model.float().eval()
        head = network.model[-1]
        # Testa end-to-end (YOLO26, YOLOv10): si usa SEMPRE la sua inferenza
        # ufficiale, uno-a-uno, forzata qui perche' il valore predefinito
        # cambia fra versioni di Ultralytics (8.4.37 accesa, 8.4.163 spenta).
        # Verificato il 2026-09-25 su una foto reale: il ramo uno-a-molti con
        # il nostro NMS trovava 38 corpi e ne ritrovava 39 dei 46 dell'uno-a-
        # uno; la scorciatoia cambiava il modello, non solo il codice.
        # La testa uno-a-uno ordina ancore x classi insieme, quindi la stessa
        # ancora puo' tornare con piu' classi: _one_to_one_rows le riunisce e
        # il recupero «pads/pins con un corpo concorrente» vale come per l'ONNX.
        session = _TorchSession(network, torch, np, "images", mapping=mapping)
        blank = {"images": np.zeros((1, 3, 640, 640), np.float32)}
        end_to_end = _force_one_to_one(head, lambda: session.raw_output(blank))
        session = _TorchSession(network, torch, np, "images", end_to_end=end_to_end,
                                mapping=mapping)
        probe = session.run(None, {"images": np.zeros((1, 3, 640, 640), np.float32)})[0]
        if probe.ndim != 3 or probe.shape[:2] != (1, 4 + len(_CLASSES)) or probe.shape[2] < 1:
            raise ValueError(f"expected raw output [1, {4 + len(_CLASSES)}, rows], got {list(probe.shape)}")
    except Exception as exc:
        raise ValueError(
            f"Cannot load compatible local YOLO model {path}: {exc}. Use a PCB YOLO "
            "detection checkpoint trained at 640 px whose classes are among: "
            + ", ".join(_CLASSES) + ".") from exc
    info = {
        "model_path": str(path), "model_sha256": hashlib.sha256(weights).hexdigest(),
        "model_format": "pt",
        "model_metadata": {"task": model.task, "names": str(names),
                           "raw_head": "one-to-one (end-to-end; anchor x class rows "
                                       "merged per box)"
                           if end_to_end else "standard"},
        "classes": list(_CLASSES), "input_shape": [1, 3, 640, 640],
        "output_shape": list(probe.shape), "providers": ["torch-cpu"],
        "runtime": f"torch {torch.__version__}; ultralytics {ultralytics.__version__}",
        **_mapping_info(names, mapping),
    }
    return session, "images", np, cv2, info


def _setup(model_path):
    path = resolve_yolo_model(model_path)
    if path.suffix.lower() == ".pt":
        return _setup_torch(path)
    np, cv2, ort = _dependencies()
    try:
        # Use the same bytes for the session and audit digest. Loading bytes also
        # requires self-contained ONNX weights, with no external tensor files.
        weights = path.read_bytes()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        session = ort.InferenceSession(
            weights, sess_options=options, providers=["CPUExecutionProvider"]
        )
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if (len(inputs) != 1 or inputs[0].type != "tensor(float)"
                or list(inputs[0].shape) != [1, 3, 640, 640]):
            raise ValueError("expected one float32 input [1, 3, 640, 640]")
        metadata = dict(session.get_modelmeta().custom_metadata_map)
        names = ast.literal_eval(metadata["names"])
        mapping = class_mapping(names)
        if (len(outputs) != 1 or outputs[0].type != "tensor(float)"
                or list(outputs[0].shape) != [1, 4 + len(names), 8400]):
            raise ValueError(f"expected one raw YOLOv8 float32 output [1, {4 + len(names)}, 8400]")
        if metadata.get("task") != "detect":
            raise ValueError("model task metadata must be 'detect'")
        for key, expected in (("imgsz", [640, 640]), ("batch", 1), ("stride", 32)):
            if key in metadata and ast.literal_eval(metadata[key]) != expected:
                raise ValueError(f"incompatible {key} metadata; expected {expected}")
        providers = session.get_providers()
        if "CPUExecutionProvider" not in providers:
            raise ValueError("CPUExecutionProvider is unavailable")
    except Exception as exc:
        raise ValueError(
            f"Cannot load compatible local YOLO model {path}: {exc}. Use a "
            "self-contained PCB YOLOv8 ONNX export with fixed 640 input, raw "
            "[1, 4 + classes, 8400] output and classes among: " + ", ".join(_CLASSES)
            + "; check the CPU onnxruntime installation."
        ) from exc
    # Il rilevatore legge i punteggi nell'ordine di _CLASSES: un modello con
    # una tabella diversa passa da una sessione che li riordina. Con la
    # tabella del rilevatore la sessione resta quella di onnxruntime.
    if mapping != list(range(len(_CLASSES))):
        session = _RemappedSession(session, mapping, np)
    info = {
        "model_path": str(path), "model_sha256": hashlib.sha256(weights).hexdigest(),
        "model_format": "onnx",
        "model_metadata": metadata, "classes": list(_CLASSES),
        "input_shape": list(inputs[0].shape),
        "output_shape": [1, 4 + len(_CLASSES), 8400],
        "model_output_shape": list(outputs[0].shape),
        "providers": providers, "runtime": getattr(ort, "__version__", "unknown"),
        **_mapping_info(names, mapping),
    }
    return session, inputs[0].name, np, cv2, info


def validate_yolo_setup(model_path=None) -> dict:
    """@brief Validate local weights, optional CPU runtime, and model contract.

    @param model_path Optional weights path; resolution follows resolve_yolo_model.
    @return Dictionary containing model path/hash, metadata, classes and shapes.
    @throws ValueError For missing weights/runtime or an incompatible model.
    @note Never accesses the network. An .onnx model gets a CPU session and no
        inference; a .pt checkpoint is loaded (which runs code from the file,
        see resolve_yolo_model) and runs one or two probe inferences on a
        blank 640 x 640 input to learn the real output shape of its head.
    """
    return _setup(model_path)[-1]


def view_overlap_px(view, overlap=_TILE_OVERLAP_FRACTION):
    """@brief Quanto una tessera si sovrappone alla successiva, in pixel.

    @param view Nome della vista, come lo genera _views.
    @param overlap Frazione di tessera ripetuta, la stessa data a _views.
    @return Pixel di sovrapposizione, o infinito per la vista d'insieme.
    @note E' la quota che decide che cosa quella vista puo' misurare: un corpo
        piu' grande della sovrapposizione tocca un bordo in OGNI tessera che lo
        contiene, quindi da li' il suo ingombro non e' osservabile, per quanto
        il rilevamento sia sicuro.
    """
    name = str(view or "")
    if not name.startswith("tile"):
        return float("inf")
    try:
        size = int(name.split("-")[0][4:])
    except ValueError:
        return float("inf")
    return size * overlap


def _prefer_complete_views(rows, complete, overlap=_TILE_OVERLAP_FRACTION):
    """@brief Toglie i frammenti che nessuna tessera poteva misurare.

    @param rows Righe da filtrare.
    @param complete Rilevamenti non tagliati, da QUALUNQUE vista e a qualunque
        confidenza: che una vista abbia visto il corpo per intero e' un fatto
        geometrico e non dipende dalla soglia operativa. Restringerlo ai soli
        candidati sopra soglia lasciava passare proprio il caso che conta --
        sulla scheda di prova la vista d'insieme vedeva il modulo intero a
        0,18, sotto soglia, e il confronto non la trovava.
    @return Le righe da tenere.
    @note Regola fisica, non una soglia: un corpo piu' grande della
        sovrapposizione fra tessere tocca un bordo in ogni tessera che lo
        contiene, quindi quella vista non ne conosce l'ingombro. Se un'altra
        vista lo contiene per intero, e' quella a misurarlo, e il frammento non
        aggiunge nulla -- anzi, e' cio' che creava il falso accordo su un bordo
        di tessera. Sotto la sovrapposizione la regola non si applica: un corpo
        piccolo che sfiora una cucitura resta un corpo osservato.
    @note Side effect: when the dropped fragment was at the operating
        threshold and no covering view is, the most confident covering view
        gets `attests_presence` True, `presence_attested_by` (fragment ids)
        and `presence_confidence`. detect_yolo_components then emits it as an
        operating-threshold body whose geometry comes from a weaker view.
    """
    keep = []
    for row in rows:
        sides = row.get("clipped_sides")
        box = row["box"]
        size = max(box[2] - box[0], box[3] - box[1])
        covering = [other for other in complete if other is not row
                    and _cover(box, other["box"]) >= _FRAGMENT_COVERED]
        if sides and size > view_overlap_px(row.get("view"), overlap) and covering:
            # Presenza ed ingombro sono due fatti distinti. Se il frammento che
            # cede il posto era sopra la soglia operativa, la PRESENZA del corpo
            # e' attestata da un rilevamento sicuro: a cedere e' solo la misura
            # dell'ingombro, che passa alla vista che lo contiene. Quella vista
            # va quindi tenuta anche se debole, altrimenti si perderebbe due
            # volte un corpo che era stato visto benissimo.
            # Se una vista che lo contiene e' gia' sopra soglia, la presenza e'
            # gia' portata da lei: nulla da promuovere. Altrimenti la promossa
            # e' UNA sola, la piu' sicura, per non moltiplicare lo stesso corpo.
            if (row.get("rejection") is None
                    and not any(other.get("rejection") is None for other in covering)):
                best = max(covering, key=lambda other: other["confidence"])
                best["attests_presence"] = True
                best.setdefault("presence_attested_by", []).append(row["proposal_id"])
                best["presence_confidence"] = max(best.get("presence_confidence", 0.0),
                                                  row["confidence"])
            continue
        keep.append(row)
    return keep


def _views(width, height, sizes=_TILE_SIZES, overlap=_TILE_OVERLAP_FRACTION):
    """Whole photo, then the magnified tiles of each chosen side (none = whole photo only).

    A tile whose bounds equal a view already produced (the overview, when the
    photo is smaller than the tile; a 1280 tile equal to a 640 one) is not
    produced again: same pixels through the same letterbox means the same
    inference repeated, whose duplicate rows NMS would only remove again.
    """
    seen = {(0, 0, width, height)}
    yield "overview", (0, 0, width, height)
    for size in sizes:
        def starts(length):
            values = range(0, max(1, length - size + 1),
                           max(1, int(size * (1 - overlap))))
            return sorted(set(values) | {max(0, length - size)})

        for y in starts(height):
            for x in starts(width):
                bounds = (x, y, min(width, x + size), min(height, y + size))
                if bounds in seen:
                    continue
                seen.add(bounds)
                yield f"tile{size}-{x}-{y}", bounds


def _letterbox(image, np, cv2):
    height, width = image.shape[:2]
    ratio = min(640 / width, 640 / height)
    nw, nh = max(1, round(width * ratio)), max(1, round(height * ratio))
    left, top = round((640 - nw) / 2 - 0.1), round((640 - nh) / 2 - 0.1)
    canvas = np.full((640, 640, 3), 114, np.uint8)
    canvas[top:top + nh, left:left + nw] = cv2.resize(
        image, (nw, nh), interpolation=cv2.INTER_LINEAR
    )
    tensor = np.ascontiguousarray(canvas[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32)
    return tensor / 255.0, ratio, left, top


def _nms(rows, np, trace=None, stage=None, iou=_NMS_IOU):
    """Suppress alternate boxes regardless of class; do not suppress containment alone."""
    if not rows:
        return []
    boxes = np.array([row["box"] for row in rows], dtype=float)
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    order = np.argsort(-np.array([row["confidence"] for row in rows]), kind="stable")
    retained = []
    while order.size:
        index, rest = order[0], order[1:]
        retained.append(rows[index])
        wh = np.maximum(0, np.minimum(boxes[index, 2:], boxes[rest, 2:])
                        - np.maximum(boxes[index, :2], boxes[rest, :2]))
        intersection = wh[:, 0] * wh[:, 1]
        overlap = intersection / (areas[index] + areas[rest] - intersection)
        if trace is not None:
            for suppressed, value in zip(rest[overlap > iou], overlap[overlap > iou]):
                trace.append({"proposal_id": rows[suppressed]["proposal_id"],
                              "suppressed_by": rows[index]["proposal_id"],
                              "stage": stage, "iou": float(value)})
        order = rest[overlap <= iou]
    return retained


#: Quanto un frammento deve stare dentro un rilevamento intero perche' sia lo
#: stesso corpo, gia' visto meglio: sotto questa frazione della propria area
#: il frammento e' l'unica prova che quel corpo esista.
_FRAGMENT_COVERED = 0.5


def _cover(first, second):
    """@brief Frazione del primo rettangolo che sta dentro il secondo."""
    wide = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    high = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    area = (first[2] - first[0]) * (first[3] - first[1])
    return (wide * high / area) if area > 0 else 0.0


def _recover_clipped(retained, fragments, confidence=_CONFIDENCE):
    """@brief Ricompone i corpi che nessuna vista ha visto per intero.

    @param retained Rilevamenti interni sopravvissuti alla NMS globale.
    @param fragments Righe scartate perche' toccavano il bordo di una tessera.
    @return Righe ricomposte, con la confidenza migliore del gruppo.
    @note Il taglio di una tessera non e' prova di assenza. La regola che
        scarta i frammenti serve a togliere i doppioni di cio' che un'altra
        vista vede per intero: quando NESSUNA vista lo vede intero, scartare
        significa perdere il corpo. Succede a chi e' piu' grande della
        sovrapposizione fra tessere -- 160 px su tessere da 640 -- e allora il
        corpo tocca un bordo in ogni tessera che lo contiene. Le sovrapposizioni
        si potrebbero allargare, ma nessuna larghezza fissa e' garantita:
        l'ingombro dei corpi non si conosce prima di averli visti.
    @note L'unione dei frammenti resta un ingombro TRONCATO quando il taglio e'
        su un bordo che nessuna tessera supera: e' un'ipotesi da rivedere, non
        una misura, e chi la riceve la marca come tale.
    @note Separazione dichiaratamente fragile. Il raggruppamento lega ogni
        frammento al capostipite, non al precedente, ma sulla scheda di prova
        l'unione piu' grande cresce comunque a quattro volte l'area del
        capostipite (31 frammenti, da 6,4x11,3 a 19,7x14,7 mm). Va bene per
        dire «qui c'e' un corpo», non per dire quanto e' grande: chi misura e'
        la riconciliazione, che riceve queste righe come ipotesi incerte.
    """
    orphans = [row for row in fragments
               if not any(_cover(row["box"], kept["box"]) >= _FRAGMENT_COVERED
                          for kept in retained)]
    groups = []
    for row in sorted(orphans, key=lambda row: -row["confidence"]):
        for group in groups:
            # Ogni frammento deve coprire il CAPOSTIPITE del gruppo, non un
            # membro qualsiasi: con il legame a catena l'unione puo' camminare
            # per tutta la scheda, un frammento alla volta. Legata al
            # capostipite, ogni membro sta per meta' dentro di lui, quindi
            # l'unione resta nell'ordine del doppio in ogni direzione.
            # ATTENZIONE, misurato sulla scheda vera: non basta a rendere
            # l'unione stretta. Il gruppo piu' grande resta di 31 frammenti e
            # cresce fino a QUATTRO VOLTE l'area del capostipite (6,4x11,3 mm
            # -> 19,7x14,7 mm). L'ingombro di un corpo ricomposto e' percio'
            # un'ipotesi grossolana, non una misura: per questo esce marcato e
            # la riconciliazione lo rimisura.
            # La porzione condivisa, non la IoU: due meta' dello stesso corpo
            # si coprono a vicenda ma hanno IoU bassa, ed e' proprio per
            # questo che la NMS non le aveva unite.
            if max(_cover(row["box"], group[0]["box"]),
                   _cover(group[0]["box"], row["box"])) >= _FRAGMENT_COVERED:
                group.append(row)
                break
        else:
            groups.append([row])
    recovered = []
    for group in groups:
        best = max(group, key=lambda row: row["confidence"])
        box = [min(row["box"][0] for row in group), min(row["box"][1] for row in group),
               max(row["box"][2] for row in group), max(row["box"][3] for row in group)]
        # Dopo l'unione un lato resta artificiale solo se TUTTI i frammenti che
        # lo toccano erano tagliati li': se un altro pezzo vede oltre, quel
        # bordo e' stato misurato davvero.
        outer = {'left': box[0], 'top': box[1], 'right': box[2], 'bottom': box[3]}
        index_of = {'left': 0, 'top': 1, 'right': 2, 'bottom': 3}
        still_clipped = [side for side in outer
                         if all(side in row.get('clipped_sides', ())
                                for row in group
                                if row['box'][index_of[side]] == outer[side])]
        recovered.append(dict(best, box=box, clipped_by_tile=True,
                              clipped_sides=still_clipped,
                              recovered_from=[row["proposal_id"] for row in group],
                              rejection=(None if best["confidence"] >= confidence
                                         else "below_confidence")))
    return recovered


def _proposals(raw, bounds, image_size, transform, view, np, confidence_threshold=_CONFIDENCE):
    """Keep auditable pre-NMS proposals; reject malformed or cropped body boxes."""
    x, y, x1, y1 = bounds
    width, height = image_size
    ratio, left, top = transform
    predictions = raw[0].T
    scores = predictions[:, 4:]
    body_classes = np.array([i for i, name in enumerate(_CLASSES) if name not in _EXCLUDED])
    valid = (np.isfinite(predictions).all(axis=1)
             & (scores >= 0).all(axis=1) & (scores <= 1).all(axis=1))
    classes = scores.argmax(axis=1)
    confidences = scores[np.arange(len(scores)), classes]
    rows, invalid_count = [], int((~valid).sum())
    for index in np.flatnonzero(valid & (confidences >= _AUDIT_CONFIDENCE)):
        prediction = predictions[index]
        class_id = int(classes[index])
        confidence = float(confidences[index])
        body_id = int(body_classes[scores[index, body_classes].argmax()])
        body_confidence = float(scores[index, body_id])
        # Argmax alone is not evidence of absence. Preserve a competing body
        # hypothesis at the SAME operating threshold, as uncertain review input.
        # This is a recall heuristic, not validation that a pad is a mounted body.
        ambiguous_body = (_CLASSES[class_id] in _EXCLUDED
                          and body_confidence >= confidence_threshold)
        selected_id = body_id if ambiguous_body else class_id
        selected_confidence = body_confidence if ambiguous_body else confidence
        cx, cy, bw, bh = map(float, prediction[:4])
        local = [(cx - bw / 2 - left) / ratio, (cy - bh / 2 - top) / ratio,
                 (cx + bw / 2 - left) / ratio, (cy + bh / 2 - top) / ratio]
        # Quali lati poggiano sul CONFINE DELLA TESSERA e non sul corpo: sono
        # artefatti del ritaglio, non bordi misurati, e chi li riceve deve
        # saperlo. Un bordo interno della tessera resta escluso dal conteggio
        # quando coincide con il bordo dell'immagine, che invece e' reale.
        clipped_sides = [name for name, touching in (
            ('left', x > 0 and local[0] <= 1),
            ('top', y > 0 and local[1] <= 1),
            ('right', x1 < width and local[2] >= x1 - x - 1),
            ('bottom', y1 < height and local[3] >= y1 - y - 1)) if touching]
        cut = bool(clipped_sides)
        # Clip letterbox padding to this view's actual image, then to the board.
        # Interior boundary fragments remain excluded even after clipping.
        box = [max(x, min(x1, local[0] + x)), max(y, min(y1, local[1] + y)),
               max(x, min(x1, local[2] + x)), max(y, min(y1, local[3] + y))]
        reason = None
        if bw <= 0 or bh <= 0 or box[2] <= box[0] or box[3] <= box[1]:
            reason = "empty_or_inverted_box"
        elif _CLASSES[class_id] in _EXCLUDED and not ambiguous_body:
            reason = "excluded_class"
        elif cut:
            reason = "clipped_by_tile"
        elif confidence < confidence_threshold:
            reason = "below_confidence"
        rows.append({"proposal_id": f"{view}:{int(index)}",
                     "view": view, "prediction_index": int(index), "box": box,
                     "raw_xywh": [cx, cy, bw, bh], "class": _CLASSES[selected_id],
                     "confidence": selected_confidence,
                     "argmax_class": _CLASSES[class_id], "argmax_confidence": confidence,
                     "best_body_class": _CLASSES[body_id], "best_body_confidence": body_confidence,
                     "class_scores": [float(score) for score in scores[index]],
                     "class_ambiguous": ambiguous_body, "clipped_by_tile": bool(cut),
                     "clipped_sides": clipped_sides,
                     "rejection": reason})
    return rows, invalid_count


def awaiting_user_keep(proposal) -> bool:
    """@brief Tell whether a proposal waits for a person to keep it before anything uses it.

    @param proposal Detector proposal (dict).
    @return True for a sub-threshold proposal (`_yolo_low_confidence`) that
        nobody kept in the review (`_yolo_user_kept` not True).
    @note Contract of the low-confidence proposals (user decision 2026-09-26):
        they are SHOWN in the review, dashed and labelled as low confidence,
        but they are opt-in -- not accepted by default and never sent to the
        paid AI verification unless the user keeps them. On the test board
        most of them were noise over empty areas, which is why they are not
        sent by default; low confidence is still not proof of absence, which
        is why they are shown instead of dropped. Proposals rebuilt from tile
        seams are weak for a geometric reason and are NOT flagged: they keep
        going to the review and to the model as before.
    """
    return (isinstance(proposal, dict) and proposal.get("_yolo_low_confidence") is True
            and proposal.get("_yolo_user_kept") is not True)


def split_ai_candidates(proposals):
    """@brief Separate the proposals that may reach the paid AI from those still awaiting a person.

    @param proposals Detector proposals, possibly after the review.
    @return (sent, withheld): two new lists, input order preserved; the dicts
        themselves are not copied or modified.
    @note Every paid path filters through here, so a caller that skips the
        review cannot send unkept low-confidence proposals by mistake.
    """
    sent, withheld = [], []
    for proposal in proposals:
        (withheld if awaiting_user_keep(proposal) else sent).append(proposal)
    return sent, withheld


def _dimension(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite positive number in mm (not a boolean).")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite positive number in mm.") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number in mm.")
    return number


def _write_audit(audit_dir, document):
    try:
        directory = Path(audit_dir)
        directory.mkdir(parents=True, exist_ok=True)
        # A unique filename preserves previous runs in a shared audit directory.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="yolo-", suffix=".json",
            dir=directory, delete=False,
        ) as handle:
            json.dump(document, handle, indent=2, allow_nan=False)
            return handle.name
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"Cannot write YOLO audit under {audit_dir!r}: {exc}") from exc


def detect_yolo_components(photo, length_mm, width_mm, model_path=None, log=None,
                           audit_dir=None, include_review_candidates=False,
                           confidence=None, settings=None) -> list[dict]:
    """@brief Detect generic mechanical candidates on an already calibrated photo.

    @param photo Nonempty H x W x 3 uint8 NumPy image in OpenCV BGR order.
    @param length_mm Finite positive board extent along image X, in mm.
    @param width_mm Finite positive board extent along image Y, in mm.
    @param model_path Optional local .onnx or .pt weights path; see resolve_yolo_model.
    @param log Optional callable receiving progress and audit-path strings.
    @param audit_dir Optional directory receiving a unique yolo-*.json audit.
    @param confidence Operating confidence threshold; None keeps the default
        (_CONFIDENCE, 0.25). Validated by operating_confidence and recorded in
        the log and in the audit recipe. Below it a proposal is weak
        (review-only or audit-only), exactly as with the default. When given
        it overrides settings["confidence"].
    @param settings Detector settings chosen by the user (see yolo_settings);
        None keeps every default. Recorded in the log and the audit recipe.
    @param include_review_candidates Also return the sub-threshold body
        hypotheses, deduplicated against the retained bodies, even without an
        audit directory. Defaults to False for standalone detector
        compatibility. Those REBUILT from tile seams come back as uncertain
        review-only candidates, as before. The other weak proposals come back
        flagged `_yolo_low_confidence` True: shown in the review as low
        confidence, opt-in, and withheld from every paid AI call until a
        person keeps them (awaiting_user_keep, split_ai_candidates). Reuses
        the same inference views; stores weak proposals in memory and runs
        extra NMS (quadratic worst case).
    @return photo_grounding-compatible dictionaries ordered top-to-bottom then
        left-to-right, with normalized _box and board-centered mm coordinates.
    @throws ValueError For invalid photo/dimensions/model/runtime, failed inference
        or an unwritable audit directory. Partial inference is never returned.
    @note X points right and Y down. Long/short dimensions determine rotation
        0/90. height_mm is an ESTIMATE from the detector class and the plan
        (photo_heights.estimate_body_height; 1.0 mm placeholder for an
        ambiguous or unknown class), never a measurement: `_height_estimated`
        is always True and `_height_source` ends in `_not_measured`.
        Classes/confidence remain detector evidence only; all kinds are generic.
        Pads/pins winners with competing body evidence at the normal confidence
        threshold survive as uncertain generic candidates, never verified bodies.
        Audit includes model hash, recipe, raw proposals at confidence >=0.05
        before filtering/NMS, invalid-row counts, and retained detector results.
        Schema 2 adds decoded BGR pixel hash, NMS suppressor links and detector
        proposal IDs on components. Low-confidence body proposals are separately
        deduplicated for review and returned only with
        include_review_candidates (`_yolo_low_confidence` marks the opt-in ones).
        _yolo_origin and _yolo_review_only distinguish operating-threshold output
        from weak review hypotheses. IDs are unique within each returned run.
        No evidence below the audit floor is stored;
        absence in this audit does not prove absence in the model output.
    """
    length_mm = _dimension(length_mm, "length_mm")
    width_mm = _dimension(width_mm, "width_mm")
    if type(include_review_candidates) is not bool:
        raise ValueError("include_review_candidates must be a boolean.")
    if log is not None and not callable(log):
        raise ValueError("log must be callable or None.")
    emit = log if log is not None else lambda message: None
    # Solo numpy/OpenCV qui: onnxruntime serve ai soli .onnx (vedi _setup).
    np, cv2 = _image_dependencies()
    if (not isinstance(photo, np.ndarray) or photo.ndim != 3
            or photo.shape[2] != 3 or min(photo.shape[:2]) <= 0
            or photo.dtype != np.uint8):
        raise ValueError("photo must be a nonempty uint8 BGR NumPy image of shape H x W x 3.")
    chosen = yolo_settings(settings)
    if confidence is not None:
        chosen["confidence"] = operating_confidence(confidence)
    threshold, nms_iou = chosen["confidence"], chosen["nms_iou"]
    overlap = chosen["tile_overlap"]
    session, input_name, np, cv2, info = _setup(model_path)
    expected_shape = tuple(info["output_shape"])
    emit(f"YOLO local model: {Path(info['model_path']).name} "
         f"({info.get('model_format', 'onnx')}; {info.get('runtime', 'unknown')}).")
    if info.get("class_order") == "remapped by name":
        missing = info.get("missing_classes") or []
        emit(f"YOLO local: {len(info['model_classes'])} model classes read by name"
             + (f"; not in this model: {', '.join(missing)}" if missing else "") + "."
             + (" Without pads/pins the recovery of bodies mistaken for pads or pins "
                "has nothing to compare." if {"pads", "pins"} & set(missing) else ""))
    height, width = photo.shape[:2]
    views = list(_views(width, height, chosen["tile_sizes"], overlap))
    emit(f"YOLO local: {len(views)} views (tiles: "
         + (", ".join(f"{size} px" for size in chosen["tile_sizes"]) or "none, whole photo only")
         + f"; tile overlap {overlap:g}); confidence {threshold:g}"
         + (" (default)" if threshold == _CONFIDENCE else " (chosen)") + "; "
         f"audit floor {_AUDIT_CONFIDENCE}; class-agnostic NMS IoU {nms_iou:g}; CPU inference.")
    proposals, candidates, view_records, fragments, whole = [], [], [], [], []
    nms_trace = [] if audit_dir is not None else None
    for view, bounds in views:
        x, y, x1, y1 = bounds
        tensor, ratio, left, top = _letterbox(photo[y:y1, x:x1], np, cv2)
        try:
            result = session.run(None, {input_name: tensor})
            if (len(result) != 1 or not isinstance(result[0], np.ndarray)
                    or result[0].shape != expected_shape or result[0].dtype != np.float32):
                # La forma misurata al caricamento, per ogni modello: 8400 righe
                # per l'ONNX (contratto invariato), quella della testa per un .pt.
                raise ValueError(f"expected float32 output {list(expected_shape)}")
        except Exception as exc:
            raise ValueError(f"Local YOLO inference failed in {view}: {exc}") from exc
        rows, invalid = _proposals(result[0], bounds, (width, height),
                                   (ratio, left, top), view, np, threshold)
        eligible = [row for row in rows if row["rejection"] is None]
        selected = _nms(eligible, np, nms_trace, "eligible_per_view", nms_iou)
        candidates.extend(selected)
        # Raccolti sempre, non solo per l'audit: senza di loro un corpo piu'
        # grande della sovrapposizione fra tessere non arriva da nessuna parte.
        fragments.extend(row for row in rows if row["rejection"] == "clipped_by_tile")
        # Chi ha visto un corpo per intero conta come misura anche sotto la
        # soglia operativa: e' un fatto geometrico, non un giudizio.
        whole.extend(row for row in rows if not row["clipped_sides"]
                     and row["rejection"] in (None, "below_confidence"))
        if audit_dir is not None or include_review_candidates:
            proposals.extend(rows)
        view_records.append({"name": view, "bounds": list(bounds), "scale": ratio,
                             "padding_left_top": [left, top], "invalid_rows": invalid,
                             "valid_rows_below_audit_floor": result[0].shape[2] - invalid - len(rows),
                             "filter_rejections": dict(Counter(
                                 row["rejection"] for row in rows if row["rejection"])),
                             "eligible_before_nms": len(eligible),
                             "raw_proposals": len(rows), "retained_before_global_nms": len(selected)})
        emit(f"YOLO local {view}: {len(selected)} candidates; {invalid} invalid rows rejected.")
    retained = _nms(candidates, np, nms_trace, "global", nms_iou)
    # Cio' che nessuna vista ha visto intero torna in gioco: sopra soglia come
    # candidato, sotto soglia come ipotesi da rivedere, come qualunque altro.
    recovered = _recover_clipped(retained, fragments, threshold)
    confident = [row for row in recovered if row["rejection"] is None]
    if confident:
        retained = _nms(retained + confident, np, nms_trace, "global_with_recovered", nms_iou)
    retained.sort(key=lambda row: (row["box"][1], row["box"][0]))
    # Chi ha visto un corpo per intero vale come misura anche se non era
    # abbastanza sicuro da diventare un candidato.
    before_preference = len(retained)
    retained = _prefer_complete_views(retained, whole, overlap)
    if len(retained) < before_preference:
        emit(f"YOLO local: {before_preference - len(retained)} clipped detections dropped; "
             "they are larger than the tile overlap, so that view could not measure them, "
             "and a view containing them whole is available.")
    # Presenza e ingombro restano due fatti distinti. Il frammento sicuro che
    # ha ceduto il posto ATTESTA che il corpo c'e': la vista che lo contiene
    # ne da' solo la misura. Se quella vista e' sotto soglia, il corpo esce
    # comunque come corpo a soglia operativa -- sempre, anche senza revisione
    # -- con la marca che la sua geometria viene dalla vista piu' debole.
    # Prima restava «below_confidence»: diventava un'ipotesi da rivedere e
    # spariva del tutto senza include_review_candidates.
    attested = [dict(row, rejection=None, geometry_from_weaker_view=True)
                for row in whole if row.get("attests_presence")
                and row.get("rejection") == "below_confidence"]
    if attested:
        retained = _nms(retained + attested, np, nms_trace, "presence_attested", nms_iou)
        retained.sort(key=lambda row: (row["box"][1], row["box"][0]))
        emit(f"YOLO local: {len(attested)} bodies attested by a confident clipped detection "
             "are kept at the operating threshold; their extent comes from a weaker view "
             "that contains them whole.")
    if recovered:
        emit(f"YOLO local: {len(recovered)} bodies rebuilt from tile seams "
             f"({len(confident)} at the operating threshold); no view saw them whole, "
             "so their extent is truncated and must be reviewed.")
    # Retained detections outrank weak proposals. Excluded classes cannot enter
    # through this route, regardless of audit storage.
    review_proposals = []
    review_trace = [] if audit_dir is not None else None
    if audit_dir is not None or include_review_candidates:
        review_pool = retained + [row for row in proposals if row["rejection"] == "below_confidence"]
        review_pool += [row for row in recovered if row["rejection"] == "below_confidence"]
        review_proposals = _prefer_complete_views(
            [row for row in _nms(review_pool, np, review_trace, "review_pool", nms_iou)
             if row["rejection"] == "below_confidence"],
            whole, overlap)
    # Le proposte deboli tornano VISIBILI nella revisione (utente, 26/09):
    # prima sparivano, perche' sulla scheda di prova erano 35 su 82, quasi
    # tutte su zone vuote, e rendevano piu' rumorosa (e piu' cara) l'unica
    # chiamata a pagamento. Ma una confidenza bassa non e' prova di assenza, e
    # la soglia che le separava viene da quella scheda. Ora escono marcate
    # `_yolo_low_confidence`: la revisione le mostra tratteggiate e NON tenute,
    # e nessun percorso a pagamento le manda al modello se l'utente non le
    # tiene (awaiting_user_keep / split_ai_candidates).
    # I corpi RICOMPOSTI dalle cuciture fra tessere restano come prima, non
    # marcati: sono sotto soglia per un motivo geometrico -- nessuna vista ne
    # ha visto piu' di un pezzo -- non perche' il rilevatore dubiti che il
    # corpo ci sia. Le viste che attestano una presenza non passano da qui:
    # sono gia' fra i corpi a soglia operativa (vedi `attested` sopra).
    rebuilt = [row for row in review_proposals if row.get("recovered_from")]
    weak = [row for row in review_proposals if not row.get("recovered_from")]
    returned = retained + ((rebuilt + weak) if include_review_candidates else [])
    returned.sort(key=lambda row: (row["box"][1], row["box"][0]))
    components = []
    for identifier, row in enumerate(returned, 1):
        x0, y0, x1, y1 = [v / dim for v, dim in zip(row["box"], (width, height, width, height))]
        sx, sy = (x1 - x0) * length_mm, (y1 - y0) * width_mm
        low_confidence = row["rejection"] == "below_confidence" and not row.get("recovered_from")
        # Altezza: stima dalla classe del rilevatore e dalla pianta, mai una
        # misura (photo_heights). Una classe ambigua o senza regola resta il
        # segnaposto dichiarato di sempre.
        estimate = (None if row["class_ambiguous"]
                    else estimate_body_height(row["class"], sx, sy))
        body_height, height_basis = estimate if estimate is not None else (
            1.0, "estimated_placeholder_not_measured")
        components.append({
            "name": f"YOLO candidate {identifier}", "kind": "generic",
            "x_mm": ((x0 + x1) / 2 - 0.5) * length_mm,
            "y_mm": ((y0 + y1) / 2 - 0.5) * width_mm,
            "length_mm": max(sx, sy), "width_mm": min(sx, sy),
            "rotation_deg": 0 if sx >= sy else 90, "height_mm": body_height,
            "_height_source": ("detector_class_" + height_basis if estimate is not None
                               else height_basis),
            "_height_basis": height_basis, "_height_estimated": True,
            "color_hex": "#303030", "marking": "", "confidence": "low",
            "_box": (x0, y0, x1, y1),
            "_source": ("YOLO local, ricomposto da cucitura" if row.get("recovered_from")
                        else "YOLO local"),
            "_uncertain": True, "_id": identifier,
            "_yolo_class": row["class"], "_yolo_confidence": row["confidence"],
            "_yolo_proposal_id": row["proposal_id"],
            "_yolo_class_ambiguous": row["class_ambiguous"],
            # L'ingombro di un corpo ricomposto e' troncato dal bordo che lo ha
            # tagliato: e' un'ipotesi da rivedere, non una misura.
            "_yolo_clipped_recovery": bool(row.get("recovered_from")),
            # Lati che poggiano sul confine di una tessera: non sono misure.
            "_yolo_clipped_sides": list(row.get("clipped_sides") or ()),
            "_yolo_origin": ("low_confidence_review" if row["rejection"] == "below_confidence"
                             else "operating_threshold"),
            "_yolo_review_only": row["rejection"] == "below_confidence",
            # Sotto soglia e non ricomposta: visibile in revisione, ma da
            # tenere a mano prima di qualunque uso (awaiting_user_keep).
            "_yolo_low_confidence": low_confidence,
            # Presenza attestata da un frammento sicuro, ingombro misurato da
            # una vista che lo contiene per intero ma con meno confidenza.
            "_yolo_geometry_from_weaker_view": bool(row.get("geometry_from_weaker_view")),
            **({"_yolo_presence_attested_by": list(row.get("presence_attested_by") or ()),
                "_yolo_presence_confidence": row.get("presence_confidence")}
               if row.get("geometry_from_weaker_view") else {}),
        })
    if audit_dir is not None:
        audit = dict(info, audit_schema_version=2,
                     image_sha256=hashlib.sha256(np.ascontiguousarray(photo).tobytes()).hexdigest(),
                     image_hash_basis="decoded uint8 BGR pixels, C order; shape in image_size",
                     image_size=[width, height], board_size_mm=[length_mm, width_mm],
                     audit_scope="Detector only; proposals below audit floor are not recorded; "
                                 "downstream AI decisions require their own audit.",
                     recipe={"overview": True, "input_size": 640,
                             "tile_sizes": list(chosen["tile_sizes"]),
                             "overlap_fraction": overlap, "confidence": threshold,
                             "raw_proposal_min_confidence": _AUDIT_CONFIDENCE,
                             "include_review_candidates": include_review_candidates,
                             "nms_iou": nms_iou, "class_agnostic": True,
                             # Tutte le passate di NMS che girano, nell'ordine;
                             # review_pool traccia a parte (review_nms_suppressions)
                             # perche' li' le deboli competono con i corpi tenuti.
                             "nms_stages": ["eligible_per_view", "global",
                                            "global_with_recovered", "presence_attested",
                                            "review_pool"],
                             "tile_edge_margin_px": 1, "excluded_classes": sorted(_EXCLUDED),
                             "excluded_class_policy": "retain uncertain competing body class "
                                                      "only at ordinary confidence threshold",
                             "component_limit": None},
                     views=view_records, raw_proposals=proposals,
                     nms_suppressions=nms_trace, review_nms_suppressions=review_trace,
                     recovered_clipped_fragments=recovered,
                     low_confidence_review_proposals=review_proposals,
                     retained_detector_results=retained, components=components)
        emit(f"YOLO local audit: {_write_audit(audit_dir, audit)}")
    if audit_dir is not None or include_review_candidates:
        if include_review_candidates:
            emit(tx("YOLO locale: {n_review_proposals} ipotesi sotto la soglia operativa. "
                    "{n_rebuilt} ricomposte dalle cuciture fra tessere entrano come candidati "
                    "dubbi; le altre {value} compaiono nella revisione come «bassa "
                    "confidenza», non tenute: arrivano al modello solo se le tieni.",
                    n_review_proposals=len(review_proposals),
                    n_rebuilt=len(rebuilt),
                    value=len(weak)))
        else:
            emit(tx("YOLO local: {n_review_proposals} additional low-confidence body "
                    "hypotheses saved for review in audit only; not returned.",
                    n_review_proposals=len(review_proposals)))
    emit(f"YOLO local: {len(retained)} operating-threshold candidates; "
         f"returned {len(components)} generic hypotheses; heights are placeholders.")
    return components
