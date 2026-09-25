"""@brief Installer fixes C1-C14 (2026-09-25): WiX source, seed, licenses, workflow, smoke test.

Static checks of what no normal run exercises (PyInstaller, WiX, GitHub
Actions), plus functional tests of the build tools and of --check-runtime.
Each behaviour has a positive and a negative case.
"""
import importlib.util
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WIX = "{http://schemas.microsoft.com/wix/2006/wi}"


def _load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def wxs():
    return ET.parse(ROOT / "packaging" / "KiCadGen.wxs").getroot().find(f"{WIX}Product")


# -- C1: «Launch KiCadGen» uses the folder chosen in the dialog ---------------

def test_the_launch_target_is_set_by_the_finish_button_before_the_launch(wxs):
    ui = wxs.find(f"{WIX}UI")
    publishes = [p for p in ui.findall(f"{WIX}Publish")
                 if p.get("Dialog") == "ExitDialog" and p.get("Control") == "Finish"]
    target = [p for p in publishes if p.get("Property") == "WixShellExecTarget"]
    launch = [p for p in publishes if p.get("Event") == "DoAction" and p.get("Value") == "LaunchKiCadGen"]
    assert len(target) == 1 and len(launch) == 1
    assert target[0].get("Value") == "[INSTALLFOLDER]KiCadGen.exe"
    assert int(target[0].get("Order")) < int(launch[0].get("Order"))


def test_no_sequenced_action_computes_the_launch_target_too_early(wxs):
    """Negative: a SetProperty in a sequence runs before InstallDirDlg."""
    early = [s for s in wxs.iter(f"{WIX}SetProperty") if s.get("Id") == "WixShellExecTarget"]
    assert early == []


# -- C3: same-version MSI replaces the installed one ---------------------------

def test_a_same_version_msi_replaces_the_installed_one(wxs):
    upgrade = wxs.find(f"{WIX}MajorUpgrade")
    assert wxs.get("Id") == "*" and upgrade.get("AllowSameVersionUpgrades") == "yes"
    assert upgrade.get("DowngradeErrorMessage")  # older versions still refused
    msi = (ROOT / "scripts" / "build-msi.ps1").read_text(encoding="utf-8")
    assert "-sice:ICE61" in msi


# -- C8/C9: shortcuts comment and remembered install folder -------------------

def test_the_install_folder_is_written_and_read_back_from_hklm(wxs):
    remembered = [p for p in wxs.findall(f"{WIX}Property") if p.get("Id") == "REMEMBEREDINSTALLFOLDER"]
    search = remembered[0].find(f"{WIX}RegistrySearch")
    assert (search.get("Root"), search.get("Key"), search.get("Name")) == ("HKLM", "Software\\KiCadGen", "InstallFolder")
    values = [v for v in wxs.iter(f"{WIX}RegistryValue") if v.get("Name") == "InstallFolder"]
    assert len(values) == 1 and values[0].get("Root") == "HKLM"
    assert values[0].get("Value") == "[INSTALLFOLDER]" and values[0].get("KeyPath") == "yes"
    feature_refs = {r.get("Id") for r in wxs.find(f"{WIX}Feature").findall(f"{WIX}ComponentRef")}
    assert "InstallFolderRegistry" in feature_refs
    set_dir = [d for d in wxs.findall(f"{WIX}SetDirectory") if d.get("Id") == "INSTALLFOLDER"]
    assert set_dir and set_dir[0].get("Value") == "[REMEMBEREDINSTALLFOLDER]"
    # Negative: an explicit INSTALLFOLDER on the command line keeps priority.
    assert "NOT CMDLINEINSTALLFOLDER" in set_dir[0].text


def test_the_hkcu_shortcut_key_paths_are_explained():
    text = (ROOT / "packaging" / "KiCadGen.wxs").read_text(encoding="utf-8")
    assert "ICE38" in text and "ICE43" in text and "leftover" in text.lower()


# -- C14: installer search aligned with the program's detection ---------------

def test_the_installer_looks_for_what_the_program_uses(wxs):
    searches = {p.get("Id"): [f.get("Name") for f in p.iter(f"{WIX}FileSearch")]
                for p in wxs.findall(f"{WIX}Property") if p.get("Id") in ("FREECADCMD", "KICADEXE")}
    assert searches["KICADEXE"] == ["kicad-cli.exe", "kicad-cli.exe"]  # not kicad.exe
    assert searches["FREECADCMD"] == ["freecadcmd.exe", "freecadcmd.exe"]
    roots = {d.get("Path") for d in wxs.iter(f"{WIX}DirectorySearch")}
    assert {"[ProgramFilesFolder]", "[ProgramFilesFolder]KiCad"} <= roots  # (x86) too
    text = (ROOT / "packaging" / "KiCadGen.wxs").read_text(encoding="utf-8")
    assert "may not have found" in text and "Not found on this computer" not in text


