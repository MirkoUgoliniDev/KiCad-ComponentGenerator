"""@brief Fixes of review R7 (2026-11-16, fixer C8): services robustness, costs, cancellation.

Every rule has a positive case and a negative case that it must tell apart
for a structural reason (file present or gone, provenance estimated or
measured, cancellation before or without a request ...). No network: every
request is faked through `urllib.request.urlopen` or the module's own seams.
"""
import json
import os
import socket
import threading
import time
import urllib.error

import pytest
from PIL import Image

from kicadgen.services import (ai_diagnostics, component_format, document_roles,
                               kicad_export_history, model_choices, openrouter, project_costs,
                               providers, recent_projects)
from kicadgen.services.analysis_profiles import ProfileError, ProfileStore
from kicadgen.services.i18n import I18n


# ── R7-1: a profile database deleted while the program runs ───────────────────

def test_a_vanished_profile_database_is_not_recreated_and_tr_falls_back(tmp_path):
    """Positive: the file is deleted after the store opened it -> the lookup
    answers from the bundled catalogue, never raises, and no empty database is
    created; the store itself says the file is missing (ProfileError)."""
    path = tmp_path / "profiles.sqlite3"
    store = ProfileStore(path)
    i18n = I18n(store, "en")
    assert i18n.text("Annulla") == "Cancel"
    path.unlink()
    store.invalidate_translations()
    assert i18n.text("Annulla") == "Cancel"
    i18n.set_language("de")
    assert i18n.text("Annulla") == "Abbrechen"
    assert not path.exists()
    with pytest.raises(ProfileError, match="non trovato"):
        store.translation("ui:Annulla", "en")
    assert not path.exists()


def test_a_present_profile_database_is_still_read_for_edited_texts(tmp_path):
    """Negative: with the file in place the store's own (edited) text wins over the bundled one."""
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    store.save_translation("ui:Annulla", "en", "Abort it")
    assert I18n(store, "en").text("Annulla") == "Abort it"


def test_a_new_profile_database_is_still_created_at_start(tmp_path):
    """Negative: the opening in __init__ is the one connection that may create the file."""
    path = tmp_path / "new" / "profiles.sqlite3"
    ProfileStore(path)
    assert path.is_file() and path.stat().st_size > 0


def test_a_store_whose_error_message_is_translated_does_not_recurse():
    """Positive: the store fails and builds its message with tx -> the nested
    lookup answers from the bundled catalogue instead of recursing."""
    from kicadgen.services import messages
    calls = []

    class Failing:
        def translation(self, key, language, fallback=None):
            calls.append(key)
            raise ProfileError(messages.tx("Database profili {path} non trovato.", path="x"))

        def _language_catalog(self, language):
            raise ProfileError("gone")

    i18n = I18n(Failing(), "en")
    messages.set_translator(i18n.raw)
    try:
        assert i18n.text("Annulla") == "Cancel"
        # Only the outer lookup reached the store: the one for its message
        # was answered from the bundled catalogue, no recursion.
        assert calls == ["ui:Annulla"]
        # The reverse index of a retranslation also survives the failing store.
        assert isinstance(i18n._rendered_sources(), dict)
    finally:
        messages.set_translator(None)


# ── R7-2: cost and choice files written durably; a blank cost file ───────────

@pytest.mark.parametrize("blank", [b"", b"\x00" * 512, b"  \r\n"])
def test_a_blank_cost_file_is_set_aside_and_costs_start_afresh(tmp_path, blank):
    """Positive: what a power cut leaves (no content at all) is read as absent,
    then set aside (never deleted) by the next write, which starts afresh."""
    path = tmp_path / project_costs.COSTS_FILE
    path.write_bytes(blank)
    assert project_costs.load_project_costs(tmp_path).calls == 0
    assert path.read_bytes() == blank     # a reader never renames
    costs = project_costs.add_call(tmp_path, {"cost_usd": 0.25, "model": "m"})
    assert costs.calls == 1
    aside = list(tmp_path.glob(project_costs.COSTS_FILE + ".corrupt-*"))
    assert len(aside) == 1 and aside[0].read_bytes() == blank


