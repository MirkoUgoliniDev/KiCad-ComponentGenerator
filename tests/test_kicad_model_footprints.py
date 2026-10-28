"""@brief Exercise linked land-pattern evidence without FreeCAD or network access."""

from pathlib import Path

import pytest

from kicadgen.cad3d import kicad_models as km


@pytest.fixture
def lib(tmp_path):
    library = tmp_path / "3dmodels"
    (library / "Parts.3dshapes").mkdir(parents=True)
    (tmp_path / "footprints" / "Parts.pretty").mkdir(parents=True)
    return library


def solid(lib, name="R_0603"):
    path = lib / "Parts.3dshapes" / (name + ".step")
    path.write_text("fixture")
    return path


def footprint(lib, model, pads, name="Different_Name", transform="", link=None):
    path = lib.parent / "footprints" / "Parts.pretty" / (name + ".kicad_mod")
    link = link or "${KICAD10_3DMODEL_DIR}/Parts.3dshapes/" + model.name
    path.write_text(f'''(footprint "{name}"
      (descr "Parentheses (and \\"quotes\\") are text")
      (fp_rect (start -99 -99) (end 99 99) (layer "F.CrtYd"))
      {pads}
      (model "{link}" {transform}))''')
    return path


PADS = '''(pad "1" smd roundrect (at -0.825 0) (size 0.8 0.95)
          (layers "F.Cu" "F.Mask") (roundrect_rratio 0.25))
          (pad "2" smd roundrect (at 0.825 0) (size 0.8 0.95)
          (layers "F.Cu" "F.Mask") (roundrect_rratio 0.25))'''


def test_explicit_link_and_copper_envelope_not_filename_or_courtyard(lib):
    model = solid(lib)
    source = footprint(lib, model, PADS)
    readings = km.footprint_sizes([model], lib)[str(model)]
    assert readings == [{"size": pytest.approx([2.45, 0.95]), "source": str(source)}]


def test_land_pattern_resolves_photo_but_rejects_wrong_size(lib, monkeypatch):
    model = solid(lib)
    footprint(lib, model, PADS)
    monkeypatch.setattr(km, "model_sizes", lambda *a: {str(model): {"total": [1.6, 0.8, 0.4]}})
    logs = []
    assert km.resolve_model("R_0603", 2.62, 1.25, library=lib, log=logs.append) == model
    assert any("lettura: piazzole" in line and "Different_Name" in line for line in logs)
    assert km.resolve_model("R_0603", 1, 0.5, library=lib) is None
    assert km.resolve_model("R_0603", 8, 4, library=lib) is None
    assert km.DEFAULT_SIZE_TOLERANCE == 0.30


@pytest.mark.parametrize("shape,size,extra,expected", [
    ("rect", "2 1", "", [3/2**0.5, 3/2**0.5]),
    ("roundrect", "2 1", "(roundrect_rratio 0.25)", [2/2**0.5+0.5]*2),
    ("oval", "2 1", "", [1/2**0.5+1]*2),
    ("circle", "1 1", "", [1, 1]),
])
def test_rotated_pad_bounds_are_shape_aware(lib, shape, size, extra, expected):
    model = solid(lib)
    footprint(lib, model, f'(pad "1" smd {shape} (at 5 -7 45) (size {size}) (layers "B.Cu") {extra})')
    assert km.footprint_sizes([model], lib)[str(model)][0]["size"] == pytest.approx(expected)


def test_non_copper_and_nonplated_holes_do_not_expand_envelope(lib):
    model = solid(lib)
    footprint(lib, model, PADS + '''
        (pad "" np_thru_hole circle (at 50 50) (size 10 10) (layers "*.Cu"))
        (pad "" smd rect (at 60 60) (size 20 20) (layers "F.Paste"))''')
    assert km.footprint_sizes([model], lib)[str(model)][0]["size"] == pytest.approx([2.45, .95])


@pytest.mark.parametrize("bad", [
    '(pad "3" smd custom (at 0 0) (size 1 1) (layers "F.Cu"))',
    '(pad "3" smd trapezoid (at 0 0) (size 1 1) (layers "F.Cu"))',
    '(pad "3" smd rect (at nan 0) (size 1 1) (layers "F.Cu"))',
    '(pad "3" smd rect (at 0 0) (size -1 1) (layers "F.Cu"))',
    '(pad "3" smd rect (at 0 0) (layers "F.Cu"))',
    '(pad "3" smd rect (at 0 0) (size 1 1))',
])
def test_one_unsupported_or_invalid_copper_pad_rejects_whole_reading(lib, bad):
    model = solid(lib)
    footprint(lib, model, PADS + bad)
    assert km.footprint_sizes([model], lib)[str(model)] == []


