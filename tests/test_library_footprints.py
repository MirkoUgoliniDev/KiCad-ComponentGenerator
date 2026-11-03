"""@brief KiCad library footprints found by the part number, as a geometry witness (user, 2026-10-31).

The user: «inserire nel progetto anche la sigla specifica in modo da guidare
eventuali match» (add the specific part number to the project to guide
matches). The footprints it names only confirm or contradict the pad
geometry read from the drawing; numbers and names stay the drawing's.
No network, no KiCad install: the footprint folder is written by the test.
"""
from pathlib import Path
from unittest.mock import Mock

import pytest

from kicadgen.services import datasheet_extractor as ext
from kicadgen.services import library_footprints as lib

#: T7N-like relay read on a BOTTOM view, page coordinates in mm: COM pin 11
#: 2 mm off the coil column. Symmetric about its own row (y = 6), so its
#: mirror image is the same layout turned.
RELAY_BOTTOM = {"11": (0.0, 6.0), "A1": (2.0, 0.0), "A2": (2.0, 12.0),
                "14": (14.2, 0.0), "12": (14.2, 12.0)}
#: The same with pin 11 off the middle: no mirror symmetry, a chiral layout.
CHIRAL_BOTTOM = dict(RELAY_BOTTOM, **{"11": (0.0, 4.0)})


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(ext, "api_key", lambda: "offline")
    monkeypatch.setattr(ext, "check_credit", lambda **kwargs: None)


def _top_view_turned(pads, dx=3.0, dy=-1.0):
    """The same relay as a KiCad library would draw it: top view, a quarter turn, moved."""
    return [(-y + dx, -x + dy) for x, y in pads.values()]


def _write_footprint(folder: Path, library: str, name: str, centres, extra=""):
    pretty = folder / f"{library}.pretty"
    pretty.mkdir(parents=True, exist_ok=True)
    pads = "".join(
        f'\n\t(pad "{index}" thru_hole circle (at {x} {y}) (size 2 2) (drill 1.3) '
        f'(layers "*.Cu" "*.Mask"))' for index, (x, y) in enumerate(centres, 1))
    (pretty / f"{name}.kicad_mod").write_text(
        f'(footprint "{name}" (layer "F.Cu"){pads}{extra}\n)', encoding="utf-8")


# -- search by part number --------------------------------------------------------

def test_a_series_code_in_the_part_number_finds_the_footprint(tmp_path):
    _write_footprint(tmp_path, "Relay_THT", "Relay_DPDT_Omron_G5V-2", [(0, 0), (5, 0)])
    _write_footprint(tmp_path, "Relay_THT", "Relay_DPDT_Hongfa_HF115F-2Z-x4", [(0, 0), (5, 0)])
    _write_footprint(tmp_path, "Relay_THT", "Relay_SPDT_Finder_40.11", [(0, 0), (5, 0)])
    _write_footprint(tmp_path, "Relay_THT", "Relay_1-Form-A_Schrack-RYII_RM5mm", [(0, 0), (5, 0)])
    names = lambda part: [p.stem for p in lib.find_footprints(part, tmp_path)]
    assert names("G5V-2-H1 12VDC") == ["Relay_DPDT_Omron_G5V-2"]
    assert names("g5v 2/h1") == ["Relay_DPDT_Omron_G5V-2"], "which separator, and case, do not matter"
    # «x» touching no letter is a placeholder: the C4 variant of the same series.
    assert names("HF115F-2Z-C4") == ["Relay_DPDT_Hongfa_HF115F-2Z-x4"]
    assert names("40.11.8.012.0000") == ["Relay_SPDT_Finder_40.11"]


def test_without_separators_the_end_of_the_series_cannot_be_placed(tmp_path):
    """Negative, declared cost of the boundary rule: «g5v2h1» could be G5V-2 + H1 or G5V-2H + 1."""
    _write_footprint(tmp_path, "Relay_THT", "Relay_DPDT_Omron_G5V-2", [(0, 0), (5, 0)])
    assert lib.find_footprints("g5v2h1", tmp_path) == []


# -- C1: anchored codes, the most specific one votes ---------------------------------

def _names(folder, part):
    return [p.stem for p in lib.find_footprints(part, folder)]