def test_a_cost_file_with_content_that_is_broken_is_still_refused(tmp_path):
    """Negative: bytes that ARE a damaged history are left for a person, as before."""
    path = tmp_path / project_costs.COSTS_FILE
    path.write_bytes(b'{"format": "kicadgen-ai-costs", "calls": ')
    with pytest.raises(project_costs.ProjectCostsError):
        project_costs.add_call(tmp_path, {"cost_usd": 0.1})
    assert path.read_bytes() == b'{"format": "kicadgen-ai-costs", "calls": '
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_cost_and_choice_writes_are_fsynced_before_the_replace(tmp_path, monkeypatch):
    """Positive: the data is forced to disk before the rename publishes it."""
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    project_costs.add_call(tmp_path, {"cost_usd": 0.1, "model": "m"})
    assert events == ["fsync", "replace"]
    events.clear()
    model_choices.save_choices(tmp_path / "c", b"photo", [{"x_mm": 1}])
    model_choices.save_candidate_report(tmp_path / "c", [{"name": "U1"}])
    assert events == ["fsync", "replace", "fsync", "replace"]
    saved = json.loads((tmp_path / "c" / model_choices.FILENAME).read_text(encoding="utf-8"))
    assert saved["version"] == 1 and len(saved["components"]) == 1
    assert not list((tmp_path / "c").glob(".model-*"))


# ── R7-3: «Copia formato» carries the height's provenance ────────────────────

def _estimated(height):
    return {"height_mm": height, "_height_source": "visual_estimate_not_measured",
            "_height_estimated": True, "_height_basis": "model_visual_estimate",
            "length_mm": 2.0, "width_mm": 1.0}


def _measured(height):
    return {"height_mm": height, "_height_source": "user", "_height_estimated": False,
            "length_mm": 3.0, "width_mm": 2.0}


def test_an_estimated_height_pasted_on_a_measured_body_stays_an_estimate():
    """Positive: the source's estimate does not take the target's «measured» marks."""
    from kicadgen.gui.component_state import height_is_estimated
    result = component_format.paste_format(_measured(1.6), component_format.copy_format(_estimated(0.9)))
    assert result["height_mm"] == 0.9
    assert height_is_estimated(result)
    assert result["_height_source"] == "visual_estimate_not_measured"
    assert result["_height_basis"] == "model_visual_estimate"


def test_a_measured_height_pasted_on_an_estimated_body_is_a_measure():
    """Negative: a measured source gives a measured target, and the target's
    own estimate marks (basis included) are gone, not mixed in."""
    from kicadgen.gui.component_state import height_is_estimated
    result = component_format.paste_format(_estimated(0.9), component_format.copy_format(_measured(1.6)))
    assert result["height_mm"] == 1.6 and not height_is_estimated(result)
    assert "_height_basis" not in result


# ── R7-4: a Cancel arriving before the request is registered ─────────────────

def test_a_sticky_abort_refuses_the_next_request_until_cleared():
    """Positive: abort with no request in flight -> the next registration is refused."""
    me = threading.get_ident()
    try:
        assert openrouter.abort_request(me, sticky=True) is False
        with pytest.raises(openrouter.RequestCancelled):
            with openrouter._ClosableRequest("DeepSeek"):
                pytest.fail("the request must not start")
    finally:
        openrouter.clear_abort(me)
    with openrouter._ClosableRequest() as inflight:
        assert not inflight.aborted


def test_a_plain_abort_without_a_request_leaves_no_trace():
    """Negative: without `sticky` the old contract holds (nothing remembered)."""
    assert openrouter.abort_request(threading.get_ident()) is False
    with openrouter._ClosableRequest() as inflight:
        assert not inflight.aborted


class _Stop(ai_diagnostics.CallCancelled):
    """The test's cancellation."""


