"""@brief The local SMD network's package family and pose reach the KiCad model (2026-10-02).

@note User request: «l'importante come primo passo che venga riconosciuto
    package ed orientamento». Before this, every body the network recognised
    stayed a generic block: the matcher demanded presence/class confidences
    and an observed terminal count the network does not produce. These tests
    pin the two new, declared exits -- the classifier identity in the
    provisional gate and the classifier pose in the builder -- each with the
    case they must refuse.
"""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from kicadgen.cad3d import kicad_models as km

CAD_DIR = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d"


@pytest.fixture
def library(tmp_path, monkeypatch):
    """@brief A mocked KiCad library: folder/name -> measured total envelope."""
    sizes = {}

    def add(folder, name, size):
        path = tmp_path / (folder + ".3dshapes") / (name + ".step")
        path.parent.mkdir(exist_ok=True)
        path.touch()
        sizes[str(path)] = {"body": list(size), "total": list(size)}
        return path
    monkeypatch.setattr(km, "model_sizes",
                        lambda paths, library=None, **kw: {str(p): sizes[str(p)] for p in paths})
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {})
    return tmp_path, add


def _network_row(**extra):
    """A row as the yolo_smd inventory writes it, after a person kept the box."""
    return dict(dict(kind="resistor", package="SMD chip resistor", length_mm=2.2, width_mm=1.15,
                     _smd_class="chip_R", _smd_accepted=True, _smd_body="endcap_chip",
                     _presence_reviewed_by_user=True, class_confidence=0.93), **extra)


def _resistors(add, *, with_0603=False):
    """Chip catalogue plus a MELF; the 0603 only when the measurement keeps it compatible.

    A model the file name already marks as incompatible is left out of the
    family search, which then declares itself incomplete (search_truncated).
    """
    if with_0603:
        add("Resistor_SMD", "R_0603_1608Metric", (1.6, 0.8, 0.45))
    add("Resistor_SMD", "R_0805_2012Metric", (2.0, 1.25, 0.5))
    # A cylindrical MELF that the measurement fits BETTER than the chip.
    return add("Resistor_SMD", "R_MicroMELF_MMU-0102", (2.2, 1.12, 1.1))


def test_the_network_identity_proposes_the_chip_size_the_measurement_allows(library):
    root, add = library
    _resistors(add)
    report = km.model_candidates(_network_row(), library=root)
    assert report["status"] == "provisional_matched"
    assert Path(report["selected_path"]).stem == "R_0805_2012Metric"


def test_a_chip_body_never_gets_the_better_fitting_melf(library):
    """@brief Negative: the MELF ranks first on size, the chip topology excludes it."""
    root, add = library
    melf = _resistors(add)
    report = km.model_candidates(_network_row(), library=root)
    assert Path(report["candidates"][0]["path"]) == melf
    assert report["selected_path"] != str(melf)


@pytest.mark.parametrize("missing", [
    dict(_smd_accepted=False),            # the network declined the class
    dict(_presence_reviewed_by_user=False),  # nobody kept the box
])
def test_without_both_network_and_person_nothing_is_proposed(library, missing):
    root, add = library
    _resistors(add)
    report = km.model_candidates(_network_row(**missing), library=root)
    assert report["status"] == "review_required" and not report.get("selected_path")


def test_two_sizes_the_photo_barely_separates_give_the_closest_provisionally(library):
    """@brief Halfway between two chip sizes: the closer one, the other named (2026-10-02).

    @note Contract changed by the user («proporre la piu' vicina»): it used to
        leave both in the picker. The other size stays a candidate.
    """
    root, add = library
    _resistors(add, with_0603=True)
    row = _network_row(length_mm=1.8, width_mm=1.02)
    report = km.model_candidates(row, library=root)
    # Decided on a complete search, not on a truncated one.
    assert not report.get("search_truncated")
    stems = {Path(c["path"]).stem for c in report["candidates"]}
    assert stems >= {"R_0603_1608Metric", "R_0805_2012Metric"}
    assert report["status"] == "provisional_matched"
    chosen = Path(report["selected_path"]).stem
    assert chosen in ("R_0603_1608Metric", "R_0805_2012Metric")
    assert report["size_tie_alternatives"] and chosen not in report["size_tie_alternatives"]


