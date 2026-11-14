"""@brief Hole style of a SHORT castellated row the outline located (night1 RS485 run, 2026-11-14).

A module with 3 and 4 castellations per side: the outline counted one notch per
declared contact, but the land reader asked for MIN_ROW_LANDS (5) lands of a
row found by its own blind chain search, so it never read those rows. The
declared «half_and_through» (half-hole plus inner through-hole) then reached
the footprint with no photo evidence for the inner hole, every size estimated.

With the notch centres the outline located, the reader reads the short row,
every land agreeing: it confirms a closed hole behind the opening, or it shows
there is none (evidence of absence over the whole measured land), and the
extraction adopts that. Positive and negative cases differ in structure (a
closed hole is or is not drawn; centres located or not; the land read whole or
not), never in a number tuned to a board.
"""
import cv2
import numpy as np
import pytest

from kicadgen.services import datasheet_extractor as extractor
from kicadgen.services.edge_lands import (MIN_ROW_LANDS, choose_land_class, hole_agreement_floor,
                                          measure_edge_holes)
from kicadgen.services.photo_inventory import (OUTLINE_NOTCHED, PHOTO_CONTACT_STYLES_KEY,
                                               photo_contact_style_questions, settle_edge_holes,
                                               short_row_contradiction)

SCALE = 40                      # px per mm, isotropic
LENGTH, WIDTH = 40.0, 20.0
PITCH = 2.54
BOARD, GOLD, HOLE, CUT = (28, 28, 28), (60, 170, 215), (120, 120, 120), (235, 235, 235)


def _px(mm):
    """Millimetres to pixels at SCALE."""
    return int(round(mm * SCALE))


def _centres(count):
    """Contact centres along the top edge (mm from its start) of `board`."""
    return [5.0 + k * PITCH for k in range(count)]


def board(count=3, land_in=2.4, opening=(0.5, 0.5), closed=(1.6, 0.45), missing_hole=None):
    """A board with `count` lands on the top edge.

    `opening` = (half width mm, depth mm) of a cut open on the edge (it shows the
    background, CUT), `closed` = (centre depth mm, radius mm) of a closed hole,
    `missing_hole` = index of a land drawn without its closed hole.
    """
    image = np.full((_px(WIDTH), _px(LENGTH), 3), BOARD, np.uint8)
    for k, centre in enumerate(_centres(count)):
        cx = _px(centre)
        cv2.rectangle(image, (cx - _px(0.9), 0), (cx + _px(0.9), _px(land_in)), GOLD, -1)
        if opening is not None:
            cv2.ellipse(image, (cx, 0), (_px(opening[0]), _px(opening[1])), 0, 0, 360, CUT, -1)
        if closed is not None and k != missing_hole:
            cv2.circle(image, (cx, _px(closed[0])), _px(closed[1]), HOLE, -1)
    return image


# ── The agreement floor ────────────────────────────────────────────────────


@pytest.mark.parametrize("row_lands, floor", [
    (None, MIN_ROW_LANDS), (3, 3), (4, 4), (MIN_ROW_LANDS + 3, MIN_ROW_LANDS), (1, 2), (0, MIN_ROW_LANDS)])
def test_the_floor_is_the_whole_short_row_or_min_row_lands(row_lands, floor):
    """@brief A blind row needs MIN_ROW_LANDS; a located short row needs every land, never fewer than two."""
    assert hole_agreement_floor(row_lands) == floor


# ── The reader on a located short row ──────────────────────────────────────


def test_a_located_short_row_is_read_and_a_blind_one_is_not():
    """@brief Positive: located centres -> the inner hole is seen. Negative: blind -> no verdict."""
    image = board()
    located = measure_edge_holes(image, "top", PITCH, LENGTH, WIDTH, confirmed_centres_mm=_centres(3))
    assert located.style == "half_and_through"
    assert located.agreeing == 3 and located.closed == 3 and located.row_lands == 3
    blind = measure_edge_holes(image, "top", PITCH, LENGTH, WIDTH)
    assert blind.style is None and blind.row_lands is None


def test_no_closed_hole_behind_a_located_short_row_reads_half_hole():
    """@brief The structural negative of the case above: the same lands with NO inner hole."""
    reading = measure_edge_holes(board(closed=None, opening=(0.5, 0.45)), "top", PITCH, LENGTH, WIDTH,
                                 confirmed_centres_mm=_centres(3), land_inward_mm=2.4)
    assert reading.style == "half_hole" and reading.closed == 0


