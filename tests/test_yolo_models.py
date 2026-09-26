"""@brief Facts about local YOLO weights: files, provenance by SHA-256 (2026-09-26)."""
import re
from pathlib import Path

from kicadgen.services import yolo_models as ym

NOTICES = Path(__file__).resolve().parents[1] / "THIRD_PARTY_NOTICES.md"


def _sections():
    """{sha256: text of its THIRD_PARTY_NOTICES section}."""
    text = NOTICES.read_text(encoding="utf-8")
    sections = {}
    for block in re.split(r"\n### ", text):
        match = re.search(r"SHA-256: `([0-9a-f]{64})`", block)
        if match:
            sections[match.group(1)] = block
    return sections


def test_every_known_model_says_what_the_notices_say():
    """@brief La scheda dei modelli e THIRD_PARTY_NOTICES.md non devono divergere."""
    sections = _sections()
    assert set(ym.KNOWN_MODELS) == set(sections)
    for sha, info in ym.KNOWN_MODELS.items():
        block = sections[sha]
        assert block.splitlines()[0].strip() == info.title
        assert info.license in block
        identifier = info.source.split("·")[-1].strip()
        assert identifier in block, (info.title, identifier)
        uncertain = ("to be verified" in block or "unresolved" in block.lower()
                     or "must be confirmed" in block)
        settled = (info.license_status == ym.STATUS_CONFIRMED
                   and info.dataset_status == ym.STATUS_CONFIRMED)
        # Un dubbio scritto nelle note deve arrivare anche nella finestra.
        assert settled != uncertain, info.title


def test_a_file_is_recognised_by_content_not_by_name(tmp_path):
    first = tmp_path / "a.onnx"
    first.write_bytes(b"pesi")
    renamed = tmp_path / "rinominato.onnx"
    renamed.write_bytes(b"pesi")
    edited = tmp_path / "a-modificato.onnx"
    edited.write_bytes(b"pesi!")
    assert ym.sha256_of(first) == ym.sha256_of(renamed) != ym.sha256_of(edited)
    assert ym.provenance_of("0" * 64) is None


def test_listing_finds_weights_only_sorted_and_adds_files_elsewhere(tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()
    for name in ("b.pt", "A.onnx", "note.txt", "c.PT"):
        (folder / name).write_bytes(b"x")
    elsewhere = tmp_path / "altrove.onnx"
    elsewhere.write_bytes(b"xy")
    names = [item.path.name for item in ym.list_models(folder, extra=[elsewhere, folder / "b.pt"])]
    assert names == ["A.onnx", "altrove.onnx", "b.pt", "c.PT"]


def test_a_missing_folder_or_file_is_not_an_error(tmp_path):
    assert ym.list_models(tmp_path / "manca") == []
    missing = ym.describe_file(tmp_path / "manca.pt")
    assert missing.size_bytes == 0 and missing.modified is None
    assert missing.format == "PyTorch" and missing.runs_code_when_loaded
    assert not ym.describe_file(tmp_path / "x.onnx").runs_code_when_loaded


def test_the_real_files_in_models_are_recognised_when_present():
    """@brief I file del repository, se ci sono, hanno la loro provenienza."""
    folder = Path(__file__).resolve().parents[1] / "models"
    for item in ym.list_models(folder):
        info = ym.provenance_of(ym.sha256_of(item.path))
        assert info is not None, item.path.name
