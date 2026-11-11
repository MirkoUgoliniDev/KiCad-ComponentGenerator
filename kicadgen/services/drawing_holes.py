"""@brief Local, deterministic check of a pad-layout reading against the hole circles of a drawing.

@details The vision model reads pad positions from a dimensioned drawing by
transcribing dimension chains; the program sums them. Those readings are often
wrong (a quote taken from the wrong chain, two quotes exchanged, a row read on
the wrong side). This module checks such a reading against the hole circles
actually drawn in the image, without any AI and without any network access:

1. the image is read (unreadable files are skipped) and reduced to an ink mask
   relative to its own paper and ink levels;
2. photographs are refused by a paper/ink gate: they are not line drawings and
   their round features are not drawn holes;
3. small drawn circles are found by a ring matched filter over a scale
   pyramid, refined on the ink and filtered against text glyphs; ink of a
   centre or extension line crossing a small hole does not count as fill,
   a ring must be localised (not a stretch of two parallel lines or a bar),
   and of two overlapping circles the less completely inked one goes;
4. the reading (pad centres in mm, page orientation) is matched to the
   circles by similarity hypotheses built on its longest baselines;
5. "confirmed" means a displacement test: mapped by a similarity (uniform
   scale, rotation, translation, never a mirror) fitted on the other pads,
   every pad lies within the per-hole tolerance (pixels) of its circle, or
   -- on a page stretched within PAGE_STRETCH_RATIO (a ratio) -- of where
   the affine map fitted on the other pads puts it. A reading that only
   fits after a larger stretch is "distorted"; one that fits only on a
   page turned beyond MAX_ROTATION_DEG is "page_skewed" (unverifiable).
   A fit that a coincidence satisfies easily (three pads, or a stretched
   page) confirms only on circles drawn like holes, else "unanchored";
6. optionally the rectangles drawn around the matched holes (solid,
   dashed or dotted, see :func:`_outline_rects`) are compared with the
   body size of the reading (:func:`_body_check`): one with the body's
   proportions is the body, and ties the reading to an absolute size
   (``scale_verified``) or contradicts it ("body_scale"); rectangles of
   other proportions say nothing. All of them are judged, by the decision
   table below. Without an agreement, a confirmation covers the
   reading's proportions only (see :class:`LayoutCheck`).

Body check: the decision table (2026-11-11, review finding V1-1). Every
rectangle drawn around the matched holes is asked two independent
questions (:func:`_judge_outline`). PROPORTIONS, scale-free: are its two
sides the body's two sizes at ONE scale -- within its own measurement
uncertainty ("identified": OUTLINE_EDGE_PX per edge), or only within the
drafting allowance ("loose": BODY_SCALE_RATIO per side)? SIZE: are its
sides the body's AT THE PADS' SCALE, within the allowance ("here"), or
not ("elsewhere")? Either way the rectangle is a CANDIDATE for the body:
it could be the declared body, drawn as loosely as the allowance admits,
at the pads' scale or at another. A rectangle of other proportions and
other sizes is no candidate, says nothing and is left out. Then, by
which candidates the page holds::

    row  here (identified   elsewhere,   elsewhere,   verdict
         or loose)          identified   loose
    1    yes                no           no           agrees
    2    yes                at least one of the two   unverifiable
    3    no                 yes          any          contradicted
    4    no                 no           any          unverifiable

Why each row, and what it rests on:

- Row 1 (derived, with one declared limit). Every candidate for the body
  on the page is at the pads' scale. The limit is the page whose only
  candidate is not the body: see the residue below.
- Row 2 (derived: it is an ambiguity, not a rule). The page holds a
  candidate at the pads' scale and one at another scale, and two
  different pages produce this SAME evidence. A correct reading of a body
  with a step or a shield can inside it: the outer rectangle is the body,
  here, and the inner one is a candidate elsewhere. And a wrongly scaled
  reading of a body with a clearance frame outside it: the inner
  rectangle is the body, elsewhere, and the frame falls inside the
  allowance of the wrong scale (V1-1 was found on a 19.1 x 15.5 mm
  body). No structural fact tells them apart: "the innermost is the
  body" fails on the step; "the body encloses its pads" holds for every
  rectangle found (:func:`_outline_rects`) and is not true of every part
  anyway (gull-wing leads reach outside the body); "a step keeps one
  margin on its four sides" is as true of the frame. Nor does "exact
  proportions outrank loose ones": real outlines are drawn up to 1.5 %
  out of proportion (the sample pages), a frame around a nearly square
  body keeps the proportions within a pixel, and :func:`_outline_rects`
  also closes rectangles out of the sides of two nested outlines, some
  of which have the body's proportions by geometry (measured on
  synthetic pages: 45 wrong scales certified in 2100 by that ranking).
  Resolving the row towards the smallest rectangle refused correct
  readings (F7-1, until 2026-11-10); resolving it towards the agreeing
  one certified wrong scales (V1-1, the morning of 2026-11-11). The pads
  stay confirmed by their shape; the scale is not verified.
- Row 3 (derived, on a declared identification). Nothing is drawn at the
  reading's scale and the body's exact proportions are drawn at another:
  the reading is scaled wrongly ("body_scale"). Only EXACT proportions
  refuse: a loose candidate elsewhere may be a frame, or a body with one
  size misread, and a mistake of the body reading must not judge the
  pads (audit A9).
- Row 4 (derived). No candidate at the pads' scale and none identified:
  the page does not say where the body is.

Cost of row 2, accepted: a correct reading loses ``scale_verified`` (it
is still confirmed) whenever a second rectangle near the outline could
be the body within the allowance -- a step or a frame whose margin
changes the proportions by less than about 10 %.

Residue of false verdicts, pinned by tests in
``tests/test_drawing_holes_generality.py``:

- row 1, a wrong scale certified: the page does NOT draw the body, only
  a frame around it (or draws the two so close that they are one line),
  and the reading's error brings that frame inside the allowance. A
  single candidate cannot be told from a loosely drawn body (the SRD
  sample page draws 19.06 x 15.25 mm for 19.1 x 15.5 mm), and a tighter
  proportion bound would be a number chosen on the sample pages;
- row 3, a correct reading refused: the page does not draw the body
  outline and its only candidate is a step or a frame that keeps the
  body's exact proportions (a square or nearly square body);
- errors of the reading within the allowance itself (5 % plus two
  tolerances): below the check's resolution.

A third entry point, :func:`measured_outline`, returns that drawn outline
in the reading's own mm frame through the inverse of the confirmed fit.

Every image analysis (photo gate, ink mask, circles) is cached per file, so
checking several competing readings against one page detects once.

Asymmetry of the verdict: any circle may help CONFIRM a reading, but a
reading is reported NOT CONFIRMED only when the image holds at least as many
circles drawn like holes (centre or extension line through them, or standing
alone on paper) as the reading has pads. Otherwise the image cannot tell a
wrong reading from a page whose circles are letters, and the answer is
"unverifiable" with reason "too_few_circles", or "no_round_holes" when no
circle at all is drawn like a hole (an SMD land pattern, a page of text).

Slots and lands (2026-11-04, review H4/M11): a closed non-circular contour
of hole size -- an obround (two semicircles joined by two parallel
segments) or an outlined rectangle -- is found as well (see
:func:`_find_regions`). An obround is a drawn hole: its centre takes part
in the matching like a circle's, with ``kind`` "obround" beside the rings.
A pad whose reading lands inside such a contour is unverifiable for that
pad, never a miss that contradicts the reading. The reading may declare
each pad's hole kind (terminal, mounting, slot) and the positions of its
non-pad holes (``hole_kinds``, ``reference_holes_mm``): a declared pad is
matched only to a hole of its kind, and a declared reference hole is never
taken for a pad.

Scale observability (2026-11-04, review H5/M18): the stretch and shear
test can be run only on AFFINE_MIN_PADS pads not nearly collinear
(:func:`_affine_observable`); ``scale_observable`` says whether it was, a
failed fit of a reading that cannot show the page's scale is reported
"no_match_unobservable" (not the losing "no_match"), and no pad is ever
moved from an anchoring that cannot show the scale or shows a stretch.

A second entry point, :func:`measured_layout`, deduces the position of ONE
misplaced pad of a reading: when every other pad (at least MIN_PADS, able
to show the page's scale) sits on its drawn hole and exactly one hole drawn
like a hole, of the pad's kind, is left free near the misplaced pad's
reading, that pad is moved onto it. Any ambiguity (several candidate
holes, several fitting anchorings, a mirrored reading -- see
:func:`mirror_variants` -- moving the pad elsewhere) refuses the
deduction; :func:`measured_layout_outcome` names the refusal.

@note :func:`check_pad_layout` is a CHECK, never a measurement: no
    millimetre value is produced from pixels to replace the quotes. It
    answers only whether the given set of pad centres fits the drawn
    circles. :func:`measured_layout` measures the position of the single
    moved pad only; the other pads keep their quotes.
@note What it cannot see: pad NUMBERS (two pads with exchanged numbers form
    the same point set), a reading that omits pads (extra circles are
    allowed), errors smaller than the tolerance, a whole axis rescaled
    consistently when no body size is given, a mirrored reading of a
    mirror-symmetric layout (same point set), anisotropy and shear of a
    single-row reading, an error that a coincidental set of other circles
    on the page happens to fit (glyph counters are filtered, not excluded),
    a drawing not to scale (the drawn proportions confirm, the quoted ones
    are "distorted"), and on a page with both a bottom and a mirrored top
    view, which view the reading was taken from (both a reading and its
    mirror can confirm).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import math
from pathlib import Path
import threading
from typing import Mapping, Sequence

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Declared limits and constants
# ---------------------------------------------------------------------------

#: Image suffixes read by the check; any other file is skipped. GIF is
#: accepted by the documents panel (document_roles.SUFFIXES) and OpenCV does
#: not decode it, so _read_image reads it through Pillow (review S9,
#: 2026-11-03): a drawing uploaded as GIF is checked like any other.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})

#: Images whose long side exceeds this are downscaled before the circle
#: search. 2000 px keeps a 1 mm hole drawn at the usual 8-20 px/mm of a
#: datasheet page at several pixels of radius, while bounding the cost of the
#: matched filter (linear in the pixel count).
MAX_LONG_SIDE_PX = 2000

#: Readings with fewer pads are not checked (unverifiable "too_few_pads"):
#: two pads fix scale, rotation and position of the fit, so any two circles
#: in the right direction would "confirm" them; a third pad is the first
#: one that can disagree.
MIN_PADS = 3

#: A reading whose second singular value is below this fraction of the first
#: is treated as one row of pads.
COLLINEAR_RATIO = 1e-3

#: An affine fit (used to call a failed reading "distorted" and to bound the
#: extra displacement of a confirmed one) needs at least this many pads:
#: three pads determine an affine map exactly, so it would explain anything.
AFFINE_MIN_PADS = 4

#: ... and a spread across the main direction of at least this fraction
#: (second / first singular value). Below it the cross-row scale of an
#: affine fit is set by sub-pixel offsets: a single row whose pads zigzag by
#: +-0.127 mm has 0.02, a 2x20 header with 2.54 mm rows 0.09. Declared
#: heuristic; it only decides "distorted" versus "no_match", never a
#: confirmation.
AFFINE_MIN_SPREAD = 0.05

#: Declared bound: the tolerance must not exceed this fraction of the
#: reading's smallest centre-to-centre distance, otherwise the answer is
#: unverifiable ("low_resolution"). The typical wrong reading puts a pad
#: half a spacing away (between two holes); with the tolerance at most a
#: quarter of the spacing, that error is at least twice the tolerance.
LOW_RESOLUTION_FRACTION = 0.25

#: Readings with more pads are not checked (unverifiable "too_many_pads").
#: 100 covers 2x40 headers and DIP/SIP parts with margin; at 100 pads one
#: check of a 1900x1200 page took about 6 s on a laptop (40 pads: about 3 s).
MAX_PADS = 100

#: At most this many circles per image take part in the matching, kept by
#: decreasing ring coverage. A drawing page of one part shows far fewer holes;
#: the cap only bounds the cost on pages crowded with circle-like text.
MAX_CIRCLES = 300

#: Number of the longest pad-to-pad baselines of the reading used to build
#: similarity hypotheses. For a correct reading any one baseline whose two
#: pads sit on drawn circles suffices; three bound the O(B * C^2) search.
BASELINES = 3

#: Ink threshold as a fraction of the page's paper-to-ink contrast: a pixel is
#: ink when darker than paper - 0.35 * (paper - ink). It keeps the faint,
#: anti-aliased thin lines of low-resolution scans that Otsu (about 0.5) drops,
#: while JPEG ringing on white paper stays far below 35 % of the contrast.
INK_LEVEL = 0.35

#: Grey levels below the paper level (90th percentile) still counted as paper
#: by the photo gate.
PAPER_BAND = 20

#: Declared heuristic (photo gate): a line drawing is mostly paper. Measured on
#: the sample pages of the repository: drawings 0.92-0.95 of paper and
#: 0.03-0.07 of ink; product photos with dimension arrows drawn over them
#: 0.66-0.72 of paper and 0.19-0.28 of ink. A colour/texture classifier would
#: make these two numbers unnecessary.
PAPER_FRACTION_MIN = 0.85

#: Declared heuristic (photo gate), see :data:`PAPER_FRACTION_MIN`.
INK_FRACTION_MAX = 0.12

#: Smallest circle radius searched, px: below that a circle cannot be told
#: from a dot or a letter counter.
R_MIN_PX = 2.5

#: Largest circle radius searched, as a fraction of the page short side:
#: holes, not round bodies or frames.
R_MAX_FRACTION = 0.04

#: Radii searched per pyramid level (level pixels). Larger circles are
#: searched on the page halved once more, so the matched-filter kernels stay
#: small and the cost does not grow with the radius.
LEVEL_R_HIGH = 12.0

#: Minimum fraction of a ring that must be inked (+-1 px): a drawn circle is
#: closed; 15 % tolerates JPEG dropout and arrows touching it.
COVERAGE_MIN = 0.85

#: Maximum spread of the per-direction ink radius divided by the radius:
#: elongated glyph bowls exceed 0.15, drawn circles stay well below.
ROUND_TOLERANCE = 0.15

#: Declared heuristic: minimum fraction of directions with paper just inside
#: AND just outside the ring. Two full centre lines (four arms) spoil up to
#: about 40 % of the directions on a 4 px hole (measured 0.35 on the relay
#: drawing); solid fills and photo texture spoil more than 80 % (measured on
#: the ESP32 and RS485 product photos before the photo gate existed). A
#: vector source (PDF drawing with explicit circles) would make it
#: unnecessary.
PAPER_SIDES_MIN = 0.5

#: Work resolution factor of the sub-pixel ring tests: drawn holes are often
#: only 8-12 px wide on a page scan.
UPSCALE = 4

#: Hypothesis gate, px: a projected pad must fall within this distance of a
#: circle (about 1 px of centre noise plus drafting inaccuracy) ...
HYPOTHESIS_TOL_PX = 3.0

#: ... or within this fraction of the pad's distance from the nearer pad of
#: the baseline (whose two pads sit exactly on circles). It absorbs the
#: anisotropy of a scanned page (0.2-1.6 % measured on the sample drawings)
#: with margin. It only proposes assignments: the verdict is the fit's.
HYPOTHESIS_TOL_SPAN = 0.06

#: Two circles are "of about the same size" when their radii differ by less
#: than this factor; the hypothesis search prefers such circles. Declared
#: heuristic: pin-label glyph counters next to holes were half the hole radius
#: on the synthetic drawings and 0.6-1.1 of it on the sample pages, and pins
#: of one footprint are usually drawn with one or two close diameters.
#: Mounting holes three times larger are still reached as a fallback. An
#: explicit hole diameter in the reading would make it unnecessary.
SIMILAR_RADIUS = 1.6

#: Gate fraction of the second pass, run only when nothing confirmed: wide
#: enough for a drawing not to scale by about a third (a DIP whose 7.62 mm
#: rows are drawn at 10.16 mm, the audit case it was chosen on) to reach the
#: affine fit and be reported "distorted" instead of "no_match". Declared
#: heuristic: an affine hypothesis search from three correspondences would
#: make it unnecessary, at a much higher cost.
DISTORTION_GATE = 0.35

#: PER-HOLE term of the error budget: where ONE drawn hole centre may sit
#: against the fit. tolerance = max(0.15 mm, 3 px / scale). 3 px = about
#: 1 px of centre noise plus the drafting inaccuracy measured on the sample
#: drawings (residuals 0.02-0.13 mm = 0.3-1 px). It stays in pixels because
#: it does not grow with the size of the part: errors that DO grow with it
#: (a page stretched by its scan, a body drawn slightly off its quote) are
#: bounded as ratios, see PAGE_STRETCH_RATIO and BODY_SCALE_RATIO.
MIN_TOLERANCE_MM = 0.15
TOLERANCE_PX = 3.0

#: PROPORTIONAL term of the error budget (generality audit A10,
#: 2026-11-10): how much the page itself may be stretched along one axis or
#: sheared (anisotropy + shear of the affine map fitted on the pads, a
#: ratio) before the reading is called "distorted". A scanned or re-rendered
#: page is never exactly isotropic, and that error grows with the distance
#: between two holes, so a pixel tolerance refused correct readings of
#: large or finely scanned parts. Declared heuristic: the sample pages of
#: the repository measured 0.2-1.6 %; each reproduction step (print, copy,
#: scan) is commonly credited with about 1 % of magnification error (stated
#: from experience, no cited standard). What the bound gives up: a whole
#: axis misread by less than it (2.50 taken for 2.54 is 1.6 %) is not seen
#: -- it could not be told from the scan stretch of the samples anyway.
#: What would make it unnecessary: a vector drawing, or two printed quotes
#: per axis used to calibrate each axis on its own.
PAGE_STRETCH_RATIO = 0.02

#: Largest page rotation within which a fit confirms, degrees. The fit
#: itself removes a rotation (a similarity has one), so this is no bound on
#: the reading: it is the limit of the line-based steps of this module
#: (hole evidence from horizontal/vertical centre lines, the outline
#: rectangle, the removal of straight runs), which assume a page whose
#: lines run along the image axes. A fit beyond it is reported
#: "page_skewed" (unverifiable: deskew the page), never "distorted": a
#: rigidly turned page says nothing against the reading (audit A10).
MAX_ROTATION_DEG = 3.0

#: Largest page skew searched when nothing fits upright, degrees. Derived,
#: not tuned: a page turned by more than an eighth of a turn is nearer to
#: the next quarter-turn orientation, and a reading matching there would be
#: a reading in another orientation, not a skewed page.
MAX_SKEW_DEG = 45.0

#: Pads a fit must hold BEYOND the two that fix a similarity (scale,
#: rotation, position) before circles of any kind may confirm it. Each such
#: pad is one independent witness: it had to land on a circle. With a
#: single witness (three pads) one coincidence among the glyph counters of
#: a page is enough, so such a fit confirms only on circles drawn like
#: holes (:func:`_hole_evidence`), else it is "unanchored"
#: (audit A8). Measured on the five sample pages with random wrong
#: readings (2026-11-10, 200 per page and pad count): three pads were
#: "confirmed" 29 times in 1000 (up to 9.5 % on one page), four and five
#: pads never in 2000. Two is the smallest count at which a chance fit
#: needs two coincidences; it is a declared heuristic all the same (a page
#: crowded with hundreds of circles could still offer two). An explicit
#: hole list of the drawing (vector source) would make it unnecessary.
MIN_WITNESS_PADS = 2

#: Body check, PROPORTIONAL term: a body outline drawn within this ratio of
#: its quoted size (plus two per-hole tolerances for its two edges) agrees
#: with the reading. Declared heuristic, replacing a fixed 0.5 mm that
#: accepted a 16 x 12 mm body drawn at 95 % and refused a 64 x 48 mm one
#: drawn at 97 % (audit A10): datasheets quote bodies with +-0.2-0.5 mm on
#: 10-25 mm (1-5 %), often as maxima while the drawing shows the nominal
#: size; the sample outlines differ from their quotes by 0.4-2.7 % (T7NS
#: page: 22.5 x 16.5 mm quoted, about 21.9 x 16.1 mm drawn; VE12H: 20.5 x
#: 10.5 quoted, 20.6 x 10.7 drawn). What it must still catch are scale
#: errors of the reading -- another unit, a pitch of another series (2.0
#: for 2.54: 21 %; 3.5 for 3.81: 8 %) -- all far above it; the near pairs
#: (2.50 / 2.54) are below any outline's accuracy. A body quote read with
#: its printed tolerance would make it unnecessary.
BODY_SCALE_RATIO = 0.05

#: Accuracy of ONE edge of a drawn rectangle, px: an edge is the centre of
#: the band of rows (columns) of its line, quantised to half a pixel, and
#: anti-aliasing or JPEG blur shifts that centre by about as much. It is
#: the rectangle's own measurement uncertainty, with no part of the pads'
#: fit in it, used to compare the PROPORTIONS of a rectangle with those of
#: the declared body (:func:`_judge_outline`). Declared heuristic (no
#: measurement of the detector's edge error was made); a vector drawing
#: would make it unnecessary.
OUTLINE_EDGE_PX = 1.0

#: Values of ``body_check`` (:class:`LayoutCheck`, :class:`MeasuredLayout`):
#: what the drawn outline said about the declared body size.
BODY_AGREES = "agrees"
BODY_CONTRADICTED = "contradicted"
BODY_UNVERIFIABLE = "unverifiable"
BODY_NONE = "none"

#: Minimum coverage of the four sides of an outline rectangle, measured after
#: the dash gaps are bridged (see OUTLINE_GAP_STROKES): 10 % tolerates JPEG
#: dropout and gaps where text or arrows overlap. A side drawn with gaps
#: longer than the bridge (a sparse dotted line, a row of separate words)
#: stays below it.
OUTLINE_COVER_MIN = 0.90

#: Longest gap bridged along an outline side, in stroke widths (plus 1 px of
#: anti-aliasing). A body outline seen "on solder pins" is often drawn
#: dashed or dotted; ISO 128-20 spaces dashes and dots by 3 line widths, so
#: a gap up to that length is a gap of one drawn line, not a gap between two
#: different drawn things. Measured on the T7NS relay pages: gaps of 1-3 px
#: at a 1.9 px stroke.
OUTLINE_GAP_STROKES = 3.0

#: Thickest line, in stroke widths (plus 2 px of anti-aliasing and JPEG
#: blur), whose pixels may be bridged into an outline side: ISO 128-20 draws
#: thick lines twice as wide as thin ones. Ink thicker than that across the
#: side direction (a letter stem, a crossing line, a filled area) is dropped
#: BEFORE bridging, so a row of text cannot be closed into a line by its
#: vertical strokes or its filled blobs.
OUTLINE_THIN_STROKES = 2.0

#: A side that needs its dash gaps bridged must stand on its own: the strips
#: one dash gap wide on both sides of it may hold clutter (ink that is
#: neither a straight line nor an enclosed hole) along at most this fraction
#: of its length. The stems of left-aligned text, stacked line over line,
#: look like a dashed vertical line but carry the rest of their letters
#: beside them. Declared heuristic, calibrated on the T7NS and VE12H pages:
#: the dashed outlines around pin holes measured 0.00-0.14, labelled dashed
#: schematic boxes 0.19-0.27, stacked text stems 0.30-0.92 (all but one
#: above 0.39). The margin to text is THIN: this guard alone does not keep
#: text out, the rectangle must also close on four sides around the holes,
#: and the horizontal sides lose text rows earlier (whole letters are
#: dropped in :func:`_bridged_thin`). A solid side is not held to it (text
#: cannot draw one). A vector drawing (lines as objects with a dash style)
#: would make it unnecessary.
OUTLINE_NEIGHBOUR_MAX = 1 / 3

#: At most this many distinct circle assignments are refined per image.
MAX_ASSIGNMENTS = 50

#: Budget of the missed-hole search, in ring probes (one radius over one
#: search window, one filter on a small crop). When it runs out the image
#: is "unverifiable" ("search_limit") instead of searched further: about one
#: second of work on a laptop.
MAX_RING_PROBES = 3000

#: Images whose analysis (photo gate, ink mask, circles) is kept in memory,
#: least recently used dropped first. The extractor checks two to four
#: readings against the same few images.
ANALYSIS_CACHE_SIZE = 8

#: Longest closed contour taken for a slot or a land, as a multiple of the
#: largest circle radius searched (R_MAX_FRACTION): its short side obeys the
#: circle bound, its long side this one. Declared heuristic: a closed
#: outline longer than four hole radii is a cut-out or a body, not a hole;
#: a slot length declared by the reading would make it unnecessary.
SLOT_LENGTH_RADII = 4.0

#: Growth factor of the straight-run lengths removed before the closed
#: contours are searched again (see :func:`_find_regions`): a slot crossed
#: by its centre lines is whole only once the lines, longer than its sides,
#: are removed and its sides, shorter, are kept. Same step as the line-free
#: masks of the circle search (15 %), coarser: a slot's sides and its
#: centre lines differ in length by at least the slot's end arcs plus the
#: two diameters a centre line runs on outside (see :func:`_hole_evidence`).
REGION_LINE_STEP = 1.3

#: Mirror layouts tested against a confirmed or measured fit: the X flip
#: (the other face of the part seen through the page) and the Y flip (the
#: same, with the page turned). ONE definition for the measure of this
#: module and for the extractor's guard (:func:`mirror_variants`).
MIRROR_FLIPS = ((-1.0, 1.0), (1.0, -1.0))

#: Circle kind of each hole kind a reading may declare (``hole_kinds`` of
#: :func:`check_pad_layout`): terminal and mounting holes are drawn as rings,
#: slots as obrounds. An unknown kind binds nothing.
HOLE_KIND_CIRCLES = {"terminal": "ring", "mounting": "ring", "slot": "obround"}

#: Status values of :class:`LayoutCheck`.
CONFIRMED = "confirmed"
NOT_CONFIRMED = "not_confirmed"
UNVERIFIABLE = "unverifiable"

#: Reason of a failed fit whose pads cannot show whether the page is drawn
#: to scale (fewer than AFFINE_MIN_PADS pads, or nearly collinear ones): the
#: image could not tell "wrong" from "not to scale on one axis", so this is
#: NOT the losing verdict "no_match" is (review M18, 2026-11-04).
NO_MATCH_UNOBSERVABLE = "no_match_unobservable"

#: Reason of a line drawing with no circle drawn like a hole at all (an SMD
#: land pattern of rectangles, a page of text): nothing round to check
#: against (review M11, 2026-11-04). "too_few_circles" keeps meaning fewer
#: such circles than pads.
NO_ROUND_HOLES = "no_round_holes"

#: Reason of a fit that holds only on a page turned by more than
#: MAX_ROTATION_DEG: unverifiable, the page must be deskewed (audit A10).
PAGE_SKEWED = "page_skewed"

#: Reason of a fit that would confirm but may be a coincidence, because its
#: circles are not all drawn like holes AND it is one of the two fits a
#: coincidence satisfies most easily: fewer than MIN_WITNESS_PADS witness
#: pads, or pads that fit only on a stretched page (audit A8). Unverifiable.
UNANCHORED = "unanchored"

# Order of the not-confirmed stages: a later stage means the reading got
# further through the checks, so its reason is the more informative one.
# PAGE_SKEWED ranks just above "no_match" only: a fit at a rotation beyond
# the limit is the easiest one for a coincidence to satisfy (the cells
# between a ring and its centre lines offer many tilted fits), so it must
# never hide an upright fit that put every pad on a circle and was then
# found distorted or of another body scale.
_STAGES = ("no_match", PAGE_SKEWED, "distorted", "body_scale", "low_resolution",
           UNANCHORED, "confirmed")

# Stages that contradict a reading (the others confirm it or cannot tell).
_CONTRADICTING = ("no_match", "distorted", "body_scale")

# Stages of a fit the image cannot judge, most informative first.
_UNJUDGED = ("search_limit", "low_resolution", PAGE_SKEWED, UNANCHORED)


@dataclass(frozen=True)
class LayoutCheck:
    """@brief Outcome of :func:`check_pad_layout`.

    @note ``status`` is "confirmed", "not_confirmed" or "unverifiable";
        ``reason`` is a machine code ("confirmed", "too_few_pads",
        "too_many_pads", "no_image", "not_a_drawing", "no_round_holes",
        "too_few_circles", "low_resolution", "search_limit", "page_skewed",
        "unanchored", "no_match", "no_match_unobservable",
        "distorted", "body_scale"); ``image`` is
        the file name of the image that decided, None when none did;
        ``max_error_mm`` and ``tolerance_mm`` come from the best fit, None
        when no fit was made; ``circles`` is the number of circles found in
        the deciding image (all of them, glyph-like ones and slot centres
        included; for "too_few_circles" it may exceed the pad count when
        too few of them are drawn like holes).
    @note ``scale_observable`` (2026-11-04) says whether the stretch and
        shear test of the fit could be run on this reading: at least
        AFFINE_MIN_PADS pads not nearly collinear (see
        :func:`_affine_observable`). When False, a confirmation does not
        cover the page's scale along either axis and a failure is reported
        as "no_match_unobservable": the caller must not take it for
        "wrong".
    @note WHAT A CONFIRMATION COVERS (audit A7, 2026-11-10). "confirmed"
        means the PROPORTIONS of the reading are those of the drawn holes.
        A point set fits at any size, so the absolute scale -- the pitch,
        the unit -- is covered only when a drawn body outline ties it to
        the declared body size: ``body_check`` is "agrees" (a rectangle
        drawn around the holes has the declared sizes at the pads' scale,
        and no other rectangle could be the body at another scale),
        "contradicted" (no rectangle has the declared sizes at the pads'
        scale and one has the declared body's exact proportions at ANOTHER
        scale: status "not_confirmed", reason "body_scale"),
        "unverifiable" (a body was given, but the page does not say which
        rectangle is it: none drawn, or only rectangles of other
        proportions -- a page frame, a table cell, the frame around a
        chamfered body -- or one rectangle has the declared sizes while
        another could be the body at another scale (a step, a frame), or
        the fit never reached the body check; the whole decision table is
        in the module docstring) or
        "none" (no body was given). ``scale_verified`` is True only for
        "agrees". A confirmed reading with ``scale_verified`` False may
        have every value multiplied by one factor (a 2.0 pitch read for
        2.54, mils taken for mm): the caller must not present it as a
        verified size. Even True ties the pads to the DECLARED body only:
        pads and body both doubled agree with each other.
    """

    status: str
    reason: str
    image: str | None
    max_error_mm: float | None
    tolerance_mm: float | None
    circles: int
    scale_observable: bool = False
    body_check: str = BODY_NONE
    scale_verified: bool = False


@dataclass(frozen=True)
class _Circle:
    """A drawn circle in working-image pixels, or the centre of a drawn slot.

    ``kind`` is "ring" for a circle and "obround" for a slot (two
    semicircles of radius ``r`` joined by two parallel segments of length
    2 ``half_length`` along the unit axis (``ux``, ``uy``)); a slot takes
    part in the matching through its centre like a circle does.
    """

    x: float
    y: float
    r: float
    coverage: float
    #: Drawn like a hole (centre line through it, or isolated on paper), see
    #: :func:`_hole_evidence`; only such circles can contradict a reading.
    evidence: bool = False
    kind: str = "ring"
    half_length: float = 0.0
    ux: float = 1.0
    uy: float = 0.0


@dataclass(frozen=True)
class _Region:
    """A closed non-circular contour drawn on the page: a slot ("obround") or an outlined land ("rectangle").

    Working-image pixels: centre (``x``, ``y``), unit long axis (``ux``,
    ``uy``), ``half_length`` along it (an obround: half its straight part;
    a rectangle: half its long side) and ``half_width`` across it (an
    obround: the radius of its end arcs). ``contour`` is the boundary of
    the enclosed paper, for point-in-region tests; ``evidence`` as for
    :class:`_Circle`.
    """

    kind: str
    x: float
    y: float
    ux: float
    uy: float
    half_length: float
    half_width: float
    contour: np.ndarray
    evidence: bool = False

    def contains(self, x: float, y: float) -> bool:
        """True when the point (px) lies inside or on the enclosed contour."""
        return cv2.pointPolygonTest(self.contour, (float(x), float(y)), False) >= 0

    def as_circle(self) -> _Circle:
        """The slot as a matching circle: its centre, the radius of its end arcs, its axis."""
        return _Circle(self.x, self.y, self.half_width, 1.0, self.evidence, self.kind,
                       self.half_length, self.ux, self.uy)


@dataclass(frozen=True)
class _Fit:
    """Result of one refined assignment: its stage, its errors and what the body outline said.

    ``body_check`` is one of the BODY_* values; it stays BODY_NONE for a
    fit made without a body.
    """

    stage: str
    max_error_mm: float
    tolerance_mm: float
    body_check: str = BODY_NONE


@dataclass(frozen=True)
class _ImageResult:
    """Per-image outcome used to choose the overall answer.

    ``hole_like`` counts the circles drawn like holes (evidence), which
    tells "no_round_holes" (none at all) from "too_few_circles".
    """

    name: str
    drawing: bool
    circles: int
    fit: _Fit | None
    hole_like: int = 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check_pad_layout(images: Sequence[Path], pads_mm: Mapping[object, Sequence[float]],
                     body_mm: Sequence[float] | None = None, *,
                     hole_kinds: Mapping[object, str] | None = None,
                     reference_holes_mm: Sequence[Sequence[float]] | None = None) -> LayoutCheck:
    """@brief Tell whether a reading of pad centres fits the hole circles drawn in an image.

    @param images Candidate drawing images; files with other suffixes than
        png/jpg/jpeg/webp/bmp and unreadable files are skipped.
    @param pads_mm Pad number -> (x, y) in mm AS SEEN ON THE PAGE: X to the
        right, Y down, arbitrary origin. The reading is never mirrored: a
        mirrored fit does not confirm (see :func:`mirror_variants` for the
        layouts a caller tests the other face with).
    @param body_mm Optional (length along page X, width along page Y) in mm.
        When given, the rectangles drawn around the matched holes are
        compared with it at the fitted scale (:func:`_body_check`): the
        answer is ``body_check`` of the result. Only a rectangle with the
        body's own proportions at another scale contradicts the reading
        ("body_scale"), and only when no rectangle has the declared sizes
        at the pads' scale; with one that has, the page shows two
        candidates and verifies nothing (see :func:`_body_check` and the
        decision table of the module docstring). A rectangle of other
        proportions is not the body
        and leaves the status as it is, with ``scale_verified`` False.
    @param hole_kinds Optional pad number -> "terminal" | "mounting" | "slot",
        what the reading DECLARED each pad's hole to be (the extractor
        passes the kinds of the chain reading's stops). A declared pad is
        matched only to a drawn hole of that kind (HOLE_KIND_CIRCLES: rings
        for terminals and mounting holes, obround slots for slots); pads
        without a declared kind, unknown kinds and numbers not in
        ``pads_mm`` bind nothing.
    @param reference_holes_mm Optional (x, y) mm, in the frame of ``pads_mm``,
        of holes the reading declared that are NOT pads (mounting or
        reference holes it placed): an assignment putting a pad on the
        drawn hole of such a position does not count.
    @return A :class:`LayoutCheck`: "confirmed" if any image confirms; else
        "not_confirmed" if at least one line drawing had at least as many
        circles drawn like holes as the reading has pads (reason
        "no_match", or "no_match_unobservable" when the reading cannot show
        the page's scale, see ``scale_observable``); else "unverifiable"
        with the most informative reason ("too_few_pads" below MIN_PADS
        pads, "too_many_pads" above MAX_PADS, then "search_limit" >
        "low_resolution" > "page_skewed" > "too_few_circles" >
        "no_round_holes" > "unanchored" > "not_a_drawing" > "no_image").
        A pad whose reading lands inside a closed non-circular contour (a
        slot, an outlined land) is unverifiable, never a miss that
        contradicts. "page_skewed": the pads fit only on a page turned by
        more than MAX_ROTATION_DEG (deskew it); "unanchored": the pads fit
        circles that are not all drawn like holes, with fewer than
        MIN_WITNESS_PADS witness pads or only on a stretched page -- it
        may be a coincidence (see :func:`_anchored_on_holes`).
    @note A confirmation covers the proportions of the reading; whether it
        covers its absolute scale is told by ``scale_verified`` (see
        :class:`LayoutCheck`).
    @throws ValueError When a pad coordinate, a body size or a reference
        hole is not a finite number (a positive one for the body).
    @note Deterministic and local: no AI, no network, no millimetres derived
        from pixels.
    """
    points = _validated_points(pads_mm)
    body = _validated_body(body_mm)
    kinds = _required_kinds(list(pads_mm.keys()), hole_kinds)
    references = _validated_references(reference_holes_mm)
    observable = _affine_observable(points) if len(points) else False
    # A body that no fit compared with an outline is "unverifiable", not "none".
    unchecked = BODY_NONE if body is None else BODY_UNVERIFIABLE
    if len(points) < MIN_PADS:
        return LayoutCheck(UNVERIFIABLE, "too_few_pads", None, None, None, 0, observable, unchecked)
    if len(points) > MAX_PADS:
        return LayoutCheck(UNVERIFIABLE, "too_many_pads", None, None, None, 0, observable, unchecked)

    results: list[_ImageResult] = []
    confirmed: _ImageResult | None = None
    for path in images:
        path = Path(path)
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        analysis = _analyse(path)
        if analysis is None:
            continue
        result = _check_image(path.name, analysis, points, body, kinds, references)
        if result.fit is not None and result.fit.stage == "confirmed":
            # The first confirming image decides, unless a body was given
            # and that image could not tie it to an outline: a later image
            # that confirms AND shows the body covers the scale as well.
            if confirmed is None or result.fit.body_check == BODY_AGREES:
                confirmed = result
            if confirmed.fit.body_check in (BODY_NONE, BODY_AGREES):
                break
            continue
        results.append(result)
    if confirmed is not None:
        fit = confirmed.fit
        return LayoutCheck(CONFIRMED, "confirmed", confirmed.name, round(fit.max_error_mm, 4),
                           round(fit.tolerance_mm, 4), confirmed.circles, observable,
                           fit.body_check, fit.body_check == BODY_AGREES)
    return _summarise(results, observable, unchecked)


def mirror_variants(pads_mm: Mapping[object, Sequence[float]]) -> list[dict]:
    """@brief The mirror layouts of a reading: its X flip, then its Y flip (MIRROR_FLIPS).

    @param pads_mm Pad number -> (x, y) mm, page orientation.
    @return One dict per flip, same keys, coordinates as floats.
    @note The ONE definition of «the mirror» used by :func:`measured_layout`
        (a mirror that also fits refuses a move) and offered to the
        extractor's guard, so both test the same layouts (review low
        «mirror tested only in X», 2026-11-04). The first entry is the X
        flip the guard tested before.
    """
    return [{key: (float(value[0]) * fx, float(value[1]) * fy) for key, value in pads_mm.items()}
            for fx, fy in MIRROR_FLIPS]


# ---------------------------------------------------------------------------
# Input validation and image reading
# ---------------------------------------------------------------------------

def _validated_points(pads_mm: Mapping[object, Sequence[float]]) -> np.ndarray:
    """Return the reading as an (n, 2) float array; reject non-finite values."""
    rows = []
    for key, value in pads_mm.items():
        x, y = (float(v) for v in value)
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError(f"pad {key!r}: non-finite coordinate {value!r}")
        rows.append((x, y))
    return np.array(rows, float).reshape(-1, 2)


def _validated_references(reference_holes_mm: Sequence[Sequence[float]] | None) -> np.ndarray:
    """Return the declared reference holes as an (m, 2) float array (m may be 0); reject non-finite values."""
    rows = []
    for value in reference_holes_mm or ():
        x, y = (float(v) for v in value)
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError(f"reference hole: non-finite coordinate {value!r}")
        rows.append((x, y))
    return np.array(rows, float).reshape(-1, 2)


def _required_kinds(keys: Sequence[object], hole_kinds: Mapping[object, str] | None) -> list[str | None]:
    """Circle kind each pad is bound to by the declared hole kinds (None = any), in pad order.

    Keys are compared as given and as text, so "5" and 5 declare the same
    pad; unknown kinds bind nothing (see HOLE_KIND_CIRCLES).
    """
    if not hole_kinds:
        return [None] * len(keys)
    declared = {str(key): HOLE_KIND_CIRCLES.get(str(kind)) for key, kind in hole_kinds.items()}
    return [declared.get(str(key)) for key in keys]


def _validated_body(body_mm: Sequence[float] | None) -> tuple[float, float] | None:
    """Return the body size as a pair of positive floats, or None."""
    if body_mm is None:
        return None
    length, width = (float(v) for v in body_mm)
    if not (math.isfinite(length) and math.isfinite(width) and length > 0 and width > 0):
        raise ValueError(f"body size must be two positive finite numbers, got {body_mm!r}")
    return length, width


def _read_image(path: Path) -> np.ndarray | None:
    """Read an image as BGR, transparent pixels composited on white; None if unreadable.

    The bytes are decoded from memory so that non-ASCII Windows paths work.
    A format OpenCV does not decode (GIF) is read through Pillow, first frame
    only, as RGBA.
    """
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    except (OSError, ValueError, cv2.error):
        return None
    if image is None:
        image = _read_with_pillow(data)
    if image is None or image.size == 0:
        return None
    if image.dtype != np.uint8:
        # 16-bit PNG and similar: bring to 8 bit by the full range of the type.
        image = (image.astype(np.float64) * 255.0 / np.iinfo(image.dtype).max).astype(np.uint8) \
            if np.issubdtype(image.dtype, np.integer) else None
        if image is None:
            return None
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        # A drawing exported with a transparent background is black on
        # transparent: composite on white paper, not on black.
        alpha = image[:, :, 3:4].astype(np.float32) / 255.0
        rgb = image[:, :, :3].astype(np.float32)
        return (rgb * alpha + 255.0 * (1.0 - alpha)).round().astype(np.uint8)
    return image[:, :, :3]


def _read_with_pillow(data: np.ndarray) -> np.ndarray | None:
    """Decode bytes OpenCV refused (GIF) with Pillow as BGRA; None when Pillow refuses them too."""
    import io
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data.tobytes())) as picture:
            rgba = np.asarray(picture.convert("RGBA"))
    except Exception:  # noqa: BLE001 - any decoder failure means «not readable», as above
        return None
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)


def _working_grey(image: np.ndarray) -> np.ndarray:
    """Grey image, downscaled so that its long side is at most MAX_LONG_SIDE_PX."""
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    long_side = max(grey.shape)
    if long_side > MAX_LONG_SIDE_PX:
        factor = MAX_LONG_SIDE_PX / long_side
        grey = cv2.resize(grey, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    return grey


# ---------------------------------------------------------------------------
# Ink mask and photo gate
# ---------------------------------------------------------------------------

def _ink_mask(grey: np.ndarray) -> tuple[np.ndarray, float]:
    """Ink mask relative to the page's own paper and ink levels, and the paper level.

    Paper = 90th percentile of grey (most of a drawing page is paper), ink =
    0.5th percentile. See :data:`INK_LEVEL`.
    """
    paper = float(np.percentile(grey, 90))
    ink = float(np.percentile(grey, 0.5))
    threshold = paper - INK_LEVEL * max(paper - ink, 1.0)
    return np.where(grey < threshold, 255, 0).astype(np.uint8), paper


def _is_line_drawing(grey: np.ndarray, mask: np.ndarray, paper: float) -> bool:
    """Photo gate: True when the page is mostly paper with little ink (declared heuristic)."""
    paper_fraction = float((grey > paper - PAPER_BAND).mean())
    ink_fraction = float((mask > 0).mean())
    return paper_fraction >= PAPER_FRACTION_MIN and ink_fraction <= INK_FRACTION_MAX


# ---------------------------------------------------------------------------
# Circle detection
# ---------------------------------------------------------------------------

def _stroke_width(mask: np.ndarray) -> float:
    """Typical line width in px: twice the median distance-transform value on stroke ridges."""
    dt = cv2.distanceTransform((mask > 0).astype(np.uint8), cv2.DIST_L2, 3)
    ridge = (dt > 0) & (dt >= cv2.dilate(dt, np.ones((3, 3), np.uint8)))
    values = dt[ridge]
    return float(max(1.0, 2 * np.median(values))) if values.size else 1.0


def _ring_kernel(radius: float, filled: bool = False, inner: float = 0.0, ss: int = 8) -> np.ndarray:
    """Normalised ring (1 px thick) or annulus kernel, supersampled and area-averaged."""
    half = int(math.ceil(radius)) + 2
    size = 2 * half + 1
    big = np.zeros((size * ss, size * ss), np.uint8)
    centre = (size * ss) // 2
    if filled:
        cv2.circle(big, (centre, centre), max(1, int(round(radius * ss))), 255, -1, lineType=cv2.LINE_AA)
        if inner > 0:
            cv2.circle(big, (centre, centre), int(round(inner * ss)), 0, -1, lineType=cv2.LINE_AA)
    else:
        cv2.circle(big, (centre, centre), int(round(radius * ss)), 255, ss, lineType=cv2.LINE_AA)
    kernel = cv2.resize(big, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32)
    return kernel / max(float(kernel.sum()), 1e-6)


def _remove_hv_lines(mask: np.ndarray, length: int) -> np.ndarray:
    """Mask without horizontal / vertical straight runs of at least ``length`` px.

    Centre lines and extension lines through a hole would otherwise pull the
    centre fit; the removal is widened by 1 px across the line only.
    """
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1)))
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, length)))
    horizontal = cv2.dilate(horizontal, np.ones((3, 1), np.uint8))
    vertical = cv2.dilate(vertical, np.ones((1, 3), np.uint8))
    return cv2.bitwise_and(mask, cv2.bitwise_not(horizontal | vertical))


def _fit_circle(x: np.ndarray, y: np.ndarray, weights: np.ndarray) -> tuple[float, float, float]:
    """Weighted algebraic (Kasa) least-squares circle fit."""
    a = np.column_stack([x, y, np.ones_like(x)]) * weights[:, None]
    b = (x * x + y * y) * weights
    sol, *_ = np.linalg.lstsq(a, b, rcond=None)
    cx, cy = sol[0] / 2, sol[1] / 2
    return float(cx), float(cy), math.sqrt(max(float(sol[2] + cx * cx + cy * cy), 0.0))


def _weighted_fit(free: np.ndarray, dark: np.ndarray, cx: float, cy: float, r: float,
                  band: float) -> tuple[float, float, float]:
    """Re-fit a circle on the line-free ink of its ring, weighted by darkness.

    Keeps the filter estimate when the fit runs away (too few or one-sided points).
    """
    height, width = free.shape
    half = int(r + band) + 2
    x0, y0 = max(0, int(cx) - half), max(0, int(cy) - half)
    x1, y1 = min(width, int(cx) + half + 1), min(height, int(cy) + half + 1)
    ys, xs = np.nonzero(free[y0:y1, x0:x1])
    xs = xs + x0
    ys = ys + y0
    sel = np.abs(np.hypot(xs - cx, ys - cy) - r) <= band
    if sel.sum() < 8:
        return cx, cy, r
    ncx, ncy, nr = _fit_circle(xs[sel].astype(float), ys[sel].astype(float),
                               dark[ys[sel], xs[sel]].astype(float))
    if math.hypot(ncx - cx, ncy - cy) > 0.5 * r or abs(nr - r) > 0.5 * r:
        return cx, cy, r
    return ncx, ncy, nr


def _fine(mask: np.ndarray, xs_u: np.ndarray, ys_u: np.ndarray) -> np.ndarray:
    """Sample the mask at UPSCALE-times coordinates (nearest neighbour), out of image = paper.

    Equivalent to indexing a nearest-neighbour upscaled copy of the mask
    without allocating it.
    """
    height, width = mask.shape
    xi = np.floor(xs_u / UPSCALE).astype(int)
    yi = np.floor(ys_u / UPSCALE).astype(int)
    inside = (xi >= 0) & (xi < width) & (yi >= 0) & (yi < height)
    out = np.zeros(xi.shape, bool)
    out[inside] = mask[yi[inside], xi[inside]] > 0
    return out


def _fine_dilated(mask: np.ndarray, xs_u: np.ndarray, ys_u: np.ndarray) -> np.ndarray:
    """As :func:`_fine` on the upscaled mask dilated by +-UPSCALE/2 fine pixels (+-0.5 px)."""
    half = UPSCALE // 2
    out = np.zeros(np.shape(xs_u), bool)
    for dx in (-half, half):
        for dy in (-half, half):
            out |= _fine(mask, xs_u + dx, ys_u + dy)
    return out


def _ring_samples(cx: float, cy: float, radii: np.ndarray, count: int) -> tuple[np.ndarray, np.ndarray]:
    """Rounded sample coordinates on ``count`` directions x ``radii`` around a centre."""
    angles = np.linspace(0, 2 * math.pi, count, endpoint=False)
    xs = np.round(cx + np.outer(np.cos(angles), radii))
    ys = np.round(cy + np.outer(np.sin(angles), radii))
    return xs, ys


def _ring_coverage(mask: np.ndarray, cu: float, cv: float, ru: float) -> float:
    """Fraction of the ring (fine coordinates) inked on the slightly dilated mask."""
    xs, ys = _ring_samples(cu, cv, np.array([ru]), 96)
    return float(_fine_dilated(mask, xs, ys).mean())


def _radial_profile(mask: np.ndarray, cu: float, cv: float, ru: float) -> np.ndarray:
    """Per direction, the ink radius closest to ``ru`` within [0.5, 1.5] ru (nan = none)."""
    radii = np.arange(0.5 * ru, 1.5 * ru, 0.5)
    xs, ys = _ring_samples(cu, cv, radii, 72)
    hit = _fine(mask, xs, ys)
    distance = np.where(hit, np.abs(radii[None, :] - ru), np.inf)
    best = distance.argmin(axis=1)
    profile = radii[best].astype(float)
    profile[~hit.any(axis=1)] = np.nan
    return profile


def _is_localised(mask: np.ndarray, cu: float, cv: float, ru: float) -> bool:
    """True when the ring is a closed curve around its centre, not a stretch of a longer structure.

    The same ring moved by one radius along +-X or +-Y must NOT pass the
    ring coverage test (COVERAGE_MIN) again. Moved by r, a third of the
    ring (a 120 degree arc through the old centre) runs across the hole's
    interior, which is paper apart from the crossings of its centre lines:
    a drawn hole keeps at most about 2/3 of its coverage there (0.45-0.73
    measured on the T7NS and VE12H relay pages). Two parallel lines, a
    solid bar beside a line or a thick stroke give the same coverage at any
    position along their direction (0.91-0.99 on the VE12H leads and body
    wall), which the paper-inside test cannot see once crossing-line
    directions are left out of it. No new threshold: "would be accepted as
    a ring there too" is the detector's own coverage criterion.
    """
    for du, dv in ((ru, 0.0), (-ru, 0.0), (0.0, ru), (0.0, -ru)):
        if _ring_coverage(mask, cu + du, cv + dv, ru) >= COVERAGE_MIN:
            return False
    return True


def _paper_sides(mask: np.ndarray, cu: float, cv: float, ru: float, margin: float,
                 lines: Sequence[tuple[str, int]] = ()) -> tuple[float, float]:
    """Fractions of directions with paper inside [0.3r, r-m] and outside [r+m, r+m+0.5r].

    ``lines`` are the crossing lines of the circle (see :func:`_crossing_lines`:
    working-image rows of horizontal lines, columns of vertical ones). A
    direction whose samples are ALL ink lying on the pixels of such a line
    tells nothing about fill or texture: it is left out of the fraction
    instead of being counted as ink. Ink off those pixels still counts, so
    a solid disc or bar crossed by a line keeps failing. Why: the inner band
    of a hole of 3 px radius lies about 1 px from its centre, where two 1 px
    centre lines alone cover most directions (paper on 0.29-0.53 of them on
    the T7NS relay page, against the PAPER_SIDES_MIN of 0.5) although the
    quarters between them are paper. A side with no direction left counts
    as 0 (no paper shown).
    """
    rows = [row for axis, row in lines if axis == "h"]
    cols = [col for axis, col in lines if axis == "v"]
    fractions = []
    for lo, hi in ((0.3 * ru, ru - margin), (ru + margin, ru + margin + 0.5 * ru)):
        if hi <= lo:
            hi = lo + 1
        xs, ys = _ring_samples(cu, cv, np.linspace(lo, hi, 6), 72)
        hit = _fine(mask, xs, ys)
        paper = (~hit).any(axis=1)
        judged = np.ones(len(paper), bool)
        if rows or cols:
            on_line = np.isin(np.floor(ys / UPSCALE).astype(int), list(rows)) \
                | np.isin(np.floor(xs / UPSCALE).astype(int), list(cols))
            judged = paper | ~(hit & on_line).all(axis=1)
        fractions.append(float(paper[judged].sum() / judged.sum()) if judged.any() else 0.0)
    return fractions[0], fractions[1]


def _is_glyph(circle: _Circle, ink: np.ndarray, others: Sequence[_Circle] = ()) -> bool:
    """True when the circle is (part of) a character of text. Declared heuristics.

    A. Own component: the ink component carrying the ring has the height of
       a text line (<= 3 d), is not much wider than three of its own heights
       (a glyph or a few touching glyphs, not a long line), and sticks out of
       the circle by > 0.3 d on only one or two sides, massively (>= half the
       ring's own ink): a bowl of B, P, R, 5, 6, 8, 9, a, e... A hole is
       either isolated, crossed by centre lines (sticks out thinly on 3-4
       sides) or attached to a long line (> 3 d).
    B. Neighbours: another small component of similar height (0.5-1.6 x),
       vertically overlapping >= 50 %, within 0.6 d to the left or right: a
       letter in a word. A hole keeps a clearance of about its diameter from
       its labels, which are usually offset diagonally. A neighbour that
       carries another candidate circle of about the same size (see
       SIMILAR_RADIUS) is the next hole of a row, not a letter.

    Tuned on: the relay and TEL-8 pages of the repository and synthetic
    pages drawn with OpenCV's Hershey font (digits 0, 4, 5, 6, 8, 9 at
    12 px). The 0.3 d protrusion, the half-ring "massive" test, the 3 d
    size bound and the 0.6 d / 0.5-1.6 x neighbour bands separate those
    glyph bowls from those holes; the 0.6 d gap had only a few percent of
    margin on Hershey zeros. Known misses: a slashed zero, the counter of a
    4, a "0" between hyphens. What would make them unnecessary: text
    recognition (OCR boxes) of the page, removing every glyph before the
    circle search.
    """
    height, width = ink.shape
    d = 2 * circle.r
    # Crop wide enough to hold a few touching glyphs of the circle's size.
    pad = int(math.ceil(d * 4))
    x0, y0 = max(0, int(circle.x - pad)), max(0, int(circle.y - pad))
    x1, y1 = min(width, int(circle.x + pad) + 1), min(height, int(circle.y + pad) + 1)
    sub = ink[y0:y1, x0:x1].astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(sub, connectivity=8)
    cx0, cx1 = circle.x - circle.r, circle.x + circle.r
    cy0, cy1 = circle.y - circle.r, circle.y + circle.r
    angles = np.linspace(0, 2 * math.pi, 48, endpoint=False)
    rx = np.clip(np.round(circle.x + circle.r * np.cos(angles)).astype(int) - x0, 0, sub.shape[1] - 1)
    ry = np.clip(np.round(circle.y + circle.r * np.sin(angles)).astype(int) - y0, 0, sub.shape[0] - 1)
    ring_labels = labels[ry, rx]
    ring_labels = ring_labels[ring_labels > 0]
    own = int(np.bincount(ring_labels).argmax()) if ring_labels.size else 0
    glyph_height = d
    if own:
        bx, by, bw, bh, _ = stats[own]
        bx += x0
        by += y0
        touches_crop = bx <= x0 or by <= y0 or bx + bw >= x1 or by + bh >= y1
        if not touches_crop and bh <= 3 * d and bw <= 3 * max(d, bh):
            extents = [cx0 - bx, (bx + bw) - cx1, cy0 - by, (by + bh) - cy1]
            sides = sum(e > 0.3 * d for e in extents)
            yy, xx = np.nonzero(labels == own)
            rr = np.hypot(xx + x0 - circle.x, yy + y0 - circle.y)
            outside = int((rr > circle.r + 1.5).sum())
            ring_ink = int((np.abs(rr - circle.r) <= 1.5).sum())
            if sides in (1, 2) and outside >= 0.5 * max(ring_ink, 1):
                return True
            glyph_height = max(d, bh)
    for index in range(1, count):
        if index == own:
            continue
        bx, by, bw, bh, area = stats[index]
        bx += x0
        by += y0
        if area < 3 or not (0.5 * glyph_height <= bh <= 1.6 * glyph_height) or bw > 3 * glyph_height:
            continue
        # A neighbour carrying another circle of about the same size is the
        # next hole of a row (its box widened by centre-line dashes), not a
        # letter: a row of holes on a centre line would otherwise read as a
        # word.
        if any(o is not circle and bx <= o.x <= bx + bw and by <= o.y <= by + bh
               and 1 / SIMILAR_RADIUS <= o.r / circle.r <= SIMILAR_RADIUS for o in others):
            continue
        overlap = min(by + bh, cy1) - max(by, cy0)
        if overlap < 0.5 * min(bh, d):
            continue
        gap = max(bx - cx1, cx0 - (bx + bw))
        if -1.5 <= gap <= 0.6 * d:
            return True
    return False


def _level_candidates(mask: np.ndarray, factor: int, r_low: float, r_high: float
                      ) -> list[tuple[float, float, float, float]]:
    """Ring matched filter on one pyramid level; candidates in working pixels.

    Score per radius r = ring coverage on the dilated mask minus the ink on
    two probe rings just inside and just outside it (a straight line crosses
    a ring in two points only, a concentric stroke or a glyph covers much of
    it). Returns (score, x, y, r) after a plateau suppression.
    """
    height, width = mask.shape
    sw = _stroke_width(mask)
    margin = sw / 2 + 1.0
    dilated = cv2.dilate(mask, np.ones((3, 3), np.uint8)).astype(np.float32) / 255.0
    plain = (mask > 0).astype(np.float32)
    best = np.full((height, width), -9, np.float32)
    best_r = np.zeros((height, width), np.float32)
    cache: dict[tuple[str, float], np.ndarray] = {}

    def ring_response(radius: float, source: np.ndarray, key: str) -> np.ndarray:
        """Ring-kernel response of ``source`` at a quarter-pixel quantised radius."""
        quantised = (key, round(radius * 4) / 4)
        if quantised not in cache:
            cache[quantised] = cv2.filter2D(source, -1, _ring_kernel(quantised[1]),
                                            borderType=cv2.BORDER_CONSTANT)
        return cache[quantised]

    for r in np.arange(r_low, r_high, 0.5):
        ring = ring_response(r, dilated, "d")
        r_out = r + margin + 0.5
        outer = ring_response(r_out, plain, "p")
        r_in = r - margin - 0.5
        inner = ring_response(r_in, plain, "p") if r_in >= 2.5 else np.zeros_like(ring)
        # A line crossing a probe ring costs about 2 w / (2 pi r) per line;
        # the budget leaves room for four such crossings.
        limit_out = min(0.6, 0.25 + 4 * sw / (2 * math.pi * r_out))
        limit_in = min(0.6, 0.25 + 4 * sw / (2 * math.pi * max(r_in, 1)))
        ok = (ring >= COVERAGE_MIN) & (inner <= limit_in) & (outer <= limit_out)
        score = np.where(ok, ring - 0.5 * (inner + outer), -9)
        better = score > best
        best[better] = score[better]
        best_r[better] = r
    ys, xs = np.nonzero(best > -9)
    order = np.argsort(-best[ys, xs], kind="stable")
    taken = np.zeros((height, width), np.uint8)
    out = []
    for index in order:
        y, x = int(ys[index]), int(xs[index])
        if taken[y, x]:
            continue
        r = float(best_r[y, x])
        # Suppress only the plateau around the candidate (0.35 r): the cells
        # between a hole and its crosshair are small round candidates
        # themselves and would otherwise hide the hole's own centre.
        cv2.circle(taken, (x, y), max(1, int(round(0.35 * r))), 1, -1)
        out.append((float(best[y, x]), (x + 0.5) * factor - 0.5, (y + 0.5) * factor - 0.5, r * factor))
    return out


def _find_circles(grey: np.ndarray, mask: np.ndarray) -> list[_Circle]:
    """Small drawn circles (holes, pins seen end-on), verified and glyph-filtered.

    Candidates come from a ring matched filter on a pyramid (radii 2.5-12 px
    on the page, then 6-12 px on the page halved, quartered...), so the
    kernels stay small for every radius. Each candidate is refined on the
    line-free ink of the working image and verified there at sub-pixel
    resolution: ring coverage, roundness, paper on both sides, not a glyph.
    """
    height, width = mask.shape
    r_max = max(R_MIN_PX + 1, R_MAX_FRACTION * min(height, width))
    candidates: list[tuple[float, float, float, float]] = []
    level, factor = 0, 1
    while True:
        r_low = R_MIN_PX if level == 0 else LEVEL_R_HIGH / 2
        if r_low * factor > r_max:
            break
        r_high = min(LEVEL_R_HIGH, r_max / factor + 0.5)
        if level == 0:
            level_mask = mask
        else:
            # Max-pool the ink so thin lines survive the reduction.
            level_mask = (cv2.resize(mask, (max(1, width // factor), max(1, height // factor)),
                                     interpolation=cv2.INTER_AREA) > 0).astype(np.uint8) * 255
        if min(level_mask.shape) < 2 * r_low + 4:
            break
        candidates += _level_candidates(level_mask, factor, r_low, r_high)
        level += 1
        factor *= 2
    candidates.sort(key=lambda item: -item[0])

    dark = (255 - grey.astype(np.int16)).clip(1, 255)
    sw = _stroke_width(mask)
    margin = sw / 2 + 1.0
    ink = mask > 0
    free_cache: dict[int, np.ndarray] = {}

    def free_for(radius: float) -> np.ndarray:
        """Line-free ink for a radius; the run length is quantised in 15 % steps."""
        length = max(6, int(math.ceil(2.2 * radius)))  # longer than any near-straight chord
        quantised = 6
        while quantised < length:
            quantised = int(math.ceil(quantised * 1.15))
        if quantised not in free_cache:
            free_cache[quantised] = _remove_hv_lines(mask, quantised) > 0
        return free_cache[quantised]

    found: list[_Circle] = []
    for _score, x, y, r in candidates:
        free = free_for(r)
        cx, cy, rr = x, y, r
        for _ in range(2):
            cx, cy, rr = _weighted_fit(free, dark, cx, cy, rr, band=max(1.5, sw))
        cu, cv, ru = (cx + 0.5) * UPSCALE - 0.5, (cy + 0.5) * UPSCALE - 0.5, rr * UPSCALE
        coverage = _ring_coverage(mask, cu, cv, ru)
        if coverage < COVERAGE_MIN:
            continue
        profile = _radial_profile(mask, cu, cv, ru)
        near = np.abs(profile - ru) <= 0.2 * ru
        if np.mean(near) < COVERAGE_MIN:
            continue
        if float(np.nanstd(profile[near]) / ru) > ROUND_TOLERANCE:
            continue
        inside, outside = _paper_sides(mask, cu, cv, ru, margin * UPSCALE,
                                       _crossing_lines(cx, cy, rr, ink, sw))
        if inside < PAPER_SIDES_MIN or outside < PAPER_SIDES_MIN:
            continue
        if not _is_localised(mask, cu, cv, ru):
            continue
        found.append(_Circle(cx, cy, rr, coverage))

    found.sort(key=lambda c: (-c.coverage, -c.r))
    kept: list[_Circle] = []
    for circle in found:
        duplicate = any(math.hypot(circle.x - k.x, circle.y - k.y) < 0.5 * max(circle.r, k.r)
                        and abs(circle.r - k.r) < 0.3 * max(circle.r, k.r) for k in kept)
        if not duplicate:
            kept.append(circle)
    # A small circle whose centre lies inside a bigger one, not concentric with
    # it, conflicts with it: two drawn holes never overlap, so one of the two
    # is not a hole. Either the small one is a cell between a hole outline
    # and its centre lines (or a glyph counter), or the big one is a
    # composite of a small hole and a leader or extension line touching it
    # (VE12H relay page: the true hole at ring coverage 1.0 inside a 0.90
    # composite). A drawn circle is a closed stroke, a composite has gaps
    # where it leaves the ring: the less covered of the two goes; on equal
    # coverage the small one goes, as before.
    kept = [c for c in kept
            if not any((_overlaps(c, k) and k.coverage >= c.coverage)
                       or (_overlaps(k, c) and k.coverage > c.coverage) for k in kept)]
    kept = [c for c in kept if not _is_glyph(c, ink, kept)][:MAX_CIRCLES]
    return [replace(c, evidence=_hole_evidence(c, ink, sw)) for c in kept]


def _overlaps(small: _Circle, big: _Circle) -> bool:
    """True when ``small``'s centre lies inside ``big`` (1.4 times larger or more), not concentric."""
    return big.r > 1.4 * small.r and 0.2 * big.r < math.hypot(small.x - big.x, small.y - big.y) < big.r


