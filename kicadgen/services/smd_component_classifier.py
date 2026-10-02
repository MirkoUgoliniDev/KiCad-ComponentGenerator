"""@brief Classify SMD components locally and match them to installed KiCad models.

@details The ``models/smd/smd_classifier.pt`` checkpoint (local, not in the installer) predicts visual
component class and orientation from detector boxes. With photo calibration,
the results are passed to KiCadGen's existing STEP and footprint matcher.
Only the checkpoint is needed at runtime; the training project, YAML catalog,
photos and dataset are not loaded. The classifier checkpoint has its own model
role and is excluded from the local YOLO detector picker.

@note PyTorch and torchvision are optional and imported only when this service
is used. Checkpoints are loaded with ``weights_only=True``.
"""
from __future__ import annotations

import math
import os
from collections.abc import Mapping
from numbers import Real
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

from PIL import Image, ImageOps

from ..app_paths import smd_model_path
from .messages import tx

_FORMAT = "smdmap-classifier-2"
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)
_PAD = (114, 114, 114)
_ANGLES = (0, 90, 180, 270)
_MAX_CHECKPOINT_BYTES = 256 * 1024 * 1024


class _Letterbox:
    """Keep the resize, rounding and padding used for the trained crops."""

    def __init__(self, size=224):
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
               command="python -m pip install -r requirements-smd.txt")
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

    @param model_path Optional checkpoint path; defaults to the SMD
        classifier resource.
    @return The validated checkpoint path.
    @throws FileNotFoundError When the checkpoint is missing.
    @throws RuntimeError When PyTorch/torchvision are unavailable.
    @throws ValueError When the checkpoint metadata is incompatible.
    @note Call before paid extraction so a missing or incompatible local model
        fails before any remote work begins.
    """
    path = Path(model_path) if model_path is not None else smd_model_path()
    if not path.is_file():
        raise FileNotFoundError(tx("Rete SMD non trovata: {path}", path=path))
    torch, _, PackageNet = _runtime()
    checkpoint = _load_checkpoint(path, torch)
    _validate_checkpoint(checkpoint, path, torch, PackageNet)
    return path


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
    @note Loads with ``weights_only=True`` (_load_checkpoint): no code from
        the file runs, so the model window may call this on its own.
    """
    path = validate_smd_setup(model_path)
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
    if not isinstance(checkpoint, Mapping):
        raise ValueError(tx("Checkpoint non compatibile con la rete SMD v3 ResNet18/224: {path}",
                            path=path))
    if (checkpoint.get("format") != _FORMAT or checkpoint.get("arch") != "resnet18"
            or checkpoint.get("img_size") != 224):
        raise ValueError(tx("Checkpoint non compatibile con la rete SMD v3 ResNet18/224: {path}",
                            path=path))
    classes = checkpoint.get("classes")
    orientations = checkpoint.get("orientations")
    threshold = checkpoint.get("suggested_threshold", 0.0)
    state = checkpoint.get("state_dict")
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