def test_a_code_followed_by_more_characters_in_its_segment_is_not_matched(tmp_path):
    """«G2RL-1» ends inside the segment «1A» of G2RL-1A-DC12: the 5-pad superset stays out."""
    _write_footprint(tmp_path, "Relay_THT", "Relay_SPST_Omron_G2RL-1A", [(0, 0), (5, 0)])
    _write_footprint(tmp_path, "Relay_THT", "Relay_SPDT_Omron_G2RL-1", [(0, 0), (5, 0), (9, 0)])
    assert _names(tmp_path, "G2RL-1A-DC12") == ["Relay_SPST_Omron_G2RL-1A"]
    # Positive counterpart: the same footprint is found when its code ends at a separator.
    assert _names(tmp_path, "G2RL-1-DC12") == ["Relay_SPDT_Omron_G2RL-1"]


def test_only_the_most_specific_code_votes(tmp_path):
    """G6K-2P-Y-DC5 carries both «G6K-2P» and «G6K-2P-Y»: the longer one alone is kept."""
    _write_footprint(tmp_path, "Relay_THT", "Relay_DPDT_Omron_G6K-2P", [(0, 0), (3.2, 0)])
    _write_footprint(tmp_path, "Relay_THT", "Relay_DPDT_Omron_G6K-2P-Y", [(0, 0), (2.54, 0)])
    _write_footprint(tmp_path, "Relay_THT", "Relay_SPDT_Omron_G2RL-1", [(0, 0), (5, 0)])
    _write_footprint(tmp_path, "Relay_THT", "Relay_SPDT_Omron_G2RL-1-E", [(0, 0), (5, 0)])
    assert _names(tmp_path, "G6K-2P-Y-DC5") == ["Relay_DPDT_Omron_G6K-2P-Y"]
    assert _names(tmp_path, "G6K-2P-DC5") == ["Relay_DPDT_Omron_G6K-2P"], "no Y segment: the base"
    assert _names(tmp_path, "G2RL-1-E-DC12") == ["Relay_SPDT_Omron_G2RL-1-E"]


def test_a_code_shared_by_footprints_of_different_geometry_gives_no_verdict(tmp_path):
    """DIP-8 names an oscillator and two widths: all are candidates, none may decide."""
    narrow = [(0, 0), (0, 2.54), (0, 5.08), (0, 7.62),
              (7.62, 7.62), (7.62, 5.08), (7.62, 2.54), (7.62, 0)]
    wide = [(x * 10.16 / 7.62, y) for x, y in narrow]
    _write_footprint(tmp_path, "Package_DIP", "DIP-8_W7.62mm", narrow)
    _write_footprint(tmp_path, "Package_DIP", "DIP-8_W10.16mm", wide)
    _write_footprint(tmp_path, "Oscillator", "Oscillator_DIP-8",
                     [(0, 0), (7.62, 0), (7.62, 7.62), (0, 7.62)])
    names = _names(tmp_path, "DIP-8")
    assert sorted(names) == ["DIP-8_W10.16mm", "DIP-8_W7.62mm", "Oscillator_DIP-8"]
    footprints = [lib.read_footprint(p) for p in lib.find_footprints("DIP-8", tmp_path)]
    reading = {str(i): (x, y) for i, (x, y) in enumerate(narrow, 1)}
    assert lib.check_layout(reading, "top", footprints, 0.1) is None
    # Positive: the narrow footprint alone confirms the same reading.
    narrow_only = [f for f in footprints if f.name == "DIP-8_W7.62mm"]
    assert lib.check_layout(reading, "top", narrow_only, 0.1).status == "confirmed"


def test_geometrically_equal_duplicates_still_give_a_verdict():
    """The same layout in two libraries (a socket, long pads) is one geometry, not an ambiguity."""
    base = _footprint(_top_view_turned(RELAY_BOTTOM), "DIP_Like")
    shifted = _footprint([(x + 40, y - 7) for x, y in _top_view_turned(RELAY_BOTTOM)],
                         "DIP_Like_Socket")
    verdict = lib.check_layout(RELAY_BOTTOM, "bottom", [base, shifted], 0.1)
    assert verdict.status == "confirmed"
    moved = dict(RELAY_BOTTOM, **{"11": (0.0, 5.0)})
    assert lib.check_layout(moved, "bottom", [base, shifted], 0.1).reason == "no_match"