@pytest.mark.parametrize("transform", [
    '(scale (xyz 2 1 1))', '(rotate (xyz 90 0 0))',
    '(rotate (xyz 0 0 45))', 'hide', '(hide yes)',
    '(scale (xyz nan 1 1))',
    '(scale (invalid 1 1 1))',
])
def test_unsupported_model_instances_cannot_certify_raw_solid(lib, transform):
    model = solid(lib)
    footprint(lib, model, PADS, transform=transform)
    assert km.footprint_sizes([model], lib)[str(model)] == []


def test_planar_quarter_turn_and_translation_preserve_size(lib):
    model = solid(lib)
    footprint(lib, model, PADS, transform='(rotate (xyz 0 0 90)) (offset (xyz 3 4 5))')
    assert km.footprint_sizes([model], lib)[str(model)]


@pytest.mark.parametrize("kind", ["absolute", "relative", "legacy", "environment"])
def test_explicit_local_model_paths(lib, monkeypatch, kind):
    model = solid(lib)
    monkeypatch.setenv("TEST_MODEL_ROOT", str(lib))
    links = {"absolute": model.as_posix(),
             "relative": "../../3dmodels/Parts.3dshapes/" + model.name,
             "legacy": "${KICAD_3DMODEL_DIR}/Parts.3dshapes/" + model.name,
             "environment": "${TEST_MODEL_ROOT}/Parts.3dshapes/" + model.name}
    footprint(lib, model, PADS, link=links[kind])
    assert km.footprint_sizes([model], lib)[str(model)]


def test_unresolved_link_wrong_directory_and_vrml_are_not_guessed(lib):
    model = solid(lib)
    for i, link in enumerate(["${UNDEFINED_TEST_VARIABLE}/" + model.name,
                              "${KICAD10_3DMODEL_DIR}/Other.3dshapes/" + model.name,
                              "${KICAD10_3DMODEL_DIR}/Parts.3dshapes/R_0603.wrl"]):
        footprint(lib, model, PADS, name=str(i), link=link)
    assert km.footprint_sizes([model], lib)[str(model)] == []


def test_assemblies_and_malformed_files_are_skipped(lib):
    model = solid(lib)
    footprint(lib, model, PADS + '(model "other.step")')
    bad = footprint(lib, model, PADS, name="Broken")
    bad.write_text('(footprint "unterminated)')
    assert km.footprint_sizes([model], lib)[str(model)] == []


def _rewrite_observably(path, text):
    """Rewrite `path` so that the stamp-based cache (size, mtime_ns) can see the edit.

    The cache is a change detector on size and nanosecond mtime, not on
    content (kicad_models._footprint_listing): a same-size rewrite within one
    NTFS timestamp tick of the previous write is invisible to it by design.
    This test checks that an observable edit is picked up, not that the
    detector sees what it cannot: when the file system gave the rewrite the
    same mtime (it happened on the GitHub runner, release v1.0.1, 2026-10-28),
    the mtime is moved forward by one tick, exactly what a later save does.
    """
    import os
    before = path.stat()
    path.write_text(text)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns):
        os.utime(path, ns=(after.st_atime_ns, before.st_mtime_ns + 10_000_000))


def test_footprint_cache_tracks_edits_additions_and_deletions(lib):
    model = solid(lib)
    path = footprint(lib, model, PADS)
    assert km.footprint_sizes([model], lib)[str(model)][0]["size"][0] == pytest.approx(2.45)
    _rewrite_observably(path, path.read_text().replace("0.825", "1.825"))
    assert km.footprint_sizes([model], lib)[str(model)][0]["size"][0] == pytest.approx(4.45)
    extra = footprint(lib, model, PADS, name="HandSolder")
    assert len(km.footprint_sizes([model], lib)[str(model)]) == 2
    path.unlink()
    assert len(km.footprint_sizes([model], lib)[str(model)]) == 1
    extra.unlink()
    assert km.footprint_sizes([model], lib)[str(model)] == []


def test_footprint_variants_are_not_combined_into_invented_rectangle():
    size = {"land_patterns": [{"size": [4, 1]}, {"size": [2, 3]}]}
    assert km.size_distance(size, 4, 3) > .30


def test_land_pattern_without_measurable_solid_cannot_resolve(lib, monkeypatch):
    model = solid(lib)
    footprint(lib, model, PADS)
    monkeypatch.setattr(km, "model_sizes", lambda *a: {str(model): None})
    assert km.resolve_model("R_0603", 2.45, .95, library=lib) is None


