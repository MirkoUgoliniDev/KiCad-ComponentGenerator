"""@brief Classify SMD components locally and match them to installed KiCad models.

@details The ``models/smd/smd_classifier.pt`` checkpoint, or its ONNX export
``models/smd/smd_classifier.onnx`` (the file the Windows installer ships,
made by ``tools/export_smd_onnx.py``), predicts visual component class and
orientation from detector boxes. With photo calibration, the results are
passed to KiCadGen's existing STEP and footprint matcher. Only the weights
file is needed at runtime; the training project, YAML catalog, photos and
dataset are not loaded. The classifier has its own model role and is
excluded from the local YOLO detector picker.

@note PyTorch and torchvision are optional and imported only for a ``.pt``
file (loaded with ``weights_only=True``); an ``.onnx`` file needs
onnxruntime only. Both formats go through the same crops, the same
preprocessing (PIL letterbox, then NumPy normalisation) and the same
four-view vote, so they give the same classes: the ONNX graph receives the
normalised tensor and returns the two heads' logits. The checkpoint's
metadata travel in the ONNX file under the ``smd_checkpoint`` key as JSON
(see ONNX_METADATA_KEY and CHECKPOINT_METADATA_KEYS).
@note Preprocessing contract (review 2026-10-27, F8): the crops are prepared
    exactly as the EXTERNAL training script prepared them -- letterbox to
    `_IMG_SIZE` with bicubic resampling, grey padding `_PAD`, ImageNet
    normalisation `_MEAN`/`_STD` (see `PREPROCESSING`). A checkpoint that
    declares its own recipe under the `preprocessing` key is checked against
    it and refused on a mismatch; a checkpoint without that key is accepted
    on the assumption that it was trained with this very recipe, which only
    the training project can guarantee.
"""
from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from numbers import Real
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

from PIL import Image, ImageOps

from ..app_paths import smd_model_path
from ..cad3d.measurement_uncertainty import EDGE_UNCERTAINTY_PX, elongation_hinge
from .messages import tx

_FORMAT = "smdmap-classifier-2"
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)
_PAD = (114, 114, 114)
_IMG_SIZE = 224
_ANGLES = (0, 90, 180, 270)
_MAX_CHECKPOINT_BYTES = 256 * 1024 * 1024

#: ONNX metadata key holding the checkpoint's non-tensor fields as JSON.
ONNX_METADATA_KEY = "smd_checkpoint"
#: Checkpoint keys copied into the ONNX metadata: what validation, inference
#: and the model window read. `args` is NOT among them: it records the
#: training command with the maintainer's local output path, and `state_dict`
#: is the graph itself.
CHECKPOINT_METADATA_KEYS = ("format", "arch", "img_size", "classes", "orientations",
                            "suggested_threshold", "preprocessing", "trained", "epoch",
                            "catalog_sha256", "metrics")
#: Names of the ONNX graph's single input and two outputs (export contract).
ONNX_INPUT, ONNX_OUTPUTS = "images", ("class_logits", "rotation_logits")

#: The preprocessing recipe this module applies to every crop, in the form a
#: checkpoint may declare under its `preprocessing` key. Values are plain
#: lists/strings so that a JSON or torch-saved dict compares equal.
PREPROCESSING = {
    "resize": "letterbox",
    "interpolation": "bicubic",
    "pad_rgb": list(_PAD),
    "mean": list(_MEAN),
    "std": list(_STD),
    "img_size": _IMG_SIZE,
}
#: Tolerance when comparing declared mean/std with the module's: the values
#: are the ImageNet statistics written with three decimals; a checkpoint that
#: stored them as float32 differs by rounding, not by recipe.
_RECIPE_TOLERANCE = 1e-4


class _Letterbox:
    """Keep the resize, rounding and padding used for the trained crops."""

    def __init__(self, size=_IMG_SIZE):
        """@brief Letterbox to a square canvas of `size` pixels (PREPROCESSING["img_size"]).

        @param size Side of the square network input, in pixels.
        """
        self.size = size

    def __call__(self, image):
        scale = self.size / max(image.size)
        width = max(1, round(image.width * scale))
        height = max(1, round(image.height * scale))
        resized = image.resize((width, height), Image.Resampling.BICUBIC)
        canvas = Image.new("RGB", (self.size, self.size), _PAD)
        canvas.paste(resized, ((self.size - width) // 2, (self.size - height) // 2))
        return canvas


def _runtime():
    """Load optional Torch dependencies and reconstruct the v3 network."""
    try:
        import torch
        from torch import nn
        from torchvision import models, transforms
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            tx("Per usare smd_classifier.pt installa il runtime: {command}",
               command="python -m pip install -r requirements/smd.txt")
        ) from exc

    class PackageNet(nn.Module):
        def __init__(self, class_count):
            super().__init__()
            self.backbone = models.resnet18(weights=None)
            features = self.backbone.fc.in_features
            self.backbone.fc = nn.Identity()
            self.dropout = nn.Dropout(0.2)
            self.class_head = nn.Linear(features, class_count)
            self.rotation_head = nn.Linear(features, 4)

        def forward(self, images):
            features = self.dropout(self.backbone(images))
            return self.class_head(features), self.rotation_head(features)

    return torch, transforms, PackageNet


def _onnx_runtime():
    """Load onnxruntime, the only runtime an .onnx network needs."""
    try:
        import onnxruntime
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            tx("Per usare la rete SMD in formato ONNX installa il runtime: {command}",
               command="python -m pip install -r requirements/yolo.txt")
        ) from exc
    return onnxruntime


