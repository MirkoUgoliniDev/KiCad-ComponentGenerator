"""@brief Type and observed-terminal vetoes must precede dimensional matching."""

import pytest

from kicadgen.cad3d import kicad_models as km


@pytest.fixture
def resolver(tmp_path, monkeypatch):
    """Return candidate count; every unconfirmed attachment stays synthetic.

    Contratto aggiornato: il segnaposto magenta e' solo per chi non ha alcun
    candidato (unresolved). Con candidati il corpo generico tiene il colore
    del rilevatore e lo stato si legge da model_review / render_placeholder.
    """
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {})
    monkeypatch.setattr(km, "model_sizes", lambda paths, library=None: {str(p): [3, 2, .5] for p in paths})
    def run(folder, package, **evidence):
        directory = tmp_path / (folder + ".3dshapes")
        directory.mkdir(exist_ok=True)
        path = directory / (package + ".step")
        path.touch()
        component = dict(package=package, length_mm=3, width_mm=2, color_hex="#303030", **evidence)
        logs = []
        count = km.attach_models([component], library=tmp_path, log=logs.append)
        assert count == 0
        assert "model_path" not in component
        report = component["model_match"]
        assert report["selected_path"] is None
        assert report["status"] == ("review_required" if report["candidates"] else "unresolved")
        assert component["model_review"] == report["status"]
        assert component["render_placeholder"] is (not report["candidates"])
        assert component["color_hex"] == ("#303030" if report["candidates"] else "#FF00FF")
        return len(report["candidates"]), component, logs, path
    return run


@pytest.mark.parametrize("folder,package,evidence", [
    ("Diode_SMD", "SOD-123", {"kind": "resistor"}),
    ("Diode_SMD", "SOD-123", {"kind": "generic", "detected_kind": "resistor"}),
    ("Diode_SMD", "SOD-123", {"kind": "generic", "_detected_kind": "resistor"}),
    ("Resistor_SMD", "R_0603", {"kind": "capacitor"}),
    ("Capacitor_SMD", "C_0603", {"kind": "resistor"}),
    ("Package_SO", "SOIC-8", {"kind": "generic", "detected_kind": "ic_qfn"}),
    ("Package_DFN_QFN", "QFN-8", {"kind": "ic_soic"}),
    ("Resistor_SMD", "R_0603", {"kind": "resistor", "detected_kind": "capacitor"}),
])
def test_type_conflicts_veto_even_a_perfect_size_match(resolver, folder, package, evidence):
    count, component, logs, _ = resolver(folder, package, **evidence)
    assert count == 0
    assert "model_path" not in component
    assert any("rifiutato" in log and "il solo ingombro" in log for log in logs)


@pytest.mark.parametrize("kind", ["ic", "ic_soic", "generic", "unknown", "novel_unmapped_kind"])
def test_valid_ic_and_no_type_evidence_remain_compatible(resolver, kind):
    count, component, _, path = resolver("Package_SO", "SOIC-8", kind=kind)
    assert count == 1 and component["model_match"]["candidates"][0]["path"] == str(path)


def test_generic_without_evidence_preserves_existing_package_resolution(resolver):
    assert resolver("Diode_SMD", "SOD-123", kind="generic")[0] == 1


def test_unknown_library_family_is_not_assigned_an_invented_type(resolver):
    assert resolver("Custom", "Unclassified", kind="resistor")[0] == 1


@pytest.mark.parametrize("observed,confidence,expected", [
    (8, "high", 1), (16, "high", 0), (16, "medium", 0),
    (16, "low", 1), (16, None, 1), (True, "high", 1), ("16", "high", 1),
])
def test_only_confident_explicit_integer_counts_can_veto(resolver, observed, confidence, expected):
    assert resolver("Package_SO", "SOIC-8", kind="ic", pin_count=observed,
                    pin_count_confidence=confidence)[0] == expected


def geometry(count, confidence="high"):
    return {"confidence": confidence, "source": "photo_observation",
            "body_bbox": [.2, .2, .8, .8],
            "terminals": [{"bbox": [0, i/count, .2, (i+.5)/count]} for i in range(count)]}


@pytest.mark.parametrize("count,confidence,expected", [(8, "high", 1), (6, "high", 0), (6, "low", 1)])
def test_confident_observed_terminal_geometry_count(resolver, count, confidence, expected):
    result = resolver("Package_SO", "SOIC-8", kind="ic", terminal_geometry=geometry(count, confidence))
    assert result[0] == expected
    if not expected:
        assert any("terminali osservati 6, package dichiara 8" in line for line in result[2])


