"""@brief Export the SMD network checkpoint to the .onnx the installed program reads.

Usage: python tools/export_smd_onnx.py [models/smd/smd_classifier.pt] [--output <file>.onnx] [--force]

The Windows installer has no PyTorch: the SMD network (a ResNet18 with a
class head and a rotation head, see smd_component_classifier.PackageNet)
is shipped as an ONNX graph that takes the normalised crop batch
[N, 3, 224, 224] and returns the two heads' logits. The checkpoint's
metadata that validation, inference and the model window read
(CHECKPOINT_METADATA_KEYS) travel in the file as JSON under the
`smd_checkpoint` key; the training command (`args`, with the maintainer's
local path) is left out on purpose.

The result is loaded back with smd_component_classifier.validate_smd_setup,
the same check the program runs, and compared with the checkpoint on a
random batch: both heads must agree within float tolerance.

@note Requires requirements/smd.txt (PyTorch, torchvision) and the `onnx`
    package. The checkpoint is loaded with weights_only=True.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Largest logit difference tolerated between the checkpoint and its export
#: on the same batch (float32 graphs computed by two runtimes).
TOLERANCE = 1e-3


def metadata_json(checkpoint) -> str:
    """@brief The checkpoint fields the ONNX file carries, as JSON.

    @param checkpoint The mapping loaded from the .pt file.
    @return JSON text of the keys in CHECKPOINT_METADATA_KEYS that the
        checkpoint has; `args` and `state_dict` never appear.
    @throws ValueError When a value is not JSON-serialisable.
    """
    from kicadgen.services.smd_component_classifier import CHECKPOINT_METADATA_KEYS
    kept = {key: checkpoint[key] for key in CHECKPOINT_METADATA_KEYS if key in checkpoint}
    try:
        return json.dumps(kept, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"checkpoint metadata not serialisable: {exc}") from exc


def export_checkpoint(source: Path, target: Path) -> dict:
    """@brief Export the checkpoint, write the metadata, verify the result.

    @param source The .pt checkpoint.
    @param target The .onnx file to write.
    @return Dictionary: `classes` (count), `sha256` of the target, and
        `max_difference` between the two runtimes' logits on a random batch.
    @throws ValueError When the export does not pass validate_smd_setup or
        differs from the checkpoint beyond TOLERANCE.
    """
    import numpy as np
    import onnx
    import torch

    from kicadgen.services import smd_component_classifier as smd

    checkpoint = smd._load_checkpoint(source, torch)
    torch_module, _, PackageNet = smd._runtime()
    smd._validate_checkpoint(checkpoint, source, torch_module, PackageNet)
    net = PackageNet(len(checkpoint["classes"]))
    net.load_state_dict(checkpoint["state_dict"])
    net.eval()
    example = torch.zeros(1, 3, smd._IMG_SIZE, smd._IMG_SIZE)
    target.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        net, (example,), str(target), input_names=[smd.ONNX_INPUT],
        output_names=list(smd.ONNX_OUTPUTS), opset_version=17, dynamo=False,
        dynamic_axes={smd.ONNX_INPUT: {0: "batch"}, smd.ONNX_OUTPUTS[0]: {0: "batch"},
                      smd.ONNX_OUTPUTS[1]: {0: "batch"}})
    proto = onnx.load(str(target))
    del proto.metadata_props[:]
    entry = proto.metadata_props.add()
    entry.key, entry.value = smd.ONNX_METADATA_KEY, metadata_json(checkpoint)
    proto.producer_name = "KiCadGen tools/export_smd_onnx.py"
    proto.doc_string = f"SMD package classifier exported from {source.name}"
    onnx.save(proto, str(target))

    smd.validate_smd_setup(target)
    _, run_onnx = smd._open_network(target)
    _, run_torch = smd._open_network(source, device="cpu")
    batch = np.random.default_rng(7).standard_normal((3, 3, smd._IMG_SIZE, smd._IMG_SIZE)).astype(np.float32)
    differences = [float(np.abs(a - b).max()) for a, b in zip(run_torch(batch), run_onnx(batch))]
    worst = max(differences)
    if worst > TOLERANCE:
        raise ValueError(f"{target}: the export differs from the checkpoint by {worst:.5f} "
                         f"(tolerance {TOLERANCE})")
    return {"classes": len(checkpoint["classes"]), "max_difference": worst,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


def main(argv) -> int:
    """@brief Command-line entry point; return the process exit status.

    @param argv Command-line arguments after the program name.
    @return 0 on success, 1 when the checkpoint is missing, the target
        exists without `--force`, or the export fails verification.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("checkpoint", type=Path, nargs="?",
                        default=ROOT / "models" / "smd" / "smd_classifier.pt")
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
    print(f"  classes: {info['classes']}; max logit difference vs checkpoint: {info['max_difference']:.6f}")
    print(f"  SHA-256: {info['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
