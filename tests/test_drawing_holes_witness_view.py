"""Tests of the drawn-hole witness on pages whose pins are drawn too small, and of the view rule.

2026-11-13 (TEL page): a bottom view drew its six pins as dots of about
2 px radius on chain-dotted centre lines, below the ring detector's
smallest radius, while other views of the page held circles drawn like
holes. Two rules of :mod:`kicadgen.services.drawing_holes`:

1. a crossing of two long centre lines, at least one of them interrupted
   (chain, dashed), with a small mark drawn around the crossing and
   nothing along the lines just beyond it, is a hole centre
   (:func:`drawing_holes._crossing_marks`); a crossing of solid lines
   (dimension and extension lines), a bare crossing and two arrowheads
   meeting on an extension line are not;
2. a failed fit contradicts a reading only when one similarity anchors a
   strict majority of its pads on circles drawn like holes
   (:func:`drawing_holes._view_anchored`), or when one drawn rectangle encloses as many circles drawn like holes as
   the reading has pads (:func:`drawing_holes._outlined_view_holds`):
   circles drawn like holes scattered over OTHER views say nothing
   against it ("too_few_circles", unverifiable).

Every synthetic page is drawn with OpenCV at run time. Each negative
differs from its positive by the structural property under test (line
style, the mark on the crossing, whether the reading's view is drawn),
never by a tuned number. The two real pages of the user's components live
under ``generated/`` (untracked): their checks are skipped when absent.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from kicadgen.services import drawing_holes
from kicadgen.services.drawing_holes import check_pad_layout

#: Page scale of the synthetic drawings, px per mm.
PPMM = 10.0
#: Page position of the pad at (0, 0) mm, px.
ORIGIN = (100, 100)
#: A TEL-like bottom view: three columns (15.24 and 2.54 mm apart), two rows 10.16 mm apart.
COLUMNS = (0.0, 15.24, 17.78)
ROWS = (0.0, 10.16)
TRUE = {"1": (0.0, 0.0), "7": (15.24, 0.0), "8": (17.78, 0.0),
        "16": (0.0, 10.16), "10": (15.24, 10.16), "9": (17.78, 10.16)}
#: Circles drawn like holes (solid crosshair through each) in another view
#: below the bottom view, at positions that do not repeat its pattern, px.
OTHER_VIEW = ((90, 350), (150, 365), (235, 345), (330, 385), (115, 440), (265, 425),
              (410, 352), (465, 438))


def _px(x_mm: float, y_mm: float) -> tuple[int, int]:
    """Page pixel of a point given in mm of the reading frame."""
    return int(round(ORIGIN[0] + x_mm * PPMM)), int(round(ORIGIN[1] + y_mm * PPMM))


def _chain_line(page: np.ndarray, start: tuple[int, int], end: tuple[int, int]) -> None:
    """Draw an axis-aligned chain line (dash 12 px, gap 3, short dash 4, gap 3) of 1 px."""
    (x0, y0), (x1, y1) = start, end
    horizontal = y0 == y1
    length = abs(x1 - x0) if horizontal else abs(y1 - y0)
    position = 0
    pattern = ((12, True), (3, False), (4, True), (3, False))
    while position < length:
        for size, inked in pattern:
            stop = min(position + size, length)
            if inked and stop > position:
                if horizontal:
                    cv2.line(page, (x0 + position, y0), (x0 + stop - 1, y0), 0, 1)
                else:
                    cv2.line(page, (x0, y0 + position), (x0, y0 + stop - 1), 0, 1)
            position = stop
            if position >= length:
                break


def _dashed_line(page: np.ndarray, start: tuple[int, int], end: tuple[int, int]) -> None:
    """Draw an axis-aligned evenly dashed (hidden) line: dash 6 px, gap 3, 1 px wide."""
    (x0, y0), (x1, y1) = start, end
    horizontal = y0 == y1
    length = abs(x1 - x0) if horizontal else abs(y1 - y0)
    for position in range(0, length, 9):
        stop = min(position + 6, length) - 1
        if horizontal:
            cv2.line(page, (x0 + position, y0), (x0 + stop, y0), 0, 1)
        else:
            cv2.line(page, (x0, y0 + position), (x0, y0 + stop), 0, 1)


def _line(page: np.ndarray, start: tuple[int, int], end: tuple[int, int], style: str) -> None:
    """Draw an axis-aligned line, "chain" (centre line), "dashed" (hidden line) or "solid"."""
    if style == "chain":
        _chain_line(page, start, end)
    elif style == "dashed":
        _dashed_line(page, start, end)
    else:
        cv2.line(page, start, end, 0, 1)


#: Centre of the drawn body, mm: where its two symmetry axes cross.
BODY_CENTRE = (9.0, 5.1)


def _page(path: Path, *, centre_lines: str | None = "chain", other_view: bool = True,
          extra_vertical_mm: tuple[float, ...] = (), frame: bool = False,
          centre_mark: bool = False) -> Path:
    """Draw a bottom view with dot pins (radius 2 px, below R_MIN_PX) and optionally another view.

    @param centre_lines "chain", "dashed", "solid" or None: how the centre
        lines through the pin rows and columns are drawn (None: not drawn).
    @param other_view Draw OTHER_VIEW: circles drawn like holes elsewhere.
    @param extra_vertical_mm X (mm) of further chain centre lines drawn
        across the rows WITHOUT a pin on them (bare crossings).
    @param frame Draw a drawing border around the whole page.
    @param centre_mark Draw the body's two chain symmetry axes with a dot
        on their crossing (a datum mark, no pin there).
    """
    page = np.full((520, 620), 255, np.uint8)
    left, top = _px(-3.0, -2.0)
    right, bottom = _px(21.0, 12.2)
    cv2.rectangle(page, (left, top), (right, bottom), 0, 1)
    if frame:
        cv2.rectangle(page, (5, 5), (page.shape[1] - 6, page.shape[0] - 6), 0, 1)
    if centre_lines is not None:
        for y in ROWS:
            _line(page, _px(-1.5, y), _px(20.0, y), centre_lines)
        for x in COLUMNS + tuple(extra_vertical_mm):
            _line(page, _px(x, -1.2), _px(x, 13.5), centre_lines)
    if centre_mark:
        _chain_line(page, _px(-2.5, BODY_CENTRE[1]), _px(20.5, BODY_CENTRE[1]))
        _chain_line(page, _px(BODY_CENTRE[0], -1.5), _px(BODY_CENTRE[0], 11.7))
        cv2.circle(page, _px(*BODY_CENTRE), 2, 0, -1)
    for x, y in TRUE.values():
        cv2.circle(page, _px(x, y), 2, 0, -1)
    if other_view:
        for cx, cy in OTHER_VIEW:
            cv2.circle(page, (cx, cy), 6, 0, 1)
            cv2.line(page, (cx - 30, cy), (cx + 30, cy), 0, 1)
            cv2.line(page, (cx, cy - 30), (cx, cy + 30), 0, 1)
    cv2.putText(page, "BOTTOM VIEW", (130, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1)
    cv2.imwrite(str(path), page)
    return path


def _marks(path: Path) -> list:
    """The crossing marks of a page, as found on its working ink mask."""
    grey = drawing_holes._working_grey(drawing_holes._read_image(path))
    mask, _paper = drawing_holes._ink_mask(grey)
    return drawing_holes._crossing_marks(mask, [])


def _near_pins(marks: list) -> int:
    """How many pins of TRUE have a mark within 1 px of their centre."""
    return sum(any(abs(m.x - px) <= 1 and abs(m.y - py) <= 1 for m in marks)
               for px, py in (_px(*xy) for xy in TRUE.values()))


# --- rule 1: centre-line crossings -----------------------------------------

def test_dots_on_chain_centre_lines_are_hole_centres(tmp_path):
    """Positive: every pin dot on two crossing chain lines becomes a target drawn like a hole."""
    marks = _marks(_page(tmp_path / "chain.png"))
    assert _near_pins(marks) == len(TRUE), marks
    assert all(m.evidence and m.kind == "ring" for m in marks)


@pytest.mark.parametrize("style", ["solid", "dashed"])
def test_dots_on_solid_or_dashed_lines_are_not_hole_centres(tmp_path, style):
    """Negative: the same dots on solid lines (dimension/extension) or on evenly dashed
    hidden lines are not taken: only the chain's long/short alternation marks a centre line."""
    assert _near_pins(_marks(_page(tmp_path / f"{style}.png", centre_lines=style))) == 0