def _bridge(run: np.ndarray, gap: int) -> np.ndarray:
    """1-D closing: fill the paper gaps of at most ``gap`` samples between two inked samples."""
    out = run.copy()
    inked = np.nonzero(run)[0]
    for a, b in zip(inked, inked[1:]):
        if 1 < b - a <= gap + 1:
            out[a:b] = True
    return out


def _crossing_lines(x: float, y: float, r: float, ink: np.ndarray, sw: float,
                    first_only: bool = False) -> list[tuple[str, int]]:
    """Horizontal / vertical lines through a circle's centre that continue outside it.

    Returns ("h", row) / ("v", column) pixel indices of the working image.
    A line counts when it is inked across the centre (+-0.6 r, at least 50 %
    after bridging dashes over gaps of up to 2 stroke widths + 1 px), runs on
    outside the ring for at least two diameters on one side (50 % inked after
    bridging) and is thin there (paper on both sides across it for at least
    70 % of its inked samples). Offsets of -1, 0 and +1 px from the centre
    are tried, so a line drawn a pixel off the fitted centre is found. These
    are the drafting conventions documented in :func:`_hole_evidence`; with
    ``first_only`` the search stops at the first line.
    """
    height, width = ink.shape
    step = 0.5
    gap = int(round((2 * sw + 1) / step))

    def inked(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """Ink at rounded sample positions; outside the image counts as paper."""
        xi, yi = np.round(xs).astype(int), np.round(ys).astype(int)
        ok = (xi >= 0) & (xi < width) & (yi >= 0) & (yi < height)
        out = np.zeros(xi.shape, bool)
        out[ok] = ink[yi[ok], xi[ok]]
        return out

    found: list[tuple[str, int]] = []
    inner = np.arange(-0.6 * r, 0.6 * r + 1e-9, step)
    outer = np.arange(r + sw + 1, 5 * r + 1e-9, step)
    for dx, dy in ((1.0, 0.0), (0.0, 1.0)):
        for offset in (-1.0, 0.0, 1.0):
            px, py = x + dy * offset, y + dx * offset
            centre_ok = _bridge(inked(px + dx * inner, py + dy * inner), gap).mean() >= 0.5
            if not centre_ok:
                continue
            for sign in (-1.0, 1.0):
                xs, ys = px + sign * dx * outer, py + sign * dy * outer
                run = inked(xs, ys)
                if not run.size or _bridge(run, gap).mean() < 0.5:
                    continue
                # A line is thin: beside its inked samples, on both sides
                # across it, there is paper. A row through a line of text
                # crosses strokes that extend across it, so bridging the
                # gaps between letters does not make it a line.
                side = sw + 1.5
                clear = ~inked(xs[run] + dy * side, ys[run] + dx * side) \
                    & ~inked(xs[run] - dy * side, ys[run] - dx * side)
                if clear.mean() >= 0.7:
                    found.append(("h", int(round(py))) if dx else ("v", int(round(px))))
                    if first_only:
                        return found
                    break
    return found


def _hole_evidence(circle: _Circle, ink: np.ndarray, sw: float) -> bool:
    """True when the circle is drawn the way a hole is drawn, not only shaped like one.

    Declared heuristic, from drafting convention: a hole is either marked by
    a horizontal or vertical line through its centre that continues outside
    it for at least two diameters on one side (centre line or extension line
    running to a dimension; dashed and dotted lines are bridged over gaps up
    to 2 stroke widths + 1 px; no stroke of a glyph runs that far straight),
    or it stands alone on paper (no
    ink within one diameter outside its ring). Glyph counters fail both: a
    slashed zero is crossed diagonally, a bar of e or theta stops at the
    glyph, and letters stand next to each other. A hole drawn without centre
    lines but with a label inside that clearance is not counted (a
    conservative miss: the image then cannot contradict a reading).

    Tuned on: the relay and TEL-8 pages (every drawn hole counted) and two
    synthetic text pages (all 58 glyph circles of a digits page refused, 4
    of 13 on a mixed paragraph accepted, through "-0-" hyphen rows). The
    numbers: line beyond two diameters, at least 50 % inked after bridging,
    paper on both sides of at least 70 % of its samples (a row through text
    crosses strokes that extend across it), isolation ring free of ink up
    to 2 % of its samples. What would make it unnecessary: text recognition
    of the page, or vector drawings whose holes are explicit circles.
    """
    r = circle.r
    if _crossing_lines(circle.x, circle.y, r, ink, sw, first_only=True):
        return True
    return _isolated(ink, circle.x, circle.y, 1.0, 0.0, 0.0, r, sw)


def _capsule_samples(x: float, y: float, ux: float, uy: float, half_length: float, rho: float,
                     count: int) -> tuple[np.ndarray, np.ndarray]:
    """``count`` points at distance ``rho`` from the segment of half-length ``half_length`` along (ux, uy) through (x, y).

    The offset curve of a slot: two semicircles of radius ``rho`` around the
    segment's ends joined by two parallel lines. With ``half_length`` 0 it
    is the circle of radius ``rho``, so one sampler serves rings and slots.
    """
    perimeter = 2 * math.pi * rho + 4 * half_length
    s = np.linspace(0, perimeter, count, endpoint=False)
    # Walk the perimeter: top side, far arc, bottom side, near arc.
    side, arc = 2 * half_length, math.pi * rho
    local_s = np.empty_like(s)
    local_t = np.empty_like(s)
    on_top = s < side
    local_s[on_top], local_t[on_top] = -half_length + s[on_top], -rho
    on_far = (s >= side) & (s < side + arc)
    angle = (s[on_far] - side) / rho - math.pi / 2
    local_s[on_far], local_t[on_far] = half_length + rho * np.cos(angle), rho * np.sin(angle)
    on_bottom = (s >= side + arc) & (s < 2 * side + arc)
    local_s[on_bottom], local_t[on_bottom] = half_length - (s[on_bottom] - side - arc), rho
    on_near = s >= 2 * side + arc
    angle = (s[on_near] - 2 * side - arc) / rho + math.pi / 2
    local_s[on_near], local_t[on_near] = -half_length + rho * np.cos(angle), rho * np.sin(angle)
    xs = x + local_s * ux - local_t * uy
    ys = y + local_s * uy + local_t * ux
    return xs, ys


def _isolated(ink: np.ndarray, x: float, y: float, ux: float, uy: float, half_length: float,
              r: float, sw: float) -> bool:
    """True when nothing is inked from one stroke outside a ring or slot out to one diameter further.

    Offsets r + sw + 1.5 to 3 r in 1 px steps, 72 samples per offset (a
    circle) or proportionally more along a slot's longer offset curves;
    2 % of the samples tolerate isolated JPEG specks. The isolation half of
    :func:`_hole_evidence`, shared with slots.
    """
    height, width = ink.shape
    offsets = np.arange(r + sw + 1.5, 3 * r + 1e-9, 1.0)
    if offsets.size == 0:
        return False
    hits = []
    for rho in offsets:
        count = int(round(72 * (2 * math.pi * rho + 4 * half_length) / (2 * math.pi * rho)))
        xs, ys = _capsule_samples(x, y, ux, uy, half_length, rho, count)
        xi, yi = np.round(xs).astype(int), np.round(ys).astype(int)
        ok = (xi >= 0) & (xi < width) & (yi >= 0) & (yi < height)
        out = np.zeros(xi.shape, bool)
        out[ok] = ink[yi[ok], xi[ok]]
        hits.append(out)
    return float(np.concatenate(hits).mean()) <= 0.02


# ---------------------------------------------------------------------------
# Closed non-circular contours: slots and outlined lands
# ---------------------------------------------------------------------------

def _shape_region(cell: np.ndarray, x0: int, y0: int, sw: float) -> _Region | None:
    """Model the enclosed paper ``cell`` (bool crop at offset x0, y0) as an obround or a rectangle, or None.

    The cell's long axis comes from its principal directions; its extents
    along and across the axis give the two candidate models: a capsule of
    end radius r = half the short extent and a straight part of the long
    extent minus the short one, and the rectangle of the two extents. The
    model whose boundary the cell's contour follows more closely (RMS
    distance) is chosen, and must follow it within ROUND_TOLERANCE of the
    half-width (the detector's own roundness criterion; a one-pixel floor
    for the contour's quantisation). A cell the detector would take for
    round (radial spread within ROUND_TOLERANCE) is left to the circle
    search. Half a stroke is added to both extents so that, like a circle's
    radius, they measure the drawn line's centre, not the paper inside it.
    The dashes and dots of centre lines drawn through the hole, no wider
    than a stroke, are closed before the contour is modelled.
    """
    raw = cell.astype(np.uint8)
    contours, hierarchy = cv2.findContours(raw, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    outer = [i for i in range(len(contours)) if hierarchy[0][i][3] < 0]
    index = max(outer, key=lambda i: cv2.contourArea(contours[i]))
    # Enclosed paper with ink drawn inside it is a frame around something
    # -- a body outline, a land around its hole -- not a hole: a hole's
    # interior is empty paper apart from the dashes, dots and crossings of
    # its centre lines: pieces no thicker than a stroke that enclose no
    # paper themselves (a ring does), which are let through.
    child = hierarchy[0][index][2]
    while child >= 0:
        if hierarchy[0][child][2] >= 0:
            return None
        bx, by, bw, bh = cv2.boundingRect(contours[child])
        filled = np.zeros((bh + 2, bw + 2), np.uint8)
        cv2.drawContours(filled, [contours[child] - np.array([bx - 1, by - 1])], -1, 1, -1)
        # The contour of a paper hole runs on the paper around the ink:
        # the island's thickness is measured on the ink inside it.
        ink_crop = np.pad(1 - raw, 1)[by:by + bh + 2, bx:bx + bw + 2]
        island = (filled & ink_crop).astype(np.uint8)
        if float(cv2.distanceTransform(island, cv2.DIST_L2, 3).max()) > sw / 2 + 1:
            return None
        child = hierarchy[0][child][0]
    # Close the thin ink pieces (islands, and dashes attached to the
    # outline) so that the contour follows the drawn outline, not them.
    k = int(math.ceil(sw)) + 2
    padded = np.pad(raw, k)
    closed = cv2.morphologyEx(padded, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))[k:-k, k:-k]
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if len(contour) < 8:
        return None
    pts = contour.reshape(-1, 2).astype(float)
    centred = pts - pts.mean(axis=0)
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    ux, uy = float(vt[0, 0]), float(vt[0, 1])
    s = centred @ np.array([ux, uy])
    t = centred @ np.array([-uy, ux])
    long_extent, short_extent = float(np.ptp(s)), float(np.ptp(t))
    if short_extent <= 0 or long_extent <= 0:
        return None
    s_mid, t_mid = (s.max() + s.min()) / 2, (t.max() + t.min()) / 2
    s, t = s - s_mid, t - t_mid
    cx = pts[:, 0].mean() + s_mid * ux - t_mid * uy
    cy = pts[:, 1].mean() + s_mid * uy + t_mid * ux
    # Roundness, as the circle detector measures it: spread of the boundary
    # radius over the directions relative to its mean.
    radius = np.hypot(s, t)
    if float(radius.std() / max(radius.mean(), 1e-9)) <= ROUND_TOLERANCE:
        return None
    half_short, half_long = short_extent / 2, long_extent / 2
    straight = max(half_long - half_short, 0.0)
    capsule = np.abs(np.hypot(np.maximum(np.abs(s) - straight, 0.0), t) - half_short)
    rectangle = np.minimum(np.abs(half_long - np.abs(s)), np.abs(half_short - np.abs(t)))
    rms_capsule = float(np.sqrt((capsule ** 2).mean()))
    rms_rectangle = float(np.sqrt((rectangle ** 2).mean()))
    kind = "obround" if rms_capsule <= rms_rectangle else "rectangle"
    if min(rms_capsule, rms_rectangle) > max(ROUND_TOLERANCE * half_short, 1.0):
        return None
    half_width = half_short + sw / 2
    half_length = straight if kind == "obround" else half_long + sw / 2
    shifted = (contour.reshape(-1, 2) + np.array([x0, y0])).astype(np.int32).reshape(-1, 1, 2)
    return _Region(kind, float(cx + x0), float(cy + y0), ux, uy, half_length, half_width, shifted)


def _find_regions(mask: np.ndarray, circles: Sequence[_Circle], sw: float) -> list[_Region]:
    """Closed non-circular contours of hole size: slots (obrounds) and outlined lands (rectangles).

    An enclosed paper area (a component of the paper that does not reach
    the page border) bounded like a hole (short side within the circle
    bound 2 R_MAX, long side within SLOT_LENGTH_RADII radii) is modelled by
    :func:`_shape_region`. The search is repeated on the mask with its
    straight horizontal and vertical runs of growing length removed (see
    REGION_LINE_STEP): a slot crossed by centre lines is split into cells
    on the plain mask and is whole once the lines, longer than its sides,
    are gone. A contour found on an earlier pass keeps the cells of later
    passes out (their centres lie inside it); a contour lying within a
    detected circle (a cell of a crossed hole), a contour enclosing drawn
    ink (a frame, see :func:`_shape_region`) and a contour that is a glyph
    (see :func:`_is_glyph`, with the contour's half-length as radius) are
    dropped. ``evidence`` is drawn-like-a-hole as for circles: a centre
    line through it, or isolation. A circle whose centre lies inside a
    kept contour is resolved by :func:`_without_composites`.

    @note Only outlined contours are seen: a filled land (solid or hatched)
        has no enclosed paper. Capped at MAX_CIRCLES contours.
    """
    height, width = mask.shape
    r_max = max(R_MIN_PX + 1, R_MAX_FRACTION * min(height, width))
    long_max = SLOT_LENGTH_RADII * r_max
    ink = mask > 0
    lengths: list[int | None] = [None]
    length = 6
    while length <= long_max + 4:
        lengths.append(length)
        length = int(math.ceil(length * REGION_LINE_STEP))
    found: list[_Region] = []
    # A removed line leaves a gap, as wide as the removal (3 px across the
    # line), in every contour it crossed; a closing two pixels wider
    # restores the contour, and only where a line was removed (the
    # closing must not solidify a dashed line elsewhere).
    restore = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    for length in lengths:
        if length is None:
            work = mask
        else:
            removed = _remove_hv_lines(mask, length)
            band = cv2.dilate(mask & cv2.bitwise_not(removed), np.ones((3, 3), np.uint8))
            work = removed | (cv2.morphologyEx(removed, cv2.MORPH_CLOSE, restore) & band)
        paper = (work == 0).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(paper, connectivity=4)
        for index in range(1, count):
            bx, by, bw, bh, _area = (int(v) for v in stats[index])
            if bx <= 0 or by <= 0 or bx + bw >= width or by + bh >= height:
                continue
            if min(bw, bh) < 2 * R_MIN_PX or min(bw, bh) > 2 * r_max + 4 or max(bw, bh) > long_max + 4:
                continue
            region = _shape_region(labels[by:by + bh, bx:bx + bw] == index, bx, by, sw)
            if region is None:
                continue
            if any(k.contains(region.x, region.y) for k in found):
                continue
            if any(_within_circle(region, c) for c in circles):
                continue
            found.append(region)
            if len(found) >= MAX_CIRCLES:
                break
        if len(found) >= MAX_CIRCLES:
            break
    kept = [r for r in found
            if not _is_glyph(_Circle(r.x, r.y, r.half_length + r.half_width, 1.0), ink, circles)]
    return [replace(r, evidence=_region_evidence(r, ink, sw)) for r in kept]


def _within_circle(region: _Region, circle: _Circle) -> bool:
    """True when the whole contour lies inside the circle (a cell between a hole's outline and its centre lines)."""
    pts = region.contour.reshape(-1, 2).astype(float)
    return bool((np.hypot(pts[:, 0] - circle.x, pts[:, 1] - circle.y) <= circle.r + 1.0).all())


def _without_composites(circles: Sequence[_Circle], regions: Sequence[_Region]) -> list[_Circle]:
    """The circles whose centre lies inside no closed contour.

    An enclosed paper contour of hole size with nothing drawn inside it is
    a closed witness of ONE hole; a circle centred inside it is a
    composite of the contour's strokes and the lines crossing them (a slot
    crossed by centre lines gives rings of its arcs, sides and lines), as
    two drawn holes never overlap (see :func:`_find_circles`).
    """
    return [c for c in circles if not any(r.contains(c.x, c.y) for r in regions)]


def _region_evidence(region: _Region, ink: np.ndarray, sw: float) -> bool:
    """True when the contour is drawn the way a hole is: a centre line through it, or isolated on paper.

    The crossing-line test of :func:`_hole_evidence` is run with the
    contour's half-length plus half-width as radius (the line must run on
    outside the whole contour); the isolation test samples the contour's
    offset curves (see :func:`_isolated`).
    """
    reach = region.half_length + region.half_width
    if _crossing_lines(region.x, region.y, reach, ink, sw, first_only=True):
        return True
    return _isolated(ink, region.x, region.y, region.ux, region.uy, region.half_length,
                     region.half_width, sw)


# ---------------------------------------------------------------------------
# Outline rectangle (body scale check)
# ---------------------------------------------------------------------------

def _bridged_thin(mask: np.ndarray, horizontal: bool, stroke: float) -> np.ndarray:
    """Thin ink along one direction with the dash gaps of a line closed.

    Pixels whose ink run ACROSS the direction is longer than a thick line
    (OUTLINE_THIN_STROKES) are dropped first; the remaining pixels are then
    closed along the direction over gaps of up to OUTLINE_GAP_STROKES stroke
    widths, so a dashed or dotted line becomes one continuous run while a
    solid line is unchanged.
    """
    thick = int(math.ceil(OUTLINE_THIN_STROKES * stroke)) + 2
    gap = int(math.ceil(OUTLINE_GAP_STROKES * stroke)) + 1
    across = (1, thick + 1) if horizontal else (thick + 1, 1)
    long_across = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, across))
    thin = cv2.bitwise_and(mask, cv2.bitwise_not(long_across))
    # Pixel by pixel the top and bottom arcs of a round letter are thin too;
    # a whole piece of ink that spreads across the direction further than a
    # thick line (a letter, a symbol, a diagonal) is not a dash or a dot.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(thin, connectivity=8)
    extent = stats[:, cv2.CC_STAT_HEIGHT if horizontal else cv2.CC_STAT_WIDTH]
    keep = extent <= thick
    keep[0] = False
    thin = np.where(keep[labels], 255, 0).astype(np.uint8)
    # Closed with a kernel 3 px across: an anti-aliased line drawn at a
    # sub-pixel offset puts consecutive dashes on adjacent rows (columns).
    along = (gap + 1, 3) if horizontal else (3, gap + 1)
    return cv2.morphologyEx(thin, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, along))