@pytest.fixture
def parts(monkeypatch):
    """@brief freecad_parts loaded without FreeCAD (only pure helpers are used)."""
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(Vector=lambda *a: a))
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("smd_parts", CAD_DIR / "freecad_parts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _box(x, y):
    return SimpleNamespace(XLength=x, YLength=y)


@pytest.mark.parametrize("body, inferred, pose, verse", [
    (0, 0, 180, 180),    # SOIC read at 180 on a horizontal axis: half turn
    (-90, 180, 90, 0),   # body angle and inferred axis already give the pose
    (-90, 0, 270, 0),
    (0, 90, 270, 180),   # SOT-23-6 of the RS485 bench
])
def test_the_network_pose_sets_only_the_verse_on_a_measured_axis(parts, body, inferred, pose,
                                                                 verse):
    component = _posed(pose, body)
    assert parts._classifier_verse(component, inferred) == verse


def test_a_pose_against_the_measured_axis_is_refused_unless_the_axis_is_undetermined(parts):
    """@brief Negative and positive: a quarter turn needs a measurement that cannot choose."""
    component = _posed(180, 0)
    assert parts._classifier_verse(component, 90) is None
    assert parts._classifier_verse(component, 90, axis_free=True) == 90


def _posed(pose, body, **extra):
    """A payload whose network pose is a real one: class with a verse, views agreeing."""
    return dict(dict(_smd_accepted=True, _smd_rotation_deg=pose, rotation_deg=body,
                     _smd_rotation_kind="full", _smd_views_agree=True), **extra)


@pytest.mark.parametrize("extra", [
    dict(_smd_accepted=False),            # the network declined the class
    dict(_smd_rotation_kind="axis"),      # chip R/C, arrays, crystals: axis only, no verse
    dict(_smd_rotation_kind=None),
    dict(_smd_views_agree=False),         # the four rotated views disagreed: a coin flip
])
def test_no_verse_without_a_real_agreed_pose(parts, extra):
    """@brief Negative (audit 2026-10-02): an axis-only class or a disputed pose sets no verse."""
    assert parts._classifier_verse(_posed(180, 0, **extra), 0) is None


def test_the_axis_is_undetermined_only_for_a_near_square_fit(parts):
    """@brief SOT-23-5 (2.8 x 2.9 mm) measured 3.43 x 3.17: noise; a chip 2.0 x 1.25: not."""
    assert parts._axis_undetermined(_box(2.8, 2.9), 3.43, 3.17)
    assert not parts._axis_undetermined(_box(2.0, 1.25), 2.46, 1.40)


def test_the_axis_margin_is_the_matcher_margin(parts):
    """@brief One notion of «the photo cannot tell», in both modules."""
    assert parts.AXIS_TIE_MARGIN == km.NAME_TIEBREAK_MARGIN


def test_tsot_names_declare_their_count():
    """@brief TSOT-23-5 declares 5 like SOT-23-5; a bare SOT-23 still declares none."""
    assert km._declared_count(Path("TSOT-23-5.step")) == 5
    assert km._declared_count(Path("SOT-23-5.step")) == 5
    assert km._declared_count(Path("SOT-23.step")) is None


def test_the_chip_catalogue_is_structural_not_a_size_list():
    """@brief 1808 and 0612 are catalogue sizes missing from _CHIP_SIZE_CODES (audit A-D1)."""
    folder = "Capacitor_SMD.3dshapes"
    assert km._is_endcap_chip_model(Path(folder) / "C_1808_4532Metric.step", "capacitor")
    assert km._is_endcap_chip_model(Path(folder) / "C_01005_0402Metric.step", "capacitor")
    assert not km._is_endcap_chip_model(Path(folder) / "C_0805_2012Metric_Pad1.18x1.45mm_HandSolder.step",
                                        "capacitor")
    assert not km._is_endcap_chip_model(Path("LED_SMD.3dshapes") / "LED_1206_3216Metric_Castellated.step",
                                        "led")


def _sot_row(count, **extra):
    return dict(dict(kind="ic", package="SOT-23 / SC-70", length_mm=2.95, width_mm=2.45,
                     _smd_class="SOT-23", _smd_accepted=True, _smd_terminal_count=count,
                     _presence_reviewed_by_user=True, class_confidence="medium"), **extra)


def _sots(add):
    add("Package_TO_SOT_SMD", "SOT-23-3", (2.9, 2.4, 1.0))
    add("Package_TO_SOT_SMD", "SOT-23-5", (2.9, 2.8, 1.0))
    add("Package_TO_SOT_SMD", "TSOT-23-5", (2.9, 2.8, 0.9))


def test_a_declared_count_only_admits_models_with_that_count(library):
    """@brief SOT-23 (3) gets SOT-23-3, never a 5-lead variant of similar size (audit A-D2)."""
    root, add = library
    _sots(add)
    report = km.model_candidates(_sot_row(3), library=root)
    assert report["status"] == "provisional_matched"
    assert Path(report["selected_path"]).stem == "SOT-23-3"
    five = km.model_candidates(_sot_row(5, length_mm=2.95, width_mm=2.85), library=root)
    assert Path(five["selected_path"] or "").stem in ("SOT-23-5", "TSOT-23-5")


def test_a_class_without_a_package_declaration_takes_the_ordinary_gates(library):
    """@brief Negative: an array, an RGB LED... names only a kind, so no classifier exit (A-D3)."""
    root, add = library
    _resistors(add)
    row = _network_row(_smd_class="R_Array", _smd_body=None)
    assert not km._local_classifier_identity(row)
    report = km.model_candidates(row, library=root)
    assert report["status"] == "review_required"


def test_an_archive_entry_outside_the_declared_package_does_not_win(library, monkeypatch):
    """@brief A MELF from the archive on a chip body goes to review, not attached (A-D5)."""
    root, add = library
    melf = _resistors(add)
    accepted = {"status": "library_matched", "selected_path": str(melf), "discovery": "accepted_library",
                "candidates": [{"path": str(melf), "package": melf.stem, "relative_xy_error": 0.02,
                                "measurement_basis": "accepted_library", "family": "resistor",
                                "status": "library_matched", "reasons": []}]}
    monkeypatch.setattr(km, "_accepted_match", lambda *a, **k: dict(accepted))
    report = km.model_candidates(_network_row(), library=root)
    assert report["status"] == "review_required" and report.get("classifier_archive_conflict")
    # Negative: the same archive entry on a body the network did not identify keeps winning.
    plain = km.model_candidates(_network_row(_smd_accepted=False), library=root)
    assert plain["status"] == "library_matched"


def test_the_band_never_mixes_measurement_bases(monkeypatch):
    """@brief Body of one model against the total of another is not a tie (audit A-D4).

    @note Pads are a valid basis (the detector box includes the solder: an
        0805 measured 2.46 x 1.40 mm is closest to its land pattern), but a
        band holds ONE kind of basis. Negative: a basis that is not a
        measurement of the model (an archive entry) proposes nothing.
    """
    monkeypatch.setattr(km, "_band_is_homogeneous", lambda band, *a: len(band) == 1)
    row = _network_row()
    base = Path("Resistor_SMD.3dshapes")
    choices = [dict(path=str(base / "R_0805_2012Metric.step"), relative_xy_error=0.05,
                    measurement_basis="piazzole R_0805", family="resistor"),
               dict(path=str(base / "R_1206_3216Metric.step"), relative_xy_error=0.06,
                    measurement_basis="total", family="resistor")]
    # Without the single-basis rule both would share a band that is not
    # homogeneous (stubbed above), and nothing would be proposed.
    assert km._classifier_geometry_choice(row, choices, True) is choices[0]
    choices[0]["measurement_basis"] = "accepted_library"
    assert km._classifier_geometry_choice(row, choices, True) is None


def test_a_reverse_geometry_chip_is_not_an_end_cap_chip():
    """@brief 0612 has the outline of a 1206 but terminals on the long sides: excluded."""
    folder = Path("Resistor_SMD.3dshapes")
    assert km._is_endcap_chip_model(folder / "R_1206_3216Metric.step", "resistor")
    assert not km._is_endcap_chip_model(folder / "R_0612_1632Metric.step", "resistor")
    assert not km._is_endcap_chip_model(folder / "R_0815_2038Metric.step", "resistor")


def test_a_name_without_a_count_is_admitted_through_its_footprint_pads(monkeypatch):
    """@brief D_SOD-123F declares no count in its name; its footprint's two pads do.

    @note Negative: a footprint with three ordinary pads, or footprints that
        disagree (None), do not confirm a two-terminal class.
    """
    row = dict(kind="diode", _smd_terminal_count=2)
    path = Path("Diode_SMD.3dshapes") / "D_SOD-123F.step"
    monkeypatch.setattr(km, "_declared_land_pattern", lambda p, library=None: (2, 0))
    assert km._classifier_admits(path, row)
    monkeypatch.setattr(km, "_declared_land_pattern", lambda p, library=None: (3, 0))
    assert not km._classifier_admits(path, row)
    monkeypatch.setattr(km, "_declared_land_pattern", lambda p, library=None: None)
    assert not km._classifier_admits(path, row)


def _tied_sizes(length=1.91, width=1.0):
    """0805 and 0603 chip envelopes with the worst-axis error model_candidates computes."""
    base = Path("Resistor_SMD.3dshapes")
    measured = sorted((length, width))
    choices = []
    for name, dims in (("R_0805_2012Metric", [2.0, 1.25]), ("R_0603_1608Metric", [1.6, 0.8])):
        error = max(abs(m - side) / side for m, side in zip(sorted(dims), measured))
        choices.append(dict(path=str(base / f"{name}.step"), relative_xy_error=error,
                            measurement_basis="total", family="resistor", dimensions_mm=dims))
    return sorted(choices, key=lambda item: item["relative_xy_error"])


def test_nearly_tied_sizes_propose_the_closest_on_both_axes():
    """@brief User decision 2026-10-02 «proporre la piu' vicina», closeness on BOTH axes.

    @note Contract changed on purpose (adversarial review A1, 2026-10-02):
        the worst axis alone picked the 0603, 18 % off on the length, over the
        0805, 2.6 % off, for a 1.95 x 1.02 mm reading. Here the 0603 heads
        the ranked list (smaller worst-axis error) and the 0805 is the closer
        size overall.
    """
    row = _network_row(length_mm=1.95, width_mm=1.02)
    choices = _tied_sizes(1.95, 1.02)
    assert Path(choices[0]["path"]).stem == "R_0603_1608Metric"
    chosen = km._classifier_geometry_choice(row, choices, True)
    assert Path(chosen["path"]).stem == "R_0805_2012Metric"
    assert chosen["tie_alternatives"] == ["R_0603_1608Metric"]


def test_the_smaller_size_wins_when_it_is_closer_on_both_axes():
    """@brief Negative of the previous: the rule is closeness, not «the larger one»."""
    row = _network_row(length_mm=1.78, width_mm=1.02)
    chosen = km._classifier_geometry_choice(row, _tied_sizes(1.78, 1.02), True)
    assert Path(chosen["path"]).stem == "R_0603_1608Metric"
    assert chosen["tie_alternatives"] == ["R_0805_2012Metric"]


def test_a_tie_on_another_basis_is_named_and_the_pick_is_not_unique():
    """@brief Review A3: the band is formed before the basis filter."""
    row = _network_row(length_mm=1.95, width_mm=1.02)
    body, pads = _tied_sizes(1.95, 1.02)
    pads = dict(pads, measurement_basis="piazzole")
    chosen = km._classifier_geometry_choice(row, [body, pads], True)
    assert chosen is body
    assert chosen["tie_alternatives"] == [Path(pads["path"]).stem]


def test_a_lone_size_with_no_rival_on_any_basis_has_no_alternatives():
    """@brief Negative of the previous: nothing within the margin, nothing to name."""
    row = _network_row(length_mm=1.6, width_mm=0.8)
    body, = [item for item in _tied_sizes(1.6, 0.8) if "0603" in item["path"]]
    far = dict(_tied_sizes(1.6, 0.8)[1], measurement_basis="piazzole")
    chosen = km._classifier_geometry_choice(row, [body, far], True)
    assert chosen is body and "tie_alternatives" not in chosen


def test_no_closest_pick_on_an_incomplete_search():
    """@brief Negative: «closest» among sizes never measured means nothing."""
    row = _network_row(length_mm=1.91, width_mm=1.0)
    assert km._classifier_geometry_choice(row, _tied_sizes(), False) is None


def test_a_name_count_contradicted_by_the_copper_is_not_admitted(monkeypatch):
    """@brief Review A2: SOIC-8-N7 names 8 positions but solders 7 pads.

    @note Negative: the plain SOIC-8, whose copper agrees with its name, stays admitted.
    """
    row = dict(kind="ic_soic", _smd_terminal_count=8)
    base = Path("Package_SO.3dshapes")
    monkeypatch.setattr(km, "_declared_land_pattern", lambda p, library=None: (7, 0))
    assert not km._classifier_admits(base / "SOIC-8-N7_3.9x4.9mm_P1.27mm.step", row)
    monkeypatch.setattr(km, "_declared_land_pattern", lambda p, library=None: (8, 0))
    assert km._classifier_admits(base / "SOIC-8_3.9x4.9mm_P1.27mm.step", row)
