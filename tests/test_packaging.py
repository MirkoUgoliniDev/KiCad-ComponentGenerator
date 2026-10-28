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


def test_the_yolo_weights_ship_only_with_the_switch_and_every_onnx_goes():
    """@brief The weights enter only with -IncludeYoloModel; then EVERY models/yolo/*.onnx.

    Contract changed on 2026-10-28 (user: «mettiamo tutti»): after the license
    review the official setup ships the base model and the three ONNX
    exports, never a .pt (no PyTorch in the package). The switch stays so
    that a checkout without the weights still builds.
    """
    spec = (ROOT / "packaging" / "KiCadGen.spec").read_text(encoding="utf-8")
    assert 'for folder in ("yolo", "smd"):' in spec and '.glob("*.onnx")' in spec
    assert "pcb_yolov8s.onnx" not in spec, "no single file is privileged any more"
    assert ".pt" not in spec.split("KICADGEN_BUNDLE_YOLO_MODEL", 1)[1].split("binaries = []", 1)[0]
    assert 'os.environ.get("KICADGEN_BUNDLE_YOLO_MODEL") == "1"' in spec
    build = (ROOT / "scripts" / "build-windows.ps1").read_text(encoding="utf-8")
    assert "[switch]$IncludeYoloModel" in build
    assert "$env:KICADGEN_BUNDLE_YOLO_MODEL = '0'" in build
    assert "-Filter '*.onnx'" in build and "tools\\export_yolo_onnx.py" in build


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