def _line_bands(mask: np.ndarray, horizontal: bool, min_length: int,
                stroke: float | None = None) -> tuple[np.ndarray, np.ndarray, list[tuple[int, int]]]:
    """Straight runs of at least ``min_length`` px and the row/column bands holding them.

    Returns (solid, dashed, bands): ``solid`` holds the straight runs of the
    ink itself; ``dashed`` those of the thin ink with its dash gaps bridged
    (see :func:`_bridged_thin`), empty when ``stroke`` is None; ``bands``
    are the runs of consecutive rows (columns) holding either.
    """
    shape = (min_length, 1) if horizontal else (1, min_length)
    element = cv2.getStructuringElement(cv2.MORPH_RECT, shape)
    solid = cv2.morphologyEx(mask, cv2.MORPH_OPEN, element)
    if stroke is None:
        dashed = np.zeros_like(solid)
    else:
        dashed = cv2.morphologyEx(_bridged_thin(mask, horizontal, stroke), cv2.MORPH_OPEN, element)
    profile = ((solid | dashed) > 0).sum(axis=1 if horizontal else 0)
    indices = np.nonzero(profile)[0]
    bands: list[tuple[int, int]] = []
    for i in indices:
        i = int(i)
        if bands and i - bands[-1][1] <= 1:
            bands[-1] = (bands[-1][0], i)
        else:
            bands.append((i, i))
    return solid, dashed, bands


