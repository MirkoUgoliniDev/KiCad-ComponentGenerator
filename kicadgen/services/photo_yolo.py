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
import re
import tempfile
try:
    from ..core.validation import covered_fraction
    from .messages import end_sentence, tx
    from .photo_audit import AUDIT_RETENTION_PER_KIND, prune_audits
    from .photo_heights import estimate_body_height
except ImportError:  # loaded as a standalone file (import isolation test)
    from kicadgen.core.validation import covered_fraction
    from kicadgen.services.messages import end_sentence, tx
    from kicadgen.services.photo_audit import AUDIT_RETENTION_PER_KIND, prune_audits
    from kicadgen.services.photo_heights import estimate_body_height


_CLASSES = (
    "battery", "button", "buzzer", "capacitor", "clock", "connector", "diode",
    "display", "fuse", "heatsink", "ic", "inductor", "led", "pads", "pins",
    "potentiometer", "relay", "resistor", "switch", "transformer", "transistor",
)
#: DEFAULT operating threshold. It is a property of the model (how well its
#: confidence is calibrated), not of the board: 0.25 suits the base yolov8s
#: ONNX; a YOLO26 trained on Roboflow performs better higher up (user,
#: 2026-09-25). That is why the caller of the detector can choose it
#: (detect_yolo_components(confidence=...)), like the Roboflow slider; here
#: only the starting value remains.
_CONFIDENCE = 0.25
_AUDIT_CONFIDENCE = 0.05


