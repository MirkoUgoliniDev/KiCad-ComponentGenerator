"""@brief Exercise local detector contracts with fake ONNX sessions, never real inference."""
import builtins
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from kicadgen.services import photo_yolo as yolo


_NAMES = (
    "battery", "button", "buzzer", "capacitor", "clock", "connector", "diode",
    "display", "fuse", "heatsink", "ic", "inductor", "led", "pads", "pins",
    "potentiometer", "relay", "resistor", "switch", "transformer", "transistor",
)


def _raw(*entries):
    result = np.zeros((1, 25, 8400), dtype=np.float32)
    for index, (xywh, class_id, confidence) in enumerate(entries):
        result[0, :4, index] = xywh
        result[0, 4 + class_id, index] = confidence
    return result


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """@brief Fail accidental network access in every detector test."""
    def forbidden(*args, **kwargs):
        pytest.fail("Local YOLO must not access the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def setup(monkeypatch, tmp_path):
    """@brief Supply a fake CPU runtime and tiny inert weights for every inference test."""
    path = tmp_path / "fake.onnx"
    path.write_bytes(b"inert ONNX test weights; never execute")
    session = SimpleNamespace(
        inputs=[SimpleNamespace(name="custom_images", type="tensor(float)", shape=[1, 3, 640, 640])],
        outputs=[SimpleNamespace(name="output0", type="tensor(float)", shape=[1, 25, 8400])],
        metadata={"task": "detect", "imgsz": "[640, 640]", "batch": "1", "stride": "32",
                  "names": repr(dict(enumerate(_NAMES)))},
        raw=_raw(), calls=[], creations=[],
    )
    session.get_inputs = lambda: session.inputs
    session.get_outputs = lambda: session.outputs
    session.get_modelmeta = lambda: SimpleNamespace(custom_metadata_map=session.metadata)
    session.get_providers = lambda: ["CPUExecutionProvider"]

    def run(output_names, feeds):
        assert output_names is None
        assert set(feeds) == {"custom_images"}
        tensor = feeds["custom_images"]
        assert tensor.dtype == np.float32 and tensor.shape == (1, 3, 640, 640)
        assert tensor.flags.c_contiguous
        call_index = len(session.calls)
        session.calls.append(tensor)
        return [session.raw(call_index) if callable(session.raw) else session.raw]

    def create(weights, *, sess_options, providers):
        assert isinstance(weights, bytes)
        assert providers == ["CPUExecutionProvider"]
        assert sess_options.intra_op_num_threads == 4
        session.creations.append(weights)
        return session

    session.run = run
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        InferenceSession=create, SessionOptions=SimpleNamespace, __version__="fake-cpu",
    ))
    monkeypatch.setenv("KICADGEN_YOLO_MODEL", str(path))
    return session, path


def test_public_import_does_not_load_optional_packages(monkeypatch):
    """@brief Import the detector while all optional runtime imports are forbidden."""
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"numpy", "cv2", "onnxruntime"}:
            pytest.fail(f"Eager optional import: {name}")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    spec = importlib.util.spec_from_file_location("isolated_photo_yolo", yolo.__file__)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_model_resolution_priority_and_default(setup, monkeypatch, tmp_path):
    """@brief Resolve explicit, environment, then repository-default weights in order."""
    _, path = setup
    explicit = tmp_path / "explicit.onnx"
    explicit.write_bytes(b"other")
    assert yolo.resolve_yolo_model(explicit) == explicit.resolve()
    assert yolo.resolve_yolo_model() == path.resolve()
    monkeypatch.delenv("KICADGEN_YOLO_MODEL")
    monkeypatch.setattr(yolo, "__file__", str(tmp_path / "kicadgen" / "services" / "photo_yolo.py"))
    default = tmp_path / "models" / "pcb_yolov8s.onnx"
    default.parent.mkdir()
    default.write_bytes(b"default")
    assert yolo.resolve_yolo_model() == default.resolve()


@pytest.mark.parametrize("filename,content", [("missing.onnx", None), ("empty.onnx", b""), ("unsafe.pt", b"data")])
def test_missing_empty_or_non_onnx_weights(tmp_path, filename, content):
    """@brief Reject missing, empty and pickle-format weights with actionable guidance."""
    path = tmp_path / filename
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(ValueError, match="KICADGEN_YOLO_MODEL"):
        yolo.resolve_yolo_model(path)