def _outline_rect(mask: np.ndarray, circles: Sequence[_Circle]) -> tuple[float, float, float, float] | None:
    """Smallest drawn rectangle enclosing all the circles: (left, top, right, bottom) px, or None.

    The first of :func:`_outline_rects`. It is A drawn rectangle, not
    necessarily the body: nothing but its size against a declared body
    (:func:`_judge_outline`) can tell a body outline from a keep-out
    frame, a table cell or the page border.
    """
    return next(_outline_rects(mask, circles), None)


def _outline_rects(mask: np.ndarray, circles: Sequence[_Circle]):
    """Every drawn rectangle enclosing all the circles, smallest area first: (left, top, right, bottom) px.

    A generator: the caller stops at the first rectangle it can use (the
    body check asks which of them, if any, is the declared body, see
    :func:`_body_check`); listing them all costs no more than finding that
    none closes.

    Each side is a straight horizontal or vertical band lying outside every
    circle, and the four sides close into a rectangle (each side is measured
    between the centres of the two sides it meets). A side counts when:

    - its ink alone covers at least OUTLINE_COVER_MIN of its length (a solid
      line), or
    - its thin ink with the dash gaps bridged (see :func:`_bridged_thin`)
      covers OUTLINE_COVER_MIN AND the strips beside it, one dash gap wide
      on each side, hold clutter (ink that is neither a straight line nor
      one of the enclosed holes) along at most OUTLINE_NEIGHBOUR_MAX of its
      length (a dashed or dotted line standing on its own, not the aligned
      stems of a column of text with the rest of their letters beside them).

    @note Same image axes as the mask: x to the right, y down, px.
    """
    height, width = mask.shape
    min_length = max(8, int(0.04 * min(height, width)))
    stroke = _stroke_width(mask)
    gap = int(math.ceil(OUTLINE_GAP_STROKES * stroke)) + 1
    h_solid, h_dashed, rows = _line_bands(mask, True, min_length, stroke)
    v_solid, v_dashed, cols = _line_bands(mask, False, min_length, stroke)
    grow = np.ones((3, 3), np.uint8)

    def along_x(lines: np.ndarray) -> np.ndarray:
        """Cumulative count, along each row, of pixels within 1 px of a line."""
        return np.cumsum(np.pad(cv2.dilate(lines, grow) > 0, ((0, 0), (1, 0))), axis=1)

    def along_y(lines: np.ndarray) -> np.ndarray:
        """Cumulative count, along each column, of pixels within 1 px of a line."""
        return np.cumsum(np.pad(cv2.dilate(lines, grow) > 0, ((1, 0), (0, 0))), axis=0)

    h_solid_ok, h_dashed_ok = along_x(h_solid), along_x(h_dashed)
    v_solid_ok, v_dashed_ok = along_y(v_solid), along_y(v_dashed)
    # What stands beside a side: ink that is neither a straight line (a
    # crossing or parallel line is drawing, not clutter) nor one of the
    # holes the rectangle encloses (holes often sit close to the outline).
    lines = cv2.dilate(h_solid | h_dashed | v_solid | v_dashed, grow)
    holes = np.zeros_like(mask)
    for c in circles:
        if c.half_length > 0:
            # A slot: its whole capsule is hole, not clutter.
            p0 = (int(round(c.x - c.half_length * c.ux)), int(round(c.y - c.half_length * c.uy)))
            p1 = (int(round(c.x + c.half_length * c.ux)), int(round(c.y + c.half_length * c.uy)))
            cv2.line(holes, p0, p1, 255, 2 * int(math.ceil(c.r + 2)))
        cv2.circle(holes, (int(round(c.x)), int(round(c.y))), int(math.ceil(c.r + 2)), 255, -1)
    clutter = (mask > 0) & (lines == 0) & (holes == 0)
    left_edge = min(c.x - c.r - c.half_length for c in circles)
    right_edge = max(c.x + c.r + c.half_length for c in circles)
    top_edge = min(c.y - c.r - c.half_length for c in circles)
    bottom_edge = max(c.y + c.r + c.half_length for c in circles)

    def centre(band: tuple[int, int]) -> float:
        """Centre of a band of rows or columns."""
        return (band[0] + band[1]) / 2

    tops = [b for b in rows if centre(b) < top_edge]
    bottoms = [b for b in rows if centre(b) > bottom_edge]
    lefts = [b for b in cols if centre(b) < left_edge]
    rights = [b for b in cols if centre(b) > right_edge]

    def cover(ok: np.ndarray, horizontal: bool, band: tuple[int, int], a: int, b: int) -> float:
        """Best covered fraction of a side over the rows (columns) of its band."""
        if horizontal:
            return max((ok[r, b] - ok[r, a]) / max(1, b - a) for r in range(band[0], band[1] + 1))
        return max((ok[b, c] - ok[a, c]) / max(1, b - a) for c in range(band[0], band[1] + 1))

    def crowded(horizontal: bool, band: tuple[int, int], a: int, b: int) -> float:
        """Fraction of a side's length with clutter in the strips beside it (1 px clear of the band)."""
        limit = height if horizontal else width
        before = slice(max(0, band[0] - 1 - gap), max(0, band[0] - 1))
        after = slice(min(limit, band[1] + 2), min(limit, band[1] + 2 + gap))
        if horizontal:
            near = clutter[before, a:b].any(axis=0) | clutter[after, a:b].any(axis=0)
        else:
            near = clutter[a:b, before].any(axis=1) | clutter[a:b, after].any(axis=1)
        return float(near.mean()) if near.size else 1.0

    verdicts: dict[tuple, bool] = {}

    def side_ok(horizontal: bool, band: tuple[int, int], a: int, b: int) -> bool:
        """True when a side between ``a`` and ``b`` is drawn solid, or dashed and on its own."""
        key = (horizontal, band, a, b)
        if key not in verdicts:
            solid_ok, dashed_ok = (h_solid_ok, h_dashed_ok) if horizontal else (v_solid_ok, v_dashed_ok)
            verdicts[key] = (cover(solid_ok, horizontal, band, a, b) >= OUTLINE_COVER_MIN
                             or (cover(dashed_ok, horizontal, band, a, b) >= OUTLINE_COVER_MIN
                                 and crowded(horizontal, band, a, b) <= OUTLINE_NEIGHBOUR_MAX))
        return verdicts[key]

    combos = [(t, b, l, r) for t in tops for b in bottoms for l in lefts for r in rights]
    combos.sort(key=lambda q: (centre(q[3]) - centre(q[2])) * (centre(q[1]) - centre(q[0])))
    for t, b, l, r in combos:
        xa, xb = int(centre(l)), int(centre(r))
        ya, yb = int(centre(t)), int(centre(b))
        if (side_ok(True, t, xa, xb) and side_ok(True, b, xa, xb)
                and side_ok(False, l, ya, yb) and side_ok(False, r, ya, yb)):
            yield centre(l), centre(t), centre(r), centre(b)


