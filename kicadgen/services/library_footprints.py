"""@brief KiCad library footprints found by the component's part number, used as a geometry witness.

The user types the part number of the component («Sigla», e.g. ``G5V-2-H1``).
The installed KiCad footprint libraries are searched for footprints whose file
name carries a series code of that part number, and the pad centres of each
one are read. A footprint found this way is NEVER the source of the
footprint: it only says whether a pad layout read from the drawing has the
same geometry (up to a translation and a quarter turn), exactly like the
holes found in the drawing image (drawing_holes). Pad numbers and names always
come from the drawing.

@note DECLARED HEURISTIC for the search only. KiCad libraries carry no
    part-number field; one would make the search exact and this matching
    superfluous. The rule rests on the SHAPE of names, never on a list of
    makers or families, and was checked against the KiCad 10 footprint
    library (relays, DIP, Molex, passives):
    - A footprint name is split on ``_`` into tokens. A token with a digit,
      at least MIN_TOKEN_CHARS literal letters/digits, and not a size
      («W7.62mm», «Pad1.08x0.95mm») or pin-arrangement («1x02») token is a
      series code. Inside a code every other non-alphanumeric character is
      a separator. Leading letters-only segments followed by a segment with
      a digit («Omron-G5LE-1», «FTR-LYCA005x») are a maker or qualifier:
      the code may match with or without them.
    - A run of ``x`` touching no letter («xx», «x4», «0XX», «1xxx2M»,
      «005x») is a placeholder, one character per ``x``.
    - The typed part number is split into segments on every
      non-alphanumeric character. A code matches only when it starts at a
      segment start AND ends at a segment end: «G2RL-1» is not in
      «G2RL-1A-DC12», and a number-only code («3650», «1700») is never
      found inside a longer segment («TPS73650DBVR»). The cost: a part
      number typed without separators («g5v2h1») cannot tell where the
      series ends, so it finds nothing rather than a guess.
    - Only the most specific code votes: the longest matched span wins, so
      a shorter sibling («G6K-2P» beside «G6K-2P-Y») drops out.
    A wrong candidate still cannot make a wrong footprint (it only votes on
    geometry), and candidates of different geometries give no verdict at
    all (check_layout). It CAN contradict a correct reading when the part
    number names a footprint of another part sharing the same segments; the
    boundary and specificity rules exist to make that rare, not impossible.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Shortest series code, in literal letters and digits (placeholders not
#: counted), that may select a footprint: shorter codes («RT2», «G6A»)
#: occur by chance as segments of unrelated part numbers.
MIN_TOKEN_CHARS = 4
#: Runtime safety cap only, NOT a specificity rule: the most-specific
#: selection in find_footprints and the "different geometries -> no
#: verdict" rule of check_layout already deal with generic part numbers.
#: A part number whose most specific code still names more files than this
#: is not worth parsing them all.
MAX_CANDIDATES = 64
#: Tokens that describe a pitch or a size, never a series («RM5mm», «P2.54mm»,
#: «W7.62mm», «Pad1.08x0.95mm»).
_SIZE_TOKEN = re.compile(r"^[a-z]*\d+(?:[.,]\d+)?(?:x\d+(?:[.,]\d+)?)*mm$", re.I)
#: Tokens that describe a pin arrangement (rows x columns), never a series («1x02», «2x05-1MP»).
_ARRANGEMENT_TOKEN = re.compile(r"^\d+x\d+(?:-\d*MP)?$", re.I)
#: A run of ``x`` touching no letter on either side: a placeholder.
_PLACEHOLDER = re.compile(r"(?<![a-z])x+(?![a-z])", re.I)


@dataclass(frozen=True)
class LibraryFootprint:
    """@brief One library footprint: its name and the centres of its numbered copper pads, mm.

    @note Centres are KiCad's: top view, X right, Y down, the hole position of
        a through-hole pad (where a drawn hole is).
    """

    name: str
    centres: tuple


@dataclass(frozen=True)
class LibraryCheck:
    """@brief Verdict of a pad layout against the library footprints, shaped like drawing_holes.LayoutCheck.

    @note ``status`` "confirmed" or "not_confirmed" (reason "no_match");
        ``image`` names the footprint that decided (or the candidates);
        ``source`` is always "library" so messages can tell it from the
        drawn holes.
    """

    status: str
    reason: str
    image: Optional[str]
    max_error_mm: Optional[float]
    tolerance_mm: Optional[float]
    circles: int
    source: str = "library"


def footprints_root() -> Optional[Path]:
    """@brief The installed KiCad ``footprints`` folder, or None when KiCad is not found.

    @return The sibling ``footprints`` folder of the 3D model library found by
        cad3d.kicad_models.find_library.
    """
    from ..cad3d.kicad_models import find_library
    library = find_library()
    if library is None:
        return None
    root = Path(library).parent / "footprints"
    return root if root.is_dir() else None


def _segments(text: str) -> tuple:
    """Lower-case letters/digits of `text` and the positions where its segments start or end.

    Every non-alphanumeric character separates segments; the positions are
    indices in the squashed text, 0 and its length included.
    """
    squashed = ""
    bounds = {0}
    for piece in re.split(r"[^a-z0-9]+", str(text or "").lower()):
        squashed += piece
        bounds.add(len(squashed))
    return squashed, bounds


def _code_pattern(segments: list) -> Optional[str]:
    """A series code (its raw segments) as regex text on _segments text, or None when too weak.

    Each placeholder ``x`` becomes ``.``; a code without a literal digit or
    with fewer than MIN_TOKEN_CHARS literal characters is no code.
    """
    pattern, literal_text = "", ""
    for segment in segments:
        position = 0
        for run in _PLACEHOLDER.finditer(segment):
            literal = segment[position:run.start()].lower()
            pattern += re.escape(literal) + "." * len(run.group())
            literal_text += literal
            position = run.end()
        literal = segment[position:].lower()
        pattern += re.escape(literal)
        literal_text += literal
    if len(literal_text) < MIN_TOKEN_CHARS or not re.search(r"\d", literal_text):
        return None
    return pattern


def _series_codes(name: str) -> list:
    """The series codes of a footprint file name, each a compiled pattern on _segments text."""
    codes = []
    for raw in name.split("_"):
        if _SIZE_TOKEN.match(raw) or _ARRANGEMENT_TOKEN.match(raw):
            continue
        segments = [s for s in re.split(r"[^A-Za-z0-9]+", raw) if s]
        variants = [segments]
        # Leading letters-only segments before one with a digit: maker or
        # qualifier, decided by token shape alone («Omron-G5LE-1»).
        lead = 0
        while lead < len(segments) - 1 and segments[lead].isalpha():
            lead += 1
        if lead and re.search(r"\d", segments[lead]):
            variants.append(segments[lead:])
        for variant in variants:
            pattern = _code_pattern(variant)
            if pattern is not None:
                codes.append(re.compile(pattern))
    return codes


def _matched_span(code: re.Pattern, squashed: str, bounds: set) -> int:
    """Length `code` covers in the part number from a segment start to a segment end, else 0.

    Every code pattern has a fixed length (literals and one-character
    placeholders), so one match attempt per segment start suffices.
    """
    best = 0
    for start in bounds:
        found = code.match(squashed, start)
        if found and found.end() > start and found.end() in bounds:
            best = max(best, found.end() - start)
    return best


def find_footprints(part_number: str, root: Optional[Path] = None) -> list:
    """@brief Library footprint files whose name carries the most specific series code of `part_number`.

    @param part_number The part number typed by the user; blank finds nothing.
        Its separators (any non-alphanumeric character) mark where a series
        code may start and end.
    @param root Footprint folder to search; None means footprints_root().
    @return Paths of ``*.kicad_mod`` files, sorted by path, whose best code
        covers the LONGEST span of the part number among all footprints
        (shorter sibling codes drop out); empty when nothing matches, or
        when more than the runtime safety cap MAX_CANDIDATES share it.
    @note The candidates returned may still differ in geometry (one code
        shared by several package widths); check_layout then gives no verdict.
    """
    squashed, bounds = _segments(part_number)
    root = footprints_root() if root is None else Path(root)
    if len(squashed) < MIN_TOKEN_CHARS or root is None or not root.is_dir():
        return []
    found = []
    for path in root.glob("*.pretty/*.kicad_mod"):
        best = max((_matched_span(code, squashed, bounds) for code in _series_codes(path.stem)),
                   default=0)
        if best:
            found.append((best, path))
    if not found:
        return []
    longest = max(best for best, _path in found)
    chosen = sorted((path for best, path in found if best == longest), key=str)
    return [] if len(chosen) > MAX_CANDIDATES else chosen


def read_footprint(path: Path) -> Optional[LibraryFootprint]:
    """@brief The numbered copper pad centres of one ``.kicad_mod``, or None when unreadable.

    @param path Footprint file.
    @return LibraryFootprint with one centre per numbered pad (non-plated
        holes and unnumbered pads left out), or None when the file cannot
        be parsed or has fewer than two such pads.
    """
    from ..cad3d.kicad_models import _children, _field, _numbers, _sexpr
    try:
        tree = _sexpr(Path(path).read_text(encoding="utf-8"))
        centres = []
        for pad in _children(tree, "pad"):
            if len(pad) < 3 or pad[2] == "np_thru_hole" or not str(pad[1]).strip():
                continue
            at = _field(pad, "at")
            x, y = _numbers(at[:2], 2)
            centres.append((x, y))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if len(centres) < 2:
        return None
    return LibraryFootprint(name=Path(path).stem, centres=tuple(centres))


def _rotations(points: list) -> list:
    """The four quarter turns of `points` (a footprint may be drawn turned on the page)."""
    turns = [points]
    for _ in range(3):
        turns.append([(-y, x) for x, y in turns[-1]])
    return turns


def _match_error(points: list, centres: tuple) -> Optional[float]:
    """Largest distance between two equal-sized point sets aligned on their centroids, one-to-one.

    None when the sets differ in size; the greedy pairing takes, for each
    point, the nearest free centre.
    """
    if len(points) != len(centres):
        return None
    def centred(values):
        cx = sum(x for x, _y in values) / len(values)
        cy = sum(y for _x, y in values) / len(values)
        return [(x - cx, y - cy) for x, y in values]
    free = centred(list(centres))
    worst = 0.0
    for x, y in centred(points):
        index = min(range(len(free)), key=lambda i: math.hypot(free[i][0] - x, free[i][1] - y))
        worst = max(worst, math.hypot(free[index][0] - x, free[index][1] - y))
        free.pop(index)
    return worst


def _best_error(points: list, centres: tuple) -> Optional[float]:
    """Smallest _match_error of `points` over its four quarter turns, None when sizes differ."""
    errors = [error for turn in _rotations(points)
              if (error := _match_error(turn, centres)) is not None]
    return min(errors) if errors else None


def _geometry_classes(footprints: list, tolerance_mm: float) -> list:
    """Candidates grouped by geometry: same pad centres up to a translation and a quarter turn.

    Both sides are top views, so no mirror. Two footprints closer than the
    reading tolerance could not be told apart by any reading anyway.
    """
    classes = []
    for footprint in footprints:
        for group in classes:
            error = _best_error(list(footprint.centres), group[0].centres)
            if error is not None and error <= tolerance_mm:
                group.append(footprint)
                break
        else:
            classes.append([footprint])
    return classes


def check_layout(pads: dict, view: Optional[str], footprints: list,
                 tolerance_mm: float) -> Optional[LibraryCheck]:
    """@brief Whether a pad layout read from a drawing has the geometry of the library footprints.

    @param pads {number: (x, y)} in mm, page orientation of the drawing.
    @param view The view the drawing declares: "bottom" mirrors X into the
        footprint's top view; "top" keeps it; anything else cannot be
        compared to a top-view footprint without guessing the side, so None.
    @param footprints LibraryFootprint candidates (find_footprints + read_footprint).
    @param tolerance_mm Largest centre distance accepted.
    @return None (no opinion) when there is nothing to compare (no candidate,
        unknown view) or when the candidates themselves have DIFFERENT
        geometries: the part number then does not name one layout, and
        confirming the reading that fits one of them would let a superset or
        a sibling width confirm a wrong reading. Otherwise "confirmed" when
        the candidates' common layout has the same pad centres up to a
        translation and a quarter turn, never a mirror; else
        "not_confirmed" / "no_match".
    @note Numbers are ignored on purpose: library footprints number pads
        their own way (1..5 for a relay whose drawing prints A1, A2, 11...).
        Geometrically equal duplicates (the same layout in two libraries)
        form one geometry and still give a verdict.
    """
    if not footprints or view not in ("top", "bottom") or len(pads) < 2:
        return None
    if len(_geometry_classes(footprints, tolerance_mm)) > 1:
        return None
    points = [((-x if view == "bottom" else x), y) for x, y in pads.values()]
    for footprint in footprints:
        error = _best_error(points, footprint.centres)
        if error is not None and error <= tolerance_mm:
            return LibraryCheck("confirmed", "confirmed", footprint.name, error,
                                tolerance_mm, len(footprint.centres))
    return LibraryCheck("not_confirmed", "no_match",
                        ", ".join(f.name for f in footprints), None, tolerance_mm, 0)
