"""@brief Review of 2026-10-29, photo/ONNX scope: findings P1-P7 of the local detectors.

P1 an end-to-end [1, K, 6] ONNX output is read only when its metadata
declare end2end; P2 the per-view row cap of an end-to-end head is recorded
and reported when it bites; P3 one default for the «YOLO + rete SMD» route;
P4 the SMD network needs a dynamic batch axis; P5 its two heads are matched
by name; P6 a loaded model is reused while its file is unchanged; P7 the
ONNX exports reproduce the PyTorch checkpoints on stored rows.
Every test runs without the real weights except the ones marked as needing
them, which are skipped when the files or PyTorch are missing.
"""
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from kicadgen import app_paths
from kicadgen.services import photo_yolo as yolo
from kicadgen.services import smd_component_classifier as smd
from kicadgen.services.yolo_models import LoadedModelCache

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "data" / "yolo_export_parity_golden.json"


# ── fake YOLO ONNX runtime ────────────────────────────────────────────────


def _yolo_runtime(monkeypatch, tmp_path, *, shape, raw, metadata=None, name="fake.onnx"):
    """Install a fake onnxruntime answering `raw` for a model with output `shape`; return its log."""
    path = tmp_path / name
    path.write_bytes(b"inert weights")
    log = SimpleNamespace(creations=0, runs=0)
    session = SimpleNamespace(
        inputs=[SimpleNamespace(name="images", type="tensor(float)", shape=[1, 3, 640, 640])],
        outputs=[SimpleNamespace(name="output0", type="tensor(float)", shape=list(shape))],
        metadata={"task": "detect", "imgsz": "[640, 640]", "batch": "1", "stride": "32",
                  "names": repr(dict(enumerate(yolo._CLASSES))), **(metadata or {})})
    session.get_inputs = lambda: session.inputs
    session.get_outputs = lambda: session.outputs
    session.get_modelmeta = lambda: SimpleNamespace(custom_metadata_map=session.metadata)
    session.get_providers = lambda: ["CPUExecutionProvider"]

    def run(names, feeds):
        log.runs += 1
        return [raw]
    session.run = run

    def create(weights, sess_options, providers):
        log.creations += 1
        return session
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        InferenceSession=create, SessionOptions=SimpleNamespace, __version__="fake"))
    monkeypatch.setenv("KICADGEN_YOLO_MODEL", str(path))
    return log, path


def _end_to_end_rows(count, total=300, score=0.9):
    """[1, total, 6] corner rows: `count` distinct boxes scoring from `score` down, zeros after."""
    raw = np.zeros((1, total, 6), np.float32)
    for k in range(count):
        x, y = 10 + (k % 20) * 31, 10 + (k // 20) * 40
        raw[0, k] = [x, y, x + 20, y + 20, score - k * 0.002, 17]
    return raw


# ── P1: [1, K, 6] needs end2end ───────────────────────────────────────────


def test_p1_an_end_to_end_shape_without_end2end_metadata_is_refused(monkeypatch, tmp_path):
    """@brief Positive: an RT-DETR-like [1, 300, 6] export (centre/size rows, no end2end) is refused, not read as 0 bodies."""
    raw = np.zeros((1, 300, 6), np.float32)
    raw[0, 0] = [320, 320, 100, 60, 0.95, 10]      # cx, cy, w, h: read as corners -> inverted box
    _yolo_runtime(monkeypatch, tmp_path, shape=(1, 300, 6), raw=raw)
    with pytest.raises(ValueError, match="end2end=True.*export_yolo_onnx"):
        yolo.validate_yolo_setup()


@pytest.mark.parametrize("value", ["False", "garbage(", "0"])
def test_p1_an_end2end_value_other_than_true_is_refused_too(monkeypatch, tmp_path, value):
    """@brief Positive: only the literal True opens the end-to-end reading."""
    _yolo_runtime(monkeypatch, tmp_path, shape=(1, 300, 6), raw=_end_to_end_rows(1),
                  metadata={"end2end": value})
    with pytest.raises(ValueError, match="end2end=True"):
        yolo.validate_yolo_setup()


def test_p1_a_declared_end_to_end_export_is_still_read(monkeypatch, tmp_path):
    """@brief Negative: the same shape with end2end True (an Ultralytics export) is read as before."""
    _yolo_runtime(monkeypatch, tmp_path, shape=(1, 300, 6), raw=_end_to_end_rows(1),
                  metadata={"end2end": "True"})
    info = yolo.validate_yolo_setup()
    assert info["raw_head"].startswith("one-to-one") and info["output_shape"] == [1, 25, 300]
    rows = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 50, 50,
                                       settings={"tile_sizes": []})
    assert len(rows) == 1