def test_a_superset_footprint_does_not_confirm_a_reading_with_a_phantom_pin():
    """Two candidates, 4 and 5 pads: the 5-pad reading must not be confirmed by the superset."""
    four = {k: v for k, v in RELAY_BOTTOM.items() if k != "11"}
    small = _footprint(_top_view_turned(four), "G2RL_1A_like")
    large = _footprint(_top_view_turned(RELAY_BOTTOM), "G2RL_1_like")
    assert lib.check_layout(RELAY_BOTTOM, "bottom", [small, large], 0.1) is None
    assert lib.check_layout(RELAY_BOTTOM, "bottom", [large], 0.1).status == "confirmed"


# -- C4: segments, makers, placeholders ----------------------------------------------

@pytest.mark.parametrize("part", ["TPS73650DBVR", "MCP1700-3302E/TO", "RC0603FR-0710KL"])
def test_a_number_only_code_inside_a_longer_segment_is_not_matched(tmp_path, part):
    """Negative: «3650», «1700», «0603» sit inside alphanumeric segments of unrelated parts."""
    _write_footprint(tmp_path, "Relay_THT", "Relay_3PST_COTO_3650", [(0, 0), (5, 0)])
    _write_footprint(tmp_path, "Inductor_THT", "L_Radial_D7.2mm_P3.00mm_Murata_1700",
                     [(0, 0), (3, 0)])
    _write_footprint(tmp_path, "Resistor_SMD", "R_0603_1608Metric", [(0, 0), (1.6, 0)])
    assert lib.find_footprints(part, tmp_path) == []


def test_a_number_only_code_matching_whole_segments_is_found(tmp_path):
    """Positive counterpart: the same codes as whole segments, dotted or hyphenated."""
    _write_footprint(tmp_path, "Relay_THT", "Relay_3PST_COTO_3650", [(0, 0), (5, 0)])
    _write_footprint(tmp_path, "Relay_THT", "Relay_DPDT_Finder_40.52", [(0, 0), (5, 0)])
    assert _names(tmp_path, "3650-1211-051") == ["Relay_3PST_COTO_3650"]
    assert _names(tmp_path, "40.52.9.024") == ["Relay_DPDT_Finder_40.52"]


def test_a_maker_segment_glued_to_the_code_is_optional(tmp_path):
    """«Omron-G5LE-1»: letters-only segment before a code segment, by shape, not by name."""
    _write_footprint(tmp_path, "Relay_THT", "Relay_SPDT_Omron-G5LE-1", [(0, 0), (5, 0)])
    assert _names(tmp_path, "G5LE-1-DC12") == ["Relay_SPDT_Omron-G5LE-1"]
    assert _names(tmp_path, "Omron G5LE-1") == ["Relay_SPDT_Omron-G5LE-1"]
    # Negative: the glued maker does not loosen the end boundary.
    assert lib.find_footprints("G5LE-1A4-DC12", tmp_path) == []


def test_a_letters_only_segment_after_the_code_is_not_a_maker(tmp_path):
    """Negative: only LEADING letters-only segments are optional; a trailing «-E» variant stays."""
    _write_footprint(tmp_path, "Relay_THT", "Relay_SPDT_Omron_G2RL-1-E", [(0, 0), (5, 0)])
    assert lib.find_footprints("G2RL-1-DC12", tmp_path) == []


def test_every_x_of_a_placeholder_run_is_a_wildcard(tmp_path):
    _write_footprint(tmp_path, "Relay_THT", "Relay_SPDT_Hongfa_HF3F-L-xx-1ZL1T", [(0, 0), (5, 0)])
    _write_footprint(tmp_path, "Relay_THT", "Relay_SPST_TE_PCH-1xxx2M", [(0, 0), (5, 0)])
    assert _names(tmp_path, "HF3F-L-05-1ZL1T") == ["Relay_SPDT_Hongfa_HF3F-L-xx-1ZL1T"]
    assert _names(tmp_path, "PCH-112D2M") == ["Relay_SPST_TE_PCH-1xxx2M"]
    # Negative: a placeholder stands for one character per x, and a ZL2T is not a ZL1T.
    assert lib.find_footprints("HF3F-L-005-1ZL1T", tmp_path) == []
    assert lib.find_footprints("HF3F-L-05-1ZL2T", tmp_path) == []


