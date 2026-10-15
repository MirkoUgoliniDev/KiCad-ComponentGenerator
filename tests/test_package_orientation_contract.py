"""@brief Orientation contract of package_geometry and its structural doubts (audit 2026-10-15).

Geometry M2: the extraction schema did not say that Y grows downward nor the
sense of the angles, and `mirrored` + `source_view: "bottom"` cancel each
other. Full-paths M3: body length and width were ambiguous between schema
and generator, so a SOIC with pins on the left and right got a body lying
over its pads. No network: schemas are inspected, answers are fakes.
"""
import copy

import pytest

from kicadgen.core.package_geometry import (
    DOUBT_BODY_OVER_LANDS, DOUBT_BODY_PADS, DOUBT_PIN_ORDER, DOUBT_VIEW_MIRROR, package_doubts,
    package_from_mapping)
from kicadgen.generators.package import build_package
from kicadgen.services import datasheet_extractor as de
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS


def _schema():
    """@brief Properties of the main extraction tool."""
    return de._openai_tool()["function"]["parameters"]["properties"]


def _dual_row(length, width, *, x=2.7, pad_length=1.55, pad_width=0.6, pitch=1.27, per_side=4,
              family="soic", view="top", mirrored=False, clockwise=False, pad_type="smd",
              orientation=0, extra=(), lead_form=None):
    """@brief A dual-row package, rows on the left and right, numbered counter-clockwise from the top.

    Pin 1 at the top of the left row, numbers growing downward, then up the
    right row: the JEDEC order seen from above with Y down. `clockwise`
    mirrors the numbering top-bottom, as a drawing read with Y up would.
    """
    pins = []
    first = -pitch * (per_side - 1) / 2
    for index in range(per_side):
        y = first + pitch * index
        pins.append(dict(number=str(index + 1), x_mm=-x, y_mm=y))
        pins.append(dict(number=str(2 * per_side - index), x_mm=x, y_mm=y))
    for pin in pins:
        if clockwise:
            pin["y_mm"] = -pin["y_mm"]
        pin.update(pad_length_mm=pad_length, pad_width_mm=pad_width, pad_type=pad_type,
                   shape="rect" if pad_type == "smd" else "circle")
        if pad_type == "thru_hole":
            pin["drill_mm"] = 0.8
    pins.extend(extra)
    return dict(family=family, body_length_mm=length, body_width_mm=width, body_height_mm=1.75,
                origin="body_center", source_view=view, mirrored=mirrored,
                orientation_deg=orientation, confidence="high", needs_review=False, pins=pins,
                lead_form=lead_form)


def _codes(data):
    """@brief Doubt codes of a package mapping."""
    return [doubt.code for doubt in package_doubts(package_from_mapping(data))]


# --- (a) schema: coordinate convention and body axes -------------------------

def test_package_geometry_schema_states_y_down_and_counter_clockwise_angles():
    description = _schema()["package_geometry"]["description"]
    assert "Y cresce verso il BASSO" in description
    assert "ANTIORARIO" in description
    properties = _schema()["package_geometry"]["properties"]
    assert "ANTIORARI" in properties["orientation_deg"]["description"]
    assert "ANTIORARI" in properties["pins"]["items"]["properties"]["rotation_deg"]["description"]


def test_package_geometry_body_axes_follow_the_coordinate_frame():
    """Length along X of the pad coordinates, whatever the datasheet calls it; width along Y."""
    properties = _schema()["package_geometry"]["properties"]
    length = properties["body_length_mm"]["description"]
    width = properties["body_width_mm"]["description"]
    assert "asse X di queste coordinate" in length and "TRASVERSALE" in length
    assert "asse Y di queste coordinate" in width and "LUNGO le file" in width


def test_top_level_body_length_no_longer_ties_x_to_the_pitch_for_explicit_pads():
    """The old text «asse X, stessa direzione del pitch» is wrong when the rows run along Y."""
    main = _schema()
    text = main["body_length_mm"]["description"]
    assert "stessa direzione del pitch" not in text
    assert "trasversale alle file" in text and "asse X del footprint" in text
    assert "asse Y del footprint" in main["body_width_mm"]["description"]
    # The single-component tool reuses the same descriptions.
    single = de._single_component_tool()["function"]["parameters"]["properties"]
    assert single["body_length_mm"] == main["body_length_mm"]
    assert single["body_width_mm"] == main["body_width_mm"]


