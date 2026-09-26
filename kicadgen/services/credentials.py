"""@brief Store, read and delete one secret in the Windows Credential Manager.

The OpenRouter API key used to live only in a hand-written ``.env`` file
(PIANO-FIX item 2): a freshly installed user had to find and edit a file
before the first analysis. This module keeps the key in the Credential
Manager of the current Windows user instead, as a *generic* credential,
through ``advapi32`` and ``ctypes``: no third-party dependency.

@note Security boundary: the secret is never logged, never put in an
    exception message and never returned by anything but `read_secret`. The
    credential blob is stored as UTF-16-LE without terminator, the encoding
    Windows' own tools (``cmdkey``) use. Windows protects it with the user's
    logon credentials (DPAPI); anyone running code as that user can read it,
    exactly as with the ``.env`` file, but it is no longer a plain file that
    ends up in backups, archives or a repository by mistake.
@note Failure contract: on a system other than Windows `read_secret`
    returns None and the writing functions raise `CredentialError`; every
    Windows failure becomes a `CredentialError` with the Windows error code.
    No other exception type leaves this module, so a caller that catches
    `CredentialError` can never be brought down by the credential store.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Optional

from .messages import tx

#: Target name of the OpenRouter key in the Credential Manager, as shown in
#: «Gestione credenziali» -> «Credenziali generiche».
OPENROUTER_TARGET = "KiCadGen/OpenRouter"

#: User name recorded with the credential: only a label, Windows shows it
#: under the target name. It is not a secret.
_USER_NAME = "KiCadGen"

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
#: CRED_MAX_CREDENTIAL_BLOB_SIZE (5 * 512 bytes) for generic credentials.
_MAX_BLOB_BYTES = 5 * 512


class CredentialError(Exception):
    """@brief Report a Credential Manager failure without exposing the secret.

    @param message Translated, user-readable text; never contains the secret.
    @param code Windows error code (GetLastError), or None when the failure
        did not come from a Windows call.
    """

    def __init__(self, message: str, code: Optional[int] = None):
        """@brief Keep the message and the optional Windows error code.

        @param message Translated text shown to the user.
        @param code Windows error code, or None.
        """
        super().__init__(message)
        self.code = code


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIALW(ctypes.Structure):
    """Layout of CREDENTIALW (wincred.h); the field order is the ABI."""

    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)
_LIBRARY = None


def _load_advapi32():
    """The real advapi32 with typed signatures, loaded once.

    Calls go through `_advapi32`, which tests replace with an in-memory fake:
    the default test run never touches the real Credential Manager.
    """
    global _LIBRARY
    if _LIBRARY is None:
        library = ctypes.WinDLL("advapi32", use_last_error=True)
        library.CredWriteW.argtypes = [_PCREDENTIALW, wintypes.DWORD]
        library.CredWriteW.restype = wintypes.BOOL
        library.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.POINTER(_PCREDENTIALW)]
        library.CredReadW.restype = wintypes.BOOL
        library.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        library.CredDeleteW.restype = wintypes.BOOL
        library.CredFree.argtypes = [ctypes.c_void_p]
        library.CredFree.restype = None
        _LIBRARY = library
    return _LIBRARY


_advapi32 = _load_advapi32


def is_supported() -> bool:
    """@brief Tell whether this system has a Windows Credential Manager.

    @return True on Windows, False elsewhere.
    """
    return sys.platform == "win32"


def _library(failure: str):
    """advapi32, or CredentialError(`failure`) when it cannot be loaded."""
    if not is_supported():
        raise CredentialError(tx("Gestore credenziali di Windows non disponibile su "
                                 "questo sistema."))
    try:
        return _advapi32()
    except (OSError, AttributeError) as exc:
        raise CredentialError(tx("{failure} ({exc}).", failure=failure,
                                 exc=f"advapi32: {type(exc).__name__}")) from None


def _windows_error(failure: str, code: int) -> CredentialError:
    """CredentialError for a failed Windows call, with its code and system text."""
    try:
        detail = (ctypes.FormatError(code).strip().rstrip(".")
                  if hasattr(ctypes, "FormatError") else "")
    except (OSError, ValueError):
        detail = ""
    return CredentialError(tx("{failure} (errore Windows {code}: {detail}).",
                              failure=failure, code=code, detail=detail or "?"), code)


def read_secret(target: str) -> Optional[str]:
    """@brief Read the secret stored under `target`.

    @param target Credential target name, e.g. `OPENROUTER_TARGET`.
    @return The secret, or None when nothing is stored there or the system
        is not Windows.
    @throws CredentialError When Windows refuses the read or the stored blob
        is not valid UTF-16 text. The message never contains the secret.
    @note The buffer returned by CredReadW is always released with CredFree,
        also when decoding fails.
    """
    if not is_supported():
        return None
    failure = tx("Lettura dal Gestore credenziali di Windows non riuscita")
    library = _library(failure)
    pointer = _PCREDENTIALW()
    try:
        ok = library.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer))
    except (OSError, ctypes.ArgumentError) as exc:
        raise CredentialError(tx("{failure} ({exc}).", failure=failure,
                                 exc=type(exc).__name__)) from None
    if not ok:
        code = ctypes.get_last_error()
        if code == _ERROR_NOT_FOUND:
            return None
        raise _windows_error(failure, code)
    try:
        credential = pointer.contents
        size = int(credential.CredentialBlobSize)
        blob = ctypes.string_at(credential.CredentialBlob, size) if size else b""
    except (ValueError, OSError) as exc:
        raise CredentialError(tx("{failure} ({exc}).", failure=failure,
                                 exc=type(exc).__name__)) from None
    finally:
        library.CredFree(pointer)
    if len(blob) % 2:
        raise CredentialError(tx("La voce {target} del Gestore credenziali non contiene "
                                 "un testo valido.", target=target))
    try:
        # A writer that included the terminating NUL leaves it in the blob.
        return blob.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        raise CredentialError(tx("La voce {target} del Gestore credenziali non contiene "
                                 "un testo valido.", target=target)) from None


def write_secret(target: str, secret: str) -> None:
    """@brief Create or replace the secret stored under `target`.

    @param target Credential target name, e.g. `OPENROUTER_TARGET`.
    @param secret Non-empty text; stored as UTF-16-LE.
    @throws CredentialError When the system is not Windows, the secret is
        empty or longer than the Credential Manager allows, or Windows
        refuses the write.
    @note Persistence is CRED_PERSIST_LOCAL_MACHINE: the credential survives
        logoff and reboot on this computer and does not roam to others. The
        temporary blob buffer is zeroed after the call.
    """
    if not isinstance(secret, str) or not secret:
        raise CredentialError(tx("La chiave è vuota."))
    blob = secret.encode("utf-16-le")
    if len(blob) > _MAX_BLOB_BYTES:
        raise CredentialError(tx("Chiave troppo lunga per il Gestore credenziali di Windows "
                                 "({size} byte, massimo {limit}).",
                                 size=len(blob), limit=_MAX_BLOB_BYTES))
    failure = tx("Salvataggio nel Gestore credenziali di Windows non riuscito")
    library = _library(failure)
    buffer = ctypes.create_string_buffer(blob, len(blob))
    try:
        credential = _CREDENTIALW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = _USER_NAME
        try:
            ok = library.CredWriteW(ctypes.byref(credential), 0)
        except (OSError, ctypes.ArgumentError) as exc:
            raise CredentialError(tx("{failure} ({exc}).", failure=failure,
                                     exc=type(exc).__name__)) from None
        if not ok:
            raise _windows_error(failure, ctypes.get_last_error())
    finally:
        ctypes.memset(buffer, 0, len(blob))


def delete_secret(target: str) -> bool:
    """@brief Delete the secret stored under `target`.

    @param target Credential target name, e.g. `OPENROUTER_TARGET`.
    @return True when a secret was deleted, False when there was none.
    @throws CredentialError When the system is not Windows or Windows
        refuses the deletion.
    """
    failure = tx("Rimozione dal Gestore credenziali di Windows non riuscita")
    library = _library(failure)
    try:
        ok = library.CredDeleteW(target, _CRED_TYPE_GENERIC, 0)
    except (OSError, ctypes.ArgumentError) as exc:
        raise CredentialError(tx("{failure} ({exc}).", failure=failure,
                                 exc=type(exc).__name__)) from None
    if ok:
        return True
    code = ctypes.get_last_error()
    if code == _ERROR_NOT_FOUND:
        return False
    raise _windows_error(failure, code)