# -- C2: seed generated from the code -------------------------------------------

def test_the_generated_seed_is_clean_and_matches_the_code(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    tool = _load("tools/make_seed_db.py", "make_seed_db")
    seed = tool.make_seed(tmp_path / "seed" / "analysis_profiles.sqlite3")
    from kicadgen.services.analysis_profiles import factory_defaults
    connection = sqlite3.connect(seed)
    try:
        metadata = dict(connection.execute("SELECT name, value FROM metadata"))
        defaults = dict(connection.execute("SELECT name, value FROM ai_defaults"))
    finally:
        connection.close()
    assert metadata.get("legacy_backup") == ""
    assert defaults["system_prompt"] == factory_defaults()["system_prompt"]


def test_a_seed_with_a_developer_path_or_stale_prompt_is_rejected(tmp_path, monkeypatch):
    """Negative: exactly the two defects of the old git-HEAD seed."""
    monkeypatch.syspath_prepend(str(ROOT))
    tool = _load("tools/make_seed_db.py", "make_seed_db")
    seed = tool.make_seed(tmp_path / "a.sqlite3")
    connection = sqlite3.connect(seed)
    with connection:
        connection.execute("UPDATE metadata SET value=? WHERE name='legacy_backup'",
                           (r"C:\Users\dev\config-before-sqlite.json",))
    connection.close()
    with pytest.raises(tool.SeedError, match="legacy_backup"):
        tool.verify_seed(seed)
    seed = tool.make_seed(tmp_path / "b.sqlite3")
    connection = sqlite3.connect(seed)
    with connection:
        connection.execute("UPDATE ai_defaults SET value='old prompt' WHERE name='grounding_prompt'")
    connection.close()
    with pytest.raises(tool.SeedError, match="grounding_prompt"):
        tool.verify_seed(seed)


# -- C4: licenses collected into the bundle -------------------------------------

def test_the_licenses_of_the_environment_and_of_cpython_are_collected(tmp_path):
    tool = _load("tools/collect_licenses.py", "collect_licenses")
    tool.collect(tmp_path / "licenses")
    out = tmp_path / "licenses"
    assert list(out.glob("Python-*/LICENSE.txt"))
    manifest = (out / "MANIFEST.txt").read_text(encoding="utf-8")
    for name in ("PyQt6 |", "PyQt6-Qt6 |", "opencv-python |", "OpenSSL |", "SQLite |"):
        assert name in manifest, name
    assert list(out.glob("opencv*/cv2/LICENSE-3RD-PARTY.txt"))  # FFmpeg's LGPL text
    assert list(out.glob("PyQt6-Qt6-*/**/LICENSE"))


def test_a_missing_cpython_license_stops_the_collection(tmp_path):
    """Negative: the interpreter always ships, so must its license."""
    tool = _load("tools/collect_licenses.py", "collect_licenses")
    with pytest.raises(tool.LicenseCollectionError):
        tool.collect(tmp_path / "licenses", distributions=[], base_prefix=tmp_path / "no-python")


def test_a_distribution_without_license_files_is_reported(tmp_path):
    tool = _load("tools/collect_licenses.py", "collect_licenses")

    class Bare:
        version = "1.0"
        metadata = {"Name": "bare", "License-Expression": "MIT"}
        files = []

        def locate_file(self, path):
            return tmp_path / path

    missing = tool.collect(tmp_path / "licenses", distributions=[Bare()])
    assert missing == ["bare"]
    assert "bare | 1.0 | MIT | NONE" in (tmp_path / "licenses" / "MANIFEST.txt").read_text(encoding="utf-8")


def test_the_spec_ships_the_collected_licenses_and_the_build_collects_them():
    spec = (ROOT / "packaging" / "KiCadGen.spec").read_text(encoding="utf-8")
    assert '(str(LICENSES), "licenses")' in spec
    build = (ROOT / "scripts" / "build-windows.ps1").read_text(encoding="utf-8")
    assert "tools\\collect_licenses.py" in build


def test_the_notices_list_the_bundled_runtime_and_the_gpl_sources():
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for needle in ("CPython", "PSF-2.0", "Qt 6", "LGPL-3.0", "PyQt6-sip", "FFmpeg",
                   "opencv_videoio_ffmpeg", "OpenSSL", "SQLite", "libffi",
                   "Source code of GPL/LGPL components", "compdetect-dnwth-2-yolo26l-t1"):
        assert needle in notices, needle
    # The Large model's Roboflow license is still unverified: keep saying so.
    large = notices.split("### Roboflow YOLO26 Large", 1)[1].split("###", 1)[0]
    assert "to be verified" in large


def test_release_builds_use_pinned_versions():
    constraints = (ROOT / "requirements-release-constraints.txt").read_text(encoding="utf-8")
    pins = dict(line.split("==") for line in constraints.splitlines()
                if line and not line.startswith("#"))
    for package in ("PyQt6", "PyQt6-Qt6", "opencv-python", "numpy", "onnxruntime", "vtk", "pyinstaller"):
        assert package in pins, package
    assert "torch" not in pins and "ultralytics" not in pins  # never in the installer
    build = (ROOT / "scripts" / "build-windows.ps1").read_text(encoding="utf-8")
    assert "requirements-release-constraints.txt" in build
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "-c requirements-release-constraints.txt" in workflow


# -- C5: release workflow permissions and tag handling --------------------------

def test_the_build_job_is_read_only_and_only_publish_can_write():
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    assert workflow["permissions"] == {}
    build, publish = workflow["jobs"]["build"], workflow["jobs"]["publish"]
    assert build["permissions"] == {"contents": "read"}
    checkout = [s for s in build["steps"] if str(s.get("uses", "")).startswith("actions/checkout")]
    assert checkout and checkout[0]["with"]["persist-credentials"] is False
    assert publish["permissions"] == {"contents": "write"} and publish["needs"] == "build"
    # Negative: the job with write access never checks out or runs project code.
    assert not any("checkout" in str(s.get("uses", "")) for s in publish["steps"])
    assert not any("scripts/" in str(s.get("run", "")) for s in publish["steps"])


def test_the_tag_is_never_interpolated_into_a_script():
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in str(step.get("run", "")), step.get("name")
    assert workflow["jobs"]["build"]["env"]["TAG"] == "${{ github.ref_name }}"


# -- C13: WiX download verified, build scripts tidy -----------------------------

def test_the_wix_download_is_verified_and_fails_closed():
    msi = (ROOT / "scripts" / "build-msi.ps1").read_text(encoding="utf-8")
    assert "Get-FileHash -Algorithm SHA256" in msi
    assert "KICADGEN_WIX_SHA256" in msi and "[string]$WixSha256" in msi
    # The hash check precedes the download; no hash is invented in the script.
    assert msi.index("'^[0-9A-F]{64}$'") < msi.index("Invoke-WebRequest")
    assert msi.index("Get-FileHash") < msi.index("Expand-Archive")


def test_build_windows_restores_the_location_and_rebuilds_a_wrong_venv():
    build = (ROOT / "scripts" / "build-windows.ps1").read_text(encoding="utf-8")
    assert "Push-Location $Root" in build and "Pop-Location" in build
    assert "Set-Location" not in build
    assert build.rindex("finally {") > build.index("build-msi.ps1') -Version")
    assert "Test-BuildPython" in build and "Remove-Item -Recurse -Force $venv" in build


# -- C7: --check-runtime proves the package, not only the libraries -------------

def test_check_runtime_passes_from_the_sources(capsys):
    main = _load("main.py", "kicadgen_main_under_test")
    assert "kicadgen.gui.main_window" in main.RUNTIME_MODULES
    assert main.check_runtime() == 0
    assert "Runtime check OK." in capsys.readouterr().out


def test_check_runtime_fails_and_names_what_is_missing(tmp_path, capsys):
    """Negative: an empty package folder and an unimportable module."""
    main = _load("main.py", "kicadgen_main_under_test")
    result = main.check_runtime(resource_path=lambda *parts: tmp_path.joinpath(*parts),
                                modules=("json", "kicadgen_module_that_does_not_exist"))
    assert result == 1
    err = capsys.readouterr().err
    assert "kicadgen_module_that_does_not_exist" in err
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "data/analysis_profiles.sqlite3",
                 "kicadgen/cad3d/freecad_build_body.py", "kicadgen/gui/assets/app-icon.svg"):
        assert name in err, name


def test_check_runtime_requires_the_license_manifest_only_in_the_package(tmp_path, monkeypatch):
    main = _load("main.py", "kicadgen_main_under_test")
    for parts in main.RUNTIME_RESOURCES:
        path = tmp_path.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    resolve = lambda *parts: tmp_path.joinpath(*parts)  # noqa: E731
    assert main.check_runtime(resource_path=resolve, modules=()) == 0
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert main.check_runtime(resource_path=resolve, modules=()) == 1
    (tmp_path / "licenses").mkdir()
    (tmp_path / "licenses" / "MANIFEST.txt").write_text("x")
    assert main.check_runtime(resource_path=resolve, modules=()) == 0


def test_check_runtime_never_reads_dotenv():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    body = source.split("def check_runtime", 1)[1].split("\ndef ", 1)[0]
    assert "dotenv" not in body and "env_files" not in body


def test_check_runtime_exit_code_from_the_command_line(tmp_path):
    """End to end through main(): the build's smoke test sees the exit code."""
    result = subprocess.run([sys.executable, str(ROOT / "main.py"), "--check-runtime"],
                            capture_output=True, text=True, timeout=300, cwd=ROOT,
                            env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"})
    assert result.returncode == 0, result.stdout + result.stderr