def test_a_mark_confirms_but_never_refutes(tmp_path):
    """A dot on the body's chain symmetry axes is taken like a pin (nothing local tells a
    datum mark from a pin drawn small), but ``marked``: it may help a fit, never refute.

    The page has no other view, so its only centres drawn like holes are
    marks: a reading with one pad on paper is unverifiable there, while the
    true reading still confirms with the extra centre mark on the page.
    """
    page = _page(tmp_path / "axes.png", other_view=False, centre_mark=True)
    marks = _marks(page)
    centre = _px(*BODY_CENTRE)
    assert any(abs(m.x - centre[0]) <= 1 and abs(m.y - centre[1]) <= 1 for m in marks), marks
    assert all(m.marked and not drawing_holes._refutes(m) for m in marks)
    assert check_pad_layout([page], TRUE).status == "confirmed"
    wrong = {**TRUE, "9": (17.78 - 1.27, 10.16 - 1.27)}
    result = check_pad_layout([page], wrong)
    assert (result.status, result.reason) == ("unverifiable", "no_round_holes"), result


def test_bare_chain_crossing_is_not_a_hole_centre(tmp_path):
    """Negative: a chain line crossing the rows where no pin is drawn adds no target there."""
    marks = _marks(_page(tmp_path / "bare.png", extra_vertical_mm=(20.32,)))
    bare = [_px(20.32, y) for y in ROWS]
    assert not any(abs(m.x - bx) <= 2 and abs(m.y - by) <= 2 for m in marks for bx, by in bare), marks
    assert _near_pins(marks) == len(TRUE)