def test_p1_a_grid_output_needs_no_end2end_metadata(monkeypatch, tmp_path):
    """@brief Negative: the standard grid is identified by its own shape, without the key."""
    _yolo_runtime(monkeypatch, tmp_path, shape=(1, 25, 8400),
                  raw=np.zeros((1, 25, 8400), np.float32))
    assert yolo.validate_yolo_setup()["raw_head"] == "standard"


_LOCAL_ONNX = sorted((ROOT / "models" / "yolo").glob("*.onnx"))


@pytest.mark.skipif(not _LOCAL_ONNX, reason="no local YOLO .onnx weights (git-ignored)")
@pytest.mark.parametrize("path", _LOCAL_ONNX, ids=lambda p: p.name)
def test_p1_the_local_onnx_detectors_still_load(path):
    """@brief The shipped exports keep loading: the YOLO26 ones carry end2end True."""
    pytest.importorskip("onnxruntime")
    info = yolo.validate_yolo_setup(path)
    if info["model_output_shape"][-1] == 6:
        assert info["raw_head"].startswith("one-to-one") and info["per_view_row_cap"] == 300
    else:
        assert info["raw_head"] == "standard" and info["per_view_row_cap"] is None


# ── P2: per-view row cap ──────────────────────────────────────────────────


def _detect_with_audit(tmp_path):
    logs = []
    audit = tmp_path / "audit"
    rows = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 50, 50,
                                       log=logs.append, settings={"tile_sizes": []},
                                       audit_dir=audit)
    document = json.loads(sorted(audit.glob("*.json"))[-1].read_text(encoding="utf-8"))
    return rows, logs, document


def test_p2_a_full_end_to_end_view_is_reported_as_capped(monkeypatch, tmp_path):
    """@brief Positive: all K rows above the audit floor -> warning, audit flags the view and the cap."""
    _yolo_runtime(monkeypatch, tmp_path, shape=(1, 300, 6), raw=_end_to_end_rows(300),
                  metadata={"end2end": "True"})
    rows, logs, document = _detect_with_audit(tmp_path)
    assert len(rows) == 300
    assert document["per_view_row_cap"] == 300 and document["recipe"]["per_view_row_cap"] == 300
    assert document["views"][0]["row_cap_reached"] is True
    assert document["views"][0]["lowest_returned_score"] == pytest.approx(0.9 - 299 * 0.002)
    from kicadgen.services.messages import WARNING, kind_of
    capped = [line for line in logs if "max_det" in line]
    assert len(capped) == 1 and kind_of(capped[0]) == WARNING and "300" in capped[0]


def test_p2_a_view_with_room_left_is_not_reported(monkeypatch, tmp_path):
    """@brief Negative: fewer bodies than K (zero padding below the floor) -> cap recorded, no warning."""
    _yolo_runtime(monkeypatch, tmp_path, shape=(1, 300, 6), raw=_end_to_end_rows(12),
                  metadata={"end2end": "True"})
    rows, logs, document = _detect_with_audit(tmp_path)
    assert len(rows) == 12
    assert document["recipe"]["per_view_row_cap"] == 300
    assert document["views"][0]["row_cap_reached"] is False
    assert not [line for line in logs if "max_det" in line]


def test_p2_the_standard_grid_has_no_row_cap(monkeypatch, tmp_path):
    """@brief Negative: a grid returns every anchor, so even a dense view is not a cap."""
    raw = np.zeros((1, 25, 8400), np.float32)
    raw[0, :4] = [[320.0], [320.0], [10.0], [10.0]]
    raw[0, 4 + 17] = 0.3                          # every anchor above the audit floor
    _yolo_runtime(monkeypatch, tmp_path, shape=(1, 25, 8400), raw=raw)
    _rows, logs, document = _detect_with_audit(tmp_path)
    assert document["per_view_row_cap"] is None and document["recipe"]["per_view_row_cap"] is None
    assert document["views"][0]["row_cap_reached"] is False
    assert not [line for line in logs if "max_det" in line]


