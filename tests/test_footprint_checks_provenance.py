"""@brief Checks that read the pad's terminal and provenance: hole_vs_lead, estimated_pad, per-axis annular ring.

Positive and negative cases differ for a structural reason: the same hole
lets a round lead pass and stops a flat blade of the same width (diagonal);
the same pad is listed when its hole is derived and not when it is read; a
pad without lead or provenance (an old project) yields nothing new.
"""
import pytest

from kicadgen.core.footprint import Footprint, Pad
from kicadgen.core.package_geometry import package_from_mapping
from kicadgen.generators.package import build_package
from kicadgen.services import footprint_checks as fc

THT = ["*.Cu", "*.Mask"]
ROUND = {"shape": "round", "diameter_mm": 0.6}
FLAT = {"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4}


def _fp(*pads):
    fp = Footprint(name="T", attr="through_hole")
    for pad in pads:
        fp.add_pad(pad)
    return fp


def _tht(number, x, *, size=(2.2, 2.2), drill=None, slot=None, lead=None, sources=None, shape="circle",
         pad_type="thru_hole"):
    return Pad(number=number, pad_type=pad_type, shape=shape, at=(x, 0), size=size, layers=THT, drill=drill,
               drill_slot=slot, lead=lead, sources=sources)


# ── hole_vs_lead ───────────────────────────────────────────────────────────

def test_a_hole_the_declared_lead_cannot_enter_is_an_error_on_that_pad():
    tight = _tht("1", 0, drill=0.6, lead=ROUND)            # a hole of the lead's own diameter
    fits = _tht("2", 5, drill=0.85, lead=ROUND)
    findings = fc.check_hole_vs_lead(_fp(tight, fits))
    assert [(f.code, f.severity, f.pads) for f in findings] == [("hole_vs_lead", "error", (0,))]
    assert "Ø0.6 mm" in findings[0].message and findings[0].hint


def test_the_same_round_hole_passes_a_round_lead_and_stops_a_blade_of_that_width():
    # Ø1.05 clears a Ø1.0 pin and the blade's 1.0 mm width, not its 1.077 mm diagonal.
    pin = _tht("1", 0, drill=1.05, lead={"shape": "round", "diameter_mm": 1.0})
    blade = _tht("2", 5, drill=1.05, lead=FLAT)
    findings = fc.check_hole_vs_lead(_fp(pin, blade))
    assert [f.pads for f in findings] == [(1,)]
    assert "1 x 0.4 mm" in findings[0].message and "Ø1.05 mm" in findings[0].message


def test_a_slot_is_judged_on_its_own_axes_and_named_with_both_sides():
    fits = _tht("1", 0, size=(1.4, 2.3), slot=(0.6, 1.5), lead=FLAT, shape="oval")
    narrow = _tht("2", 5, size=(1.2, 2.3), slot=(0.4, 1.5), lead=FLAT, shape="oval")
    findings = fc.check_hole_vs_lead(_fp(fits, narrow))
    assert [f.pads for f in findings] == [(1,)] and "0.4 x 1.5 mm" in findings[0].message


def test_a_pad_without_a_lead_or_without_a_hole_is_never_judged():
    # The tight hole of the first test, with nothing declared to pass through it.
    no_lead = _tht("1", 0, drill=0.6)
    smd = Pad(number="2", pad_type="smd", shape="rect", at=(5, 0), size=(1, 1), layers=["F.Cu"], lead=ROUND)
    assert fc.check_hole_vs_lead(_fp(no_lead, smd)) == []


def test_the_hint_says_a_misread_lead_needs_a_new_analysis_not_a_dialog():
    """Review 2026-11-11, K8: no dialog edits `PinSpec.lead`, so the remedy must not ask to correct it by hand."""
    hint = fc.check_hole_vs_lead(_fp(_tht("1", 0, drill=0.6, lead=ROUND)))[0].hint
    assert "nessun dialog lo modifica" in hint and "nuova analisi dei documenti" in hint
    assert "correggilo sul disegno" not in hint
    # Negative twin: a value the pad dialog does edit keeps a dialog remedy, no analysis.
    ring = fc.check_annular_ring(_fp(_tht("1", 0, size=(1.0, 1.0), drill=0.8)))[0].hint
    assert "dialog del pad" in ring and "analisi" not in ring
    # Translated in the four languages, with the same meaning.
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    entry = DEFAULT_TRANSLATIONS["ui:" + fc.GEOMETRY_HINTS["hole_vs_lead"]]
    assert set(entry) == {"it", "en", "fr", "de"} and "new analysis" in entry["en"]


def test_a_mechanical_hole_carrying_a_lead_is_judged_like_a_plated_one():
    peg = _tht("", 0, drill=0.6, lead=ROUND, pad_type="np_thru_hole", size=(0.6, 0.6))
    assert [f.pads for f in fc.check_hole_vs_lead(_fp(peg))] == [(0,)]


# ── estimated_pad ──────────────────────────────────────────────────────────

def test_one_warning_lists_the_pads_with_derived_or_estimated_values_and_which():
    derived = _tht("1", 0, drill=0.85, sources={"drill_mm": "derived", "pad_size": "derived", "shape": "read"})
    read = _tht("2", 5, drill=0.85, sources={"drill_mm": "read", "pad_size": "read", "shape": "read"})
    guessed = _tht("3", 10, drill=0.85, sources={"shape": "estimated"})
    findings = fc.check_estimated_pads(_fp(derived, read, guessed))
    assert [(f.code, f.severity, f.pads) for f in findings] == [("estimated_pad", "warning", (0, 2))]
    message = findings[0].message
    assert "1 (foro, dimensione)" in message and "3 (forma)" in message and "2 (" not in message
    assert findings[0].hint


def test_the_estimated_pad_hint_names_both_ways_out_in_four_languages():
    """Review 2026-11-11, K5: typed values, or the dialog's «Valori verificati sul disegno»."""
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    hint = fc.GEOMETRY_HINTS["estimated_pad"]
    assert "Valori verificati sul disegno" in hint and "scrivi i valori letti" in hint
    entry = DEFAULT_TRANSLATIONS["ui:" + hint]
    labels = DEFAULT_TRANSLATIONS["ui:Valori verificati sul disegno"]
    # Each language's hint names the checkbox exactly as that language labels it.
    assert all(labels[lang] in entry[lang] for lang in ("it", "en", "fr", "de"))


def test_read_values_an_estimated_lead_alone_or_no_provenance_are_not_listed():
    read = _tht("1", 0, drill=0.85, sources={"drill_mm": "read"})
    lead_only = _tht("2", 5, drill=0.85, lead=ROUND, sources={"lead": "estimated"})
    old = _tht("3", 10, drill=0.85)
    assert fc.check_estimated_pads(_fp(read, lead_only, old)) == []


def test_a_generated_package_reports_its_estimates_and_an_old_one_reports_nothing_new():
    def pin(number, x, **extra):
        return {"number": number, "x_mm": x, "y_mm": 0.0, "pad_length_mm": 1.65, "pad_width_mm": 1.65,
                "pad_type": "thru_hole", "shape": "circle", "drill_mm": 0.85, **extra}

    def codes(*pins):
        geometry = package_from_mapping({"name": "P", "family": "custom", "body_length_mm": 12.0,
                                         "body_width_mm": 6.0, "pins": list(pins)})
        return [f.code for f in fc.check_footprint_geometry(build_package(geometry))]

    old = codes(pin("1", -3.0), pin("2", 3.0))
    assert "estimated_pad" not in old and "hole_vs_lead" not in old
    new = codes(pin("1", -3.0, lead=ROUND, sources={"drill_mm": "derived", "pad_size": "derived"}),
                pin("2", 3.0, lead=FLAT, sources={"drill_mm": "read"}))
    # The 0.85 mm hole takes the Ø0.6 pin, not the 1 x 0.4 blade; the error comes first.
    assert new[0] == "hole_vs_lead" and new.count("hole_vs_lead") == 1 and new.count("estimated_pad") == 1
    # Nothing else appears or disappears because of the metadata.
    assert [code for code in new if code not in ("hole_vs_lead", "estimated_pad")] == old


# ── annular_ring per axis ──────────────────────────────────────────────────

def test_a_non_round_pad_names_both_rings_and_their_axes():
    # Oval 2.1 x 1.7 with a 1.3 hole: 0.4 mm along the length, 0.2 mm along the width.
    oval = _tht("1", 0, size=(2.1, 1.7), drill=1.3, shape="oval")
    assert fc.annular_rings(oval) == pytest.approx((0.4, 0.2)) and fc.annular_ring(oval) == pytest.approx(0.2)
    findings = fc.check_annular_ring(_fp(oval), minimum=0.25)
    assert [f.pads for f in findings] == [(0,)]
    message = findings[0].message
    assert "0.20 mm lungo la larghezza del pad (1.70 mm)" in message
    assert "0.40 mm lungo la lunghezza (2.10 mm)" in message


def test_the_thin_axis_is_named_first_whichever_it_is():
    turned = _tht("1", 0, size=(1.7, 2.1), drill=1.3, shape="oval")
    message = fc.check_annular_ring(_fp(turned), minimum=0.25)[0].message
    assert "0.20 mm lungo la lunghezza del pad (1.70 mm)" in message
    assert "0.40 mm lungo la larghezza (2.10 mm)" in message


def test_a_round_pad_keeps_the_single_ring_message_and_a_wide_oval_passes():
    round_pad = _tht("1", 0, size=(1.0, 1.0), drill=0.8)
    message = fc.check_annular_ring(_fp(round_pad))[0].message
    assert "0.10 mm attorno al foro" in message and "lungo" not in message
    # The oval of the first test at the default minimum (0.15): 0.2 mm is enough.
    assert fc.check_annular_ring(_fp(_tht("1", 0, size=(2.1, 1.7), drill=1.3, shape="oval"))) == []
    assert fc.annular_rings(_tht("", 0, size=(1.0, 1.0), drill=1.0, pad_type="np_thru_hole")) is None