def _within_allowance(outline_px: tuple[float, float], body: tuple[float, float], scale: float,
                      tolerance_px: float) -> bool:
    """True when both sizes of a rectangle (width, height px) are the body's (mm) AT THE PADS' SCALE.

    Each within two per-hole tolerances (the two edges of a side against a
    scale that the pads fix no better than that) plus the proportional
    drafting allowance BODY_SCALE_RATIO (:func:`body_allowance_mm`, in
    pixels).

    @note NECESSARY for an agreement, never sufficient (review finding
        V1-1, 2026-11-11). The allowance is several percent of each size,
        taken side by side: a clearance frame one margin outside the body
        falls inside it for a whole range of wrongly scaled readings. Alone
        it says "nothing excludes this size", not "this is the body"; what
        else is drawn around the holes decides (:func:`_body_check`).
    """
    return all(abs(drawn - size * scale) <= 2 * tolerance_px + BODY_SCALE_RATIO * size * scale
               for drawn, size in zip(outline_px, body))


def _has_body_proportions(outline_px: tuple[float, float], body: tuple[float, float],
                          drafting_ratio: float = 0.0) -> bool:
    """True when ONE scale maps both body sizes (mm) onto a rectangle (width, height px).

    Scale-free: nothing of the pads enters the test, so it holds or fails
    whatever scale the reading was taken at.

    - ``drafting_ratio`` 0 (the default): within the rectangle's own
      measurement uncertainty alone (two edges of OUTLINE_EDGE_PX per
      side). The rectangle has the declared proportions as exactly as it
      can be measured: it is IDENTIFIED as the body.
    - ``drafting_ratio`` BODY_SCALE_RATIO: each side may also be off its
      quote by the drafting allowance, at that one scale. The rectangle
      COULD be the body, drawn as loosely as the allowance admits; so could
      a frame a small margin from it.
    """
    own = 2 * OUTLINE_EDGE_PX
    # Scales (px per mm) at which each body size alone could be the drawn
    # one: the proportions are the body's when the two ranges meet.
    lowest = max((drawn - own) / (size * (1 + drafting_ratio)) for drawn, size in zip(outline_px, body))
    highest = min((drawn + own) / (size * (1 - drafting_ratio)) for drawn, size in zip(outline_px, body))
    return lowest <= highest


# What ONE rectangle says about a declared body (:func:`_judge_outline`).
# "Identified": the body's proportions within the measurement uncertainty.
# "Loose": only within the drafting allowance. "Here": its sizes are the
# body's at the pads' scale. "Elsewhere": they are not.
_IDENTIFIED_HERE = "identified_here"
_IDENTIFIED_ELSEWHERE = "identified_elsewhere"
_LOOSE_HERE = "loose_here"
_LOOSE_ELSEWHERE = "loose_elsewhere"


def _judge_outline(outline_px: tuple[float, float], body: tuple[float, float], scale: float,
                   tolerance_px: float) -> str | None:
    """What ONE drawn rectangle (width, height px) says about a body size (mm), or None when nothing.

    Two independent questions: does it have the body's PROPORTIONS
    (:func:`_has_body_proportions`, scale-free, asked exactly and then
    within the drafting allowance), and are its sizes the body's AT THE
    PADS' SCALE (:func:`_within_allowance`)?

    - _IDENTIFIED_HERE: exact proportions, sizes at the pads' scale.
    - _IDENTIFIED_ELSEWHERE: exact proportions, at another scale than the
      pads were read at.
    - _LOOSE_HERE: sizes at the pads' scale, proportions off by more than
      the measurement uncertainty. A body drawn loosely (the sample pages
      draw bodies up to 1.5 % out of proportion) -- or a frame that happens
      to fall inside the allowance of a wrongly scaled reading (V1-1).
    - _LOOSE_ELSEWHERE: proportions within the drafting allowance only,
      sizes not at the pads' scale. A loosely drawn body at another scale,
      or a step or a frame a small margin from the body.
    - None: other proportions altogether (a page frame, a table cell, a
      keep-out area, the frame around a chamfered body; audit A9). A body
      with ONE size misread falls here too: that is a mistake of the body
      reading, which must not judge the pads.

    @note Limits of the identification, declared. A rectangle that merely
        shares the body's proportions is taken for the body: a square
        frame around a square body that is not drawn as a rectangle, or a
        frame a constant margin outside a nearly square body. The fewer
        pixels the rectangle spans, the wider "the same proportions" is.
        The body's POSITION against the pads would identify it far better;
        the reading does not carry it to this module.
    """
    here = _within_allowance(outline_px, body, scale, tolerance_px)
    if _has_body_proportions(outline_px, body):
        return _IDENTIFIED_HERE if here else _IDENTIFIED_ELSEWHERE
    if here:
        return _LOOSE_HERE
    return _LOOSE_ELSEWHERE if _has_body_proportions(outline_px, body, BODY_SCALE_RATIO) else None


def _body_check(mask: np.ndarray, holes: Sequence[_Circle], body: tuple[float, float], scale: float,
                tolerance_px: float) -> tuple[str, tuple[float, float, float, float] | None]:
    """Compare a declared body with the rectangles drawn around the matched holes.

    EVERY rectangle is judged (:func:`_judge_outline`) before anything is
    decided, because the order they come in (smallest first) says nothing
    about which one is the body. The decision table, with its reasons, is
    in the module docstring ("Body check: the decision table"):

    1. something at the pads' scale (identified or loose) and nothing
       elsewhere: BODY_AGREES, with the rectangle at the pads' scale
       nearest the declared sizes;
    2. something at the pads' scale AND something elsewhere (identified or
       loose): BODY_UNVERIFIABLE, no rectangle -- two candidates for the
       body at two scales;
    3. nothing at the pads' scale, one identified elsewhere:
       BODY_CONTRADICTED, with the smallest such rectangle;
    4. nothing at the pads' scale and none identified elsewhere (loose
       elsewhere only, other proportions only, nothing drawn):
       BODY_UNVERIFIABLE, no rectangle.
    """
    found: dict[str, list[tuple[float, float, float, float]]] = {
        _IDENTIFIED_HERE: [], _IDENTIFIED_ELSEWHERE: [], _LOOSE_HERE: [], _LOOSE_ELSEWHERE: []}
    for rect in _outline_rects(mask, holes):
        verdict = _judge_outline((rect[2] - rect[0], rect[3] - rect[1]), body, scale, tolerance_px)
        if verdict is not None:
            found[verdict].append(rect)

    def distance(rect: tuple[float, float, float, float]) -> float:
        """Largest relative gap between a rectangle's sizes and the declared ones at the pads' scale."""
        return max(abs(drawn - size * scale) / (size * scale)
                   for drawn, size in zip((rect[2] - rect[0], rect[3] - rect[1]), body))

    here = found[_IDENTIFIED_HERE] + found[_LOOSE_HERE]
    if here:
        if found[_IDENTIFIED_ELSEWHERE] or found[_LOOSE_ELSEWHERE]:
            return BODY_UNVERIFIABLE, None
        return BODY_AGREES, min(here, key=distance)
    if found[_IDENTIFIED_ELSEWHERE]:
        return BODY_CONTRADICTED, found[_IDENTIFIED_ELSEWHERE][0]
    return BODY_UNVERIFIABLE, None


# ---------------------------------------------------------------------------
# Matching a reading to the circles
# ---------------------------------------------------------------------------

def _hypothesis_assignments(points: np.ndarray, circles: Sequence[_Circle],
                            max_misses: int = 0,
                            gate_fraction: float = HYPOTHESIS_TOL_SPAN,
                            kinds: Sequence[str | None] | None = None,
                            max_rotation_deg: float = MAX_ROTATION_DEG + 2) -> list[np.ndarray]:
    """Circle assignments (one distinct circle per pad, -1 = none) proposed by similarity hypotheses.

    Each of the BASELINES longest pad pairs is mapped onto every ordered pair
    of circles whose direction differs by at most ``max_rotation_deg``
    (MAX_ROTATION_DEG + 2 degrees unless a skewed page is searched, see
    :func:`_skewed_fit`; no mirror: the reading is in page orientation). A hypothesis survives
    only while at most ``max_misses`` pads project farther than the gate
    distance from every circle; pads are tested farthest-from-the-baseline
    first, so most hypotheses are dropped after a few pads. Cost:
    O(BASELINES * C^2 + surviving * n * C); with ``max_misses`` > 0 the
    BASELINES longest pairs leaving each pad out are baselines too (up to
    n * BASELINES baselines), so that a misread pad taking part in every
    longest pair does not hide the anchoring of the others. Assignments with fewer
    misses come first. ``gate_fraction`` scales the gate with the pad's
    distance from the nearer baseline pad. ``kinds`` (one circle kind or None per
    pad, see :func:`_required_kinds`) restricts a pad to circles of its
    declared kind: a declared slot is never put on a ring, nor a declared
    terminal on a slot.
    """
    n = len(points)
    q = points[:, 0] + 1j * points[:, 1]
    centres = np.array([c.x + 1j * c.y for c in circles])
    radii = np.array([c.r for c in circles])
    circle_kinds = np.array([c.kind for c in circles])
    allowed = np.ones((n, len(circles)), bool)
    for p, kind in enumerate(kinds or ()):
        if kind is not None:
            allowed[p] = circle_kinds == kind
    pair_i, pair_j = np.triu_indices(n, 1)
    lengths = np.abs(q[pair_j] - q[pair_i])
    min_spacing = float(lengths.min())
    by_length = np.argsort(-lengths, kind="stable")
    baselines = list(by_length[:BASELINES])
    if max_misses > 0:
        # An anchoring that leaves pad p out must be reachable from a
        # baseline without p: when the misread pad takes part in every
        # longest pair (a pad below a short row), the BASELINES longest
        # pairs that leave each pad out are added (at most n * BASELINES
        # baselines; a page not to scale rotates every baseline off its
        # axes, so one pair per pad is not enough).
        for p in range(n):
            without = [int(k) for k in by_length if pair_i[k] != p and pair_j[k] != p][:BASELINES]
            baselines += [k for k in without if k not in baselines]
    a_idx, b_idx = np.nonzero(~np.eye(len(circles), dtype=bool))
    found: dict[tuple[int, ...], np.ndarray] = {}
    for k in baselines:
        i, j = int(pair_i[k]), int(pair_j[k])
        dq = q[j] - q[i]
        z = (centres[b_idx] - centres[a_idx]) / dq  # scale * exp(i rotation)
        keep = np.abs(np.degrees(np.angle(z))) <= max_rotation_deg
        # Two pads closer than two tolerances could share one circle's
        # neighbourhood: such a scale cannot tell pads apart.
        keep &= np.abs(z) * min_spacing >= 2 * TOLERANCE_PX
        keep &= allowed[i][a_idx] & allowed[j][b_idx]
        za, origin = z[keep], centres[a_idx[keep]]
        if za.size == 0:
            continue
        base_r = (radii[a_idx[keep]] + radii[b_idx[keep]]) / 2
        assign = np.full((za.size, n), -1, int)
        misses = np.zeros(za.size, int)
        # The two baseline pads sit exactly on their circles, so a small
        # anisotropy or shear displaces a pad in proportion to its distance
        # from the nearer of them: the gate grows with that distance.
        anchor = np.minimum(np.abs(q - q[i]), np.abs(q - q[j]))
        order = np.argsort(-anchor, kind="stable")
        alive = np.arange(za.size)
        for p in order:
            projected = origin[alive] + za[alive] * (q[p] - q[i])
            distance = np.abs(projected[:, None] - centres[None, :])
            distance = np.where(allowed[p][None, :], distance, np.inf)
            gate = np.maximum(HYPOTHESIS_TOL_PX, gate_fraction * np.abs(za[alive]) * anchor[p])
            rows = np.arange(alive.size)
            # Prefer a circle of about the baseline holes' size (declared
            # heuristic, see SIMILAR_RADIUS): with a wide gate the counter of
            # a pin label next to a hole can be nearer than the hole itself.
            ratio = radii[None, :] / base_r[alive][:, None]
            similar = (ratio >= 1 / SIMILAR_RADIUS) & (ratio <= SIMILAR_RADIUS)
            preferred = np.where(similar, distance, np.inf).argmin(axis=1)
            nearest = distance.argmin(axis=1)
            use_preferred = distance[rows, preferred] <= gate
            nearest = np.where(use_preferred, preferred, nearest)
            good = distance[rows, nearest] <= gate
            assign[alive[good], p] = nearest[good]
            misses[alive[~good]] += 1
            alive = alive[misses[alive] <= max_misses]
            if alive.size == 0:
                break
        for row in assign[alive]:
            key = tuple(int(v) for v in row)
            matched = [v for v in key if v >= 0]
            if len(set(matched)) == len(matched) and key not in found:
                found[key] = row
    return sorted(found.values(), key=lambda row: int((row < 0).sum()))


