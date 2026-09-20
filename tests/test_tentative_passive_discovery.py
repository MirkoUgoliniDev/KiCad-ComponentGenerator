"""@brief Tentative chip sizes must not manufacture complete or unique CAD matches."""

import copy

import pytest

from kicadgen.cad3d import kicad_models as km


@pytest.fixture
def cold_catalog(tmp_path, monkeypatch):
    sizes, measured = {}, []

    def add(folder, name, dimensions, group=""):
        path = tmp_path / group / (folder + ".3dshapes") / (name + ".step")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        sizes[str(path)] = (None if dimensions is None else {
            "body": list(dimensions), "total": list(dimensions),
            "body_method": "total_standard_endcap"})
        return path

    def measure(paths, library=None):
        measured.extend(paths)
        return {str(path): sizes[str(path)] for path in paths}

    monkeypatch.setattr(km, "load_index", lambda *args, **kwargs: {})
    monkeypatch.setattr(km, "model_sizes", measure)
    monkeypatch.setattr(km, "footprint_sizes", lambda *args, **kwargs: {})
    return tmp_path, add, measured


def _evidence(kind, **changes):
    return dict({
        "kind": kind, "class": kind, "package": "0603 (approx)",
        "length_mm": 1.6, "width_mm": .8, "bbox_basis": "body",
        "presence_confidence": "high", "class_confidence": "medium",
        "confidence": "high", "pin_count": 2, "pin_count_confidence": "high",
        "pin_count_source": "complete_lead_observation",
        "terminal_count": 2, "terminal_count_basis": "visible_including_tabs",
        "terminal_layout": "two metal endcaps left and right",
    }, **changes)


def _names(kind):
    return ("Resistor", "R") if kind == "resistor" else ("Capacitor", "C")


@pytest.mark.parametrize("kind", ["resistor", "capacitor"])
def test_cold_declared_chip_catalog_is_exhaustive_before_unique_estimate(cold_catalog, kind):
    root, add, measured = cold_catalog
    family, prefix = _names(kind)
    good = add(family + "_SMD", prefix + "_0603_1608Metric", [1.6, .8, .4])
    small = add(family + "_SMD", prefix + "_0402_1005Metric", [1, .5, .3])
    large = add(family + "_SMD", prefix + "_1206_3216Metric", [3.2, 1.6, .6])
    other_family, other_prefix = _names("capacitor" if kind == "resistor" else "resistor")
    incompatible = {
        add(other_family + "_SMD", other_prefix + "_0603_1608Metric", [1.6, .8, .4]),
        add(family + "_THT", prefix + "_Axial_1.6x0.8mm", [1.6, .8, .4]),
        add("Capacitor_SMD", "CP_Elec_1.6x0.8mm", [1.6, .8, .4]),
        add("Resistor_SMD", "R_Trimmer_1.6x0.8mm", [1.6, .8, .4]),
        add(family + "_SMD", prefix + "_Array_1.6x0.8mm", [1.6, .8, .4]),
    }
    component = _evidence(kind)
    before = copy.deepcopy(component)
    report = km.model_candidates(component, library=root)

    assert set(measured) == {good, small, large}
    assert not incompatible.intersection(measured)
    assert len(measured) <= km.MAX_CANDIDATE_MEASUREMENTS
    assert report["discovery_scope"] == "standard_chip_endcaps"
    assert not report.get("search_truncated")
    assert [row["path"] for row in report["candidates"]] == [str(good)]
    assert report["status"] == "auto_matched"
    assert report["selected_path"] == str(good)
    assert component == before


