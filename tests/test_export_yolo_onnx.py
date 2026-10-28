"""@brief Contracts of tools/export_yolo_onnx.py without PyTorch, Ultralytics or onnx.

The real export was run on 2026-10-28 on the three development checkpoints
and compared with the .pt files through the detector on two photos: the
YOLOv8m raw export and the YOLO26 end-to-end exports gave the same bodies.
Here only the decisions of the tool are checked: which branch, which
arguments, which metadata is rewritten, and that a result read back as the
other branch is refused.
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("export_yolo_onnx", ROOT / "tools" / "export_yolo_onnx.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("head,expected", [
    (SimpleNamespace(one2one_cv2=object()), True),     # YOLO26 head with its branch
    (SimpleNamespace(one2one_cv2=None), False),        # fused checkpoint: branch removed
    (SimpleNamespace(), False),                        # YOLOv8 standard head
])
def test_the_branch_follows_the_head_not_the_name(tool, head, expected):
    """@brief The one-to-one branch is exported only when the head really has it."""
    assert tool.has_end_to_end_head(head) is expected


def test_export_arguments_keep_the_detector_contract(tool):
    """@brief nms=False selects the end-to-end branch, nms=None the raw grid; the rest is fixed."""
    end_to_end, raw = tool.export_arguments(True), tool.export_arguments(False)
    assert end_to_end["nms"] is False and raw["nms"] is None
    for arguments in (end_to_end, raw):
        assert arguments["format"] == "onnx" and arguments["imgsz"] == 640
        assert arguments["batch"] == 1 and arguments["dynamic"] is False


def test_only_the_description_is_rewritten(tool):
    """@brief The training path lives in `description`; the other keys are kept as written."""
    props = [SimpleNamespace(key="description",
                             value="Ultralytics YOLO26l model trained on /train/cache/dataset/data.yaml"),
             SimpleNamespace(key="names", value="{0: 'ic'}"),
             SimpleNamespace(key="license", value="AGPL-3.0 License")]
    model = SimpleNamespace(metadata_props=props)
    assert tool.scrub_metadata(model, "KiCadGen detector") == ["description"]
    assert props[0].value == "KiCadGen detector"
    assert props[1].value == "{0: 'ic'}" and props[2].value == "AGPL-3.0 License"
    assert tool.scrub_metadata(SimpleNamespace(metadata_props=props[1:]), "x") == []


def _fake_runtime(monkeypatch, tool, tmp_path, *, head, read_back_head, calls):
    """Fake ultralytics/onnx and the detector's _setup; record the export arguments."""
    exported = tmp_path / "work" / "model.onnx"

    class FakeYOLO:
        def __init__(self, path, task):
            calls.append(("load", Path(path).name, task))
            self.model = SimpleNamespace(model=[None, head])

        def export(self, **kwargs):
            calls.append(("export", kwargs))
            exported.parent.mkdir(parents=True, exist_ok=True)
            exported.write_bytes(b"exported")
            return str(exported)

    proto = SimpleNamespace(metadata_props=[SimpleNamespace(key="description", value="/train/x")])
    fake_onnx = SimpleNamespace(load=lambda path: proto,
                                save=lambda model, path: Path(path).write_bytes(b"saved:" + model.metadata_props[0].value.encode()))
    monkeypatch.setitem(sys.modules, "ultralytics", SimpleNamespace(YOLO=FakeYOLO))
    monkeypatch.setitem(sys.modules, "onnx", fake_onnx)
    from kicadgen.services import photo_yolo
    monkeypatch.setattr(photo_yolo, "_setup", lambda path: (None, None, None, None, {
        "raw_head": read_back_head, "model_output_shape": [1, 300, 6],
        "model_classes": ["ic"], "class_order": "remapped by name"}))
    return proto


def test_an_end_to_end_checkpoint_is_exported_scrubbed_and_verified(tool, monkeypatch, tmp_path):
    """@brief Positive: YOLO26 head -> nms=False, description rewritten, result read back."""
    calls = []
    source = tmp_path / "models" / "yolo" / "detector.pt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"checkpoint")
    _fake_runtime(monkeypatch, tool, tmp_path, head=SimpleNamespace(one2one_cv2=object()),
                  read_back_head="one-to-one (end-to-end; anchor x class rows merged per box)",
                  calls=calls)
    target = tmp_path / "out" / "detector.onnx"
    info = tool.export_checkpoint(source, target)
    assert calls[0] == ("load", "detector.pt", "detect")
    assert calls[1][1]["nms"] is False
    assert target.read_bytes() == b"saved:KiCadGen PCB component detector exported from detector.pt"
    assert info["end_to_end"] is True and len(info["sha256"]) == 64


def test_a_result_read_as_the_other_branch_is_refused(tool, monkeypatch, tmp_path):
    """@brief Negative: exported raw but read back as end-to-end -> the file would not reproduce the checkpoint."""
    calls = []
    source = tmp_path / "detector.pt"
    source.write_bytes(b"checkpoint")
    _fake_runtime(monkeypatch, tool, tmp_path, head=SimpleNamespace(),
                  read_back_head="one-to-one (end-to-end)", calls=calls)
    with pytest.raises(ValueError, match="exported raw output"):
        tool.export_checkpoint(source, tmp_path / "detector.onnx")
    assert calls[1][1]["nms"] is None


def test_main_refuses_a_non_checkpoint_and_an_existing_target(tool, tmp_path, capsys):
    """@brief Negative: a .onnx source or an existing target without --force exits 1."""
    onnx = tmp_path / "weights.onnx"
    onnx.write_bytes(b"x")
    assert tool.main([str(onnx)]) == 1
    checkpoint = tmp_path / "weights.pt"
    checkpoint.write_bytes(b"x")
    assert tool.main([str(checkpoint)]) == 1
    assert "--force" in capsys.readouterr().err
