"""@brief Switch-contact role words in pin names, shared by the extraction and the component check.

A relay or switch pin is often NAMED by its role only: «NC», «N.O.»,
«Common», «NA», «NC1». When the program (or the user) settles the role of
such a pin differently from what the model first read, the name must follow
the role, and a name that still spells another role than the pin's function
is a finding (night4 real run, 2026-11-14: a symbol showed a pin named NC
whose function was NO).

Known limit, declared: the vocabulary is NC / NORMALLY CLOSED / NF (French
«normalement fermé») for the normally closed contact, NO / NORMALLY OPEN / NA
(Italian «normalmente aperto») for the normally open one, COM / COMMON for
the common, each optionally followed by a contact number («NC1», «NO 2»,
«COM-1»). Make/break, German Öffner/Schließer and other languages are not
recognised: such a name is left as it is and never reported.
"""
from __future__ import annotations

import re
from typing import Optional

#: The switch roles a name may spell, and the letters (case, spaces and
#: punctuation ignored) that spell them.
ROLE_WORDS = {
    "nc": ("nc", "normallyclosed", "nf"),
    "no": ("no", "normallyopen", "na"),
    "common": ("com", "common"),
}

#: The abbreviation of a role in each spelling family, so that a rename keeps
#: the family: English NC/NO, Italian NC/NA, French NF/NO.
_FAMILY_PAIRS = {"na": ("nc", "na"), "nf": ("nf", "no")}

#: The program's own abbreviation of each role, used towards or from «common».
_ROLE_TEXT = {"nc": "NC", "no": "NO", "common": "COM"}

_SUFFIX = re.compile(r"^(?P<word>.*?)(?P<suffix>[\s._\-]*\d+)$")


def _split(name: str):
    """(word part, contact-number suffix) of a name; the suffix is "" without a trailing number."""
    match = _SUFFIX.match(name)
    if match and match.group("word").strip(" ._-"):
        return match.group("word"), match.group("suffix")
    return name, ""


def role_word_of(name) -> Optional[tuple]:
    """@brief The switch role a pin name merely spells, with its contact-number suffix.

    @param name A pin name (any value).
    @return (role, suffix) -- role one of "nc", "no", "common", suffix the
        trailing contact number as written («1», « 2», «-1») or "" -- when
        the name is only a role word (ROLE_WORDS) optionally followed by a
        number; None for any other name («A1», «OUT_A», «GPIO19», «Coil»).
    """
    if not isinstance(name, str) or not name.strip():
        return None
    word, suffix = _split(name.strip())
    if any(ch.isdigit() for ch in word):
        return None
    key = re.sub(r"[^a-z]", "", word.lower())
    role = next((role for role, words in ROLE_WORDS.items() if key in words), None)
    return (role, suffix) if role else None


def renamed_role_word(name: str, new_role: str) -> str:
    """@brief A role-word pin name rewritten for `new_role` in the same style.

    @param name The pin name; returned unchanged unless `role_word_of` reads it.
    @param new_role "nc", "no" or "common".
    @return The name with its role word replaced: case, dots and the contact
        number kept («N.C.» -> «N.O.», «nc1» -> «no1»); the spelling family
        kept for the abbreviations (Italian NA <-> NC, French NF <-> NO;
        NC <-> NO otherwise, the English pair, since a name alone cannot tell
        English from Italian); long forms swap «closed»/«open»; towards or
        from «common» the abbreviation COM / NC / NO in the name's case.
    """
    read = role_word_of(name)
    if read is None or read[0] == new_role or new_role not in _ROLE_TEXT:
        return name
    old_role, suffix = read
    word = name[:len(name) - len(suffix)] if suffix else name
    key = re.sub(r"[^a-z]", "", word.lower())
    if {old_role, new_role} == {"nc", "no"}:
        if key in ("normallyclosed", "normallyopen"):
            source, target = ("closed", "open") if old_role == "nc" else ("open", "closed")
            match = re.search(source, word, re.IGNORECASE)
            if match:
                found = match.group(0)
                new = (target.upper() if found.isupper() else target.capitalize()
                       if found[0].isupper() else target)
                return word[:match.start()] + new + word[match.end():] + suffix
        # An abbreviation: the pair of its family, letter by letter in its style.
        pair = _FAMILY_PAIRS.get(key, ("nc", "no"))
        target = pair[0] if new_role == "nc" else pair[1]
        letters = [index for index, ch in enumerate(word) if ch.isalpha()]
        if len(letters) == len(target):
            chars = list(word)
            for index, letter in zip(letters, target):
                chars[index] = letter.upper() if word[index].isupper() else letter
            return "".join(chars) + suffix
    text = _ROLE_TEXT[new_role]
    letters = "".join(ch for ch in word if ch.isalpha())
    return (text.lower() if letters.islower() else text) + suffix


def role_conflicts(entries) -> list:
    """@brief The pins whose name spells a switch role different from their function.

    @param entries A pinout: dicts with "pin", "name" and "function".
    @return [{"pin", "name", "function"}] for every pin whose name is a role
        word (`role_word_of`) and whose function is a DIFFERENT switch role;
        empty otherwise. A function that is no switch role (coil, other) is
        not judged.
    """
    found = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        read = role_word_of(entry.get("name"))
        function = entry.get("function")
        if read is not None and function in ROLE_WORDS and read[0] != function:
            found.append({"pin": str(entry.get("pin")).strip(), "name": entry.get("name"),
                          "function": function})
    return found