def _arrow_page(path: Path, *, arrows: bool) -> Path:
    """A chain extension line crossing a solid dimension line, with arrowheads or a dot on the crossing."""
    page = np.full((300, 400), 255, np.uint8)
    _chain_line(page, (200, 40), (200, 260))
    cv2.line(page, (60, 150), (340, 150), 0, 1)
    if arrows:
        # Two arrowheads meeting on the extension line from both sides
        # (a chain of two dimensions): ink in all four quadrants.
        cv2.fillPoly(page, [np.array([[200, 150], [184, 145], [184, 155]], np.int32)], 0)
        cv2.fillPoly(page, [np.array([[200, 150], [216, 145], [216, 155]], np.int32)], 0)
    else:
        cv2.circle(page, (200, 150), 2, 0, -1)
    cv2.imwrite(str(path), page)
    return path


@pytest.mark.parametrize("arrows, taken", [(False, True), (True, False)])
def test_arrowheads_on_a_crossing_are_not_a_hole_centre(tmp_path, arrows, taken):
    """Arrowheads fill the four quadrants but run along the dimension line; a dot ends at once."""
    marks = _marks(_arrow_page(tmp_path / f"arrow{arrows}.png", arrows=arrows))
    found = any(abs(m.x - 200) <= 1 and abs(m.y - 150) <= 1 for m in marks)
    assert found is taken, marks


# --- rules 1 and 2 through check_pad_layout --------------------------------

def test_true_layout_on_chain_crossings_is_confirmed(tmp_path):
    """Positive: the true reading is confirmed on the dot pins, despite holes in another view."""
    result = check_pad_layout([_page(tmp_path / "page.png")], TRUE)
    assert (result.status, result.reason) == ("confirmed", "confirmed"), result


@pytest.mark.parametrize("frame", [False, True])
def test_true_layout_without_its_view_is_unverifiable_not_contradicted(tmp_path, frame):
    """Rule 2 positive: pins drawn with no centre line are invisible; the other view's holes cannot refute.

    Before 2026-11-13 the eight circles drawn like holes of the other view
    were enough for "no_match". With a drawing border around the whole
    page (generality audit C1) the border encloses all eight, but also
    the body outline of the bottom view, a closed rectangle without a hole
    of its own: the border is not one view and holds nothing.
    """
    page = _page(tmp_path / f"nolines{frame}.png", centre_lines=None, frame=frame)
    result = check_pad_layout([page], TRUE)
    assert (result.status, result.reason) == ("unverifiable", "too_few_circles"), result


