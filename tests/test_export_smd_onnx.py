"""@brief Contracts of tools/export_smd_onnx.py without PyTorch or onnx.

The real export was run on 2026-10-28: 30 classes, max logit difference
2e-6 against the checkpoint, same classes and rotations on real boxes of
two photos. Here only the tool's decisions are checked: which metadata
leave the checkpoint and the command-line refusals.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("export_smd_onnx", ROOT / "tools" / "export_smd_onnx.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_metadata_keep_what_inference_reads_and_drop_the_training_command(tool):
    """@brief Positive/negative: classes, recipe and metrics travel; `args` (local path) and tensors do not."""
    checkpoint = {
        "format": "smdmap-classifier-2", "arch": "resnet18", "img_size": 224,
        "classes": ["chip_R", "_other"], "orientations": {"chip_R": "axis", "_other": "none"},
        "suggested_threshold": 0.0, "epoch": 40, "trained": "20261001-162356",
        "catalog_sha256": "ab" * 32, "metrics": {"val": {"samples": 42}},
        "args": {"output": "C:\\Lavori\\Python\\TorchObjectDetection\\models\\smd_classifier.pt"},
        "state_dict": {"weight": object()},
    }
    text = tool.metadata_json(checkpoint)
    data = json.loads(text)
    assert data["classes"] == ["chip_R", "_other"] and data["metrics"] == {"val": {"samples": 42}}
    assert "args" not in data and "state_dict" not in data
    assert "Lavori" not in text and "TorchObjectDetection" not in text


def test_a_value_that_is_not_json_fails_loudly(tool):
    """@brief Negative: a metadata value the JSON cannot carry stops the export instead of being dropped silently."""
    with pytest.raises(ValueError, match="not serialisable"):
        tool.metadata_json({"format": "smdmap-classifier-2", "metrics": {"tensor": object()}})


def test_main_refuses_a_non_checkpoint_and_an_existing_target(tool, tmp_path, capsys):
    """@brief Negative: an .onnx source or an existing target without --force exits 1."""
    onnx = tmp_path / "smd_classifier.onnx"
    onnx.write_bytes(b"x")
    assert tool.main([str(onnx)]) == 1
    checkpoint = tmp_path / "smd_classifier.pt"
    checkpoint.write_bytes(b"x")
    assert tool.main([str(checkpoint)]) == 1
    assert "--force" in capsys.readouterr().err
