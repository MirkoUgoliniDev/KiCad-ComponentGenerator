"""@brief Services findings of the adversarial review of 2026-10-27 (E1-E8).

Every finding has a positive case (the defect is gone) and a negative case
(the behaviour that was right before is unchanged), so a rule tuned on one
example alone would not pass. No network: ``urlopen`` is replaced wherever a
request would be made, and the Credential Manager is the in-memory fake
installed by ``conftest.py``.
"""
from __future__ import annotations

import base64
import io
import json
import re
import sys
import urllib.error
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path

import pytest

from kicadgen.services import credentials, deepseek_pricing, installed_programs, openrouter
from kicadgen.services import providers
from kicadgen.services import datasheet_extractor as de

SERVICES = Path(__file__).resolve().parents[1] / "kicadgen" / "services"


# ── E1: PDF metadata never leaves the computer ─────────────────────────────

_XMP = (b'<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b'<dc:creator><rdf:Seq><rdf:li>Mario Rossi</rdf:li></rdf:Seq></dc:creator>'
        b'</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')


def _pdf_with_metadata(pages: int = 2) -> bytes:
    """A PDF carrying an Information dictionary and XMP at document and page level."""
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, StreamObject

    def xmp_stream():
        stream = StreamObject()
        stream.set_data(_XMP)
        stream[NameObject("/Type")] = NameObject("/Metadata")
        stream[NameObject("/Subtype")] = NameObject("/XML")
        return stream

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(200, 100)
    writer.add_metadata({"/Author": "Mario Rossi", "/Producer": "SecretTool 9",
                         "/Creator": "SecretTool", "/CreationDate": "D:20261027120000"})
    writer.root_object[NameObject("/Metadata")] = writer._add_object(xmp_stream())
    writer.pages[0][NameObject("/Metadata")] = writer._add_object(xmp_stream())
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _sent_bytes(block: dict) -> bytes:
    url = block["file"]["file_data"]
    assert url.startswith("data:application/pdf;base64,")
    return base64.b64decode(url.split(",", 1)[1])


def test_uploaded_pdf_carries_no_metadata_and_still_parses(tmp_path):
    """@brief Positive: author, producer, dates and both XMP streams are gone; pages stay."""
    from pypdf import PdfReader
    raw = _pdf_with_metadata()
    assert raw.count(b"Mario Rossi") == 3 and b"SecretTool" in raw
    path = tmp_path / "datasheet.pdf"
    path.write_bytes(raw)
    sent = _sent_bytes(de._file_to_content_block(path))
    for secret in (b"Mario Rossi", b"SecretTool", b"20261027", b"/Metadata", b"/Author",
                   b"/Producer", b"/CreationDate", b"xmpmeta"):
        assert secret not in sent
    reader = PdfReader(io.BytesIO(sent))
    assert len(reader.pages) == 2
    assert not reader.metadata and reader.xmp_metadata is None
    assert all("/Metadata" not in page for page in reader.pages)


@pytest.mark.parametrize("payload", [b"%PDF-1.4 garbage without objects", b"not a pdf at all",
                                     b"%PDF-1.7\n" + b"\x00" * 64])
def test_an_unreadable_pdf_is_refused_and_nothing_is_uploaded(tmp_path, payload, monkeypatch):
    """@brief Positive: fail closed, as for an image, with the file name in the message."""
    path = tmp_path / "broken.pdf"
    path.write_bytes(payload)
    encoded = []
    monkeypatch.setattr(de.base64, "b64encode", lambda data: encoded.append(data) or b"")
    with pytest.raises(de.ExtractionError, match=r"broken\.pdf"):
        de._file_to_content_block(path)
    assert encoded == []


def test_an_encrypted_pdf_is_refused_not_sent_raw(tmp_path):
    """@brief Positive: a file the program cannot rebuild keeps its metadata, hence stays home."""
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(10, 10)
    writer.add_metadata({"/Author": "Mario Rossi"})
    writer.encrypt("password")
    out = io.BytesIO()
    writer.write(out)
    path = tmp_path / "locked.pdf"
    path.write_bytes(out.getvalue())
    with pytest.raises(de.ExtractionError, match=r"locked\.pdf"):
        de._file_to_content_block(path)