def test_unreadable_weights(setup, monkeypatch):
    """@brief Convert filesystem permission failures to a model-path error."""
    _, path = setup
    def denied(*args, **kwargs):
        raise PermissionError("test access denied")
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(ValueError, match="Cannot read local YOLO weights.*access denied"):
        yolo.resolve_yolo_model(path)


@pytest.mark.parametrize("package", ["numpy", "cv2", "onnxruntime"])
def test_missing_optional_runtime_is_actionable(setup, monkeypatch, package):
    """@brief Explain how to install a missing optional runtime without doing so."""
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == package:
            raise ImportError(f"missing {package}")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(ValueError, match="requirements-yolo.txt"):
        yolo.validate_yolo_setup()


def test_preflight_reads_metadata_and_hash_without_inference(setup):
    """@brief Validate the fixed CPU model contract without executing a prediction."""
    session, path = setup
    info = yolo.validate_yolo_setup()
    assert info["model_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert info["classes"] == list(_NAMES)
    assert info["input_shape"] == [1, 3, 640, 640]
    assert info["providers"] == ["CPUExecutionProvider"]
    assert not session.calls


@pytest.mark.parametrize("field,value", [
    ("input_shape", ["batch", 3, 640, 640]), ("input_shape", [1, 3, 320, 320]),
    ("input_type", "tensor(float16)"), ("output_shape", [1, 8400, 25]),
    ("output_shape", [1, 84, 8400]), ("output_type", "tensor(int64)"),
    ("task", "segment"), ("imgsz", "[1280, 1280]"), ("batch", "2"),
    ("stride", "64"), ("names", "{0: 'ic'}"),
    ("names", "__import__('os').system('echo unsafe')"),
])
def test_reject_incompatible_model_contract(setup, field, value):
    """@brief Reject dynamic/wrong tensors, tasks, or unsafe class metadata."""
    session, _ = setup
    if field.startswith("input_"):
        setattr(session.inputs[0], field[6:], value)
    elif field.startswith("output_"):
        setattr(session.outputs[0], field[7:], value)
    else:
        session.metadata[field] = value
    with pytest.raises(ValueError, match="compatible local YOLO model"):
        yolo.validate_yolo_setup()
    assert not session.calls


def test_reject_reordered_classes(setup):
    """@brief Reject a same-sized model whose class indices have different semantics."""
    session, _ = setup
    names = dict(enumerate(_NAMES))
    names[13], names[10] = names[10], names[13]
    session.metadata["names"] = repr(names)
    with pytest.raises(ValueError, match="21 PCB classes in order"):
        yolo.validate_yolo_setup()


def test_corrupt_model_load_is_actionable(setup, monkeypatch):
    """@brief Convert ONNX parser errors into a useful setup error."""
    def fail(*args, **kwargs):
        raise RuntimeError("invalid protobuf")
    monkeypatch.setattr(sys.modules["onnxruntime"], "InferenceSession", fail)
    with pytest.raises(ValueError, match="invalid protobuf.*self-contained"):
        yolo.validate_yolo_setup()


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), "20", None])
@pytest.mark.parametrize("axis", [0, 1])
def test_invalid_board_dimensions(setup, value, axis):
    """@brief Reject nonnumeric, nonfinite, boolean and nonpositive calibration."""
    session, _ = setup
    dimensions = [20, 10]
    dimensions[axis] = value
    with pytest.raises(ValueError, match="finite positive number"):
        yolo.detect_yolo_components(np.zeros((10, 20, 3), np.uint8), *dimensions)
    assert not session.creations


@pytest.mark.parametrize("photo", [
    None, [], np.zeros((0, 10, 3), np.uint8), np.zeros((10, 0, 3), np.uint8),
    np.zeros((10, 20), np.uint8), np.zeros((10, 20, 4), np.uint8),
    np.full((10, 20, 3), np.nan), np.zeros((10, 20, 3), np.float32),
])
def test_invalid_photo(setup, photo):
    """@brief Reject empty, NaN, grayscale and non-BGR-array photos before inference."""
    session, _ = setup
    with pytest.raises(ValueError, match="nonempty uint8 BGR"):
        yolo.detect_yolo_components(photo, 20, 10)
    assert not session.creations


