"""@brief Windows Credential Manager wrapper, through the ctypes protocol of advapi32.

The default run never touches the real Credential Manager: `conftest.py`
installs `fake_credman.FakeAdvapi32`, which speaks the same ctypes protocol
(CREDENTIALW filled through the caller's pointer, CredFree, last error). One
real round trip exists, skipped unless KICADGEN_REAL_CREDMAN=1.
"""
import os
import sys
import uuid

import pytest

from kicadgen.services import credentials

SECRET = "sk-or-v1-segreto-di-prova-àé€"


@pytest.fixture
def vault(monkeypatch):
    """The in-memory advapi32 installed by conftest, on a Windows-like platform."""
    monkeypatch.setattr(sys, "platform", "win32")
    return credentials._advapi32


def test_round_trip_stores_utf16le_and_frees_every_buffer(vault):
    """@brief Positivo: scrittura, lettura e cancellazione, con blob UTF-16-LE e CredFree."""
    assert credentials.read_secret("KiCadGen/Test") is None
    credentials.write_secret("KiCadGen/Test", SECRET)
    assert vault.vault["KiCadGen/Test"] == SECRET.encode("utf-16-le")
    assert vault.last_written["type"] == 1       # CRED_TYPE_GENERIC
    assert vault.last_written["persist"] == 2    # CRED_PERSIST_LOCAL_MACHINE
    assert credentials.read_secret("KiCadGen/Test") == SECRET
    assert credentials.read_secret("KiCadGen/Test") == SECRET
    # Every buffer handed out by CredReadW went back through CredFree.
    assert vault.outstanding == {}
    assert len([address for address in vault.freed if address]) == 2
    assert credentials.delete_secret("KiCadGen/Test") is True
    assert credentials.read_secret("KiCadGen/Test") is None


def test_a_second_write_replaces_the_first(vault):
    credentials.write_secret("KiCadGen/Test", "prima")
    credentials.write_secret("KiCadGen/Test", "seconda")
    assert credentials.read_secret("KiCadGen/Test") == "seconda"


def test_not_found_is_none_and_deleting_nothing_is_false(vault):
    """@brief Negativo: ERROR_NOT_FOUND non e' un guasto, ne' in lettura ne' in cancellazione."""
    assert credentials.read_secret("KiCadGen/Assente") is None
    assert credentials.delete_secret("KiCadGen/Assente") is False
    assert vault.outstanding == {}


def test_a_terminating_nul_written_by_another_tool_is_dropped(vault):
    vault.vault["KiCadGen/Test"] = "abc".encode("utf-16-le") + b"\x00\x00"
    assert credentials.read_secret("KiCadGen/Test") == "abc"


def test_an_odd_length_blob_is_an_error_and_the_buffer_is_still_freed(vault):
    """@brief Un blob non UTF-16 (lunghezza dispari) e' un errore chiaro, e CredFree avviene comunque."""
    vault.vault["KiCadGen/Test"] = b"abc"
    with pytest.raises(credentials.CredentialError, match="KiCadGen/Test"):
        credentials.read_secret("KiCadGen/Test")
    assert vault.outstanding == {}


def test_an_unpaired_surrogate_is_an_error_not_a_crash(vault):
    vault.vault["KiCadGen/Test"] = b"\x00\xd8"   # lone high surrogate U+D800
    with pytest.raises(credentials.CredentialError):
        credentials.read_secret("KiCadGen/Test")
    assert vault.outstanding == {}


@pytest.mark.parametrize("operation", ["read", "write", "delete"])
def test_a_windows_failure_carries_its_code_and_never_the_secret(vault, operation):
    """@brief Ogni errore di Windows diventa CredentialError con il codice, senza la chiave."""
    vault.vault["KiCadGen/Test"] = SECRET.encode("utf-16-le")
    vault.fail_code = 5   # ERROR_ACCESS_DENIED
    with pytest.raises(credentials.CredentialError) as caught:
        if operation == "read":
            credentials.read_secret("KiCadGen/Test")
        elif operation == "write":
            credentials.write_secret("KiCadGen/Test", SECRET)
        else:
            credentials.delete_secret("KiCadGen/Test")
    assert caught.value.code == 5
    assert "5" in str(caught.value)
    assert SECRET not in str(caught.value)
    assert vault.outstanding == {}


def test_empty_or_oversized_secrets_are_refused_before_windows(vault):
    """@brief Negativo: niente di vuoto o piu' lungo di CRED_MAX_CREDENTIAL_BLOB_SIZE arriva a Windows."""
    with pytest.raises(credentials.CredentialError):
        credentials.write_secret("KiCadGen/Test", "")
    with pytest.raises(credentials.CredentialError) as caught:
        credentials.write_secret("KiCadGen/Test", "x" * 1281)   # 2562 bytes > 2560
    assert "x" * 50 not in str(caught.value)
    assert vault.vault == {} and vault.last_written is None
    credentials.write_secret("KiCadGen/Test", "x" * 1280)       # exactly 2560 bytes
    assert credentials.read_secret("KiCadGen/Test") == "x" * 1280


def test_off_windows_reading_is_none_and_writing_is_a_clear_error(monkeypatch):
    """@brief Su un sistema non Windows: lettura None, scrittura e cancellazione CredentialError."""
    monkeypatch.setattr(sys, "platform", "linux")
    vault = credentials._advapi32
    assert credentials.is_supported() is False
    assert credentials.read_secret("KiCadGen/Test") is None
    with pytest.raises(credentials.CredentialError):
        credentials.write_secret("KiCadGen/Test", SECRET)
    with pytest.raises(credentials.CredentialError):
        credentials.delete_secret("KiCadGen/Test")
    assert vault.vault == {} and vault.last_written is None


def test_a_library_that_cannot_be_loaded_is_a_credential_error(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def broken():
        raise OSError("advapi32 missing")
    monkeypatch.setattr(credentials, "_advapi32", broken)
    for call in (lambda: credentials.read_secret("T"),
                 lambda: credentials.write_secret("T", SECRET),
                 lambda: credentials.delete_secret("T")):
        with pytest.raises(credentials.CredentialError):
            call()


@pytest.mark.skipif(os.environ.get("KICADGEN_REAL_CREDMAN") != "1" or sys.platform != "win32",
                    reason="real Credential Manager only with KICADGEN_REAL_CREDMAN=1 on Windows")
def test_real_credential_manager_round_trip(monkeypatch):
    """@brief Prova vera, opzionale: una voce usa-e-getta, sempre cancellata nel finally."""
    monkeypatch.setattr(credentials, "_advapi32", credentials._load_advapi32)
    target = f"KiCadGen/test-{uuid.uuid4().hex}"
    try:
        assert credentials.read_secret(target) is None
        credentials.write_secret(target, SECRET)
        assert credentials.read_secret(target) == SECRET
        credentials.write_secret(target, "sostituito")
        assert credentials.read_secret(target) == "sostituito"
    finally:
        deleted = credentials.delete_secret(target)
    assert deleted is True
    assert credentials.read_secret(target) is None
    assert credentials.delete_secret(target) is False
