"""@brief Session index of KiCad footprints: same results as a full scan, re-indexed on change."""

import random
from pathlib import Path

import pytest

from kicadgen.cad3d import kicad_models as km


PADS = '''(pad "1" smd rect (at -1 0) (size 0.8 0.9) (layers "F.Cu"))
          (pad "2" smd rect (at 1 0) (size 0.8 0.9) (layers "F.Cu"))'''


@pytest.fixture
def tree(tmp_path):
    """A fake KiCad share: 3dmodels/<Lib>.3dshapes beside footprints/<Lib>.pretty."""
    library = tmp_path / "3dmodels"
    for lib in ("A", "B"):
        (library / f"{lib}.3dshapes").mkdir(parents=True)
        (tmp_path / "footprints" / f"{lib}.pretty").mkdir(parents=True)
    km.clear_footprint_index()
    yield library
    km.clear_footprint_index()


def _solid(library, lib, name):
    path = library / f"{lib}.3dshapes" / f"{name}.step"
    path.write_text("solid", encoding="utf-8")
    return path


def _footprint(library, lib, name, link, pads=PADS):
    path = library.parent / "footprints" / f"{lib}.pretty" / f"{name}.kicad_mod"
    path.write_text(f'(footprint "{name}" {pads} (model "{link}"))', encoding="utf-8")
    return path


def _link(lib, model):
    return "${KICAD10_3DMODEL_DIR}/" + f"{lib}.3dshapes/{model}.step"


def _reference_selection(root: Path, names):
    """The per-call scan that the index replaces, copied verbatim as the oracle."""
    selected = []
    for footprint in sorted(root.rglob("*.kicad_mod")):
        stamp = km.index_key(footprint)
        hints, variable = km._link_hints(footprint, stamp)
        if not variable and not names.intersection(hints):
            continue
        selected.append((footprint, stamp))
    return selected


def _count_builds(monkeypatch):
    builds = []
    original = km._FootprintIndex.__init__

    def counting(self, root, listing):
        builds.append(root)
        original(self, root, listing)

    monkeypatch.setattr(km._FootprintIndex, "__init__", counting)
    return builds


def test_index_selects_the_same_footprints_in_the_same_order_as_a_full_scan(tree):
    rng = random.Random(20260927)
    models = [_solid(tree, lib, f"M{i}") for i in range(12) for lib in ("A",)]
    for i in range(80):
        lib = rng.choice("AB")
        kind = rng.random()
        if kind < 0.1:
            link = "${SOME_VARIABLE}"          # variable link: always a candidate
        elif kind < 0.2:
            link = "other.wrl"
        else:
            link = _link("A", rng.choice(models).stem)
        _footprint(tree, lib, f"F{i:03d}_{rng.randint(0, 999)}", link)
    root = tree.parent / "footprints"
    for trial in range(30):
        names = {p.name.casefold() for p in rng.sample(models, rng.randint(1, 4))}
        assert km._linked_footprints(root, names) == _reference_selection(root, names)
    sizes = km.footprint_sizes(models, tree)
    assert sum(map(len, sizes.values())) > 0


def test_index_is_built_once_while_the_library_is_unchanged(tree, monkeypatch):
    builds = _count_builds(monkeypatch)
    model = _solid(tree, "A", "R")
    _footprint(tree, "A", "R_fp", _link("A", "R"))
    for _ in range(5):
        assert len(km.footprint_sizes([model], tree)[str(model)]) == 1
        assert km._declared_land_pattern(model) == (2, 0)
    assert len(builds) == 1


def test_added_footprint_is_seen_even_within_one_directory_timer_tick(tree, monkeypatch):
    builds = _count_builds(monkeypatch)
    model = _solid(tree, "A", "R")
    _footprint(tree, "A", "R_fp", _link("A", "R"))
    folder = tree.parent / "footprints" / "B.pretty"
    before = folder.stat().st_mtime_ns
    assert len(km.footprint_sizes([model], tree)[str(model)]) == 1
    _footprint(tree, "B", "R_other", _link("A", "R"))
    # Negative control of the detector: pinning the folder mtime reproduces a
    # coarse-tick filesystem, and the addition must still be found.
    import os
    os.utime(folder, ns=(before, before))
    assert len(km.footprint_sizes([model], tree)[str(model)]) == 2
    assert len(builds) == 2


def test_in_place_edit_of_an_unrelated_footprint_that_now_links_the_model(tree, monkeypatch):
    builds = _count_builds(monkeypatch)
    model = _solid(tree, "A", "R")
    unrelated = _footprint(tree, "B", "Other", _link("A", "Nothing"))
    assert km.footprint_sizes([model], tree)[str(model)] == []
    assert km._declared_land_pattern(model) is None
    # Rewritten in place: no directory entry is added, only size/mtime change.
    unrelated.write_text(unrelated.read_text(encoding="utf-8").replace("Nothing", "R")
                         + " ", encoding="utf-8")
    assert [r["source"] for r in km.footprint_sizes([model], tree)[str(model)]] == [str(unrelated)]
    assert km._declared_land_pattern(model) == (2, 0)
    assert len(builds) == 2


def test_removed_footprint_is_forgotten(tree):
    model = _solid(tree, "A", "R")
    path = _footprint(tree, "A", "R_fp", _link("A", "R"))
    assert km.footprint_sizes([model], tree)[str(model)]
    path.unlink()
    assert km.footprint_sizes([model], tree)[str(model)] == []
    assert km._declared_land_pattern(model) is None


def test_separate_roots_keep_separate_indexes(tmp_path):
    km.clear_footprint_index()
    results = []
    for name in ("one", "two"):
        library = tmp_path / name / "3dmodels"
        (library / "A.3dshapes").mkdir(parents=True)
        (library.parent / "footprints" / "A.pretty").mkdir(parents=True)
        model = _solid(library, "A", "R")
        if name == "two":
            _footprint(library, "A", "R_fp", _link("A", "R"))
        results.append(km.footprint_sizes([model], library)[str(model)])
    assert results[0] == [] and len(results[1]) == 1
    km.clear_footprint_index()
