"""@brief KiCad-style substring filter over every solid of the installed 3D libraries.

@details The candidate dialog proposes at most five ranked models. When the
wanted solid is not among them, browsing thousands of files by folder is the
wrong tool: KiCad itself lets the user type a few fragments and filters the
footprint or symbol list as they type. This module gives the same experience
for the 3D library: one lazy walk of every discovered root, cached per
process, and a token filter over ``library/stem``.

Root discovery is not duplicated here: :func:`discovered_roots` delegates to
``kicad_models._search_roots`` so the environment override, the vendor
installation and the user repositories (``*.3dshapes`` folders under
``3rdparty``) stay defined in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import os
from pathlib import Path
from typing import Iterable, Optional

from ..cad3d.kicad_models import _search_roots

#: Solid geometry, the only format the CAD importer can attach to a component.
STEP_SUFFIXES = (".step", ".stp")

#: Display mesh, listed only when the library ships no solid twin for it.
MESH_SUFFIXES = (".wrl",)

#: Upper bound on the rows returned by one filter. A list longer than this is
#: not something a person scans: the query has to be narrowed, and the flag on
#: the result says so. It is a presentation budget, not a matching threshold.
MAX_RESULTS = 200

#: Process-wide index keyed by the tuple of walked roots, so two dialogs on
#: the same library never walk it twice.
_CACHE: dict[tuple[str, ...], list["LibraryModel"]] = {}


@dataclass(frozen=True)
class LibraryModel:
    """@brief One model file of the installed KiCad 3D libraries.

    @note ``library`` is the name of the enclosing ``*.3dshapes`` folder, or
    the root folder name when a repository keeps files outside such a folder.
    """

    path: Path
    library: str
    stem: str

    @property
    def label(self) -> str:
        """@brief Return the ``library/stem`` text shown and filtered in the GUI.

        @return "<library>/<stem>".
        """
        return f"{self.library}/{self.stem}"


class FilterResult(list):
    """@brief Filter rows plus the size of the complete match set.

    @note Behaves as a plain ``list[LibraryModel]``; ``total`` counts every
    match before the cap and ``truncated`` tells whether rows were dropped.
    """

    def __init__(self, models: Iterable[LibraryModel] = (), total: int = 0):
        """@brief Wrap the capped rows.

        @param models Rows kept after the cap, already sorted.
        @param total Number of matches before the cap.
        """
        super().__init__(models)
        self.total = int(total)

    @property
    def truncated(self) -> bool:
        """@brief Whether the complete match set exceeded ``MAX_RESULTS``.

        @return True when `total` is larger than the entries kept.
        """
        return self.total > len(self)


def discovered_roots() -> list[Path]:
    """@brief Return every installed KiCad 3D library root, primary first.

    @return Existing root directories; empty when no library is installed.
    @note Delegates to the discovery of ``kicad_models`` so overrides and user
    repositories are recognised identically by matching and by search.
    """
    return [Path(root) for root in _search_roots(None)]


def _library_name(directory: Path, root: Path) -> str:
    """Nearest ``*.3dshapes`` ancestor up to the root, else the root name."""
    for folder in (directory, *directory.parents):
        if folder.name.lower().endswith(".3dshapes"):
            return folder.name
        if folder == root:
            break
    return root.name


def _walk_root(root: Path) -> list[LibraryModel]:
    """Collect solids of one root; a mesh is listed only without a solid twin.

    Unreadable directories are skipped one by one through the ``os.walk``
    error hook, so a permission problem in a sub-folder never hides the rest
    of the library.
    """
    root = Path(root)
    found: list[LibraryModel] = []
    try:
        walker = os.walk(root, onerror=lambda error: None)
        for directory, _folders, files in walker:
            folder = Path(directory)
            solids = {Path(name).stem.casefold() for name in files
                      if Path(name).suffix.lower() in STEP_SUFFIXES}
            library = _library_name(folder, root)
            for name in files:
                suffix = Path(name).suffix.lower()
                stem = Path(name).stem
                if suffix in STEP_SUFFIXES:
                    found.append(LibraryModel(folder / name, library, stem))
                elif suffix in MESH_SUFFIXES and stem.casefold() not in solids:
                    found.append(LibraryModel(folder / name, library, stem))
    except OSError:
        return []
    return found


def library_models(roots: Optional[Iterable[os.PathLike | str]] = None,
                   refresh: bool = False) -> list[LibraryModel]:
    """@brief Index every model of the given roots, walking each root once per process.

    @param roots Library roots to walk; None uses :func:`discovered_roots`.
    @param refresh True discards the cached index for these roots and walks again.
    @return Models sorted by library then stem, case-insensitively.
    @note ``.step``/``.stp`` files are listed in any letter case. A ``.wrl``
    mesh is listed only when the same folder holds no solid with the same
    stem: the mesh cannot be attached as a solid, so it is offered only when
    it is the sole evidence that the model exists. Roots that cannot be read
    are skipped, never raised.
    """
    root_list = [Path(root) for root in (discovered_roots() if roots is None else roots)]
    key = tuple(str(root) for root in root_list)
    if refresh:
        _CACHE.pop(key, None)
    cached = _CACHE.get(key)
    if cached is not None:
        return list(cached)
    models: list[LibraryModel] = []
    seen: set[str] = set()
    for root in root_list:
        for model in _walk_root(root):
            identity = str(model.path)
            if identity in seen:
                continue
            seen.add(identity)
            models.append(model)
    models.sort(key=lambda model: (model.library.casefold(), model.stem.casefold(), str(model.path)))
    _CACHE[key] = list(models)
    return models


def _token_matches(haystack: str, token: str) -> bool:
    """Substring test; ``*``/``?`` turn the token into a glob matched anywhere."""
    if "*" in token or "?" in token:
        return fnmatch.fnmatchcase(haystack, "*" + token + "*")
    return token in haystack


def filter_models(models: Iterable[LibraryModel], query: str) -> FilterResult:
    """@brief Apply a KiCad-style filter: every token must occur in ``library/stem``.

    @param models Indexed models, typically from :func:`library_models`.
    @param query Whitespace-separated fragments; case-insensitive. ``*`` and
    ``?`` act as wildcards inside a token.
    @return Matches sorted by (stem length, stem, library), capped at
    ``MAX_RESULTS``; ``total`` and ``truncated`` describe the complete set.
    @note An empty or blank query returns no rows: listing the whole library
    is never useful and would cost thousands of list items.
    """
    tokens = [token.casefold() for token in str(query or "").split()]
    if not tokens:
        return FilterResult((), 0)
    matches = [model for model in models
               if all(_token_matches(model.label.casefold(), token) for token in tokens)]
    # Shorter stems first: a longer name is almost always a more specific
    # variant, and the plain name is what a user typing a fragment expects.
    matches.sort(key=lambda model: (len(model.stem), model.stem.casefold(),
                                    model.library.casefold(), str(model.path)))
    return FilterResult(matches[:MAX_RESULTS], len(matches))
