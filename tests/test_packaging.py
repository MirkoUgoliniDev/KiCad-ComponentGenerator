"""@brief Controlli statici del packaging Windows (eseguibile e MSI, 2026-09-25).

La build vera (PyInstaller, WiX) non gira nella suite: qui si verifica cio'
che la farebbe fallire, o peggio riuscire sbagliata, senza che nessun altro
test se ne accorga. Modello: la skill installer-change di employee-scheduling
(«l'area che nessun compilatore controlla»).
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPGRADE_CODE = "E97182CB-C126-4B0C-B336-172FE2830FB7"


def test_the_version_has_three_numbers_as_the_msi_requires():
    from kicadgen import __version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_the_upgrade_code_never_changes():
    """Cambiarlo farebbe installare una seconda copia invece di aggiornare."""
    wxs = (ROOT / "packaging" / "KiCadGen.wxs").read_text(encoding="utf-8")
    assert f'UpgradeCode="{UPGRADE_CODE}"' in wxs
    assert 'InstallScope="perMachine"' in wxs and "<MajorUpgrade" in wxs


def test_every_resource_the_spec_ships_exists():
    """Le sorgenti dei dati del pacchetto (tranne il seme, preparato dalla build)."""
    spec = (ROOT / "packaging" / "KiCadGen.spec").read_text(encoding="utf-8")
    for relative in ("kicadgen/gui/assets", "kicadgen/cad3d", ".env.example",
                     "packaging/KiCadGen.ico", "main.py"):
        assert (ROOT / relative).exists(), relative
    assert '"kicadgen" / "cad3d" / "*.py"' in spec  # script FreeCAD come file veri
    assert (ROOT / "kicadgen" / "cad3d" / "freecad_build_body.py").is_file()


def test_the_yolo_weights_ship_only_with_the_switch_and_exactly_the_manifest():
    """@brief The weights enter only with -IncludeYoloModel; then exactly packaging/model-weights.json.

    Contract changed on 2026-10-28 (user: «mettiamo tutti»): after the license
    review the official setup ships the base model and the three ONNX
    exports, never a .pt (no PyTorch in the package). The switch stays so
    that a checkout without the weights still builds.

    CONTRACT CHANGED 2026-10-29 (review K1): «mettiamo tutti» meant the known
    detectors of the manifest, not whatever .onnx lies in models/. The spec
    no longer globs: it ships the files `verified_weights` returns (size and
    SHA-256 checked, a missing or different file stops the build), and
    build-windows.ps1 runs `fetch_model_weights.py --check` before
    PyInstaller. The behaviour itself is proved on files in
    tests/test_fetch_model_weights.py (verified_weights tests).
    """
    spec = (ROOT / "packaging" / "KiCadGen.spec").read_text(encoding="utf-8")
    block = spec.split('os.environ.get("KICADGEN_BUNDLE_YOLO_MODEL") == "1"', 1)[1].split("binaries = []", 1)[0]
    assert '"tools" / "fetch_model_weights.py"' in block
    assert "verified_weights(" in block and '"packaging" / "model-weights.json"' in block
    # Negative: no glob of the models folders, no .pt, no privileged file name.
    assert ".glob(" not in block and ".pt" not in block and "pcb_yolov8s.onnx" not in spec
    build = (ROOT / "scripts" / "build-windows.ps1").read_text(encoding="utf-8")
    assert "[switch]$IncludeYoloModel" in build
    assert "$env:KICADGEN_BUNDLE_YOLO_MODEL = '0'" in build
    switch_block = build.split("if ($IncludeYoloModel) {", 1)[1].split("} else {", 1)[0]
    assert "tools\\fetch_model_weights.py') --check" in switch_block
    assert "Assert-LastCommand" in switch_block
    assert "Get-ChildItem" not in switch_block and "*.onnx" not in switch_block


def _spec_weights_block():
    """Source of the spec's weights block, to run it outside PyInstaller."""
    spec = (ROOT / "packaging" / "KiCadGen.spec").read_text(encoding="utf-8")
    start = spec.index('if os.environ.get("KICADGEN_BUNDLE_YOLO_MODEL") == "1":')
    return spec[start:spec.index("binaries = []", start)]