def test_p2_the_torch_session_reports_its_lowest_row_only_for_an_end_to_end_head():
    """@brief Positive/negative: the .pt path gives the same signal as the ONNX one, none for a grid."""
    class Tensor:
        def __init__(self, array):
            self.array = array

        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self.array

    class NoGrad:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    torch = SimpleNamespace(from_numpy=Tensor, no_grad=NoGrad)
    detections = _end_to_end_rows(300)
    e2e = yolo._TorchSession(lambda t: Tensor(detections), torch, np, "images", end_to_end=True)
    feeds = {"images": np.zeros((1, 3, 640, 640), np.float32)}
    _outputs, lowest = e2e.run_with_lowest_score(None, feeds)
    assert lowest == pytest.approx(float(detections[0, -1, 4]))
    grid = yolo._TorchSession(lambda t: Tensor(np.ones((1, 25, 8400), np.float32)), torch, np,
                              "images")
    assert grid.run_with_lowest_score(None, feeds)[1] is None


def _export_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("export_yolo_onnx_review",
                                                  ROOT / "tools" / "export_yolo_onnx.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_p2_the_export_passes_max_det_explicitly():
    """@brief Positive: both branches carry max_det; the cap is a declared property of the file."""
    tool = _export_tool()
    for end_to_end in (True, False):
        assert tool.export_arguments(end_to_end)["max_det"] == tool.EXPORT_MAX_DET == 300


@pytest.mark.parametrize("cap, refused", [(100, True), (300, False)])
def test_p2_an_export_whose_cap_is_not_max_det_is_refused(monkeypatch, tmp_path, cap, refused):
    """@brief Positive (100 rows) / negative (300 rows): the read-back K must be the max_det passed."""
    tool = _export_tool()

    class FakeYOLO:
        def __init__(self, path, task):
            self.model = SimpleNamespace(model=[SimpleNamespace(one2one_cv2=object())])

        def export(self, **kwargs):
            exported = tmp_path / "work.onnx"
            exported.write_bytes(b"x")
            return str(exported)
    proto = SimpleNamespace(metadata_props=[])
    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=FakeYOLO))
    monkeypatch.setitem(sys.modules, "onnx", SimpleNamespace(
        load=lambda path: proto, save=lambda model, path: Path(path).write_bytes(b"saved")))
    monkeypatch.setattr(yolo, "_setup", lambda path: (None, None, None, None, {
        "raw_head": "one-to-one", "per_view_row_cap": cap}))
    source = tmp_path / "detector.pt"
    source.write_bytes(b"checkpoint")
    if refused:
        with pytest.raises(ValueError, match="expected max_det 300"):
            tool.export_checkpoint(source, tmp_path / "detector.onnx")
    else:
        assert tool.export_checkpoint(source, tmp_path / "detector.onnx")["end_to_end"] is True


# ── P3: one default for the YOLO + SMD route ──────────────────────────────


@pytest.fixture
def model_folders(monkeypatch, tmp_path):
    """Point the program's resources at an empty temporary tree."""
    monkeypatch.setattr(app_paths, "resource_path", lambda *parts: tmp_path.joinpath(*parts))
    folder = tmp_path / "models" / "yolo"
    folder.mkdir(parents=True)
    return folder


def test_p3_without_the_checkpoint_the_route_uses_its_onnx_export(model_folders):
    """@brief Positive: the installed program (export only) gets the export, not a missing .pt."""
    (model_folders / app_paths.DEFAULT_YOLO_SMD_ONNX).write_bytes(b"x")
    assert app_paths.default_yolo_smd_model() == model_folders / app_paths.DEFAULT_YOLO_SMD_ONNX


