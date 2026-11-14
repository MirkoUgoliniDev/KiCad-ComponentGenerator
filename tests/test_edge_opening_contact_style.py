"""@brief A half-hole is a cut of the board edge; an impossible double contact names its type (fix set C, 2026-11-13).

The analysis of 2026-11-13 18:32 (a devkit with plain through holes for pin
headers, U-shaped pads whose copper-free semicircle reaches a STRAIGHT board
edge) had the photo turn the model's «half_hole» into «half_and_through»;
the generator then refused the side with «resterebbero -0.81mm di scheda»
and a remedy about the row spacing only. The rules checked here (after the
generality audit, H1/H2/M3/L5):
- the land colours see a hole segment, the board OUTLINE says whether the
  edge is cut (`photo_inventory.settle_edge_holes`);
- an opening narrower than the photo resolves gives NO verdict, never a
  through hole (`photo_inventory.edge_opening_style`);
- through_hole is a positive observation: a closed hole and no opening
  (`edge_lands.measure_edge_holes`), refused when the outline is notched;
- a half_and_through side the declared rows make impossible becomes a
  through_hole side when that fits, with a note
  (`datasheet_extractor._settle_impossible_double_contacts`);
- the generator's wall error names the contact type when a through hole fits.
"""
import pathlib
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_THROUGH_HOLE, MIN_HOLE_WALL_MM, CastellatedModuleParams,
    CastellatedSide)
from kicadgen.services import datasheet_extractor as de
from kicadgen.services.edge_lands import measure_edge_holes
from kicadgen.services.photo_inventory import (OUTLINE_NOTCHED, OUTLINE_SMOOTH, PHOTO_CONTACT_STYLES_KEY,
                                               edge_opening_style, photo_contact_style_questions,
                                               edge_opening_window_mm, extract_inventory,
                                               settle_edge_holes)
from kicadgen.cad3d.measurement_uncertainty import EDGE_UNCERTAINTY_PX


def _reading(style, drill, chord, agreeing=20, contacts=20):
    return {"edge": "bottom", "style": style, "contacts": contacts, "agreeing": agreeing,
            "drill_mm": drill, "chord_mm": chord, "reason": "",
            "openings": agreeing if chord else 0}


# --- the opening criterion --------------------------------------------------------------------

def test_the_window_is_the_readers_hole_wall_plus_two_boundary_pixels():
    assert edge_opening_window_mm(40.0) == pytest.approx(MIN_HOLE_WALL_MM + 2 * EDGE_UNCERTAINTY_PX / 40.0)
    with pytest.raises(ValueError):
        edge_opening_window_mm(0)


@pytest.mark.parametrize("style, drill, chord", [
    ("half_hole", 1.00, 0.96),            # a castellation: chord as wide as the hole
    ("half_and_through", 1.00, 0.80),     # a cut on the edge and a closed hole behind
])
def test_a_resolved_opening_keeps_the_half_hole(style, drill, chord):
    settled = edge_opening_style(_reading(style, drill, chord), 40.96)
    assert settled["style"] == style and "measured_style" not in settled


def test_an_unresolved_opening_gives_no_verdict_never_a_through_hole():
    """H1: 0.26 mm is resolved by a sharp photo, not by a coarse one -> no verdict there."""
    sharp, coarse = 100.0, 20.0
    assert edge_opening_window_mm(sharp) < 0.26 < edge_opening_window_mm(coarse)
    assert edge_opening_style(_reading("half_hole", 0.30, 0.26), sharp)["style"] == "half_hole"
    settled = edge_opening_style(_reading("half_hole", 0.30, 0.26), coarse)
    assert settled["style"] is None and settled["measured_style"] == "half_hole"
    assert settled["reason"]


def test_a_through_hole_reading_is_left_as_it_is():
    reading = _reading(CONTACT_THROUGH_HOLE, 1.0, 0.0)
    assert edge_opening_style(reading, 40.0) == reading


# --- two photo witnesses: the land colours and the outline (H2) -------------------------------

def test_a_through_hole_on_a_notched_outline_gives_no_verdict_and_a_warning():
    settled, warning = settle_edge_holes(_reading(CONTACT_THROUGH_HOLE, 0.98, 0.0), 40.96,
                                         outline=OUTLINE_NOTCHED)
    assert settled is None
    assert "tacche sul contorno ai contatti dichiarati ma nessuna apertura" in warning


