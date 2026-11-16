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

#: Characters KiCad itself mis-handles in a footprint or symbol NAME, which
#: the component name becomes. «%»: KiCad passes the name to the C runtime's
#: printf family as a FORMAT string. Observed with kicad-cli 10.0 on Windows
#: (2026-11-15, `fp export svg` and `sym export svg` of a one-pad footprint
#: and a one-rectangle symbol): «x%NOPE%y», «x%NOPE», «x%n», «x%N» end with
#: 0xC0000409 (the runtime's invalid-parameter fast fail), «x%Zy» and «x%Sy»
#: with 0xC0000005 (access violation), on every run; «x%y», «x%d», «x%s»,
#: «%x» happened to read -- undefined behaviour of a stray conversion, not a
#: safe subset; «x%TEMP%y» read because the defined variable is expanded
#: first. «$», «{», «}» read in every probe («x${NOPE}y», «x{y}»). So the
#: whole character is refused: which sequences crash depends on the runtime
#: and on what lies on its stack, not on the component.
KICAD_UNSAFE_NAME_CHARACTERS = "%"


def kicad_unsafe_characters(name) -> str:
    """@brief The characters of `name` that KiCad mis-handles in a footprint or symbol name.

    @param name Candidate component name.
    @return The distinct characters of `KICAD_UNSAFE_NAME_CHARACTERS` found
        in `name`, in that constant's order; '' for a safe or non-string name.
    """
    if not isinstance(name, str):
        return ""
    return "".join(char for char in KICAD_UNSAFE_NAME_CHARACTERS if char in name)


def is_valid_output_name(name) -> bool:
    """@brief Whether `name` is a plain file name, never a path, that KiCad handles.

    @param name Candidate component name.
    @return False for an empty or non-string name, ``.`` and ``..``, a name
        ending with a dot or a space (Windows strips them, so the file would
        not carry the name written), a name holding a separator, a
        character Windows refuses or a control character, or a character
        KiCad mis-handles in a footprint or symbol name
        (`KICAD_UNSAFE_NAME_CHARACTERS`).
    @note A dot INSIDE the name (``ESP32.module``) is fine: only the pure
        directory names and trailing dots are refused.
    @note CONTRACT CHANGED 2026-11-15 (round 4): «%» is refused too. It is a
        valid Windows file name character, but a footprint or symbol named
        with it makes kicad-cli 10 crash on every read
        (`KICAD_UNSAFE_NAME_CHARACTERS`): a name the program generates must
        be one KiCad can open.
    """
    if not isinstance(name, str) or not name or name in (".", ".."):
        return False
    if name != name.rstrip(" ."):
        return False
    return _FORBIDDEN.search(name) is None and not kicad_unsafe_characters(name)


def validate_output_name(name) -> None:
    """@brief Refuse a component name that is not a plain file name KiCad handles.

    @param name Candidate component name.
    @throws ValueError With the user-facing message when `is_valid_output_name`
        is False; nothing is written before this check in any generator.
    @note Same message as the interface's own check, so a script and the
        window describe the same refusal with the same words. A name that
        is a plain file name but holds a character KiCad mis-handles gets
        its own message naming the characters (a «%» is a valid file name
        character, so «not a valid file name» would not explain it).
    """
    if is_valid_output_name(name):
        return
    unsafe = kicad_unsafe_characters(name)
    # The specific message only when the unsafe characters are the ONLY
    # problem: the same name with them replaced is accepted.
    if unsafe and is_valid_output_name("".join("_" if char in unsafe else char for char in name)):
        raise ValueError(tx("Il nome del componente non può contenere «{characters}»: KiCad legge "
                            "questo carattere nei nomi di footprint e simboli come un codice di "
                            "formato e si chiude.", characters=" ".join(unsafe)))
    raise ValueError(tx("Il nome del componente non è un nome di file valido."))


def fp_filter_pattern(name: str) -> str:
    """@brief The automatic footprint filter of a component: ONE token matching its footprint.

    @param name The component (footprint) name.
    @return ``<name>*`` with every blank replaced by KiCad's single-character
        wildcard «?»: KiCad splits ``ki_fp_filters`` at spaces, so a name with
        spaces would otherwise become several unrelated filters; '' for an
        empty name.
    @note Moved here from `gui.main_window` (2026-11-15) so the installation
        (`services.kicad_library`), which renames a hand-renamed symbol, and
        the window's own rename follow ONE rule; the window re-exports it.
    """
    name = str(name or "").strip()
    return (re.sub(r"\s", "?", name) + "*") if name else ""