def _logged(call, cancel_after=None):
    """make_logged_call around `call`, cancelled `cancel_after` seconds after it starts."""
    started = []

    def cancel():
        if cancel_after is not None and started and time.monotonic() - started[0] >= cancel_after:
            raise _Stop("stop")

    def wrapped():
        started.append(time.monotonic())
        return call()
    return ai_diagnostics.make_logged_call(wrapped, None, cancel=cancel)


def test_a_cancel_during_the_preparation_stops_the_paid_request_before_it_is_sent():
    """Positive: Cancel while the call prepares (no request registered yet) -> never sent."""
    sent = []

    def call():
        time.sleep(0.8)                       # encoding files, resolving the model ...
        with openrouter._ClosableRequest():
            sent.append(1)                    # «urlopen»
        return {"ok": True}
    with pytest.raises(_Stop):
        _logged(call, cancel_after=0.05)()
    assert sent == []
    # The pending cancellation belonged to that attempt only.
    with openrouter._ClosableRequest() as inflight:
        assert not inflight.aborted


def test_without_a_cancel_the_prepared_request_is_sent_once():
    """Negative: same call, no Cancel -> sent once, answer returned."""
    sent = []

    def call():
        time.sleep(0.3)
        with openrouter._ClosableRequest():
            sent.append(1)
        return {"ok": True}
    assert _logged(call)() == {"ok": True}
    assert sent == [1]


# ── R7-5: free credit / balance requests closed by Cancel ────────────────────

def test_a_free_request_is_abandoned_when_its_thread_is_cancelled():
    """Positive: a blocked GET returns RequestCancelled within a fraction of a second."""
    release = threading.Event()
    me = threading.get_ident()
    timer = threading.Timer(0.1, lambda: openrouter.abort_request(me))
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(openrouter.RequestCancelled):
            openrouter.run_abandonable(lambda: release.wait(10), "OpenRouter")
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        timer.cancel()


def test_a_free_request_that_is_not_cancelled_returns_or_raises_as_it_did():
    """Negative: value and exception of the request pass through unchanged."""
    assert openrouter.run_abandonable(lambda: {"a": 1}, "OpenRouter") == {"a": 1}
    error = openrouter.OpenRouterError("x", http_status=401)
    with pytest.raises(openrouter.OpenRouterError) as caught:
        openrouter.run_abandonable(lambda: (_ for _ in ()).throw(error), "OpenRouter")
    assert caught.value is error


def test_a_cancel_during_the_credit_check_stops_the_run_at_once(monkeypatch):
    """Positive: the credit GET hangs, Cancel is pressed -> the cancellation is
    raised quickly, never logged as «credit not verifiable»."""
    monkeypatch.setenv(openrouter.API_KEY_ENV, "sk-or-test")
    release = threading.Event()

    def hanging(*_a, **_k):
        release.wait(10)
        raise urllib.error.URLError(TimeoutError("timed out"))
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", hanging)
    pressed = time.monotonic() + 0.2

    def cancel():
        if time.monotonic() >= pressed:
            raise _Stop("stop")
    log = []
    started = time.monotonic()
    try:
        with pytest.raises(_Stop):
            providers.ensure_providers_ready(["vendor/model"], log.append, cancel=cancel)
        assert time.monotonic() - started < 3.0
        assert not any("non verificabile" in line for line in log)
    finally:
        release.set()


def test_an_unreachable_credit_endpoint_does_not_wait_for_the_second(monkeypatch):
    """Positive: /credits not reached -> /auth/key on the same host not tried."""
    monkeypatch.setenv(openrouter.API_KEY_ENV, "sk-or-test")
    asked = []

    def down(request, timeout=None):
        asked.append(request.full_url)
        raise urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", down)
    with pytest.raises(openrouter.OpenRouterError):
        openrouter.credit_status(force_refresh=True)
    assert asked == [openrouter.CREDITS_URL]