def test_disagreeing_trustworthy_counts_refuse_import(resolver):
    assert resolver("Package_SO", "SOIC-8", kind="ic", pin_count=8,
                    pin_count_confidence="high", terminal_geometry=geometry(6))[0] == 0


def test_package_number_is_not_a_terminal_count(resolver):
    assert resolver("Package_TO_SOT_SMD", "SOT-23", kind="generic", pin_count=3,
                    pin_count_confidence="high")[0] == 1


def test_general_component_confidence_does_not_certify_pin_count(resolver):
    assert resolver("Package_SO", "SOIC-8", kind="ic", pin_count=16, confidence="high")[0] == 1
    assert resolver("Package_SO", "SOIC-8", kind="ic", pin_count=16, confidence="high",
                    pin_count_source="photo_observation")[0] == 0


def test_type_veto_clears_a_previously_attached_model_path(resolver):
    count, component, _, _ = resolver("Diode_SMD", "SOD-123", kind="resistor", model_path="old.step")
    assert count == 0 and "model_path" not in component


def test_explicitly_unresolved_geometry_is_not_a_count(resolver):
    observation = geometry(6)
    observation["resolved"] = False
    assert resolver("Package_SO", "SOIC-8", kind="ic", terminal_geometry=observation)[0] == 1


def test_invalid_or_duplicate_terminal_boxes_are_not_count_evidence(resolver):
    observation = geometry(6)
    observation["terminals"] = [observation["terminals"][0]] * 6
    assert resolver("Package_SO", "SOIC-8", kind="ic", terminal_geometry=observation)[0] == 1


def test_type_gate_does_not_relax_dimensional_gate(resolver, monkeypatch):
    monkeypatch.setattr(km, "model_sizes", lambda paths, library=None: {str(p): [10, 10, 1] for p in paths})
    assert resolver("Package_SO", "SOIC-8", kind="ic")[0] == 0


@pytest.mark.parametrize("flag", ["type_verified", "_type_verified"])
def test_verified_public_type_supersedes_old_provisional_hint(resolver, flag):
    assert resolver("Resistor_SMD", "R_0603", kind="resistor", detected_kind="diode", **{flag: True})[0] == 1
    result = resolver("Diode_SMD", "SOD-123", kind="resistor", detected_kind="diode", **{flag: True})
    assert result[0] == 0
    assert any("tipo verificato" in log for log in result[2])


def test_provisional_conflict_still_vetoes_with_explicit_diagnostic(resolver):
    result = resolver("Diode_SMD", "SOD-123", kind="generic", detected_kind="resistor", type_verified=False)
    assert result[0] == 0
    assert any("tipo provvisorio/non verificato" in log for log in result[2])


def test_module_size_match_never_claims_variant_identification(resolver):
    result = resolver("RF_Module", "ESP32-S3-WROOM-1", kind="generic")
    assert result[0] == 1
    assert "Identity and variant require user review" in result[1]["model_match"]["candidates"][0]["reasons"]


def test_all_incompatible_candidates_are_rejected_before_measurement(resolver, monkeypatch):
    def no_measurement(*args, **kwargs):
        pytest.fail("A known type conflict must be rejected before dimensional measurement")
    monkeypatch.setattr(km, "model_sizes", no_measurement)
    assert resolver("Diode_SMD", "SOD-123", kind="resistor")[0] == 0


@pytest.mark.parametrize("folder,evidence,expected", [
    ("RF_Module", {"kind": "generic"}, False),
    ("Custom", {"kind": "module"}, False),
    ("Custom", {"kind": "generic", "detected_kind": "module"}, False),
    ("Package_SO", {"kind": "ic"}, True),
])
def test_board_extension_veto_despite_matching_shield_and_pads(
        tmp_path, monkeypatch, folder, evidence, expected):
    model = tmp_path / (folder + ".3dshapes") / "SyntheticPackage.step"
    model.parent.mkdir()
    model.touch()
    monkeypatch.setattr(km, "model_sizes", lambda *a, **k: {str(model): {
        "total": [18, 25.5, 3], "body": [15.8, 17.6, 3]}})
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {str(model): [
        {"size": [19, 18.96], "source": "synthetic-footprint"}]})
    component = dict(package="SyntheticPackage", length_mm=19.36, width_mm=17.17, **evidence)
    logs = []
    assert km.attach_models([component], library=tmp_path, log=logs.append) == 0
    assert "model_path" not in component
    assert bool(component["model_match"]["candidates"]) is expected
    assert component["model_match"]["status"] == ("review_required" if expected else "unresolved")
    if not expected:
        assert "model_path" not in component
        assert any("ingombro totale" in line and "31.7%" in line for line in logs)


