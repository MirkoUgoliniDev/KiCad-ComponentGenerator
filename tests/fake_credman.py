"""@brief In-memory stand-in for the four advapi32 credential functions.

`tests/conftest.py` installs it for every test, so the default run never
reads or writes the real Windows Credential Manager (and never sees the
developer's real key). It speaks the same ctypes protocol as advapi32:
CredReadW fills the caller's CREDENTIALW pointer with a buffer that only
CredFree releases, and failures are reported through the last-error slot.
"""
import ctypes

from kicadgen.services import credentials

ERROR_NOT_FOUND = 1168


class FakeAdvapi32:
    """@brief A credential vault held in a dict, with call accounting."""

    def __init__(self):
        """@brief Start empty, with no forced failure."""
        self.vault = {}
        self.outstanding = {}
        self.freed = []
        self.fail_code = None
        self.last_written = None

    def __call__(self):
        """@brief Stand in for `credentials._advapi32()`, which returns the library."""
        return self

    def _fail(self):
        code = self.fail_code
        if code is None:
            return False
        ctypes.set_last_error(code)
        return True

    def CredWriteW(self, credential_ref, flags):  # noqa: N802 - Windows API name
        """@brief Keep a copy of the blob, as Windows would."""
        if self._fail():
            return 0
        credential = credential_ref._obj
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        self.vault[credential.TargetName] = blob
        self.last_written = {"type": credential.Type, "persist": credential.Persist,
                             "user": credential.UserName, "flags": flags}
        return 1

    def CredReadW(self, target, kind, flags, pointer_ref):  # noqa: N802
        """@brief Hand out a fresh CREDENTIALW that the caller must CredFree."""
        if self._fail():
            return 0
        if target not in self.vault:
            ctypes.set_last_error(ERROR_NOT_FOUND)
            return 0
        blob = self.vault[target]
        buffer = ctypes.create_string_buffer(blob, max(len(blob), 1))
        credential = credentials._CREDENTIALW()
        credential.Type = kind
        credential.TargetName = target
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        pointer_ref._obj.contents = credential
        self.outstanding[ctypes.addressof(credential)] = (credential, buffer)
        return 1

    def CredDeleteW(self, target, kind, flags):  # noqa: N802
        """@brief Forget the target, or report ERROR_NOT_FOUND."""
        if self._fail():
            return 0
        if self.vault.pop(target, None) is None:
            ctypes.set_last_error(ERROR_NOT_FOUND)
            return 0
        return 1

    def CredFree(self, pointer):  # noqa: N802
        """@brief Release a buffer handed out by CredReadW (NULL is accepted)."""
        address = ctypes.cast(pointer, ctypes.c_void_p).value if pointer else None
        if address is not None:
            self.outstanding.pop(address)
        self.freed.append(address)