def test_a_refused_credit_endpoint_still_lets_the_second_answer(monkeypatch):
    """Negative: an HTTP refusal is an answer -> the key endpoint is read."""
    import io
    monkeypatch.setenv(openrouter.API_KEY_ENV, "sk-or-test")
    asked = []

    class _Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def serve(request, timeout=None):
        asked.append(request.full_url)
        if request.full_url == openrouter.CREDITS_URL:
            raise urllib.error.HTTPError(request.full_url, 500, "x", {}, io.BytesIO(b"{}"))
        return _Answer(json.dumps({"data": {"limit": 5, "limit_remaining": 4.0}}).encode())
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", serve)
    status = openrouter.credit_status(force_refresh=True)
    assert asked == [openrouter.CREDITS_URL, openrouter.KEY_STATUS_URL]
    assert status.key_remaining == 4.0


# ── R7-6 / R7-7 / R7-8: unknown is never «free», «no limit» or «exhausted» ───

@pytest.mark.parametrize("value", [None, "", "abc", True, [], "-1"])
def test_a_price_that_is_not_a_price_is_unknown_not_free(value):
    """Positive: absent, unreadable, boolean or sentinel prices are NaN."""
    import math
    assert math.isnan(openrouter._parse_price(value))


def test_a_declared_zero_price_is_free_and_a_missing_one_is_not():
    """Negative/positive pair on whole catalogue rows."""
    free = openrouter._model_from_payload({"id": "v/free", "pricing": {"prompt": "0",
                                                                      "completion": "0"}})
    unknown = openrouter._model_from_payload({"id": "v/unknown", "pricing": {}})
    assert free.is_free and not unknown.is_free


def test_an_unread_account_balance_is_not_called_unlimited():
    """Positive: account not read, key without ceiling -> «non verificabile»;
    Negative: both read without limits -> «nessun limite» as before."""
    unread = openrouter.CreditStatus(account_error="connessione fallita").describe()
    assert "non verificabile" in unread and "nessun limite" not in unread
    assert "nessun limite" in openrouter.CreditStatus().describe()


@pytest.mark.parametrize("payload,available", [
    ({"is_available": "false"}, None), ({}, None), ({"is_available": 1}, None),
    ({"is_available": False}, False), ({"is_available": True}, True)])
def test_a_malformed_deepseek_balance_is_unverifiable_and_the_run_proceeds(monkeypatch, payload,
                                                                           available):
    """User decision 2026-11-16. Positive: a malformed answer is unknown -> the
    run proceeds with the «non verificabile» warning, never «esaurito»;
    Negative: a declared false refuses the run as exhausted, a declared true
    passes without the warning."""
    monkeypatch.setattr(providers, "deepseek_request", lambda url, key=None: payload)
    monkeypatch.setattr(providers, "deepseek_api_key", lambda: "sk-test")
    balance = providers.deepseek_balance()
    assert balance.available is available
    log = []
    if available is False:
        with pytest.raises(providers.ProviderError, match="esaurito"):
            providers.ensure_providers_ready([providers.DEEPSEEK_PREFIX + "m"], log.append)
        return
    providers.ensure_providers_ready([providers.DEEPSEEK_PREFIX + "m"], log.append)
    warned = [line for line in log if "non verificabile" in line and "Si prosegue" in line]
    assert len(warned) == (1 if available is None else 0)
    assert not any("esaurito" in line for line in log)
    if available is None:
        assert "non leggibile" in balance.describe() and "esaurito" not in balance.describe()


# ── R7-9: network errors with a reason, never their free text ────────────────

