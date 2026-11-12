"""@brief Per-pad terminal (`lead`) and provenance (`sources`) from the mapping to the footprint pad.

The data contract of `core.tht_estimates` travels: pin mapping -> `PinSpec`
-> `pins_seen_from_top` -> `build_package` -> `Pad` (metadata, never in the
.kicad_mod). A mapping without the two keys (a project saved before
2026-11-10) behaves exactly as before. The defects of the same area fixed
with it: a hole on an SMD pad, a null pin number, a circle turned into an
oval by float noise.
"""
from dataclasses import asdict, replace
import itertools
import json

import pytest

from kicadgen.core import footprint as footprint_module
from kicadgen.core.footprint import Pad
from kicadgen.core.package_geometry import (PackageGeometry, PinSpec, mark_user_edited, mark_user_verified,
                                            package_from_mapping)
from kicadgen.generators.package import (ESTIMATED_PADS_LISTED, ESTIMATED_PADS_MORE, ESTIMATED_PADS_NOTE,
                                         build_package, estimated_pad_list)

ROUND = {"shape": "round", "diameter_mm": 0.6}
FLAT = {"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4}


def _pin(number, x, *, lead=None, sources=None, drill=0.85, pad=1.65, shape="circle"):
    """One through-hole pin mapping at (x, 0); `lead` and `sources` only when given."""
    mapping = {"number": number, "x_mm": x, "y_mm": 0.0, "pad_length_mm": pad, "pad_width_mm": pad,
               "pad_type": "thru_hole", "shape": shape, "drill_mm": drill}
    if lead is not None:
        mapping["lead"] = lead
    if sources is not None:
        mapping["sources"] = sources
    return mapping


def _package(*pins, **fields):
    return {"name": "PART", "family": "custom", "body_length_mm": 12.0, "body_width_mm": 6.0,
            "pins": list(pins), **fields}


def _render(geometry, monkeypatch):
    """The .kicad_mod text with deterministic UUIDs, so two builds compare byte for byte."""
    counter = itertools.count()
    monkeypatch.setattr(footprint_module, "_uuid", lambda: f"00000000-0000-0000-0000-{next(counter):012d}")
    return build_package(geometry).render()


# ── the contract travels ───────────────────────────────────────────────────

def test_lead_and_sources_reach_the_pinspec_the_top_view_and_the_pad():
    sources = {"drill_mm": "derived", "pad_size": "derived", "shape": "estimated", "lead": "read"}
    geometry = package_from_mapping(_package(
        _pin("1", -3.0, lead=ROUND, sources=sources),
        _pin("2", 3.0, lead=FLAT, sources={"drill_mm": "read"}, drill=1.35, pad=2.15),
        source_view="bottom"))
    assert [pin.lead for pin in geometry.pins] == [ROUND, FLAT]
    assert geometry.pins[0].sources == sources
    # A bottom view mirrors positions, never what a pad is made of.
    top = geometry.pins_seen_from_top()
    assert [(pin.number, pin.lead, pin.sources) for pin in top] == [
        ("1", ROUND, sources), ("2", FLAT, {"drill_mm": "read"})]
    pads = build_package(geometry).pads
    assert [(pad.number, pad.lead, pad.sources) for pad in pads] == [
        ("1", ROUND, sources), ("2", FLAT, {"drill_mm": "read"})]


def test_the_pin_keeps_private_copies_of_the_given_mappings():
    lead, sources = dict(ROUND), {"drill_mm": "estimated"}
    pin = PinSpec("1", 0, 0, 1.65, 1.65, pad_type="thru_hole", shape="circle", drill_mm=0.85,
                  lead=lead, sources=sources)
    lead["diameter_mm"] = 9.0
    sources["drill_mm"] = "read"
    assert pin.lead == ROUND and pin.sources == {"drill_mm": "estimated"}


def test_a_geometry_with_provenance_survives_asdict_json_and_back():
    geometry = package_from_mapping(_package(
        _pin("1", -3.0, lead=ROUND, sources={"drill_mm": "derived"}),
        _pin("2", 3.0, lead=FLAT, drill=1.35, pad=2.15)))
    saved = json.loads(json.dumps(asdict(geometry)))
    assert saved["pins"][0]["lead"] == ROUND and saved["pins"][0]["sources"] == {"drill_mm": "derived"}
    assert saved["pins"][1]["sources"] is None
    assert package_from_mapping(saved) == geometry


def test_pins_with_a_lead_stay_hashable_and_equal_pins_hash_alike():
    first = PinSpec("1", 0, 0, 1.65, 1.65, pad_type="thru_hole", shape="circle", drill_mm=0.85, lead=ROUND)
    twin = PinSpec("1", 0, 0, 1.65, 1.65, pad_type="thru_hole", shape="circle", drill_mm=0.85, lead=dict(ROUND))
    other = replace(first, lead=FLAT)
    assert first == twin and hash(first) == hash(twin) and len({first, twin}) == 1
    assert first != other and len({first, other}) == 2


@pytest.mark.parametrize("extra", [
    {"lead": {"shape": "hexagonal", "diameter_mm": 0.6}},
    {"lead": {"shape": "round", "diameter_mm": -0.6}},
    {"lead": {"shape": "round", "diameter_mm": 0.6, "note": "x"}},
    {"sources": {"drill_mm": "guessed"}},
    {"sources": {"hole": "read"}},
])
def test_a_lead_or_a_provenance_out_of_contract_is_refused_with_the_pin_named(extra):
    with pytest.raises(ValueError, match="pin 7"):
        package_from_mapping(_package({**_pin("7", 0.0), **extra}))


# ── a project saved before the provenance existed ──────────────────────────

def test_an_old_mapping_round_trips_unchanged_and_writes_the_same_file(monkeypatch):
    old = _package(_pin("1", -3.0), _pin("2", 3.0, drill=1.35, pad=2.15))
    geometry = package_from_mapping(json.loads(json.dumps(old)))
    assert all(pin.lead is None and pin.sources is None for pin in geometry.pins)
    again = package_from_mapping(json.loads(json.dumps(asdict(geometry))))
    assert again == geometry
    # Every key the old mapping held comes back with its value.
    for before, after in zip(old["pins"], asdict(again)["pins"]):
        assert {key: after[key] for key in before} == before
    # Explicit nulls (what asdict writes) are the same as absent keys.
    nulls = _package(*({**pin, "lead": None, "sources": None} for pin in old["pins"]))
    assert package_from_mapping(nulls) == geometry
    plain = _render(geometry, monkeypatch)
    assert _render(again, monkeypatch) == plain
    assert all(pad.lead is None and pad.sources is None for pad in build_package(geometry).pads)
    assert "stimat" not in plain


def test_lead_and_sources_never_change_the_pad_nodes_of_the_file(monkeypatch):
    plain = package_from_mapping(_package(_pin("1", -3.0), _pin("2", 3.0, drill=1.35, pad=2.15)))
    # Read values and a declared lead: metadata only, the file is the same byte for byte.
    read = package_from_mapping(_package(
        _pin("1", -3.0, lead=ROUND, sources={"drill_mm": "read", "pad_size": "read", "shape": "read"}),
        _pin("2", 3.0, lead=FLAT, drill=1.35, pad=2.15)))
    assert _render(read, monkeypatch) == _render(plain, monkeypatch)
    # Estimated values: only the description says so; no new node, no new pad field.
    marked = package_from_mapping(_package(
        _pin("1", -3.0, lead=ROUND, sources={"drill_mm": "derived", "pad_size": "derived"}),
        _pin("2", 3.0, lead=FLAT, drill=1.35, pad=2.15)))
    plain_text, marked_text = _render(plain, monkeypatch), _render(marked, monkeypatch)
    note = "; " + ESTIMATED_PADS_NOTE.format(numbers="1")
    assert note in marked_text and marked_text.replace(note, "") == plain_text
    for word in ("lead", "sources", "derived", "diameter_mm"):
        assert word not in marked_text


def test_the_description_names_only_the_pads_with_unverified_values():
    geometry = package_from_mapping(_package(
        _pin("1", -3.0, sources={"drill_mm": "estimated"}),
        _pin("2", 0.0, sources={"drill_mm": "read", "lead": "estimated"}),   # the pad values are read
        _pin("3", 3.0, sources={"shape": "derived"})))
    assert build_package(geometry).description.endswith(ESTIMATED_PADS_NOTE.format(numbers="1, 3"))


def test_the_note_compresses_runs_skips_blank_numbers_and_stays_bounded():
    """Review 2026-11-11, K7: a part with many estimated pads wrote every number, blanks included."""
    # Runs of three or more integers become ranges; two in a row stay listed.
    assert estimated_pad_list(["1", "2", "3", "4", "6", "7", "9"]) == "1-4, 6, 7, 9"
    # Text numbers are never ranged; a repeated number (split pad) is named once.
    assert estimated_pad_list(["A1", "A2", "A3", "EP", "EP"]) == "A1, A2, A3, EP"
    # Blank numbers are counted, never written as empty entries.
    assert estimated_pad_list(["1", "", " ", "5"]) == "1, 5, 2 senza numero"
    assert estimated_pad_list(["", ""]) == "2 senza numero"
    assert estimated_pad_list([]) == ""
    # Beyond ESTIMATED_PADS_LISTED entries the rest is a count of pads.
    scattered = [str(n) for n in range(1, 40, 2)]          # 20 isolated numbers
    words = estimated_pad_list(scattered)
    assert words.count(",") == ESTIMATED_PADS_LISTED - 1
    assert words.endswith(ESTIMATED_PADS_MORE.format(count=20 - ESTIMATED_PADS_LISTED))
    # A long contiguous run stays one entry, whatever its length.
    assert estimated_pad_list([str(n) for n in range(1, 201)]) == "1-200"


def test_the_description_of_a_package_names_its_unnumbered_estimated_holes_by_count():
    mechanical = {"number": "", "x_mm": 0.0, "y_mm": 3.0, "pad_length_mm": 1.5, "pad_width_mm": 1.5,
                  "pad_type": "np_thru_hole", "shape": "circle", "drill_mm": 1.0,
                  "sources": {"drill_mm": "estimated"}}
    geometry = package_from_mapping(_package(_pin("1", -3.0, sources={"drill_mm": "estimated"}),
                                             _pin("2", 3.0), mechanical))
    description = build_package(geometry).description
    assert description.endswith(ESTIMATED_PADS_NOTE.format(numbers="1, 1 senza numero"))
    assert "pad , " not in description and ", ," not in description


# ── a pad edited by hand stops being an estimate ───────────────────────────

def _estimated_pin():
    return PinSpec("1", 0, 0, 1.65, 1.65, pad_type="thru_hole", shape="circle", drill_mm=0.85, lead=ROUND,
                   sources={"drill_mm": "derived", "pad_size": "derived", "shape": "estimated", "lead": "read"})


def test_only_the_values_the_user_changed_become_read():
    pin = _estimated_pin()
    drilled = mark_user_edited(pin, replace(pin, drill_mm=0.9))
    assert drilled.sources == {"drill_mm": "read", "pad_size": "derived", "shape": "estimated", "lead": "read"}
    sized = mark_user_edited(pin, replace(pin, pad_length_mm=2.0, pad_width_mm=2.0))
    assert sized.sources["pad_size"] == "read" and sized.sources["drill_mm"] == "derived"
    slotted = mark_user_edited(pin, replace(pin, drill_length_mm=1.2))
    assert slotted.sources["drill_mm"] == "read"
    reshaped = mark_user_edited(pin, replace(pin, shape="rect"))
    assert reshaped.sources["shape"] == "read" and reshaped.sources["pad_size"] == "derived"


def test_a_move_a_rotation_or_an_unchanged_value_keeps_every_mark():
    pin = _estimated_pin()
    moved = replace(pin, x_mm=1.0, rotation_deg=90.0)
    assert mark_user_edited(pin, moved) is moved and moved.sources == pin.sources
    retyped = replace(pin, drill_mm=0.85 + 1e-12)        # the same number in the file
    assert mark_user_edited(pin, retyped).sources["drill_mm"] == "derived"


def test_a_pad_declared_verified_turns_its_unverified_values_into_read_and_keeps_the_lead():
    """Review 2026-11-11, K5: the user's explicit «Valori verificati sul disegno»."""
    pin = _estimated_pin()
    verified = mark_user_verified(pin)
    assert verified.sources == {"drill_mm": "read", "pad_size": "read", "shape": "read", "lead": "read"}
    assert (verified.drill_mm, verified.pad_length_mm, verified.shape) == (0.85, 1.65, "circle")
    estimated_lead = replace(pin, sources={**pin.sources, "lead": "estimated"})
    assert mark_user_verified(estimated_lead).sources["lead"] == "estimated"


def test_a_pad_with_nothing_unverified_is_returned_unchanged():
    read = replace(_estimated_pin(), sources={"drill_mm": "read", "lead": "estimated"})
    assert mark_user_verified(read) is read
    old = PinSpec("1", 0, 0, 1.65, 1.65, pad_type="thru_hole", shape="circle", drill_mm=0.85)
    assert mark_user_verified(old) is old and old.sources is None


def test_a_pad_made_smd_drops_the_hole_provenance_and_an_old_pad_gains_none():
    pin = _estimated_pin()
    smd = mark_user_edited(pin, replace(pin, pad_type="smd", drill_mm=None))
    assert "drill_mm" not in smd.sources and smd.sources["pad_size"] == "derived"
    old = PinSpec("1", 0, 0, 1.65, 1.65, pad_type="thru_hole", shape="circle", drill_mm=0.85)
    edited = replace(old, drill_mm=1.0)
    assert mark_user_edited(old, edited) is edited and edited.sources is None


# ── defects of the same area ───────────────────────────────────────────────

@pytest.mark.parametrize("pad_type", [None, "smd", "connect"])
def test_a_hole_on_a_pad_type_without_hole_is_refused_not_dropped(pad_type):
    # No pad_type in the mapping means "smd": the hole used to vanish in silence.
    pin = {"number": "1", "x_mm": 0.0, "y_mm": 0.0, "pad_length_mm": 1.7, "pad_width_mm": 1.7, "drill_mm": 1.0}
    if pad_type is not None:
        pin["pad_type"] = pad_type
    with pytest.raises(ValueError, match="drill_mm"):
        package_from_mapping(_package(pin))


def test_a_holed_type_keeps_its_hole_and_a_null_drill_on_smd_is_no_hole():
    holed = package_from_mapping(_package(_pin("1", 0.0, drill=1.0, pad=1.7)))
    assert build_package(holed).pads[0].drill == 1.0
    smd = {"number": "1", "x_mm": 0.0, "y_mm": 0.0, "pad_length_mm": 1.7, "pad_width_mm": 1.7,
           "pad_type": "smd", "drill_mm": None}
    assert build_package(package_from_mapping(_package(smd))).pads[0].drill is None


def test_a_null_pin_number_is_refused_and_a_textual_one_is_kept():
    with pytest.raises(ValueError):
        package_from_mapping(_package({**_pin("1", 0.0), "number": None}))
    with pytest.raises(ValueError):
        PackageGeometry(pins=[PinSpec(None, 0, 0, 1, 1)]).validate()
    # «None» typed as text is a legitimate (if odd) pin name: only the missing value is refused.
    kept = package_from_mapping(_package({**_pin("A1", 0.0)}, {**_pin("None", 4.0)}))
    assert [pin.number for pin in kept.pins] == ["A1", "None"]


def test_float_noise_between_the_two_sizes_keeps_a_circle_a_real_difference_makes_an_oval():
    noisy = 1.7 + 2e-16 * 4          # what a unit conversion leaves behind
    assert noisy != 1.7
    assert PinSpec("1", 0, 0, 1.7, noisy, shape="circle").shape == "circle"
    assert PinSpec("1", 0, 0, 1.7, 1.700001, shape="circle").shape == "oval"   # 1 nm: a different number in the file
    assert PinSpec("1", 0, 0, 2.1, 1.7, shape="circle").shape == "oval"
    pad = Pad(number="1", pad_type="smd", shape="circle", at=(0, 0), size=(1.7, noisy), layers=["F.Cu"])
    assert pad.shape == "circle"
    assert Pad(number="1", pad_type="smd", shape="circle", at=(0, 0), size=(2.1, 1.7), layers=["F.Cu"]).shape == "oval"
