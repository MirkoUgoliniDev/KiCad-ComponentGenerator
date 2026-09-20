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


def supported(**kwargs):
    return component(**dict(dict(presence_confidence="high", class_confidence="medium",
        pin_count=2, pin_count_confidence="high", pin_count_source="photo_observation",
        terminal_count=2), **kwargs))


@pytest.mark.parametrize("package", ["", "two-terminal chip, approximately 1.6x0.8mm"])
def test_family_fallback_and_automatic_mechanical_estimate(library, package):
    root, add, measured = library
    good = add("Resistor_SMD", "R_0603_1608Metric")
    add("Capacitor_SMD", "C_0603_1608Metric")
    c = supported(package=package, rotation_deg=37, model_rotation_deg=90)
    before = {key: c[key] for key in ("length_mm", "width_mm", "bbox_basis", "pin_count", "terminal_count")}
    assert km.attach_models([c], library=root) == 1
    assert c["model_match"]["status"] == "auto_matched"
    assert c["model_match"]["discovery"] == "family_dimensions"
    assert c["model_path"] == str(good)
    assert "model_selection" not in c and "model_rotation_deg" not in c
    assert c["rotation_deg"] == 37
    assert {key: c[key] for key in before} == before
    assert all(km._candidate_family(p) == "resistor" for p in measured)
    assert "identity and variant not verified" in c["model_match"]["candidates"][0]["reasons"][-1]


def test_literal_marking_selects_user_installed_module_model(library):
    root, add, measured = library
    exact = add("espressif", "ESP32-S3-WROOM-1U",
                {"total": [19.2, 18.0, 3.2], "body": [19.2, 18.0, 3.2]})
    component = dict(
        kind="module", **{"class": "rf_module"},
        package="Shielded castellated SMD module with U.FL/IPEX antenna connector",
        marking="ESPRESSIF ESP32-S3-WROOM-1U FCC ID:2AC7Z-ESPS3WROOM1U",
        length_mm=19.2, width_mm=18.0, bbox_basis="total",
        presence_confidence="high", class_confidence="high",
    )
    assert km.attach_models([component], library=root) == 1
    assert component["model_match"]["status"] == "auto_matched"
    assert component["model_path"] == str(exact)
    assert component["model_match"]["discovery_scope"] == "literal_marking_model"
    assert measured == [exact]


@pytest.mark.parametrize("fields", [
    {"presence_confidence": "low"}, {"presence_confidence": None},
    {"class_confidence": "low"}, {"class_confidence": None},
    {"_yolo_review_only": True}, {"_yolo_origin": "low_confidence_review"},
    {"kind": "unknown"}, {"kind": "other"}, {"kind": "semiconductor_unknown"},
    {"bbox_basis": "unknown"}, {"pin_count_confidence": "low"},
    {"terminal_count": 3}, {"terminal_count": True}, {"pin_count": 3}])
def test_auto_requires_independent_supported_evidence(library, fields):
    root, add, _ = library
    add("Resistor_SMD", "R_0603")
    c = supported(**fields)
    assert km.attach_models([c], library=root) == 0
    assert c["model_match"]["status"] != "confirmed"
    assert c["model_match"]["selected_path"] is None


def test_auto_never_uses_ui_limit_to_hide_ambiguity(library):
    root, add, _ = library
    for i in range(5):
        add("Resistor_SMD", f"R_0603_variant{i}")
    c = supported()
    assert len(km.model_candidates(c, library=root)["candidates"]) == 5
    report = km.model_candidates(c, library=root, limit=1)
    assert report["status"] == "review_required" and report["selected_path"] is None


def test_truncated_family_search_never_auto_selects(library):
    root, add, measured = library
    good = add("Resistor_SMD", "R_0603_1608Metric")
    for i in range(80):
        add("Resistor_SMD", f"Other_{i}", {"body": [10, 10]})
    report = km.model_candidates(supported(package=""), library=root)
    assert measured[0] == good
    assert measured == [good]
    assert report["search_truncated"] and report["status"] == "review_required"


def test_unreadable_alternative_prevents_auto(library, monkeypatch):
    root, add, _ = library
    good = add("Resistor_SMD", "R_0603")
    add("Resistor_SMD", "R_0603_Unreadable")
    monkeypatch.setattr(km, "model_sizes", lambda *a: {str(good): {"body": [1.6, .8]}})
    assert km.model_candidates(supported(), library=root)["status"] == "review_required"


@pytest.mark.parametrize("status", ["rejected", "confirmed"])
def test_explicit_choice_wins_over_auto_eligible(library, status):
    root, add, measured = library
    add("Resistor_SMD", "R_0603")
    choice = add("Custom", "Chosen")
    c = supported(model_selection={"status": status, "path": str(choice), "rotation_deg": 0})
    assert km.attach_models([c], library=root) == (status == "confirmed")
    assert not measured
    assert c["model_match"]["status"] == ("confirmed" if status == "confirmed" else "unresolved")


@pytest.mark.parametrize("text", ["SOT23/SC70", "SOT-23-6 / SC-70-6 class",
    "small outline (SOT23 or SC70)"])