def test_p3_the_checkpoint_wins_and_neither_names_the_checkpoint(model_folders):
    """@brief Negative: with the .pt present it stays the default; with neither, its name for the error."""
    assert app_paths.default_yolo_smd_model() == model_folders / app_paths.DEFAULT_YOLO_PT
    (model_folders / app_paths.DEFAULT_YOLO_SMD_ONNX).write_bytes(b"x")
    (model_folders / app_paths.DEFAULT_YOLO_PT).write_bytes(b"x")
    assert app_paths.default_yolo_smd_model() == model_folders / app_paths.DEFAULT_YOLO_PT


@pytest.mark.parametrize("checkpoint_present", [False, True])
def test_p3_the_inventory_follows_the_shared_rule(model_folders, monkeypatch, tmp_path,
                                                   checkpoint_present):
    """@brief Positive (export only -> export) / negative (checkpoint -> checkpoint) in extract_inventory."""
    from kicadgen.services import photo_inventory as inv
    (model_folders / app_paths.DEFAULT_YOLO_SMD_ONNX).write_bytes(b"x")
    if checkpoint_present:
        (model_folders / app_paths.DEFAULT_YOLO_PT).write_bytes(b"x")
    full = np.zeros((40, 60, 3), np.uint8)

    def prepare(path, length, width, calibration, *, frame_callback):
        frame_callback(full, (10, 10, 30, 30))
        return full[10:30, 10:30].copy()
    seen = []

    def detect(photo, length, width, **kwargs):
        seen.append(Path(kwargs["model_path"]).name)
        return []
    monkeypatch.setattr(inv, "prepare_photo", prepare)
    monkeypatch.setattr(yolo, "detect_yolo_components", detect)
    monkeypatch.setattr(smd, "classify_photo_boxes", lambda *a, **k: [])
    inv.extract_inventory(['top.png'], 'unused-remote-model', 10, 10, [],
                          lambda **kw: pytest.fail('Remote photo call'), top_photo_index=0,
                          detector='yolo_smd', cache_dir=tmp_path / "cache")
    assert seen == [app_paths.DEFAULT_YOLO_PT if checkpoint_present
                    else app_paths.DEFAULT_YOLO_SMD_ONNX]


# ── fake SMD ONNX runtime (P4, P5, P6) ────────────────────────────────────

_META = {"format": "smdmap-classifier-2", "arch": "resnet18", "img_size": 224,
         "classes": ["chip_R", "SOT-23", "QFN", "_other"],
         "orientations": {"chip_R": "axis", "SOT-23": "full", "QFN": "full", "_other": "none"},
         "suggested_threshold": 0.0}


def _smd_runtime(monkeypatch, *, batch="batch", outputs=("class_logits", "rotation_logits")):
    """Fake onnxruntime for a 4-class network whose class head says 'SOT-23'.

    `outputs` gives the declared output order; `run` answers by the names it
    is asked for, as onnxruntime does, and refuses a batch other than a
    fixed `batch`.
    """
    log = SimpleNamespace(opens=0, batches=[])
    # Read in the wrong head, the rotation logits would make the class "QFN".
    heads = {"class_logits": np.array([0., 10., 0., 0.], np.float32),
             "rotation_logits": np.array([0., 0., 10., 0.], np.float32)}

    class Session:
        def __init__(self, path, sess_options, providers):
            log.opens += 1

        def get_modelmeta(self):
            return SimpleNamespace(custom_metadata_map={smd.ONNX_METADATA_KEY: json.dumps(_META)})

        def get_inputs(self):
            return [SimpleNamespace(name="images", type="tensor(float)", shape=[batch, 3, 224, 224])]

        def get_outputs(self):
            return [SimpleNamespace(name=n, type="tensor(float)", shape=[batch, 4]) for n in outputs]

        def run(self, names, feeds):
            images = feeds["images"]
            if isinstance(batch, int) and len(images) != batch:
                raise RuntimeError("Got invalid dimensions for input: images")
            log.batches.append(len(images))
            names = list(names) if names is not None else list(outputs)
            return [np.repeat(heads[name][None], len(images), axis=0) for name in names]
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        InferenceSession=Session, SessionOptions=SimpleNamespace))
    return log