def _is_onnx(path) -> bool:
    """True for a weights file read with onnxruntime, by its extension."""
    return Path(path).suffix.lower() == ".onnx"


def _preprocess(views, np):
    """Letterboxed PIL crops -> normalised float32 batch [N, 3, S, S].

    The same operations, in the same order, as torchvision's ToTensor
    (uint8 / 255) followed by Normalize ((x - mean) / std) in float32, so
    that the batch is bit-identical to the one the training script built;
    tests compare it with torchvision's output.
    """
    letterbox = _Letterbox()
    mean = np.asarray(_MEAN, np.float32).reshape(3, 1, 1)
    std = np.asarray(_STD, np.float32).reshape(3, 1, 1)
    batch = []
    for view in views:
        pixels = np.asarray(letterbox(view), np.uint8).transpose(2, 0, 1)
        scaled = pixels.astype(np.float32) / np.float32(255)
        batch.append((scaled - mean) / std)
    return np.ascontiguousarray(np.stack(batch), dtype=np.float32)


def _softmax(logits, np):
    """Row-wise softmax of a float32 [N, K] array, computed in float64 like torch's float32 within rounding."""
    shifted = logits.astype(np.float64) - logits.max(axis=1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=1, keepdims=True)


def _open_network(path, device="auto"):
    """Validate a weights file and return (metadata, network).

    `network(batch)` takes a normalised float32 [N, 3, S, S] NumPy array and
    returns the two heads' logits as float32 NumPy arrays [N, classes] and
    [N, 4]: a PackageNet on `device` for a .pt, an onnxruntime session (CPU)
    for an .onnx. Only this object differs between the formats.
    """
    import numpy as np
    if _is_onnx(path):
        session, metadata = _load_onnx(path)
        input_name = session.get_inputs()[0].name

        def run_onnx(batch):
            class_logits, rotation_logits = session.run(None, {input_name: batch})
            return (np.asarray(class_logits, np.float32), np.asarray(rotation_logits, np.float32))
        return metadata, run_onnx
    torch, _, PackageNet = _runtime()
    checkpoint = _load_checkpoint(path, torch)
    _validate_checkpoint(checkpoint, path, torch, PackageNet)
    net = PackageNet(len(checkpoint["classes"]))
    net.load_state_dict(checkpoint["state_dict"])
    selected_device = ("cuda" if torch.cuda.is_available() else "cpu") if device == "auto" else device
    net.to(selected_device).eval()

    def run_torch(batch):
        with torch.inference_mode():
            class_logits, rotation_logits = net(torch.from_numpy(batch).to(selected_device))
        return (class_logits.detach().cpu().numpy().astype(np.float32),
                rotation_logits.detach().cpu().numpy().astype(np.float32))
    return checkpoint, run_torch


def _crop(image, box, margin):
    x1, y1, x2, y2 = box
    dx, dy = (x2 - x1) * margin, (y2 - y1) * margin
    left, top = max(0, round(x1 - dx)), max(0, round(y1 - dy))
    right, bottom = min(image.width, round(x2 + dx)), min(image.height, round(y2 + dy))
    if right <= left or bottom <= top:
        raise ValueError(tx("Riquadro vuoto: {box}", box=list(box)))
    return image.crop((left, top, right, bottom))


def validate_smd_setup(model_path=None):
    """@brief Validate the optional local SMD classifier checkpoint.

    @param model_path Optional weights path (.pt or .onnx); defaults to the
        SMD classifier resource (app_paths.smd_model_path).
    @return The validated weights path.
    @throws FileNotFoundError When the file is missing.
    @throws RuntimeError When the runtime of the format (PyTorch/torchvision
        for .pt, onnxruntime for .onnx) is unavailable.
    @throws ValueError When the metadata or the network are incompatible.
    @note Call before paid extraction so a missing or incompatible local model
        fails before any remote work begins.
    """
    path = Path(model_path) if model_path is not None else smd_model_path()
    if not path.is_file():
        raise FileNotFoundError(tx("Rete SMD non trovata: {path}", path=path))
    if _is_onnx(path):
        _load_onnx(path)
        return path
    torch, _, PackageNet = _runtime()
    checkpoint = _load_checkpoint(path, torch)
    _validate_checkpoint(checkpoint, path, torch, PackageNet)
    return path