def test_sot_sc_ambiguous_prose_preserved(text):
    assert km._package_token(text) == ""


def test_sc_terminal_variant_and_metric_namespace(library):
    root, add, _ = library
    good = add("Package_TO_SOT_SMD", "SC-70-6")
    add("Package_TO_SOT_SMD", "SC-70-5")
    c = component(kind="semiconductor_unknown", package="small outline SC70-6 package",
                  pin_count=6, pin_count_confidence="high")
    assert [x["path"] for x in km.model_candidates(c, library=root)["candidates"]] == [str(good)]
    assert km._package_token("chip 0603 metric") == "0603Metric"


def test_ambiguous_prose_searches_only_declared_alternatives(library):
    root, add, measured = library
    expected = {add("Package_TO_SOT_SMD", "SOT-23-6"),
                add("Package_TO_SOT_SMD", "SC-70-6")}
    add("Package_TO_SOT_SMD", "SOT-223-6")
    add("Package_DFN_QFN", "QFN-6_1.6x0.8mm")
    c = supported(kind="ic", package="small outline SOT23-6 / SC70-6 class",
                  pin_count=6, terminal_count=6)
    report = km.model_candidates(c, library=root)
    assert set(measured) == expected
    assert report["status"] == "review_required"
    assert {item["path"] for item in report["candidates"]} == {str(p) for p in expected}


def test_cached_family_dimensions_keep_basis_and_allow_complete_search(library, monkeypatch):
    root, add, measured = library
    good = add("Resistor_SMD", "R_0603_1608Metric")
    bad = add("Resistor_SMD", "Other", {"body": [10, 10], "total": [1.6, .8]})
    monkeypatch.setattr(km, "load_index", lambda *a: {
        km.index_key(good): {"body": [1.6, .8]},
        km.index_key(bad): {"body": [10, 10], "total": [1.6, .8]}})
    report = km.model_candidates(supported(package=""), library=root)
    assert measured == [good]
    assert report["status"] == "auto_matched" and not report.get("search_truncated")


def test_cold_structured_switch_discovery_is_bounded_and_never_proves_uniqueness(library):
    root, add, measured = library
    for i in range(90):
        add("Button_Switch_SMD", f"Switch_Vendor{i}")
    report = km.model_candidates(component(kind="switch", package="small tactile switch"), library=root)
    assert len(measured) == km.MAX_CANDIDATE_MEASUREMENTS
    assert report["status"] == "review_required"
    assert report["selected_path"] is None
    assert report["search_truncated"]


def test_structural_rejection_logging_is_bounded(library):
    root, add, _ = library
    add("Resistor_SMD", "R_0603")
    for i in range(100):
        add("Fuse", f"Fuse_0603_{i}")
    logs = []
    report = km.model_candidates(component(), library=root, log=logs.append)
    assert report["rejected_candidate_count"] == 100
    assert len(logs) == 2


@pytest.mark.parametrize("observed_class", ["semiconductor_unknown", "transistor", "regulator"])
def test_production_ic_kind_preserves_semiconductor_class(library, monkeypatch, observed_class):
    root, add, measured = library
    sot = add("Package_TO_SOT_SMD", "SOT-23-6")
    wrong_count = add("Package_TO_SOT_SMD", "SOT-23-5")
    so = add("Package_SO", "SOIC-6")
    monkeypatch.setattr(km, "load_index", lambda *a: {
        km.index_key(p): {"body": [1.6, .8]} for p in (sot, wrong_count, so)})
    c = supported(kind="ic", package="", pin_count=6, terminal_count=6,
                  **{"class": observed_class})
    report = km.model_candidates(c, library=root)
    expected = {sot, so} if observed_class == "semiconductor_unknown" else {sot}
    assert set(measured) == expected
    assert {x["path"] for x in report["candidates"]} == {str(p) for p in expected}
    assert c["kind"] == "ic" and c["class"] == observed_class
    if observed_class == "semiconductor_unknown":
        assert report["status"] == "review_required"


@pytest.mark.parametrize("package", ["QFN-style leadless body", "QFN/LQFN-style plastic package"])
def test_semiconductor_unknown_class_keeps_qfn_prose_narrowing(library, monkeypatch, package):
    root, add, measured = library
    qfn = add("Package_DFN_QFN", "QFN-6")
    sot = add("Package_TO_SOT_SMD", "SOT-23-6")
    so = add("Package_SO", "SOIC-6")
    monkeypatch.setattr(km, "load_index", lambda *a: {
        km.index_key(p): {"body": [1.6, .8]} for p in (qfn, sot, so)})
    c = supported(kind="ic", package=package, pin_count=6, terminal_count=6,
                  **{"class": "semiconductor_unknown"})
    report = km.model_candidates(c, library=root)
    assert measured == [qfn]
    assert report["status"] == "review_required" and report["selected_path"] is None