def _ring_page(path: Path, *, outline: bool, frame: bool = False) -> Path:
    """The TRUE pins drawn as detectable rings (6 px) with solid centre lines (+-35 px).

    @param outline Draw the body outline around them.
    @param frame Draw a drawing border around the whole page.
    """
    page = np.full((420, 560), 255, np.uint8)
    if outline:
        cv2.rectangle(page, _px(-3.0, -2.0), _px(21.0, 12.2), 0, 1)
    if frame:
        cv2.rectangle(page, (5, 5), (page.shape[1] - 6, page.shape[0] - 6), 0, 1)
    for x, y in TRUE.values():
        cx, cy = _px(x, y)
        cv2.circle(page, (cx, cy), 6, 0, 1)
        cv2.line(page, (cx - 35, cy), (cx + 35, cy), 0, 1)
        cv2.line(page, (cx, cy - 35), (cx, cy + 35), 0, 1)
    cv2.imwrite(str(path), page)
    return path


#: Pad 9 read 1.27 mm off diagonally, onto paper.
ONE_OFF = {**TRUE, "9": (17.78 - 1.27, 10.16 - 1.27)}


def test_one_pad_read_on_paper_is_still_contradicted(tmp_path):
    """Rule 2 negative: the view is anchored by five pads on rings, so the sixth on paper contradicts."""
    result = check_pad_layout([_ring_page(tmp_path / "rings.png", outline=False)], ONE_OFF)
    assert (result.status, result.reason) == ("not_confirmed", "no_match"), result


def test_marks_alone_never_refute_a_pad_read_on_paper(tmp_path):
    """The same reading on the dot page: its view is made of crossing marks only, which may
    confirm but never refute (generality audit S1), so the page cannot tell.

    CONTRACT CHANGED 2026-11-13 (same day as the test was written): this
    case was "no_match" while crossing marks counted against a reading; a
    mark may be a datum on the body's symmetry axes, so they no longer do.
    """
    result = check_pad_layout([_page(tmp_path / "page.png")], ONE_OFF)
    assert (result.status, result.reason) == ("unverifiable", "too_few_circles"), result


def test_row_shifted_by_one_pitch_onto_a_populated_crossing_is_not_confirmed(tmp_path):
    """Negative: the bottom row read one pitch to the right puts pad 16 on a bare chain
    crossing, pad 10 on pin 9's populated crossing and pad 9 on paper: never confirmed.

    Unverifiable rather than refused: the pins are crossing marks, which
    never refute (see the test above).
    """
    shifted = dict(TRUE, **{"16": (2.54, 10.16), "10": (17.78, 10.16), "9": (20.32, 10.16)})
    page = _page(tmp_path / "bare.png", extra_vertical_mm=(2.54, 20.32))
    result = check_pad_layout([page], shifted)
    assert (result.status, result.reason) == ("unverifiable", "too_few_circles"), result
    # The same page still confirms the true reading: the bare crossings did
    # not disturb it.
    assert check_pad_layout([page], TRUE).status == "confirmed"


#: TRUE read with every X halved (a pitch misread by a factor of two):
#: no similarity puts more than a pair of rows' worth of its pads on holes.
HALVED = {key: (x / 2, y) for key, (x, y) in TRUE.items()}


@pytest.mark.parametrize("outline, frame, expected", [
    (True, False, ("not_confirmed", "no_match")),
    (True, True, ("not_confirmed", "no_match")),
    (False, False, ("unverifiable", "too_few_circles")),
])
def test_a_reading_wrong_in_most_pads_is_refused_only_inside_an_outlined_view(tmp_path, outline,
                                                                              frame, expected):
    """Rule 2, page side: the outlined view holds six holes, so the halved reading is refused there.

    A drawing border around it changes nothing (the body outline is the
    smaller rectangle and holds the six). Without the outline the same six
    rings could be any view, and the reading anchors too few of its pads
    to show its own: unverifiable.
    """
    page = _ring_page(tmp_path / f"rings{outline}{frame}.png", outline=outline, frame=frame)
    assert check_pad_layout([page], TRUE).status == "confirmed"
    result = check_pad_layout([page], HALVED)
    assert (result.status, result.reason) == expected, result


# --- the chain pattern, line by line (re-audit D5) --------------------------

def _row(pattern, total=200):
    """A 1-D run of ink drawn from (on, off) pairs repeated over ``total`` samples."""
    run = np.zeros(total, bool)
    position, index = 0, 0
    while position < total:
        on, off = pattern[index % len(pattern)]
        run[position:position + on] = True
        position += on + off
        index += 1
    return run


def test_a_repeated_long_short_alternation_is_a_chain():
    """Positive: long dash, short dash, repeated (ISO 128 chain)."""
    assert drawing_holes._is_chain(_row([(24, 4), (6, 4)]), stroke=2.0)