def _manifest_copy(tmp_path, payloads):
    """A fake repository root with a manifest recording `payloads` ({(dir, name): bytes})."""
    import hashlib
    import json
    (tmp_path / "packaging").mkdir()
    (tmp_path / "tools").mkdir()
    shutil.copy(ROOT / "tools" / "fetch_model_weights.py", tmp_path / "tools")
    weights = [{"file": name, "dir": folder, "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "url": f"https://x/{name}"}
               for (folder, name), data in payloads.items()]
    (tmp_path / "packaging" / "model-weights.json").write_text(json.dumps({"weights": weights}),
                                                              encoding="utf-8")
    return tmp_path


def _run_spec_block(root, monkeypatch, switch="1"):
    """Execute the spec's weights block against `root`; return its `datas`."""
    import os
    monkeypatch.setenv("KICADGEN_BUNDLE_YOLO_MODEL", switch)
    namespace = {"os": os, "ROOT": root, "datas": []}
    exec(compile(_spec_weights_block(), "KiCadGen.spec", "exec"), namespace)
    return namespace["datas"]


def test_the_spec_ships_the_manifest_files_and_not_an_unknown_onnx(tmp_path, monkeypatch):
    """@brief Positive: manifest files with the right digest ship; an extra .onnx in the folder does not (K1)."""
    root = _manifest_copy(tmp_path, {("yolo", "a.onnx"): b"aaa", ("smd", "s.onnx"): b"ss"})
    for folder, name, data in (("yolo", "a.onnx", b"aaa"), ("smd", "s.onnx", b"ss"),
                               ("yolo", "unknown_export.onnx", b"zz"), ("yolo", "a.pt", b"pt")):
        (root / "models" / folder).mkdir(parents=True, exist_ok=True)
        (root / "models" / folder / name).write_bytes(data)
    datas = _run_spec_block(root, monkeypatch)
    assert datas == [(str(root / "models" / "yolo" / "a.onnx"), "models/yolo"),
                     (str(root / "models" / "smd" / "s.onnx"), "models/smd")]
    # Negative: without the switch nothing is even read.
    assert _run_spec_block(root, monkeypatch, switch="0") == []


def test_the_spec_refuses_a_weights_file_with_another_digest_or_missing(tmp_path, monkeypatch):
    """@brief Negative: a same-size file with another SHA-256, or a missing one, stops the build (K1)."""
    root = _manifest_copy(tmp_path, {("yolo", "a.onnx"): b"aaa"})
    (root / "models" / "yolo").mkdir(parents=True)
    with pytest.raises(ValueError, match="missing"):
        _run_spec_block(root, monkeypatch)
    (root / "models" / "yolo" / "a.onnx").write_bytes(b"aab")
    with pytest.raises(ValueError, match="SHA-256 differs"):
        _run_spec_block(root, monkeypatch)


def test_the_green_run_message_is_printed_before_the_gate_returns():
    """@brief Review 2026-10-29, K6: the success line is reachable and names the green run.

    Negative: no statement follows the final throw, and the undefined `$run` is gone.
    """
    script = (ROOT / "scripts" / "publish-release.ps1").read_text(encoding="utf-8")
    gate = script.split("function Assert-TestsGreenOnGitHub", 1)[1].split("\nfunction ", 1)[0]
    green = gate.split("if ($green.Count -gt 0) {", 1)[1].split("}", 1)[0]
    assert "$($green[0].url)" in green and green.rstrip().endswith("return")
    assert "$run." not in gate
    tail = gate.rsplit("throw ", 1)[1].splitlines()[1:]
    assert [line.strip() for line in tail if line.strip()] == ["}"]


def test_the_branch_is_pushed_before_the_tag_is_created():
    """@brief Review 2026-10-29, K7: a refused branch push leaves no local tag behind.

    Positive: push branch, then create the tag, then push it. Negative: the
    tag is still annotated and still pushed (the release workflow needs it).
    """
    script = (ROOT / "scripts" / "publish-release.ps1").read_text(encoding="utf-8")
    push_branch = script.index("& git push origin $branch")
    create_tag = script.index('& git tag -a $tag -m "KiCadGen $tag"')
    push_tag = script.index("& git push origin $tag")
    assert push_branch < create_tag < push_tag
    assert script.count("& git tag -a") == 1


def test_the_seed_database_is_generated_from_the_code_not_copied():
    """Contract changed (fix C2): git HEAD held stale prompts and a developer
    path in metadata.legacy_backup; the seed is now built by make_seed_db."""
    build = (ROOT / "scripts" / "build-windows.ps1").read_text(encoding="utf-8")
    assert "tools\\make_seed_db.py" in build
    assert "build\\seed\\data\\analysis_profiles.sqlite3" in build
    assert "git','show'" not in build and "HEAD:data/" not in build
    spec = (ROOT / "packaging" / "KiCadGen.spec").read_text(encoding="utf-8")
    assert '(str(SEED / "data" / "analysis_profiles.sqlite3"), "data")' in spec
    assert '(str(ROOT / "data"' not in spec  # never the repository copy


def test_build_outputs_and_environment_are_ignored_by_git():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in (".venv-build/", "dist/", "build/", "Claude outputs/"):
        assert entry in ignored, entry


#: Every PowerShell script of the repository, read from the folder so that a new
#: script is parsed too without anyone adding it to a list.
POWERSHELL_SCRIPTS = sorted(path.name for path in (ROOT / "scripts").glob("*.ps1"))


def test_the_known_powershell_scripts_are_found():
    """Negative of the parse test: an empty glob would make it pass vacuously."""
    assert {"build-windows.ps1", "build-msi.ps1", "publish-release.ps1",
            "configure-github-repo.ps1"} <= set(POWERSHELL_SCRIPTS)


@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell non disponibile")
@pytest.mark.parametrize("script", POWERSHELL_SCRIPTS)
def test_the_powershell_scripts_parse(script):
    """Come la skill installer-change: il parser di PowerShell, senza eseguire nulla."""
    path = ROOT / "scripts" / script
    command = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{path}',[ref]$null,[ref]$e); exit $e.Count")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_application_icon_renders(qapp, tmp_path):
    pytest.importorskip("PyQt6.QtSvg")
    from PyQt6.QtSvg import QSvgRenderer
    assert QSvgRenderer(str(ROOT / "kicadgen" / "gui" / "assets" / "app-icon.svg")).isValid()
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "make_app_icon.py"),
                             str(tmp_path / "icon.ico")], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "icon.ico").stat().st_size > 1000


def test_the_licenses_ship_with_the_program_and_the_code_is_agpl():
    """Utente, 2026-09-25: codice AGPL-3.0; licenza e avvisi nel menu «?» anche installato."""
    spec = (ROOT / "packaging" / "KiCadGen.spec").read_text(encoding="utf-8")
    assert '(str(ROOT / "LICENSE"), ".")' in spec
    assert '(str(ROOT / "THIRD_PARTY_NOTICES.md"), ".")' in spec
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.lstrip().startswith("GNU AFFERO GENERAL PUBLIC LICENSE")
    assert "Version 3, 19 November 2007" in license_text
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    # Ogni peso usato nello sviluppo e il dataset hanno la loro voce.
    for name in ("pcb_component_detector_yolo26_roboflow_m.pt",
                 "pcb_component_detector_yolo26_roboflow_l.pt",
                 "pcb_component_detector_yolov8m_best.pt", "pcb_yolov8s.onnx",
                 "universe.roboflow.com/dataset-lmrsw/compdetect", "CC BY 4.0"):
        assert name in notices