def test_mirrored_is_defined_as_not_the_bottom_view():
    text = _schema()["package_geometry"]["properties"]["mirrored"]["description"]
    assert "BOTTOM VIEW" in text and "mirrored false" in text


# --- (b) bottom view also declared mirrored -----------------------------------

def test_bottom_view_declared_mirrored_is_a_doubt_and_is_not_normalised():
    data = _dual_row(3.9, 4.9, view="bottom", mirrored=True, clockwise=False)
    geometry = package_from_mapping(data)
    doubts = package_doubts(geometry)
    assert DOUBT_VIEW_MIRROR in [doubt.code for doubt in doubts]
    # Not corrected: the reflections still cancel, the pads are as drawn.
    top = geometry.pins_seen_from_top()
    assert [(p.x_mm, p.y_mm) for p in top] == [(p.x_mm, p.y_mm) for p in geometry.pins]


@pytest.mark.parametrize("view,mirrored", [("bottom", False), ("top", True), ("top", False)])
def test_a_single_reflection_or_none_is_not_a_view_doubt(view, mirrored):
    """Negative: only the two declarations together cancel each other."""
    assert DOUBT_VIEW_MIRROR not in _codes(_dual_row(3.9, 4.9, view=view, mirrored=mirrored))


# --- (c) body over the pads ---------------------------------------------------

def test_transposed_soic_body_is_a_doubt_with_the_swap_proposed():
    """Audit repro: SOIC-8, rows at x = +-2.7, body 4.9 written along X instead of along the rows."""
    data = _dual_row(4.9, 3.9)
    doubts = package_doubts(package_from_mapping(data))
    body = [doubt for doubt in doubts if doubt.code == DOUBT_BODY_PADS]
    assert len(body) == 1 and body[0].swap_clears is True
    assert set(body[0].pins) == {"1", "4", "5", "8"}  # end leads reach past the 3.9 mm face
    assert body[0].message.startswith("DUBBIO corpo/pad")


@pytest.mark.parametrize("pads", [dict(x=2.7, pad_length=1.55), dict(x=2.475, pad_length=1.95)])
def test_correct_soic_body_is_not_a_doubt(pads):
    """Negative: the same part with the body across the rows on X (KiCad's own SOIC-8 lands too)."""
    assert _codes(_dual_row(3.9, 4.9, **pads)) == []


def test_package_with_length_shorter_than_width_is_not_a_false_doubt():
    """Negative: TSSOP-8 (3.0 along the rows, 4.4 across) - a pad-coverage comparison would flag it."""
    data = _dual_row(4.4, 3.0, x=2.8625, pad_length=1.475, pad_width=0.45, pitch=0.65,
                     family="tssop")
    assert _codes(data) == []


def test_body_too_short_both_ways_is_a_doubt_without_swap():
    """A body shorter than the rows in both directions: no swap can fix it."""
    data = _dual_row(3.5, 3.5)
    body = [d for d in package_doubts(package_from_mapping(data)) if d.code == DOUBT_BODY_PADS]
    assert len(body) == 1 and body[0].swap_clears is False