@pytest.mark.parametrize("outline", [OUTLINE_SMOOTH, None])
def test_a_through_hole_elsewhere_is_adopted(outline):
    settled, warning = settle_edge_holes(_reading(CONTACT_THROUGH_HOLE, 0.98, 0.0), 40.96,
                                         outline=outline)
    assert settled["style"] == CONTACT_THROUGH_HOLE and warning is None


def test_an_opening_on_a_straight_outline_is_no_cut():
    """The 18:32 situation: lands read as an opening + closed hole, the outline has no notch."""
    settled, warning = settle_edge_holes(_reading("half_and_through", 0.98, 0.87), 40.96,
                                         outline=OUTLINE_SMOOTH)
    assert settled["style"] == CONTACT_THROUGH_HOLE and warning is None
    assert settled["measured_style"] == "half_and_through" and settled["reason"]
    # Without a closed hole there is nothing left to adopt: no verdict.
    settled, warning = settle_edge_holes(_reading("half_hole", 0.98, 0.87), 40.96,
                                         outline=OUTLINE_SMOOTH)
    assert settled is None and "non confermato" in warning


def test_an_opening_needs_the_outline():
    """No outline witness (uncountable edge): the colours alone never prove a cut."""
    settled, warning = settle_edge_holes(_reading("half_hole", 0.98, 0.87), 40.96, outline=None)
    assert settled is None and "non conferma tacche" in warning
    settled, warning = settle_edge_holes(_reading("half_hole", 0.98, 0.87), 40.96,
                                         outline=OUTLINE_NOTCHED)
    assert settled["style"] == "half_hole" and warning is None


def test_the_photo_overrides_the_models_half_hole_with_a_note():
    """The settled reading reaches the side through _apply_measured_holes, with the reason."""
    reading, _warning = settle_edge_holes(_reading("half_and_through", 0.98, 0.87), 40.96,
                                          outline=OUTLINE_SMOOTH)
    params = SimpleNamespace(castellated_sides=[{"edge": "bottom", "contact_style": "half_hole"}])
    notes = []
    de._apply_measured_holes({"_measured_holes": {"bottom": reading}}, params, notes)
    assert params.castellated_sides[0]["contact_style"] == CONTACT_THROUGH_HOLE
    assert "contorno della scheda senza tacche" in params.castellated_sides[0][
        "_contact_style_evidence"]
    assert len(notes) == 1 and "«half_hole»" in notes[0]


# --- synthetic castellations at several resolutions (H1, L5) ----------------------------------

_BG, _BOARD, _GOLD, _HOLE = (235, 235, 235), (28, 28, 28), (60, 170, 215), (120, 120, 120)
_LENGTH, _WIDTH = 40.0, 20.0


def _castellated(scale, cut=True, closed=None, count=12, pitch=1.27, hole=0.6):
    """Lands on the top edge with a half-hole centred on it: the background shows through a
    real cut (`cut`), the board colour fills a copper-free notch; `closed` = (depth, radius)
    of a closed hole behind each land, in mm."""
    def px(mm):
        return int(round(mm * scale))
    image = np.full((px(_WIDTH), px(_LENGTH), 3), _BOARD, np.uint8)
    for k in range(count):
        cx = px(5.0 + k * pitch)
        cv2.rectangle(image, (cx - px(pitch * 0.35), 0), (cx + px(pitch * 0.35), px(pitch)), _GOLD, -1)
        if hole:
            cv2.ellipse(image, (cx, 0), (px(hole / 2), px(hole / 2)), 0, 0, 360,
                        _BG if cut else _BOARD, -1)
        if closed:
            cv2.circle(image, (cx, px(closed[0])), px(closed[1]), _HOLE, -1)
    return image


@pytest.mark.parametrize("scale", [12, 15, 20, 40])
def test_a_real_castellation_is_never_read_as_a_through_hole(scale):
    """H1 positive: 0.6 mm half-holes on a 1.27 mm pitch, from 40 down to 12 px/mm."""
    reading = measure_edge_holes(_castellated(scale), "top", 1.27, _LENGTH, _WIDTH)
    assert reading.style in ("half_hole", None)
    if reading.style:
        settled, _warning = settle_edge_holes(reading.as_dict(), scale, outline=OUTLINE_NOTCHED)
        assert settled is None or settled["style"] == "half_hole"