@pytest.mark.parametrize("total,expected", [([13, 10, 3], True), ([13.01, 10, 3], False),
                                         ([10, 10, 3], True), (None, False)])
def test_module_veto_uses_existing_tolerance_and_requires_total(
        tmp_path, monkeypatch, total, expected):
    model = tmp_path / "RF_Module.3dshapes" / "SyntheticModule.step"
    model.parent.mkdir()
    model.touch()
    monkeypatch.setattr(km, "model_sizes", lambda *a, **k: {str(model): {
        "total": total, "body": [10, 10, 3]}})
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {})
    result = km.resolve_model("SyntheticModule", 10, 10, library=tmp_path)
    assert (result == model) is expected
    assert km.DEFAULT_SIZE_TOLERANCE == .30


@pytest.mark.parametrize("hint,counts,observed,expected", [
    ("QFN", [24, 32], None, None),
    ("QFN", [32, 32], None, "QFN-32_5x5mm_A"),
    ("QFN-32", [24, 32], None, "QFN-32_5x5mm_B"),
    ("QFN", [24, 32], 32, "QFN-32_5x5mm_B"),
    ("QFN-24_5x5mm_A", [24, 32], None, "QFN-24_5x5mm_A"),
])
def test_uncounted_package_cannot_choose_between_equivalent_contact_topologies(
        tmp_path, monkeypatch, hint, counts, observed, expected):
    directory = tmp_path / "Package_DFN_QFN.3dshapes"
    directory.mkdir()
    paths = [directory / f"QFN-{count}_5x5mm_{suffix}.step"
             for count, suffix in zip(counts, "AB")]
    for path in paths:
        path.touch()
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: {})
    monkeypatch.setattr(km, "model_sizes", lambda paths, library=None: {str(p): [5, 5, 1] for p in paths})
    component = dict(package=hint, kind="ic_qfn", length_mm=5, width_mm=5,
                     pin_count=observed, pin_count_confidence="high")
    logs = []
    resolved = km.attach_models([component], library=tmp_path, log=logs.append)
    assert resolved == 0 and "model_path" not in component
    report = component["model_match"]
    assert report["status"] == "review_required" and report["selected_path"] is None
    names = [c["package"] for c in report["candidates"]]
    if expected is None:
        assert names == [p.stem for p in paths]
    else:
        assert names[0] == expected
    component["model_selection"] = {"status": "confirmed", "path": report["candidates"][0]["path"], "rotation_deg": 90}
    assert km.attach_models([component], library=tmp_path) == 1
    assert component["model_path"] == str(directory / (names[0] + ".step"))
    assert component["model_rotation_deg"] == 90


@pytest.mark.parametrize("kind", ["ic", "regulator"])
@pytest.mark.parametrize("basis,xy", [("body", [6.5, 3.5]), ("total", [7, 6.5]),
                                    ("leads", [7, 6.5])])
def test_sot223_family_and_measured_tab_remain_reviewable(tmp_path, monkeypatch, kind, basis, xy):
    path = tmp_path / "Package_TO_SOT_SMD.3dshapes" / "SOT-223.step"
    path.parent.mkdir()
    path.touch()
    monkeypatch.setattr(km, "model_sizes", lambda *a, **k: {str(path): {
        "body": [6.5, 3.5, 1.7], "total": [7, 6.5, 1.7]}})
    monkeypatch.setattr(km, "footprint_sizes", lambda *a, **k: pytest.fail("Explicit body/lead basis needs no pads"))
    # Semiconductor is a mechanical family shared by ICs and discrete devices;
    # it does not claim a regulator/transistor electrical identity.
    assert km._candidate_family(path) == "semiconductor"
    c = dict(kind=kind, package="SOT-223", length_mm=xy[0], width_mm=xy[1], bbox_basis=basis)
    assert km.attach_models([c], library=tmp_path) == 0
    report = c["model_match"]
    assert report["status"] == "review_required" and "model_path" not in c
    candidate = report["candidates"][0]
    assert candidate["dimensions_mm"] == [*xy, 1.7]
    assert candidate["relative_xy_error"] == 0
    assert candidate["family"] == "semiconductor"
    if basis == "body":
        c.update(bbox_basis="total")
        assert km.model_candidates(c, library=tmp_path)["candidates"] == []


@pytest.mark.parametrize("folder,name", [("Mounting", "CustomPackage"),
    ("Custom", "Screw_CustomPackage"), ("Custom", "Nut_CustomPackage"),
    ("Custom", "Washer_CustomPackage")])