def test_letterbox_rgb_normalization_and_geometry(setup):
    """@brief Verify padding, BGR-to-RGB conversion and inverse letterbox coordinates."""
    session, _ = setup
    photo = np.empty((100, 200, 3), np.uint8)
    photo[:] = (0, 128, 255)
    # Original rectangle (20,10)-(80,30), scale 3.2, top padding 160.
    session.raw = _raw(([160, 224, 192, 64], 17, 0.75))
    components = yolo.detect_yolo_components(photo, 40, 20)
    assert len(components) == 1
    c = components[0]
    assert c["_box"] == pytest.approx((0.1, 0.1, 0.4, 0.3))
    assert (c["x_mm"], c["y_mm"], c["length_mm"], c["width_mm"]) == pytest.approx((-10, -6, 12, 4))
    assert c["rotation_deg"] == 0
    assert c["kind"] == "generic" and c["name"] == "YOLO candidate 1"
    assert c["_id"] == 1 and c["_source"] == "YOLO local" and c["_uncertain"] is True
    assert c["height_mm"] == 1.0 and c["_height_estimated"] is True
    assert "placeholder" in c["_height_source"] and "not_measured" in c["_height_source"]
    assert c["_yolo_class"] == "resistor" and c["_yolo_confidence"] == 0.75
    assert c["color_hex"] == "#303030" and c["marking"] == ""
    assert "package" not in c and "_detected_kind" not in c
    tensor = session.calls[0]
    assert tensor[0, :, 0, 0] == pytest.approx([114 / 255] * 3)
    assert tensor[0, :, 160, 0] == pytest.approx([1, 128 / 255, 0])


def test_rotation_uses_physical_long_axis(setup):
    """@brief Choose the long axis in calibrated millimeters, not source pixels."""
    session, _ = setup
    session.raw = _raw(([320, 320, 200, 100], 10, 0.8))
    c = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 32, 128)[0]
    assert c["length_mm"] == 20 and c["width_mm"] == 10 and c["rotation_deg"] == 90


def test_recipe_has_41_views_and_complete_edge_coverage():
    """@brief Freeze the experiment recipe and cover irregular image edges."""
    views = list(yolo._views(4000, 1848))
    assert len(views) == 41
    assert views[0] == ("overview", (0, 0, 4000, 1848))
    assert sum(name.startswith("tile640") for name, _ in views) == 32
    assert sum(name.startswith("tile1280") for name, _ in views) == 8
    for tile_size in (640, 1280):
        covered = np.zeros((1300, 1700), dtype=bool)
        for name, (x0, y0, x1, y1) in yolo._views(1700, 1300):
            if name.startswith(f"tile{tile_size}"):
                covered[y0:y1, x0:x1] = True
        assert covered.all()


def test_a_fragment_is_dropped_only_when_another_view_saw_the_body_whole(setup):
    """@brief Il taglio di una tessera non e' prova di assenza.

    La regola sui frammenti serve a togliere i doppioni di cio' che un'altra
    vista vede per intero: qui la vista 2 lo vede intero, quindi il frammento
    della vista 1 e' un doppione e sparisce, e sopravvive l'ingombro NON
    troncato. Il caso opposto e' nel test seguente.
    """
    session, _ = setup
    # Stesso corpo: intero in tile640-360-0 (x 600..680), tagliato dal bordo
    # destro di tile640-0-0 (x 600..640).
    outputs = [_raw(), _raw(([630, 50, 60, 40], 10, 0.9)),
               _raw(([280, 50, 80, 60], 10, 0.8)), _raw()]
    session.raw = lambda index: outputs[index]
    results = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64)
    assert len(results) == 1
    assert results[0]["_box"] == pytest.approx((0.6, 20 / 640, 0.68, 80 / 640))
    assert not results[0]["_yolo_clipped_recovery"]