def _onnx_file(tmp_path, name="smd.onnx", content=b"graph"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _boxes(count):
    return [[(k % 8) * 10, (k // 8) * 10, (k % 8) * 10 + 8, (k // 8) * 10 + 8] for k in range(count)]


# ── P4: dynamic batch axis only ───────────────────────────────────────────


def test_p4_a_fixed_batch_graph_is_refused_before_any_inference(monkeypatch, tmp_path):
    """@brief Positive: a [1, 3, 224, 224] graph is refused with the reason, not by onnxruntime mid-run."""
    from PIL import Image
    log = _smd_runtime(monkeypatch, batch=1)
    path = _onnx_file(tmp_path)
    with pytest.raises(ValueError, match="batch axis fixed"):
        smd.validate_smd_setup(path)
    with pytest.raises(ValueError, match="batch axis fixed"):
        smd.classify_photo_boxes(Image.new("RGB", (80, 80)), _boxes(3), model_path=path)
    assert log.batches == []


def test_p4_a_dynamic_batch_graph_takes_batches_of_any_size(monkeypatch, tmp_path):
    """@brief Negative: the symbolic axis keeps working for 40 boxes in chunks of 32 and 8."""
    from PIL import Image
    monkeypatch.setattr(smd, "_attach_kicad_results", lambda *args: None)
    log = _smd_runtime(monkeypatch)
    rows = smd.classify_photo_boxes(Image.new("RGB", (80, 80)), _boxes(40),
                                    model_path=_onnx_file(tmp_path))
    assert len(rows) == 40 and sorted(set(log.batches)) == [8, 32]


# ── P5: heads matched by name ─────────────────────────────────────────────


def test_p5_heads_declared_in_the_other_order_are_read_by_name(monkeypatch, tmp_path):
    """@brief Positive: with 4 classes both heads are [N, 4]; the class still comes from class_logits."""
    from PIL import Image
    monkeypatch.setattr(smd, "_attach_kicad_results", lambda *args: None)
    _smd_runtime(monkeypatch, outputs=("rotation_logits", "class_logits"))
    row = smd.classify_photo_boxes(Image.new("RGB", (80, 80)), _boxes(1),
                                   model_path=_onnx_file(tmp_path))[0]
    assert row["class"] == "SOT-23"


@pytest.mark.parametrize("outputs", [("logits", "rotation_logits"), ("class_logits", "class_logits")])
def test_p5_heads_with_other_names_are_refused(monkeypatch, tmp_path, outputs):
    """@brief Negative: shapes alone cannot tell the heads apart, so unknown names are refused."""
    _smd_runtime(monkeypatch, outputs=outputs)
    with pytest.raises(ValueError, match="ONNX outputs"):
        smd.validate_smd_setup(_onnx_file(tmp_path))


# ── P6: loaded models reused while the file is unchanged ──────────────────


def _rewrite_with_new_identity(path, content):
    """Rewrite a file so that its size AND modification time differ."""
    stat = path.stat()
    path.write_bytes(content)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))


def test_p6_validate_then_detect_open_the_yolo_model_once(monkeypatch, tmp_path):
    """@brief Positive: the second call reuses the session; the info is a private copy."""
    log, _path = _yolo_runtime(monkeypatch, tmp_path, shape=(1, 25, 8400),
                               raw=np.zeros((1, 25, 8400), np.float32))
    info = yolo.validate_yolo_setup()
    info["classes"].append("edited by the caller")
    yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 50, 50,
                                settings={"tile_sizes": []})
    assert log.creations == 1 and log.runs == 1
    assert "edited by the caller" not in yolo.validate_yolo_setup()["classes"]


def test_p6_a_changed_yolo_file_is_loaded_again(monkeypatch, tmp_path):
    """@brief Negative: new size and mtime -> a new session (and a new hash in the info)."""
    log, path = _yolo_runtime(monkeypatch, tmp_path, shape=(1, 25, 8400),
                              raw=np.zeros((1, 25, 8400), np.float32))
    first = yolo.validate_yolo_setup()["model_sha256"]
    _rewrite_with_new_identity(path, b"retrained weights, longer")
    second = yolo.validate_yolo_setup()["model_sha256"]
    assert log.creations == 2 and first != second


def test_p6_another_runtime_never_gets_a_cached_session(monkeypatch, tmp_path):
    """@brief Negative: the same unchanged file under a different onnxruntime is loaded by that one."""
    first, path = _yolo_runtime(monkeypatch, tmp_path, shape=(1, 25, 8400),
                                raw=np.zeros((1, 25, 8400), np.float32))
    yolo.validate_yolo_setup(path)
    stat = path.stat()
    second, _ = _yolo_runtime(monkeypatch, tmp_path, shape=(1, 25, 8400),
                              raw=np.zeros((1, 25, 8400), np.float32))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))   # same bytes, same mtime
    yolo.validate_yolo_setup(path)
    assert first.creations == 1 and second.creations == 1


