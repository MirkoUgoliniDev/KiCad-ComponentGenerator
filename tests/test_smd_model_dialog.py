"""@brief «Scegli la rete SMD» (user, 2026-10-02): the checkpoints with what each declares."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import shutil

import pytest
pytest.importorskip("PyQt6")
pytest.importorskip("torch")
from PyQt6.QtWidgets import QDialogButtonBox

from kicadgen.app_paths import smd_model_path
from kicadgen.gui.smd_model_dialog import SmdModelDialog
from kicadgen.services.smd_component_classifier import describe_smd_checkpoint


def test_the_real_checkpoint_declares_its_metadata():
    if not smd_model_path().is_file():
        pytest.skip("SMD checkpoint not installed")
    info = describe_smd_checkpoint()
    assert info["arch"] and info["classes"] and info["threshold"] >= 0.5
    assert set(info["orientations"]) == set(info["classes"])


def test_the_window_lists_the_folder_and_shows_the_checkpoint(qapp, tmp_path):
    """@brief Positive: the real network; negative: a broken file cannot be chosen."""
    if not smd_model_path().is_file():
        pytest.skip("SMD checkpoint not installed")
    folder = tmp_path / "smd"
    folder.mkdir()
    good = folder / "smd_classifier.pt"
    shutil.copyfile(smd_model_path(), good)
    broken = folder / "broken.pt"
    broken.write_bytes(b"not a checkpoint")
    dialog = SmdModelDialog("", folder, default=str(good))
    try:
        names = [dialog.model_list.item(i).text().split("\n")[0]
                 for i in range(dialog.model_list.count())]
        assert names == ["broken.pt", "smd_classifier.pt  (in uso, predefinito)"]
        html = dialog.details_html(str(good), "abc", dict(describe_smd_checkpoint(good), ok=True))
        assert "resnet18" in html and "Soglia di accettazione" in html and "Validazione" in html
        bad = dialog.details_html(str(broken), "", {"ok": False, "error": "illeggibile"})
        assert "Non utilizzabile" in bad
        # A rejected file cannot be confirmed.
        dialog.model_list.setCurrentRow(0)
        from kicadgen.gui.yolo_model_dialog import _remember
        _remember(str(broken), "smd_check", {"ok": False, "error": "illeggibile"})
        dialog._show_selected()
        assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    finally:
        dialog.shutdown()
        dialog.close()


def test_few_validation_samples_are_said(qapp, tmp_path):
    dialog = SmdModelDialog("", tmp_path)
    try:
        check = {"ok": True, "format": "f", "arch": "a", "img_size": 224, "trained": "",
                 "epochs": 1, "classes": ["chip_R"], "orientations": {"chip_R": "axis"},
                 "threshold": 0.5, "validation": {"samples": 42, "accuracy": 1.0}}
        assert "Pochi campioni" in dialog.details_html("x.pt", "", check)
        check["validation"]["samples"] = 5000
        assert "Pochi campioni" not in dialog.details_html("x.pt", "", check)
    finally:
        dialog.shutdown()
        dialog.close()
