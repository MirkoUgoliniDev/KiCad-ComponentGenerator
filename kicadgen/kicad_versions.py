"""@brief Order KiCad installation folders by their numeric version.

KiCad installs one folder per major version (``9.0``, ``10.0``). A plain text
sort ranks ``9.0`` above ``10.0``, so the choice of "the newest" installation
must compare parsed numbers instead. This module has no dependency on the
rest of the package, so both ``kicadgen.services`` and ``kicadgen.cad3d`` can
use it.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional, TypeVar

T = TypeVar("T")

_NUMERIC_VERSION = re.compile(r"\d+(?:\.\d+)*")


def parse_version(name: str) -> Optional[tuple[int, ...]]:
    """@brief Parse a dotted numeric folder name into a comparable tuple.

    @param name Folder name such as ``"10.0"`` or ``"10.0.1"``.
    @return The integer components, or None when the name is not a dotted
        number (for example ``"nightly"``).
    """
    text = (name or "").strip()
    if not _NUMERIC_VERSION.fullmatch(text):
        return None
    return tuple(int(part) for part in text.split("."))


def newest_version_first(items: Iterable[T],
                         version_name: Callable[[T], str] = str) -> list[T]:
    """@brief Sort items so the highest numeric version comes first.

    @param items Folders, paths or names to order.
    @param version_name Maps an item to the folder name holding its version.
    @return Items with a numeric version, highest first (``10.0.1`` > ``10.0``
        > ``9.0``), followed by items whose version is not numeric, in
        reverse text order, so they are still offered but never preferred.
    @note Ties are broken by the item's text so the result is deterministic.
    """
    numeric: list[tuple[tuple[int, ...], str, T]] = []
    other: list[tuple[str, T]] = []
    for item in items:
        version = parse_version(version_name(item))
        if version is None:
            other.append((str(item), item))
        else:
            numeric.append((version, str(item), item))
    numeric.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    other.sort(key=lambda entry: entry[0], reverse=True)
    return [entry[-1] for entry in numeric] + [entry[-1] for entry in other]