def test_p6_validate_describe_and_classify_open_the_smd_onnx_once(monkeypatch, tmp_path):
    """@brief Positive: the three entry points share one session of the unchanged file."""
    from PIL import Image
    monkeypatch.setattr(smd, "_attach_kicad_results", lambda *args: None)
    log = _smd_runtime(monkeypatch)
    path = _onnx_file(tmp_path)
    smd.validate_smd_setup(path)
    smd.describe_smd_checkpoint(path)
    smd.classify_photo_boxes(Image.new("RGB", (80, 80)), _boxes(1), model_path=path)
    assert log.opens == 1


def test_p6_a_changed_smd_file_is_opened_again(monkeypatch, tmp_path):
    """@brief Negative: a rewritten network is validated again."""
    log = _smd_runtime(monkeypatch)
    path = _onnx_file(tmp_path)
    smd.validate_smd_setup(path)
    _rewrite_with_new_identity(path, b"another graph, longer")
    smd.validate_smd_setup(path)
    assert log.opens == 2


def test_p6_the_smd_checkpoint_is_loaded_once_for_validate_and_describe(monkeypatch, tmp_path):
    """@brief Positive: the .pt route too; a changed checkpoint is loaded again (negative)."""
    loads = []
    checkpoint = dict(_META, state_dict={})
    monkeypatch.setattr(smd, "_runtime", lambda: (SimpleNamespace(), None, None))
    monkeypatch.setattr(smd, "_load_checkpoint", lambda path, torch: loads.append(path) or checkpoint)
    monkeypatch.setattr(smd, "_validate_checkpoint", lambda *args: None)
    path = tmp_path / "smd.pt"
    path.write_bytes(b"checkpoint")
    smd.validate_smd_setup(path)
    assert smd.describe_smd_checkpoint(path)["classes"] == _META["classes"]
    assert len(loads) == 1
    _rewrite_with_new_identity(path, b"another checkpoint")
    smd.validate_smd_setup(path)
    assert len(loads) == 2


def test_p6_failed_loads_are_not_cached_and_capacity_is_bounded(tmp_path):
    """@brief Unit: an exception stores nothing; the least recently used file is evicted."""
    cache = LoadedModelCache(capacity=2)
    files = []
    for name in ("a", "b", "c"):
        files.append(tmp_path / name)
        files[-1].write_bytes(name.encode())
    calls = []

    def failing():
        calls.append("fail")
        raise ValueError("incompatible")
    for _ in range(2):
        with pytest.raises(ValueError):
            cache.get_or_load(files[0], failing)
    assert calls == ["fail", "fail"]
    for path in files:
        cache.get_or_load(path, lambda path=path: calls.append(path.name) or path.name)
    cache.get_or_load(files[2], lambda: calls.append("c again"))
    cache.get_or_load(files[0], lambda: calls.append("a again") or "a")
    assert calls[2:] == ["a", "b", "c", "a again"]
    with pytest.raises(ValueError):
        LoadedModelCache(capacity=0)


# ── P7: export parity on stored rows ──────────────────────────────────────

#: Detectors compared: (role, stem). The end-to-end YOLO26 export and the
#: standard-grid YOLOv8 export, the two heads tools/export_yolo_onnx.py makes.
_PARITY_MODELS = (("end_to_end", "pcb_component_detector_yolo26_roboflow_m"),
                  ("grid", "pcb_component_detector_yolov8m_best"))