def test_a_body_no_view_saw_whole_is_rebuilt_instead_of_lost(setup):
    """@brief Il caso del regolatore: piu' grande della sovrapposizione fra tessere.

    Su dati reali il rilevatore lo aveva visto quattro volte (la migliore a
    0,165) e ogni volta il riquadro toccava un bordo, perche' i suoi ~350 px
    non stanno nei 160 px di sovrapposizione delle tessere da 640. Scartando
    ogni frammento il corpo spariva del tutto. Qui due meta' viste da tessere
    diverse tornano un corpo solo, marcato come ricomposto perche' il suo
    ingombro resta troncato.
    """
    session, _ = setup
    outputs = [_raw(), _raw(([500, 50, 300, 40], 10, 0.9)),
               _raw(([20, 50, 40, 40], 10, 0.7)), _raw()]
    session.raw = lambda index: outputs[index]
    results = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64)
    assert len(results) == 1
    row = results[0]
    assert row["_yolo_clipped_recovery"] and "cucitura" in row["_source"]
    assert not row["_yolo_review_only"]
    # Unione dei due frammenti: 350..640 dalla prima tessera, 360..400 dalla
    # seconda. Restano i bordi esterni, cioe' l'ingombro piu' ampio visto.
    assert row["_box"] == pytest.approx((0.35, 30 / 640, 0.64, 70 / 640))
    assert row["_yolo_confidence"] == pytest.approx(0.9)


@pytest.mark.parametrize("xywh", [[0, 320, 20, 40], [640, 320, 20, 40],
                                   [320, 0, 40, 20], [320, 640, 40, 20]])
def test_board_edges_are_clipped_and_retained(setup, xywh):
    """@brief Preserve genuine detections touching any outer board edge."""
    session, _ = setup
    session.raw = _raw((xywh, 10, 0.9))
    c = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64)[0]
    x0, y0, x1, y1 = c["_box"]
    assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1


@pytest.mark.parametrize("xywh", [[0, 320, 20, 40], [640, 320, 20, 40],
                                   [320, 0, 40, 20], [320, 640, 40, 20]])
def test_all_interior_tile_edges_rejected(xywh):
    """@brief Treat all four internal crop boundaries as possible body cuts."""
    rows, invalid = yolo._proposals(_raw((xywh, 10, 0.9)), (100, 100, 740, 740),
                                    (1000, 1000), (1, 0, 0), "tile", np)
    assert invalid == 0 and rows[0]["rejection"] == "clipped_by_tile"


def test_full_view_keeps_large_module_and_small_contained_body(setup):
    """@brief Preserve a large overview envelope and distinct small contained detection."""
    session, _ = setup
    overview = _raw(([320, 320, 580, 300], 10, 0.85), ([320, 320, 20, 20], 3, 0.9))
    session.raw = lambda index: overview if index == 0 else _raw()
    results = yolo.detect_yolo_components(np.zeros((1280, 2560, 3), np.uint8), 80, 40)
    assert len(results) == 2
    assert max(c["length_mm"] for c in results) == pytest.approx(72.5)


def test_nms_class_agnostic_excludes_pads_pins_before_suppression(setup):
    """@brief Suppress duplicates across classes without letting pads/pins erase bodies."""
    session, _ = setup
    session.raw = _raw(
        ([200, 200, 100, 100], 13, 0.99), ([200, 200, 100, 100], 14, 0.98),
        ([200, 200, 100, 100], 3, 0.85), ([202, 202, 100, 100], 17, 0.75),
        ([400, 400, 50, 50], 10, 0.25), ([500, 500, 50, 50], 10, 0.24),
    )
    results = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64)
    assert [c["_yolo_class"] for c in results] == ["capacitor", "ic"]
    assert [c["_id"] for c in results] == [1, 2]


def test_empty_inference_is_empty_inventory_with_audit(setup, tmp_path):
    """@brief Report zero detections as success and preserve the empty audit."""
    results = yolo.detect_yolo_components(np.zeros((20, 40, 3), np.uint8), 40, 20,
                                          audit_dir=tmp_path / "audit")
    assert results == []
    document = json.loads(next((tmp_path / "audit").glob("yolo-*.json")).read_text())
    assert document["raw_proposals"] == document["retained_detector_results"] == []


def test_invalid_predictions_do_not_poison_good_results_or_json(setup, tmp_path):
    """@brief Reject NaN, infinite, invalid-score and inverted-box predictions independently."""
    session, _ = setup
    session.raw = _raw(
        ([100, 100, 20, 20], 10, 0.75), ([float("nan"), 0, 20, 20], 10, 0.9),
        ([0, 0, 20, 20], 10, float("nan")), ([float("inf"), 0, 20, 20], 10, 0.9),
        ([100, 100, -20, 20], 10, 0.9), ([100, 100, 20, 20], 10, 1.5),
    )
    results = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64,
                                          audit_dir=tmp_path / "audit")
    assert len(results) == 1
    contents = next((tmp_path / "audit").glob("yolo-*.json")).read_text()
    assert "NaN" not in contents and "Infinity" not in contents
    document = json.loads(contents)
    assert all(view["invalid_rows"] == 4 for view in document["views"])