def test_one_land_of_a_short_row_disagreeing_gives_no_verdict():
    """@brief A short located row must agree on every land: one land without its hole decides nothing."""
    reading = measure_edge_holes(board(missing_hole=1), "top", PITCH, LENGTH, WIDTH,
                                 confirmed_centres_mm=_centres(3))
    assert reading.style is None and reading.row_lands == 3


def test_a_land_longer_than_the_window_is_read_whole_when_measured():
    """@brief The inner hole lies past one pitch from the edge.

    Without the measured land the reader sees only the opening and must NOT
    call it «half_hole» (a closed hole may lie past the window); with the land
    measured inwards it reads the whole land and finds the hole.
    """
    image = board(land_in=4.2, closed=(3.3, 0.45))
    unmeasured = measure_edge_holes(image, "top", PITCH, LENGTH, WIDTH, confirmed_centres_mm=_centres(3))
    assert unmeasured.style is None
    measured = measure_edge_holes(image, "top", PITCH, LENGTH, WIDTH, confirmed_centres_mm=_centres(3),
                                  land_inward_mm=4.2)
    assert measured.style == "half_and_through" and measured.closed == 3


# ── What the photo stage and the extraction do with it ─────────────────────


def _along():
    return float(SCALE)


def _declared_answer(style, count, holes):
    """An answer with one top side of `count` contacts declared `style` and the photo's `holes`."""
    side = dict(edge="top", count=count, pitch_mm=PITCH, first_pin=1, contact_style=style)
    data = dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
                body_length_mm=LENGTH, body_width_mm=WIDTH, castellated_sides=[side])
    extractor._canonicalize_answer(data)
    data["_measured_holes"] = holes
    return data


def test_a_long_located_row_without_inner_holes_settles_half_hole_and_replaces_the_declaration():
    """@brief Long rows keep the settle contract: 10 lands with no closed hole -> half_hole adopted, with a note."""
    reading = measure_edge_holes(board(count=10, closed=None, opening=(0.5, 0.45)), "top", PITCH, LENGTH,
                                 WIDTH, confirmed_centres_mm=_centres(10), land_inward_mm=2.4)
    settled, warning = settle_edge_holes(reading.as_dict(), _along(), outline=OUTLINE_NOTCHED)
    assert warning is None and settled["style"] == "half_hole"
    assert short_row_contradiction(settled, "half_and_through") is None
    params = extractor._params_from_tool_arguments(
        _declared_answer("half_and_through", 10, {"top": settled}))
    assert params.castellated_sides[0]["contact_style"] == "half_hole"
    assert "al posto di «half_and_through»" in params.notes


def test_a_settled_inner_hole_confirms_the_declared_style():
    """@brief The positive twin: the inner hole seen -> the declared half_and_through stands, no change."""
    reading = measure_edge_holes(board(), "top", PITCH, LENGTH, WIDTH, confirmed_centres_mm=_centres(3),
                                 land_inward_mm=2.4)
    settled, warning = settle_edge_holes(reading.as_dict(), _along(), outline=OUTLINE_NOTCHED)
    assert warning is None and settled["style"] == "half_and_through"
    assert short_row_contradiction(settled, "half_and_through") is None


# ── M1: a short row that contradicts the declaration is asked ──────────────


def _short_half_hole():
    """The settled reading of a 3-land located row with no closed hole."""
    reading = measure_edge_holes(board(closed=None, opening=(0.5, 0.45)), "top", PITCH, LENGTH, WIDTH,
                                 confirmed_centres_mm=_centres(3), land_inward_mm=2.4)
    settled, _warning = settle_edge_holes(reading.as_dict(), _along(), outline=OUTLINE_NOTCHED)
    assert settled["style"] == "half_hole" and settled["row_lands"] == 3
    return settled


def test_a_short_row_contradicting_the_declaration_is_withheld_with_its_reason():
    """@brief Positive: 3 lands, no closed hole, declared half_and_through -> withheld, why says 3 of 3."""
    why = short_row_contradiction(_short_half_hole(), "half_and_through")
    assert why and "3 piazzole di 3" in why and "half_and_through" in why


@pytest.mark.parametrize("declared, row_lands", [
    ("half_hole", 3),                    # agreement only confirms
    (None, 3),                           # nothing declared, nothing contradicted
    ("half_and_through", MIN_ROW_LANDS),  # a long row keeps the settle contract
    ("half_and_through", None)])         # a blind row keeps it too
