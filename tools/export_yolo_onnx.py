"""@brief Export a YOLO .pt checkpoint to the .onnx the installed program reads.

Usage: python tools/export_yolo_onnx.py models/yolo/<name>.pt [--output <file>.onnx] [--force]

The Windows installer has no PyTorch: it reads only .onnx weights. This tool
produces, from a checkpoint used during development, the ONNX file that gives
the SAME bodies as the checkpoint through the detector:

- standard head (YOLOv8): the raw score grid `[1, 4 + classes, 8400]`;
- end-to-end head (YOLO26, YOLOv10): the one-to-one detections `[1, K, 6]`,
  which is what the .pt path uses (photo_yolo._force_one_to_one). The raw
  grid of such a model would go through the detector's own NMS and find
  different bodies (2026-10-28: 127 against 111 on one photo).

Ultralytics writes the training data path into the `description` metadata;
it is replaced here so that no local path leaves the maintainer's machine.
The result is finally loaded with photo_yolo._setup, the same check the
program runs, and its SHA-256 is printed for THIRD_PARTY_NOTICES.md.

@note Requires requirements/yolo-pt.txt (Ultralytics, onnx, onnxslim). Runs
    offline: YOLO_OFFLINE is set so that Ultralytics does not reach the
    network. The checkpoint is copied to a temporary folder first because
    Ultralytics writes next to the file it loads.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Export arguments shared by both heads: the detector's fixed contract.
_EXPORT_ARGUMENTS = {"format": "onnx", "imgsz": 640, "batch": 1, "dynamic": False,
                     "simplify": True, "opset": 17, "verbose": False}


def has_end_to_end_head(head) -> bool:
    """@brief True when the detection head carries a one-to-one branch.

    @param head Last module of the Ultralytics network.
    @return True for an end-to-end head whose one-to-one branch is present;
        False for a standard head and for a fused checkpoint that dropped
        the branch (`one2one_cv2` set to None), which can only export the
        raw grid.
    """
    return getattr(head, "one2one_cv2", None) is not None


def export_arguments(end_to_end: bool) -> dict:
    """@brief Ultralytics `export` keyword arguments for the chosen branch.

    @param end_to_end True to export the one-to-one detections [1, K, 6].
    @return The arguments of `YOLO.export`.
    @note In Ultralytics 8.4 `nms=False` ENABLES the end-to-end branch
        (`model.end2end = args.nms is False`) and `nms=None` exports the raw
        grid; `nms=True` would bake an NMS operator in, which the detector
        does not read.
    """
    return {**_EXPORT_ARGUMENTS, "nms": False if end_to_end else None}


def scrub_metadata(model, description: str) -> list:
    """@brief Replace metadata values that carry local paths.

    @param model An ONNX ModelProto (or any object with `metadata_props`
        entries having `key` and `value`).
    @param description The new `description` value.
    @return The keys that were rewritten.
    @note Only `description` is known to carry the training data path
        (`Ultralytics YOLO26l model trained on /train/.../data.yaml`); the
        other keys Ultralytics writes (names, task, imgsz, batch, stride,
        license, author, date, version, end2end) hold no path.
    """
    rewritten = []
    for prop in model.metadata_props:
        if prop.key == "description":
            prop.value = description
            rewritten.append(prop.key)
    return rewritten


def export_checkpoint(source: Path, target: Path) -> dict:
    """@brief Export one checkpoint, scrub its metadata and verify the result.

    @param source The .pt checkpoint.
    @param target The .onnx file to write.
    @return The detector's `info` of the exported file (photo_yolo._setup),
        with `end_to_end` and `sha256` added.
    @throws ValueError When the exported file does not satisfy the
        detector's contract, or when the branch read back is not the one
        exported (the result would not reproduce the checkpoint).
    """
    os.environ.setdefault("YOLO_OFFLINE", "1")
    import onnx
    from ultralytics import YOLO

    from kicadgen.services import photo_yolo

    with tempfile.TemporaryDirectory(prefix="kicadgen-onnx-") as folder:
        work = Path(folder) / source.name
        shutil.copy2(source, work)
        model = YOLO(str(work), task="detect")
        end_to_end = has_end_to_end_head(model.model.model[-1])
        exported = Path(model.export(**export_arguments(end_to_end)))
        proto = onnx.load(str(exported))
        scrub_metadata(proto, f"KiCadGen PCB component detector exported from {source.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        onnx.save(proto, str(target))
    info = photo_yolo._setup(target)[4]
    read_back = info["raw_head"].startswith("one-to-one")
    if read_back != end_to_end:
        raise ValueError(f"{target}: exported {'end-to-end' if end_to_end else 'raw'} output "
                         f"but the detector reads it as {info['raw_head']}")
    return {**info, "end_to_end": end_to_end,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def main(argv) -> int:
    """@brief Command-line entry point; return the process exit status.

    @param argv Command-line arguments after the program name.
    @return 0 on success, 1 when the checkpoint is missing, the target
        exists without `--force`, or the export fails verification.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("checkpoint", type=Path, help="the .pt checkpoint to export")
    parser.add_argument("--output", type=Path, default=None,
                        help="target .onnx (default: same name beside the checkpoint)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing target")
    args = parser.parse_args(argv)
    source = args.checkpoint
    if source.suffix.lower() != ".pt" or not source.is_file():
        print(f"Not a .pt checkpoint: {source}", file=sys.stderr)
        return 1
    target = args.output or source.with_suffix(".onnx")
    if target.exists() and not args.force:
        print(f"Target exists, use --force to overwrite: {target}", file=sys.stderr)
        return 1
    try:
        info = export_checkpoint(source, target)
    except Exception as exc:  # the reason is what the maintainer needs
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    print(f"{target} ({target.stat().st_size / 1_000_000:.1f} MB)")
    print(f"  head: {info['raw_head']}; model output {info['model_output_shape']}")
    print(f"  classes: {len(info['model_classes'])} ({info['class_order']})")
    print(f"  SHA-256: {info['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