def test_an_x_inside_a_word_or_a_pin_arrangement_is_not_a_placeholder(tmp_path):
    """Negative: «IMxxG» is a word, «1x02» a rows-by-columns token, neither a series code wildcard."""
    _write_footprint(tmp_path, "Relay_SMD", "Relay_2P2T_10x6mm_TE_IMxxG4", [(0, 0), (5, 0)])
    _write_footprint(tmp_path, "Connector", "PinHeader_1x02_P2.54mm", [(0, 0), (2.54, 0)])
    assert lib.find_footprints("IM06G4", tmp_path) == []
    assert lib.find_footprints("1302", tmp_path) == []
    assert lib.find_footprints("1x02", tmp_path) == []
    assert _names(tmp_path, "IMxxG4") == ["Relay_2P2T_10x6mm_TE_IMxxG4"], "literal x still matches"


@pytest.mark.parametrize("part", ["", "  ", "T7N", "RM5mm", "T7NS5D4-12"])
def test_no_footprint_for_a_short_part_number_a_size_or_an_unknown_series(tmp_path, part):
    """Negative: a size token («RM5mm») or a code shorter than MIN_TOKEN_CHARS selects nothing."""
    _write_footprint(tmp_path, "Relay_THT", "Relay_1-Form-A_Schrack-RYII_RM5mm", [(0, 0), (5, 0)])
    _write_footprint(tmp_path, "Relay_THT", "Relay_DPDT_Omron_G6A", [(0, 0), (5, 0)])
    assert lib.find_footprints(part, tmp_path) == []


def test_a_part_number_naming_too_many_footprints_names_none(tmp_path):
    """Runtime safety cap only: beyond MAX_CANDIDATES files nothing is parsed."""
    for index in range(lib.MAX_CANDIDATES + 1):
        _write_footprint(tmp_path, "Lib", f"Part_ABCD12_v{index}", [(0, 0), (5, 0)])
    assert lib.find_footprints("ABCD12", tmp_path) == []


def test_a_dozen_candidates_of_one_geometry_are_kept(tmp_path):
    """C5: no arbitrary count limit below the safety cap; one shared geometry still decides."""
    for index in range(13):
        _write_footprint(tmp_path, f"Lib{index}", "Part_ABCD12", [(index, 0), (index + 5, 0)])
    paths = lib.find_footprints("ABCD12", tmp_path)
    assert len(paths) == 13
    footprints = [lib.read_footprint(p) for p in paths]
    verdict = lib.check_layout({"1": (0, 0), "2": (0, 5)}, "top", footprints, 0.1)
    assert verdict.status == "confirmed"


def test_unnumbered_pads_and_mounting_holes_are_not_centres(tmp_path):
    extra = ('\n\t(pad "" thru_hole circle (at 9 9) (size 2 2) (drill 1) (layers "*.Cu"))'
             '\n\t(pad "M" np_thru_hole circle (at 7 7) (size 3 3) (drill 3) (layers "*.Cu"))')
    _write_footprint(tmp_path, "Lib", "Part_ABCD12", [(0, 0), (5, 0)], extra)
    footprint = lib.read_footprint(tmp_path / "Lib.pretty" / "Part_ABCD12.kicad_mod")
    assert footprint.centres == ((0.0, 0.0), (5.0, 0.0))
    (tmp_path / "broken.kicad_mod").write_text("(footprint", encoding="utf-8")
    assert lib.read_footprint(tmp_path / "broken.kicad_mod") is None


# -- geometry ------------------------------------------------------------------------

def _footprint(centres, name="Relay_Lib"):
    return lib.LibraryFootprint(name=name, centres=tuple(centres))


def test_the_same_layout_turned_and_moved_is_confirmed_in_the_declared_view():
    footprint = _footprint(_top_view_turned(RELAY_BOTTOM))
    verdict = lib.check_layout(RELAY_BOTTOM, "bottom", [footprint], 0.1)
    assert verdict.status == "confirmed" and verdict.image == "Relay_Lib"
    assert verdict.source == "library"


def test_a_mirrored_chiral_layout_or_another_layout_is_no_match():
    """Negative: read as a TOP view, a chiral page is the mirror image of the part."""
    chiral = _footprint(_top_view_turned(CHIRAL_BOTTOM))
    assert lib.check_layout(CHIRAL_BOTTOM, "bottom", [chiral], 0.1).status == "confirmed"
    assert lib.check_layout(CHIRAL_BOTTOM, "top", [chiral], 0.1).reason == "no_match"
    footprint = _footprint(_top_view_turned(RELAY_BOTTOM))
    moved = dict(RELAY_BOTTOM, **{"11": (0.0, 5.0)})
    assert lib.check_layout(moved, "bottom", [footprint], 0.1).reason == "no_match"
    # A reading with one pad fewer than the footprint is no geometric evidence
    # either way: the witness abstains (review S3, 2026-11-03), it never says
    # "no_match" for a count.
    four = {k: v for k, v in RELAY_BOTTOM.items() if k != "11"}
    assert lib.check_layout(four, "bottom", [footprint], 0.1) is None