def test_audit_contains_hash_recipe_raw_and_retained_without_overwrite(setup, tmp_path):
    """@brief Preserve enough evidence to replay filtering and distinguish repeated runs."""
    session, path = setup
    session.raw = _raw(([200, 200, 100, 100], 10, 0.8), ([400, 400, 20, 20], 13, 0.9))
    logs = []
    for _ in range(2):
        results = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64,
                                              log=logs.append, audit_dir=tmp_path / "audit")
    paths = list((tmp_path / "audit").glob("yolo-*.json"))
    assert len(paths) == 2
    audit = json.loads(paths[0].read_text())
    assert audit["model_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert audit["recipe"]["tile_sizes"] == [640, 1280]
    assert audit["recipe"]["confidence"] == 0.25
    assert len(audit["raw_proposals"]) == 6 and len(audit["retained_detector_results"]) == 1
    assert any(row["rejection"] == "excluded_class" for row in audit["raw_proposals"])
    assert audit["components"] == json.loads(json.dumps(results))
    assert any("audit:" in message for message in logs)


def test_no_silent_component_limit(setup):
    """@brief Keep hundreds of disjoint boxes rather than applying a hidden top-k limit."""
    session, _ = setup
    session.raw = _raw(*[([x, y, 8, 8], 10, 0.8)
                         for y in range(10, 630, 30) for x in range(10, 630, 30)])
    results = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64)
    assert len(results) == 441
    assert [c["_id"] for c in results] == list(range(1, 442))


@pytest.mark.parametrize("output", [np.zeros((1, 25, 1), np.float32),
                                     np.zeros((1, 25, 8400), np.float64), None])
def test_invalid_runtime_output_fails_explicitly(setup, output):
    """@brief Refuse unexpected output tensors instead of claiming a complete inventory."""
    session, _ = setup
    session.raw = output
    with pytest.raises(ValueError, match="inference failed"):
        yolo.detect_yolo_components(np.zeros((20, 40, 3), np.uint8), 40, 20)


def test_inference_failure_does_not_return_partial_inventory(setup):
    """@brief Fail the entire detection when a later tile cannot be inferred."""
    session, _ = setup
    def output(index):
        if index:
            raise RuntimeError("simulated CPU failure")
        return _raw(([320, 320, 100, 100], 10, 0.9))
    session.raw = output
    with pytest.raises(ValueError, match="simulated CPU failure"):
        yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64)


def test_unwritable_audit_is_reported(setup, tmp_path):
    """@brief Do not silently omit requested audit evidence when the destination fails."""
    blocker = tmp_path / "file-not-directory"
    blocker.write_text("occupied")
    with pytest.raises(ValueError, match="Cannot write YOLO audit"):
        yolo.detect_yolo_components(np.zeros((20, 40, 3), np.uint8), 40, 20, audit_dir=blocker)


def test_audit_accounts_for_every_proposal_and_links_components(setup, tmp_path):
    """@brief Distinguish confidence/class filtering, both NMS stages and retained bodies."""
    session, _ = setup
    session.raw = _raw(([200, 200, 100, 100], 10, 0.9),
                       ([201, 200, 100, 100], 3, 0.8),
                       ([400, 400, 20, 20], 17, 0.2),
                       ([450, 450, 20, 20], 13, 0.9))
    photo = np.zeros((640, 640, 3), np.uint8)
    components = yolo.detect_yolo_components(photo, 64, 64, audit_dir=tmp_path)
    audit = json.loads(next(tmp_path.glob('yolo-*.json')).read_text())
    assert audit['audit_schema_version'] == 2
    assert audit['image_sha256'] == hashlib.sha256(photo.tobytes()).hexdigest()
    proposals = {row['proposal_id']: row for row in audit['raw_proposals']}
    rejected = {key for key, row in proposals.items() if row['rejection']}
    suppressed = {row['proposal_id'] for row in audit['nms_suppressions']}
    retained = {row['proposal_id'] for row in audit['retained_detector_results']}
    assert len(proposals) == len(audit['raw_proposals']) == 12
    assert not (rejected & suppressed or rejected & retained or suppressed & retained)
    assert rejected | suppressed | retained == proposals.keys()
    assert len(audit['nms_suppressions']) == len(suppressed) == 5
    assert {row['stage'] for row in audit['nms_suppressions']} == {'eligible_per_view', 'global'}
    for row in audit['nms_suppressions']:
        assert row['suppressed_by'] in proposals
        assert row['iou'] > audit['recipe']['nms_iou']
    assert {row['_yolo_proposal_id'] for row in components} == retained
    for view in audit['views']:
        assert view['invalid_rows'] + view['valid_rows_below_audit_floor'] + view['raw_proposals'] == 8400
        assert view['filter_rejections'] == {'below_confidence': 1, 'excluded_class': 1}
        assert view['eligible_before_nms'] == 2
    assert 'below audit floor' in audit['audit_scope']