def operating_confidence(value=None) -> float:
    """@brief Validate the operating confidence threshold chosen for a run.

    @param value Threshold in (0.05, 1), or None for the default _CONFIDENCE.
    @return The threshold as a float.
    @throws ValueError For a value that is not a number, a boolean, an
        infinite or NaN value, or a value outside (audit floor, 1): below the
        audit floor the weak proposals would not even be recorded.
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
#: Sides of the enlarged tiles, in pixels of the calibrated photo.
_TILE_SIZES = (640, 1280)
#: Fraction of a tile repeated in the next one. The step is its complement:
#: it is the same constant used by _views, kept here because it decides which
#: body a tile can measure and which it cannot.
_TILE_OVERLAP_FRACTION = 0.25

#: Detector parameters the user can choose (user, 2026-09-25:
#: «Parametri YOLO» (YOLO parameters) modal). The values are the ones used so
#: far: choosing nothing leaves the behaviour identical. Deliberately left
#: out: the fragment union (_FRAGMENT_COVERED, internal and declared fragile),
#: the audit threshold (it does not change the result) and the 640 px input
#: (fixed by the model's training).
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
    from ..app_paths import DEFAULT_YOLO_ONNX, resource_path, yolo_model_path
except ImportError:  # module loaded on its own (test): same rule as the source
    def resource_path(*parts):
        """@brief Resolve a resource path inside the source repository when app_paths is unavailable.

        Fallback: the repository layout.

        @param parts Path components relative to the repository root.
        @return The absolute Path of the resource.
        """
        return Path(__file__).resolve().parents[2].joinpath(*parts)

def detector_classes() -> tuple:
    """@brief Categories the local detector can assign.

    @return The known classes, excluding those that are not mounted bodies.
    @note Bare pads and pin rows are not components: they are also left out
        of the list offered to the reviewer, because choosing one of them
        would be a mistake nobody downstream could correct.
    """
    return tuple(name for name in _CLASSES if name not in _EXCLUDED)


#: Weights formats the detector loads: ONNX with onnxruntime, PyTorch
#: checkpoints with Ultralytics (user's request, 2026-09-25).
_MODEL_SUFFIXES = (".onnx", ".pt")


def resolve_yolo_model(model_path=None) -> Path:
    """@brief Resolve and check a readable, nonempty local weights file.

    @param model_path Optional local path overriding KICADGEN_YOLO_MODEL.
    @return Absolute Path, defaulting to models/yolo/pcb_yolov8s.onnx (app_paths).
    @throws ValueError If the path is invalid, missing, unreadable, or neither
        .onnx nor .pt.
    @note No downloads, deserialization or optional imports occur here. A .pt
        file is a Python pickle: LOADING it (in `_setup`) runs code from the
        file, so only files the user chose themselves are ever loaded.
    """
    selected = model_path if model_path is not None else os.environ.get(
        "KICADGEN_YOLO_MODEL", yolo_model_path(DEFAULT_YOLO_ONNX)
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
            f"Cannot read local YOLO weights {selected!r}: {end_sentence(exc)} Supply model_path "
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
            "Local YOLO runtime is unavailable. Install requirements/yolo.txt "
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
        "KICADGEN_YOLO_MODEL", yolo_model_path(DEFAULT_YOLO_ONNX))
    path = Path(os.fspath(selected))
    return f"{path.name} ({_FORMAT_NAMES.get(path.suffix.lower(), path.suffix or '?')})"


def _image_dependencies():
    """numpy and OpenCV only: what every weights format needs."""
    try:
        import numpy as np
        import cv2
    except (ImportError, OSError) as exc:
        raise ValueError(
            "Local YOLO runtime is unavailable. Install requirements/yolo.txt "
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
        """@brief Wrap an onnxruntime session with a class-index remapping.

        @param session The onnxruntime InferenceSession of the model.
        @param mapping Model class index -> index in _CLASSES.
        @param np The numpy module (passed in so the import stays lazy).
        """
        self._session, self._mapping, self._np = session, list(mapping), np

    def run(self, output_names, feeds):
        """@brief Run the model, then reorder its scores; see _remap_scores.

        @param output_names Output names forwarded to the wrapped onnxruntime session.
        @param feeds Input feeds forwarded to the wrapped session.
        @return The session outputs, with every [1, 4 + classes, rows] array
            remapped to _CLASSES order and the others passed through.
        """
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
        """@brief Wrap a loaded PyTorch detection network.

        @param network The network in evaluation mode.
        @param torch The torch module (passed in so the import stays lazy).
        @param np The numpy module.
        @param input_name Name of the single input feed, as onnxruntime names it.
        @param end_to_end True when the network already returns NMS-filtered
            rows (end-to-end export) instead of the raw score grid.
        @param mapping Optional model class index -> index in _CLASSES;
            identity when omitted.
        """
        self._network, self._torch, self._np = network, torch, np
        self._input = input_name
        self._end_to_end = end_to_end
        # Model class index -> index in _CLASSES (class_mapping).
        self._mapping = list(mapping) if mapping is not None else list(range(len(_CLASSES)))

    def raw_output(self, feeds):
        """@brief Run the network and return its first output as float32, unconverted.

        @param feeds Mapping input name -> [1, 3, 640, 640] float32 letterboxed tensor.
        @return The first network output as a contiguous float32 numpy array.
        """
        np = self._np
        tensor = self._torch.from_numpy(feeds[self._input])
        with self._torch.no_grad():
            output = self._network(tensor)
        raw = output[0] if isinstance(output, (list, tuple)) else output
        return np.ascontiguousarray(raw.detach().cpu().numpy(), dtype=np.float32)

    def run(self, output_names, feeds):
        """@brief Run the network on one letterboxed tensor; return [raw output].

        @param output_names Ignored; kept for the onnxruntime `run` signature.
        @param feeds Mapping input name -> [1, 3, 640, 640] float32 letterboxed tensor.
        @return A one-element list holding the raw [1, 4 + classes, rows]
            float32 array, scores remapped to _CLASSES order when needed.
        """
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
    # The detector is local: on import Ultralytics checks the connection
    # (DNS lookups towards Cloudflare and Google) and may sync settings.
    # YOLO_OFFLINE turns this off; a value already chosen by the user is left
    # as it is (HANDOFF 131, block B: found by the network guard of the
    # tests).
    import os
    os.environ.setdefault("YOLO_OFFLINE", "1")
    try:
        import torch
        import ultralytics
        from ultralytics import YOLO
    except (ImportError, OSError) as exc:
        raise ValueError(
            "PyTorch support for .pt models is not installed. Install "
            "requirements/yolo-pt.txt in the application's Python environment, or "
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
        # End-to-end head (YOLO26, YOLOv10): its official one-to-one inference
        # is ALWAYS used, forced here because the default changes between
        # Ultralytics versions (8.4.37 on, 8.4.163 off).
        # Verified on 2026-09-25 on a real photo: the one-to-many branch with
        # our NMS found 38 bodies and recovered 39 of the 46 of the
        # one-to-one; the shortcut changed the model, not just the code.
        # The one-to-one head ranks anchors x classes together, so the same
        # anchor can come back with several classes: _one_to_one_rows merges
        # them and the recovery «pads/pins con un corpo concorrente»
        # (pads/pins with a competing body) applies as for the ONNX.
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
            f"Cannot load compatible local YOLO model {path}: {end_sentence(exc)} Use a PCB YOLO "
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
            f"Cannot load compatible local YOLO model {path}: {end_sentence(exc)} Use a "
            "self-contained PCB YOLOv8 ONNX export with fixed 640 input, raw "
            "[1, 4 + classes, 8400] output and classes among: " + ", ".join(_CLASSES)
            + "; check the CPU onnxruntime installation."
        ) from exc
    # The detector reads the scores in the order of _CLASSES: a model with a
    # different table goes through a session that reorders them. With the
    # detector's table the session stays the onnxruntime one.
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
    """@brief How much one tile overlaps the next, in pixels.

    @param view Name of the view, as _views generates it.
    @param overlap Fraction of the tile that is repeated, the same given to _views.
    @return Overlap in pixels, or infinity for the whole-board view.
    @note This is the quantity that decides what that view can measure: a
        body larger than the overlap touches an edge in EVERY tile that
        contains it, so its extent cannot be observed from there, however
        confident the detection is.
    """
    name = str(view or "")
    if not name.startswith("tile"):
        return float("inf")
    try:
        size = int(name.split("-")[0][4:])
    except ValueError:
        return float("inf")
    return size * overlap


def _prefer_complete_views(rows, complete, overlap=_TILE_OVERLAP_FRACTION, iou=_NMS_IOU):
    """@brief Drop the fragments that no tile could measure.

    @param rows Rows to filter.
    @param complete Unclipped detections, from ANY view and at any
        confidence: that a view saw the body whole is a geometric fact and
        does not depend on the operating threshold. Restricting it to the
        candidates above threshold let through exactly the case that
        matters -- on the test board the whole-board view saw the entire
        module at 0.18, below threshold, and the comparison did not find it.
    @param overlap Overlap fraction between tiles.
    @param iou Identity criterion of the NMS: a whole view counts only when
        it is the SAME body as the fragment (`_same_body_as_fragment`), not
        when it merely contains it. With containment alone a weak box as
        large as the board (ESP32-S3-DEV-KIT, 08/10: 61x15 mm at 0.055)
        "covered" a confident fragment and was promoted to a real body.
    @return The rows to keep.
    @note A physical rule, not a threshold: a body larger than the overlap
        between tiles touches an edge in every tile that contains it, so that
        view does not know its extent. If another view contains it whole,
        that view measures it and the fragment adds nothing -- indeed it is
        what created the false agreement on a tile edge. Below the overlap
        the rule does not apply: a small body grazing a seam remains an
        observed body.
    @note Side effect: when the dropped fragment was at the operating
        threshold and no covering view is, the most confident covering view
        gets `attests_presence` True, `presence_attested_by` (fragment ids)
        and `presence_confidence`. detect_yolo_components then emits it as an
        operating-threshold body whose geometry comes from a weaker view.
    @note Performance only: the covering views are searched just for the rows
        the rule can drop (clipped and larger than the overlap), with a
        vectorised prefilter confirmed by the scalar `_cover`; the kept rows,
        their order and the side effects are identical to testing every pair.
    """
    rows, complete = list(rows), list(complete)
    measurable = []
    for index, row in enumerate(rows):
        box = row["box"]
        size = max(box[2] - box[0], box[3] - box[1])
        if row.get("clipped_sides") and size > view_overlap_px(row.get("view"), overlap):
            measurable.append(index)
    covering_of = _covering_views([rows[i] for i in measurable], complete, iou)
    covering_of = dict(zip(measurable, covering_of))
    keep = []
    for index, row in enumerate(rows):
        covering = covering_of.get(index)
        if covering:
            # Presence and footprint are two distinct facts. If the fragment
            # giving way was above the operating threshold, the PRESENCE of
            # the body is attested by a confident detection: only the
            # footprint measurement gives way, passing to the view that
            # contains it. That view must therefore be kept even if weak,
            # otherwise a body that had been seen perfectly would be lost twice.
            # If a view that contains it is already above threshold, presence
            # is already carried by it: nothing to promote. Otherwise ONE view
            # only is promoted, the most confident, so as not to multiply the
            # same body.
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


#: Rows compared at once by the vectorised cover prefilter (bounds memory to
#: a few MB per chunk, whatever the number of complete views).
_COVER_CHUNK = 256


def _same_body_as_fragment(row, other, iou=_NMS_IOU):
    """@brief Tell whether two detections, one or both clipped by a tile, are the same body.

    @param row Detection whose `clipped_sides` rest on a tile seam; its other
        sides are edges the detector actually saw.
    @param other Second detection, whole or clipped in turn.
    @param iou Identity criterion, the same one NMS uses for "same body".
    @return True when the two boxes, compared only where BOTH tiles saw, match.
    @note Containment is not identity. A box as large as the board contains
        every fragment, yet it is not the body the fragment saw. Each box is
        cut where the other one was cut by its tile, so both describe the
        region seen by both views; there the same body gives the same box
        under the ordinary NMS identity -- no new threshold. For a whole view
        only the fragment's seams apply; two halves of one body on either
        side of a seam reduce to the same strip of tile overlap.
    """
    first, second = list(row["box"][:4]), list(other["box"][:4])
    index_of = {"left": 0, "top": 1, "right": 2, "bottom": 3}
    for source, target in ((row, second), (other, first)):
        for side in source.get("clipped_sides") or ():
            index = index_of.get(side)
            if index is None:
                continue
            cut = source["box"][index]
            target[index] = max(target[index], cut) if index < 2 else min(target[index], cut)
    wide = min(first[2], second[2]) - max(first[0], second[0])
    high = min(first[3], second[3]) - max(first[1], second[1])
    if wide <= 0 or high <= 0:
        return False
    shared = wide * high
    union = ((first[2] - first[0]) * (first[3] - first[1])
             + (second[2] - second[0]) * (second[3] - second[1]) - shared)
    return union > 0 and shared / union > iou


def _covering_views(rows, complete, iou=_NMS_IOU):
    """For each row, the complete views of the SAME body (see `_same_body_as_fragment`).

    A view qualifies when it holds at least _FRAGMENT_COVERED of the row's box
    and, cut at the row's seams, matches it under the NMS identity `iou`.
    Same result, in the same order of `complete`, as testing every pair with
    the scalar checks. NumPy only prefilters: its margin of 1e-9 below the
    threshold, and any NaN, send a pair to the scalar `_cover`, which alone
    decides, so rounding or NaN ordering differences between the two can never
    change a verdict. Boxes that NumPy cannot read as numbers use the scalar path.
    """
    if not rows or not complete:
        return [[] for _ in rows]
    def scalar(row):
        return [other for other in complete if other is not row
                and _cover(row["box"], other["box"]) >= _FRAGMENT_COVERED
                and _same_body_as_fragment(row, other, iou)]
    try:
        import numpy as np
        views = np.asarray([other["box"][:4] for other in complete], dtype=np.float64)
        boxes = np.asarray([row["box"][:4] for row in rows], dtype=np.float64)
        if views.shape != (len(complete), 4) or boxes.shape != (len(rows), 4):
            raise ValueError("box shape")
    except (ImportError, TypeError, ValueError, KeyError, IndexError):
        return [scalar(row) for row in rows]
    result = []
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        for start in range(0, len(rows), _COVER_CHUNK):
            first = boxes[start:start + _COVER_CHUNK, None, :]
            wide = np.maximum(0.0, np.minimum(first[..., 2], views[None, :, 2])
                              - np.maximum(first[..., 0], views[None, :, 0]))
            high = np.maximum(0.0, np.minimum(first[..., 3], views[None, :, 3])
                              - np.maximum(first[..., 1], views[None, :, 1]))
            area = (first[..., 2] - first[..., 0]) * (first[..., 3] - first[..., 1])
            cover = wide * high / area
            maybe = (cover >= _FRAGMENT_COVERED - 1e-9) | np.isnan(cover)
            for offset, columns in enumerate(maybe):
                row = rows[start + offset]
                result.append([complete[j] for j in np.flatnonzero(columns)
                               if complete[j] is not row
                               and _cover(row["box"], complete[j]["box"]) >= _FRAGMENT_COVERED
                               and _same_body_as_fragment(row, complete[j], iou)])
    return result


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


#: How much of a fragment must lie inside a whole detection for it to be the
#: same body, already seen better: below this fraction of its own area the
#: fragment is the only evidence that the body exists.
_FRAGMENT_COVERED = 0.5


#: Fraction of the first rectangle inside the second (shared implementation;
#: the private name stays because callers and tests refer to it).
_cover = covered_fraction


def _recover_clipped(retained, fragments, confidence=_CONFIDENCE, iou=_NMS_IOU):
    """@brief Reassemble the bodies that no view saw whole.

    @param retained Interior detections that survived the global NMS.
    @param fragments Rows discarded because they touched a tile edge.
    @param confidence Operating threshold.
    @param iou Identity criterion of the NMS (`_same_body_as_fragment`).
    @return Reassembled rows, with the best confidence of the group.
    @note A tile cut is not proof of absence. The rule that discards the
        fragments exists to remove the duplicates of what another view sees
        whole: when NO view sees it whole, discarding means losing the body.
        It happens to whatever is larger than the overlap between tiles --
        160 px on 640 px tiles -- and then the body touches an edge in every
        tile that contains it. The overlaps could be widened, but no fixed
        width is guaranteed: the extent of the bodies is not known before
        seeing them.
    @note The union of the fragments remains a TRUNCATED extent when the cut
        lies on an edge that no tile goes beyond: it is a hypothesis to
        review, not a measurement, and the receiver marks it as such.
    @note The grouping uses the identity on the strips seen by both tiles
        (since 2026-10-08); the earlier measurements (31 fragments, union up
        to four times the seed) held for the old containment-only criterion
        and no longer describe this one. The reassembled extent remains in
        any case a hypothesis to review, not a measurement.
    """
    orphans = [row for row in fragments
               if not any(_cover(row["box"], kept["box"]) >= _FRAGMENT_COVERED
                          and _same_body_as_fragment(row, kept, iou)
                          for kept in retained)]
    # Grouping by IDENTITY, not by containment: two pieces are the same body
    # only if they coincide where both tiles see (_same_body_as_fragment). The
    # groups are the connected components of this relation, so a body cut by
    # two seams (three tiles, or one vertical and one horizontal seam) comes
    # back as a single piece even if the first and last pieces are never seen
    # together. Chaining had been removed because, with containment alone, the
    # union walked across the board; with identity on the shared strips every
    # link must measure the same body, and two different bodies side by side
    # in a strip do not get linked.
    # A piece with no links stays a group on its own.
    ordered = sorted(orphans, key=lambda row: -row["confidence"])
    parent = list(range(len(ordered)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for first in range(len(ordered)):
        for second in range(first + 1, len(ordered)):
            # Only between DIFFERENT views: within the same tile the boxes are
            # alternative hypotheses of the same detector pass, and chained the
            # union would grow from hypothesis to hypothesis (measured on
            # ESP32-S3, 08/10: 13 boxes of the same tile, from 3.3 to 10 mm
            # tall, linked into a single strip). Duplicates are left to the NMS.
            if (ordered[first].get("view") != ordered[second].get("view")
                    and _same_body_as_fragment(ordered[first], ordered[second], iou)):
                parent[root(second)] = root(first)
    by_root = {}
    for index, row in enumerate(ordered):
        by_root.setdefault(root(index), []).append(row)
    groups = list(by_root.values())
    recovered = []
    for group in groups:
        best = max(group, key=lambda row: row["confidence"])
        box = [min(row["box"][0] for row in group), min(row["box"][1] for row in group),
               max(row["box"][2] for row in group), max(row["box"][3] for row in group)]
        # After the union a side stays artificial only if ALL the fragments
        # touching it were cut there: if another piece sees beyond it, that
        # edge was really measured.
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


def _on_body_surface(retained, others, container_confidence):
    """@brief Mark the detections that lie on the top surface of a body already found.

    @param retained Rows kept at the operating threshold (internal dicts).
    @param others Further rows to test (the weak review proposals); may be empty.
    @param container_confidence Minimum confidence of a box allowed to act as
        a container (the caller passes max(operating threshold, _CONFIDENCE)).
    @return List of (proposal_id, container proposal_id) pairs; each marked
        row gets ``on_body_of`` = the container's proposal id, and
        ``abuts_body`` True when its centre is outside but its box overlaps
        the container (a pad or lead on the body's edge).
    @note Physical fact: two components cannot occupy the same footprint on
        the top side, so a smaller box whose CENTRE lies on a body's box
        describes that body's surface (a shield can, a label, a lead, a
        connector mounted on a module) -- unless the large box is not one body.
        Containers are therefore only boxes seen whole by one view (no clipped
        side, not rebuilt from seams, not measured by a weaker view) at
        container_confidence or above. Measured on two boards (ESP32-S3
        module board, RS485 board, 2026-10-08): real bodies overlap no larger
        container at all (0.00), surface features 0.51-1.00; real containers
        were 0.67-0.92, spurious board-sized boxes <= 0.10.
    @note HEURISTIC, declared as such: which box may be a container depends on
        its confidence alone, with the factory threshold _CONFIDENCE as the
        floor even when a person lowers the operating threshold (at 0.05 a
        board-sized box at 0.06 would demote real parts). Validated on two
        boards only. Marked rows are DEMOTED, never deleted or merged: they stay
        visible in the review and a person can keep them, so a real part
        inside an over-large confident box is not lost (the old regression of
        passives absorbed into a large IC is not reintroduced).
    """
    def area(box):
        return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])

    containers = sorted(
        (row for row in retained
         if row.get("rejection") is None and not row.get("recovered_from")
         and not row.get("clipped_sides") and not row.get("geometry_from_weaker_view")
         and row["confidence"] >= container_confidence
         and all(math.isfinite(value) for value in row["box"][:4])),
        key=lambda row: -area(row["box"]))
    links = []
    for container in containers:
        if container.get("on_body_of"):
            continue
        x0, y0, x1, y1 = container["box"][:4]
        size = area(container["box"])
        for row in list(retained) + list(others):
            if row is container or row.get("on_body_of"):
                continue
            box = row["box"]
            if not all(math.isfinite(value) for value in box[:4]) or area(box) >= size:
                continue
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                row["on_body_of"] = container["proposal_id"]
                links.append((row["proposal_id"], container["proposal_id"]))
            elif (min(box[2], x1) > max(box[0], x0)
                    and min(box[3], y1) > max(box[1], y0)):
                # Centre outside but the boxes OVERLAP: a contact pad or lead
                # of the body (castellation, terminal), which belongs to the
                # body's own footprint, or a feature straddling its edge.
                # Two distinct bodies keep a courtyard gap between them
                # (measured >= 0.35 mm on both boards: 18.7 px ESP32, 8.0 px
                # RS485), so their boxes do not overlap; a proximity margin
                # was rejected because on the 23 px/mm RS485 photo the box
                # uncertainty equals that gap (zero margin). No new number.
                row["on_body_of"] = container["proposal_id"]
                row["abuts_body"] = True
                links.append((row["proposal_id"], container["proposal_id"]))
    return links


#: Edge uncertainty of a detector box, in MODEL-INPUT pixels (the 640 px
#: letterboxed tensor). HEURISTIC, declared as such: it is the finest feature
#: stride (P3 = 8 px) of the YOLOv8/v11/26 detection heads the loader accepts,
#: i.e. the cell size at which the head regresses the edges of small bodies.
#: Measured on the ESP32-S3 photo (2026-10-08, 640 px tiles, ratio 1): boxes
#: of a shield can cut by the tile seam at 640 px ended at 636-638 px, 2-4 px
#: inside the cut, so the previous 1 px tolerance took a cut piece for a whole
#: body. Reading the strides from the weights would make this exact; the ONNX
#: metadata only exposes `stride` = 32, the COARSEST level (the input-size
#: divisibility the loader checks), which says nothing about edge resolution,
#: and the .pt head's `stride` tensor [8, 16, 32] is not threaded into the
#: per-view proposal step. A model family with a different finest stride
#: would need this value read from its head.
#: It is NOT the outline uncertainty of a measurement on the photo
#: (cad3d.measurement_uncertainty.EDGE_UNCERTAINTY_PX, the single source for
#: that quantity): this one is the regression cell of the DETECTOR, in pixels
#: of the model input, and only the tile-seam test uses it (review 2026-10-27 F4).
_BOX_UNCERTAINTY_INPUT_PX = 8


def _tile_edge_margin_px(ratio):
    """@brief Distance from an interior tile edge within which a box side counts as cut.

    @param ratio Letterbox scale of the view (model-input px per photo px),
        as returned by _letterbox; must be finite and positive.
    @return Margin in PHOTO pixels of that view: the detector's edge
        uncertainty (_BOX_UNCERTAINTY_INPUT_PX) scaled back to the photo, plus
        the 1 px that absorbs the letterbox rounding.
    @note Rule: a tile does not measure a body whose box ends within the
        detector's own edge uncertainty of the tile's cut, because the head
        cannot tell "the body ends here" from "the image ends here" at that
        resolution. A magnified tile (ratio 1 on a 640 px tile) gets 9 px; a
        1280 px tile (ratio 0.5) gets 17 px. Image borders are real edges and
        are never subject to this rule.
    """
    return _BOX_UNCERTAINTY_INPUT_PX / ratio + 1


def _proposals(raw, bounds, image_size, transform, view, np, confidence_threshold=_CONFIDENCE):
    """@brief Keep auditable pre-NMS proposals; reject malformed or cropped body boxes.

    @param raw Raw head output [1, 4 + classes, rows] for this view.
    @param bounds (x, y, x1, y1) of the view in photo pixels.
    @param image_size (width, height) of the photo.
    @param transform (ratio, left, top) of the view's letterbox.
    @param view Name of the view (proposal ids are prefixed with it).
    @param np NumPy module.
    @param confidence_threshold Operating threshold.
    @return (rows, invalid_count): proposal dicts at the audit floor and the
        number of malformed rows.
    @note A side is `clipped` when it lies within _tile_edge_margin_px(ratio)
        of an INTERIOR tile edge: the tile cut the body there (or could have,
        within the detector's edge uncertainty), so the box is not a measure
        of that body. Sides on the image border are real edges, never clipped.
    """
    x, y, x1, y1 = bounds
    width, height = image_size
    ratio, left, top = transform
    margin = _tile_edge_margin_px(ratio)
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
        # Which sides rest on the TILE BOUNDARY and not on the body: they are
        # cropping artefacts, not measured edges, and the receiver must know
        # it. An inner tile edge is left out of the count when it coincides
        # with the image edge, which is real instead.
        # The margin is not 1 px: it is the detector's uncertainty on the box
        # edge (_tile_edge_margin_px), because a body cut by the tile comes
        # back with its side a few pixels INSIDE the cut (ESP32-S3, 08/10:
        # 636-638 out of 640) and with 1 px it passed as whole.
        clipped_sides = [name for name, touching in (
            ('left', x > 0 and local[0] <= margin),
            ('top', y > 0 and local[1] <= margin),
            ('right', x1 < width and local[2] >= x1 - x - margin),
            ('bottom', y1 < height and local[3] >= y1 - y - margin)) if touching]
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
        seams below the threshold are flagged too (user, 2026-10-08): their
        confidence is that of their best piece, so no piece reached it.
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


#: Field keeping the detector's own `_yolo_review_only` verdict once a
#: reviewer's action has overwritten the live one (user decision 2026-10-16).
#: Deliberately without the `_yolo_` prefix: every `_yolo_*` key is forwarded
#: as `detector_metadata` to the paid inventory request
#: (photo_global_inventory), and a bookkeeping field of the review window must
#: not change that request nor its cache key.
DETECTOR_REVIEW_ONLY_FIELD = "_detector_review_only"


def set_review_only(proposal, value):
    """@brief Write `_yolo_review_only` on a proposal, keeping the detector's original verdict.

    @param proposal Detector proposal (dict), modified in place.
    @param value New boolean value of `_yolo_review_only`.
    @note The one writer of `_yolo_review_only` after the detector: the first
        time a reviewer's action (keep, leave out, edit, move, resize) changes
        it, the value the detector wrote is stored once in
        DETECTOR_REVIEW_ONLY_FIELD and never overwritten afterwards, so the
        detector's own strong/weak count survives every later gesture
        and every save/reopen of the review bundle.
    """
    if DETECTOR_REVIEW_ONLY_FIELD not in proposal:
        proposal[DETECTOR_REVIEW_ONLY_FIELD] = bool(proposal.get("_yolo_review_only"))
    proposal["_yolo_review_only"] = bool(value)


def detector_review_only(proposal) -> bool:
    """@brief Tell whether the DETECTOR left this proposal for review (a «debole» one).

    @param proposal Detector proposal (dict).
    @return The stored detector verdict when a reviewer's action overwrote
        `_yolo_review_only`; otherwise `_yolo_review_only` itself.
    @note The fallback is exact, not an estimate: a proposal without
        DETECTOR_REVIEW_ONLY_FIELD was never touched through set_review_only,
        so its live value is still the detector's. The only case it cannot
        recover is a proposal changed and saved by a version older than the
        field: there the reviewer's choice already replaced the detector's
        verdict on disk and nothing else records it.
    """
    if DETECTOR_REVIEW_ONLY_FIELD in proposal:
        return bool(proposal[DETECTOR_REVIEW_ONLY_FIELD])
    return bool(proposal.get("_yolo_review_only"))


#: Declared provenance of a box the user has moved, resized or drawn: the
#: same string for all three cases, because downstream they are the same
#: thing -- the eye of the person looking.
MANUAL_SOURCE = "revisione manuale"

#: «interno» (inner) handle of mark_manual_box: a move, no side looked at.
MOVE_HANDLE = (0, 0)


def box_geometry_mm(box, length_mm, width_mm):
    """@brief Board-millimetre geometry of a normalised full-board box.

    @param box Normalised rectangle (x0, y0, x1, y1) on the calibrated crop.
    @param length_mm Board extent along X, in mm.
    @param width_mm Board extent along Y, in mm.
    @return dict(x_mm, y_mm, length_mm, width_mm, rotation_deg), the
        detector's own convention: origin at the board centre, X right, Y
        down, length = long side, rotation 90 when the long side lies along Y.
    @note The one conversion shared by the detector rows, the review windows
        and the hand-drawn boxes, so no box can land on a different scale.
    """
    x0, y0, x1, y1 = box
    sx, sy = (x1 - x0) * float(length_mm), (y1 - y0) * float(width_mm)
    return {
        "x_mm": ((x0 + x1) / 2 - 0.5) * float(length_mm),
        "y_mm": ((y0 + y1) / 2 - 0.5) * float(width_mm),
        "length_mm": max(sx, sy), "width_mm": min(sx, sy),
        "rotation_deg": 0 if sx >= sy else 90,
    }


def mark_manual_box(proposal, handle=MOVE_HANDLE):
    """@brief Declare that the geometry of `proposal` is the one the reviewer saw.

    @param proposal Detector proposal, modified in place.
    @param handle Handle the box was edited with, as photo-frame signs
        (hx, hy) in {-1, 0, 1}: -1 the left/top side, +1 the right/bottom
        side, MOVE_HANDLE for a move. It decides which sides were looked at.
    @note `_yolo_manual_box` carries the `_yolo_` prefix on purpose: it is
        forwarded in the `detector_metadata` of the request, and the model
        must know that box was not measured by a machine. Marks that lapse:
        `_edges_measured` (OpenCV edge measurement) goes, the edge is now
        where the eye put it; of `_yolo_clipped_sides` (sides resting on a
        tile seam, not measurements) ONLY the side the handle dragged lapses,
        a move keeps the list as it is so the model keeps knowing that edge
        is an artefact; `_yolo_clipped_recovery` stays declared while at
        least one clipped side remains. The previous provenance is kept in
        `_yolo_source_before_manual`.
    @note Reviewer's decision: the hand of whoever looks IS the decision of
        presence. A weak proposal (`_yolo_review_only`) that was moved or
        resized is no longer a low-confidence hypothesis -- the eye verified
        it -- so `_yolo_review_only` becomes False (through set_review_only,
        which keeps the detector's verdict for its own count), the previous
        origin kept in `_yolo_origin_before_manual`. Same rule for the sub-threshold ones:
        touching one is keeping it (`_yolo_user_kept`, awaiting_user_keep).
    """
    if not proposal.get("_yolo_manual_box"):
        proposal["_yolo_source_before_manual"] = proposal.get("_source")
    proposal["_yolo_manual_box"] = True
    proposal["_source"] = MANUAL_SOURCE
    proposal.pop("_edges_measured", None)
    hx, hy = handle
    looked_at = set()
    if hx < 0:
        looked_at.add("left")
    elif hx > 0:
        looked_at.add("right")
    if hy < 0:
        looked_at.add("top")
    elif hy > 0:
        looked_at.add("bottom")
    clipped = [side for side in (proposal.get("_yolo_clipped_sides") or ())
               if side not in looked_at]
    proposal["_yolo_clipped_sides"] = clipped
    if not clipped:
        proposal["_yolo_clipped_recovery"] = False
    if proposal.get("_yolo_review_only"):
        if "_yolo_origin" in proposal and "_yolo_origin_before_manual" not in proposal:
            proposal["_yolo_origin_before_manual"] = proposal["_yolo_origin"]
        set_review_only(proposal, False)
    if proposal.get("_yolo_low_confidence"):
        proposal["_yolo_user_kept"] = True


def manual_proposal(box, identifier, length_mm, width_mm):
    """@brief A proposal for a body the detector did not see, drawn by the reviewer.

    @param box Normalised rectangle (x0, y0, x1, y1) on the calibrated crop.
    @param identifier `_id` not used by any other proposal of the review.
    @param length_mm Board extent along X, in mm.
    @param width_mm Board extent along Y, in mm.
    @return A new proposal row in the detector's format: `generic` kind,
        placeholder height, no class (the model decides it from the photo),
        origin `user_added`, `_yolo_manual_box` True and MANUAL_SOURCE, so
        downstream it is indistinguishable from a retouched box.
    """
    x0, y0, x1, y1 = box
    return {
        "name": f"Corpo aggiunto {identifier}", "kind": "generic",
        **box_geometry_mm(box, length_mm, width_mm), "height_mm": 1.0,
        "_height_source": "estimated_placeholder_not_measured", "_height_estimated": True,
        "color_hex": "#303030", "marking": "", "confidence": "low",
        "_box": (x0, y0, x1, y1), "_source": MANUAL_SOURCE,
        "_uncertain": True, "_id": identifier,
        "_yolo_class": "",
        "_yolo_confidence": None, "_yolo_proposal_id": None,
        "_yolo_class_ambiguous": False,
        "_yolo_origin": "user_added", "_yolo_review_only": False,
        "_yolo_manual_box": True,
    }


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


#: File names written by _write_audit (tempfile adds 8 random characters).
_AUDIT_NAME = re.compile(r"yolo-[A-Za-z0-9_]+\.json")


def _write_audit(audit_dir, document, keep=AUDIT_RETENTION_PER_KIND):
    """@brief Write one local-detector audit and prune older ``yolo-*`` audits.

    @param audit_dir Directory receiving a unique ``yolo-*.json`` file.
    @param document JSON-serializable audit content.
    @param keep Retention limit for detector audits (AUDIT_RETENTION_PER_KIND).
    @return Path of the written audit, as a string.
    @throws ValueError If the audit cannot be written.
    @note Only files named like detector audits are pruned, never the one just
        written; other audit kinds in the same directory are untouched.
    """
    try:
        directory = Path(audit_dir)
        directory.mkdir(parents=True, exist_ok=True)
        # A unique filename preserves previous runs in a shared audit directory.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="yolo-", suffix=".json",
            dir=directory, delete=False,
        ) as handle:
            json.dump(document, handle, indent=2, allow_nan=False)
            written = handle.name
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"Cannot write YOLO audit under {audit_dir!r}: {exc}") from exc
    prune_audits(directory, _AUDIT_NAME, keep, protect=written)
    return written


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
        compatibility. All of them, those REBUILT from tile seams included
        (since 2026-10-08), come back flagged `_yolo_low_confidence` True: shown in the review as low
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
    # Only numpy/OpenCV here: onnxruntime is needed only for .onnx (see _setup).
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
                # The shape measured at load time, for each model: 8400 rows
                # for the ONNX (contract unchanged), the head's one for a .pt.
                raise ValueError(f"expected float32 output {list(expected_shape)}")
        except Exception as exc:
            raise ValueError(f"Local YOLO inference failed in {view}: {exc}") from exc
        rows, invalid = _proposals(result[0], bounds, (width, height),
                                   (ratio, left, top), view, np, threshold)
        eligible = [row for row in rows if row["rejection"] is None]
        selected = _nms(eligible, np, nms_trace, "eligible_per_view", nms_iou)
        candidates.extend(selected)
        # Always collected, not only for the audit: without them a body larger
        # than the overlap between tiles gets nowhere.
        fragments.extend(row for row in rows if row["rejection"] == "clipped_by_tile")
        # A view that saw a body whole counts as a measurement even below the
        # operating threshold: it is a geometric fact, not a judgement.
        whole.extend(row for row in rows if not row["clipped_sides"]
                     and row["rejection"] in (None, "below_confidence"))
        if audit_dir is not None or include_review_candidates:
            proposals.extend(rows)
        view_records.append({"name": view, "bounds": list(bounds), "scale": ratio,
                             "padding_left_top": [left, top],
                             "tile_edge_margin_px": _tile_edge_margin_px(ratio),
                             "invalid_rows": invalid,
                             "valid_rows_below_audit_floor": result[0].shape[2] - invalid - len(rows),
                             "filter_rejections": dict(Counter(
                                 row["rejection"] for row in rows if row["rejection"])),
                             "eligible_before_nms": len(eligible),
                             "raw_proposals": len(rows), "retained_before_global_nms": len(selected)})
        emit(f"YOLO local {view}: {len(selected)} candidates; {invalid} invalid rows rejected.")
    retained = _nms(candidates, np, nms_trace, "global", nms_iou)
    # What no view saw whole comes back into play: above threshold as a
    # candidate, below threshold as a hypothesis to review, like any other.
    recovered = _recover_clipped(retained, fragments, threshold, nms_iou)
    confident = [row for row in recovered if row["rejection"] is None]
    if confident:
        retained = _nms(retained + confident, np, nms_trace, "global_with_recovered", nms_iou)
    retained.sort(key=lambda row: (row["box"][1], row["box"][0]))
    # A view that saw a body whole counts as a measurement even if it was not
    # confident enough to become a candidate.
    before_preference = len(retained)
    retained = _prefer_complete_views(retained, whole, overlap, nms_iou)
    if len(retained) < before_preference:
        emit(f"YOLO local: {before_preference - len(retained)} clipped detections dropped; "
             "they are larger than the tile overlap, so that view could not measure them, "
             "and a view containing them whole is available.")
    # Presence and footprint stay two distinct facts. The confident fragment
    # that gave way ATTESTS that the body is there: the view that contains it
    # only provides its measurement. If that view is below threshold, the body
    # still comes out as an operating-threshold body -- always, even without
    # review -- marked as having its geometry from the weaker view.
    # Before, it stayed «below_confidence»: it became a hypothesis to review
    # and disappeared entirely without include_review_candidates.
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
            whole, overlap, nms_iou)
    # Weak proposals come back VISIBLE in the review (user, 26/09): before,
    # they disappeared, because on the test board they were 35 out of 82,
    # almost all on empty areas, and made the only paid call noisier (and
    # more expensive). But a low confidence is no proof of absence, and the
    # threshold that separated them came from that board. Now they come out
    # marked `_yolo_low_confidence`: the review shows them dashed and NOT
    # kept, and no paid path sends them to the model unless the user keeps
    # them (awaiting_user_keep / split_ai_candidates).
    # Bodies REASSEMBLED from below-threshold seams are marked too (user,
    # 08/10): their confidence is that of the BEST piece, so no piece reached
    # the threshold -- the cut does not explain a low score on every piece.
    # Before, they stayed kept by default and a silkscreen strip at 0.10
    # entered the bodies. Views that attest a presence do not pass through
    # here: they are already among the operating-threshold bodies (see
    # `attested` above).
    # What lies on the surface of an already found body (shield can, label,
    # leads, connector mounted on the module) is not another body: demoted to
    # a proposal to be kept by hand, never deleted.
    on_body = _on_body_surface(retained, review_proposals, max(threshold, _CONFIDENCE))
    if on_body:
        emit(f"YOLO local: {len(on_body)} boxes lie on a body already found (centre inside "
             "a confident whole box, or overlapping its edge like a pad); demoted to "
             "proposals to keep by hand, not deleted.")
    rebuilt = [row for row in review_proposals if row.get("recovered_from")]
    weak = [row for row in review_proposals if not row.get("recovered_from")]
    returned = retained + ((rebuilt + weak) if include_review_candidates else [])
    returned.sort(key=lambda row: (row["box"][1], row["box"][0]))
    components = []
    for identifier, row in enumerate(returned, 1):
        x0, y0, x1, y1 = [v / dim for v, dim in zip(row["box"], (width, height, width, height))]
        sx, sy = (x1 - x0) * length_mm, (y1 - y0) * width_mm
        low_confidence = row["rejection"] == "below_confidence" or bool(row.get("on_body_of"))
        # Height: estimate from the detector class and the footprint, never a
        # measurement (photo_heights). An ambiguous class or one without a
        # rule keeps the usual declared placeholder.
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
            # The footprint of a reassembled body is truncated by the edge
            # that cut it: it is a hypothesis to review, not a measurement.
            "_yolo_clipped_recovery": bool(row.get("recovered_from")),
            # Sides resting on a tile boundary: they are not measurements.
            "_yolo_clipped_sides": list(row.get("clipped_sides") or ()),
            "_yolo_origin": ("low_confidence_review" if row["rejection"] == "below_confidence"
                             else "operating_threshold"),
            "_yolo_review_only": low_confidence,
            # Below threshold, reassembled or not: visible in the review, but
            # to be kept by hand before any use (awaiting_user_keep).
            "_yolo_low_confidence": low_confidence,
            # On the surface of this body (proposal_id): see _on_body_surface.
            **({"_yolo_on_body_of": row["on_body_of"],
                "_yolo_abuts_body": bool(row.get("abuts_body"))} if row.get("on_body_of") else {}),
            # Presence attested by a confident fragment, footprint measured by
            # a view that contains it whole but with less confidence.
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
                             # All the NMS passes that run, in order;
                             # review_pool is tracked separately (review_nms_suppressions)
                             # because there the weak ones compete with the kept bodies.
                             "nms_stages": ["eligible_per_view", "global",
                                            "global_with_recovered", "presence_attested",
                                            "review_pool"],
                             "on_body_surface_container_confidence": max(threshold, _CONFIDENCE),
                             # Per view: see `tile_edge_margin_px` in `views`.
                             "tile_edge_margin_px": {
                                 "rule": "box_uncertainty_input_px / view_scale + 1",
                                 "box_uncertainty_input_px": _BOX_UNCERTAINTY_INPUT_PX,
                                 "per_view": {record["name"]: record["tile_edge_margin_px"]
                                              for record in view_records}},
                             "excluded_classes": sorted(_EXCLUDED),
                             "excluded_class_policy": "retain uncertain competing body class "
                                                      "only at ordinary confidence threshold",
                             "component_limit": None},
                     views=view_records, raw_proposals=proposals,
                     nms_suppressions=nms_trace, review_nms_suppressions=review_trace,
                     recovered_clipped_fragments=recovered,
                     low_confidence_review_proposals=review_proposals,
                     on_body_surface=[{"proposal_id": child, "on_body_of": parent}
                                      for child, parent in on_body],
                     retained_detector_results=retained, components=components)
        emit(f"YOLO local audit: {_write_audit(audit_dir, audit)}")
    if audit_dir is not None or include_review_candidates:
        if include_review_candidates:
            emit(tx("YOLO locale: {n_review_proposals} ipotesi sotto la soglia operativa "
                    "({n_rebuilt} ricomposte dalle cuciture fra tessere, {value} isolate): "
                    "compaiono nella revisione come «bassa confidenza», non tenute: "
                    "arrivano al modello solo se le tieni.",
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