@pytest.mark.parametrize("error,expected", [
    (urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed host.secret")), "DNS"),
    (urllib.error.URLError(ConnectionRefusedError(10061, "refused host.secret")), "rifiutata"),
    (ConnectionResetError(10054, "reset by host.secret"), "chiusa"),
    (urllib.error.URLError(TimeoutError("timed out")), "timed out"),
])
def test_a_network_failure_names_its_category(error, expected):
    """Positive: each socket error class has its category; the free text never shows."""
    text = openrouter.network_reason(error)
    assert expected in text and "secret" not in text
    assert providers._network_reason(error) == text


def test_an_unknown_network_failure_names_only_its_class():
    """Negative: no category -> «omitted» plus the class name, still no free text."""
    text = openrouter.network_reason(OSError("proxy http://user:pw@host.secret"))
    assert "OSError" in text and "secret" not in text and "pw" not in text


def test_a_get_cut_while_waiting_is_a_connection_failure_not_an_undecodable_answer(monkeypatch):
    """Positive: a reset while waiting -> «connessione fallita: ...», unreachable."""
    def reset(*_a, **_k):
        raise ConnectionResetError(10054, "x")
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", reset)
    with pytest.raises(openrouter.OpenRouterError) as caught:
        openrouter._get_json("https://openrouter.invalid/x", 5, key="k-test")
    assert "connessione fallita" in str(caught.value)
    assert "non decodificabile" not in str(caught.value) and caught.value.unreachable


# ── R7-10: the credit cache belongs to the key it was read with ──────────────

def test_the_credit_cache_is_read_again_after_a_key_change(monkeypatch):
    """Positive: a different key reads again; Negative: the same key is served from the cache."""
    import io

    class _Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    seen = []

    def serve(request, timeout=None):
        seen.append(request.get_header("Authorization"))
        return _Answer(json.dumps({"data": {"total_credits": 10, "total_usage": 1}}).encode())
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", serve)
    monkeypatch.setenv(openrouter.API_KEY_ENV, "sk-or-first")
    openrouter.credit_status()
    openrouter.credit_status()
    assert len(seen) == 2              # both endpoints, once
    monkeypatch.setenv(openrouter.API_KEY_ENV, "sk-or-second")
    openrouter.credit_status()
    assert len(seen) == 4 and seen[-1] == "Bearer sk-or-second"
    assert all("sk-or" not in str(part) for part in openrouter._credit_cache[:2])


# ── R7-11: the KiCad export history under a lock ─────────────────────────────

def test_concurrent_export_records_are_all_kept(tmp_path):
    """Positive: eight writers at once -> eight records (one was lost without the lock)."""
    target = tmp_path / "history.json"
    barrier = threading.Barrier(8)
    results = []

    def write(index):
        barrier.wait()
        results.append(kicad_export_history.record_export(
            component=f"C{index}", kind="project", source="s", destination="d", path=target))
    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [True] * 8
    names = {record["component"] for record in kicad_export_history.load_history(target)}
    assert names == {f"C{i}" for i in range(8)}
    assert not (tmp_path / "history.json.lock").exists()


def test_a_history_locked_by_another_writer_is_not_overwritten(tmp_path):
    """Negative: a live lock held elsewhere -> False, the history untouched."""
    target = tmp_path / "history.json"
    assert kicad_export_history.record_export(component="A", kind="k", source="s",
                                              destination="d", path=target)
    before = target.read_bytes()
    (tmp_path / "history.json.lock").write_text("12345")
    assert not kicad_export_history.record_export(component="B", kind="k", source="s",
                                                  destination="d", path=target)
    assert target.read_bytes() == before


# ── R7-12: recent projects on an unreachable drive ───────────────────────────

def test_an_unreachable_recent_project_does_not_block_the_list(tmp_path, monkeypatch):
    """Positive: one check hangs -> the others come back within the budget, in order."""
    paths = [str(tmp_path / name) for name in ("a", "slow", "c")]
    release = threading.Event()

    def is_project(path):
        if path.endswith("slow"):
            release.wait(10)
        return True
    monkeypatch.setattr(recent_projects, "is_project", is_project)
    started = time.monotonic()
    try:
        assert recent_projects.load({"recent_projects": paths}, timeout_s=0.3) == [paths[0], paths[2]]
        assert time.monotonic() - started < 2.0
    finally:
        release.set()


def test_reachable_recent_projects_are_all_listed_in_order(tmp_path, monkeypatch):
    """Negative: every check answers -> every project, in the stored order; non-projects skipped."""
    paths = [str(tmp_path / name) for name in ("a", "b", "gone", "d")]
    monkeypatch.setattr(recent_projects, "is_project", lambda path: not path.endswith("gone"))
    assert recent_projects.load({"recent_projects": paths}) == [paths[0], paths[1], paths[3]]


# ── R7-13: a replaced document that cannot be deleted ────────────────────────

def _picture(path, color):
    Image.new("RGB", (32, 20), color).save(path)
    return path


def test_a_replaced_document_locked_by_a_viewer_does_not_fail_the_import(tmp_path, monkeypatch):
    """Positive: the old file cannot be deleted -> the import still succeeds
    and the manifest lists only the new document."""
    store = document_roles.DocumentSet(tmp_path / "docs")
    old = store.import_file("top", _picture(tmp_path / "old.png", "red"))
    real_unlink = type(old).unlink

    def locked(self, *args, **kwargs):
        if self.name == old.name:
            raise PermissionError(32, "in use")
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(type(old), "unlink", locked)
    new = store.import_file("top", _picture(tmp_path / "new.png", "blue"), replace=old.name)
    paths, roles = document_roles.DocumentSet(store.directory).selected()
    assert paths == [new] and list(roles) == [new.name]
    assert old.exists()            # left in place, in no manifest


def test_a_replaced_document_that_can_be_deleted_is_deleted(tmp_path):
    """Negative: nothing holds the old file -> it is gone, as before."""
    store = document_roles.DocumentSet(tmp_path / "docs")
    old = store.import_file("top", _picture(tmp_path / "old.png", "red"))
    store.import_file("top", _picture(tmp_path / "new.png", "blue"), replace=old.name)
    assert not old.exists()


def test_a_removed_document_locked_by_a_viewer_is_still_removed_from_the_manifest(tmp_path,
                                                                                  monkeypatch):
    """Positive for `remove`: the manifest change stands, no error reaches the GUI."""
    store = document_roles.DocumentSet(tmp_path / "docs")
    doc = store.import_file("top", _picture(tmp_path / "x.png", "red"))
    real_unlink = type(doc).unlink

    def locked(self, *args, **kwargs):
        if self.name == doc.name:
            raise PermissionError(32, "in use")
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(type(doc), "unlink", locked)
    store.remove(doc.name)
    assert document_roles.DocumentSet(store.directory).documents() == []


# ── Per-run usage scope (fixer C6's report) ──────────────────────────────────

def _paid(cost, model="v/m"):
    openrouter._record_usage({"model": model, "usage": {"cost": cost, "prompt_tokens": 1,
                                                        "completion_tokens": 1}})


def test_a_run_in_its_scope_is_not_zeroed_by_another_run_resetting_its_own():
    """Positive: two scoped runs in two threads; the second resets at its start
    and the first keeps its cost; the process-wide tally still sees both."""
    openrouter.reset_usage()
    first, second = openrouter.UsageScope(), openrouter.UsageScope()
    first_paid, second_started = threading.Event(), threading.Event()

    def run_first():
        with first.active():
            openrouter.reset_usage()
            _paid(0.5)
            first_paid.set()
            second_started.wait(5)
            _paid(0.25)

    def run_second():
        first_paid.wait(5)
        with second.active():
            openrouter.reset_usage()          # what the extraction does at its start
            second_started.set()
            _paid(1.0, "v/other")
    threads = [threading.Thread(target=run_first), threading.Thread(target=run_second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert first.totals().cost_usd == pytest.approx(0.75) and first.totals().calls == 2
    assert second.totals().cost_usd == pytest.approx(1.0) and set(second.totals().by_model) == {"v/other"}
    assert openrouter.usage_totals().cost_usd == pytest.approx(1.75)


def test_outside_a_scope_the_process_wide_tally_behaves_as_before():
    """Negative: no scope -> reset_usage and usage_totals act on the shared tally."""
    openrouter.reset_usage()
    _paid(0.1)
    openrouter.record_cached_answer("v/m")
    totals = openrouter.usage_totals()
    assert totals.calls == 1 and totals.cached_by_model == {"v/m": 1}
    openrouter.reset_usage()
    assert openrouter.usage_totals().calls == 0


# ── R12-1 follow-up: the key readers decode a .env as the launcher does ──────

@pytest.mark.parametrize("encoding", ["utf-16", "utf-8-sig", "utf-8"])
def test_a_key_in_a_utf16_or_utf8_env_file_is_found(tmp_path, monkeypatch, encoding):
    """Positive: a .env saved by PowerShell 5.1 or Notepad «Unicode» (UTF-16
    with its mark) gives its keys, like a UTF-8 one, to all three readers."""
    from kicadgen.services import dotenv_keys
    env = tmp_path / ".env"
    env.write_bytes("OPENROUTER_API_KEY=sk-or-file\nDEEPSEEK_API_KEY=sk-ds-file\n".encode(encoding))
    monkeypatch.delenv(openrouter.API_KEY_ENV, raising=False)
    monkeypatch.setattr(openrouter, "env_files", lambda: [env])
    monkeypatch.setattr(providers, "env_files", lambda: [env])
    assert openrouter._dotenv_key() == ("sk-or-file", env)
    assert providers._deepseek_dotenv_key() == ("sk-ds-file", str(env))
    assert dotenv_keys.read_key(env, "DEEPSEEK_API_KEY") == "sk-ds-file"


def test_an_undecodable_env_file_is_skipped_as_before(tmp_path, monkeypatch):
    """Negative: bytes that are neither marked nor UTF-8 (a UTF-16 file without
    its mark, an ANSI file) are skipped -- never decoded by guessing -- and
    the next file is still read."""
    from kicadgen.services import dotenv_keys
    bad = tmp_path / "bad.env"
    bad.write_bytes("OPENROUTER_API_KEY=sk-or-bad\n".encode("utf-16-le"))
    ansi = tmp_path / "ansi.env"
    ansi.write_bytes(b"OPENROUTER_API_KEY=sk-\xe8\n")
    good = tmp_path / "good.env"
    good.write_text("OPENROUTER_API_KEY=sk-or-good\n", encoding="utf-8")
    monkeypatch.delenv(openrouter.API_KEY_ENV, raising=False)
    monkeypatch.setattr(openrouter, "env_files", lambda: [bad, ansi, good])
    assert openrouter._dotenv_key() == ("sk-or-good", good)
    assert dotenv_keys.read_key(bad, "OPENROUTER_API_KEY") == ""
    assert dotenv_keys.read_key(ansi, "OPENROUTER_API_KEY") == ""


# ── Adversarial review of the C8 fixes ───────────────────────────────────────

def _unc(path):
    """The administrative-share UNC spelling of a local absolute path, or None when unusable."""
    drive, rest = os.path.splitdrive(str(path))
    if len(drive) != 2 or drive[1] != ":":
        return None
    unc = "\\\\localhost\\" + drive[0] + "$" + rest
    try:
        return unc if os.path.isdir(os.path.dirname(unc)) else None
    except OSError:
        return None


def test_a_profile_database_on_a_unc_path_is_read(tmp_path):
    """Positive (UNC): a store opened through \\\\localhost\\C$\\... keeps answering
    after start-up -- an SQLite URI made from such a path was refused."""
    folder = tmp_path / "unc"
    folder.mkdir()
    unc = _unc(folder)
    if unc is None:
        pytest.skip("administrative shares are not available on this machine")
    from pathlib import Path
    store = ProfileStore(Path(unc) / "profiles.sqlite3")
    store.save_translation("ui:Annulla", "en", "Abort it")
    store.invalidate_translations()
    assert store.translation("ui:Annulla", "en") == "Abort it"


def test_a_profile_database_in_a_non_ascii_folder_is_read(tmp_path):
    """Positive (non-ASCII path): lookups after start-up still read the file."""
    store = ProfileStore(tmp_path / "profili àèü ß 日本" / "profiles.sqlite3")
    store.save_translation("ui:Annulla", "en", "Abort it")
    store.invalidate_translations()
    assert store.translation("ui:Annulla", "en") == "Abort it"


def test_restoring_a_working_copy_puts_back_a_vanished_database(tmp_path):
    """Positive: the database vanished -> the restore recreates it from the copy.
    Negative: a database changed by someone else is still refused."""
    path = tmp_path / "profiles.sqlite3"
    store = ProfileStore(path)
    copy = tmp_path / "copy.sqlite3"
    store.backup(copy)
    signature = store.contents_signature()
    path.unlink()
    store.restore_from(copy, signature)
    store.invalidate_translations()
    assert path.is_file() and store.translation("ui:Annulla", "en") == "Cancel"
    store.save_translation("ui:Annulla", "en", "changed elsewhere")
    with pytest.raises(ProfileError):
        store.restore_from(copy, signature)


@pytest.mark.parametrize("encoding,mark", [("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"),
                                           ("utf-8", b"\xef\xbb\xbf"), ("utf-8", b"")])
def test_a_key_is_removed_from_a_env_file_in_its_own_encoding(tmp_path, encoding, mark):
    """Positive: «Spostala in Windows» removes the key from a UTF-16 file too, and
    the file keeps its encoding, mark and other lines byte for byte."""
    from kicadgen.services import dotenv_keys
    env = tmp_path / ".env"
    env.write_bytes(mark + "# keep\r\nDEEPSEEK_API_KEY=sk-ds\r\nOTHER=1\r\n".encode(encoding))
    assert dotenv_keys.remove_key_result(env, "DEEPSEEK_API_KEY") == dotenv_keys.REMOVED
    assert env.read_bytes() == mark + "# keep\r\nOTHER=1\r\n".encode(encoding)


def test_a_env_file_without_mark_that_is_not_utf8_is_refused_unchanged(tmp_path):
    """Negative: no mark and not UTF-8 -> OSError, file untouched (never guessed)."""
    from kicadgen.services import dotenv_keys
    env = tmp_path / ".env"
    raw = "DEEPSEEK_API_KEY=sk-ds\n".encode("utf-16-le")
    env.write_bytes(raw)
    with pytest.raises(OSError):
        dotenv_keys.remove_key_result(env, "DEEPSEEK_API_KEY")
    assert env.read_bytes() == raw


def test_a_read_timeout_on_the_balance_still_reads_the_key_ceiling(monkeypatch):
    """Positive for the reachable host: /credits connects then times out while
    reading -> /auth/key is still read, so a key ceiling below the minimum is seen."""
    import io
    monkeypatch.setenv(openrouter.API_KEY_ENV, "sk-or-test")
    asked = []

    class _Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def serve(request, timeout=None):
        asked.append(request.full_url)
        if request.full_url == openrouter.CREDITS_URL:
            raise TimeoutError("timed out")        # read timeout: not wrapped in URLError
        return _Answer(json.dumps({"data": {"limit": 5, "limit_remaining": 0.1}}).encode())
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", serve)
    with pytest.raises(openrouter.OpenRouterError, match="insufficiente"):
        openrouter.check_credit(force_refresh=True)
    assert asked == [openrouter.CREDITS_URL, openrouter.KEY_STATUS_URL]


def test_the_fallback_catalogues_are_reused_while_the_store_fails():
    """Positive: a failing store -> the same empty catalogues, the reverse index
    is built once. Negative: the store answering again -> its own catalogues."""
    state = {"fail": True}
    catalogs = {language: {} for language in ("it", "en", "fr", "de")}

    class Flaky:
        def translation(self, key, language, fallback=None):
            if state["fail"]:
                raise ProfileError("gone")
            return catalogs[language].get(key, fallback)

        def _language_catalog(self, language):
            if state["fail"]:
                raise ProfileError("gone")
            return catalogs[language]

    i18n = I18n(Flaky(), "en")
    first = i18n._store_catalogs()
    assert first is i18n._store_catalogs()
    index = i18n._rendered_sources()
    assert i18n._rendered_sources() is index
    state["fail"] = False
    from kicadgen.services.i18n import _LANGUAGE_ORDER
    answering = i18n._store_catalogs()
    assert answering is not first
    assert all(mine is catalogs[language] for mine, language in zip(answering, _LANGUAGE_ORDER))
