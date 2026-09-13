"""@brief Verify external KiCad and FreeCAD dependency detection."""

from pathlib import Path

from kicadgen.services import system_dependencies as dependencies


def test_find_kicad_cli_prefers_explicit_override(tmp_path, monkeypatch):
    """@brief Prefer the user-configured KiCad CLI executable."""
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"")
    monkeypatch.setenv("KICAD_CLI_PATH", str(executable))
    monkeypatch.setattr(dependencies.shutil, "which", lambda name: None)

    assert dependencies.find_kicad_cli() == str(executable.resolve())


def test_find_kicad_cli_uses_path(tmp_path, monkeypatch):
    """@brief Detect kicad-cli from PATH when no override is configured."""
    executable = tmp_path / "kicad-cli.exe"
    executable.write_bytes(b"")
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    monkeypatch.setattr(
        dependencies.shutil, "which",
        lambda name: str(executable) if name == "kicad-cli" else None,
    )

    assert dependencies.find_kicad_cli() == str(executable.resolve())


def test_dependency_report_lists_each_missing_application(monkeypatch):
    """@brief Report KiCad and FreeCAD independently."""
    monkeypatch.setattr(dependencies, "find_kicad_cli", lambda: None)
    monkeypatch.setattr(dependencies, "find_pcbnew", lambda _cli=None: None)
    monkeypatch.setattr(
        dependencies, "find_freecadcmd",
        lambda: (_ for _ in ()).throw(dependencies.Cad3DError("missing")),
    )

    report = dependencies.detect_external_applications()

    assert report.kicad_cli is None
    assert report.freecadcmd is None
    assert report.missing == ("KiCad", "FreeCAD")


def test_dependency_report_keeps_detected_paths(tmp_path, monkeypatch):
    """@brief Keep normalized paths when both applications are available."""
    kicad = tmp_path / "kicad-cli.exe"
    freecad = tmp_path / "freecadcmd.exe"
    kicad.write_bytes(b"")
    freecad.write_bytes(b"")
    monkeypatch.setattr(dependencies, "find_kicad_cli", lambda: str(kicad))
    monkeypatch.setattr(dependencies, "find_pcbnew", lambda _cli=None: None)
    monkeypatch.setattr(dependencies, "find_freecadcmd", lambda: str(freecad))

    report = dependencies.detect_external_applications()

    assert Path(report.kicad_cli) == kicad.resolve()
    assert Path(report.freecadcmd) == freecad.resolve()
    assert report.missing == ()


def test_find_pcbnew_next_to_kicad_cli(tmp_path, monkeypatch):
    """@brief Prefer the PCB Editor shipped beside the detected KiCad CLI."""
    cli = tmp_path / "kicad-cli.exe"
    pcbnew = tmp_path / "pcbnew.exe"
    cli.touch()
    pcbnew.touch()
    monkeypatch.delenv("KICAD_PCBNEW_PATH", raising=False)
    assert dependencies.find_pcbnew(str(cli)) == str(pcbnew.resolve())
