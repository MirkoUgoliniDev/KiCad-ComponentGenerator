"""@brief «Verifica componente» runs the footprint copper rule on the .kicad_mod on disk (2026-11-14).

@details Real run of an RS485 module: 16 mm pads whose copper overlapped
between two pads got «UTILIZZABILE CON RISERVE», because the component check
ran no footprint geometry. The check now runs the footprint tab's own rule
(`footprint_checks.check_pads_overlap`) on the file: an overlap is an ERROR
of importance 1 (the dialog's «NON USARE») with the rule's remedy; a clean
footprint gives an OK and no error. Pads sharing one number (a split pad) are
one pin and may overlap. Offline, no KiCad needed.
"""

from dataclasses import replace

from kicadgen.services import component_check
from kicadgen.services.component_check import (ACTION_EDIT_PINOUT, IMPORTANCE_BLOCKS_USE,
                                               footprint_geometry_checks, lead_hole_checks,
                                               pin_role_checks, verify_component)
from tests.test_footprint_draft import project  # noqa: F401 - pytest fixture


def _pad(number, x, size, kind="thru_hole", extra=""):
    """One round plated pad of the file, at (x, 0) mm."""
    drill = "(drill 1)" if kind == "thru_hole" else ""
    layers = '"*.Cu" "*.Mask"' if kind == "thru_hole" else '"F.Cu" "F.Mask"'
    return (f'  (pad "{number}" {kind} circle (at {x} 0) (size {size} {size}) {drill} '
            f'(layers {layers}){extra})\n')


def _footprint(*pads):
    """A minimal .kicad_mod text with `pads`."""
    return '(footprint "X"\n  (layer "F.Cu")\n' + "".join(pads) + ")\n"


def test_overlapping_pads_are_an_error_that_blocks_the_use():
    """@brief Positive: 16 mm pads 10 mm apart overlap -> ERROR, importance 1, both pads, a remedy."""
    results = footprint_geometry_checks(_footprint(_pad("2", 0, 16), _pad("5", 10, 16)))
    assert [r.status for r in results] == ["ERROR"]
    error = results[0]
    assert error.importance == IMPORTANCE_BLOCKS_USE and error.code == "footprint_pads_overlap"
    assert error.pins == ("2", "5") and error.remedy


def test_a_clean_footprint_and_a_split_pad_give_no_error():
    """@brief Negative: pads apart, or two pieces of ONE pin overlapping -> OK only."""
    clean = footprint_geometry_checks(_footprint(_pad("1", 0, 1.6), _pad("2", 2.54, 1.6)))
    assert [r.status for r in clean] == ["OK"]
    # CONTRACT CHANGED 2026-11-14 (third review M2): the pieces of one pin
    # differ in size; two same-size copies of one pad are a double reading.
    split = footprint_geometry_checks(_footprint(_pad("3", 0, 2), _pad("3", 1, 3)))
    assert [r.status for r in split] == ["OK"]


def test_a_drill_offset_moves_the_copper_that_is_checked():
    """@brief Positive: two holes 4 mm apart, copper shifted onto each other -> overlap; negative: no shift."""
    shifted = _footprint(_pad("1", 0, 2).replace("(drill 1)", "(drill 1 (offset 1 0))"),
                         _pad("2", 4, 2).replace("(drill 1)", "(drill 1 (offset -1 0))"))
    assert [r.status for r in footprint_geometry_checks(shifted)] == ["ERROR"]
    assert [r.status for r in footprint_geometry_checks(
        _footprint(_pad("1", 0, 2), _pad("2", 4, 2)))] == ["OK"]


