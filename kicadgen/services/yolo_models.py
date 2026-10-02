"""@brief What is known about the local YOLO weights files the user can choose.

@details The model picker (user's request, 2026-09-26) shows, next to each
file, its format, size and date, where it comes from, under which license and
with which training data. File facts are read from the disk. Provenance comes
from THIRD_PARTY_NOTICES.md, the project's single source for it; this module
repeats those facts in a form a window can show, and a test checks that the
two say the same thing.

A file is recognised by its SHA-256, never by its name: a renamed copy keeps
its provenance, a retrained or edited file with the old name does not
inherit it.

This module imports no Qt and never loads a model: hashing reads bytes only.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..app_paths import SMD_MODELS_DIR, resource_path

#: Weights formats the detector loads (see photo_yolo._MODEL_SUFFIXES).
MODEL_SUFFIXES = (".onnx", ".pt")

#: How settled a license or dataset statement is.
STATUS_CONFIRMED = "confirmed"
STATUS_TO_VERIFY = "to_verify"
STATUS_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Provenance:
    """@brief Origin of one known weights file, as THIRD_PARTY_NOTICES.md states it.

    @note Proper names, identifiers and license names are not translated;
        the statuses are keys the window turns into a sentence.
    """

    title: str
    family: str
    source: str
    license: str
    license_status: str
    dataset: str
    dataset_status: str
    classes: str


#: Known files, by SHA-256. Keep in step with THIRD_PARTY_NOTICES.md
#: (tests/test_yolo_models.py compares them).
KNOWN_MODELS: dict[str, Provenance] = {
    "4acc9608fcac382a5c0eabe747aed32af9bd48482ce29117243651d1209ec613": Provenance(
        title="Roboflow YOLO26 Medium",
        family="YOLO26 (Ultralytics 8.4.55)",
        source="Roboflow · compdetect-dnwth-1-yolo26m-t1",
        license="AGPL-3.0", license_status=STATUS_CONFIRMED,
        dataset="CompDetect (Roboflow Universe, CC BY 4.0)", dataset_status=STATUS_CONFIRMED,
        classes="21"),
    "e354ea0db881d2e96a630b74591b6356f656c9cdaa0413b3d657e125ccab923a": Provenance(
        title="Roboflow YOLO26 Large",
        family="YOLO26 (Ultralytics 8.4.55)",
        source="Roboflow · compdetect-dnwth-2-yolo26l-t1",
        license="AGPL-3.0", license_status=STATUS_TO_VERIFY,
        dataset="CompDetect (Roboflow Universe, CC BY 4.0)", dataset_status=STATUS_CONFIRMED,
        classes="16"),
    "27af3b3d1e17251b124c9de860b024833a46f77db04ab8767fc2554a3b3a4607": Provenance(
        title="YOLOv8m trained in Colab",
        family="YOLOv8m (Ultralytics 8.4.37)",
        source="Ultralytics · yolov8m.pt",
        license="AGPL-3.0", license_status=STATUS_CONFIRMED,
        dataset="dataset.verified.yaml (Colab)", dataset_status=STATUS_TO_VERIFY,
        classes=""),
    "bfc398bb93ce61d9d7d00389db51b651bd90c4e2141163a0ae6dad79a247ae29": Provenance(
        title="Base ONNX model",
        family="YOLOv8s",
        source="Arshia82sbn/pcb-yolov8s-detection",
        license="AGPL-3.0", license_status=STATUS_UNRESOLVED,
        dataset="", dataset_status=STATUS_TO_VERIFY,
        classes=""),
}


@dataclass(frozen=True)
class ModelFile:
    """@brief Facts about one weights file on disk (nothing loaded)."""

    path: Path
    size_bytes: int
    modified: datetime | None

    @property
    def format(self) -> str:
        """@brief «ONNX» or «PyTorch», from the file extension."""
        return {".onnx": "ONNX", ".pt": "PyTorch"}.get(self.path.suffix.lower(), "?")

    @property
    def runs_code_when_loaded(self) -> bool:
        """@brief True for a .pt checkpoint: loading it unpickles, i.e. runs code."""
        return self.path.suffix.lower() == ".pt"


def describe_file(path) -> ModelFile:
    """@brief Read size and date of a weights file.

    @param path File path.
    @return ModelFile; size 0 and no date when the file cannot be read.
    """
    path = Path(path)
    try:
        info = path.stat()
        return ModelFile(path, info.st_size, datetime.fromtimestamp(info.st_mtime))
    except OSError:
        return ModelFile(path, 0, None)


def list_models(folder, extra=()) -> list[ModelFile]:
    """@brief Weights files of a folder, plus files chosen elsewhere.

    @param folder Folder to scan (not recursive); missing is not an error.
    @param extra Other paths to include (the model in use, a file picked
        with «Altro file...»), shown even outside the folder.
    @return One entry per file, sorted by name, without duplicates.
    """
    found = {}
    try:
        for entry in Path(folder).iterdir():
            if entry.is_file() and model_role(entry) == "yolo_detector":
                found[os.path.normcase(str(entry.resolve()))] = entry
    except OSError:
        pass
    for path in extra:
        if not path:
            continue
        path = Path(path)
        if model_role(path) == "yolo_detector":
            found.setdefault(os.path.normcase(str(path.resolve())), path)
    return sorted((describe_file(path) for path in found.values()),
                  key=lambda item: item.path.name.casefold())


def model_role(path) -> str | None:
    """@brief Return the role of a local model file without loading it.

    @param path Candidate weights path.
    @return ``smd_classifier`` for a file in the SMD network folder
        (app_paths.SMD_MODELS_DIR), ``yolo_detector`` for other supported
        weights, or None for an unsupported extension.
    @note The classifier is recognized by its folder, not its basename
        (user decision 2026-10-02: detectors and SMD network in two folders).
        A custom YOLO checkpoint with the same name elsewhere stays
        selectable, and arbitrary custom ``.pt``/``.onnx`` files remain valid.
    """
    candidate = Path(path)
    if candidate.suffix.lower() not in MODEL_SUFFIXES:
        return None
    try:
        folder = resource_path(*SMD_MODELS_DIR).resolve()
        if candidate.resolve().parent == folder:
            return "smd_classifier"
    except OSError:
        pass
    return "yolo_detector"


def sha256_of(path, chunk=1 << 20) -> str:
    """@brief SHA-256 of a file, read in chunks (a checkpoint is ~50 MB).

    @param path File path.
    @param chunk Bytes per read.
    @return Hex digest.
    @throws OSError When the file cannot be read.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def provenance_of(sha256: str) -> Provenance | None:
    """@brief Known provenance of a file, by its SHA-256.

    @param sha256 Hex digest.
    @return The Provenance, or None for a file the project does not know.
    """
    return KNOWN_MODELS.get(str(sha256).lower())