# Real datasheet dimensions (review 2026-10-15, M3): rows on the left and right,
# so X = E1 across the rows and Y = D along them; lands as KiCad/IPC draw them.
# (label, pins, pitch, land-centre span, land length, land width, E1, D, family)
LEGITIMATE = [
    ("SOIC-8 nominal", 8, 1.27, 5.4, 1.55, 0.6, 3.9, 4.9, "soic"),
    ("SOIC-8 min", 8, 1.27, 5.4, 1.55, 0.6, 3.8, 4.8, "soic"),
    ("SOIC-8 max", 8, 1.27, 5.4, 1.55, 0.6, 4.0, 5.0, "soic"),
    ("SOIC-8 KiCad lands", 8, 1.27, 4.95, 1.95, 0.6, 3.9, 4.9, "soic"),
    ("SOIC-14 min", 14, 1.27, 5.4, 1.55, 0.6, 3.8, 8.55, "soic"),
    ("SOIC-16 min", 16, 1.27, 5.4, 1.55, 0.6, 3.8, 9.8, "soic"),
    ("SOIC-16W min", 16, 1.27, 9.3, 2.0, 0.6, 7.4, 10.1, "soic"),
    ("TSSOP-8 min", 8, 0.65, 5.725, 1.475, 0.45, 4.3, 2.9, "tssop"),
    ("TSSOP-14 min", 14, 0.65, 5.725, 1.475, 0.45, 4.3, 4.9, "tssop"),
    ("TSSOP-16 TI PW nominal", 16, 0.65, 5.8, 1.5, 0.45, 4.4, 5.0, "tssop"),
    ("TSSOP-16 TI PW min", 16, 0.65, 5.8, 1.5, 0.45, 4.3, 4.9, "tssop"),
    ("TSSOP-20 min", 20, 0.65, 5.725, 1.475, 0.45, 4.3, 6.4, "tssop"),
    ("QSOP-16 min", 16, 0.635, 5.4, 1.5, 0.4, 3.8, 4.8, "tssop"),
    ("QSOP-24 min", 24, 0.635, 5.4, 1.5, 0.4, 3.8, 8.55, "tssop"),
    ("SSOP-20 5.3 mm min", 20, 0.65, 7.2, 1.75, 0.45, 5.0, 6.9, "tssop"),
    ("SSOP-28 5.3 mm min", 28, 0.65, 7.2, 1.75, 0.45, 5.0, 9.9, "tssop"),
    ("DIP-8 min", 8, 2.54, 7.62, 1.6, 1.6, 6.1, 9.0, "dip"),
    ("DIP-16 nominal", 16, 2.54, 7.62, 1.6, 1.6, 6.35, 19.18, "dip"),
]