def test_unknown_nonnumeric_package_never_proposes_hardware(tmp_path, monkeypatch, folder, name):
    path = tmp_path / (folder + ".3dshapes") / (name + ".step")
    path.parent.mkdir()
    path.touch()
    monkeypatch.setattr(km, "model_sizes", lambda *a, **k: pytest.fail("Hardware must be filtered before measurement"))
    report = km.model_candidates(dict(kind="unknown", package="CustomPackage", length_mm=3, width_mm=2), library=tmp_path)
    assert report == {"status": "unresolved", "candidates": [], "selected_path": None}


@pytest.mark.parametrize("name,expected", [
    ("SOT-23", None), ("SOT223", None), ("SOT-223", None),
    ("SOT-23-5", 5), ("SOT-223-8", 8), ("sot_23_5", 5),
    ("SOT-23-5_HandSoldering", 5), ("SOT-23_3x2mm", None),
    ("SOT-23-5A", None), ("SOT-23-0", None),
])
def test_sot_count_is_only_an_explicit_variant_suffix(name, expected):
    assert km._declared_count(km.Path(name + ".step")) == expected


@pytest.mark.parametrize("package,variant,observed,confidence,keep_variant", [
    ("SOT-223", "SOT-223-8", 4, "medium", False),
    ("SOT-223", "SOT-223-8", 8, "medium", True),
    ("SOT-223", "SOT-223-8", 4, "low", True),
    ("SOT-23", "SOT-23-5", 3, "medium", False),
    ("SOT-23", "SOT-23-5", 5, "medium", True),
])
def test_observed_sot_terminals_filter_variants_before_measurement(
        tmp_path, monkeypatch, package, variant, observed, confidence, keep_variant):
    directory = tmp_path / "Package_TO_SOT_SMD.3dshapes"
    directory.mkdir()
    paths = [directory / (name + ".step") for name in (package, variant)]
    for path in paths:
        path.touch()
    measured = []
    def sizes(requested, library=None):
        measured.extend(requested)
        return {str(p): {"body": [6.5, 3.5, 1.7]} for p in requested}
    monkeypatch.setattr(km, "model_sizes", sizes)
    component = dict(kind="regulator", package=package, length_mm=6.5, width_mm=3.5,
        bbox_basis="body", pin_count=observed, pin_count_confidence=confidence,
        pin_count_source="photo_observation")
    report = km.model_candidates(component, library=tmp_path)
    expected = paths if keep_variant else paths[:1]
    assert measured == expected
    # Quali varianti sopravvivono al filtro e' l'oggetto di questo test.
    # L'ordine in cui vengono proposte lo decide il conteggio dei reofori, ed
    # e' verificato dalla coppia di test qui sotto.
    assert {c["path"] for c in report["candidates"]} == {str(p) for p in expected}
    assert report["status"] == "review_required" and report["selected_path"] is None


@pytest.mark.parametrize("variant_size,first", [
    ([6.5, 3.5, 1.7], "SOT-23-5"),
    ([8.0, 4.5, 1.7], "SOT-23"),
])
def test_matching_terminal_count_ranks_first_only_within_the_measurement_band(
        tmp_path, monkeypatch, variant_size, first):
    """@brief Il conteggio dichiarato ordina dove la misura non distingue.

    Positivo: due solidi di ingombro identico: la fotografia non puo'
    separarli, e i cinque reofori osservati corrispondono a quelli dichiarati
    da SOT-23-5, che quindi va proposto per primo.

    Negativo: lo stesso conteggio corrispondente su un solido piu' grande
    dello scarto che la misura sa risolvere. Li' la misura decide, e SOT-23 --
    che di reofori non ne dichiara -- resta primo. La differenza fra i due
    casi e' strutturale (dentro o fuori NAME_TIEBREAK_MARGIN), non una
    coincidenza numerica fra due ingombri scelti bene.
    """
    directory = tmp_path / "Package_TO_SOT_SMD.3dshapes"
    directory.mkdir()
    sizes = {"SOT-23": [6.5, 3.5, 1.7], "SOT-23-5": variant_size}
    for name in sizes:
        (directory / (name + ".step")).touch()
    monkeypatch.setattr(km, "model_sizes", lambda requested, library=None: {
        str(p): {"body": sizes[p.stem]} for p in requested})
    component = dict(kind="regulator", package="SOT-23", length_mm=6.5, width_mm=3.5,
        bbox_basis="body", pin_count=5, pin_count_confidence="medium",
        pin_count_source="photo_observation")
    report = km.model_candidates(component, library=tmp_path)
    assert [km.Path(c["path"]).stem for c in report["candidates"]][0] == first
    assert {km.Path(c["path"]).stem for c in report["candidates"]} == set(sizes)