def test_a_rebuilt_pdf_still_carrying_metadata_is_refused(tmp_path, monkeypatch):
    """@brief Negative of the boundary: the read-back check, not pypdf, is the last word."""
    import pypdf
    original = pypdf.PdfWriter.write

    def keep_metadata(self, stream):
        # A writer that silently keeps the Information dictionary.
        self.add_metadata({"/Producer": "SecretTool"})
        return original(self, stream)
    monkeypatch.setattr(pypdf.PdfWriter, "write", keep_metadata)
    path = tmp_path / "sticky.pdf"
    path.write_bytes(_pdf_with_metadata(1))
    with pytest.raises(de.ExtractionError, match=r"sticky\.pdf"):
        de._file_to_content_block(path)


def test_the_size_cap_applies_to_the_rebuilt_pdf(tmp_path, monkeypatch):
    """@brief The 20 MB cap guards the bytes that leave, measured after the rebuild."""
    path = tmp_path / "big.pdf"
    path.write_bytes(_pdf_with_metadata(1))
    monkeypatch.setattr(de, "_pdf_without_metadata", lambda data: b"%PDF-" + b"x" * 64)
    monkeypatch.setattr(de, "MAX_FILE_BYTES", 60)
    with pytest.raises(de.ExtractionError, match="troppo grande"):
        de._file_to_content_block(path)


def test_an_image_does_not_go_through_the_pdf_cleaner(tmp_path, monkeypatch):
    """@brief Negative: the image path is unchanged, a clean PNG is sent byte for byte."""
    from PIL import Image
    path = tmp_path / "board.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path, format="PNG")
    monkeypatch.setattr(de, "_pdf_without_metadata",
                        lambda data: (_ for _ in ()).throw(AssertionError("image went to pypdf")))
    block = de._file_to_content_block(path)
    url = block["image_url"]["url"]
    assert base64.b64decode(url.split(",", 1)[1]) == path.read_bytes()


# ── E2: an install folder is absolute or nothing ───────────────────────────

@pytest.mark.parametrize("entry", [
    {"UninstallString": "MsiExec.exe /X{1F2E3D4C-0000-1111-2222-333344445555}"},
    {"DisplayIcon": "MsiExec.exe,1"},
    {"InstallLocation": "C:"},
    {"InstallLocation": "."},
    {"InstallLocation": r"\KiCad\10.0"},
    {"InstallLocation": r"KiCad\10.0"},
    {"DisplayIcon": r"C:kicad-cli.exe"},
])
def test_relative_or_drive_relative_entries_declare_no_folder(entry):
    """@brief Positive: nothing that would resolve against the working directory is returned."""
    assert installed_programs.install_dir_from_entry(entry) is None


def test_relative_entries_never_make_the_working_directory_a_candidate(tmp_path, monkeypatch):
    """@brief The executable found in the current directory is never offered to run."""
    (tmp_path / "kicad-cli.exe").write_bytes(b"MZ")
    monkeypatch.chdir(tmp_path)
    entries = [{"DisplayName": "KiCad 10.0",
                "UninstallString": "MsiExec.exe /X{1F2E3D4C-0000-1111-2222-333344445555}"}]
    folders = installed_programs.registered_install_dirs("KiCad", entries=entries)
    assert folders == []
    assert installed_programs.executables_in(folders, "kicad-cli.exe") == []


@pytest.mark.parametrize("entry, folder", [
    ({"InstallLocation": "", "DisplayIcon": r"C:\Program Files\FreeCAD 1.1\bin\FreeCAD.exe"},
     r"C:\Program Files\FreeCAD 1.1\bin"),
    ({"InstallLocation": r"D:\Apps\KiCad\10.0"}, r"D:\Apps\KiCad\10.0"),
    ({"UninstallString": r'"E:\Tools\FreeCAD 1.1\Uninstall-FreeCAD.exe" /S'},
     r"E:\Tools\FreeCAD 1.1"),
    ({"InstallLocation": r"\\server\share\KiCad"}, r"\\server\share\KiCad"),
])
def test_absolute_entries_still_name_their_folder(entry, folder):
    """@brief Negative: absolute folders, UNC included, keep working as before."""
    assert installed_programs.install_dir_from_entry(entry) == Path(folder)


# ── E3: comments of the reviewed services are English ──────────────────────

