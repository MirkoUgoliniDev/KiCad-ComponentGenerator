"""@brief Review-first candidate matching against a mocked KiCad library."""

import json

import pytest

from kicadgen.cad3d import kicad_models as km


@pytest.fixture
def library(tmp_path, monkeypatch):
    sizes = {}
    measured = []

    def add(folder, name, size=None):
        path = tmp_path / (folder + ".3dshapes") / (name + ".step")
        path.parent.mkdir(exist_ok=True)
        path.touch()
        sizes[str(path)] = size if size is not None else {"body": [1.6, .8, .4], "total": [1.6, .8, .4]}
        return path

    def measure(paths, library=None):
        measured.extend(paths)
        return {str(p): sizes[str(p)] for p in paths}

    monkeypatch.setattr(km, "model_sizes", measure)
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {})
    return tmp_path, add, measured


def component(**kwargs):
    return dict(dict(kind="resistor", package="0603", length_mm=1.6,
                     width_mm=.8, bbox_basis="body"), **kwargs)


def test_structural_filters_before_measurement(library):
    root, add, measured = library
    good = add("Resistor_SMD", "R_0603_1608Metric")
    add("Mounting", "Screw_ABC0603")
    add("Mounting", "Screw_0603")
    add("Fuse", "Fuse_0603")
    add("Resistor_SMD", "R_Array_0603")
    add("Capacitor_SMD", "C_0603")
    report = km.model_candidates(component(), library=root)
    assert measured == [good]
    assert report["status"] == "review_required"
    assert report["selected_path"] is None
    assert [c["path"] for c in report["candidates"]] == [str(good)]
    json.dumps(report, allow_nan=False)


def test_capacitor_never_matches_fuse(library):
    root, add, measured = library
    add("Fuse", "Fuse_0603")
    good = add("Capacitor_SMD", "C_0603")
    assert km.model_candidates(component(kind="capacitor"), library=root)["candidates"][0]["path"] == str(good)
    assert measured == [good]


def test_unknown_family_does_not_admit_hardware_or_unknown_numeric_models(library):
    root, add, measured = library
    add("Mounting", "Screw_0603")
    add("Custom", "Mystery_0603")
    assert km.model_candidates(component(kind="unknown"), library=root)["status"] == "unresolved"
    assert not measured


@pytest.mark.parametrize("basis,expected", [("body", "review_required"), ("total", "unresolved"),
                                           ("leads", "unresolved"), ("body_and_leads", "unresolved"),
                                           ("pads", "unresolved"), ("unknown", "review_required")])
def test_measurement_basis_is_strict(library, basis, expected):
    root, add, _ = library
    add("Resistor_SMD", "R_0603", {"body": [1.6, .8], "total": [3, 2]})
    assert km.model_candidates(component(bbox_basis=basis), library=root)["status"] == expected


def test_no_forced_alternatives_and_bounded_measurement(library):
    root, add, measured = library
    for i in range(100):
        add("Resistor_SMD", f"R_0603_{i:03}")
    report = km.model_candidates(component(), limit=999, library=root)
    assert len(report["candidates"]) == 5
    assert len(measured) == km.MAX_CANDIDATE_MEASUREMENTS
    assert km.model_candidates(component(length_mm=10), library=root)["candidates"] == []


def test_attach_stores_review_report_and_clears_stale_import(library):
    root, add, _ = library
    add("Resistor_SMD", "R_0603")
    c = component(model_path="stale.step", model_rotation_deg=90)
    assert km.attach_models([c], library=root) == 0
    assert c["model_match"]["status"] == "review_required"
    assert c["color_hex"] == "#FF00FF"
    assert "model_path" not in c and c["model_rotation_deg"] == 90


def test_explicit_selection_and_local_rotation(library):
    root, add, measured = library
    selected = add("Custom", "ExactChoice")
    c = component(rotation_deg=37, model_selection={"status": "confirmed", "path": str(selected), "rotation_deg": 90})
    assert km.attach_models([c], library=root) == 1
    assert c["model_path"] == str(selected)
    assert c["model_rotation_deg"] == 90 and c["rotation_deg"] == 37
    assert c["model_match"]["status"] == "confirmed"
    assert c["model_match"]["selected_path"] == str(selected)
    assert not measured


@pytest.mark.parametrize("rotation_fields,expected", [({}, None),
    ({"rotation_deg": None}, None), ({"rotation_deg": 0}, 0)])