@pytest.mark.parametrize("case", LEGITIMATE, ids=[case[0] for case in LEGITIMATE])
def test_real_packages_at_nominal_and_limit_sizes_are_not_doubts(case):
    """Negative: fine-pitch lands reach the body end at its minimum length and must not be flagged."""
    _label, count, pitch, span, land_l, land_w, e1, d, family = case
    tht = family == "dip"
    data = _dual_row(e1, d, x=span / 2, pad_length=land_l, pad_width=land_w, pitch=pitch,
                     per_side=count // 2, family=family, pad_type="thru_hole" if tht else "smd")
    assert _codes(data) == []


@pytest.mark.parametrize("case", [c for c in LEGITIMATE if c[0] in (
    "SOIC-8 nominal", "SOIC-14 min", "SOIC-16 min", "TSSOP-20 min", "QSOP-24 min", "DIP-16 nominal")],
    ids=lambda case: case[0])
def test_the_same_packages_transposed_are_doubts_with_swap(case):
    """Positive: the same parts with E1 and D written the other way round."""
    _label, count, pitch, span, land_l, land_w, e1, d, family = case
    tht = family == "dip"
    data = _dual_row(d, e1, x=span / 2, pad_length=land_l, pad_width=land_w, pitch=pitch,
                     per_side=count // 2, family=family, pad_type="thru_hole" if tht else "smd")
    doubts = package_doubts(package_from_mapping(data))
    assert [(d.code, d.swap_clears) for d in doubts] == [(DOUBT_BODY_PADS, True)]


def test_lqfp_and_plcc_are_not_doubts():
    """Negative: four-sided packages, gull-wing LQFP-48 7x7 and J-lead PLCC-44."""
    def quad(n_side, pitch, centre, land_l, land_w, body, lead_form=None):
        pins, number = [], 1
        for side in range(4):
            for i in range(n_side):
                off = (i - (n_side - 1) / 2) * pitch
                x, y, length, width = [(-centre, off, land_l, land_w), (off, centre, land_w, land_l),
                                       (centre, -off, land_l, land_w), (-off, -centre, land_w, land_l)][side]
                pins.append(dict(number=str(number), x_mm=x, y_mm=y, pad_length_mm=length,
                                 pad_width_mm=width))
                number += 1
        return dict(family="qfp", body_length_mm=body, body_width_mm=body, source_view="top",
                    pins=pins, lead_form=lead_form)
    assert _codes(quad(12, 0.5, 4.15, 1.5, 0.3, 7.0)) == []
    assert _codes(quad(11, 1.27, 7.8, 1.6, 0.6, 16.5, "j_lead")) == []


def test_overall_width_read_as_body_width_is_its_own_doubt_and_no_swap():
    """M4: E (lead tip to tip) instead of E1 -> the body reaches the lands; swapping would be wrong."""
    for data in (_dual_row(6.0, 4.9),
                 _dual_row(6.4, 3.0, x=2.8625, pad_length=1.475, pad_width=0.45, pitch=0.65,
                           family="tssop")):
        doubts = package_doubts(package_from_mapping(data))
        assert [d.code for d in doubts] == [DOUBT_BODY_OVER_LANDS]
        assert doubts[0].swap_clears is None
        assert "E1" in doubts[0].message


def test_j_leads_under_the_body_edge_are_expected_when_declared():
    """SOJ-26: lands under the body edge. Declared j_lead -> fine; undeclared -> asked, not swapped."""
    soj = dict(x=3.4, pad_length=2.0, pad_width=0.65, pitch=1.27, per_side=13)
    assert _codes(_dual_row(7.62, 17.15, lead_form="j_lead", **soj)) == []
    doubts = package_doubts(package_from_mapping(_dual_row(7.62, 17.15, **soj)))
    assert [d.code for d in doubts] == [DOUBT_BODY_OVER_LANDS]
    assert "J" in doubts[0].message
    # Even a J-lead land never lies entirely under the body.
    assert DOUBT_BODY_OVER_LANDS in _codes(_dual_row(9.5, 17.15, lead_form="j_lead", **soj))


def test_lead_form_is_validated_and_in_the_schema():
    with pytest.raises(ValueError):
        package_from_mapping(_dual_row(3.9, 4.9, lead_form="curly"))
    enum = _schema()["package_geometry"]["properties"]["lead_form"]["enum"]
    assert set(enum) == {"gull_wing", "j_lead", "unknown"}


@pytest.mark.parametrize("family,tht", [("dip", True), ("soic", False)])
def test_two_pins_per_side_numbering_is_judged(family, tht):
    """L6: DIP-4 / SOIC-4 have only corner pins; a mirrored numbering is still a doubt."""
    options = (dict(x=3.81, pad_length=1.6, pad_width=1.6, pitch=2.54, pad_type="thru_hole")
               if tht else dict(x=2.7, pad_length=1.55, pad_width=0.6, pitch=1.27))
    width = 4.6 if tht else 2.5
    length = 6.5 if tht else 3.9
    assert _codes(_dual_row(length, width, per_side=2, family=family, **options)) == []
    assert _codes(_dual_row(length, width, per_side=2, family=family, clockwise=True,
                            **options)) == [DOUBT_PIN_ORDER]


def test_exposed_pad_under_the_body_is_not_a_lead():
    """Negative: a thermal pad in the middle of a SOIC lies under the body by design."""
    pad = dict(number="9", x_mm=0.0, y_mm=0.0, pad_length_mm=2.4, pad_width_mm=3.1, shape="rect")
    assert _codes(_dual_row(3.9, 4.9, extra=[pad])) == []


@pytest.mark.parametrize("family", ["qfn", "unknown", "module", "custom", "sip"])
def test_families_with_terminals_under_the_body_are_not_checked(family):
    """Negative: the same transposed numbers on a bottom-terminated/undeclared family: no evidence."""
    assert DOUBT_BODY_PADS not in _codes(_dual_row(4.9, 3.9, family=family))


def test_transposed_dip_body_is_a_doubt():
    """Through-hole side leads: DIP-8, 7.62 mm rows, body 9.27 x 6.35 written the wrong way round."""
    data = _dual_row(9.27, 6.35, x=3.81, pad_length=1.6, pad_width=1.6, pitch=2.54,
                     family="dip", pad_type="thru_hole")
    body = [d for d in package_doubts(package_from_mapping(data)) if d.code == DOUBT_BODY_PADS]
    assert len(body) == 1 and body[0].swap_clears is True
    data = _dual_row(6.35, 9.27, x=3.81, pad_length=1.6, pad_width=1.6, pitch=2.54,
                     family="dip", pad_type="thru_hole")
    assert _codes(data) == []


@pytest.mark.parametrize("orientation", [0, 90, 30])
def test_the_check_does_not_depend_on_the_footprint_rotation(orientation):
    """Body and pads share the source frame: rotating the footprint changes nothing."""
    assert DOUBT_BODY_PADS in _codes(_dual_row(4.9, 3.9, orientation=orientation))
    assert _codes(_dual_row(3.9, 4.9, orientation=orientation)) == []


# --- pin numbering sense ------------------------------------------------------

def test_clockwise_numbering_seen_from_top_is_a_doubt():
    """A drawing read with Y up turns the JEDEC counter-clockwise order into a clockwise one."""
    assert DOUBT_PIN_ORDER in _codes(_dual_row(3.9, 4.9, clockwise=True))


def test_bottom_view_numbering_is_judged_after_the_reflection():
    """Negative: seen from below the order IS clockwise; the declared bottom view makes it right."""
    assert _codes(_dual_row(3.9, 4.9, clockwise=True, view="bottom")) == []
    # The same coordinates declared top -> doubt; declared bottom AND mirrored -> both doubts.
    codes = _codes(_dual_row(3.9, 4.9, clockwise=True, view="bottom", mirrored=True))
    assert DOUBT_VIEW_MIRROR in codes and DOUBT_PIN_ORDER in codes


def test_non_numeric_pins_are_not_judged():
    """Negative: a grid label such as «A1» has no numbering sense to compare."""
    data = _dual_row(3.9, 4.9, clockwise=True)
    for pin in data["pins"]:
        pin["number"] = "A" + pin["number"]
    assert DOUBT_PIN_ORDER not in _codes(data)


# --- extraction result: exposed, never corrected ------------------------------

def _answer(geometry):
    return dict(layout="matrix", confidence="high", drawing_unit="mm", contacts_view="top",
                package_geometry=geometry)


def test_extraction_exposes_the_doubt_and_keeps_the_values_as_read():
    geometry = _dual_row(4.9, 3.9)
    params = de._params_from_tool_arguments(_answer(copy.deepcopy(geometry)))
    assert params.geometry_doubts and params.geometry_doubts[0]["code"] == DOUBT_BODY_PADS
    assert params.geometry_doubts[0]["swap_clears"] is True
    assert params.geometry_doubts[0]["pins"] == ["1", "8", "4", "5"]
    assert "DUBBIO corpo/pad" in params.notes
    # Never swapped silently.
    assert params.package_geometry["body_length_mm"] == 4.9
    assert params.package_geometry["body_width_mm"] == 3.9


def test_extraction_without_contradictions_has_no_doubts():
    params = de._params_from_tool_arguments(_answer(_dual_row(3.9, 4.9)))
    assert params.geometry_doubts is None
    assert "DUBBIO corpo/pad" not in params.notes


def test_generated_body_still_follows_the_declared_axes():
    """The generator draws length along X: the doubt is what tells the user, not a hidden swap."""
    footprint = build_package(package_from_mapping(_dual_row(3.9, 4.9)), "SOIC8_contract")
    fab = [g for g in footprint.graphics
           if type(g).__name__ == "FpRect" and g.layer == "F.Fab"]
    assert len(fab) == 1
    assert (fab[0].start, fab[0].end) == ((-1.95, -2.45), (1.95, 2.45))


# --- translations -------------------------------------------------------------

def test_every_doubt_message_is_in_the_catalog_in_four_languages():
    sources = [key for key in DEFAULT_TRANSLATIONS
               if key.startswith(("ui:DUBBIO vista: il disegno e' dichiarato",
                                  "ui:DUBBIO corpo/pad", "ui:DUBBIO corpo/piazzole",
                                  "ui:DUBBIO ordine dei pin"))]
    assert len(sources) == 6
    for key in sources:
        entry = DEFAULT_TRANSLATIONS[key]
        assert set(entry) == {"it", "en", "fr", "de"} and entry["it"] == key[3:]
        assert all(entry[language].strip() for language in entry)
    data = _dual_row(6.0, 6.0, clockwise=True, view="top")
    data["source_view"], data["mirrored"] = "bottom", True
    produced = package_doubts(package_from_mapping(data))
    produced += package_doubts(package_from_mapping(_dual_row(4.9, 3.9)))
    produced += package_doubts(package_from_mapping(_dual_row(3.5, 3.5)))
    produced += package_doubts(package_from_mapping(_dual_row(6.0, 4.9, pad_type="thru_hole",
                                                              pad_length=1.6, pad_width=1.6, pitch=2.54)))
    assert {d.code for d in produced} == {DOUBT_VIEW_MIRROR, DOUBT_BODY_PADS, DOUBT_PIN_ORDER,
                                          DOUBT_BODY_OVER_LANDS}
    for doubt in produced:
        assert any(doubt.message.startswith(key[3:].split("{")[0]) for key in sources)