def test_a_closed_hole_with_no_opening_is_a_through_hole():
    """H1 negative: the same lands without the edge notch, a closed hole behind."""
    reading = measure_edge_holes(_castellated(20, hole=None, closed=(0.75, 0.3)), "top", 1.27,
                                 _LENGTH, _WIDTH)
    assert reading.style == CONTACT_THROUGH_HOLE and reading.openings == 0


def _photo_stage(tmp_path, crop, scale, style, pitch, count):
    margin = max(20, int(1.5 * scale))
    image = np.full((crop.shape[0] + 2 * margin, crop.shape[1] + 2 * margin, 3), _BG, np.uint8)
    image[margin:margin + crop.shape[0], margin:margin + crop.shape[1]] = crop
    path = pathlib.Path(tmp_path) / "top.png"
    assert cv2.imwrite(str(path), image)
    declared = dict(layout="castellated", castellated_sides=[
        dict(edge="top", count=count, pitch_mm=pitch, contact_style=style)])
    lines = []

    class _Stop(Exception):
        """The photo stage is over once the vision model would be called."""

    def no_ai(**_kwargs):
        raise _Stop

    with pytest.raises(_Stop):
        extract_inventory([path], "m", _LENGTH, _WIDTH, ["generic"], no_ai, top_photo_index=0,
                          detector="vlm", declared=declared, progress=lambda m: lines.append(str(m)))
    return declared.get("_measured_holes", {}).get("top"), lines


def test_a_low_resolution_castellation_end_to_end(tmp_path):
    """L5: 0.6 mm half-holes at 12 px/mm through the whole photo stage stay half-holes."""
    reading, _lines = _photo_stage(tmp_path, _castellated(12), 12, "half_hole", 1.27, 12)
    assert reading is None or reading["style"] == "half_hole"


def test_a_u_shaped_pad_on_a_straight_edge_end_to_end(tmp_path):
    """The 18:32 board in miniature: copper-free notches inside a straight outline, closed
    holes behind -> through_hole, with the reader's style kept."""
    crop = _castellated(40, cut=False, closed=(0.85, 0.3), hole=0.5)
    reading, _lines = _photo_stage(tmp_path, crop, 40, "half_and_through", 1.27, 12)
    assert reading["style"] == CONTACT_THROUGH_HOLE
    assert reading["measured_style"] == "half_and_through"


def test_the_notch_guard_end_to_end(tmp_path, monkeypatch):
    """H2: the outline notched at the contacts while the lands show no opening -> no verdict,
    and the warning is shown (the land reader is forced to a through_hole reading)."""
    from kicadgen.services import edge_lands
    from kicadgen.services.edge_lands import HoleStyle
    monkeypatch.setattr(edge_lands, "measure_edge_holes",
                        lambda *a, **k: HoleStyle("top", CONTACT_THROUGH_HOLE, 12, 12, 0.6, 0.0))
    reading, lines = _photo_stage(tmp_path, _castellated(40), 40, "half_hole", 1.27, 12)
    assert reading is None
    assert any("tacche sul contorno ai contatti dichiarati ma nessuna apertura" in line
               for line in lines)


# --- a double contact the declared rows rule out ----------------------------------------------

def _sides(style, half=1.8):
    return [{"edge": edge, "count": 22, "pitch_mm": 2.54, "contact_style": style,
             "half_hole_diameter_mm": half} for edge in ("top", "bottom")]


def _params(style=CONTACT_HALF_AND_THROUGH, row_spacing=22.86, half=1.8, drill=None):
    """A two-row castellated result; `drill` given -> the through-hole drill is DECLARED."""
    sides = _sides(style, half)
    sources = {f"castellated_sides.{edge}.half_hole_diameter_mm": "derived"
               for edge in ("top", "bottom")}
    if drill is not None:
        for side in sides:
            side["through_hole_drill_mm"] = drill
        sources.update({f"castellated_sides.{edge}.through_hole_drill_mm": "derived"
                        for edge in ("top", "bottom")})
    return SimpleNamespace(
        body_length_mm=63.3, body_width_mm=25.4, row_spacing_mm=row_spacing,
        castellated_sides=sides, drill_mm=None, pad_diameter_mm=None, value_sources=sources)