def test_agreement_long_or_blind_rows_are_not_withheld(declared, row_lands):
    """@brief Negatives: only a SHORT located row that CONTRADICTS a declared style is withheld."""
    settled = dict(_short_half_hole(), row_lands=row_lands)
    assert short_row_contradiction(settled, declared) is None


def test_the_withheld_edge_becomes_a_question_recommending_the_photo():
    """@brief The question offers half_hole (no closed hole seen) and recommends it, with the reason."""
    settled = _short_half_hole()
    why = short_row_contradiction(settled, "half_and_through")
    pending = photo_contact_style_questions(
        {"top": {"reading": settled, "px_per_mm": _along(), "pitch_mm": PITCH,
                 "photo_style": "half_hole", "why": why}}, {}, {"top": OUTLINE_NOTCHED})
    entry = pending["top"]
    assert "half_hole" in entry["options"] and entry["recommended"] == "half_hole"
    assert entry["recommended_from"] == "photo"
    assert entry["photo_style"] == "half_hole" and entry["why"] == why


@pytest.mark.parametrize("twin_outline, recommended, source", [
    (OUTLINE_NOTCHED, "half_and_through", "like_edge"),   # a twin the outline settled wins
    (None, "half_hole", "photo")])                        # no outline witness: no twin, the photo
def test_a_twin_edge_settled_by_the_outline_beats_the_short_rows_own_reading(twin_outline, recommended,
                                                                             source):
    """@brief Same land pattern on an edge an outline witness settled -> its style is recommended.

    The twin's settled style is set apart from the photo's on purpose, so the
    test shows which evidence the recommendation follows.
    """
    settled = _short_half_hole()
    why = short_row_contradiction(settled, "half_and_through")
    twin = dict(settled, edge="bottom")
    pending = photo_contact_style_questions(
        {"top": {"reading": settled, "px_per_mm": _along(), "pitch_mm": PITCH,
                 "photo_style": "half_hole", "why": why},
         "bottom": {"reading": twin, "px_per_mm": _along(), "pitch_mm": PITCH}},
        {"bottom": dict(twin, style="half_and_through")},
        {"top": OUTLINE_NOTCHED, "bottom": twin_outline})
    entry = pending["top"]
    assert entry["recommended"] == recommended and entry["recommended_from"] == source
    assert entry["like_edge"] == ("bottom" if source == "like_edge" else None)


@pytest.mark.parametrize("row_lands, seen", [(3, True), (None, False)])
def test_closed_holes_of_a_located_short_row_count_as_seen(row_lands, seen):
    """@brief The question's «through hole seen» uses the reader's own floor: 3 of 3 located, not 3 blind."""
    reading = dict(edge="top", style="half_and_through", contacts=3, agreeing=3, drill_mm=0.9,
                   chord_mm=0.1, reason="", openings=3, closed=3, row_lands=row_lands)
    pending = photo_contact_style_questions(
        {"top": {"reading": reading, "px_per_mm": _along(), "pitch_mm": PITCH}}, {}, {})
    assert pending["top"]["through_hole_seen"] is seen
    assert ("half_hole" in pending["top"]["options"]) is (not seen)
    assert pending["top"]["photo_style"] is None and pending["top"]["why"] is None


def _photo_stage(tmp_path, count, closed):
    """Run the photo stage of extract_inventory on a synthetic notched top row, without AI."""
    from kicadgen.services.photo_inventory import extract_inventory
    margin = 60
    crop = board(count=count, closed=closed)
    image = np.full((crop.shape[0] + 2 * margin, crop.shape[1] + 2 * margin, 3), CUT, np.uint8)
    image[margin:margin + crop.shape[0], margin:margin + crop.shape[1]] = crop
    path = tmp_path / f"top-{count}-{closed is not None}.png"
    assert cv2.imwrite(str(path), image)
    declared = dict(layout="castellated", castellated_sides=[
        dict(edge="top", count=count, pitch_mm=PITCH, contact_style="half_and_through")])

    class _Stop(Exception):
        """The photo stage is over once the vision model would be called."""

    def no_ai(**_kwargs):
        raise _Stop

    with pytest.raises(_Stop):
        extract_inventory([path], "m", LENGTH, WIDTH, ["generic"], no_ai,
                          top_photo_index=0, detector="vlm", declared=declared)
    return declared