def test_nms_trace_preserves_small_contained_and_adjacent_bodies():
    """@brief Trace true overlap suppression without absorbing contained or adjacent bodies."""
    rows = [dict(proposal_id=str(i), box=box, confidence=score) for i, (box, score) in enumerate([
        ([0, 0, 100, 100], .9), ([10, 10, 20, 20], .8),
        ([21, 10, 31, 20], .7), ([10, 10, 20, 20], .6),
    ])]
    trace = []
    result = yolo._nms(rows, np, trace, 'global')
    assert result == yolo._nms(rows, np) == rows[:3]
    assert trace == [dict(proposal_id='3', suppressed_by='1', stage='global', iou=1.0)]


def test_image_hash_uses_pixels_and_changes_with_input(setup, tmp_path):
    """@brief Hash noncontiguous BGR input consistently and distinguish changed photo pixels."""
    photo = np.zeros((20, 40, 3), np.uint8)[:, ::2]
    assert not photo.flags.c_contiguous
    hashes = []
    for index in range(2):
        photo[0, 0, 0] = index
        directory = tmp_path / str(index)
        yolo.detect_yolo_components(photo, 40, 20, audit_dir=directory)
        audit = json.loads(next(directory.glob('yolo-*.json')).read_text())
        hashes.append(audit['image_sha256'])
        assert hashes[-1] == hashlib.sha256(photo.tobytes()).hexdigest()
        assert audit['nms_suppressions'] == []
    assert hashes[0] != hashes[1]


@pytest.mark.parametrize('excluded_id', [13, 14])
def test_competing_body_evidence_survives_excluded_argmax(setup, tmp_path, excluded_id):
    """@brief Keep uncertain bodies with sufficient alternate evidence, not bare pads/pins."""
    session, _ = setup
    session.raw = _raw(([200, 200, 20, 20], excluded_id, .8),
                       ([300, 300, 20, 20], excluded_id, .8))
    session.raw[0, 4 + 3, 0] = .4
    session.raw[0, 4 + 3, 1] = .1
    result = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64,
                                         audit_dir=tmp_path)
    assert len(result) == 1
    assert result[0]['kind'] == 'generic' and result[0]['_uncertain']
    assert result[0]['_yolo_class_ambiguous']
    assert result[0]['_yolo_confidence'] == pytest.approx(.4)
    audit = json.loads(next(tmp_path.glob('yolo-*.json')).read_text())
    kept = audit['retained_detector_results'][0]
    assert kept['argmax_class'] == _NAMES[excluded_id]
    assert kept['argmax_confidence'] == pytest.approx(.8)
    assert kept['best_body_class'] == 'capacitor'
    assert all(row['rejection'] == 'excluded_class' for row in audit['raw_proposals']
               if row['prediction_index'] == 1)