def test_verify_component_reports_the_overlap(tmp_path, monkeypatch):
    """@brief The check on disk carries the finding; a clean file carries none."""
    monkeypatch.setattr(component_check, "find_kicad_cli", lambda: None)
    folder = tmp_path / "X.pretty"
    folder.mkdir()
    rows = [{"pin": "2", "name": "A"}, {"pin": "5", "name": "B"}]
    (folder / "X.kicad_mod").write_text(_footprint(_pad("2", 0, 16), _pad("5", 10, 16)),
                                        encoding="utf-8")
    results = verify_component(tmp_path, "X", rows)
    overlaps = [r for r in results if r.code == "footprint_pads_overlap"]
    assert len(overlaps) == 1 and overlaps[0].status == "ERROR"
    (folder / "X.kicad_mod").write_text(_footprint(_pad("2", 0, 1.6), _pad("5", 10, 1.6)),
                                        encoding="utf-8")
    results = verify_component(tmp_path, "X", rows)
    assert not [r for r in results if r.code == "footprint_pads_overlap"]
    assert [r.status for r in results if r.code == "footprint_geometry"] == ["OK"]


# ── Pin names against the declared switch functions (night run 2026-11-14) ──

def test_a_pin_named_after_another_switch_role_is_an_error():
    """@brief Positive: «NC» declared NO, «N.O.» declared NC, «COM1» declared NC -> one error each."""
    rows = [{"pin": "3", "name": "NC", "function": "no"},
            {"pin": "4", "name": "N.O.", "function": "nc"},
            {"pin": "5", "name": "COM1", "function": "nc"}]
    results = pin_role_checks(rows)
    assert [r.pins for r in results] == [("3",), ("4",), ("5",)]
    assert all(r.status == "ERROR" and r.importance == IMPORTANCE_BLOCKS_USE
               and r.action == ACTION_EDIT_PINOUT and r.remedy for r in results)


def test_the_shared_vocabulary_reads_na_nf_and_long_forms():
    """@brief Positive: «NA» (Italian NO), «NF» (French NC), «Normally Closed 2» against other roles.

    The check uses `core.contact_roles`, the extraction's own vocabulary:
    one list of role words for both.
    """
    rows = [{"pin": "1", "name": "NA", "function": "nc"},
            {"pin": "2", "name": "NF", "function": "no"},
            {"pin": "3", "name": "Normally Closed 2", "function": "common"}]
    assert [r.pins for r in pin_role_checks(rows)] == [("1",), ("2",), ("3",)]
    rows = [{"pin": "1", "name": "NA", "function": "no"}, {"pin": "2", "name": "NF", "function": "nc"}]
    assert pin_role_checks(rows) == []


def test_names_that_agree_or_are_no_switch_pin_give_nothing():
    """@brief Negative: agreeing names, an IC pin «NC» (not connected), a coil, free names -> nothing."""
    rows = [{"pin": "1", "name": "NC1", "function": "nc"},
            {"pin": "2", "name": "NO", "function": "no"},
            {"pin": "3", "name": "COMMON", "function": "common"},
            {"pin": "4", "name": "NC"},                                  # no function: not connected
            {"pin": "5", "name": "NC", "function": "coil"},
            {"pin": "6", "name": "OUT_A", "function": "no"}]
    assert pin_role_checks(rows) == []


# ── Declared terminals against the holes on disk ─────────────────────────

ROUND_06 = {"shape": "round", "diameter_mm": 0.6}
BLADE = {"shape": "flat", "width_mm": 0.8, "thickness_mm": 0.6}       # diagonal 1.0 mm


def test_terminals_that_do_not_fit_their_holes_are_an_error():
    """@brief Positive: a 0.6 mm pin in a 0.5 mm hole, a blade (diagonal 1.0) in a 1.0 mm hole."""
    text = _footprint(_pad("1", 0, 1.6).replace("(drill 1)", "(drill 0.5)"),
                      _pad("2", 2.54, 1.6), _pad("3", 5.08, 1.6))
    results = lead_hole_checks(text, {"1": ROUND_06, "2": BLADE, "3": ROUND_06})
    assert [r.status for r in results] == ["ERROR"]
    assert results[0].pins == ("1", "2") and results[0].importance == IMPORTANCE_BLOCKS_USE
    assert "Piazzole del componente THT" in results[0].remedy