#: Declared heuristic, after tests/test_message_localization.py: Italian
#: function words and accented vowels that never occur in an English comment.
#: The elision clause of that scan («puo'») is left out on purpose: an
#: English comment quoting a word ('yolo') would trip it.
_ITALIAN = re.compile(
    r"\b(il|lo|gli|di|del|della|dei|delle|non|con|che|nel|nella|dal|dalla|sono|una|"
    r"questo|questa|anche|senza|nessun|nessuna|manca|deve|oppure|ogni|gia|piu|puo|"
    r"valgono|espongono)\b(?!-)|[àèéìòù]", re.I)


@pytest.mark.parametrize("module", ["datasheet_extractor", "installed_programs", "openrouter",
                                    "providers", "deepseek_pricing", "credentials",
                                    "system_dependencies"])
def test_no_italian_comment_in_the_reviewed_services(module):
    """@brief Positive: the translated comment and every other `#` line read as English."""
    found = []
    for number, line in enumerate(
            (SERVICES / f"{module}.py").read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text.startswith("#") or "«" in text:
            continue
        if _ITALIAN.search(text):
            found.append(f"{module}.py:{number}: {text}")
    assert found == []


def test_the_italian_detector_sees_the_old_comment():
    """@brief Negative of the scan: the sentence that was there would be caught."""
    assert _ITALIAN.search('# I modelli "auto" espongono prezzi sentinella negativi: valgono 0.')
    assert not _ITALIAN.search("# The auto router models publish negative sentinel prices.")


# ── E4: DeepSeek peak rule as published on 2026-10-27 ──────────────────────

def test_the_price_list_was_re_read_on_the_review_date():
    assert deepseek_pricing.PRICES_CHECKED == "2026-10-27"
    assert deepseek_pricing.PRICES_SOURCE == "https://api-docs.deepseek.com/quick_start/pricing"
    # Table of the page on that date, off-peak column.
    assert deepseek_pricing.price_of("deepseek-flash") == (0.003, 0.15, 0.60)
    assert deepseek_pricing.price_of("deepseek-v4-pro") == (0.022, 0.66, 1.98)
    assert deepseek_pricing.price_of("deepseek-v4-flash") == (0.003, 0.15, 0.60)


@pytest.mark.parametrize("moment, peak", [
    # «Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through
    # Friday»: Tuesday 2026-10-27.
    (datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 27, 3, 59, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc), False),
    (datetime(2026, 10, 27, 5, 30, tzinfo=timezone.utc), False),
    (datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 27, 9, 59, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc), False),
    (datetime(2026, 10, 27, 0, 59, tzinfo=timezone.utc), False),
    # «All other hours are off-peak, including weekends»: Saturday and Sunday.
    (datetime(2026, 10, 31, 7, 0, tzinfo=timezone.utc), False),
    (datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc), False),
    # A local time is converted: 09:00 in Rome (CET, UTC+1) is 08:00 UTC, peak;
    # 11:30 in Rome is 10:30 UTC, off-peak.
    (datetime(2026, 10, 27, 9, 0, tzinfo=timezone(__import__("datetime").timedelta(hours=1))), True),
    (datetime(2026, 10, 27, 11, 30, tzinfo=timezone(__import__("datetime").timedelta(hours=1))), False),
])
def test_peak_windows_follow_the_published_text(moment, peak):
    assert deepseek_pricing.is_peak(moment) is peak


def test_peak_costs_exactly_twice_the_off_peak_price():
    """@brief «Off-peak rates are half of the peak rates»: 1M miss + 1M output at both rates."""
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    off = deepseek_pricing.call_cost("deepseek-v4-pro", usage,
                                     datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc))
    peak = deepseek_pricing.call_cost("deepseek-v4-pro", usage,
                                      datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc))
    assert off == pytest.approx(0.66 + 1.98)
    assert peak == pytest.approx(1.32 + 3.96)
    assert deepseek_pricing.PEAK_FACTOR == 2.0


# ── E5: a stored blob is a key, not just decodable text ────────────────────

@pytest.fixture
def vault(monkeypatch):
    """The in-memory advapi32 installed by conftest, on a Windows-like platform."""
    monkeypatch.setattr(sys, "platform", "win32")
    return credentials._advapi32