def test_review_queue_preserves_weak_bodies_without_promoting_pads_or_duplicates(setup, tmp_path):
    """@brief Surface uncertain body hypotheses separately from accepted detector results."""
    session, _ = setup
    session.raw = _raw(([100, 100, 20, 20], 10, .8),
                       ([100, 100, 20, 20], 3, .2),
                       ([300, 300, 20, 20], 3, .2),
                       ([400, 400, 20, 20], 13, .2),
                       ([500, 500, 20, 20], 17, .01))
    results = yolo.detect_yolo_components(np.zeros((640, 640, 3), np.uint8), 64, 64,
                                          audit_dir=tmp_path)
    audit = json.loads(next(tmp_path.glob('yolo-*.json')).read_text())
    assert len(results) == 1
    queue = audit['low_confidence_review_proposals']
    assert len(queue) == 1 and queue[0]['prediction_index'] == 2
    assert queue[0]['rejection'] == 'below_confidence'
    assert queue[0]['proposal_id'] not in {r['_yolo_proposal_id'] for r in results}


@pytest.mark.parametrize('audit_enabled', [False, True])
def test_opt_in_review_candidates_are_deduplicated_and_audit_independent(setup, tmp_path, audit_enabled):
    """@brief Return weak bodies only on opt-in with unique IDs and explicit provenance."""
    session, _ = setup
    session.raw = _raw(([100, 100, 20, 20], 10, .8),
                       ([100, 100, 20, 20], 3, .2),
                       ([300, 300, 20, 20], 3, .2),
                       ([400, 400, 20, 20], 13, .2),
                       ([500, 500, 20, 20], 14, .2),
                       ([550, 550, 20, 20], 17, .01))
    photo = np.zeros((640, 640, 3), np.uint8)
    baseline = yolo.detect_yolo_components(photo, 64, 64)
    result = yolo.detect_yolo_components(photo, 64, 64, include_review_candidates=True,
                                         audit_dir=tmp_path if audit_enabled else None)
    assert len(baseline) == 1 and len(result) == 2
    assert [c['_id'] for c in result] == [1, 2]
    assert len({c['_yolo_proposal_id'] for c in result}) == 2
    assert result[0] == baseline[0]
    weak = result[1]
    assert weak['_yolo_origin'] == 'low_confidence_review' and weak['_yolo_review_only']
    assert weak['_yolo_confidence'] == pytest.approx(.2)
    assert weak['_uncertain'] and weak['confidence'] == 'low' and weak['kind'] == 'generic'
    assert weak['_box'] == pytest.approx([290/640, 290/640, 310/640, 310/640])
    assert weak['length_mm'] == pytest.approx(2) and weak['width_mm'] == pytest.approx(2)
    assert 'package' not in weak
    if audit_enabled:
        audit = json.loads(next(tmp_path.glob('yolo-*.json')).read_text())
        assert audit['recipe']['include_review_candidates'] is True
        assert len(audit['retained_detector_results']) == 1
        assert audit['components'] == json.loads(json.dumps(result))
    else:
        assert not list(tmp_path.glob('yolo-*.json'))


def test_a_weak_fragment_comes_back_as_a_weak_hypothesis_not_as_nothing(setup):
    """@brief Sotto soglia e tagliato restano due cose diverse.

    Il frammento ricomposto rientra dalla porta che gli spetta -- quella delle
    ipotesi deboli, da rivedere -- e non da quella dei candidati sicuri. E' il
    caso reale: il regolatore era a 0,165, sotto la soglia operativa.
    """
    session, _ = setup
    outputs = [_raw(), _raw(([630, 50, 60, 40], 3, .2)), _raw(), _raw()] * 2
    session.raw = lambda index: outputs[index]
    result = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                         include_review_candidates=True)
    assert len(result) == 1
    assert result[0]['_yolo_review_only'] and result[0]['_yolo_clipped_recovery']
    assert result[0]['_yolo_confidence'] == pytest.approx(.2)
    # Senza l'adesione esplicita alla revisione, un'ipotesi debole non esce.
    assert yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64) == []


@pytest.mark.parametrize('value', [None, 'false', 0, 1])
def test_review_candidate_opt_in_rejects_non_boolean(setup, value):
    """@brief Require explicit boolean semantics before running local inference."""
    session, _ = setup
    with pytest.raises(ValueError, match='include_review_candidates must be a boolean'):
        yolo.detect_yolo_components(np.zeros((10, 10, 3), np.uint8), 10, 10,
                                    include_review_candidates=value)
    assert not session.calls and not session.creations


