"""@brief The contact style of a castellated edge is read from the photo, not declared (2026-10-08).

The model declared «half_hole» or «half_and_through» for the same documents
from one run to the next, and the header pins of the 3D depend on it. On
the calibrated crop each land of the periodic row shows how its hole meets
the board edge: an opening whose widest row is on the edge (a half-hole), a
closed hole behind an opening (half-hole plus through-hole), or a closed
hole only (a through-hole with a tongue to the edge). Positive and negative
cases differ in structure, never in a number tuned to a board.
"""
import cv2
import numpy as np
import pytest

from kicadgen.services import datasheet_extractor as extractor
from kicadgen.services.edge_lands import MIN_ROW_LANDS, measure_edge_holes

SCALE = 40                      # px per mm, isotropic
LENGTH, WIDTH = 40.0, 20.0
BOARD, GOLD, HOLE, LABEL = (28, 28, 28), (60, 170, 215), (120, 120, 120), (235, 235, 235)


def _px(mm):
    return int(round(mm * SCALE))


def board(count=10, pitch=2.54, land_w=1.8, land_in=2.6, opening=None, closed=None, labels=False,
          setback=0.0, opening_colour=HOLE):
    """A board with `count` lands on the top edge; `opening` = (half width mm, depth mm) of a
    notch open on the edge, `closed` = (centre depth mm, radius mm) of a closed hole,
    `setback` = mm of mask between the edge and the land (a ring set back from the edge),
    `opening_colour` = what the notch shows (the background for a real cut, BOARD for the
    copper-free semicircle of a U-shaped pad)."""
    image = np.full((_px(WIDTH), _px(LENGTH), 3), BOARD, np.uint8)
    for k in range(count):
        cx = _px(5.0 + k * pitch)
        cv2.rectangle(image, (cx - _px(land_w / 2), _px(setback)), (cx + _px(land_w / 2), _px(land_in)), GOLD, -1)
        if opening is not None:
            half, depth = opening
            cv2.ellipse(image, (cx, 0), (_px(half), _px(depth)), 0, 0, 360, opening_colour, -1)
        if closed is not None:
            depth, radius = closed
            cv2.circle(image, (cx, _px(depth)), _px(radius), HOLE, -1)
        if labels:
            cv2.putText(image, str(k), (cx + _px(pitch / 2) - 8, _px(1.2)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, LABEL, 1)
    return image


def test_an_opening_on_the_edge_and_a_closed_hole_behind_it_is_half_and_through():
    reading = measure_edge_holes(board(opening=(0.5, 0.4), closed=(1.6, 0.45)), "top", 2.54, LENGTH, WIDTH)
    assert reading.style == "half_and_through" and reading.agreeing >= MIN_ROW_LANDS
    assert reading.drill_mm == pytest.approx(0.9, abs=0.15)
    assert reading.chord_mm is not None and reading.chord_mm > 0


def test_a_closed_hole_in_a_land_touching_the_edge_is_a_through_hole():
    """@brief Positive: a land reaching the edge around a closed hole, no opening -> through_hole.

    CONTRACT CHANGED 2026-11-13 evening (user, fix set C; audit H1): it was
    «half_and_through» (an edge pad with a hole), and the generator then drilled
    a half-hole the board does not have. No opening segment, no half-hole.
    """
    reading = measure_edge_holes(board(closed=(1.6, 0.45)), "top", 2.54, LENGTH, WIDTH)
    assert reading.style == "through_hole" and reading.chord_mm == 0.0 and reading.openings == 0
    assert reading.drill_mm == pytest.approx(0.9, abs=0.15)


def test_a_closed_hole_in_a_land_set_back_from_the_edge_is_a_through_hole():
    """@brief Negativo: corona arretrata dal bordo, nessun rame sul bordo -> through_hole.

    Limite dichiarato: una corona arretrata oltre la fascia di anelli che il
    lettore guarda (COPPER_STRIP_RINGS) non e' piu' una piazzola di bordo per
    il lettore, che non da' verdetto: resta lo stile dichiarato.
    """
    reading = measure_edge_holes(board(closed=(1.6, 0.45), setback=0.3), "top", 2.54, LENGTH, WIDTH)
    assert reading.style == "through_hole" and reading.chord_mm == 0.0
    assert measure_edge_holes(board(closed=(1.6, 0.45), setback=0.5), "top", 2.54, LENGTH, WIDTH).style is None


def test_a_semicircle_centred_on_the_edge_is_a_half_hole():
    reading = measure_edge_holes(board(opening=(0.45, 0.45)), "top", 2.54, LENGTH, WIDTH)
    assert reading.style == "half_hole"


def test_labels_between_the_lands_do_not_change_the_reading():
    """@brief Negativo: le scritte nei vuoti fra le piazzole non sono fori."""
    reading = measure_edge_holes(board(closed=(1.6, 0.45), labels=True), "top", 2.54, LENGTH, WIDTH)
    assert reading.style == "through_hole"   # contract of 2026-11-13 evening, see above


def test_too_few_lands_or_no_row_give_no_verdict():
    assert measure_edge_holes(board(count=MIN_ROW_LANDS - 1, closed=(1.6, 0.45)), "top", 2.54,
                              LENGTH, WIDTH).style is None
    assert measure_edge_holes(board(count=0), "top", 2.54, LENGTH, WIDTH).style is None
    assert measure_edge_holes(board(closed=(1.6, 0.45)), "bottom", 2.54, LENGTH, WIDTH).style is None


# ── The extraction adopts the style the photo read ────────────────────────


def _answer(style="half_hole", holes=None):
    side = dict(edge="top", count=10, pitch_mm=2.54, first_pin=1, contact_style=style,
                value_sources={"pitch_mm": "read"})
    data = dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
                body_length_mm=LENGTH, body_width_mm=WIDTH, castellated_sides=[side])
    extractor._canonicalize_answer(data)
    if holes is not None:
        data["_measured_holes"] = holes
    return data