def _load_onnx(path):
    """Open an .onnx network, validate its metadata and its graph; return (session, metadata).

    The metadata are the checkpoint fields (CHECKPOINT_METADATA_KEYS) as
    JSON under ONNX_METADATA_KEY. The graph must take one float32 input
    [N, 3, img_size, img_size] (N fixed or symbolic) and return exactly two
    float32 outputs [N, classes] and [N, 4], in this order: the class head
    and the rotation head of PackageNet.
    """
    size = Path(path).stat().st_size
    if size <= 0 or size > _MAX_CHECKPOINT_BYTES:
        raise ValueError(tx("Dimensione checkpoint SMD non valida: {path}", path=path))
    onnxruntime = _onnx_runtime()
    try:
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 4
        session = onnxruntime.InferenceSession(str(path), sess_options=options,
                                               providers=["CPUExecutionProvider"])
    except Exception as exc:
        raise ValueError(tx("Checkpoint non compatibile con la rete SMD v3 ResNet18/224: {path}",
                            path=path)) from exc
    raw = dict(session.get_modelmeta().custom_metadata_map).get(ONNX_METADATA_KEY)
    try:
        metadata = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        metadata = None
    if not isinstance(metadata, Mapping):
        raise ValueError(tx("Checkpoint non compatibile con la rete SMD v3 ResNet18/224: {path}",
                            path=path))
    _validate_metadata(metadata, path)
    inputs, outputs = session.get_inputs(), session.get_outputs()

    def batch_shape(shape, last):
        # The batch axis may be symbolic (dynamic export) or a fixed count.
        return (len(shape) == len(last) + 1 and (not isinstance(shape[0], int) or shape[0] >= 1)
                and list(shape[1:]) == list(last))
    if (len(inputs) != 1 or inputs[0].type != "tensor(float)"
            or not batch_shape(list(inputs[0].shape), [3, _IMG_SIZE, _IMG_SIZE])):
        raise ValueError(tx("Checkpoint SMD non valido: {path} ({reason})",
                            path=path, reason="ONNX input"))
    expected = ([len(metadata["classes"])], [4])
    if (len(outputs) != 2 or any(output.type != "tensor(float)" for output in outputs)
            or not all(batch_shape(list(output.shape), last)
                       for output, last in zip(outputs, expected))):
        raise ValueError(tx("Checkpoint SMD non valido: {path} ({reason})",
                            path=path, reason="ONNX outputs"))
    return session, metadata


#: Below this many validation samples the declared accuracy says little about
#: other boards: the model window says so next to the figure. Interface
#: threshold for an honest note, not a property of any checkpoint.
FEW_VALIDATION_SAMPLES = 200


def describe_smd_checkpoint(model_path=None) -> dict:
    """@brief Validate a checkpoint of the SMD network and return what it declares about itself.

    @param model_path Checkpoint path; None for the default in models/smd.
    @return Dictionary: path, format, arch, img_size, trained (as stored),
        epochs, classes (list), orientations (class -> none/axis/full),
        threshold (the effective acceptance threshold, max(0.5, suggested)),
        catalog_sha256, and validation (samples, accuracy, balanced_accuracy)
        when the checkpoint carries metrics, else None.
    @throws The same exceptions as validate_smd_setup.
    @note Loads with ``weights_only=True`` (_load_checkpoint) or reads the
        ONNX metadata: no code from the file runs, so the model window may
        call this on its own.
    """
    path = validate_smd_setup(model_path)
    if _is_onnx(path):
        checkpoint = _load_onnx(path)[1]
    else:
        torch, _, _ = _runtime()
        checkpoint = _load_checkpoint(path, torch)
    metrics = checkpoint.get("metrics") if isinstance(checkpoint.get("metrics"), Mapping) else {}
    val = metrics.get("val") if isinstance(metrics.get("val"), Mapping) else None
    validation = None
    if val is not None:
        validation = {key: val.get(key) for key in ("samples", "accuracy", "balanced_accuracy")}
    classes = [name for name in checkpoint["classes"]]
    return {
        "path": str(path), "format": str(checkpoint.get("format") or ""),
        "arch": str(checkpoint.get("arch") or ""), "img_size": checkpoint.get("img_size"),
        "trained": str(checkpoint.get("trained") or ""), "epochs": checkpoint.get("epoch"),
        "classes": classes, "orientations": dict(checkpoint["orientations"]),
        "threshold": max(0.5, float(checkpoint.get("suggested_threshold") or 0)),
        "catalog_sha256": str(checkpoint.get("catalog_sha256") or ""),
        "validation": validation,
    }


def _load_checkpoint(path, torch):
    """Load a bounded, weights-only checkpoint from a user-controlled path."""
    size = Path(path).stat().st_size
    if size <= 0 or size > _MAX_CHECKPOINT_BYTES:
        raise ValueError(tx("Dimensione checkpoint SMD non valida: {path}", path=path))
    return torch.load(path, map_location="cpu", weights_only=True)