def test_confirmed_automatic_rotation_is_distinct_from_explicit_zero(library, rotation_fields, expected):
    root, add, measured = library
    selected = add("Custom", "ExactChoice")
    c = component(rotation_deg=37, model_rotation_deg=90,
        model_selection={"status": "confirmed", "path": str(selected), **rotation_fields})
    assert km.attach_models([c], library=root) == 1
    assert c["model_path"] == str(selected) and c["model_match"]["status"] == "confirmed"
    assert c["rotation_deg"] == 37
    if expected is None:
        assert "model_rotation_deg" not in c
    else:
        assert c["model_rotation_deg"] == 0
    assert not measured


@pytest.mark.parametrize("status,path,rotation", [("rejected", "R_0603.step", 0),
    ("confirmed", "missing.step", 0), ("confirmed", "", 0), ("confirmed", "R_0603.step", float("nan"))])
def test_rejected_or_invalid_selection_never_substituted(library, status, path, rotation):
    root, add, measured = library
    real = add("Resistor_SMD", "R_0603")
    c = component(model_path=str(real), model_selection={"status": status,
        "path": str(real.parent / path), "rotation_deg": rotation})
    assert km.attach_models([c], library=root) == 0
    assert c["model_match"]["status"] == "unresolved"
    assert c["color_hex"] == "#FF00FF"
    assert "model_path" not in c and not measured


def test_real_size_cache_reused(tmp_path, monkeypatch):
    path = tmp_path / "Resistor_SMD.3dshapes" / "R_0603.step"
    path.parent.mkdir()
    path.touch()
    monkeypatch.setattr(km, "load_index", lambda *a: {km.index_key(path): {"body": [1.6, .8]}})
    monkeypatch.setattr(km, "measure_models", lambda *a: pytest.fail("Cached sizes must not be remeasured"))
    assert km.model_candidates(component(), library=tmp_path)["status"] == "review_required"


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf"), "invalid"])
def test_invalid_dimensions_are_json_safe_and_do_not_measure(library, value):
    root, add, measured = library
    add("Resistor_SMD", "R_0603")
    result = km.model_candidates(component(length_mm=value), library=root)
    assert result["status"] == "unresolved" and not measured
    json.dumps(result, allow_nan=False)


def test_named_package_numbers_have_boundaries(library):
    root, add, _ = library
    good = add("Package_TO_SOT_SMD", "SOT-23")
    add("Package_TO_SOT_SMD", "SOT-230")
    assert km.candidates("SOT-23", root) == [good]


def test_ambiguous_topologies_are_candidates_not_confirmation(library):
    root, add, _ = library
    for count in (24, 32):
        add("Package_DFN_QFN", f"QFN-{count}")
    c = component(kind="ic_qfn", package="QFN")
    report = km.model_candidates(c, library=root)
    assert len(report["candidates"]) == 2
    assert report["status"] == "review_required" and report["selected_path"] is None
    c.update(pin_count=32, pin_count_confidence="high")
    report = km.model_candidates(c, library=root)
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["package"] == "QFN-32"


def test_package_family_filters_even_without_kind(library):
    root, add, measured = library
    good = add("Resistor_SMD", "R_0603")
    add("Fuse", "R_0603_Fuse")
    result = km.model_candidates(component(kind="unknown", package="R_0603"), library=root)
    assert [c["path"] for c in result["candidates"]] == [str(good)]
    assert measured == [good]


def test_missing_body_does_not_use_total(library):
    root, add, _ = library
    add("Resistor_SMD", "R_0603", [1.6, .8])
    assert km.model_candidates(component(), library=root)["candidates"] == []


def test_pads_require_a_measured_solid_and_keep_variants_separate(library, monkeypatch):
    root, add, _ = library
    p = add("Resistor_SMD", "R_0603", {"total": [4, 3]})
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {str(p): [
        {"source": "A", "size": [1.6, 4]}, {"source": "B", "size": [4, .8]}]})
    assert km.model_candidates(component(bbox_basis="pads"), library=root)["candidates"] == []
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {str(p): [
        {"source": "C", "size": [1.6, .8]}]})
    assert km.model_candidates(component(bbox_basis="pads"), library=root)["candidates"][0]["measurement_basis"] == "piazzole C"
    monkeypatch.setattr(km, "model_sizes", lambda *a, **k: {})
    assert km.model_candidates(component(bbox_basis="pads"), library=root)["candidates"] == []