#: Tolerances between two runtimes on the same float32 graph: box corners in
#: letterbox pixels, scores absolute. Measured 2026-10-29: 6.1e-5 on the
#: rows at the audit floor, 2.5e-3 on the grid; the bounds leave margin for
#: another CPU, not for another model.
_BOX_TOLERANCE_PX, _SCORE_TOLERANCE = 0.05, 5e-3
#: Grid anchors stored: the strongest ones of the checkpoint on the board.
_GRID_ANCHORS = 16


def _synthetic_board():
    """A deterministic 640 x 640 BGR board: green mask, dark ICs with leads, capped chips."""
    import cv2
    rng = np.random.default_rng(1029)
    image = np.zeros((640, 640, 3), np.uint8)
    image[:] = (40, 110, 30)
    for _ in range(6):
        x, y = (int(v) for v in rng.integers(20, 520, 2))
        w, h = (int(v) for v in rng.integers(60, 110, 2))
        cv2.rectangle(image, (x, y), (x + w, y + h), (25, 25, 25), -1)
        for k in range(x + 6, x + w - 4, 10):
            cv2.rectangle(image, (k, y - 8), (k + 4, y), (190, 190, 190), -1)
            cv2.rectangle(image, (k, y + h), (k + 4, y + h + 8), (190, 190, 190), -1)
    for _ in range(30):
        x, y = (int(v) for v in rng.integers(10, 610, 2))
        cv2.rectangle(image, (x, y), (x + 20, y + 10), (60, 120, 170), -1)
        cv2.rectangle(image, (x, y), (x + 4, y + 10), (200, 200, 200), -1)
        cv2.rectangle(image, (x + 16, y), (x + 20, y + 10), (200, 200, 200), -1)
    return image


def _board_tensor():
    import cv2
    return yolo._letterbox(_synthetic_board(), np, cv2)[0]


def _live_rows(role, stem, tensor):
    """Raw rows of the checkpoint (through the detector's .pt session) and of the ONNX export."""
    import onnxruntime as ort
    folder = ROOT / "models" / "yolo"
    session = yolo._setup_torch(folder / f"{stem}.pt")[0]
    checkpoint = session.raw_output({"images": tensor})
    export = ort.InferenceSession(str(folder / f"{stem}.onnx"),
                                  providers=["CPUExecutionProvider"]).run(None, {"images": tensor})[0]
    if role == "end_to_end":
        keep = checkpoint[0, :, 4] >= yolo._AUDIT_CONFIDENCE
        return checkpoint[0, keep], export[0, :int(keep.sum())]
    anchors = np.argsort(-checkpoint[0, 4:].max(axis=0), kind="stable")[:_GRID_ANCHORS]
    return checkpoint[0][:, anchors].T, export[0][:, anchors].T


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_golden():
    """Regenerate tests/data/yolo_export_parity_golden.json from the local weights (maintainer only).

    Run from the repository root, with requirements/yolo-pt.txt installed:
    `PYTHONPATH=. .venv/Scripts/python.exe tests/test_review_1029_photo.py`.
    Only after a deliberate change of the shipped weights: the stored rows
    are the reference the live test compares against.
    """
    tensor = _board_tensor()
    document = {"note": "Raw rows of two YOLO checkpoints and of their ONNX exports on the "
                        "synthetic board of tests/test_review_1029_photo.py; written by "
                        "write_golden() from the local weights.",
                "tensor_sha256": hashlib.sha256(tensor.tobytes()).hexdigest(), "models": {}}
    for role, stem in _PARITY_MODELS:
        checkpoint, export = _live_rows(role, stem, tensor)
        folder = ROOT / "models" / "yolo"
        document["models"][role] = {
            "stem": stem, "pt_sha256": _sha(folder / f"{stem}.pt"),
            "onnx_sha256": _sha(folder / f"{stem}.onnx"),
            "checkpoint_rows": np.round(checkpoint.astype(np.float64), 5).tolist(),
            "export_rows": np.round(export.astype(np.float64), 5).tolist()}
    GOLDEN.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")