def test_no_candidate_or_an_unknown_view_gives_no_opinion():
    footprint = _footprint(_top_view_turned(RELAY_BOTTOM))
    assert lib.check_layout(RELAY_BOTTOM, "unknown", [footprint], 0.1) is None
    assert lib.check_layout(RELAY_BOTTOM, "bottom", [], 0.1) is None


# -- in the extraction ---------------------------------------------------------------

def _drawing(tmp_path):
    from PIL import Image
    path = tmp_path / "quote.png"
    Image.new("RGB", (300, 200), "white").save(path)
    return path


def _main_answer(pads):
    return dict(drawing_unit="mm", contacts_view="bottom", confidence="high",
                body_length_mm=22.5, body_width_mm=16.5, body_height_mm=16.9, lead_exit="bottom",
                pads_origin="custom", value_sources={},
                pads=[{"number": n, "x_mm": x, "y_mm": y, "pad_type": "thru_hole",
                       "drill_mm": 1.3, "pad_length_mm": 2.2, "pad_width_mm": 2.2}
                      for n, (x, y) in pads.items()])


#: The dedicated reading of the same drawing with pin 11 put in the coil column.
WRONG_CHAINS = {"drawing_unit": "mm", "contacts_view": "bottom", "chains": [
    {"axis": "x", "stops": ["11,A1,A2", "14,12"], "values": [14.2]},
    {"axis": "y", "stops": ["A1,14", "11", "A2,12"], "values": [6.0, 6.0]}]}


def _extract(monkeypatch, tmp_path, part_number, library_centres, main=CHIRAL_BOTTOM):
    folder = tmp_path / "footprints"
    _write_footprint(folder, "Relay_THT", "Relay_SPDT_Maker_ZZ1234", library_centres)
    monkeypatch.setattr(lib, "footprints_root", lambda: folder)
    call = Mock(side_effect=[_main_answer(main), WRONG_CHAINS, {"drawing_unit": "mm"}])
    monkeypatch.setattr(ext, "call_tool", call)
    logs = []
    params = ext.extract_pin_header_params(
        [_drawing(tmp_path)], component_family="single_tht", part_number=part_number,
        document_roles={"quote.png": ["pinout"]}, progress=logs.append)
    return params, logs


def test_end_to_end_the_library_footprint_settles_two_disagreeing_readings(offline, monkeypatch,
                                                                           tmp_path):
    params, logs = _extract(monkeypatch, tmp_path, "ZZ1234-12VDC",
                            _top_view_turned(CHIRAL_BOTTOM))
    assert not params.pin_map_conflict
    numbers = sorted(p["number"] for p in params.package_geometry["pins"])
    assert numbers == ["11", "12", "14", "A1", "A2"], "numbers stay the drawing's"
    assert "Relay_SPDT_Maker_ZZ1234" in params.notes
    assert any("Relay_SPDT_Maker_ZZ1234" in line for line in logs)


def test_end_to_end_a_mirror_symmetric_layout_is_not_settled_by_the_library(offline,
                                                                            monkeypatch, tmp_path):
    """Declared limit, same as the drawn holes (_pick_by_holes): when the
    chosen layout's mirror image fits too, geometry cannot tell the side the
    pads are seen from, so nothing is chosen."""
    params, _logs = _extract(monkeypatch, tmp_path, "ZZ1234-12VDC",
                             _top_view_turned(RELAY_BOTTOM), main=RELAY_BOTTOM)
    assert params.pin_map_conflict


def test_end_to_end_a_library_footprint_of_another_layout_settles_nothing(offline, monkeypatch,
                                                                          tmp_path):
    """Negative: the footprint named by the part number matches neither reading."""
    other = [(0, 0), (5, 0), (10, 0), (0, 7.5), (10, 7.5)]
    params, _logs = _extract(monkeypatch, tmp_path, "ZZ1234-12VDC", other)
    assert params.pin_map_conflict and "non hanno questa disposizione" in params.pin_map_conflict