@pytest.mark.parametrize("class_fields", [{}, {"class": "ic"}, {"class": "semiconductor_unknown"}])
def test_generic_ic_never_fabricates_unique_tssop_by_omitting_sot(library, monkeypatch, class_fields):
    root, add, measured = library
    tssop = add("Package_SO", "TSSOP-6")
    sot = add("Package_TO_SOT_SMD", "SOT-23-6")
    monkeypatch.setattr(km, "load_index", lambda *a: {
        km.index_key(p): {"body": [1.6, .8]} for p in (tssop, sot)})
    c = supported(kind="ic", package="", pin_count=6, terminal_count=6, **class_fields)
    assert km.attach_models([c], library=root) == 0
    assert set(measured) == {tssop, sot}
    report = c["model_match"]
    assert {x["path"] for x in report["candidates"]} == {str(tssop), str(sot)}
    assert report["status"] == "review_required" and report["selected_path"] is None
    assert "model_path" not in c


@pytest.mark.parametrize("count,layout", [
    (4, "three broad leads on one edge and one wide tab on the opposite edge"),
    (6, "three visible terminals on each of two opposite sides")])
def test_provisional_observed_morphology_filters_unknown_class(library, monkeypatch, count, layout):
    root, add, measured = library
    good = add("Package_TO_SOT_SMD", f"SOT-23-{count}")
    htssop = add("Package_SO", "HTSSOP-24-1EP_4.4x7.8mm")
    bga = add("Package_BGA", "NXP_SOT1982-1_VFBGA-98_7x7mm")
    monkeypatch.setattr(km, "load_index", lambda *a: {
        km.index_key(p): {"body": [1.6, .8]} for p in (good, htssop, bga)})
    c = supported(kind="ic", package="", confidence="low", class_confidence="low",
                  pin_count=count, pin_count_confidence="low", terminal_count=count,
                  terminal_layout=layout, **{"class": "semiconductor_unknown"})
    assert km._observed_count(c) == set()
    assert km.attach_models([c], library=root) == 0
    assert measured == [good]
    assert c["model_match"]["status"] == "review_required"
    assert c["model_match"]["rejected_candidate_count"] == 2
    assert c["terminal_count"] == count and c["pin_count_confidence"] == "low"


@pytest.mark.parametrize("fields", [
    {"presence_confidence": "low"}, {"terminal_layout": ""},
    {"terminal_layout": "unknown"}, {"terminal_layout": None},
    {"terminal_count": True}, {"terminal_count": 0}, {"terminal_count": "4"}])
def test_provisional_count_requires_presence_and_valid_observed_layout(library, fields):
    _, add, _ = library
    path = add("Package_BGA", "BGA-98")
    c = component(**dict(dict(kind="ic", pin_count=4, pin_count_confidence="low",
        terminal_count=4, terminal_layout="three leads and one tab", presence_confidence="high"), **fields))
    assert km._compatible_candidates([path], c, lambda message: None) == [path]


def test_provisional_conflict_does_not_override_confident_count(library):
    _, add, _ = library
    paths = [add("Package_SO", "TSSOP-6"), add("Package_SO", "TSSOP-4")]
    c = supported(kind="ic", pin_count=6, terminal_count=4,
                  terminal_layout="three leads and one tab")
    assert km._compatible_candidates(paths, c, lambda message: None) == []


def test_provisional_count_alone_never_enables_auto(library):
    root, add, _ = library
    add("Package_SO", "TSSOP-6")
    c = supported(kind="ic", package="TSSOP-6", pin_count=6, terminal_count=6,
                  pin_count_confidence="low", terminal_layout="three leads each side")
    assert km.attach_models([c], library=root) == 0
    assert c["model_match"]["status"] == "review_required"


@pytest.mark.parametrize("name,expected", [
    ("HTSSOP-24-1EP_4.4x7.8mm", 24),
    ("NXP_SOT1982-1_VFBGA-98_7x7mm", 98),
    ("Vendor_TQFP-48_7x7mm", 48), ("Vendor_LQFP-64_10x10mm", 64),
    ("NXP_SOT1982-1", None), ("Vendor_TO-263-3_TabPin2", None),
    ("VendorNotVFBGA-98", None), ("Vendor_VFBGA-98ABC", None),
    ("SOT-23-6", 6), ("SOT-223", None)])
def test_bounded_embedded_package_count_tokens(name, expected):
    from pathlib import Path
    assert km._declared_count(Path(name + ".step")) == expected


def test_confident_raw_u4_count_filters_vendor_packages(library):
    _, add, _ = library
    htssop = add("Package_SO", "HTSSOP-24-1EP_4.4x7.8mm")
    bga = add("Package_BGA", "NXP_SOT1982-1_VFBGA-98_7x7mm")
    tab = add("Package_TO_SOT_SMD", "TO-263-3_TabPin2")
    c = component(kind="ic", pin_count=4, pin_count_confidence="medium",
                  pin_count_source="photo_observation", confidence="medium",
                  terminal_count=4, presence_confidence="high", class_confidence="low",
                  terminal_layout="three leads and a single wide tab",
                  **{"class": "semiconductor_unknown"})
    assert km._observed_count(c) == {4}
    assert km._compatible_candidates([htssop, bga, tab], c, lambda message: None) == [tab]


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