def _validate_checkpoint(checkpoint, path, torch, PackageNet):
    """Check every field inference consumes, including exact model tensor shapes.

    Constructing the expected architecture on the meta device checks the
    state-dict schema without allocating a second ResNet's weights during the
    preflight. Inference still loads into its real model with strict matching.
    """
    _validate_metadata(checkpoint, path)
    classes = checkpoint["classes"]
    state = checkpoint.get("state_dict")
    if not isinstance(state, Mapping):
        raise ValueError(tx("Checkpoint SMD non valido: {path} ({reason})",
                            path=path, reason="state_dict"))
    try:
        with torch.device("meta"):
            expected = PackageNet(len(classes)).state_dict()
    except (AttributeError, RuntimeError, TypeError) as exc:
        raise ValueError(tx("Checkpoint SMD non valido: {path} ({reason})",
                            path=path, reason="architecture")) from exc
    if set(state) != set(expected):
        raise ValueError(tx("Checkpoint SMD non valido: {path} ({reason})",
                            path=path, reason="state_dict keys"))
    for name, reference in expected.items():
        value = state[name]
        if (not torch.is_tensor(value) or value.device.type == "meta"
                or value.layout != torch.strided
                or tuple(value.shape) != tuple(reference.shape)
                or value.dtype != reference.dtype):
            raise ValueError(tx("Checkpoint SMD non valido: {path} ({reason})",
                                path=path, reason=f"state_dict {name}"))
        if (value.is_floating_point() or value.is_complex()) and not bool(
                torch.isfinite(value).all().item()):
            raise ValueError(tx("Checkpoint SMD non valido: {path} ({reason})",
                                path=path, reason=f"state_dict {name} non finito"))


def _validate_metadata(checkpoint, path):
    """Check the fields both formats share: format, architecture, recipe, classes, orientations, threshold.

    The tensors are checked apart: the state dict for a .pt
    (_validate_checkpoint), the graph's shapes for an .onnx (_load_onnx).
    """
    if not isinstance(checkpoint, Mapping):
        raise ValueError(tx("Checkpoint non compatibile con la rete SMD v3 ResNet18/224: {path}",
                            path=path))
    if (checkpoint.get("format") != _FORMAT or checkpoint.get("arch") != "resnet18"
            or checkpoint.get("img_size") != _IMG_SIZE):
        raise ValueError(tx("Checkpoint non compatibile con la rete SMD v3 ResNet18/224: {path}",
                            path=path))
    validate_preprocessing(checkpoint, path)
    classes = checkpoint.get("classes")
    orientations = checkpoint.get("orientations")
    threshold = checkpoint.get("suggested_threshold", 0.0)
    if (not isinstance(classes, list) or not classes or len(classes) > 512
            or any(not isinstance(label, str) or not label.strip() for label in classes)
            or len(set(classes)) != len(classes)):
        raise ValueError(tx("Checkpoint SMD non valido: {path} ({reason})",
                            path=path, reason="classes"))
    if (not isinstance(orientations, Mapping) or set(orientations) != set(classes)
            or any(value not in ("none", "axis", "full")
                   for value in orientations.values())):
        raise ValueError(tx("Checkpoint SMD non valido: {path} ({reason})",
                            path=path, reason="orientations"))
    if (isinstance(threshold, bool) or not isinstance(threshold, Real)
            or not math.isfinite(float(threshold)) or not 0 <= float(threshold) <= 1):
        raise ValueError(tx("Checkpoint SMD non valido: {path} ({reason})",
                            path=path, reason="suggested_threshold"))


def validate_preprocessing(checkpoint, path):
    """@brief Refuse a checkpoint whose declared preprocessing differs from this module's.

    @param checkpoint Mapping loaded from the checkpoint file.
    @param path Checkpoint path, for the error message only.
    @throws ValueError When `preprocessing` is present but is not a mapping,
        names a key this module does not implement, or gives a different
        value for a key it shares with PREPROCESSING (numbers compared
        within _RECIPE_TOLERANCE).
    @note A checkpoint WITHOUT the key is accepted: see the module docstring
        for the contract with the external training script.
    """
    declared = checkpoint.get("preprocessing")
    if declared is None:
        return

    def mismatch(reason):
        return ValueError(tx("Checkpoint SMD non valido: {path} ({reason})",
                             path=path, reason=f"preprocessing {reason}"))
    if not isinstance(declared, Mapping):
        raise mismatch("non e' un dizionario")
    for key, value in declared.items():
        if key not in PREPROCESSING:
            # A step we do not perform (another crop, a colour transform):
            # the crops the network will see are not the ones it learnt on.
            raise mismatch(f"{key} sconosciuto")
        expected = PREPROCESSING[key]
        if not _same_recipe_value(value, expected):
            raise mismatch(f"{key}: {value!r} invece di {expected!r}")


def _same_recipe_value(value, expected):
    """Compare one declared recipe value with the module's, tolerating float rounding."""
    if isinstance(expected, str):
        return isinstance(value, str) and value.strip().lower() == expected
    if isinstance(expected, list):
        try:
            values = [float(v) for v in value]
        except (TypeError, ValueError):
            return False
        return (len(values) == len(expected) and all(
            math.isfinite(v) and abs(v - float(e)) <= _RECIPE_TOLERANCE
            for v, e in zip(values, expected)))
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    return math.isfinite(float(value)) and abs(float(value) - float(expected)) <= _RECIPE_TOLERANCE


#: `rotation_basis` values: the axis was decided by the elongation of the box
#: beyond its uncertainty, or by the network's vote.
ROTATION_BASIS_BOX = "box_aspect"
ROTATION_BASIS_NETWORK = "network"