@pytest.mark.parametrize("blob", [
    b"sk-or-v1-abcdef0123456789abcd",      # UTF-8 ASCII, even length: decodes to CJK garbage
    "sk-àé".encode("utf-8"),                # UTF-8 with accents, even length
    "sk or v1 key".encode("utf-16-le"),     # UTF-16, but blanks inside
    "sk-\tkey".encode("utf-16-le"),         # UTF-16 with a control character
    b"\x00\x00",                            # UTF-16 empty text
])
def test_a_blob_that_is_not_a_key_is_a_credential_error_naming_the_target(vault, blob):
    """@brief Positive: the wrong encoding or a blank is caught here, not as an HTTP 401."""
    vault.vault["KiCadGen/Test"] = blob
    with pytest.raises(credentials.CredentialError, match="KiCadGen/Test") as caught:
        credentials.read_secret("KiCadGen/Test")
    assert "sk" not in str(caught.value)
    assert vault.outstanding == {}


@pytest.mark.parametrize("secret", ["sk-or-v1-abcdef0123456789abcd", "sk-àé€-non-ascii-kept",
                                    "k"])
def test_a_proper_utf16_key_reads_back(vault, secret):
    """@brief Negative: text stored as UTF-16-LE, ASCII or not, comes back unchanged."""
    vault.vault["KiCadGen/Test"] = secret.encode("utf-16-le")
    assert credentials.read_secret("KiCadGen/Test") == secret
    vault.vault["KiCadGen/Test"] = secret.encode("utf-16-le") + b"\x00\x00"
    assert credentials.read_secret("KiCadGen/Test") == secret


# ── E6: successful bodies are read with a bound ────────────────────────────

class _Response(io.BytesIO):
    """A urlopen result: context manager, ``read(n)``, and it records what was asked."""
    asked: list = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size=-1):
        _Response.asked.append(size)
        return super().read(size)


def _serve(monkeypatch, module, body: bytes):
    _Response.asked = []
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: _Response(body))


def _over_limit_body() -> bytes:
    limit = openrouter._MAX_RESPONSE_BODY_BYTES
    return b'{"data": "' + b"x" * limit + b'"}'


def test_the_helper_accepts_exactly_the_limit_and_refuses_one_byte_more():
    body = json.dumps({"k": "v"}).encode()
    assert openrouter._json_from_response(_Response(body), limit=len(body)) == {"k": "v"}
    with pytest.raises(ValueError):
        openrouter._json_from_response(_Response(body), limit=len(body) - 1)
    # Only limit + 1 bytes are requested, never the whole stream.
    assert _Response.asked[-1] == len(body)


def test_get_json_over_the_limit_is_an_undecodable_answer_not_unreachable(monkeypatch):
    _serve(monkeypatch, openrouter, _over_limit_body())
    with pytest.raises(openrouter.OpenRouterError, match="non decodificabile") as caught:
        openrouter._get_json("https://openrouter.invalid/x", 5, key="k-test")
    assert caught.value.unreachable is False
    assert _Response.asked == [openrouter._MAX_RESPONSE_BODY_BYTES + 1]


def test_get_json_under_the_limit_is_parsed(monkeypatch):
    _serve(monkeypatch, openrouter, b'{"data": {"limit": 1}}')
    assert openrouter._get_json("https://openrouter.invalid/x", 5, key="k-test") == {
        "data": {"limit": 1}}


def test_a_chat_answer_over_the_limit_is_possibly_billed(monkeypatch):
    monkeypatch.setenv(openrouter.API_KEY_ENV, "k-test")
    _serve(monkeypatch, openrouter, _over_limit_body())
    seen = []
    openrouter.set_usage_listener(seen.append)
    try:
        with pytest.raises(openrouter.OpenRouterError, match="non decodificabile") as caught:
            openrouter.call_tool("vendor/model", "p", [], {"function": {"name": "report"}})
        assert caught.value.possibly_billed
        assert len(seen) == 1 and seen[0]["cost_usd"] is None
    finally:
        openrouter.set_usage_listener(None)


def test_a_models_list_over_the_limit_falls_back_to_the_cache(monkeypatch):
    cached = [{"id": "vendor/cached", "pricing": {"prompt": "0", "completion": "0"},
               "architecture": {"input_modalities": ["text", "image", "file"]}}]
    monkeypatch.setattr(openrouter, "_load_models_cache",
                        lambda ignore_ttl=False: cached if ignore_ttl else None)
    written = []
    monkeypatch.setattr(openrouter, "_write_models_cache", written.append)
    _serve(monkeypatch, openrouter, _over_limit_body())
    models = openrouter.list_models()
    assert [m.id for m in models] == ["vendor/cached"] and written == []
    # Negative: a list within the limit is parsed and cached.
    fresh = [{"id": "vendor/fresh", "pricing": {"prompt": "0", "completion": "0"},
              "architecture": {"input_modalities": ["text", "image", "file"]}}]
    _serve(monkeypatch, openrouter, json.dumps({"data": fresh}).encode())
    assert [m.id for m in openrouter.list_models()] == ["vendor/fresh"] and written == [fresh]


