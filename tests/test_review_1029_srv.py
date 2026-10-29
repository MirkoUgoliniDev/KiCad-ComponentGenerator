"""@brief Fixes of the 2026-10-29 review, services scope (E1-E8).

No network: every request goes to a stand-in for ``urlopen``; the in-flight
cancellation is exercised on a local socket pair. The Windows Credential
Manager is never read (``credentials.is_supported`` is forced False) and the
keys are fixed test strings set in the environment, which outranks any
``.env`` file.
"""
import http.client
import json
import os
import socket
import sqlite3
import threading
import time
import urllib.error

import pytest

from kicadgen.services import accepted_models as am
from kicadgen.services import ai_diagnostics, analysis_profiles, credentials, openrouter, providers
from kicadgen.services.analysis_profiles import ProfileError, ProfileStore
from kicadgen.services.datasheet_extractor import ExtractionError, _file_to_content_block

TOOL = {"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}
ANSWER = {"choices": [{"message": {"tool_calls": [
    {"function": {"name": "t", "arguments": "{\"ok\": 1}"}}]}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "cost": 0.001}}


class _Response:
    """A successful answer as urlopen returns it (context manager with bounded read)."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self._body


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(credentials, "is_supported", lambda: False)
    monkeypatch.setenv(openrouter.API_KEY_ENV, "sk-or-v1-good")
    monkeypatch.setenv(providers.DEEPSEEK_KEY_ENV, "sk-ds-good")
    openrouter.reset_usage()
    yield
    openrouter.reset_usage()


def _forbid_network(monkeypatch, module):
    calls = []

    def urlopen(*args, **kwargs):
        calls.append(1)
        raise AssertionError("urlopen must not be reached")

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    return calls


# ── E1: an unusable key is refused before any request ─────────────────────

@pytest.mark.parametrize("bad", ["sk-or-abc\ndef", "sk-or-abé€c"])
def test_e1_an_unusable_openrouter_key_is_never_sent_nor_billed(monkeypatch, bad):
    monkeypatch.setenv(openrouter.API_KEY_ENV, bad)
    calls = _forbid_network(monkeypatch, openrouter)
    with pytest.raises(openrouter.InvalidApiKeyError) as info:
        openrouter.call_tool("vendor/model", "sys", [], TOOL)
    assert calls == []
    assert info.value.possibly_billed is False and info.value.unreachable is False
    assert openrouter.API_KEY_ENV in str(info.value) and bad not in str(info.value)
    assert openrouter.usage_totals().calls == 0
    # A MissingApiKeyError: the GUI offers «Account AI...» as for a missing key.
    assert isinstance(info.value, openrouter.MissingApiKeyError)


@pytest.mark.parametrize("bad", ["sk-ds\nx", "sk-ds-é"])
def test_e1_an_unusable_deepseek_key_is_never_sent_nor_billed(monkeypatch, bad):
    monkeypatch.setenv(providers.DEEPSEEK_KEY_ENV, bad)
    calls = _forbid_network(monkeypatch, providers)
    with pytest.raises(providers.ProviderKeyInvalid) as info:
        openrouter.call_tool("deepseek-api/deepseek-flash", "sys", [], TOOL)
    assert calls == [] and openrouter.usage_totals().calls == 0
    assert isinstance(info.value, providers.ProviderKeyMissing)
    assert info.value.possibly_billed is False


def test_e1_a_pasted_unusable_key_is_judged_without_network(monkeypatch):
    calls = _forbid_network(monkeypatch, openrouter)
    with pytest.raises(openrouter.OpenRouterError) as info:
        openrouter.verify_api_key("sk-or\nabc")
    assert calls == [] and "a capo" in str(info.value)


def test_e1_a_valid_key_still_reaches_the_provider(monkeypatch):
    """Negative: a printable ASCII key is sent and the paid answer is recorded."""
    seen = []

    def urlopen(request, timeout=None, **kwargs):
        seen.append(request.get_header("Authorization"))
        return _Response(ANSWER)

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", urlopen)
    assert openrouter.call_tool("vendor/model", "sys", [], TOOL) == {"ok": 1}
    assert seen == ["Bearer sk-or-v1-good"]
    assert openrouter.usage_totals().calls == 1


def test_e1_a_header_http_client_would_refuse_is_reported_as_never_sent():
    request = openrouter.urllib.request.Request("https://x.invalid", headers={"X-A": "a\nb"})
    with pytest.raises(openrouter.OpenRouterError) as info:
        openrouter._refuse_unsendable_headers(request)
    assert info.value.possibly_billed is False
    # Negative: a folded header (line break followed by a blank) is legal.
    openrouter._refuse_unsendable_headers(
        openrouter.urllib.request.Request("https://x.invalid", headers={"X-A": "a\r\n b"}))


# ── E5: a connection lost after sending is possibly billed ─────────────────

@pytest.mark.parametrize("failure", [http.client.RemoteDisconnected("closed"),
                                     ConnectionResetError(10054, "reset"),
                                     http.client.BadStatusLine("garbage")])
def test_e5_openrouter_failure_after_sending_is_possibly_billed(monkeypatch, failure):
    def urlopen(*args, **kwargs):
        raise failure

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", urlopen)
    with pytest.raises(openrouter.OpenRouterError) as info:
        openrouter.call_tool("vendor/model", "sys", [], TOOL)
    assert info.value.possibly_billed is True and info.value.retryable is False
    totals = openrouter.usage_totals()
    assert totals.calls == 1 and totals.calls_without_cost == 1


@pytest.mark.parametrize("failure", [http.client.RemoteDisconnected("closed"),
                                     ConnectionResetError(10054, "reset"),
                                     http.client.BadStatusLine("garbage")])
def test_e5_deepseek_failure_after_sending_is_possibly_billed(monkeypatch, failure):
    def urlopen(*args, **kwargs):
        raise failure

    monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)
    with pytest.raises(providers.ProviderError) as info:
        openrouter.call_tool("deepseek-api/deepseek-flash", "sys", [], TOOL)
    assert info.value.possibly_billed is True and info.value.unreachable is False
    assert openrouter.usage_totals().calls_without_cost == 1


def test_e5_a_failure_while_sending_is_not_billed(monkeypatch):
    """Negative: urllib wraps every failure while sending in URLError: never sent, never billed."""
    def urlopen(*args, **kwargs):
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)
    with pytest.raises(openrouter.OpenRouterError) as info:
        openrouter.call_tool("vendor/model", "sys", [], TOOL)
    assert info.value.possibly_billed is False
    with pytest.raises(providers.ProviderError) as info:
        openrouter.call_tool("deepseek-api/deepseek-flash", "sys", [], TOOL)
    assert info.value.possibly_billed is False and info.value.unreachable is True
    assert openrouter.usage_totals().calls == 0


def test_e5_a_free_deepseek_get_lost_after_sending_is_not_billed(monkeypatch):
    """Negative: only a POST (a chat call) can be billed."""
    def urlopen(*args, **kwargs):
        raise ConnectionResetError(10054, "reset")

    monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)
    with pytest.raises(providers.ProviderError) as info:
        providers.deepseek_request(providers.DEEPSEEK_BALANCE_URL)
    assert info.value.possibly_billed is False


# ── E6: Cancel closes the in-flight connection ─────────────────────────────

def _blocking_urlopen(ready, sockets, attempts):
    """urlopen stand-in: registers one end of a socket pair and waits for the answer on it.

    The answer is awaited as http.client does, through a file made with
    ``makefile`` (which keeps the Python-level close from closing the
    handle), with a timeout so that a broken abort fails instead of hanging.
    """
    def urlopen(request, timeout=None, context=None):
        attempts.append(1)
        assert isinstance(context, openrouter._ClosableContext)
        reader, writer = socket.socketpair()
        sockets.extend((reader, writer))
        reader.settimeout(20)
        stream = reader.makefile("rb")
        # What the closable TLS context does when it wraps the connection.
        openrouter._track_socket(reader)
        ready.set()
        try:
            if stream.read(1) == b"":
                raise http.client.RemoteDisconnected("closed")
        finally:
            stream.close()
        return _Response(ANSWER)
    return urlopen


@pytest.mark.parametrize("model,module", [("vendor/model", openrouter),
                                          ("deepseek-api/deepseek-flash", providers)])
def test_e6_cancel_closes_the_request_in_flight(monkeypatch, model, module):
    ready, sockets, attempts = threading.Event(), [], []
    monkeypatch.setattr(module.urllib.request, "urlopen",
                        _blocking_urlopen(ready, sockets, attempts))
    pressed = threading.Event()

    class Stop(ai_diagnostics.CallCancelled):
        pass

    def cancel():
        if pressed.is_set():
            raise Stop("stopped by the user")

    events = []
    call = ai_diagnostics.make_logged_call(openrouter.call_tool, events.append, cancel=cancel)
    timer = threading.Thread(target=lambda: ready.wait(5) and pressed.set())
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(Stop) as info:
            call(model=model, system_prompt="s", content_blocks=[], tool=TOOL)
    finally:
        timer.join()
        for item in sockets:
            item.close()
    assert time.monotonic() - started < 5
    assert len(attempts) == 1  # never retried
    cause = info.value.__cause__
    assert isinstance(cause, openrouter.RequestCancelled) and cause.possibly_billed is True
    totals = openrouter.usage_totals()
    assert totals.calls == 1 and totals.calls_without_cost == 1
    assert any(event.startswith("AI_CALL_CANCELLED ") for event in events)
    assert openrouter._inflight == {}


def test_e6_without_cancel_the_request_completes(monkeypatch):
    """Negative: an answer that arrives is used; nothing is aborted."""
    ready, sockets, attempts = threading.Event(), [], []
    monkeypatch.setattr(openrouter.urllib.request, "urlopen",
                        _blocking_urlopen(ready, sockets, attempts))
    answer = threading.Thread(target=lambda: ready.wait(5) and sockets[1].sendall(b"x"))
    answer.start()
    call = ai_diagnostics.make_logged_call(openrouter.call_tool, None, cancel=lambda: None)
    try:
        assert call(model="vendor/model", system_prompt="s", content_blocks=[],
                    tool=TOOL) == {"ok": 1}
    finally:
        answer.join()
        for item in sockets:
            item.close()
    assert len(attempts) == 1 and openrouter.usage_totals().calls_without_cost == 0
    assert openrouter.abort_request(threading.get_ident()) is False


def test_e6_the_closable_context_keeps_the_default_verification():
    context = openrouter._closable_context()
    assert isinstance(context, openrouter._ClosableContext)
    assert context.check_hostname is True and context.verify_mode == openrouter.ssl.CERT_REQUIRED


def test_e6_an_abort_before_the_connection_shuts_it_when_it_arrives():
    with openrouter._ClosableRequest() as record:
        assert openrouter.abort_request(threading.get_ident()) is True
        reader, writer = socket.socketpair()
        try:
            openrouter._track_socket(reader)
            # Closed at once: nothing can be received on it any more.
            assert record.aborted and reader.fileno() == -1
            with pytest.raises(OSError):
                reader.recv(1)
        finally:
            reader.close()
            writer.close()


def test_e6_a_cancel_during_the_tls_handshake_does_not_wait_for_the_timeout():
    """The handshake runs on the tracked TLS socket: an abort ends it at once."""
    # The peer never answers the ClientHello: the handshake blocks until
    # the Cancel, pressed from another thread while it is in progress.
    me = threading.get_ident()
    raw, peer = socket.socketpair()
    raw.settimeout(20)
    canceller = threading.Thread(target=lambda: peer.recv(1) and openrouter.abort_request(me))
    started = time.monotonic()
    try:
        with openrouter._ClosableRequest():
            canceller.start()
            with pytest.raises(OSError):
                openrouter._closable_context().wrap_socket(raw, server_hostname="example.invalid")
    finally:
        canceller.join()
        raw.close()
        peer.close()
    assert time.monotonic() - started < 5


def test_e6_an_abort_leaves_other_threads_and_idle_sockets_alone():
    """Negative: only the in-flight request of the named thread is closed."""
    reader, writer = socket.socketpair()
    try:
        # No request in flight: tracking is a no-op and nothing is aborted.
        openrouter._track_socket(reader)
        assert openrouter.abort_request(threading.get_ident()) is False
        with openrouter._ClosableRequest() as record:
            openrouter._track_socket(reader)
            assert openrouter.abort_request(threading.get_ident() + 1) is False
            assert record.aborted is False and reader.fileno() != -1
            writer.sendall(b"y")
            assert reader.recv(1) == b"y"
    finally:
        reader.close()
        writer.close()
    assert openrouter._inflight == {}


# ── E2: one file signature per bound, writes still seen ────────────────────

def test_e2_repeated_lookups_do_not_read_the_file_each_time(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path / "p.sqlite3")
    store.translation("app.file", "de")
    checks = []
    original = store._database_signature
    monkeypatch.setattr(store, "_database_signature", lambda: checks.append(1) or original())
    started = time.monotonic()
    for _ in range(500):
        store.translation("app.file", "de")
    elapsed = time.monotonic() - started
    assert len(checks) <= 1 + elapsed / analysis_profiles._EXTERNAL_CHANGE_CHECK_S


def test_e2_a_write_through_a_second_connection_is_seen_after_the_bound(tmp_path):
    path = tmp_path / "p.sqlite3"
    store = ProfileStore(path)
    assert store.translation("app.file", "de") != "Projekt E2"
    with sqlite3.connect(path) as outside:
        outside.execute("UPDATE translations SET text='Projekt E2' "
                        "WHERE key='app.file' AND language='de'")
    outside.close()
    time.sleep(analysis_profiles._EXTERNAL_CHANGE_CHECK_S * 1.5)
    assert store.translation("app.file", "de") == "Projekt E2"


def test_e2_a_write_of_another_store_in_this_process_is_seen_at_once(tmp_path):
    """Negative of the bound: in-process writes never wait for it."""
    store = ProfileStore(tmp_path / "p.sqlite3")
    other = ProfileStore(tmp_path / "p.sqlite3")
    assert store.translation("app.file", "de") != "Projekt sofort"
    other.save_translation("app.file", "de", "Projekt sofort")
    assert store.translation("app.file", "de") == "Projekt sofort"


# ── E4: an unusable profile database is a ProfileError with a remedy ───────

def test_e4_a_corrupt_profile_database_is_a_profile_error(tmp_path):
    bad = tmp_path / "corrupt.sqlite3"
    bad.write_bytes(b"garbage" * 1000)
    with pytest.raises(ProfileError) as info:
        ProfileStore(bad)
    assert str(bad) in str(info.value) and "backup" in str(info.value)
    assert isinstance(info.value.__cause__, sqlite3.DatabaseError)


def test_e4_a_locked_database_names_the_other_program(tmp_path):
    store = ProfileStore(tmp_path / "p.sqlite3")
    text = store._database_problem(sqlite3.OperationalError("database is locked"))
    assert "bloccato" in text and str(store.path) in text
    # Negative: a damaged file gets the restore remedy, not «close the others».
    text = store._database_problem(sqlite3.DatabaseError("file is not a database"))
    assert "bloccato" not in text and "backup" in text


def test_e4_a_healthy_database_still_opens(tmp_path):
    ProfileStore(tmp_path / "p.sqlite3")
    assert ProfileStore(tmp_path / "p.sqlite3").defaults()["model"]


# ── E3: migrations of the accepted-models archive are atomic and guarded ───

_V1_TABLE = ("CREATE TABLE accepted_models (id INTEGER PRIMARY KEY, category TEXT NOT NULL, "
             "basis TEXT NOT NULL, long_mm REAL NOT NULL, short_mm REAL NOT NULL, height_mm REAL, "
             "terminal_count INTEGER, model_ref TEXT NOT NULL, model_name TEXT NOT NULL, "
             "rotation_deg REAL, samples INTEGER NOT NULL DEFAULT 1, first_accepted TEXT NOT NULL, "
             "last_accepted TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', "
             "example_name TEXT NOT NULL DEFAULT ''{extra})")


def _v1_archive(path, half_migrated):
    db = sqlite3.connect(path)
    db.execute(_V1_TABLE.format(extra=", side_uncertainty_mm REAL" if half_migrated else ""))
    db.execute("INSERT INTO accepted_models (category, basis, long_mm, short_mm, model_ref, "
               "model_name, first_accepted, last_accepted) "
               "VALUES ('resistor','body',1.6,0.8,'R.step','R','t','t')")
    db.execute("PRAGMA user_version=1")
    db.commit()
    db.close()


@pytest.mark.parametrize("half_migrated", [True, False])
def test_e3_a_v1_archive_migrates_whether_or_not_its_column_exists(tmp_path, half_migrated):
    path = tmp_path / "a.sqlite3"
    _v1_archive(path, half_migrated)
    store = am.AcceptedModelStore(path)
    with store._connect(create=True):
        pass
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == am.SCHEMA_VERSION
        columns = {row[1] for row in db.execute("PRAGMA table_info(accepted_model_images)")}
    db.close()
    assert {"component_name", "model_rotation_deg"} <= columns
    assert len(store.entries()) == 1


def test_e3_a_failed_migration_leaves_the_archive_as_it_was(tmp_path, monkeypatch):
    path = tmp_path / "a.sqlite3"
    _v1_archive(path, half_migrated=False)
    original = am.AcceptedModelStore._migrate

    def failing(db):
        original(db)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(am.AcceptedModelStore, "_migrate", staticmethod(failing))
    with pytest.raises(am.AcceptedModelsError):
        with am.AcceptedModelStore(path)._connect(create=True):
            pass
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "side_uncertainty_mm" not in {r[1] for r in db.execute(
            "PRAGMA table_info(accepted_models)")}
        assert db.execute("SELECT name FROM sqlite_master WHERE name='accepted_model_images'"
                          ).fetchone() is None
    db.close()


def test_e3_a_read_never_migrates(tmp_path):
    """Negative: reading an old archive leaves its schema untouched."""
    path = tmp_path / "a.sqlite3"
    _v1_archive(path, half_migrated=False)
    assert len(am.AcceptedModelStore(path).entries()) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    db.close()


# ── E7: the crop rows are read once per change of the archive ─────────────

from test_accepted_models import library, _body  # noqa: E402,F401 - pytest fixture
from test_accepted_visual_first import picture, save  # noqa: E402


def _query():
    from kicadgen.services.component_image_match import describe_crop, descriptor_bytes
    return _body(_accepted_image_descriptor=descriptor_bytes(describe_crop(picture())))


def _count_reads(monkeypatch):
    reads = []
    original = am.AcceptedModelStore._connect

    def counted(self, *, create):
        if not create:
            reads.append(1)
        return original(self, create=create)

    monkeypatch.setattr(am.AcceptedModelStore, "_connect", counted)
    return reads


def test_e7_bodies_after_the_first_reuse_the_crop_rows(library, monkeypatch):
    save(library)
    first = am.AcceptedModelStore().find(_query(), [library])
    reads = _count_reads(monkeypatch)
    results = [am.AcceptedModelStore().find(_query(), [library]) for _ in range(5)]
    assert reads == []
    for result in results:
        assert result["visual_score"] == first["visual_score"]
        assert result["entry"] == first["entry"] and result["path"] == first["path"]
    # The returned entry is a copy: annotating it does not touch the cache.
    results[0]["entry"]["model_ref"] = "changed by a caller"
    assert am.AcceptedModelStore().find(_query(), [library])["entry"]["model_ref"] != \
        "changed by a caller"


def test_e7_a_change_of_the_archive_is_read_again(library):
    """Negative: a new example (own write) and a removal by another connection are seen."""
    save(library)
    assert am.AcceptedModelStore().find(_query(), [library])["image_examples"] == 1
    save(library)
    assert am.AcceptedModelStore().find(_query(), [library])["image_examples"] == 2
    path = am.AcceptedModelStore().path
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM accepted_model_images")
    db.close()
    assert am.AcceptedModelStore()._find_visual(_query(), [library], None) is None


# ── E8: a vanished document is an ExtractionError ──────────────────────────

def test_e8_a_vanished_document_is_an_extraction_error(tmp_path):
    missing = tmp_path / "gone.png"
    with pytest.raises(ExtractionError) as info:
        _file_to_content_block(missing)
    assert "gone.png" in str(info.value) and str(tmp_path) not in str(info.value)
    assert isinstance(info.value.__cause__, OSError)


def test_e8_a_readable_document_still_becomes_a_block(tmp_path):
    """Negative: the guard does not change a readable file or the unsupported-format refusal."""
    import cv2
    image = tmp_path / "board.png"
    cv2.imwrite(str(image), picture())
    assert _file_to_content_block(image)["type"] == "image_url"
    other = tmp_path / "notes.txt"
    other.write_text("x")
    with pytest.raises(ExtractionError) as info:
        _file_to_content_block(other)
    assert "notes.txt" in str(info.value)