def test_terminals_that_fit_give_an_ok_row_and_undeclared_ones_nothing():
    """@brief Negative: a 0.6 mm pin in a 1 mm hole and a blade in a slot -> one OK row; nothing declared -> nothing.

    CONTRACT CHANGED 2026-11-14 (real run, SRD relay): terminals that all
    fit gave no row at all, so the user could not tell the check had run;
    they now give one OK row with the number of pads checked.
    """
    slot = _pad("2", 2.54, 1.6).replace("(drill 1)", "(drill oval 1.2 0.9)")
    text = _footprint(_pad("1", 0, 1.6), slot)
    results = lead_hole_checks(text, {"1": ROUND_06, "2": BLADE})
    assert [(r.status, r.code) for r in results] == [("OK", "lead_hole")] and "2" in results[0].detail
    assert lead_hole_checks(text, {}) == [] and lead_hole_checks(text, None) == []


#: The pads of the real SRD relay run (project saved 2026-11-14): lead and drill as applied.
SRD_PADS = {"1": ({"shape": "round", "diameter_mm": 0.6}, 0.85),
            "2": ({"shape": "round", "diameter_mm": 0.6}, 0.85),
            "3": ({"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4}, 1.35),
            "4": ({"shape": "flat", "width_mm": 1.1, "thickness_mm": 0.3}, 1.4),
            "5": ({"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4}, 1.35)}


def _srd_footprint(drills):
    """The SRD footprint with the given {pad: drill} (round holes)."""
    return _footprint(*(_pad(number, index * 3.0, 2.2).replace("(drill 1)", f"(drill {drill})")
                        for index, (number, drill) in enumerate(drills.items())))


def test_the_real_srd_pads_all_fit_and_say_so():
    """@brief Real shape: entries as the window builds them -> one OK row for the 5 pads."""
    leads = {number: {"lead": lead, "fit": None, "source": "estimated"}
             for number, (lead, _drill) in SRD_PADS.items()}
    results = lead_hole_checks(_srd_footprint({n: d for n, (_l, d) in SRD_PADS.items()}), leads)
    assert [r.status for r in results] == ["OK"] and "5" in results[0].detail


def test_a_blade_in_a_small_hole_is_an_error_unless_press_fit_or_contradicted():
    """@brief The 1 x 0.4 blade (diagonal 1.08) in a 0.85 hole: ERROR; press-fit: skipped;
    a lead the user's hole contradicts: WARN importance 2."""
    blade = SRD_PADS["3"][0]
    text = _srd_footprint({"1": 0.85, "2": 0.85})
    error = lead_hole_checks(text, {"2": {"lead": blade, "fit": None, "source": "read"}})
    assert [(r.status, r.importance, r.pins) for r in error] == [("ERROR", IMPORTANCE_BLOCKS_USE, ("2",))]
    assert lead_hole_checks(text, {"2": {"lead": blade, "fit": "press_fit", "source": "read"}}) == []
    warn = lead_hole_checks(text, {"2": {"lead": blade, "fit": None, "source": "contradicted"}})
    assert [(r.status, r.importance, r.code) for r in warn] == [
        ("WARN", component_check.IMPORTANCE_BEFORE_PRODUCTION, "lead_hole_contradicted")]
    assert warn[0].remedy


def test_verify_component_runs_both_checks(tmp_path, monkeypatch):
    """@brief The check on disk carries the pin-role and the lead-hole errors; clean inputs carry none."""
    monkeypatch.setattr(component_check, "find_kicad_cli", lambda: None)
    folder = tmp_path / "X.pretty"
    folder.mkdir()
    (folder / "X.kicad_mod").write_text(
        _footprint(_pad("1", 0, 1.6).replace("(drill 1)", "(drill 0.5)"), _pad("2", 2.54, 1.6)),
        encoding="utf-8")
    rows = [{"pin": "1", "name": "NC", "function": "no"}, {"pin": "2", "name": "NO", "function": "no"}]
    codes = [r.code for r in verify_component(tmp_path, "X", rows, pad_leads={"1": ROUND_06})
             if r.status == "ERROR"]
    assert "pin_role_contradiction" in codes and "lead_hole" in codes
    rows[0]["name"] = "NO2"
    codes = [r.code for r in verify_component(tmp_path, "X", rows, pad_leads={"2": ROUND_06})
             if r.status == "ERROR"]
    assert "pin_role_contradiction" not in codes and "lead_hole" not in codes


def test_the_window_passes_the_applied_terminals(project):  # noqa: F811
    """@brief Positive: a declared lead reaches the check; negative: a draft lead does not, nor a pad without one."""
    window, _generations = project
    package = window.package_geometry
    pins = list(package.pins)
    window.package_geometry = replace(package, pins=[replace(pins[0], lead=ROUND_06)] + pins[1:])
    leads = window._applied_pad_leads()
    assert leads == {str(pins[0].number): {"lead": ROUND_06, "fit": None,
                                           "source": (pins[0].sources or {}).get("lead")}}
    assert window._component_check_args()["pad_leads"] == leads


# ── lead_fit carried end to end (2026-11-14) ─────────────────────────────

def test_a_press_fit_pad_survives_the_round_trip_and_is_not_judged():
    """@brief Positive: «press_fit» kept by mapping -> PinSpec -> asdict -> mapping, to the Pad,
    and skipped by both terminal rules; negative: the same pad without it is an error."""
    from dataclasses import asdict
    from kicadgen.core.package_geometry import package_from_mapping
    from kicadgen.generators.package import build_package
    from kicadgen.services.footprint_checks import check_hole_vs_lead
    blade = {"shape": "flat", "width_mm": 1.0, "thickness_mm": 0.4}      # diagonal 1.08
    pins = [{"number": "1", "x_mm": 0, "y_mm": 0, "pad_length_mm": 1.6, "pad_width_mm": 1.6,
             "pad_type": "thru_hole", "shape": "circle", "drill_mm": 0.85, "lead": blade,
             "lead_fit": "press_fit"},
            {"number": "2", "x_mm": 5.08, "y_mm": 0, "pad_length_mm": 1.6, "pad_width_mm": 1.6,
             "pad_type": "thru_hole", "shape": "circle", "drill_mm": 0.85, "lead": blade}]
    geometry = package_from_mapping({"name": "P", "family": "unknown", "pins": pins})
    again = package_from_mapping(asdict(geometry))
    assert [p.lead_fit for p in again.pins] == ["press_fit", None]
    footprint = build_package(again)
    assert [getattr(pad, "lead_fit", None) for pad in footprint.pads] == ["press_fit", None]
    assert [f.pads for f in check_hole_vs_lead(footprint)] == [(1,)], "only the clearance pad"
    leads = {str(p.number): {"lead": p.lead, "fit": p.lead_fit, "source": None} for p in again.pins}
    text = _footprint(_pad("1", 0, 1.6).replace("(drill 1)", "(drill 0.85)"),
                      _pad("2", 5.08, 1.6).replace("(drill 1)", "(drill 0.85)"))
    assert [r.pins for r in lead_hole_checks(text, leads)] == [("2",)]


def test_an_unknown_lead_fit_is_refused():
    """@brief Negative: a fit outside the contract fails validation, the known ones pass."""
    import pytest
    from kicadgen.core.package_geometry import PinSpec
    base = dict(number="1", x_mm=0, y_mm=0, pad_length_mm=1.6, pad_width_mm=1.6,
                pad_type="thru_hole", shape="circle", drill_mm=0.8)
    for fit in (None, "clearance", "press_fit"):
        PinSpec(**base, lead_fit=fit).validate()
    with pytest.raises(ValueError):
        PinSpec(**base, lead_fit="tight").validate()