#: The photo READ both edges as plain through-holes (`_measured_holes`).
_PHOTO_THROUGH = {"_measured_holes": {edge: {"style": CONTACT_THROUGH_HOLE}
                                      for edge in ("top", "bottom")}}


def test_an_impossible_double_contact_becomes_a_through_hole_through_the_photo():
    """CONTRACT CHANGED 2026-11-13 (generality audit item 3): the settle's own style switch
    could not be reached -- `_apply_measured_holes` runs first and adopts the photo's
    through-hole verdict. In that real order the sides become through-holes by the photo
    adoption (its note), and the settle, run next, changes nothing."""
    params, notes = _params(drill=1.0), []
    data = {"_measured_holes": {edge: {"style": CONTACT_THROUGH_HOLE, "agreeing": 22,
                                       "contacts": 22, "drill_mm": 1.0, "chord_mm": 0.0}
                                for edge in ("top", "bottom")}}
    de._apply_measured_holes(data, params, notes)
    de._settle_impossible_double_contacts(data, params, notes)
    assert [s["contact_style"] for s in params.castellated_sides] == [CONTACT_THROUGH_HOLE] * 2
    assert len(notes) == 2 and all("letto sulla foto" in note for note in notes)
    assert not any("l'unico compatibile" in note for note in notes)


def test_an_assumed_drill_is_reduced_before_any_switch():
    """CONTRACT CHANGED 2026-11-13 (round 7, audit H3): the old fixture's drill was only
    ASSUMED. An assumed drill is no evidence the contact cannot exist: it is reduced to the
    largest that leaves the wall, written as an estimate with a note, and the double contact
    stays (rows at 21.0 mm leave room for a 0.83 mm drill)."""
    params, notes = _params(row_spacing=21.0), []
    de._settle_impossible_double_contacts(_PHOTO_THROUGH, params, notes)
    assert [s["contact_style"] for s in params.castellated_sides] == [CONTACT_HALF_AND_THROUGH] * 2
    assert {s["through_hole_drill_mm"] for s in params.castellated_sides} == {0.83}
    assert len(notes) == 2 and all("foro passante stimato ridotto" in note for note in notes)


def test_an_assumed_drill_that_cannot_shrink_enough_changes_nothing():
    """CONTRACT CHANGED 2026-11-13 (round 7, audit H3): rows at 22.86 mm leave no room even
    for the smallest estimated drill; with an assumed drill nothing is switched and the
    generator blocks with its remedy (no silent change of contact type)."""
    params, notes = _params(), []
    de._settle_impossible_double_contacts(_PHOTO_THROUGH, params, notes)
    assert [s["contact_style"] for s in params.castellated_sides] == [CONTACT_HALF_AND_THROUGH] * 2
    assert not notes


def test_a_photo_that_measured_an_opening_keeps_the_double_contact():
    """Two witnesses disagree: the side is not changed, the generator names the type instead.

    CONTRACT CHANGED 2026-11-13 (generality audit item 3): the settle never switches a style;
    with declared sizes a double contact the rows rule out stays for the generator to refuse,
    whatever the photo read on that edge."""
    params, notes = _params(drill=1.0), []
    data = {"_measured_holes": {"top": {"style": "half_and_through", "chord_mm": 0.9}}}
    de._settle_impossible_double_contacts(data, params, notes)
    assert {s["contact_style"] for s in params.castellated_sides} == {CONTACT_HALF_AND_THROUGH}
    assert not notes


def test_a_double_contact_that_fits_is_not_touched():
    """Negative: rows far enough from the edge leave the wall -> nothing changes."""
    params, notes = _params(row_spacing=17.0), []
    de._settle_impossible_double_contacts({}, params, notes)
    assert [s["contact_style"] for s in params.castellated_sides] == [CONTACT_HALF_AND_THROUGH] * 2
    assert not notes


def test_rows_too_close_for_any_closed_ring_change_nothing():
    """Negative: rows 0.2 mm from the edge hold no closed ring, so the generator reads them as
    the half-hole rows (no through hole, no wall to break) and nothing is switched."""
    params, notes = _params(row_spacing=25.0), []
    de._settle_impossible_double_contacts({}, params, notes)
    assert [s["contact_style"] for s in params.castellated_sides] == [CONTACT_HALF_AND_THROUGH] * 2
    assert not notes