def _assert_same_rows(role, checkpoint, export):
    checkpoint, export = np.asarray(checkpoint, np.float64), np.asarray(export, np.float64)
    assert checkpoint.shape == export.shape and len(checkpoint)
    if role == "end_to_end":
        assert np.abs(checkpoint[:, :4] - export[:, :4]).max() <= _BOX_TOLERANCE_PX
        assert np.abs(checkpoint[:, 4] - export[:, 4]).max() <= _SCORE_TOLERANCE
        assert np.array_equal(checkpoint[:, 5], export[:, 5])
    else:
        assert np.abs(checkpoint[:, :4] - export[:, :4]).max() <= _BOX_TOLERANCE_PX
        assert np.abs(checkpoint[:, 4:] - export[:, 4:]).max() <= _SCORE_TOLERANCE
        assert np.array_equal(checkpoint[:, 4:].argmax(axis=1), export[:, 4:].argmax(axis=1))


@pytest.mark.parametrize("role", [role for role, _stem in _PARITY_MODELS])
def test_p7_the_stored_export_rows_match_the_checkpoint_rows(role):
    """@brief Stored rows only (no weights needed): the export gives the checkpoint's rows."""
    stored = json.loads(GOLDEN.read_text(encoding="utf-8"))["models"][role]
    _assert_same_rows(role, stored["checkpoint_rows"], stored["export_rows"])


def test_p7_both_stored_end_to_end_rows_give_the_same_detector_proposals():
    """@brief The detector's conversion turns the two stored row sets into the same bodies."""
    stored = json.loads(GOLDEN.read_text(encoding="utf-8"))["models"]["end_to_end"]
    proposals = []
    for key in ("checkpoint_rows", "export_rows"):
        raw = np.asarray(stored[key], np.float32)[None]
        rows = yolo._one_to_one_rows(raw, list(range(len(yolo._CLASSES))), np)
        found, _invalid = yolo._proposals(rows, (0, 0, 640, 640), (640, 640), (1.0, 0, 0),
                                          "overview", np)
        proposals.append([(row["class"], row["rejection"], [round(v, 1) for v in row["box"]])
                          for row in found])
    assert proposals[0] == proposals[1] and any(rej is None for _c, rej, _b in proposals[0])


def test_p7_a_changed_export_would_fail_the_comparison():
    """@brief Negative: the comparison is not vacuous -- a class swap or a shifted box fails it."""
    stored = json.loads(GOLDEN.read_text(encoding="utf-8"))["models"]
    rows = np.asarray(stored["end_to_end"]["export_rows"], np.float64)
    shifted = rows.copy()
    shifted[0, 0] += 1.0
    swapped = rows.copy()
    swapped[0, 5] = (swapped[0, 5] + 1) % len(yolo._CLASSES)
    for broken in (shifted, swapped):
        with pytest.raises(AssertionError):
            _assert_same_rows("end_to_end", stored["end_to_end"]["checkpoint_rows"], broken)


def _weights_present(stem):
    folder = ROOT / "models" / "yolo"
    return (folder / f"{stem}.pt").is_file() and (folder / f"{stem}.onnx").is_file()


@pytest.mark.parametrize("role, stem", _PARITY_MODELS)
def test_p7_the_local_weights_still_reproduce_the_stored_rows(role, stem):
    """@brief Live check where the weights and PyTorch exist; skipped elsewhere (CI)."""
    if not _weights_present(stem):
        pytest.skip("local YOLO weights absent (git-ignored)")
    pytest.importorskip("torch")
    pytest.importorskip("ultralytics")
    pytest.importorskip("onnxruntime")
    document = json.loads(GOLDEN.read_text(encoding="utf-8"))
    stored = document["models"][role]
    folder = ROOT / "models" / "yolo"
    if (_sha(folder / f"{stem}.pt"), _sha(folder / f"{stem}.onnx")) != (
            stored["pt_sha256"], stored["onnx_sha256"]):
        pytest.skip("local weights differ from the ones the golden rows were written with")
    tensor = _board_tensor()
    assert hashlib.sha256(tensor.tobytes()).hexdigest() == document["tensor_sha256"]
    checkpoint, export = _live_rows(role, stem, tensor)
    _assert_same_rows(role, checkpoint, export)
    _assert_same_rows(role, checkpoint, stored["checkpoint_rows"])
    _assert_same_rows(role, export, stored["export_rows"])


if __name__ == "__main__":
    write_golden()