@pytest.mark.parametrize("kind", ["resistor", "capacitor"])
@pytest.mark.parametrize("limit", [1, 5])
def test_approximate_0603_keeps_compatible_0805_and_never_claims_uniqueness(
        cold_catalog, kind, limit):
    root, add, measured = cold_catalog
    family, prefix = _names(kind)
    small = add(family + "_SMD", prefix + "_0603_1608Metric", [1.6, .8, .4])
    adjacent = add(family + "_SMD", prefix + "_0805_2012Metric", [2, 1.25, .5])
    far = add(family + "_SMD", prefix + "_0402_1005Metric", [1, .5, .3])
    component = _evidence(kind, length_mm=1.8, width_mm=1.0)
    assert km.size_distance([1.6, .8], 1.8, 1.0) <= km.DEFAULT_SIZE_TOLERANCE
    assert km.size_distance([2, 1.25], 1.8, 1.0) <= km.DEFAULT_SIZE_TOLERANCE

    report = km.model_candidates(component, library=root, limit=limit)

    assert set(measured) == {small, adjacent, far}
    assert report["discovery_scope"] == "standard_chip_endcaps"
    assert not report.get("search_truncated")
    assert report["status"] == "review_required"
    assert report["selected_path"] is None
    if limit == 5:
        assert {row["path"] for row in report["candidates"]} == {str(small), str(adjacent)}
    else:
        assert len(report["candidates"]) == 1  # UI truncation is not search uniqueness.


@pytest.mark.parametrize("kind", ["resistor", "capacitor"])
def test_unreadable_standard_alternative_prevents_automatic_choice(cold_catalog, kind):
    root, add, measured = cold_catalog
    family, prefix = _names(kind)
    good = add(family + "_SMD", prefix + "_0603_1608Metric", [1.6, .8, .4])
    unreadable = add(family + "_SMD", prefix + "_0805_2012Metric", None)

    report = km.model_candidates(_evidence(kind), library=root)

    assert set(measured) == {good, unreadable}
    assert [row["path"] for row in report["candidates"]] == [str(good)]
    assert report["status"] == "review_required"
    assert report["selected_path"] is None


@pytest.mark.parametrize("kind", ["resistor", "capacitor"])
def test_standard_chip_suffix_variants_remain_plausible_alternatives(cold_catalog, kind):
    root, add, measured = cold_catalog
    family, prefix = _names(kind)
    standard = add(family + "_SMD", prefix + "_0603_1608Metric", [1.6, .8, .4])
    variant = add(family + "_SMD", prefix + "_0603_1608Metric_Tall", [1.6, .8, .7])

    report = km.model_candidates(_evidence(kind), library=root)

    assert set(measured) == {standard, variant}
    assert report["discovery_scope"] == "standard_chip_endcaps"
    assert {row["path"] for row in report["candidates"]} == {str(standard), str(variant)}
    assert not report.get("search_truncated")
    assert report["status"] == "review_required"
    assert report["selected_path"] is None


@pytest.mark.parametrize("kind", ["resistor", "capacitor"])
@pytest.mark.parametrize("changes,remove_layout", [
    ({"package": ""}, False),
    ({"terminal_layout": ""}, False),
    ({}, True),
    ({"class": "unknown"}, False),
    ({"package": "0603/0805"}, False),
])
def test_missing_or_ambiguous_chip_evidence_keeps_conservative_fallback(
        cold_catalog, kind, changes, remove_layout):
    root, add, measured = cold_catalog
    family, prefix = _names(kind)
    good = add(family + "_SMD", prefix + "_0603_1608Metric", [1.6, .8, .4])
    far = add(family + "_SMD", prefix + "_0402_1005Metric", [1, .5, .3])
    component = _evidence(kind, **changes)
    if remove_layout:
        component.pop("terminal_layout")

    report = km.model_candidates(component, library=root)

    assert measured == [good]
    assert far not in measured
    assert report.get("discovery_scope") != "standard_chip_endcaps"
    assert report.get("search_truncated")
    assert report["status"] == "review_required"
    assert report["selected_path"] is None