READ = {"top": dict(edge="top", style="half_and_through", contacts=10, agreeing=9,
                    drill_mm=0.9, chord_mm=0.7, reason="")}


def test_a_style_the_photo_contradicts_is_replaced_with_a_note():
    params = extractor._params_from_tool_arguments(_answer("half_hole", READ))
    [side] = params.castellated_sides
    assert side["contact_style"] == "half_and_through"
    assert "9 piazzole su 10" in side["_contact_style_evidence"]
    assert "al posto di «half_hole»" in params.notes


def test_a_style_the_photo_confirms_is_left_alone():
    """CONTRACT CHANGED (night real run 2026-11-14): the style stays, but no longer silently.

    A style the photo confirms carried no trace, so the result could not tell
    «declared, never checked» from «confirmed by the photo». The style is
    unchanged; the evidence and a «confermato dalla foto» note are present,
    and the visible barrel width is never written as a drill size.
    """
    params = extractor._params_from_tool_arguments(_answer("half_and_through", READ))
    [side] = params.castellated_sides
    assert side["contact_style"] == "half_and_through"
    assert "9 piazzole su 10" in side["_contact_style_evidence"]
    assert "confermato dalla foto" in params.notes and "al posto di" not in params.notes
    assert side.get("drill_mm") != 0.9 and params.drill_mm != 0.9


def test_a_side_without_a_photo_reading_carries_no_evidence():
    """Negative: no measured hole for the edge -> declared, nothing confirmed."""
    params = extractor._params_from_tool_arguments(_answer("half_and_through", {}))
    [side] = params.castellated_sides
    assert "_contact_style_evidence" not in side and "confermato dalla foto" not in params.notes


def test_a_raw_answer_cannot_smuggle_a_hole_reading():
    data = dict(layout="castellated", confidence="high", drawing_unit="mm", contacts_view="top",
                body_length_mm=LENGTH, body_width_mm=WIDTH, _measured_holes=READ,
                castellated_sides=[dict(edge="top", count=10, pitch_mm=2.54, first_pin=1,
                                        contact_style="half_hole")])
    params = extractor._params_from_tool_arguments(data)
    assert params.castellated_sides[0]["contact_style"] == "half_hole"


# CONTRACT CHANGED 2026-11-13 evening (user, fix set C; generality audit H1/H2):
# the land colours see a hole segment, not a cut; the board OUTLINE says whether
# the edge is cut. Three boards, same closed hole behind each land:
# - no edge feature -> through_hole (the reader saw no opening);
# - a copper-free semicircle of a U-shaped pad on a STRAIGHT outline (the 18:32
#   devkit) -> the reader sees «half_and_through», the outline no notch ->
#   through_hole, the reader's style kept as `measured_style`;
# - a real cut (the background shows through, the outline is notched at the
#   contacts) -> half_and_through.
@pytest.mark.parametrize("opening, colour, expected, measured", [
    (None, None, "through_hole", None),
    ((0.5, 0.5), BOARD, "through_hole", "half_and_through"),
    ((0.5, 0.5), (235, 235, 235), "half_and_through", None)])
def test_the_photo_stage_reads_the_holes_and_the_extraction_adopts_the_style(tmp_path, opening,
                                                                             colour, expected,
                                                                             measured):
    """@brief End to end, senza AI: foto -> `_measured_holes` -> stile del lato corretto."""
    from kicadgen.services.photo_inventory import extract_inventory
    margin = 60
    crop = board(opening=opening, closed=(1.6, 0.45), opening_colour=colour or HOLE)
    image = np.full((crop.shape[0] + 2 * margin, crop.shape[1] + 2 * margin, 3), (235, 235, 235), np.uint8)
    image[margin:margin + crop.shape[0], margin:margin + crop.shape[1]] = crop
    path = tmp_path / "top.png"
    assert cv2.imwrite(str(path), image)
    declared = dict(layout="castellated", castellated_sides=[
        dict(edge="top", count=10, pitch_mm=2.54, contact_style="half_and_through")])

    class _Stop(Exception):
        """The photo stage is over once the vision model would be called."""

    def no_ai(**_kwargs):
        raise _Stop

    with pytest.raises(_Stop):
        extract_inventory([path], "m", LENGTH, WIDTH, ["generic"], no_ai,
                          top_photo_index=0, detector="vlm", declared=declared)
    reading = declared["_measured_holes"]["top"]
    assert reading["style"] == expected and reading["agreeing"] >= MIN_ROW_LANDS
    # A changed verdict keeps what the reader saw and says why.
    assert reading.get("measured_style") == measured
    assert bool(reading.get("reason")) is (measured is not None)
    # Declared «half_hole» instead: the extraction takes the photo's style.
    data = _answer("half_hole", declared["_measured_holes"])
    params = extractor._params_from_tool_arguments(data)
    assert params.castellated_sides[0]["contact_style"] == expected
