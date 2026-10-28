"""@brief One rule for the file name a generated component may carry.

The component name becomes a folder (``<name>.3dshapes``) and the stem of
every generated file (STEP, WRL, preview JSON, FCStd, footprint, symbol).
Joined to an output folder unvalidated, a name such as ``..\\evil`` escapes
that folder (2026-10-27 review, H4). The graphical interface already refused
such names (`main_window._validate_output_name`); this module is the shared
rule so that script callers of the 3D generator are protected by the very
same criterion, not by a second copy of it.

@note It lives in `kicadgen.core` because the parameter validators of the
    footprint generators and of `core.package_geometry` apply it too, and core
    must not depend on the 3D layer; `kicadgen.cad3d.output_names` re-exports
    it for the 3D generator and older importers.
"""

from __future__ import annotations

import re

from ..services.messages import tx

#: Characters Windows refuses in a file name, plus the control characters;
#: the path separators of every platform are among them. The rule is the
#: strictest of the supported platforms, so a name accepted here is a plain
#: file name everywhere.
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def is_valid_output_name(name) -> bool:
    """@brief Whether `name` is a plain file name, never a path.

    @param name Candidate component name.
    @return False for an empty or non-string name, ``.`` and ``..``, a name
        ending with a dot or a space (Windows strips them, so the file would
        not carry the name written), or a name holding a separator, a
        character Windows refuses or a control character.
    @note A dot INSIDE the name (``ESP32.module``) is fine: only the pure
        directory names and trailing dots are refused.
    """
    if not isinstance(name, str) or not name or name in (".", ".."):
        return False
    if name != name.rstrip(" ."):
        return False
    return _FORBIDDEN.search(name) is None


def validate_output_name(name) -> None:
    """@brief Refuse a component name that is not a plain file name.

    @param name Candidate component name.
    @throws ValueError With the user-facing message when `is_valid_output_name`
        is False; nothing is written before this check in any generator.
    @note Same message as the interface's own check, so a script and the
        window describe the same refusal with the same words.
    """
    if not is_valid_output_name(name):
        raise ValueError(tx("Il nome del componente non è un nome di file valido."))