def _module(style, row_spacing):
    return CastellatedModuleParams(
        name="Rows", body_length=63.3, body_width=25.4, row_spacing=row_spacing,
        hole_diameter=1.8,
        sides=[CastellatedSide(edge="top", count=22, pitch=2.54, first_pin=1, contact_style=style),
               CastellatedSide(edge="bottom", count=22, pitch=2.54, first_pin=23, reverse=True,
                               contact_style=style)])


def test_the_wall_error_names_the_contact_type_when_a_through_hole_fits():
    with pytest.raises(ValueError) as error:
        _module(CONTACT_HALF_AND_THROUGH, 22.86).validate()
    assert "«foro passante»" in str(error.value) and "tipo di contatto" in str(error.value)
    # The named alternative really is buildable.
    _module(CONTACT_THROUGH_HOLE, 22.86).validate()


@pytest.mark.parametrize("offset, named", [(0.6, False), (1.0, True)])
def test_the_type_is_named_only_when_its_ring_closes_inside_the_board(offset, named):
    """Structural: a through hole 0.6 mm from the edge leaves its 1.7 mm ring open (no type
    named); at 1.0 mm the ring closes (type named). Both double contacts are refused."""
    module = CastellatedModuleParams(
        name="Offset", body_length=63.3, body_width=25.4, hole_diameter=1.8, thru_hole_offset=offset,
        sides=[CastellatedSide(edge="top", count=22, pitch=2.54, first_pin=1,
                               contact_style=CONTACT_HALF_AND_THROUGH)])
    assert (module.through_hole_alternative(module.sides[0]) is not None) is named
    with pytest.raises(ValueError) as error:
        module.validate()
    assert ("«foro passante»" in str(error.value)) is named


# --- edges the photo cannot settle: one question per side (real runs 3a/3b, 2026-11-13) -------

def test_the_reader_counts_the_closed_holes_behind_the_lands():
    """A cut with a closed hole behind counts its closed holes; a castellation alone has none."""
    both = measure_edge_holes(_castellated(40, closed=(0.85, 0.3), hole=0.5), "top", 1.27,
                              _LENGTH, _WIDTH)
    assert both.style == "half_and_through" and both.closed == both.agreeing
    alone = measure_edge_holes(_castellated(40), "top", 1.27, _LENGTH, _WIDTH)
    assert alone.style == "half_hole" and alone.closed == 0


def _measured(style, drill, chord, closed, contacts=20, px=40.96, pitch=2.54):
    reading = {"edge": "x", "style": style, "contacts": contacts, "agreeing": contacts,
               "drill_mm": drill, "chord_mm": chord, "reason": "", "openings": contacts,
               "closed": closed}
    return {"reading": reading, "px_per_mm": px, "pitch_mm": pitch}


def _bottom_settled():
    """The bottom edge of the real runs: settled through_hole by its straight outline."""
    reading = _measured("half_and_through", 0.53, 0.87, 20)
    settled, _w = settle_edge_holes(reading["reading"], 40.96, outline=OUTLINE_SMOOTH)
    return reading, settled


def test_an_unsettled_edge_like_a_settled_one_is_recommended_its_style():
    """Positive: the top edge (outline unreadable) shows the bottom's pattern -> through_hole
    recommended, half_hole left out because closed holes were measured."""
    bottom, settled = _bottom_settled()
    readings = {"top": _measured("half_and_through", 0.56, 0.87, 19, contacts=19),
                "bottom": bottom}
    questions = photo_contact_style_questions(
        readings, {"bottom": settled}, {"bottom": OUTLINE_SMOOTH},
        evidence=lambda edge: {"document": "top.jpg", "crop": [0, 0, 10, 10]})
    assert list(questions) == ["top"]
    entry = questions["top"]
    assert entry["like_edge"] == "bottom" and entry["recommended"] == CONTACT_THROUGH_HOLE
    assert entry["through_hole_seen"] is True
    assert entry["options"] == [CONTACT_THROUGH_HOLE, CONTACT_HALF_AND_THROUGH]
    assert entry["evidence"] == {"document": "top.jpg", "crop": [0, 0, 10, 10]}