def test_end_to_end_without_a_part_number_nothing_is_searched(offline, monkeypatch, tmp_path):
    params, logs = _extract(monkeypatch, tmp_path, None, _top_view_turned(RELAY_BOTTOM))
    assert params.pin_map_conflict, "two disagreeing readings and nothing else: blocked"
    assert not any("Footprint KiCad con la sigla" in line for line in logs)


# -- Pads repeating a number (L13) ---------------------------------------------------

def _write_pads(folder: Path, name: str, pads):
    """A footprint whose pads are (number, x, y), numbers possibly repeated."""
    pretty = folder / "Lib.pretty"
    pretty.mkdir(parents=True, exist_ok=True)
    body = "".join(f'\n\t(pad "{n}" smd rect (at {x} {y}) (size 1 1) (layers "F.Cu"))'
                   for n, x, y in pads)
    (pretty / f"{name}.kicad_mod").write_text(f'(footprint "{name}" (layer "F.Cu"){body}\n)',
                                              encoding="utf-8")
    return pretty / f"{name}.kicad_mod"


def test_stacked_pads_of_one_number_are_one_centre_and_still_confirm(tmp_path):
    """Positive: «EP» drawn as four stacked/tiled pads is one terminal at their mean."""
    path = _write_pads(tmp_path, "QFN_X", [("1", 0, 0), ("2", 4, 0),
                                            ("EP", 1.5, 1.5), ("EP", 2.5, 1.5),
                                            ("EP", 1.5, 2.5), ("EP", 2.5, 2.5)])
    footprint = lib.read_footprint(path)
    assert footprint.centres == ((0.0, 0.0), (4.0, 0.0), (2.0, 2.0))
    verdict = lib.check_layout({"1": (0, 0), "2": (4, 0), "EP": (2, 2)}, "top", [footprint], 0.1)
    assert verdict.status == "confirmed"


def test_distinct_numbers_at_distinct_places_stay_distinct(tmp_path):
    """Negative: without repeated numbers nothing is merged; a reading missing a pad has
    another count, on which the witness abstains (review S3), while a reading with the
    same count and another geometry is still no match."""
    path = _write_pads(tmp_path, "Row_X", [("1", 0, 0), ("2", 2, 0), ("3", 4, 0)])
    footprint = lib.read_footprint(path)
    assert len(footprint.centres) == 3 and footprint.repeated_apart == ()
    assert lib.check_layout({"1": (0, 0), "2": (2, 0)}, "top", [footprint], 0.1) is None
    verdict = lib.check_layout({"1": (0, 0), "2": (2, 0), "3": (4, 1)}, "top", [footprint], 0.1)
    assert verdict.reason == "no_match"


# -- review S3 (2026-11-03): a number repeated at distant places -----------------------

def test_mounting_tabs_sharing_a_number_far_apart_make_the_witness_abstain(tmp_path):
    """Positive: two «MP» tabs at opposite ends of a connector are two terminals; their mean
    is a phantom point no reading has, so the footprint gives no verdict instead of
    "no_match" against a correct reading."""
    path = _write_pads(tmp_path, "Conn_X", [("1", 0, 0), ("2", 1, 0), ("3", 2, 0),
                                             ("MP", -2, 2), ("MP", 4, 2)])
    footprint = lib.read_footprint(path)
    assert footprint.repeated_apart == ("MP",)
    reading = {"1": (0, 0), "2": (1, 0), "3": (2, 0), "MP": (-2, 2)}
    assert lib.check_layout(reading, "top", [footprint], 0.1) is None


def test_touching_pads_of_one_number_still_merge_and_judge(tmp_path):
    """Negative: the same number on pads whose extents touch is one copper island (stacked
    or tiled pads): merged into one centre, and the witness still says no_match for a
    reading of the same count with another geometry."""
    path = _write_pads(tmp_path, "Tab_X", [("1", 0, 0), ("2", 4, 0),
                                            ("MP", 2, 3), ("MP", 2.9, 3)])
    footprint = lib.read_footprint(path)
    assert footprint.repeated_apart == () and len(footprint.centres) == 3
    assert lib.check_layout({"1": (0, 0), "2": (4, 0), "MP": (2.45, 3)}, "top",
                            [footprint], 0.1).status == "confirmed"
    assert lib.check_layout({"1": (0, 0), "2": (4, 0), "MP": (2.45, 5)}, "top",
                            [footprint], 0.1).reason == "no_match"