def _rotation(orientation, probs, box, edge_px=EDGE_UNCERTAINTY_PX):
    """Axis or pose of one box: (angle, confidence, basis).

    For an `axis` class the box's shape overrides the network's axis vote
    ONLY when its elongation exceeds `elongation_hinge` of the short side:
    below that, the difference between the sides is within what two edges
    each uncertain by `edge_px` can produce, and the shape says nothing
    (review 2026-10-27, F3; replaces a fixed 1.25 that one pixel could flip
    on a 1210). The confidence stays the one the network gives that axis
    (audit 2026-10-02: an imposed 0.99 showed in review a certainty the
    network did not have).
    """
    if orientation == "none":
        return None, None, None
    if orientation == "axis":
        horizontal, vertical = probs[0] + probs[2], probs[1] + probs[3]
        width, height = box[2] - box[0], box[3] - box[1]
        hinge = elongation_hinge(min(width, height), edge_px)
        if width > hinge * height:
            return 0, horizontal, ROTATION_BASIS_BOX
        if height > hinge * width:
            return 90, vertical, ROTATION_BASIS_BOX
        return ((0, horizontal, ROTATION_BASIS_NETWORK) if horizontal >= vertical
                else (90, vertical, ROTATION_BASIS_NETWORK))
    if orientation != "full":
        raise ValueError(tx("Orientamento non valido nel checkpoint: {orientation}",
                            orientation=repr(orientation)))
    index = max(range(4), key=probs.__getitem__)
    return _ANGLES[index], probs[index], ROTATION_BASIS_NETWORK


#: Classes of the checkpoint whose body is a rectangular chip with two
#: metallised end caps. A declared property of the classifier's own fixed
#: vocabulary (the checkpoint's class list), not a rule on part names: it lets
#: the KiCad matcher keep a "chip" body inside the standard chip catalogue
#: instead of a cylindrical MELF, a shunt or a tantalum case of similar size.
ENDCAP_CHIP_CLASSES = frozenset({"chip_R", "chip_C", "chip_LED"})


#: Terminal count each checkpoint class declares by its own definition (the
#: package name of the class carries it, as in KiCad's naming): a declared
#: property of the classifier's fixed vocabulary, used by the KiCad matcher as
#: a constraint. Classes whose count the label does not fix (QFN, DFN, MSOP,
#: LQFP, SOT-223 with its tab, arrays, connectors, modules...) declare none,
#: and the matcher then does not take the classifier's exit for them.
CLASS_TERMINAL_COUNTS = {
    "chip_R": 2, "chip_C": 2, "chip_LED": 2,
    "SOD-123": 2, "SOD-123F": 2, "SOD-323": 2, "MiniMELF": 2, "SMA": 2,
    "SOT-23": 3, "SOT-23-5": 5, "SOT-23-6": 6,
    "SOIC-8": 8, "SOIC-14": 14, "SOIC-16": 16,
}


#: Row fields through which the network's identity and pose reach the KiCad
#: matcher and the 3D builder. They describe what the network SAW: a person's
#: edit of the body (kind, package, size, position, rotation, pasted format)
#: makes them stale, and drop_classifier_identity removes them.
SMD_IDENTITY_KEYS = ("_smd_accepted", "_smd_body", "_smd_rotation_deg",
                     "_smd_rotation_kind", "_smd_views_agree", "_smd_terminal_count")


def drop_classifier_identity(row):
    """@brief Withdraw the network's identity and pose from a row a person edited.

    @param row Component mapping (modified in place); anything else is ignored.
    @return True when the row carried a network identity that is now withdrawn.
    @note `_smd_prediction` and `_smd_class` stay as history, with
        `_smd_superseded` set, so the review can say the reading no longer
        applies; the matcher and the builder only read SMD_IDENTITY_KEYS.
    """
    if not isinstance(row, dict):
        return False
    carried = any(key in row for key in SMD_IDENTITY_KEYS)
    for key in SMD_IDENTITY_KEYS:
        row.pop(key, None)
    if carried:
        row["_smd_superseded"] = True
    return carried


def _body_topology(label):
    """Body topology declared for a checkpoint class, or None when not declared."""
    return "endcap_chip" if label in ENDCAP_CHIP_CLASSES else None