@pytest.mark.parametrize("kind", ["resistor", "capacitor"])
def test_real_models_beyond_measurement_budget_do_not_manufacture_uniqueness(cold_catalog, kind):
    root, add, measured = cold_catalog
    family, prefix = _names(kind)
    # Separate models in the canonical family folder count toward the CAD
    # budget, including suffix variants whose envelopes are not yet measured.
    good = add(family + "_SMD", prefix + "_0603_1608Metric", [1.6, .8, .4])
    variants = [add(family + "_SMD", prefix + f"_0603_1608Metric_Variant{i:03d}",
                    [3.2, 1.6, .6]) for i in range(km.MAX_CANDIDATE_MEASUREMENTS)]
    paths = [good, *variants]

    report = km.model_candidates(_evidence(kind), library=root)

    assert len(set(paths)) == km.MAX_CANDIDATE_MEASUREMENTS + 1
    assert len(measured) == len(set(measured)) == km.MAX_CANDIDATE_MEASUREMENTS
    assert paths[0] in measured
    assert report["discovery_scope"] == "standard_chip_endcaps"
    assert report.get("search_truncated")
    assert [row["path"] for row in report["candidates"]] == [str(paths[0])]
    assert report["status"] == "review_required"
    assert report["selected_path"] is None


@pytest.mark.parametrize("kind", ["resistor", "capacitor"])
@pytest.mark.parametrize("layout", [
    "No endcaps visible; two projecting leads",
    "two metal endcaps, not visible",
    "two metal endcaps; without a clear observation",
    "two metal endcaps; neither one is confirmed",
    "two endcaps; terminal arrangement unknown",
    "two metal endcaps (uncertain)",
    "two endcaps; unclear left side",
    "two endcaps, partly occluded",
    "two metal endcaps hidden by solder",
    "two endcaps absent from this view",
    "two endcaps or projecting leads",
    "two endcaps, maybe",
    "two endcaps, possibly",
    "possibly two metal endcaps",
    "endcaps at both ends",
    "two terminals, endcaps assumed",
    "one endcap visible",
    "three endcaps",
])
def test_negated_uncertain_or_unsupported_endcap_prose_does_not_narrow_discovery(
        cold_catalog, kind, layout):
    root, add, measured = cold_catalog
    family, prefix = _names(kind)
    good = add(family + "_SMD", prefix + "_0603_1608Metric", [1.6, .8, .4])
    far = add(family + "_SMD", prefix + "_0402_1005Metric", [1, .5, .3])

    report = km.model_candidates(_evidence(kind, terminal_layout=layout), library=root)

    assert measured == [good]
    assert far not in measured
    assert report.get("discovery_scope") != "standard_chip_endcaps"
    assert report.get("search_truncated")
    assert report["status"] == "review_required"
    assert report["selected_path"] is None


@pytest.mark.parametrize("kind", ["resistor", "capacitor"])
@pytest.mark.parametrize("layout", [
    "two endcaps",
    "2 metal endcaps left and right",
    "two metallized endcaps on opposite ends",
    "two plated endcaps",
    "two tinned endcaps",
    "two opposing endcaps",
    "two opposed endcaps",
    "two opposite endcaps",
    "two visible endcaps",
    "2 visible opposing metallized endcaps left and right",
    "two metal end-caps left and right",
    "two metal end caps left and right",
    "  TWO METAL ENDCAPS LEFT AND RIGHT  ",
])
def test_explicit_affirmative_endcap_grammar_supports_exhaustive_chip_scope(
        cold_catalog, kind, layout):
    root, add, measured = cold_catalog
    family, prefix = _names(kind)
    good = add(family + "_SMD", prefix + "_0603_1608Metric", [1.6, .8, .4])
    far = add(family + "_SMD", prefix + "_0402_1005Metric", [1, .5, .3])

    report = km.model_candidates(_evidence(kind, terminal_layout=layout), library=root)

    assert set(measured) == {good, far}
    assert report["discovery_scope"] == "standard_chip_endcaps"
    assert not report.get("search_truncated")
    assert report["status"] == "auto_matched"
    assert report["selected_path"] == str(good)