@pytest.mark.parametrize("name", ["hidden", "crossed_1px", "crossed_stroke", "dotted_jitter",
                                  "single_alternation", "two_gaps_antialiased"])
def test_dashes_crossed_or_jittered_are_not_a_chain(name):
    """Negatives: an evenly dashed hidden line, the same crossed in one gap by a line one or
    two stroke widths wide, a dotted line with 1 px jitter, a line with ONE alternation, and a
    hidden line crossed in two consecutive gaps by lines one stroke plus one anti-aliasing
    pixel wide (re-audit (b): 3 px at a 2.0 stroke)."""
    hidden = _row([(12, 4)])
    if name == "two_gaps_antialiased":
        run = _row([(12, 6)])
        run[13:16] = True
        run[31:34] = True
    elif name == "hidden":
        run = hidden
    elif name == "crossed_1px":
        run = hidden.copy()
        run[29] = True
    elif name == "crossed_stroke":
        run = hidden.copy()
        run[61:63] = True
    elif name == "dotted_jitter":
        run = _row([(2, 2), (2, 2), (1, 3)])
    else:
        run = _row([(24, 4)] * 3 + [(6, 4)] + [(24, 4)] * 4)
    assert not drawing_holes._is_chain(run, stroke=2.0), drawing_holes._ink_segments(run)


# --- what a confirmation rests on (LayoutCheck.marks_used / unassigned_refuting)

def test_marks_used_counts_crossing_marks_only(tmp_path):
    """Dot pins are all marks; ring pins none."""
    dots = check_pad_layout([_page(tmp_path / "dots.png")], TRUE)
    assert (dots.status, dots.marks_used) == ("confirmed", len(TRUE)), dots
    rings = check_pad_layout([_ring_page(tmp_path / "rings.png", outline=False)], TRUE)
    assert (rings.status, rings.marks_used) == ("confirmed", 0), rings


def test_a_reading_that_loses_a_drawn_position_reports_it(tmp_path):
    """Positive: five pads read where six rings are drawn confirm, with one ring left in the view.

    Negatives: the full reading leaves none; on the dot page the eight
    rings of the other view lie outside the view and are not counted.
    """
    page = _ring_page(tmp_path / "rings.png", outline=False)
    lost = {key: value for key, value in TRUE.items() if key != "7"}
    result = check_pad_layout([page], lost)
    assert (result.status, result.unassigned_refuting) == ("confirmed", 1), result
    assert check_pad_layout([page], TRUE).unassigned_refuting == 0
    other = check_pad_layout([_page(tmp_path / "dots.png")], TRUE)
    assert (other.status, other.unassigned_refuting) == ("confirmed", 0), other


def test_a_lost_end_column_is_reported(tmp_path):
    """Re-audit (a): the reading drops the END column (pads 8 and 9), one pitch outside the
    hull of the pads it kept; its two rings are still counted. The full reading counts none."""
    page = _ring_page(tmp_path / "rings.png", outline=False)
    lost = {key: value for key, value in TRUE.items() if key not in ("8", "9")}
    result = check_pad_layout([page], lost)
    assert (result.status, result.unassigned_refuting) == ("confirmed", 2), result
    assert check_pad_layout([page], TRUE).unassigned_refuting == 0


#: DIP-8 under the usual convention: pins 1-4 along the bottom row, 5-8
#: back along the top row; pitch 2.54 mm, rows 7.62 mm apart (page Y down).
DIP8 = {**{str(n): ((n - 1) * 2.54, 7.62) for n in range(1, 5)},
        **{str(n): ((8 - n) * 2.54, 0.0) for n in range(5, 9)}}


def _dip8_page(path: Path) -> Path:
    """DIP8 drawn at 20 px/mm as 8 px rings with solid centre lines (+-40 px)."""
    page = np.full((500, 600), 255, np.uint8)
    for x, y in DIP8.values():
        cx, cy = int(round(150 + 20 * x)), int(round(150 + 20 * y))
        cv2.circle(page, (cx, cy), 8, 0, 1)
        cv2.line(page, (cx - 40, cy), (cx + 40, cy), 0, 1)
        cv2.line(page, (cx, cy - 40), (cx, cy + 40), 0, 1)
    cv2.imwrite(str(path), page)
    return path