def _is_collinear(points: np.ndarray, ratio: float = COLLINEAR_RATIO) -> bool:
    """True when the points lie on one straight line within ``ratio``.

    ``ratio`` bounds the second singular value of the centred points as a
    fraction of the first.
    """
    centred = points - points.mean(axis=0)
    singular = np.linalg.svd(centred, compute_uv=False)
    return bool(singular[1] <= ratio * singular[0])


def _min_spacing_mm(points: np.ndarray) -> float:
    """Smallest centre-to-centre distance of the reading, mm."""
    q = points[:, 0] + 1j * points[:, 1]
    pair_i, pair_j = np.triu_indices(len(points), 1)
    return float(np.abs(q[pair_j] - q[pair_i]).min())


@dataclass(frozen=True)
class _Similarity:
    """Uniform scale + rotation + translation from reading mm to image px (no mirror)."""

    gain: complex
    shift: complex

    @property
    def scale(self) -> float:
        """px per mm."""
        return max(abs(self.gain), 1e-9)

    @property
    def rotation_deg(self) -> float:
        """Rotation of the page relative to the reading, degrees."""
        return math.degrees(np.angle(self.gain))

    def project(self, points: np.ndarray) -> np.ndarray:
        """Image px of reading points (n, 2)."""
        z = self.gain * (points[:, 0] + 1j * points[:, 1]) + self.shift
        return np.column_stack([z.real, z.imag])


def _fit_similarity(points: np.ndarray, targets: np.ndarray) -> _Similarity:
    """Least-squares similarity (complex linear fit) of reading points onto image targets."""
    q = points[:, 0] + 1j * points[:, 1]
    c = targets[:, 0] + 1j * targets[:, 1]
    sol, *_ = np.linalg.lstsq(np.column_stack([q, np.ones_like(q)]), c, rcond=None)
    return _Similarity(complex(sol[0]), complex(sol[1]))


