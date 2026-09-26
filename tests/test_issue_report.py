"""@brief «Segnala un problema» URL: well-formed, bounded, free of personal data."""
import getpass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from kicadgen.services import issue_report as ir


def _environment(**overrides):
    values = dict(app_version="1.0.0", os="Windows-11-10.0.26200-SP0",
                  python="3.13.1 (64-bit)", qt="Qt 6.9.0, PyQt 6.9.0",
                  freecad_found=True, freecad_version="1.0",
                  kicad_found=False, kicad_version=None, frozen=True)
    values.update(overrides)
    return ir.IssueEnvironment(**values)


def _query(url):
    parts = urlsplit(url)
    return parts, parse_qs(parts.query)


def test_url_is_well_formed_and_prefilled():
    """@brief Positive: GitHub new-issue URL with title and a body carrying the facts."""
    url = ir.build_issue_url("Il 3D non si genera", environment=_environment())
    parts, query = _query(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == ir.ISSUES_NEW_URL
    assert query["title"] == ["Il 3D non si genera"]
    body = query["body"][0]
    assert "KiCadGen: 1.0.0 (installed)" in body
    assert "OS: Windows-11-10.0.26200-SP0" in body and "Python: 3.13.1 (64-bit)" in body
    assert "FreeCAD: found (1.0)" in body and "KiCad: not found" in body
    assert "- [ ] `analisi-ai.log`" in body and "crash-reports" in body


def test_default_title_and_translated_texts():
    """@brief An empty title falls back; translated headings replace the defaults."""
    url = ir.build_issue_url("", environment=_environment(),
                             texts={"default_title": "Segnalazione", "environment": "Ambiente"})
    _, query = _query(url)
    assert query["title"] == ["Segnalazione"] and "### Ambiente" in query["body"][0]


def test_unknown_tools_are_reported_as_unknown():
    """@brief Negative: without detection the tools are neither found nor missing."""
    body = ir.build_issue_body(_environment(freecad_found=None, freecad_version=None,
                                            kicad_found=None))
    assert "FreeCAD: unknown" in body and "KiCad: unknown" in body


def test_length_is_capped_with_a_truncation_note():
    """@brief A body that does not fit is cut at a line and says so."""
    long_title = "x" * 5000
    url = ir.build_issue_url(long_title, environment=_environment(), max_length=900)
    assert len(url) <= 900
    _, query = _query(url)
    assert len(query["title"][0]) == ir.MAX_TITLE_LENGTH
    assert query["body"][0].rstrip().endswith(ir.DEFAULT_TEXTS["truncated"])


def test_default_cap_holds_with_long_translations():
    """@brief Even very long overrides stay below MAX_URL_LENGTH."""
    texts = {key: "è" * 400 for key in ir.DEFAULT_TEXTS}
    url = ir.build_issue_url("titolo", environment=_environment(), texts=texts)
    assert len(url) <= ir.MAX_URL_LENGTH


def test_tiny_limit_still_returns_a_valid_link():
    """@brief Negative edge: a limit smaller than any body keeps at most the title."""
    url = ir.build_issue_url("abc", environment=_environment(), max_length=len(ir.ISSUES_NEW_URL) + 5)
    assert url.startswith(ir.ISSUES_NEW_URL) and len(url) <= len(ir.ISSUES_NEW_URL) + 5
    assert "body=" not in url


def test_no_home_path_or_user_name_leaks(monkeypatch):
    """@brief Paths from the machine never reach the public URL, only version digits."""
    home = str(Path.home())
    user = getpass.getuser()
    environment = ir.collect_environment(
        detect_applications=False,
        freecad_path=home + r"\AppData\Local\Programs\FreeCAD 1.0\bin\freecadcmd.exe",
        kicad_path=r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe")
    url = ir.build_issue_url(f"Errore in {home}\\Documents\\KiCadGen\\progetto",
                             environment=environment)
    decoded = " ".join(value for values in _query(url)[1].values() for value in values)
    assert home.lower() not in decoded.lower()
    if len(user) >= 3:
        assert user.lower() not in decoded.lower()
    assert "FreeCAD: found (1.0)" in decoded and "KiCad: found (9.0)" in decoded
    assert "Program Files" not in decoded


def test_other_user_folders_are_masked():
    """@brief Negative guard: a foreign Users segment is masked too."""
    url = ir.build_issue_url(r"C:\Users\mario.rossi\Desktop\foto.jpg", environment=_environment())
    title = _query(url)[1]["title"][0]
    assert "mario.rossi" not in title and r"C:\Users\<user>\Desktop" in title


def test_environment_collection_never_raises(monkeypatch):
    """@brief A failing detection yields «unknown», not an exception."""
    from kicadgen.services import system_dependencies

    def broken():
        raise OSError("no access")
    monkeypatch.setattr(system_dependencies, "detect_external_applications", broken)
    environment = ir.collect_environment()
    assert environment.freecad_found is None and environment.kicad_found is None
    import kicadgen
    assert environment.app_version == kicadgen.__version__


@pytest.mark.parametrize("path,expected", [
    (r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe", "9.0"),
    (r"C:\Program Files\FreeCAD 1.0\bin\freecadcmd.exe", "1.0"),
    ("/usr/bin/freecadcmd", None),
    (None, None),
])
def test_tool_version_digits_only(path, expected):
    """@brief Only a version token next to the product folder is kept."""
    assert ir._tool_version(path) == expected