def _rotation(orientation, probs, box):
    if orientation == "none":
        return None, None
    if orientation == "axis":
        horizontal, vertical = probs[0] + probs[2], probs[1] + probs[3]
        width, height = box[2] - box[0], box[3] - box[1]
        # L'asse lo decide la forma del riquadro; la confidenza resta quella
        # che la rete da' a quell'asse (audit 2026-10-02: un 0.99 imposto
        # mostrava in revisione una certezza che la rete non aveva).
        if width >= 1.25 * height:
            return 0, horizontal
        if height >= 1.25 * width:
            return 90, vertical
        return (0, horizontal) if horizontal >= vertical else (90, vertical)
    if orientation != "full":
        raise ValueError(tx("Orientamento non valido nel checkpoint: {orientation}",
                            orientation=repr(orientation)))
    index = max(range(4), key=probs.__getitem__)
    return _ANGLES[index], probs[index]


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
        # 'fuse' non e' un tipo del programma (COMPONENT_KINDS): generico,
        # come la normalizzazione lo renderebbe comunque.
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
        component = {
            "kind": row["kind"], "class": row["kind"],
            "package": row["package"], "length_mm": length, "width_mm": width,
            "rotation_deg": row["rotation_deg"], "bbox_basis": "unknown",
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
) -> list[dict]:
    """@brief Classify photo boxes and optionally match them to KiCad footprints and STEP models.

    @param photo Image path, PIL RGB image or OpenCV uint8 BGR array.
    @param boxes Bounding boxes as ``[x1, y1, x2, y2]`` in pixels, or 0..1
        coordinates when ``box_format='normalized'``.
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

    torch, transforms, PackageNet = _runtime()
    path = Path(model_path) if model_path is not None else smd_model_path()
    checkpoint = _load_checkpoint(path, torch)
    _validate_checkpoint(checkpoint, path, torch, PackageNet)
    classes, orientations = list(checkpoint["classes"]), checkpoint["orientations"]
    net = PackageNet(len(classes))
    net.load_state_dict(checkpoint["state_dict"])
    selected_device = ("cuda" if torch.cuda.is_available() else "cpu") if device == "auto" else device
    net.to(selected_device).eval()
    transform = transforms.Compose([
        _Letterbox(), transforms.ToTensor(), transforms.Normalize(_MEAN, _STD),
    ])
    threshold = max(0.5, float(checkpoint.get("suggested_threshold") or 0))
    results = []
    with torch.inference_mode():
        for start in range(0, len(crops), batch_size):
            if cancel is not None and cancel.is_set():
                raise RuntimeError(tx("Classificazione SMD annullata"))
            chunk = crops[start:start + batch_size]
            class_sum = rotation_sum = None
            # Posa scelta da ciascuna delle quattro viste ruotate, riportata
            # all'orientamento iniziale: se non concordano, la media nasconde
            # un sorteggio (audit 2026-10-02: SOD-123F a 0.54/0.46).
            view_poses = []
            for turn in range(4):
                views = [crop.rotate(90 * turn, expand=True) if turn else crop for crop in chunk]
                tensor = torch.stack([transform(view) for view in views]).to(selected_device)
                class_logits, rotation_logits = net(tensor)
                probabilities = torch.softmax(class_logits, dim=1)
                turns = torch.roll(torch.softmax(rotation_logits, dim=1), -turn, dims=1)
                class_sum = probabilities if class_sum is None else class_sum + probabilities
                rotation_sum = turns if rotation_sum is None else rotation_sum + turns
                view_poses.append(turns.argmax(dim=1).cpu().tolist())
            class_probs, rotation_probs = (class_sum / 4).cpu().tolist(), (rotation_sum / 4).cpu().tolist()
            for offset, (probabilities, turns) in enumerate(zip(class_probs, rotation_probs)):
                ranked = sorted(zip(classes, probabilities), key=lambda item: item[1], reverse=True)
                label, confidence = ranked[0]
                accepted = label != "_other" and confidence >= threshold
                box = clean_boxes[start + offset]
                rotation_kind = orientations.get(label, "none") if accepted else "none"
                rotation, rotation_confidence = _rotation(rotation_kind, turns, box)
                views_agree = len({poses[offset] for poses in view_poses}) == 1
                size = ([round((box[2] - box[0]) * float(mm_per_pixel[0]), 3),
                         round((box[3] - box[1]) * float(mm_per_pixel[1]), 3)]
                        if mm_per_pixel is not None else None)
                kind, package = _kind_package(label) if accepted else ("generic", "")
                status = ("needs_scale" if size is None else "unresolved") if accepted else (
                    "unknown" if label == "_other" else "low_confidence")
                results.append({
                    "box_px": list(box), "class": label, "kind": kind, "package": package,
                    "body": _body_topology(label) if accepted else None,
                    "class_confidence": confidence, "accepted": accepted, "threshold": threshold,
                    "rotation_deg": rotation, "rotation_confidence": rotation_confidence,
                    "rotation_kind": rotation_kind, "rotation_views_agree": views_agree,
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