def test_a_deepseek_answer_over_the_limit_is_possibly_billed_not_unreachable(monkeypatch):
    _serve(monkeypatch, providers, _over_limit_body())
    with pytest.raises(providers.ProviderError) as caught:
        providers.deepseek_request(providers.DEEPSEEK_CHAT_URL, {"model": "x"}, key="k-test")
    assert caught.value.possibly_billed is True and caught.value.unreachable is False
    _serve(monkeypatch, providers, b'{"ok": 1}')
    assert providers.deepseek_request(providers.DEEPSEEK_CHAT_URL, {"model": "x"},
                                      key="k-test") == {"ok": 1}


# ── E7: unreachable means «never sent», nothing else ───────────────────────

@pytest.mark.parametrize("failure, unreachable, billed", [
    (TimeoutError("timed out"), False, True),
    (IncompleteRead(b"partial", 10), False, True),
    (ValueError("bad json"), False, True),
    (urllib.error.URLError("refused"), True, False),
    (urllib.error.URLError(TimeoutError("connect timed out")), True, False),
])
def test_deepseek_post_failures_are_either_unreachable_or_possibly_billed(
        monkeypatch, failure, unreachable, billed):
    def fail(*_a, **_k):
        raise failure
    monkeypatch.setattr(providers.urllib.request, "urlopen", fail)
    with pytest.raises(providers.ProviderError) as caught:
        providers.deepseek_request(providers.DEEPSEEK_CHAT_URL, {"model": "x"}, key="k-test")
    assert caught.value.unreachable is unreachable
    assert caught.value.possibly_billed is billed
    assert not (caught.value.unreachable and caught.value.possibly_billed)


def test_a_deepseek_get_that_times_out_is_neither_unreachable_nor_billed(monkeypatch):
    """@brief Negative: a GET (no body) cannot be billed, and a read timeout is not «down»."""
    def fail(*_a, **_k):
        raise TimeoutError("timed out")
    monkeypatch.setattr(providers.urllib.request, "urlopen", fail)
    with pytest.raises(providers.ProviderError) as caught:
        providers.deepseek_request(providers.DEEPSEEK_MODELS_URL, key="k-test")
    assert caught.value.unreachable is False and not caught.value.possibly_billed


# ── E8: the name heuristic is declared and never silent ────────────────────

def _body(name, **extra):
    return dict(name=name, x_mm=1.0, y_mm=2.0, length_mm=3.0, width_mm=2.0, height_mm=1.0,
                **extra)


def test_a_pad_named_body_without_object_type_is_dropped_with_a_note():
    """@brief Positive: the heuristic fires, and says so, naming the body."""
    notes = []
    assert de._components_3d([_body("Pad di test IC")], notes) is None
    assert len(notes) == 1 and "Pad di test IC" in notes[0]
    assert "tipo di oggetto" in notes[0]


def test_a_declared_pcb_feature_is_dropped_by_its_type_without_the_name_note():
    """@brief The declared criterion needs no heuristic: no note about the name."""
    notes = []
    assert de._components_3d([_body("U7 regolatore", object_type="pcb_feature")],
                             notes, trusted=True) is None
    assert notes == []


def test_a_normal_body_is_kept_without_any_note():
    """@brief Negative: a body whose name is not a PCB feature passes untouched."""
    notes = []
    result = de._components_3d([_body("U1 regolatore lineare", kind="generic")], notes)
    assert result is not None and result[0]["name"] == "U1 regolatore lineare"
    assert notes == []


def test_a_trusted_component_named_like_a_pad_is_kept_by_its_type():
    """@brief Negative: with `object_type` declared the name is not consulted."""
    notes = []
    result = de._components_3d([_body("pad (unknown)", object_type="component")],
                               notes, trusted=True)
    assert result is not None and result[0]["name"] == "pad (unknown)"
    assert notes == []