@pytest.mark.parametrize('audit_enabled', [False, True])
def test_production_inventory_sends_weak_bodies_to_global_review(setup, monkeypatch, tmp_path, audit_enabled):
    """@brief Run the production detector/reviewer chain with local fake inference and AI."""
    from kicadgen.services import photo_inventory
    session, _ = setup
    session.raw = _raw(([100, 100, 20, 20], 10, .8),
                       ([300, 300, 20, 20], 3, .2),
                       ([400, 400, 20, 20], 13, .2))
    monkeypatch.setattr(photo_inventory, 'prepare_photo',
                        lambda *args: np.zeros((640, 640, 3), np.uint8))
    requests = []
    def review(**kwargs):
        requests.append(kwargs)
        payload = json.loads(kwargs['content_blocks'][4]['text'])
        assert [c['id'] for c in payload['candidates']] == [1, 2]
        assert payload['candidates'][1]['yolo_confidence'] == pytest.approx(.2)
        metadata = payload['candidates'][1]['detector_metadata']
        assert metadata['_yolo_review_only'] is True
        assert metadata['_yolo_origin'] == 'low_confidence_review'
        assert metadata['_yolo_proposal_id']
        return dict(components=[], candidate_decisions=[
            dict(candidate_id=c['id'], status='uncertain', component_id=None,
                 rejection_category=None, reason='Physical presence unresolved; preserve for review')
            for c in payload['candidates']], uncertainties=['Unresolved body evidence'])
    report = photo_inventory.extract_inventory(
        [tmp_path / 'top.png'], 'offline/model', 64, 64, [], review,
        detector='yolo', top_photo_index=0,
        cache_dir=tmp_path / 'cache' if audit_enabled else None)
    assert len(requests) == 1 and len(report.components) == 2
    assert report.calibration['proposal_count'] == 2
    assert report.photo_png and report.overlay_png
    weak = next(c for c in report.components if c['_yolo_review_only'])
    assert weak['_yolo_origin'] == 'low_confidence_review' and weak['_uncertain']
    assert weak['kind'] == 'generic' and weak['package'] == ''
    assert weak['_global_inventory_status'] == 'validated_hypotheses'


def test_a_clipped_edge_is_named_so_that_nobody_mistakes_it_for_a_measure(setup):
    """@brief Un bordo sul confine della tessera non e' un bordo del corpo.

    E' l'errore che ha rimpicciolito del 15% il modulo radio della scheda di
    prova: tre frammenti tagliati sulla stessa cucitura «concordavano» su un
    bordo che era solo il taglio della tessera, e chi li ha fusi lo ha preso
    per buono. Chi riceve le proposte deve sapere quali lati sono artefatti.
    """
    session, _ = setup
    # Un corpo tagliato dal bordo DESTRO della prima tessera (x=640).
    outputs = [_raw(), _raw(([620, 320, 80, 60], 10, .9)), _raw(), _raw()]
    session.raw = lambda index: outputs[index]
    row = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                      include_review_candidates=True)[0]
    assert row['_yolo_clipped_recovery'] and row['_yolo_clipped_sides'] == ['right']


def test_an_edge_another_view_saw_beyond_is_not_clipped_any_more(setup):
    """@brief Caso negativo: la ricomposizione non deve marcare cio' che e' misurato.

    Se un secondo frammento vede oltre quella cucitura, quel bordo e' stato
    osservato davvero e non va dichiarato artefatto: dichiararlo tale
    inviterebbe chi sta a valle a ignorare una misura buona.
    """
    session, _ = setup
    # Due frammenti dello stesso corpo: il primo tagliato dal bordo destro
    # della sua tessera (x=640), il secondo tagliato a sinistra ma che il
    # bordo destro lo VEDE, a x=700.
    outputs = [_raw(), _raw(([570, 320, 140, 60], 10, .9)),
               _raw(([170.5, 320, 339, 60], 10, .8)), _raw()]
    session.raw = lambda index: outputs[index]
    rows = yolo.detect_yolo_components(np.zeros((640, 1000, 3), np.uint8), 100, 64,
                                       include_review_candidates=True)
    merged = [row for row in rows if row['_yolo_clipped_recovery']]
    assert len(merged) == 1, 'i due frammenti dovevano essere ricomposti in uno'
    sides = merged[0]['_yolo_clipped_sides']
    assert 'right' not in sides, 'quel bordo lo ha misurato il secondo frammento'
    assert 'left' in sides, 'quello sinistro resta un taglio di tessera'
    assert merged[0]['_box'][2] == pytest.approx(.7)
