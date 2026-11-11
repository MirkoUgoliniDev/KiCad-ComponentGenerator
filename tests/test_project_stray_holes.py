"""@brief A project saved by an older version with a hole on a hole-less pad still opens (review 2026-11-10, K1).

Versions up to 1.0.8 copied a drill onto a pad whatever its type (a missing
type is "smd") and wrote the footprint without it. The pad contract now
refuses such a pad for NEW data; the load path drops the stray hole, so the
project opens and produces the same footprint as before.
"""
import pytest

from kicadgen.core.package_geometry import package_from_mapping
from kicadgen.gui.project_state import ProjectConfigError, saved_package_geometry, without_stray_holes


def _package(*pins):
    return {"name": "PART", "pins": list(pins)}


def _pad(number, **extra):
    return {"number": number, "x_mm": float(number) * 3.0, "y_mm": 0.0,
            "pad_length_mm": 1.5, "pad_width_mm": 1.5, **extra}


def test_an_old_project_with_a_hole_on_an_smd_pad_opens_and_says_so():
    saved = _package(_pad("1", drill_mm=0.8),                       # no type: smd
                     _pad("2", pad_type="smd", drill_mm=0.8, drill_length_mm=1.2),
                     _pad("3", pad_type="thru_hole", drill_mm=0.8))
    dropped = []
    package = saved_package_geometry(saved, stray_holes=dropped)
    assert dropped == ["1", "2"]
    by_number = {pin.number: pin for pin in package.pins}
    assert by_number["1"].drill_mm is None and by_number["2"].drill_length_mm is None
    # A drilled pad keeps its hole: only holes on hole-less pads are stray.
    assert by_number["3"].drill_mm == 0.8
    # The saved value itself is never modified.
    assert saved["pins"][0]["drill_mm"] == 0.8


def test_a_clean_project_is_returned_unchanged_and_new_data_is_still_refused():
    clean = _package(_pad("1", pad_type="thru_hole", drill_mm=0.8), _pad("2", pad_type="smd"))
    dropped = []
    assert without_stray_holes(clean, dropped) is clean and dropped == []
    # The contract for data that does not come from a saved project is unchanged.
    with pytest.raises(ValueError):
        package_from_mapping(_package(_pad("1", pad_type="smd", drill_mm=0.8)))


def test_a_project_broken_in_another_way_is_still_refused():
    with pytest.raises(ProjectConfigError):
        saved_package_geometry(_package(_pad("1", pad_type="thru_hole")))   # drilled pad without a hole