def _kind_package(label):
    """Translate learned visual labels to the application's existing families."""
    known = {
        "chip_R": ("resistor", "SMD chip resistor"),
        "R_Array": ("resistor", "SMD resistor array"),
        "chip_C": ("capacitor", "SMD chip capacitor"),
        "CP_Elec": ("capacitor", "SMD electrolytic capacitor"),
        "CP_Tantalum": ("capacitor", "SMD tantalum capacitor"),
        "chip_LED": ("led", "SMD chip LED"),
        "LED_RGB": ("led", "SMD RGB LED"),
        "crystal": ("crystal", "SMD crystal"),
        # 'fuse' is not a type of the program (COMPONENT_KINDS): generic,
        # as the normalisation would make it anyway.
        "fuse": ("generic", "SMD fuse"),
        "inductor": ("inductor", "SMD inductor"),
        "module": ("module", "module"),
        "switch": ("switch", "SMD tactile switch"),
        "USB_C": ("connector", "USB-C receptacle"),
        "USB_micro": ("connector", "SMD micro USB receptacle"),
        "SOT-23": ("ic", "SOT-23 / SC-70"),
        "SOT-23-5": ("ic", "SOT-23-5 / SC-70-5"),
        "SOT-23-6": ("ic", "SOT-23-6 / SC-70-6"),
    }
    if label in known:
        return known[label]
    if label.startswith("SOD-") or label in ("SMA", "SMB", "MiniMELF"):
        return "diode", label
    if label.startswith(("SOT-", "SOIC-")) or label in ("QFN", "DFN", "MSOP", "TSSOP", "LQFP"):
        return "ic", label
    return "generic", ""


def _checked_measurements(measured_mm, count):
    """One validated measurement dict (or None) per box, in the boxes' order.

    @throws ValueError When the sequence length differs from the box count
        or an entry is neither None nor a dict of finite positive sides with
        a finite rotation.
    """
    if measured_mm is None:
        return [None] * count
    rows = list(measured_mm)
    if len(rows) != count:
        raise ValueError(tx("Riquadro non valido: {box}", box="measured_mm"))
    checked = []
    for entry in rows:
        if entry is None:
            checked.append(None)
            continue
        if not isinstance(entry, Mapping):
            raise ValueError(tx("Riquadro non valido: {box}", box=entry))
        try:
            length = float(entry["length_mm"])
            width = float(entry["width_mm"])
            rotation = float(entry.get("rotation_deg") or 0.0)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(tx("Riquadro non valido: {box}", box=entry)) from exc
        if not all(math.isfinite(v) for v in (length, width, rotation)) or min(length, width) <= 0:
            raise ValueError(tx("Riquadro non valido: {box}", box=entry))
        checked.append({"length_mm": length, "width_mm": width, "rotation_deg": rotation})
    return checked


def _attach_kicad_results(rows, library, log, cancel):
    """Use existing CAD measurements and actual footprint-to-STEP links."""
    pending = [row for row in rows if row["accepted"] and row["size_mm"] is not None]
    if not pending:
        return
    from ..cad3d import kicad_models

    library = Path(library) if library is not None else None
    paths_by_root = {}
    for row in pending:
        length, width = sorted(row["size_mm"], reverse=True)
        measured = row.get("measured_mm")
        # A body measured on its own edges (photo_refine) carries its true
        # sides and its angle: the matcher gets those, not the axis-aligned
        # box that encloses an oblique body (a 5 x 5 QFN at 45 degrees
        # encloses 7.1 x 7.1; review 2026-10-27, F5). The network's pose is
        # a verse on top of the measured angle, it does not replace it.
        rotation = measured["rotation_deg"] if measured else row["rotation_deg"]
        component = {
            "kind": row["kind"], "class": row["kind"],
            "package": row["package"], "length_mm": length, "width_mm": width,
            "rotation_deg": rotation,
            "bbox_basis": "measured_edges" if measured else "unknown",
            "_source": "SMD local", "_smd_class": row["class"],
            "_smd_confidence": row["class_confidence"], "_smd_body": row.get("body"),
        }
        report = kicad_models.model_candidates(component, library=library, log=log, cancel=cancel)
        row["model_match"] = report
        row["model_status"] = report["status"]
        row["step_candidates"] = [dict(item) for item in report.get("candidates", [])]
        selected = report.get("selected_path")
        if selected:
            row["step_model"] = {"path": selected, "file": selected,
                                 "exists": Path(selected).is_file()}
        paths = [item["path"] for item in report.get("candidates", [])]
        if selected:
            paths.append(selected)
        for value in paths:
            path = Path(value)
            root = path.parent.parent if path.parent.name.endswith(".3dshapes") else library
            if root is not None:
                paths_by_root.setdefault(root, set()).add(value)

    # Read links once per library, even when many boxes use the same model.
    links = {}
    for root, paths in paths_by_root.items():
        if cancel is not None and cancel.is_set():
            raise kicad_models.MatchingCancelled(measured={})
        links.update(kicad_models.footprint_sizes(sorted(paths), library=root, log=log))
    for row in pending:
        report = row["model_match"]
        selected = report.get("selected_path")
        model_paths = list(dict.fromkeys(
            [item["path"] for item in report.get("candidates", [])] + ([selected] if selected else [])))
        footprints = []
        for model_path in model_paths:
            for reading in links.get(model_path, []):
                path = Path(reading["source"])
                reference = f"{path.parent.name.removesuffix('.pretty')}:{path.stem}"
                footprints.append({"footprint": reference, "file": str(path),
                                   "step_file": model_path, "pad_size_mm": reading["size"]})
        row["footprint_candidates"] = footprints
        selected_footprints = [item for item in footprints if item["step_file"] == selected]
        if selected and len(selected_footprints) == 1:
            row["footprint"] = selected_footprints[0]["footprint"]
            row["footprint_status"] = "match"
        elif footprints:
            row["footprint_status"] = "ambiguous"
        else:
            row["footprint_status"] = "no_footprint" if selected else "unresolved"


