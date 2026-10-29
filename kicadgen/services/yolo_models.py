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
        dataset="Detecting the PCB object, Arshiasbn (MIT)", dataset_status=STATUS_TO_VERIFY,
        classes="21"),
    "e354ea0db881d2e96a630b74591b6356f656c9cdaa0413b3d657e125ccab923a": Provenance(
        title="Roboflow YOLO26 Large",
        family="YOLO26 (Ultralytics 8.4.55)",
        source="Roboflow · compdetect-dnwth-2-yolo26l-t1",
        license="AGPL-3.0", license_status=STATUS_CONFIRMED,
        dataset="CompDetect (Roboflow Universe, CC BY 4.0)", dataset_status=STATUS_CONFIRMED,
        classes="16"),
    "27af3b3d1e17251b124c9de860b024833a46f77db04ab8767fc2554a3b3a4607": Provenance(
        title="YOLOv8m trained in Colab",
        family="YOLOv8m (Ultralytics 8.4.37)",
        source="Ultralytics · yolov8m.pt",
        license="AGPL-3.0", license_status=STATUS_CONFIRMED,
        dataset="Detecting the PCB object, Arshiasbn (MIT)", dataset_status=STATUS_CONFIRMED,
        classes="21"),
    # ONNX exports of the three checkpoints above (tools/export_yolo_onnx.py,
    # 2026-10-28): the files the installer ships, same bodies as the .pt.
    "66528ed90ad53e91b697d58fa1b9dacf45f35363798089107fa2efdb4776b29b": Provenance(
        title="Roboflow YOLO26 Large (ONNX export)",
        family="YOLO26 (Ultralytics 8.4.163 export, end-to-end output)",
        source="Roboflow · compdetect-dnwth-2-yolo26l-t1",
        license="AGPL-3.0", license_status=STATUS_CONFIRMED,
        dataset="CompDetect (Roboflow Universe, CC BY 4.0)", dataset_status=STATUS_CONFIRMED,
        classes="16"),
    "c3169e2ba2cbbed794039e983f14a21e4ebc6e217574a132fdd80285e0541988": Provenance(
        title="Roboflow YOLO26 Medium (ONNX export)",
        family="YOLO26 (Ultralytics 8.4.163 export, end-to-end output)",
        source="Roboflow · compdetect-dnwth-1-yolo26m-t1",
        license="AGPL-3.0", license_status=STATUS_CONFIRMED,
        dataset="Detecting the PCB object, Arshiasbn (MIT)", dataset_status=STATUS_TO_VERIFY,
        classes="21"),
    "bb040d1f2758bbd1f812362832d80d6de376437ca256d1a30a27c30452bf8752": Provenance(
        title="YOLOv8m trained in Colab (ONNX export)",
        family="YOLOv8m (Ultralytics 8.4.163 export, raw output)",
        source="Ultralytics · yolov8m.pt",
        license="AGPL-3.0", license_status=STATUS_CONFIRMED,
        dataset="Detecting the PCB object, Arshiasbn (MIT)", dataset_status=STATUS_CONFIRMED,
        classes="21"),
    "bfc398bb93ce61d9d7d00389db51b651bd90c4e2141163a0ae6dad79a247ae29": Provenance(
        title="Base ONNX model",
        family="YOLOv8s",
        source="Arshia82sbn/pcb-yolov8s-detection",
        license="AGPL-3.0", license_status=STATUS_CONFIRMED,
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
        """@brief «ONNX» or «PyTorch», from the file extension.

        @return "ONNX", "PyTorch" or "?" for another extension.
        """
        return {".onnx": "ONNX", ".pt": "PyTorch"}.get(self.path.suffix.lower(), "?")

    @property
    def runs_code_when_loaded(self) -> bool:
        """@brief True for a .pt checkpoint: loading it unpickles, i.e. runs code.

        @return Whether the file extension is .pt.
        """
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


class LoadedModelCache:
    """@brief Keep the last few loaded model files, reused while the file is unchanged.

    @details Validating a model before a paid analysis and then running it
    used to read, hash and open the same weights twice (review 2026-10-29,
    P6). The loaded object (an onnxruntime session, a PyTorch network) is
    kept here under the file's identity and reused by the next caller.
    @note The key is (resolved path, size, st_mtime_ns) plus the identity of
        the runtime modules the caller names: a file rewritten in place gets
        a new size or modification time and is loaded again, and a different
        runtime (another onnxruntime, a test's fake) never receives an
        object built by the previous one. A file replaced with the same size
        AND the same nanosecond modification time is not detected; that is
        the accepted limit of a stat-based key.
    @note Failed loads are never stored. The loader runs outside the lock,
        so two threads asking for the same new file may both load it once;
        the cache itself stays consistent. This module still never loads a
        model on its own: it only remembers what the caller's loader built.
    """

    def __init__(self, capacity=2):
        """@brief Create an empty cache.

        @param capacity Number of loaded files kept, least recently used
            evicted first; at least 1.
        @throws ValueError For a capacity that is not a positive integer.
        """
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("cache capacity must be a positive integer")
        import threading
        self._capacity = capacity
        self._entries = []          # [(key, runtime objects kept alive, value)], newest last
        self._lock = threading.Lock()

    @staticmethod
    def _key(path, runtime):
        # The ids are safe only because the entry keeps the runtime objects
        # alive: an id cannot be reused while its object exists.
        resolved = Path(path).resolve()
        stat = resolved.stat()
        return (str(resolved), stat.st_size, stat.st_mtime_ns,
                tuple(id(item) for item in runtime))

    def get_or_load(self, path, loader, runtime=tuple):
        """@brief Return the cached object for `path`, or load it and remember it.

        @param path Weights file.
        @param loader Callable without arguments that loads and validates
            the file; its exceptions propagate and nothing is stored.
        @param runtime Callable without arguments returning the runtime
            objects (modules) the loaded object depends on; read before the
            lookup and again after a load, so a module the loader imports
            for the first time is part of the stored key. Default: none.
        @return The loader's result, fresh or cached.
        @throws OSError When the file cannot be examined.
        """
        key = self._key(path, runtime())
        with self._lock:
            for index, (stored, _alive, value) in enumerate(self._entries):
                if stored == key:
                    self._entries.append(self._entries.pop(index))
                    return value
        value = loader()
        alive = tuple(runtime())
        # Stat of the file BEFORE the load: a file changed while it was being
        # loaded gets a newer key at the next call and is loaded again.
        key = key[:3] + (tuple(id(item) for item in alive),)
        with self._lock:
            self._entries = [entry for entry in self._entries if entry[0] != key]
            self._entries.append((key, alive, value))
            del self._entries[:-self._capacity]
        return value

    def clear(self):
        """@brief Forget every loaded file (tests, or to free memory)."""
        with self._lock:
            self._entries.clear()