def test_the_photo_stage_settles_a_short_row_that_confirms_and_asks_one_that_contradicts(tmp_path):
    """@brief End to end, without AI, on a 3-notch row declared half_and_through.

    Inner holes drawn -> the photo confirms, half_and_through settled.
    None drawn -> nothing settled; one question, half_hole recommended, why given.
    """
    with_holes = _photo_stage(tmp_path, 3, (1.6, 0.45))
    assert with_holes["_measured_holes"]["top"]["style"] == "half_and_through"
    assert with_holes["_measured_holes"]["top"]["row_lands"] == 3
    without = _photo_stage(tmp_path, 3, None)
    assert "top" not in (without.get("_measured_holes") or {})
    entry = without[PHOTO_CONTACT_STYLES_KEY]["top"]
    assert entry["recommended"] == "half_hole" and entry["photo_style"] == "half_hole"
    assert "3 piazzole di 3" in entry["why"]


def test_the_photo_stage_settles_a_long_row_that_contradicts(tmp_path):
    """@brief Long-row contract unchanged: 10 notches, no inner hole -> half_hole settled, no question."""
    declared = _photo_stage(tmp_path, 10, None)
    assert declared["_measured_holes"]["top"]["style"] == "half_hole"
    assert "top" not in (declared.get(PHOTO_CONTACT_STYLES_KEY) or {})


# ── L1: a long located row ─────────────────────────────────────────────────


@pytest.mark.parametrize("closed, expected", [(None, "half_hole"), ((1.6, 0.45), "half_and_through")])
def test_a_long_located_row_reads_like_a_blind_one(closed, expected):
    """@brief 10 located lands: no closed hole -> half_hole; a closed hole behind -> half_and_through."""
    image = board(count=10, closed=closed, opening=(0.5, 0.45))
    reading = measure_edge_holes(image, "top", PITCH, LENGTH, WIDTH, confirmed_centres_mm=_centres(10),
                                 land_inward_mm=2.4)
    assert reading.style == expected and reading.row_lands == 10 and reading.agreeing == 10
    assert measure_edge_holes(image, "top", PITCH, LENGTH, WIDTH).style == expected


# ── L2: a located centre off the edge ──────────────────────────────────────


def test_a_located_centre_off_the_edge_gives_no_verdict():
    """@brief Every located centre on the edge -> verdict; one off the edge -> none (row no longer matched)."""
    image = board()
    assert measure_edge_holes(image, "top", PITCH, LENGTH, WIDTH,
                              confirmed_centres_mm=_centres(3)).style == "half_and_through"
    dropped = measure_edge_holes(image, "top", PITCH, LENGTH, WIDTH,
                                 confirmed_centres_mm=_centres(3) + [LENGTH + 5.0])
    assert dropped.style is None and dropped.row_lands == 4


# ── M2: a rival colour class that passes the floor ─────────────────────────


def test_a_rival_class_with_another_verdict_passing_the_floor_abstains():
    """@brief Tie (3 vs 3) and near-tie (3 vs 2 with floor 2) -> undecided; a rival below the floor -> decided."""
    copper = [(2, "half_and_through")] * 3
    tie = choose_land_class(copper + [(1, "through_hole")] * 3, 3, 0, 3)
    assert tie[4] is False
    near_tie = choose_land_class(copper + [(1, "through_hole")] * 2, 3, 0, 2)
    assert near_tie[1] == "half_and_through" and near_tie[4] is False
    below = choose_land_class(copper + [(1, "through_hole")] * 2, 3, 0, 3)
    assert below[:3] == (2, "half_and_through", 3) and below[4] is True
    same = choose_land_class(copper + [(1, "half_and_through")] * 3, 3, 0, 3)
    assert same[4] is True
    # The mask class never holds the lands, whatever it reads.
    assert choose_land_class([(0, "through_hole")] * 5, 3, 0, 3) is None


@pytest.mark.parametrize("width_2, width_1, expected", [
    (45.0, 29.0, (2, "half_and_through")),  # class 2 encloses class 1 by more than the ring walls
    (45.0, 40.0, None),                      # wider by less than the two walls: unknown -> abstain
    (29.0, 45.0, (1, "through_hole"))])      # class 1 encloses class 2: class 1 is the land
def test_the_enclosing_class_is_the_land(width_2, width_1, expected):
    """@brief A land surrounds its hole: the class wider by both ring walls wins a conflict, else abstain."""
    verdicts = [(2, "half_and_through")] * 4 + [(1, "through_hole")] * 4
    choice = choose_land_class(verdicts, 3, 0, 4, {2: width_2, 1: width_1}, enclosure_px=11.0)
    if expected is None:
        assert choice[4] is False
    else:
        assert choice[:2] == expected and choice[4] is True
