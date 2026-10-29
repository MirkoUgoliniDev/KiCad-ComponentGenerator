"""@brief CI review of 2026-10-27: the suite must pass on a machine without KiCad, FreeCAD or weights.

Findings I1, I2, I11 (environment of the GitHub runner), I4 (release gate),
I5 (third-party notices and pypdf), I8/I9 (crash report secrets) and I10
(seed database verification). Every behaviour has a positive and a negative
case; nothing here needs KiCad, FreeCAD, the model weights or the network.
"""
import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from kicadgen import crash_report
from kicadgen.generators import symbol_glyphs as g

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_LIBRARY = ROOT / "tests" / "data" / "glyph_fixture.kicad_sym"


def _load_tool(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- I1: the window fixture does not depend on an installed FreeCAD -------------

def test_i1_the_window_fixture_starts_with_freecad_available_whatever_is_installed(window):
    """Positive: the shared fixture fakes the FreeCAD probe, so the STEP group starts on."""
    assert window._freecad_available is True
    assert window.group_3d.isChecked() is True


def test_i1_a_window_whose_probe_fails_starts_without_freecad(qapp, monkeypatch, tmp_path):
    """Negative: the real rule is untouched, a failing probe still means «FreeCAD missing»."""
    pytest.importorskip("pyvistaqt")
    import qt_fixtures
    from PyQt6.QtWidgets import QMessageBox
    from kicadgen.gui import main_window as gui

    def missing():
        raise gui.Cad3DError("freecadcmd not found (test)")

    monkeypatch.setattr(gui, "find_freecadcmd", missing)
    monkeypatch.setattr(gui, "Preview3DVtk", qt_fixtures.PreviewStub)
    monkeypatch.setattr(gui, "load_config", lambda: {"component_type": "", "system_prompt": ""})
    monkeypatch.setattr(gui, "save_config", lambda config: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(gui, "DEFAULT_LIBRARY_ROOT", tmp_path)
    w = gui.MainWindow()
    try:
        assert w._freecad_available is False
        assert w.group_3d.isChecked() is False
    finally:
        qt_fixtures.shutdown_main_window(w, qapp)


# -- I2: glyph geometry from the suite's own library ----------------------------

def test_i2_the_fixture_library_is_hand_written_not_a_copy_of_kicad():
    """The header names the suite as generator: a KiCad library (CC-BY-SA) would say kicad_symbol_editor."""
    head = FIXTURE_LIBRARY.read_text(encoding="utf-8")[:300]
    assert re.search(r'\(generator\s+"kicadgen_test_fixture"\)', head)
    assert "kicad_symbol_editor" not in head


def test_i2_the_fixture_catalogue_has_the_geometry_the_editor_tests_rely_on(glyph_library):
    """Positive: resistor pins at 3.81 mm, three-terminal potentiometer, two-terminal crystal, a body hint."""
    by_key = {glyph.key: glyph for glyph in g.catalogue()}
    assert sorted(abs(t.y) for t in by_key["resistor"].terminals) == [3.81, 3.81]
    assert len(by_key["potentiometer"].terminals) == 3
    assert len(by_key["crystal"].terminals) == 2
    hinted = [glyph for glyph in g.catalogue() if glyph.body_hint is not None]
    assert hinted and hinted[0].body_hint == (20.32, 10.16)
    assert all(glyph.source.startswith("kicad:") for glyph in by_key.values()
               if glyph.key in ("resistor", "potentiometer", "crystal"))
    # The library folder holds one copy per library name the catalogue reads.
    assert {path.name for path in glyph_library.glob("*.kicad_sym")} >= {"Device.kicad_sym", "Relay.kicad_sym"}


def test_i2_without_any_library_the_catalogue_is_the_built_in_one(monkeypatch):
    """Negative: no KiCad and no fixture -> built-in shapes, which lack what the fixture provides."""
    monkeypatch.setattr(g, "kicad_symbols_dir", lambda: None)
    g.catalogue.cache_clear()
    by_key = {glyph.key: glyph for glyph in g.catalogue()}
    assert "potentiometer" not in by_key and "crystal" not in by_key
    assert sorted(abs(t.y) for t in by_key["resistor"].terminals) == [5.08, 5.08]
    assert all(glyph.body_hint is None for glyph in by_key.values())
    assert by_key["resistor"].source == "builtin"


def test_i2_the_fixture_resolves_only_the_entries_it_holds():
    """A missing symbol is skipped, not invented: the loader answers None for it."""
    assert g.load_library_glyph(FIXTURE_LIBRARY, "R") is not None
    assert g.load_library_glyph(FIXTURE_LIBRARY, "Q_NPN_BCE") is None


def test_i2_kicad_reads_the_fixture_library(tmp_path):
    """Where kicad-cli exists the hand-written file must be a valid KiCad 10 library."""
    cli = shutil.which("kicad-cli") or next(
        (str(p) for p in sorted(Path("C:/Program Files/KiCad").glob("*/bin/kicad-cli.exe"), reverse=True)), None)
    if not cli:
        pytest.skip("KiCad CLI not installed")
    result = subprocess.run([cli, "sym", "upgrade", "--force", "-o", str(tmp_path / "upgraded.kicad_sym"),
                             str(FIXTURE_LIBRARY)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


# -- I11: the catalogue cache never leaks between tests -------------------------

def test_i11_the_catalogue_cache_is_empty_when_a_test_starts():
    """Positive: the autouse fixture cleared it, whatever the previous test built."""
    assert g.catalogue.cache_info().currsize == 0
    g.catalogue()
    assert g.catalogue.cache_info().currsize == 1


def test_i11_the_previous_test_left_nothing_behind():
    """Negative twin, run right after a test that filled the cache: still empty."""
    assert g.catalogue.cache_info().currsize == 0


# -- I4: publish-release.ps1 refuses to tag without a green suite -----------------

@pytest.fixture(scope="module")
def publish_script():
    return (ROOT / "scripts" / "publish-release.ps1").read_text(encoding="utf-8")


def test_i4_the_release_script_gates_the_tag_on_the_tests(publish_script):
    """The gate (GitHub run, local run or an explicit skip) comes BEFORE the tag is created."""
    gate = publish_script.index("Assert-TestsGreenOnGitHub $head")
    local = publish_script.index("Invoke-TestsLocally")
    tag = publish_script.index("git tag -a $tag")
    assert gate < tag and local < tag
    assert "--workflow Tests" in publish_script and "--commit $sha" in publish_script
    assert "run_test_halves.py') both" in publish_script
    assert "[switch]$RunTestsLocally" in publish_script and "[switch]$SkipTests" in publish_script


def test_i4_skipping_the_tests_is_explicit_and_loud(publish_script):
    """Negative: -SkipTests is a switch (off unless typed) and prints a red warning."""
    assert re.search(r"if \(\$SkipTests\)\s*\{\s*Write-Host .*-ForegroundColor Red", publish_script)
    assert "WITHOUT any proof" in publish_script
    # Contract changed on 2026-10-28 (v1.0.2): the same commit may have a
    # green run on one branch and a cancelled duplicate on another, so the
    # gate looks at every run of the commit and tags only when ONE is green;
    # a failure refuses, a pending run waits, anything else asks for a re-run.
    assert "--limit 10" in publish_script
    # CONTRACT CHANGED 2026-10-29 (review K6): the green branch prints the run
    # URL BEFORE returning; the old message after the final throw was
    # unreachable and named an undefined $run, so it must be gone.
    assert "conclusion -eq 'success'" in publish_script
    assert re.search(r"if \(\$green\.Count -gt 0\) \{\s*(?:#[^\n]*\s*)*Write-Host \"Tests green on GitHub for \$sha "
                     r"\(\$\(\$green\[0\]\.url\)\)\.\" -ForegroundColor Green\s*return\s*\}", publish_script)
    assert "$run.url" not in publish_script
    assert "conclusion -eq 'failure'" in publish_script and "fix the suite before tagging" in publish_script
    assert "status -ne 'completed'" in publish_script and "wait for it" in publish_script
    assert "re-run it on GitHub" in publish_script
    assert "No «Tests» run exists" in publish_script


@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell non disponibile")
def test_i4_the_release_script_still_parses():
    path = ROOT / "scripts" / "publish-release.ps1"
    command = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{path}',[ref]$null,[ref]$e); exit $e.Count")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


# -- I5: notices list the transitive packages and pypdf ---------------------------

def _pins():
    text = (ROOT / "requirements" / "release-constraints.txt").read_text(encoding="utf-8")
    return dict(line.strip().split("==") for line in text.splitlines()
                if line.strip() and not line.startswith("#"))


def test_i5_pypdf_is_a_declared_pinned_and_noticed_dependency():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^pypdf\s*$", requirements, re.MULTILINE)
    pins = _pins()
    assert pins["pypdf"] == "6.21.3"
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert re.search(r"^\| pypdf \| 6\.21\.3 \| BSD-3-Clause \|", notices, re.MULTILINE)


@pytest.mark.parametrize("package", [
    "matplotlib", "certifi", "requests", "urllib3", "idna", "charset-normalizer", "protobuf",
    "pooch", "platformdirs", "scooby", "contourpy", "kiwisolver", "fonttools", "cycler",
    "pyparsing", "six", "packaging", "python-dateutil"])
def test_i5_every_bundled_transitive_package_is_in_the_notices_with_its_pinned_version(package):
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    row = re.search(rf"^\| {re.escape(package)} \| ([^|]+) \| ([^|]+) \|", notices, re.MULTILINE)
    assert row, package
    assert row.group(1).strip() == _pins()[package], package
    assert row.group(2).strip(), f"{package}: licence missing"


def test_i5_the_notices_name_the_manifest_as_the_authoritative_list():
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "MANIFEST.txt" in notices.split("## Python dependencies", 1)[1].split("|", 1)[0]


def test_i5_pypdf_is_imported_by_name_so_pyinstaller_collects_it():
    """A plain `from pypdf import ...` is what PyInstaller's analysis follows: no hook needed."""
    source = (ROOT / "kicadgen" / "services" / "datasheet_extractor.py").read_text(encoding="utf-8")
    assert re.search(r"^\s*from pypdf import ", source, re.MULTILINE)
    spec = (ROOT / "packaging" / "KiCadGen.spec").read_text(encoding="utf-8")
    assert "pypdf" not in spec, "pure Python: a hidden import would be a sign the import is dynamic"


# -- I8: secrets that never enter the environment are masked too ------------------

@pytest.fixture
def crash_hooks():
    yield
    crash_report.uninstall()


def test_i8_a_credential_manager_key_without_the_sk_shape_is_masked(crash_hooks, monkeypatch, tmp_path):
    """Positive: a key that is neither in the environment nor `sk-`-shaped disappears from the report."""
    secret = "cm" + "X9y" * 8                       # not in os.environ, no sk- prefix
    assert secret not in os.environ.values()
    crash_report.install(lambda: tmp_path, secrets=lambda: [secret, ""])
    report = crash_report.format_report(RuntimeError, RuntimeError(f"Bearer {secret} refused"), None)
    assert secret not in report and "<redacted>" in report
    assert crash_report.scrub(f"x {secret} y") == "x <redacted> y"


def test_i8_without_the_callable_only_environment_and_pattern_mask(crash_hooks, tmp_path):
    """Negative: the same key is NOT masked when nothing tells the scrubber about it."""
    secret = "cm" + "X9y" * 8
    crash_report.install(lambda: tmp_path)
    assert crash_report.scrub(f"x {secret} y") == f"x {secret} y"


def test_i8_a_failing_or_short_secret_never_loses_the_report(crash_hooks, tmp_path, monkeypatch):
    def broken():
        raise OSError("credential store unreadable")

    monkeypatch.setenv("OPENROUTER_API_KEY", "envsecretvalue1234")
    crash_report.install(lambda: tmp_path, secrets=broken)
    assert crash_report.scrub("a envsecretvalue1234 b") == "a <redacted> b"
    crash_report.install(lambda: tmp_path, secrets=lambda: ["short", None, 42])
    assert crash_report.scrub("short 42") == "short 42", "values under MIN_SECRET_LENGTH are left"
    assert crash_report.current_secrets() == {"envsecretvalue1234"}


def test_i8_uninstall_forgets_the_callable(crash_hooks, tmp_path):
    crash_report.install(lambda: tmp_path, secrets=lambda: ["forgottensecret"])
    crash_report.uninstall()
    assert crash_report.scrub("forgottensecret") == "forgottensecret"


def test_i8_the_launcher_wires_the_services_key_readers(monkeypatch, tmp_path):
    """`main.provider_secrets` returns the Credential Manager keys the services would use."""
    launcher = _load_tool("main.py", "kicadgen_launcher_for_secrets")
    from kicadgen.services import credentials
    credentials.write_secret(credentials.OPENROUTER_TARGET, "stored-openrouter-key-value")
    values = launcher.provider_secrets()
    assert "stored-openrouter-key-value" in values
    assert len(values) == 2 and all(isinstance(v, str) for v in values)
    # Wired into the hooks: a report written by the installed hook masks it.
    # `install_crash_reports` replaces sys.excepthook (and the crash module
    # threading.excepthook): monkeypatch restores both after the test, as
    # tests/test_launcher.py does, so the suite's own hook comes back.
    import sys
    import threading
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr("kicadgen.app_paths.logs_dir", lambda: tmp_path / "logs")
    try:
        launcher.install_crash_reports(exit_process=lambda code: None)
        assert crash_report.scrub("key stored-openrouter-key-value here") == "key <redacted> here"
    finally:
        crash_report.uninstall()


def test_i9_the_install_docstring_describes_the_real_wiring():
    """The launcher exits after the report; the docstring must not promise a continuing event loop."""
    doc = crash_report.install.__doc__
    assert "event loop continues" not in doc
    assert "EXITS" in doc and "install_crash_reports" in doc


# -- I10: the seed verification covers translations and component types -----------

@pytest.fixture
def seed_tool(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    return _load_tool("tools/make_seed_db.py", "make_seed_db_review")


def _edit(seed, statement, *params):
    connection = sqlite3.connect(seed)
    with connection:
        connection.execute(statement, params)
    connection.close()


def test_i10_a_fresh_seed_passes_the_extended_verification(seed_tool, tmp_path):
    seed = seed_tool.make_seed(tmp_path / "ok.sqlite3")
    seed_tool.verify_seed(seed)


def test_i10_a_seed_missing_a_language_is_rejected(seed_tool, tmp_path):
    seed = seed_tool.make_seed(tmp_path / "fr.sqlite3")
    _edit(seed, "DELETE FROM translations WHERE language='fr'")
    with pytest.raises(seed_tool.SeedError, match="miss a language"):
        seed_tool.verify_seed(seed)


def test_i10_a_seed_with_an_extra_translation_row_is_rejected(seed_tool, tmp_path):
    seed = seed_tool.make_seed(tmp_path / "extra.sqlite3")
    _edit(seed, "INSERT INTO translations VALUES ('developer.only.key','it','x')")
    with pytest.raises(seed_tool.SeedError, match="translation rows"):
        seed_tool.verify_seed(seed)


def test_i10_a_seed_with_a_changed_translation_is_rejected(seed_tool, tmp_path):
    seed = seed_tool.make_seed(tmp_path / "changed.sqlite3")
    _edit(seed, "UPDATE translations SET text='edited on this machine' WHERE rowid=1")
    with pytest.raises(seed_tool.SeedError, match="differ from the catalogue"):
        seed_tool.verify_seed(seed)


def test_i10_a_seed_with_a_stale_or_added_component_type_is_rejected(seed_tool, tmp_path):
    seed = seed_tool.make_seed(tmp_path / "type.sqlite3")
    _edit(seed, "UPDATE component_types SET instructions='old prompt' WHERE rowid=1")
    with pytest.raises(seed_tool.SeedError, match="stale or not built-in"):
        seed_tool.verify_seed(seed)
    seed = seed_tool.make_seed(tmp_path / "added.sqlite3")
    _edit(seed, "INSERT INTO component_types VALUES ('mine','Mine','mine','p',1,0,99)")
    with pytest.raises(seed_tool.SeedError, match="extra \\['mine'\\]"):
        seed_tool.verify_seed(seed)