def test_a_lost_row_at_the_wider_row_spacing_is_reported(tmp_path):
    """Re-audit H3: the view grows by the largest row or column spacing, not the smallest.

    Six of eight pads (row 1-4 and two of the other row) leave the two lost
    pins, 7.62 mm away across the rows, counted; the full reading counts
    none; a reading of one row only has nothing that says how far its view
    extends across the row: None (not computed), never 0.
    """
    page = _dip8_page(tmp_path / "dip8.png")
    full = check_pad_layout([page], DIP8)
    assert (full.status, full.unassigned_refuting) == ("confirmed", 0), full
    six = {key: DIP8[key] for key in ("1", "2", "3", "4", "5", "8")}
    result = check_pad_layout([page], six)
    assert (result.status, result.unassigned_refuting) == ("confirmed", 2), result
    row = {key: DIP8[key] for key in ("1", "2", "3", "4")}
    single = check_pad_layout([page], row)
    assert (single.status, single.unassigned_refuting) == ("confirmed", None), single


def test_evidence_fields_are_none_when_not_computed(tmp_path):
    """A result that is not a confirmation carries None, not 0, in both fields: «not computed»
    is not «none» (a confirmation on rings carries 0)."""
    page = _ring_page(tmp_path / "rings.png", outline=False)
    refused = check_pad_layout([page], ONE_OFF)
    assert refused.status == "not_confirmed"
    assert (refused.marks_used, refused.unassigned_refuting) == (None, None), refused
    unverifiable = check_pad_layout([_page(tmp_path / "nolines.png", centre_lines=None)], TRUE)
    assert unverifiable.status == "unverifiable"
    assert (unverifiable.marks_used, unverifiable.unassigned_refuting) == (None, None)
    confirmed = check_pad_layout([page], TRUE)
    assert (confirmed.marks_used, confirmed.unassigned_refuting) == (0, 0), confirmed


# --- the two real pages (untracked, skipped when absent) -------------------

ROOT = Path(__file__).resolve().parents[1]
TEL_PAGE = ROOT / "generated" / "TEL 87-2411WI" / "docs" / "quote.png"
T7N_PAGE = ROOT / "generated" / "T7NS5D4-12" / "docs" / "Pinout.png"


@pytest.mark.skipif(not TEL_PAGE.exists(), reason="user page not in this checkout")
def test_tel_page_true_layout_never_refused_and_wrong_pitch_never_confirmed():
    """TEL bottom view: the true reading is not refused; the 5.08 mm pitch reading is not confirmed.

    CONTRACT CHANGED 2026-11-13 (re-audit D5): the true reading was
    "confirmed" while one alternation of long and short dashes made a
    chain line. A chain must now repeat its alternation, and the column of
    pins 1 and 16 shows it once (its other dashes merge with crossing
    lines): those two pins are not targets and the true reading is
    "unverifiable", never "not_confirmed".
    """
    true = {"1": (3.00, 1.77), "7": (18.24, 1.77), "8": (20.78, 1.77),
            "16": (3.00, 11.93), "10": (18.24, 11.93), "9": (20.78, 11.93)}
    wrong = {"1": (-8.90, -6.85), "7": (-3.82, -6.85), "8": (-1.28, -6.85),
             "16": (-8.90, 6.85), "10": (-3.82, 6.85), "9": (-1.28, 6.85)}
    assert check_pad_layout([TEL_PAGE], true, (23.8, 13.7)).status != "not_confirmed"
    assert check_pad_layout([TEL_PAGE], wrong, (23.8, 13.7)).status != "confirmed"


@pytest.mark.skipif(not T7N_PAGE.exists(), reason="user page not in this checkout")
def test_t7n_page_unchanged():
    """T7N pinout: the true five pads confirm with the body; a phantom sixth pad is contradicted."""
    pads = {"11": (2.05, 6.0), "12": (16.25, 12.0), "14": (16.25, 0.0),
            "A1": (4.05, 0.0), "A2": (4.05, 12.0)}
    result = check_pad_layout([T7N_PAGE], pads, body_mm=(22.5, 16.5))
    assert (result.status, result.body_check) == ("confirmed", "agrees"), result
    assert result.max_error_mm == pytest.approx(0.1143, abs=1e-3)
    phantom = check_pad_layout([T7N_PAGE], dict(pads, C=(16.25, 6.0)), body_mm=(22.5, 16.5))
    assert (phantom.status, phantom.reason) == ("not_confirmed", "no_match"), phantom