def test_all_candidates_beyond_eighth_participate_and_meshes_do_not(lib, monkeypatch):
    paths = [solid(lib, f"Crystal_SMD_{i:02d}") for i in range(12)]
    mesh = paths[0].with_suffix(".wrl")
    mesh.write_text("mesh")
    seen = []
    def measure(requested, library):
        seen.extend(requested)
        return {str(p): [3.2, 2.5] if p == paths[-1] else [10, 10] for p in requested}
    monkeypatch.setattr(km, "model_sizes", measure)
    assert km.resolve_model("SMD_Crystal", 3.2, 2.5, library=lib) == paths[-1]
    assert seen == paths
    assert km.candidates("SMD_Resonator", lib) == []
    assert km.resolve_model("SMD_Crystal", .5, .5, library=lib) is None


def test_cache_keys_distinguish_identical_names_in_different_libraries(lib, tmp_path):
    first = solid(lib)
    second = tmp_path / first.name
    second.write_bytes(first.read_bytes())
    import os
    os.utime(second, ns=(first.stat().st_atime_ns, first.stat().st_mtime_ns))
    assert km.index_key(first) != km.index_key(second)


def test_full_path_variable_is_expanded_on_each_call(lib, monkeypatch):
    model = solid(lib)
    footprint(lib, model, PADS, link="${RESOLVER_TEST_MODEL}")
    monkeypatch.setenv("RESOLVER_TEST_MODEL", str(model))
    assert km.footprint_sizes(iter([model]), lib)[str(model)]
    monkeypatch.setenv("RESOLVER_TEST_MODEL", str(model.with_name("Other.step")))
    assert km.footprint_sizes([model], lib)[str(model)] == []


def test_attachment_reports_footprint_candidates_without_auto_import(lib, monkeypatch):
    model = solid(lib)
    footprint(lib, model, PADS)
    original = km.footprint_sizes
    scans = []
    def scan(paths, library=None, log=None):
        scans.append(paths)
        return original(paths, library, log)
    monkeypatch.setattr(km, "footprint_sizes", scan)
    monkeypatch.setattr(km, "model_sizes", lambda *a: {str(model): [1.6, .8, .4]})
    components = [{"package": "R_0603", "length_mm": 2.45, "width_mm": .95},
                  {"package": "R_0603", "length_mm": 1.6, "width_mm": .8}]
    assert km.attach_models(components, library=lib) == 0
    assert scans and all(list(paths) == [model] for paths in scans)
    for component in components:
        assert "model_path" not in component
        assert component["model_match"]["status"] == "review_required"
        assert component["model_match"]["candidates"][0]["path"] == str(model)
    assert components[0]["model_match"]["candidates"][0]["measurement_basis"].startswith("piazzole ")
    for component in components:
        component["model_selection"] = {"status": "confirmed", "path": str(model)}
    assert km.attach_models(components, library=lib) == 2
    assert all(component["model_path"] == str(model) for component in components)


def test_ambiguity_is_reported_without_changing_exact_name_preference(lib, monkeypatch):
    exact, variant = solid(lib, "SOT-223"), solid(lib, "SOT-223-8")
    monkeypatch.setattr(km, "model_sizes", lambda *a: {str(p): [6.5, 3.5] for p in [exact, variant]})
    logs = []
    assert km.resolve_model("SOT-223", 6.5, 3.5, library=lib, log=logs.append) == exact
    assert any("non distingue" in line for line in logs)


def test_failed_measurement_is_retried_instead_of_cached_forever(lib, monkeypatch):
    model = solid(lib)
    batches = iter([{}, {str(model): {"total": [1.6, .8, .4]}}])
    monkeypatch.setattr(km, "measure_models", lambda paths: next(batches))
    assert km.model_sizes([model], lib)[str(model)] is None
    assert km.model_sizes([model], lib)[str(model)]["total"] == [1.6, .8, .4]


@pytest.mark.parametrize("bad", [True, 42, "123", [True, 1], [float("nan"), 1]])
def test_malformed_cached_sizes_are_not_evidence(bad):
    assert km.size_distance(bad, 1, 1) is None


def test_installed_resistor_footprint_links_to_real_step():
    library = km.find_library()
    if library is None:
        pytest.skip("KiCad not installed")
    paths = [p for p in km.candidates("R_0603_1608Metric", library) if p.suffix == ".step"]
    if not paths or not (library.parent / "footprints").is_dir():
        pytest.skip("Required footprint library absent")
    readings = km.footprint_sizes(paths, library)
    assert any(km.size_matches(r["size"], 2.45, .95, tolerance=.01)
               for values in readings.values() for r in values)