@pytest.mark.parametrize("change", [
    dict(drill=1.2),            # drill differs by more than the window (0.30 mm)
    dict(chord=0.2),            # opening differs by more than the window
    dict(pitch=2.0),            # another pitch: another row
    dict(style="half_hole"),    # another reader verdict
])
def test_a_different_pattern_recommends_nothing(change):
    """Negative: one structural difference from the settled edge -> no recommendation."""
    bottom, settled = _bottom_settled()
    top = _measured(change.get("style", "half_and_through"), change.get("drill", 0.56),
                    change.get("chord", 0.87), 19, contacts=19, pitch=change.get("pitch", 2.54))
    questions = photo_contact_style_questions({"top": top, "bottom": bottom},
                                              {"bottom": settled}, {"bottom": OUTLINE_SMOOTH})
    assert questions["top"]["like_edge"] is None and questions["top"]["recommended"] is None


def test_without_closed_holes_a_half_hole_stays_an_option():
    """Negative of rule (a): no closed hole measured behind the lands -> all three styles offered."""
    questions = photo_contact_style_questions(
        {"top": _measured("half_hole", 0.6, 0.55, 0, contacts=12)}, {}, {})
    assert questions["top"]["through_hole_seen"] is False
    assert questions["top"]["options"] == [CONTACT_THROUGH_HOLE, CONTACT_HALF_AND_THROUGH,
                                           "half_hole"]
    assert questions["top"]["like_edge"] is None


def test_a_settled_edge_or_an_edge_without_outline_witness_is_no_model():
    """Settled edges get no question; an edge settled without an outline witness (a through_hole
    reading adopted as it is) does not recommend its style to another edge."""
    bottom, settled = _bottom_settled()
    readings = {"top": _measured("half_and_through", 0.56, 0.87, 19, contacts=19),
                "bottom": bottom}
    questions = photo_contact_style_questions(readings, {"bottom": settled}, {})
    assert "bottom" not in questions and questions["top"]["like_edge"] is None


def _two_edged(scale, top_cut, bottom_cut, closed=(0.85, 0.3), hole=0.5):
    """Lands on the top and the bottom edge: the bottom half is the top one mirrored."""
    half = _castellated(scale, cut=top_cut, closed=closed, hole=hole)
    other = _castellated(scale, cut=bottom_cut, closed=closed, hole=hole)[::-1]
    rows = half.shape[0] // 2
    return np.ascontiguousarray(np.vstack([half[:rows], other[rows:]]))


def test_the_photo_stage_leaves_one_question_for_the_unsettled_side(tmp_path):
    """End to end: U-shaped pads on both straight edges; only the bottom side is declared with
    a half-hole style, so only its outline is witnessed (settled through_hole); the top side,
    declared through_hole, has no outline witness -> one entry, «like» the bottom."""
    crop = _two_edged(40, top_cut=False, bottom_cut=False)
    margin = 60
    image = np.full((crop.shape[0] + 2 * margin, crop.shape[1] + 2 * margin, 3), _BG, np.uint8)
    image[margin:margin + crop.shape[0], margin:margin + crop.shape[1]] = crop
    path = pathlib.Path(tmp_path) / "top.png"
    assert cv2.imwrite(str(path), image)
    declared = dict(layout="castellated", castellated_sides=[
        dict(edge="top", count=12, pitch_mm=1.27, contact_style="through_hole"),
        dict(edge="bottom", count=12, pitch_mm=1.27, contact_style="half_and_through")])

    class _Stop(Exception):
        """The photo stage is over once the vision model would be called."""

    def no_ai(**_kwargs):
        raise _Stop

    with pytest.raises(_Stop):
        extract_inventory([path], "m", _LENGTH, _WIDTH, ["generic"], no_ai, top_photo_index=0,
                          detector="vlm", declared=declared)
    assert declared["_measured_holes"]["bottom"]["style"] == CONTACT_THROUGH_HOLE
    (edge, entry), = declared[PHOTO_CONTACT_STYLES_KEY].items()
    assert edge == "top" and entry["like_edge"] == "bottom"
    assert entry["recommended"] == CONTACT_THROUGH_HOLE and entry["through_hole_seen"]
    # The evidence names the photo and a strip of it along the top edge.
    assert entry["evidence"]["document"] == "top.png"
    x0, y0, x1, y1 = entry["evidence"]["crop"]
    assert y0 == margin and x1 - x0 == crop.shape[1] and 0 < y1 - y0 < crop.shape[0] // 2