def _fit_affine(points: np.ndarray, targets: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares affine map; returns the projected points (px) and the 2x2 linear part."""
    design = np.column_stack([points, np.ones(len(points))])
    sol, *_ = np.linalg.lstsq(design, targets, rcond=None)  # 3x2: [A^T; t]
    return design @ sol, sol[:2].T


def _leave_one_out_px(points: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Displacement of each pad from where the similarity fitted on the OTHER pads puts it, px.

    A least-squares fit spreads one wrong pad's error over all pads (with
    three pads a 0.22 mm error shows as 0.07 mm); fitting on the others
    measures each pad against the rest of the reading, which is what "the
    reading's own quotes land on the drawn holes" means. Closed form of the
    complex linear fit with one point removed.
    """
    q = points[:, 0] + 1j * points[:, 1]
    c = targets[:, 0] + 1j * targets[:, 1]
    m = len(q) - 1
    q_mean = (q.sum() - q) / m
    c_mean = (c.sum() - c) / m
    qq = (np.abs(q) ** 2).sum() - np.abs(q) ** 2 - m * np.abs(q_mean) ** 2
    cq = (c * np.conj(q)).sum() - c * np.conj(q) - m * c_mean * np.conj(q_mean)
    gain = cq / np.maximum(qq, 1e-12)
    return np.abs(c - (c_mean + gain * (q - q_mean)))


def _leave_one_out_affine_px(points: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Displacement of each pad from where the AFFINE map fitted on the other pads puts it, px.

    The affine counterpart of :func:`_leave_one_out_px`, for a page that is
    stretched within the allowed ratio: a stretch moves every pad by one
    common linear map, a misread pad moves alone, and only fitting on the
    others shows the difference (a least-squares affine fit of four pads
    hides three quarters of one pad's error). Closed form: the ordinary
    residual divided by one minus the pad's leverage; a pad the others do
    not determine (leverage 1) gets an infinite displacement.
    """
    design = np.column_stack([points, np.ones(len(points))])
    pseudo = np.linalg.pinv(design)
    residual = np.linalg.norm(targets - design @ (pseudo @ targets), axis=1)
    leverage = np.einsum("ij,ji->i", design, pseudo)
    free = 1.0 - leverage
    return np.where(free > 1e-9, residual / np.maximum(free, 1e-9), np.inf)


def _stretch_ratio(linear: np.ndarray) -> float:
    """Stretch of an affine map beyond a similarity, as a ratio: anisotropy |sx / sy - 1| plus shear (radians).

    ``linear`` is the 2x2 linear part (reading mm -> px); sx and sy are the
    scales it gives the reading's X and Y axes. A whole X axis read 2.45
    instead of 2.54 on a 2 x 20 header shows as 3.5 % here, while the
    least-squares similarity would split it into +-0.14 mm of residual.
    """
    sx, sy = float(np.linalg.norm(linear[:, 0])), float(np.linalg.norm(linear[:, 1]))
    cos_angle = float(linear[:, 0] @ linear[:, 1] / max(sx * sy, 1e-12))
    shear = abs(math.pi / 2 - math.acos(max(-1.0, min(1.0, abs(cos_angle)))))
    return abs(sx / max(sy, 1e-12) - 1) + shear


def _allowed_stretch(points: np.ndarray, tolerance_mm: float) -> float:
    """Largest stretch ratio (:func:`_stretch_ratio`) a fit may show before it is "distorted".

    Two separate terms (audit A10):

    - PAGE_STRETCH_RATIO, what the page itself may be stretched by: a
      ratio, the same for a small and a large part;
    - the accuracy with which THESE pads can show a stretch at all: the
      per-hole tolerance over the spread of the reading across its main
      direction (second singular value of the centred pads, mm: the root
      of the summed squared offsets from the main axis). That is the
      standard error of a fitted scale, with the tolerance in the place of
      the noise; it shrinks as pads are added, so a 2 x 20 header shows a
      stretch far more finely than four pads of the same extent.

    The first term is a declared heuristic (see the constant), the second
    is derived from the fit.
    """
    spread = float(np.linalg.svd(points - points.mean(axis=0), compute_uv=False)[1])
    return PAGE_STRETCH_RATIO + tolerance_mm / max(spread, 1e-9)


def _anchored_on_holes(n_pads: int, holes: Sequence[_Circle], stretched: bool) -> bool:
    """True when the circles of a fit may confirm it.

    Any circle may confirm a reading (a hole drawn with a label close by is
    still a hole), except in the two fits a coincidence satisfies most
    easily, where every circle must be drawn like a hole:

    - fewer than MIN_WITNESS_PADS witness pads (two pads fix the
      similarity, every further pad is a witness): three pads;
    - ``stretched``: the pads fit only on a stretched page (step 3 of
      :func:`_fit_assignment`), the freest fit of this module, six
      parameters instead of four. A page of text is a lattice of glyph
      counters on which a reading on a regular grid fits with a little
      stretch; the stretch of a page is believed only where it is
      measured on drawn holes.

    Evidence of the circles is preferred to uniqueness among the
    alternative fits: a chance fit on glyph counters is usually the ONLY
    fit of a wrong reading, so uniqueness would let it through, and it
    would refuse a correct reading on a page that draws the pattern twice;
    what the pads sit on is the thing in doubt, and evidence is what this
    module already asks of a circle before it may contradict a reading.
    """
    if stretched or n_pads - 2 < MIN_WITNESS_PADS:
        return all(c.evidence for c in holes)
    return True


def _affine_observable(points: np.ndarray) -> bool:
    """True when an affine fit of the reading is over-determined in both directions.

    It needs at least AFFINE_MIN_PADS pads that are not nearly collinear
    (see AFFINE_MIN_SPREAD); otherwise its cross-row scale is set by a few
    sub-pixel offsets and it would "explain" anything.
    """
    return len(points) >= AFFINE_MIN_PADS and not _is_collinear(points, AFFINE_MIN_SPREAD)


def _tolerance_mm(scale: float) -> float:
    """Residual tolerance: max(MIN_TOLERANCE_MM, TOLERANCE_PX / scale)."""
    return max(MIN_TOLERANCE_MM, TOLERANCE_PX / max(scale, 1e-9))


def _fit_assignment(points: np.ndarray, circles: Sequence[_Circle], assign: np.ndarray,
                    mask: np.ndarray, body: tuple[float, float] | None, *,
                    anchoring: bool = True, outlines: dict | None = None) -> _Fit:
    """Fit one complete assignment and classify it.

    The error budget has two separate terms (audit A10, 2026-11-10): the
    PER-HOLE tolerance in pixels (:func:`_tolerance_mm`) and the
    PROPORTIONAL stretch of the page as a ratio (:func:`_allowed_stretch`).

    1. Displacement test. The reading, mapped onto the page by a similarity
       (uniform scale, rotation, translation, no mirror) fitted on the
       other pads, must put every pad within the tolerance of its circle
       (:func:`_leave_one_out_px`; max_error_mm reports it).
    2. Stretch. When the pads can show one (:func:`_affine_observable`),
       the affine map fitted on them must not mirror and its stretch
       (:func:`_stretch_ratio`) must stay within the allowed ratio; else
       the reading is "distorted" (a drawing not to scale, or a whole axis
       read wrong).
    3. A reading that fails 1 but whose pads show a stretch within the
       allowed ratio is judged on the stretched page: every pad within the
       tolerance of where the AFFINE map fitted on the other pads puts it
       (:func:`_leave_one_out_affine_px`; max_error_mm then reports that),
       and every circle must be drawn like a hole (step 5).
       This is what lets a large or finely scanned part pass under the scan
       stretch of a real page. A failed reading that an affine map explains
       otherwise (beyond the ratio, or only when fitted on all pads) is
       "distorted", any other "no_match". Fewer than AFFINE_MIN_PADS pads,
       or nearly collinear ones, get no proportional allowance: they cannot
       show a stretch, so nothing tells it from a misread pad.
    4. Rotation beyond MAX_ROTATION_DEG: "page_skewed" (the fit removed the
       rotation; the page must be deskewed before it can confirm).
    5. Resolution ("low_resolution"), then the circles of the fit
       ("unanchored", see :func:`_anchored_on_holes`): those of a fit with
       too few witness pads (only with ``anchoring``), and those of a fit
       on a stretched page.
    6. With a body, the rectangles drawn around the holes
       (:func:`_body_check`): "body_scale" when one has the body's
       proportions at another scale and none has its sizes at this one;
       the answer is carried in ``body_check`` either way.

    ``anchoring`` False skips the witness rule: for the anchored subset of
    a move, which must show the page's scale and therefore holds at least
    AFFINE_MIN_PADS pads. ``outlines`` is an optional memo of the body check
    per set of circles (the rectangles around a set of holes do not depend
    on which pad sits on which).
    """
    targets = np.array([(circles[k].x, circles[k].y) for k in assign], float)
    sim = _fit_similarity(points, targets)
    residual_px = _leave_one_out_px(points, targets)
    tolerance = _tolerance_mm(sim.scale)
    tolerance_px = tolerance * sim.scale
    max_error = float(residual_px.max() / sim.scale)
    # A body no outline was compared with: the fit stopped before step 6.
    unchecked = BODY_NONE if body is None else BODY_UNVERIFIABLE
    within_ratio = stretched = False
    if _affine_observable(points):
        affine_projected, linear = _fit_affine(points, targets)
        proper = bool(np.linalg.det(linear) > 0)
        within_ratio = proper and _stretch_ratio(linear) <= _allowed_stretch(points, tolerance)
        if residual_px.max() > tolerance_px:
            if not proper or np.linalg.norm(targets - affine_projected, axis=1).max() > tolerance_px:
                return _Fit("no_match", max_error, tolerance, unchecked)
            stretched_px = _leave_one_out_affine_px(points, targets)
            if not within_ratio or stretched_px.max() > tolerance_px:
                return _Fit("distorted", max_error, tolerance, unchecked)
            max_error = float(stretched_px.max() / sim.scale)
            stretched = True
        elif not within_ratio:
            return _Fit("distorted", max_error, tolerance, unchecked)
    elif residual_px.max() > tolerance_px:
        return _Fit("no_match", max_error, tolerance, unchecked)
    if abs(sim.rotation_deg) > MAX_ROTATION_DEG:
        return _Fit(PAGE_SKEWED, max_error, tolerance, unchecked)
    if tolerance > LOW_RESOLUTION_FRACTION * _min_spacing_mm(points):
        return _Fit("low_resolution", max_error, tolerance, unchecked)
    holes = [circles[k] for k in assign]
    # Without ``anchoring`` the witness rule is waived (pad count taken as
    # sufficient); a fit on a stretched page is held to its circles anyway.
    if not _anchored_on_holes(len(points) if anchoring else MIN_WITNESS_PADS + 2, holes, stretched):
        return _Fit(UNANCHORED, max_error, tolerance, unchecked)
    if body is None:
        return _Fit("confirmed", max_error, tolerance, BODY_NONE)
    key = (frozenset(int(k) for k in assign), round(sim.scale, 6), round(tolerance_px, 6))
    if outlines is not None and key in outlines:
        verdict = outlines[key]
    else:
        verdict, _rect = _body_check(mask, holes, body, sim.scale, tolerance_px)
        if outlines is not None:
            outlines[key] = verdict
    stage = "body_scale" if verdict == BODY_CONTRADICTED else "confirmed"
    return _Fit(stage, max_error, tolerance, verdict)


def body_allowance_mm(size_mm: float, tolerance_mm: float) -> float:
    """@brief How far a drawn body side may be from its quoted size and still agree with it, mm.

    @param size_mm The quoted size of that side, mm.
    @param tolerance_mm Per-hole tolerance of the fit, mm (``LayoutCheck.tolerance_mm``).
    @return Two tolerances (the two drawn edges of the side) plus
        BODY_SCALE_RATIO of the size (the drafting allowance, proportional).
    @note The one definition of the allowance of the body check
        (:func:`_within_allowance` applies it in pixels), for callers and
        tests that need to reason about it.
    """
    return 2 * float(tolerance_mm) + BODY_SCALE_RATIO * float(size_mm)


def body_axes_distinguishable(body_mm: Sequence[float], tolerance_mm: float | None) -> bool:
    """@brief Whether a drawn outline can tell which of two body sizes runs along which page axis.

    @param body_mm The two plan sizes of the body, mm, in any order.
    @param tolerance_mm Residual tolerance of the fit, mm (``LayoutCheck.tolerance_mm``);
        None when no fit was made.
    @return True when the two sizes differ by more than the sum of their
        two allowances (:func:`body_allowance_mm`).
    @note Derived from the check, not tuned: a drawn side agrees with a
        size within that size's allowance, so two sizes closer than the sum
        of their allowances can both agree with the same drawn side and
        the outline says nothing about their order. A caller that tests
        the body the other way round must not read an agreement as a
        measure below this separation. The allowance is necessary for an
        agreement, not sufficient (:func:`_body_check`), so above this
        separation two orders still cannot both agree.
    """
    if tolerance_mm is None:
        return False
    first, second = (float(v) for v in body_mm)
    return abs(first - second) > body_allowance_mm(first, tolerance_mm) + body_allowance_mm(second, tolerance_mm)


class _BudgetExceeded(Exception):
    """Raised when the bounded search for missed holes runs out of probes."""


class _Budget:
    """Remaining ring probes (one radius tested over one search window)."""

    def __init__(self, probes: int) -> None:
        """@brief Start with ``probes`` probes.

        @param probes Number of ring probes allowed.
        """
        self.left = probes

    def spend(self, count: int) -> None:
        """Consume ``count`` probes; raise _BudgetExceeded when none are left."""
        self.left -= count
        if self.left < 0:
            raise _BudgetExceeded()


def _ring_at(mask: np.ndarray, x: float, y: float, r: float, search: float, budget: _Budget) -> bool:
    """True when a closed ring of about radius ``r`` is inked within ``search`` px of (x, y).

    Same closed-ring test as the detector's verification (COVERAGE_MIN of
    96 ring samples at UPSCALE sub-pixel resolution, on the mask dilated by
    half a pixel), for radii 0.7-1.3 r in half-pixel steps and centres on a
    half-pixel grid over the search window, without the detector's later
    filters. All centres of one radius are tested at once; each radius
    costs one probe of ``budget``.
    """
    offsets = np.arange(-search, search + 1e-9, 0.5)
    gx, gy = np.meshgrid(x + offsets, y + offsets)
    cu = (gx.ravel() + 0.5) * UPSCALE - 0.5
    cv = (gy.ravel() + 0.5) * UPSCALE - 0.5
    angles = np.linspace(0, 2 * math.pi, 96, endpoint=False)
    cos_a, sin_a = np.cos(angles), np.sin(angles)
    radii = np.arange(0.7 * r, 1.3 * r + 1e-9, 0.5)
    budget.spend(len(radii))
    for rr in radii:
        ru = rr * UPSCALE
        xs = np.round(cu[:, None] + ru * cos_a[None, :])
        ys = np.round(cv[:, None] + ru * sin_a[None, :])
        if _fine_dilated(mask, xs, ys).mean(axis=1).max() >= COVERAGE_MIN:
            return True
    return False


def _missed_hole_explains(points: np.ndarray, circles: Sequence[_Circle], assign: np.ndarray,
                          mask: np.ndarray, budget: _Budget,
                          regions: Sequence[_Region] = ()) -> bool:
    """True when the pads that do not fit sit on rings the detector did not keep, or inside closed contours.

    Detection fails on small, low-resolution or crowded holes (a hole missed,
    or its centre pulled by a crossing leader line). Before a reading is
    reported as contradicted, a robust similarity is fitted: one from each of
    the longest assigned pad pairs, the one with the smallest median
    residual kept, its inliers refitted twice. When the inliers are a strict
    majority (and at least MIN_PADS), the rotation is within the limit and
    EVERY other pad lands on a closed ring of the matched holes' size, or
    inside a closed non-circular contour (a slot, an outlined land: see
    :func:`_find_regions`; review H4/M11, 2026-11-04), the image cannot
    contradict the reading. A wrong pad lands on paper or on a centre line,
    which is neither a closed ring nor inside a closed contour, or on a
    circle that WAS detected (another pad's hole), which is not a missed
    hole.
    """
    n = len(points)
    # Only circles drawn like holes may excuse a reading, as only they may
    # contradict one (_check_image): an explanation anchored on glyph
    # counters scattered over the page excused a reading wrong everywhere
    # and kept a confirmed one from being chosen (user run 2026-10-31,
    # T7N relay: three of the four anchors were letters far from the view).
    assigned = [p for p in range(n) if assign[p] >= 0 and circles[assign[p]].evidence]
    if len(assigned) < MIN_PADS:
        return False
    sub = points[assigned]
    targets = np.array([(circles[assign[p]].x, circles[assign[p]].y) for p in assigned], float)
    q = sub[:, 0] + 1j * sub[:, 1]
    c = targets[:, 0] + 1j * targets[:, 1]
    pair_i, pair_j = np.triu_indices(len(assigned), 1)
    longest = np.argsort(-np.abs(q[pair_j] - q[pair_i]), kind="stable")[:3 * BASELINES]
    best_median, model = math.inf, None
    for k in longest:
        i, j = pair_i[k], pair_j[k]
        gain = (c[j] - c[i]) / (q[j] - q[i])
        median = float(np.median(np.abs(c - (c[i] + gain * (q - q[i])))))
        if median < best_median:
            best_median, model = median, _Similarity(complex(gain), complex(c[i] - gain * q[i]))
    if model is None:
        return False
    keep = np.ones(len(assigned), bool)
    for _ in range(3):
        residual = np.linalg.norm(targets - model.project(sub), axis=1)
        keep = residual <= _tolerance_mm(model.scale) * model.scale
        if keep.sum() < MIN_PADS:
            return False
        model = _fit_similarity(sub[keep], targets[keep])
    residual = np.linalg.norm(targets - model.project(sub), axis=1)
    search = _tolerance_mm(model.scale) * model.scale
    keep = residual <= search
    inliers = [assigned[k] for k in range(len(assigned)) if keep[k]]
    if len(inliers) < MIN_PADS or 2 * len(inliers) <= n or abs(model.rotation_deg) > MAX_ROTATION_DEG:
        return False
    outliers = [p for p in range(n) if p not in inliers]
    if not outliers:
        return False
    projected = model.project(points[outliers])
    centres = np.array([(c.x, c.y) for c in circles], float)
    # A pad landing on a circle the detector DID keep (typically the hole of
    # another pad) is a wrong pad, not a missed hole.
    if np.any(np.linalg.norm(projected[:, None, :] - centres[None, :, :], axis=2) <= search):
        return False
    radius = float(np.median([circles[assign[p]].r for p in inliers]))
    return all(any(region.contains(x, y) for region in regions)
               or _ring_at(mask, x, y, radius, search, budget) for x, y in projected)


# ---------------------------------------------------------------------------
# Per-image analysis, cached
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Analysis:
    """What the check needs from one image: the photo gate, the ink mask, the circles and the closed contours.

    ``circles`` holds the rings AND the slots (kind "obround", see
    :class:`_Circle`); ``regions`` the closed non-circular contours (the
    same slots, and outlined rectangular lands), see :func:`_find_regions`.
    """

    drawing: bool
    mask: np.ndarray | None
    circles: tuple[_Circle, ...]
    regions: tuple[_Region, ...] = ()


_ANALYSES: OrderedDict[tuple[str, int, int], _Analysis | None] = OrderedDict()
_ANALYSES_LOCK = threading.Lock()


def _analyse(path: Path) -> _Analysis | None:
    """Gate and circles of one image file, cached by (resolved path, mtime, size).

    The extractor checks two to four competing readings against the same
    images; detection is the costly part and does not depend on the reading.
    None when the file cannot be read.
    """
    try:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    with _ANALYSES_LOCK:
        if key in _ANALYSES:
            _ANALYSES.move_to_end(key)
            return _ANALYSES[key]
    image = _read_image(path)
    if image is None:
        analysis = None
    else:
        grey = _working_grey(image)
        mask, paper = _ink_mask(grey)
        if _is_line_drawing(grey, mask, paper):
            circles = _find_circles(grey, mask)
            regions = _find_regions(mask, circles, _stroke_width(mask))
            circles = _without_composites(circles, regions)
            slots = [r.as_circle() for r in regions if r.kind == "obround"]
            analysis = _Analysis(True, mask, tuple(circles + slots), tuple(regions))
        else:
            analysis = _Analysis(False, None, ())
    with _ANALYSES_LOCK:
        _ANALYSES[key] = analysis
        _ANALYSES.move_to_end(key)
        while len(_ANALYSES) > ANALYSIS_CACHE_SIZE:
            _ANALYSES.popitem(last=False)
    return analysis


def clear_analysis_cache() -> None:
    """@brief Forget the cached per-image analyses (gate, ink mask, circles).

    @note The cache is keyed by resolved path, modification time and size,
        so a changed file is analysed again anyway; this is for tests and
        for releasing memory.
    """
    with _ANALYSES_LOCK:
        _ANALYSES.clear()


def _better(fit: _Fit, best: _Fit | None) -> bool:
    """True when ``fit`` got further through the checks than ``best``.

    At the same stage a fit whose body outline agrees comes first (a
    confirmation that also covers the scale), then the smaller error.
    """
    def rank(item: _Fit) -> tuple:
        """Stage, body agreement, then smaller error."""
        return _STAGES.index(item.stage), item.body_check == BODY_AGREES, -item.max_error_mm

    return best is None or rank(fit) > rank(best)


def _skewed_fit(points: np.ndarray, circles: Sequence[_Circle], mask: np.ndarray,
                kinds: Sequence[str | None] | None, references: np.ndarray) -> _Fit | None:
    """The fit of a reading on a page turned beyond MAX_ROTATION_DEG, or None.

    Run when nothing fits upright (audit A10): the hypotheses are searched
    again with the rotation free up to MAX_SKEW_DEG, and an assignment that
    passes every pad test there is a reading on a skewed page, reported
    "page_skewed" instead of contradicted. Only for a reading that can show
    the page's stretch (:func:`_affine_observable`): the failure of any
    other is "no_match_unobservable", which contradicts nothing, and three
    pads would find a rotated chance fit on most crowded pages.

    @note At most MAX_ASSIGNMENTS assignments are fitted, upright ones
        first, so on a page crowded with circles a skewed fit can be
        missed and the reading stay "no_match".
    """
    if not _affine_observable(points):
        return None
    for assign in _hypothesis_assignments(points, circles, kinds=kinds,
                                          max_rotation_deg=MAX_SKEW_DEG)[:MAX_ASSIGNMENTS]:
        if _on_reference(points, circles, assign, references):
            continue
        fit = _fit_assignment(points, circles, assign, mask, None)
        if fit.stage == PAGE_SKEWED:
            return fit
    return None


def _on_reference(points: np.ndarray, circles: Sequence[_Circle], assign: np.ndarray,
                  references: np.ndarray) -> bool:
    """True when a pad of the assignment sits on the drawn hole of a declared reference position.

    The references (mm, reading frame) are projected by the similarity
    fitted on the assigned pads; an assigned circle within the fit's
    tolerance of a projected reference is that reference's hole, not a
    pad's, so the assignment does not count.
    """
    if references.size == 0:
        return False
    assigned = np.nonzero(assign >= 0)[0]
    if len(assigned) < 2:
        return False
    targets = _centres(circles, assign[assigned])
    sim = _fit_similarity(points[assigned], targets)
    projected = sim.project(references)
    limit = _tolerance_mm(sim.scale) * sim.scale
    return bool((np.linalg.norm(targets[:, None, :] - projected[None, :, :], axis=2) <= limit).any())


def _check_image(name: str, analysis: _Analysis, points: np.ndarray,
                 body: tuple[float, float] | None, kinds: Sequence[str | None] | None = None,
                 references: np.ndarray | None = None) -> _ImageResult:
    """Run the matching of one reading on one analysed image."""
    if not analysis.drawing:
        return _ImageResult(name, False, 0, None)
    circles, mask = list(analysis.circles), analysis.mask
    hole_like = sum(c.evidence for c in circles)
    references = np.zeros((0, 2)) if references is None else references
    if len(circles) < len(points):
        return _ImageResult(name, True, len(circles), None, hole_like)
    best: _Fit | None = None
    outlines: dict = {}
    for assign in _hypothesis_assignments(points, circles, kinds=kinds)[:MAX_ASSIGNMENTS]:
        if _on_reference(points, circles, assign, references):
            continue
        fit = _fit_assignment(points, circles, assign, mask, body, outlines=outlines)
        if _better(fit, best):
            best = fit
        # A confirmation ends the search, unless a body was given and this
        # fit could not tie it to an outline: another fit (the same pattern
        # drawn in another view of the page) may still do so.
        if best.stage == "confirmed" and best.body_check in (BODY_NONE, BODY_AGREES):
            break
    if best is not None and best.stage == "confirmed":
        return _ImageResult(name, True, len(circles), best, hole_like)
    if best is None or best.stage == "no_match":
        # Second pass with a wide gate: a reading that fits only after a
        # stretch (drawing not to scale, a whole axis read wrong) is
        # reported "distorted", which is a stronger statement than
        # "no_match" and must not be lost to the narrow confirmation gate.
        for assign in _hypothesis_assignments(points, circles, gate_fraction=DISTORTION_GATE,
                                              kinds=kinds)[:MAX_ASSIGNMENTS]:
            if _on_reference(points, circles, assign, references):
                continue
            fit = _fit_assignment(points, circles, assign, mask, body, outlines=outlines)
            if fit.stage != "confirmed" and _better(fit, best):
                best = fit
    if best is None or best.stage == "no_match":
        # Still nothing: a page turned beyond the upright window is not a
        # wrong reading.
        skewed = _skewed_fit(points, circles, mask, kinds, references)
        if skewed is not None:
            best = replace(skewed, body_check=BODY_NONE if body is None else BODY_UNVERIFIABLE)
    if best is not None and best.stage in ("low_resolution", PAGE_SKEWED):
        return _ImageResult(name, True, len(circles), best, hole_like)
    # Every circle may confirm (a hole drawn with a label close by is still a
    # hole), but only circles drawn like holes may contradict: a page whose
    # circles are glyph counters says nothing against the reading.
    if hole_like < len(points):
        return _ImageResult(name, True, len(circles), None, hole_like)
    if best is not None and best.stage == UNANCHORED:
        # The pads do fit, on circles that cannot vouch for them: said as
        # such, after the page-level reasons above (a page with too few
        # circles drawn like holes is the more telling answer).
        return _ImageResult(name, True, len(circles), best, hole_like)
    if best is None or best.stage == "no_match":
        # Nothing fits: before calling the reading wrong, check whether a hole
        # the detector missed, or a closed contour that is not a circle (a
        # slot, a land), explains it. When every pad did meet a circle
        # (distorted, body_scale) a missed hole is not the explanation.
        budget = _Budget(MAX_RING_PROBES)
        try:
            for assign in _hypothesis_assignments(points, circles, max_misses=1,
                                                  kinds=kinds)[:MAX_ASSIGNMENTS]:
                if _missed_hole_explains(points, circles, assign, mask, budget, analysis.regions):
                    return _ImageResult(name, True, len(circles), None, hole_like)
        except _BudgetExceeded:
            return _ImageResult(name, True, len(circles), _Fit("search_limit", math.nan, math.nan),
                                hole_like)
    if best is None:
        best = _Fit("no_match", math.inf, math.nan)
    return _ImageResult(name, True, len(circles), best, hole_like)


def _summarise(results: Sequence[_ImageResult], observable: bool = False,
               unchecked: str = BODY_NONE) -> LayoutCheck:
    """Choose the overall non-confirmed answer from the per-image results.

    "not_confirmed" needs an image whose reading reached a contradicting
    stage (no_match, distorted, body_scale); a "no_match" of a reading that
    cannot show the page's scale (``observable`` False) is reported as
    "no_match_unobservable". Otherwise the most informative unverifiable
    reason wins: search_limit > low_resolution > page_skewed > unanchored
    > too_few_circles > no_round_holes > not_a_drawing > no_image (within
    one image "unanchored" comes after the last two, see
    :func:`_check_image`). ``unchecked`` is the ``body_check`` of an
    answer no fit carries one for (BODY_UNVERIFIABLE when a body was given,
    else BODY_NONE); ``scale_verified`` is never True here.
    """
    def rounded(value: float) -> float | None:
        """Finite values rounded to 0.1 um, others None."""
        return round(value, 4) if math.isfinite(value) else None

    contradicting = [r for r in results if r.fit is not None and r.fit.stage in _CONTRADICTING]
    if contradicting:
        decisive = max(contradicting, key=lambda r: (_STAGES.index(r.fit.stage), -r.fit.max_error_mm))
        fit = decisive.fit
        reason = NO_MATCH_UNOBSERVABLE if fit.stage == "no_match" and not observable else fit.stage
        body_check = BODY_CONTRADICTED if fit.stage == "body_scale" else unchecked
        return LayoutCheck(NOT_CONFIRMED, reason, decisive.name, rounded(fit.max_error_mm),
                           rounded(fit.tolerance_mm), decisive.circles, observable, body_check)
    for reason in _UNJUDGED:
        flagged = [r for r in results if r.fit is not None and r.fit.stage == reason]
        if flagged:
            decisive = flagged[0]
            return LayoutCheck(UNVERIFIABLE, reason, decisive.name, rounded(decisive.fit.max_error_mm),
                               rounded(decisive.fit.tolerance_mm), decisive.circles, observable, unchecked)
    drawings = [r for r in results if r.drawing]
    if drawings:
        decisive = max(drawings, key=lambda r: (r.hole_like, r.circles))
        reason = "too_few_circles" if decisive.hole_like else NO_ROUND_HOLES
        return LayoutCheck(UNVERIFIABLE, reason, decisive.name, None, None, decisive.circles, observable,
                           unchecked)
    if results:
        return LayoutCheck(UNVERIFIABLE, "not_a_drawing", None, None, None, 0, observable, unchecked)
    return LayoutCheck(UNVERIFIABLE, "no_image", None, None, None, 0, observable, unchecked)


# ---------------------------------------------------------------------------
# Measured layout: one pad moved onto the only free drawn hole
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MeasuredLayout:
    """@brief Outcome of :func:`measured_layout`: pad positions deduced from the drawn holes.

    @note ``positions_mm`` maps every pad of the reading to (x, y) mm in the
        reading's own frame (page orientation): the reading's values for the
        pads that sit on their holes, the measured value for ``moved``.
        ``measured_mm`` gives every pad as measured from its hole through the
        inverse of the fit, for comparison. ``moved`` is the key of the pad
        put onto a hole, None when every pad already sat on one;
        ``moved_from_mm`` is its value in the reading. ``max_error_mm`` is
        the largest leave-one-out displacement of the anchored pads and
        ``tolerance_mm`` the tolerance it was held to; ``image`` is the file
        name of the drawing that decided. Measured positions are rounded to
        0.01 mm; their real uncertainty is about one pixel of the drawing
        (``tolerance_mm`` is three pixels or 0.15 mm, whichever is larger).
    @note ``scale_observable`` (2026-11-04) says whether the anchored pads
        could show the page's stretch and shear (see
        :func:`_affine_observable`); a pad is moved only when they could
        and showed none beyond the allowed ratio (PAGE_STRETCH_RATIO).
    @note ``body_check`` and ``scale_verified`` (2026-11-10) are those of
        :class:`LayoutCheck`, for the completed layout: the positions are
        tied to an absolute size only when ``scale_verified`` is True (a
        drawn outline agrees with the body given); a contradicting outline
        refuses the layout ("body_scale"), so ``body_check`` is never
        "contradicted" here.
    """

    positions_mm: dict
    measured_mm: dict
    moved: object | None
    moved_from_mm: tuple[float, float] | None
    max_error_mm: float
    tolerance_mm: float
    image: str
    scale_observable: bool = False
    body_check: str = BODY_NONE
    scale_verified: bool = False


@dataclass(frozen=True)
class MeasureOutcome:
    """@brief Outcome of :func:`measured_layout_outcome`: the measured layout, or why none was given.

    @note ``layout`` is the :class:`MeasuredLayout` (then ``refused`` is
        None), or None with ``refused`` a machine code: "too_few_pads",
        "too_many_pads", "no_drawing" (no readable line drawing),
        "no_fit" (no anchoring of at least MIN_PADS pads fits), "ambiguous"
        (several anchorings, or several free holes for the missing pad),
        "scale_unobservable" (the anchored pads cannot show the page's
        stretch: fewer than AFFINE_MIN_PADS or nearly collinear),
        "distorted" (the anchored pads fit only with a stretch or shear
        beyond the allowed ratio: the page is not to scale), "page_skewed"
        (they fit only on a page turned by more than MAX_ROTATION_DEG),
        "unanchored" (the pads fit circles that are not all drawn like
        holes, with three pads only or on a stretched page, see
        :func:`_anchored_on_holes`), "hole_kind"
        (the only free hole is of another kind than the pad's declared kind
        or than the anchored pads' holes), "not_a_hole" (the only free
        circle is not drawn like a hole), "no_free_hole" (no free circle of
        the anchored holes' size within reach), "mirror" (a mirror of the
        reading also fits, see :func:`mirror_variants`), "body_scale" /
        "low_resolution" / "no_match" (the completed layout fails the whole
        check with that stage), "images_disagree" (two drawings give
        different answers). ``image`` names the drawing the code refers to,
        None when none decided.
    """

    layout: MeasuredLayout | None
    refused: str | None
    image: str | None = None


# Marker of an anchoring whose missing pad could go onto several free holes.
_AMBIGUOUS = "ambiguous"

# Refusal codes of one image, most informative first: the code of the
# anchoring that got furthest is reported when nothing is measured.
_REFUSALS = ("mirror", "body_scale", "low_resolution", "no_match", "ambiguous", "hole_kind",
             "not_a_hole", "no_free_hole", "distorted", PAGE_SKEWED, "scale_unobservable",
             UNANCHORED, "no_fit")


def measured_layout(images: Sequence[Path], pads_mm: Mapping[object, Sequence[float]],
                    body_mm: Sequence[float] | None = None, *,
                    hole_kinds: Mapping[object, str] | None = None,
                    reference_holes_mm: Sequence[Sequence[float]] | None = None) -> MeasuredLayout | None:
    """@brief Deduce pad positions from the drawn holes when at most one pad of a reading is off.

    A similarity fit (uniform scale, rotation within MAX_ROTATION_DEG,
    translation, never a mirror) is searched, with the machinery of
    :func:`check_pad_layout`, in which every pad but at most ONE lands on a
    detected hole within the tolerance, the anchored pads alone passing the
    same confirmation test (leave-one-out displacement, stretch and shear,
    resolution). When one pad misses, the free hole it should take is looked
    for around the place where the fit puts its reading: a detected circle
    of about the anchored holes' size (SIMILAR_RADIUS), assigned to no other
    pad and to no declared reference hole, of the pad's kind, no farther
    than the distance from that pad's reading to its nearest anchored pad.
    Exactly one such circle must exist and it must be drawn like a hole
    (centre line or isolation, see :func:`_hole_evidence`); the pad is
    moved onto it.

    Why the deduction is safe: n - 1 anchored pads (at least MIN_PADS) fix
    scale, rotation and translation without the missing pad, so its
    position is the only unknown, and a single free hole leaves a single
    answer. Every way the answer could be one of several is refused. The
    anchored pads must also be able to SHOW that the page is to scale
    (review H5, 2026-11-04): at least AFFINE_MIN_PADS of them, not nearly
    collinear (:func:`_affine_observable`), fitting without the stretch an
    affine map would add; otherwise a page drawn not to scale on the axis
    of the move would put a printed quote wrong, and the move is refused.

    @param images Candidate drawing images, as for :func:`check_pad_layout`.
    @param pads_mm Pad key -> (x, y) mm AS SEEN ON THE PAGE (X right, Y
        down, arbitrary origin), e.g. a dedicated pad reading.
    @param body_mm Optional (length along page X, width along page Y) mm;
        when given and an outline is found around the holes, the final
        layout must agree with it as in :func:`check_pad_layout`.
    @param hole_kinds Optional declared hole kind per pad, as for
        :func:`check_pad_layout`: the free hole taken must be of the moved
        pad's declared kind; without a declaration, of the one kind of the
        anchored pads' holes when they share one (a terminal row never
        takes a slot, a slot never takes a ring).
    @param reference_holes_mm Optional declared non-pad holes (mm, reading
        frame), as for :func:`check_pad_layout`: their drawn holes are never
        free candidates for the moved pad.
    @return A :class:`MeasuredLayout`, or None when the answer is not unique
        or not deducible: fewer than MIN_PADS anchored pads (a move needs at
        least MIN_PADS + 1 pads), an anchoring that cannot show the page's
        scale or shows a stretch, more than one pad off its hole, no free
        hole or several candidate holes for the missing pad, the only
        candidate not drawn like a hole or of another kind, several
        different anchorings fitting, the reading mirrored about either
        page axis (:func:`mirror_variants`) also fitting (checked when a
        pad is moved), the body outline disagreeing, or two images giving
        different answers. With every pad on its hole the reading is
        returned unchanged (moved None). :func:`measured_layout_outcome`
        tells which.
    @throws ValueError When a pad coordinate, a body size or a reference
        hole is not a finite number (a positive one for the body).
    @note POSITIONS ONLY: pad numbers always come from the reading, never
        from the holes, and only the moved pad gets a position measured in
        pixels; the others keep their quoted values.
    @note Declared heuristic: the search radius (distance from the pad's
        reading to its nearest anchored pad) bounds how far a pad can be
        misread before the correction would carry it past another pad,
        which is no longer a local misreading. Chosen on the T7NS relay
        reading (pad 11 read 2.0 mm right of its hole, nearest pad 6.3 mm
        away, the hole the mirrored reading would take 14.4 mm away). A
        second independent reading of the misplaced pad, or a vector drawing
        with explicit hole numbers, would make it unnecessary.
    """
    return measured_layout_outcome(images, pads_mm, body_mm, hole_kinds=hole_kinds,
                                   reference_holes_mm=reference_holes_mm).layout


def measured_layout_outcome(images: Sequence[Path], pads_mm: Mapping[object, Sequence[float]],
                            body_mm: Sequence[float] | None = None, *,
                            hole_kinds: Mapping[object, str] | None = None,
                            reference_holes_mm: Sequence[Sequence[float]] | None = None
                            ) -> MeasureOutcome:
    """@brief :func:`measured_layout` with the reason of a refusal.

    @param images, pads_mm, body_mm, hole_kinds, reference_holes_mm As for
        :func:`measured_layout`.
    @return A :class:`MeasureOutcome`: the layout, or None and the code of
        the refusal (see the class). When several drawings refuse, the code
        of the one whose anchoring got furthest is given (``_REFUSALS``
        order).
    @throws ValueError As :func:`measured_layout`.
    """
    points = _validated_points(pads_mm)
    body = _validated_body(body_mm)
    keys = list(pads_mm.keys())
    kinds = _required_kinds(keys, hole_kinds)
    references = _validated_references(reference_holes_mm)
    if len(points) < MIN_PADS:
        return MeasureOutcome(None, "too_few_pads")
    if len(points) > MAX_PADS:
        return MeasureOutcome(None, "too_many_pads")
    answer: MeasuredLayout | None = None
    refusals: list[tuple[str, str]] = []
    for path in images:
        path = Path(path)
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        analysis = _analyse(path)
        if analysis is None or not analysis.drawing:
            continue
        result, refused = _measure_image(path.name, analysis, points, keys, body, kinds, references)
        if result is None:
            refusals.append((refused or "no_fit", path.name))
            continue
        if answer is not None and (result.positions_mm != answer.positions_mm
                                   or result.moved != answer.moved):
            return MeasureOutcome(None, "images_disagree", path.name)
        answer = answer or result
    if answer is not None:
        return MeasureOutcome(answer, None, answer.image)
    if refusals:
        code, name = min(refusals, key=lambda item: _REFUSALS.index(item[0])
                         if item[0] in _REFUSALS else len(_REFUSALS))
        return MeasureOutcome(None, code, name)
    return MeasureOutcome(None, "no_drawing")


def _layout_candidates(points: np.ndarray, circles: Sequence[_Circle], mask: np.ndarray,
                       kinds: Sequence[str | None] | None = None,
                       references: np.ndarray | None = None) -> tuple[set, set, set]:
    """Distinct complete assignments (all pads on holes) and one-pad-moved ones of a reading.

    Returns (full, moved, refusals): ``full`` holds assignment tuples whose
    fit is confirmed as drawn; ``moved`` holds (assignment, moved pad)
    pairs, or an (_AMBIGUOUS, anchoring) entry when an anchoring leaves
    several free holes for its missing pad, so that the ambiguity counts as
    a second answer; ``refusals`` the codes of the anchorings that gave no
    move (see :class:`MeasureOutcome`). A move needs an anchoring that can
    show the page's scale (:func:`_affine_observable`) and fits without
    stretch; its free hole must be of the moved pad's declared kind, or of
    the anchored holes' one kind, and not the hole of a declared reference.
    """
    full: set = set()
    moved: set = set()
    refusals: set = set()
    if len(circles) < len(points) - 1:
        return full, moved, refusals
    radii = np.array([c.r for c in circles])
    centres = np.array([(c.x, c.y) for c in circles], float)
    references = np.zeros((0, 2)) if references is None else references
    for assign in _hypothesis_assignments(points, circles, max_misses=1, kinds=kinds)[:MAX_ASSIGNMENTS]:
        if _on_reference(points, circles, assign, references):
            continue
        missing = np.nonzero(assign < 0)[0]
        if missing.size == 0:
            stage = _fit_assignment(points, circles, assign, mask, None).stage
            if stage == "confirmed":
                full.add(tuple(int(v) for v in assign))
            elif stage in (PAGE_SKEWED, UNANCHORED):
                # The reading fits as it is, but the image cannot vouch for
                # it: named, so the caller does not read "nothing fits".
                refusals.add(stage)
            continue
        pad = int(missing[0])
        anchored = np.nonzero(assign >= 0)[0]
        if len(anchored) < MIN_PADS:
            continue
        # The witness rule is for whole readings: an anchoring that may
        # move a pad must show the page's scale, which takes more pads.
        stage = _fit_assignment(points[anchored], circles, assign[anchored], mask, None,
                                anchoring=False).stage
        if stage != "confirmed":
            refusals.add(stage if stage in _REFUSALS else "no_fit")
            continue
        if not _affine_observable(points[anchored]):
            refusals.add("scale_unobservable")
            continue
        sim = _fit_similarity(points[anchored], centres[assign[anchored]])
        read_px = sim.project(points[pad:pad + 1])[0]
        reach_px = float(np.linalg.norm(points[anchored] - points[pad], axis=1).min()) * sim.scale
        used = {int(v) for v in assign[anchored]}
        if references.size:
            limit = _tolerance_mm(sim.scale) * sim.scale
            projected = sim.project(references)
            used |= {int(k) for k in np.nonzero(
                (np.linalg.norm(centres[:, None, :] - projected[None, :, :], axis=2) <= limit).any(axis=1))[0]}
        ratio = radii / float(np.median(radii[assign[anchored]]))
        near = np.linalg.norm(centres - read_px, axis=1) <= reach_px
        similar = (ratio >= 1 / SIMILAR_RADIUS) & (ratio <= SIMILAR_RADIUS)
        free = [int(k) for k in np.nonzero(near & similar)[0] if int(k) not in used]
        # A declared kind chooses among the free holes (a declared slot pad
        # takes the slot beside a ring). Undeclared, every free hole counts
        # for the ambiguity -- the drawing offers each as an explanation and
        # no declaration picks one -- and a lone free hole of another kind
        # than the one kind the anchored pads' holes share is refused: a
        # row of terminals never takes a slot.
        declared = kinds[pad] if kinds and kinds[pad] else None
        of_kind = [k for k in free if declared is None or circles[k].kind == declared]
        anchoring = tuple(int(v) for v in assign)
        if len(of_kind) > 1:
            moved.add((_AMBIGUOUS, anchoring))
            continue
        if not of_kind:
            refusals.add("hole_kind" if free else "no_free_hole")
            continue
        anchored_kinds = {circles[int(k)].kind for k in assign[anchored]}
        if declared is None and len(anchored_kinds) == 1 and circles[of_kind[0]].kind not in anchored_kinds:
            refusals.add("hole_kind")
            continue
        if not circles[of_kind[0]].evidence:
            refusals.add("not_a_hole")
            continue
        complete = list(anchoring)
        complete[pad] = of_kind[0]
        moved.add((tuple(complete), pad))
    if not full and not moved and not refusals:
        refusals |= _stretched_anchorings(points, circles, mask, kinds)
    return full, moved, refusals


def _stretched_anchorings(points: np.ndarray, circles: Sequence[_Circle], mask: np.ndarray,
                          kinds: Sequence[str | None] | None) -> set:
    """{"distorted"} when some anchoring of all pads but at most one fits only after a stretch, else empty.

    Run when nothing anchored within the confirmation gate: with the wide
    gate of the "distorted" pass (DISTORTION_GATE) the pads reach their
    holes on a page not drawn to scale, and the anchorings leaving each pad
    out (the misread pad is assigned too under a wide gate) are fitted.
    Never a move: the wide gate only names the refusal, so that the caller
    can tell "the page is not to scale" from "nothing fits".
    """
    for assign in _hypothesis_assignments(points, circles, max_misses=1,
                                          gate_fraction=DISTORTION_GATE, kinds=kinds)[:MAX_ASSIGNMENTS]:
        assigned = np.nonzero(assign >= 0)[0]
        subsets = [assigned] + [assigned[assigned != p] for p in assigned]
        for anchored in subsets:
            if len(anchored) < MIN_PADS or not _affine_observable(points[anchored]):
                continue
            if _fit_assignment(points[anchored], circles, assign[anchored], mask, None,
                               anchoring=False).stage == "distorted":
                return {"distorted"}
    return set()


def _centres(circles: Sequence[_Circle], assign: np.ndarray) -> np.ndarray:
    """Image px of the circles of a complete assignment, (n, 2)."""
    return np.array([(circles[k].x, circles[k].y) for k in assign], float)


def _measured_points(points: np.ndarray, circles: Sequence[_Circle], assign: np.ndarray,
                     pad: int | None) -> np.ndarray:
    """Every pad's hole in the reading's mm frame, by the inverse of the fit on the pads other than ``pad``."""
    centres = _centres(circles, assign)
    anchored = [p for p in range(len(points)) if p != pad]
    sim = _fit_similarity(points[anchored], centres[anchored])
    z = (centres[:, 0] + 1j * centres[:, 1] - sim.shift) / sim.gain
    return np.column_stack([z.real, z.imag])


def _measure_image(name: str, analysis: _Analysis, points: np.ndarray, keys: list,
                   body: tuple[float, float] | None, kinds: Sequence[str | None] | None = None,
                   references: np.ndarray | None = None) -> tuple[MeasuredLayout | None, str | None]:
    """Unique measured layout of one reading on one analysed drawing, or (None, refusal code)."""
    circles, mask = list(analysis.circles), analysis.mask
    full, moved, refusals = _layout_candidates(points, circles, mask, kinds, references)
    if full:
        # The reading fits as it is: nothing to deduce, unless it fits in
        # two different ways.
        if len(full) != 1:
            return None, "ambiguous"
        assign, pad = np.array(next(iter(full))), None
    else:
        if len(moved) > 1:
            return None, "ambiguous"
        if not moved:
            best = min(refusals, key=_REFUSALS.index) if refusals else "no_fit"
            return None, best
        complete, pad = next(iter(moved))
        if complete == _AMBIGUOUS:
            return None, "ambiguous"
        assign = np.array(complete)
    measured = _measured_points(points, circles, assign, pad)
    anchored = [p for p in range(len(points)) if p != pad]
    anchored_fit = _fit_assignment(points[anchored], circles, assign[anchored], mask, None, anchoring=False)
    if pad is not None:
        # A reading of the other face (mirrored about a page axis, the same
        # flips as mirror_variants) that also fits is a second explanation.
        # It is harmless only when it moves the same pad to the same place
        # (a layout symmetric about that axis with the pad on the axis); a
        # full fit of the mirror, or a move elsewhere, leaves two answers:
        # refuse.
        for flip in (np.array(f, float) for f in MIRROR_FLIPS):
            other_full, other_moved, _ = _layout_candidates(points * flip, circles, mask, kinds,
                                                            None if references is None
                                                            else references * flip)
            if other_full:
                return None, "mirror"
            for other_assign, other_pad in other_moved:
                if other_assign == _AMBIGUOUS or other_pad != pad:
                    return None, "mirror"
                other = _measured_points(points * flip, circles, np.array(other_assign), pad)[pad] * flip
                if np.linalg.norm(other - measured[pad]) > anchored_fit.tolerance_mm:
                    return None, "mirror"
    final = points.copy()
    if pad is not None:
        final[pad] = measured[pad]
    # The completed layout must pass the whole check, body outline included.
    final_fit = _fit_assignment(final, circles, assign, mask, body)
    if final_fit.stage != "confirmed":
        return None, final_fit.stage if final_fit.stage in _REFUSALS else "no_fit"

    def pair(row: np.ndarray) -> tuple[float, float]:
        """Point rounded to 0.01 mm, without negative zero."""
        return (round(float(row[0]), 2) + 0.0, round(float(row[1]), 2) + 0.0)

    return MeasuredLayout(
        positions_mm={key: (pair(measured[i]) if i == pad else (float(points[i][0]), float(points[i][1])))
                      for i, key in enumerate(keys)},
        measured_mm={key: pair(measured[i]) for i, key in enumerate(keys)},
        moved=None if pad is None else keys[pad],
        moved_from_mm=None if pad is None else (float(points[pad][0]), float(points[pad][1])),
        max_error_mm=round(anchored_fit.max_error_mm, 4),
        tolerance_mm=round(anchored_fit.tolerance_mm, 4),
        image=name,
        scale_observable=_affine_observable(points[anchored]),
        body_check=final_fit.body_check,
        scale_verified=final_fit.body_check == BODY_AGREES,
    ), None


# ---------------------------------------------------------------------------
# Measured outline: the drawn body rectangle in the reading's frame
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MeasuredOutline:
    """@brief Outcome of :func:`measured_outline`: the body outline drawn around the holes.

    @note ``left_mm``, ``top_mm``, ``right_mm`` and ``bottom_mm`` are the
        edges of the drawn outline in the reading's own mm frame (the frame
        of ``pads_mm``: page orientation, X right, Y down, the reading's
        origin), rounded to 0.01 mm; left < right and top < bottom.
        ``tolerance_mm`` is the tolerance of the confirmed fit (three pixels
        or 0.15 mm, whichever is larger): each edge is placed to about that
        accuracy. ``image`` is the file name of the drawing that decided.
    """

    left_mm: float
    top_mm: float
    right_mm: float
    bottom_mm: float
    tolerance_mm: float
    image: str

    @property
    def width_mm(self) -> float:
        """@brief Outline extent along page X, mm.

        @return right_mm - left_mm.
        """
        return self.right_mm - self.left_mm

    @property
    def height_mm(self) -> float:
        """@brief Outline extent along page Y, mm.

        @return bottom_mm - top_mm.
        """
        return self.bottom_mm - self.top_mm


def measured_outline(images: Sequence[Path], pads_mm: Mapping[object, Sequence[float]],
                     body_mm: Sequence[float] | None = None) -> MeasuredOutline | None:
    """@brief Measure the body outline drawn around the holes, in the frame of a confirmed reading.

    The reading is matched to the drawn holes exactly as by
    :func:`check_pad_layout` without a body: the first assignment whose fit
    is confirmed (displacement test, stretch and shear, rotation,
    resolution) decides. A rectangle drawn around ITS holes, solid or
    dashed/dotted (see :func:`_outline_rects`) -- the smallest one, or with
    ``body_mm`` the one identified as that body -- is then mapped back
    into the reading's mm frame through the inverse of the similarity fitted
    on all the pads. Each edge is the mean of its two mapped corners (the
    fit may rotate the page by up to MAX_ROTATION_DEG).

    @param images Candidate drawing images, as for :func:`check_pad_layout`.
    @param pads_mm Pad key -> (x, y) mm AS SEEN ON THE PAGE (X right, Y
        down, arbitrary origin); the outline comes back in this frame.
    @param body_mm Optional (length along page X, width along page Y) mm;
        when given, the outline returned is the rectangle the body check
        of :func:`check_pad_layout` identifies as that body and finds
        agreeing with it (:func:`_body_check`: within two fit tolerances
        plus BODY_SCALE_RATIO of each size; of several agreeing ones, the
        nearest to the declared sizes); rectangles of other
        proportions around the holes are passed over.
    @return A :class:`MeasuredOutline`, or None when fewer than MIN_PADS or
        more than MAX_PADS pads are given, no image confirms the reading,
        the confirmed image has no closed rectangle around the matched
        holes (with ``body_mm``: the body check is "unverifiable" -- none
        taken for the body, or two candidates at two scales, e.g. a nearly
        square body with a step inside), the rectangles
        of any image contradict ``body_mm`` (the body's proportions at
        another scale and none with its sizes at this one), or two images
        give outlines whose edges differ by
        more than their larger tolerance.
    @throws ValueError When a pad coordinate or a body size is not a finite
        number (a positive one for the body).
    @note A MEASUREMENT in pixels, scaled by the reading's own quotes: it is
        as good as the drawing's proportions (the T7NS relay page draws its
        22.5 x 16.5 mm body about 21.9 x 16.1 mm). The rectangle is a DRAWN
        outline (often the body seen from the solder side). WITHOUT
        ``body_mm`` it is the smallest rectangle around the holes, whatever
        it is: a keep-out frame, a table cell or the page border when the
        body is not drawn as a rectangle (audit A9). Only a body size
        identifies the body, see :func:`_judge_outline` for the limits.
    """
    points = _validated_points(pads_mm)
    body = _validated_body(body_mm)
    if len(points) < MIN_PADS or len(points) > MAX_PADS:
        return None
    answer: MeasuredOutline | None = None
    for path in images:
        path = Path(path)
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        analysis = _analyse(path)
        if analysis is None or not analysis.drawing:
            continue
        result = _outline_image(path.name, analysis, points, body)
        if result is _REFUSED:
            return None
        if result is None:
            continue
        if answer is not None:
            limit = max(answer.tolerance_mm, result.tolerance_mm)
            edges = ((answer.left_mm, result.left_mm), (answer.top_mm, result.top_mm),
                     (answer.right_mm, result.right_mm), (answer.bottom_mm, result.bottom_mm))
            if any(abs(a - b) > limit for a, b in edges):
                return None
        answer = answer or result
    return answer


# Marker of an image whose outline contradicts the body size given.
_REFUSED = "refused"


def _outline_image(name: str, analysis: _Analysis, points: np.ndarray,
                   body: tuple[float, float] | None) -> MeasuredOutline | str | None:
    """Outline of one analysed drawing in the reading's frame, None, or _REFUSED on a body contradiction.

    Every confirmed assignment of the reading is looked at, not the first
    (generality audit M3, 2026-10-31): a page drawing the same hole pattern
    twice (a PCB layout and a bottom view, or a schematic box whose
    terminals copy the holes) confirms the reading on each, and the outline
    of whichever came first would place the body. The outline counts only
    when every confirmed assignment anchored on circles drawn like holes
    gives the same rectangle (within the fit tolerance), and at least one
    does.
    """
    circles, mask = list(analysis.circles), analysis.mask
    if len(circles) < len(points):
        return None
    found: list[MeasuredOutline] = []
    for assign in _hypothesis_assignments(points, circles)[:MAX_ASSIGNMENTS]:
        fit = _fit_assignment(points, circles, assign, mask, None)
        if fit.stage != "confirmed":
            continue
        if not all(circles[k].evidence for k in assign):
            continue
        holes = [circles[k] for k in assign]
        sim = _fit_similarity(points, _centres(circles, assign))
        if body is None:
            rect = _outline_rect(mask, holes)
        else:
            verdict, rect = _body_check(mask, holes, body, sim.scale, fit.tolerance_mm * sim.scale)
            if verdict == BODY_CONTRADICTED:
                return _REFUSED
        if rect is None:
            return None
        left, top, right, bottom = rect
        corners = np.array([left + 1j * top, right + 1j * top, right + 1j * bottom, left + 1j * bottom])
        mm = (corners - sim.shift) / sim.gain

        def edge(a: complex, b: complex, imaginary: bool) -> float:
            """Mean of one coordinate of two mapped corners, rounded to 0.01 mm."""
            value = (a.imag + b.imag) / 2 if imaginary else (a.real + b.real) / 2
            return round(float(value), 2) + 0.0

        found.append(MeasuredOutline(
            left_mm=edge(mm[0], mm[3], False), top_mm=edge(mm[0], mm[1], True),
            right_mm=edge(mm[1], mm[2], False), bottom_mm=edge(mm[2], mm[3], True),
            tolerance_mm=round(fit.tolerance_mm, 4), image=name))
    if not found:
        return None
    first = found[0]
    for other in found[1:]:
        limit = max(first.tolerance_mm, other.tolerance_mm)
        if any(abs(a - b) > limit for a, b in (
                (first.left_mm, other.left_mm), (first.top_mm, other.top_mm),
                (first.right_mm, other.right_mm), (first.bottom_mm, other.bottom_mm))):
            return None
    return first