def classify_photo_boxes(
    photo: str | os.PathLike | Image.Image,
    boxes: Sequence[Sequence[float]],
    *,
    model_path: str | os.PathLike | None = None,
    mm_per_pixel: tuple[float, float] | None = None,
    model_root: str | os.PathLike | None = None,
    box_format: str = "pixels",
    box_frame_px: Sequence[float] | None = None,
    device: str = "auto",
    margin: float = 0.10,
    batch_size: int = 32,
    log=None,
    cancel=None,
    phase=None,
    measured_mm: Sequence[dict | None] | None = None,
) -> list[dict]:
    """@brief Classify photo boxes and optionally match them to KiCad footprints and STEP models.

    @param photo Image path, PIL RGB image or OpenCV uint8 BGR array.
    @param boxes Bounding boxes as ``[x1, y1, x2, y2]`` in pixels, or 0..1
        coordinates when ``box_format='normalized'``.
    @param measured_mm Optional, one entry per box: a dict with `length_mm`,
        `width_mm` and `rotation_deg` when that body's edges were measured
        on the photo, else None. A measured body is matched on its own sides
        and angle (`size_mm` = [length, width], `size_basis` =
        "measured_edges"); an unmeasured one on the axis-aligned box
        (`size_basis` = "detector_box").
    @param model_path Optional SMD checkpoint; defaults to models/smd/smd_classifier.pt.
    @param mm_per_pixel Optional positive ``(scale_x, scale_y)`` calibration.
    @param model_root Optional KiCad library root used by the existing matcher.
    @param box_format Coordinate convention: ``pixels`` or ``normalized``.
    @param box_frame_px Optional full-photo frame for boxes local to a board ROI.
    @param device Torch device or ``auto``.
    @param margin Crop margin as a fraction of each box dimension, from 0 to 0.5.
    @param batch_size Number of boxes processed together.
    @param log Optional matcher log callback.
    @param cancel Optional event or callable cancellation check.
    @param phase Optional callback receiving the current matching phase.
    @return One dictionary per box with classification, rotation, calibrated
        dimensions and, when possible, KiCad model/footprint candidates.
        `rotation_kind` is the checkpoint's orientation kind of the class
        (none/axis/full: only "full" is a KiCad pose with a verse),
        `rotation_views_agree` says whether the four rotated views chose the
        same pose, `terminal_count` is the count the class declares
        (CLASS_TERMINAL_COUNTS) or None. `accepted` means a class other than
        `_other` at or above max(0.5, the checkpoint's suggested threshold).
    @throws ValueError When inputs are invalid or the checkpoint is incompatible.
    @throws RuntimeError When dependencies are unavailable or cancellation is requested.
    @throws OSError When an image or checkpoint cannot be read.

    Photo may be a path, PIL RGB image or OpenCV uint8 BGR array. Boxes are
    [x1, y1, x2, y2] pixels; box_format='normalized' accepts 0..1 coordinates.
    box_frame_px defines the board ROI in the supplied full photo. With that
    frame, boxes are local to the ROI but crop margins can extend outside it.
    mm_per_pixel=(scale_x, scale_y) supplies the existing photo calibration.
    Without that scale, the result includes classification and rotation only.
    Matching uses KiCadGen's own library/measurement machinery, retaining its
    unresolved and review states. No TorchObjectDetection catalog is loaded.
    """
    if cancel is not None and not hasattr(cancel, 'is_set'):
        if not callable(cancel):
            raise ValueError(tx("cancel deve essere un evento o una funzione"))
        cancel = SimpleNamespace(is_set=cancel)
    if not math.isfinite(margin) or not 0 <= margin <= 0.5:
        raise ValueError(tx("margin deve essere tra 0 e 0,5"))
    if mm_per_pixel is not None and (len(mm_per_pixel) != 2 or any(
            not math.isfinite(float(v)) or float(v) <= 0 for v in mm_per_pixel)):
        raise ValueError(tx("mm_per_pixel deve contenere due scale positive (X, Y)"))
    measured_rows = _checked_measurements(measured_mm, len(boxes))
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError(tx("batch_size deve essere un intero positivo"))
    if box_format not in ("pixels", "normalized"):
        raise ValueError(tx("box_format deve essere 'pixels' oppure 'normalized'"))
    if isinstance(photo, Image.Image):
        image = ImageOps.exif_transpose(photo).convert("RGB")
    elif hasattr(photo, "shape"):
        import numpy as np
        array = np.asarray(photo)
        if (array.ndim != 3 or array.shape[2] != 3 or array.dtype != np.uint8
                or min(array.shape[:2]) <= 0):
            raise ValueError(tx("photo deve essere un array uint8 BGR, H x W x 3, non vuoto"))
        image = Image.fromarray(array[:, :, ::-1].copy())
    else:
        with Image.open(photo) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
    frame = (0.0, 0.0, float(image.width), float(image.height))
    if box_frame_px is not None:
        if len(box_frame_px) != 4 or any(not math.isfinite(float(v)) for v in box_frame_px):
            raise ValueError(tx("box_frame_px deve contenere quattro coordinate finite"))
        frame = tuple(map(float, box_frame_px))
        if (frame[0] < 0 or frame[1] < 0 or frame[2] > image.width
                or frame[3] > image.height or frame[2] <= frame[0] or frame[3] <= frame[1]):
            raise ValueError(tx("box_frame_px fuori dai limiti della foto"))
    frame_width, frame_height = frame[2] - frame[0], frame[3] - frame[1]
    clean_boxes, crops = [], []
    for box in boxes:
        if len(box) != 4 or any(not math.isfinite(float(v)) for v in box):
            raise ValueError(tx("Riquadro non valido: {box}", box=repr(box)))
        x1, y1, x2, y2 = map(float, box)
        if box_format == "normalized":
            x1, x2 = x1 * frame_width, x2 * frame_width
            y1, y2 = y1 * frame_height, y2 * frame_height
        if x1 < 0 or y1 < 0 or x2 > frame_width or y2 > frame_height or x2 <= x1 or y2 <= y1:
            raise ValueError(tx("Riquadro fuori dai limiti della foto: {box}", box=list(box)))
        box = (x1 + frame[0], y1 + frame[1], x2 + frame[0], y2 + frame[1])
        clean_boxes.append(box)
        crops.append(_crop(image, box, margin))
    if not crops:
        return []

    import numpy as np
    path = Path(model_path) if model_path is not None else smd_model_path()
    checkpoint, network = _open_network(path, device)
    classes, orientations = list(checkpoint["classes"]), checkpoint["orientations"]
    threshold = max(0.5, float(checkpoint.get("suggested_threshold") or 0))
    results = []
    for start in range(0, len(crops), batch_size):
        if cancel is not None and cancel.is_set():
            raise RuntimeError(tx("Classificazione SMD annullata"))
        chunk = crops[start:start + batch_size]
        class_sum = rotation_sum = None
        # Pose chosen by each of the four rotated views, brought back to
        # the initial orientation: if they do not agree, the mean hides a
        # coin toss (audit 2026-10-02: SOD-123F at 0.54/0.46).
        view_poses = []
        for turn in range(4):
            views = [crop.rotate(90 * turn, expand=True) if turn else crop for crop in chunk]
            class_logits, rotation_logits = network(_preprocess(views, np))
            probabilities = _softmax(class_logits, np)
            turns = np.roll(_softmax(rotation_logits, np), -turn, axis=1)
            class_sum = probabilities if class_sum is None else class_sum + probabilities
            rotation_sum = turns if rotation_sum is None else rotation_sum + turns
            view_poses.append(turns.argmax(axis=1).tolist())
        class_probs, rotation_probs = (class_sum / 4).tolist(), (rotation_sum / 4).tolist()
        for offset, (probabilities, turns) in enumerate(zip(class_probs, rotation_probs)):
            ranked = sorted(zip(classes, probabilities), key=lambda item: item[1], reverse=True)
            label, confidence = ranked[0]
            accepted = label != "_other" and confidence >= threshold
            box = clean_boxes[start + offset]
            rotation_kind = orientations.get(label, "none") if accepted else "none"
            rotation, rotation_confidence, rotation_basis = _rotation(rotation_kind, turns, box)
            views_agree = len({poses[offset] for poses in view_poses}) == 1
            measured = measured_rows[start + offset]
            if measured is not None:
                size = [measured["length_mm"], measured["width_mm"]]
                size_basis = "measured_edges"
            else:
                size = ([round((box[2] - box[0]) * float(mm_per_pixel[0]), 3),
                         round((box[3] - box[1]) * float(mm_per_pixel[1]), 3)]
                        if mm_per_pixel is not None else None)
                size_basis = "detector_box"
            kind, package = _kind_package(label) if accepted else ("generic", "")
            status = ("needs_scale" if size is None else "unresolved") if accepted else (
                "unknown" if label == "_other" else "low_confidence")
            results.append({
                "box_px": list(box), "class": label, "kind": kind, "package": package,
                "body": _body_topology(label) if accepted else None,
                "class_confidence": confidence, "accepted": accepted, "threshold": threshold,
                "rotation_deg": rotation, "rotation_confidence": rotation_confidence,
                "rotation_basis": rotation_basis,
                "rotation_kind": rotation_kind, "rotation_views_agree": views_agree,
                "size_basis": size_basis, "measured_mm": measured,
                "terminal_count": CLASS_TERMINAL_COUNTS.get(label) if accepted else None,
                "size_mm": size, "footprint": None, "footprint_status": status,
                "footprint_candidates": [], "step_model": None, "step_candidates": [],
                "model_match": None, "model_status": status,
                "top_classes": [{"class": name, "confidence": probability}
                                for name, probability in ranked[:3]],
            })
    if phase is not None and any(row['accepted'] and row['size_mm'] for row in results):
        phase('kicad', tx('abbinamento dei componenti SMD alla libreria KiCad'))
    _attach_kicad_results(results, model_root, log, cancel)
    return results
