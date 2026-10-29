"""@brief Regression tests of the 2026-10-29 review, CAD scope (kicadgen/cad3d).

H1 a measuring child that dies on one model no longer starves the others;
H2 no unbounded wait on a child whose kill was refused; H3x the footprint
link hints persist across processes; H4/H5 one listing scope per
`model_candidates` call; H6 the body section of a library model is computed
once per file; H7 the FreeCAD parameter file never leaks; H8 a truncated
STEP is a failed run; H9 the FreeCAD log is capped.

No test launches FreeCAD: the measuring child is a plain Python interpreter
running the REAL measuring script against fake `FreeCAD`/`Part` modules put on
its PYTHONPATH, so the exchange protocol under test is the production one.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicadgen.cad3d import kicad_models as km
from kicadgen.cad3d import model_size_cache, process_log, step_generator
from kicadgen.cad3d.body_params import Body3DParams
from kicadgen.cad3d.step_generator import Cad3DError


ROOT = Path(__file__).resolve().parents[1]
CAD_DIR = ROOT / "kicadgen" / "cad3d"


@pytest.fixture
def user_cache(monkeypatch, tmp_path):
    """@brief Point every user cache of the CAD modules at a private folder."""
    folder = tmp_path / "cache"
    monkeypatch.setattr(model_size_cache, "user_cache_dirs", lambda: [folder])
    return folder


# ── H1: a crash on one model, unreadable models ───────────────────────────

_FAKE_FREECAD = "class Vector:\n    def __init__(self, *values):\n        self.values = values\n"

_FAKE_PART = '''
import os
# One line per child: the number of launches is the number of lines.
with open(os.environ["FAKE_LAUNCHES"], "a", encoding="utf-8") as handle:
    handle.write("launch\\n")
if os.environ.get("FAKE_DIE_ON_IMPORT"):
    os._exit(4)  # dies before the measuring loop opens progress.jsonl


class _Box:
    XMin = YMin = ZMin = 0.0
    XLength, YLength, ZLength = 1.6, 0.8, 0.45


class _Empty:
    Volume = 0


class _Shape:
    BoundBox = _Box()

    def common(self, other):
        return _Empty()


def makeBox(*args):
    return _Shape()


def read(path):
    name = os.path.basename(path)
    if name in os.environ.get("FAKE_CRASH", "").split(","):
        os._exit(3)  # a native crash of the reader: no Python exception, no line
    if name in os.environ.get("FAKE_UNREADABLE", "").split(","):
        raise RuntimeError("unreadable STEP")
    if name in os.environ.get("FAKE_HANG", "").split(","):
        # A long reading in progress: announces itself, then works for a minute.
        with open(os.environ["FAKE_HANG_FLAG"], "w", encoding="utf-8") as handle:
            handle.write("reading")
        import time
        time.sleep(60)
    return _Shape()
'''


@pytest.fixture
def fake_reader(monkeypatch, tmp_path, user_cache):
    """@brief A measuring child: this Python with fake FreeCAD/Part modules.

    @return Callable returning the number of children launched so far.
    """
    modules = tmp_path / "fake_modules"
    modules.mkdir()
    (modules / "FreeCAD.py").write_text(_FAKE_FREECAD, encoding="utf-8")
    (modules / "Part.py").write_text(_FAKE_PART, encoding="utf-8")
    launches = tmp_path / "launches.txt"
    monkeypatch.setenv("PYTHONPATH", str(modules))
    monkeypatch.setenv("FAKE_LAUNCHES", str(launches))
    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: sys.executable)

    def count():
        return len(launches.read_text(encoding="utf-8").splitlines()) if launches.exists() else 0
    return count


def _models(tmp_path, *names):
    library = tmp_path / "3dmodels"
    (library / "X.3dshapes").mkdir(parents=True, exist_ok=True)
    paths = []
    for name in names:
        path = library / "X.3dshapes" / f"{name}.step"
        path.write_text(f"ISO-10303-21; {name}", encoding="utf-8")
        paths.append(path)
    return library, paths


def _measured(sizes):
    return {Path(path).stem: value is not None for path, value in sizes.items()}


def test_h1_a_crash_on_one_model_measures_the_others_and_remembers_the_culprit(
        fake_reader, monkeypatch, tmp_path):
    """@brief Positive: crash on b -> a and c measured, b remembered with its exit code."""
    monkeypatch.setenv("FAKE_CRASH", "b.step")
    library, (a, b, c) = _models(tmp_path, "a", "b", "c")
    sizes = km.model_sizes([a, b, c], library)
    assert _measured(sizes) == {"a": True, "b": False, "c": True}
    assert fake_reader() == 2, "a new child measured the models after the culprit"
    record = km.load_stalls()[km.index_key(b)]
    assert record["status"] == "crashed" and record["exit_code"] == 3
    assert record["model"] == model_size_cache.file_identity(b)
    # Second generation: a and c come from the index, b from the memo: no child.
    assert _measured(km.model_sizes([a, b, c], library)) == {"a": True, "b": False, "c": True}
    assert fake_reader() == 2


def test_h1_a_rewritten_culprit_is_measured_again(fake_reader, monkeypatch, tmp_path):
    """@brief Negative: a new file identity is a new question, measured by a child."""
    monkeypatch.setenv("FAKE_CRASH", "b.step")
    library, (a, b, c) = _models(tmp_path, "a", "b", "c")
    km.model_sizes([a, b, c], library)
    launched = fake_reader()
    b.write_text("ISO-10303-21; b, fixed by its author", encoding="utf-8")
    monkeypatch.setenv("FAKE_CRASH", "")
    sizes = km.model_sizes([a, b, c], library)
    assert fake_reader() == launched + 1
    assert _measured(sizes) == {"a": True, "b": True, "c": True}


def test_h1_a_child_dying_before_measuring_blames_no_model(fake_reader, monkeypatch, tmp_path):
    """@brief Negative: no progress file means a broken reader, not a broken model.

    The child dies while importing Part, before the measuring loop opens
    progress.jsonl: nobody is remembered, and the next run tries again.
    """
    monkeypatch.setenv("FAKE_DIE_ON_IMPORT", "1")
    library, (a, b) = _models(tmp_path, "a", "b")
    assert _measured(km.model_sizes([a, b], library)) == {"a": False, "b": False}
    assert fake_reader() == 1, "one failed batch, not one child per model"
    assert km.load_stalls() == {}
    km.model_sizes([a, b], library)
    assert fake_reader() == 2, "the models stay retryable"


def test_h1_an_unreadable_model_is_remembered_for_the_same_freecad(fake_reader, monkeypatch,
                                                                   tmp_path):
    """@brief Positive: a `null` answer is memoised, so FreeCAD does not re-read it every run."""
    monkeypatch.setenv("FAKE_UNREADABLE", "b.step")
    library, (a, b) = _models(tmp_path, "a", "b")
    assert _measured(km.model_sizes([a, b], library)) == {"a": True, "b": False}
    assert fake_reader() == 1
    assert km.load_stalls()[km.index_key(b)]["status"] == "unreadable"
    km.model_sizes([a, b], library)
    assert fake_reader() == 1


def test_h1_another_freecad_build_reads_an_unreadable_model_again(fake_reader, monkeypatch,
                                                                  tmp_path):
    """@brief Negative: the verdict belongs to (file, reader); a new reader retries."""
    monkeypatch.setenv("FAKE_UNREADABLE", "b.step")
    library, (a, b) = _models(tmp_path, "a", "b")
    km.model_sizes([a, b], library)
    monkeypatch.setattr(km, "_reader_identity", lambda: "another freecadcmd:1:2")
    km.model_sizes([a, b], library)
    assert fake_reader() == 2


def test_h1_a_measurer_answering_nothing_leaves_models_retryable(user_cache, monkeypatch,
                                                                 tmp_path):
    """@brief Negative: a model ABSENT from the answer (no FreeCAD) is never remembered."""
    library, (a,) = _models(tmp_path, "a")
    calls = []
    monkeypatch.setattr(km, "measure_models", lambda paths, **kw: calls.append(1) or {})
    km.model_sizes([a], library)
    km.model_sizes([a], library)
    assert len(calls) == 2 and km.load_stalls() == {}


# ── H2: refused kills never hang ───────────────────────────────────────────

class _Unkillable:
    """@brief A child whose kill is ignored and whose wait never succeeds."""

    pid = 999999
    returncode = None

    def __init__(self, *args, **kwargs):
        self.waits = []
        self.stdout = kwargs.get("stdout")

    def poll(self):
        return None

    def kill(self):
        pass

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout is None:
            raise AssertionError("unbounded wait on a child whose kill was refused")
        raise subprocess.TimeoutExpired("freecadcmd", timeout)


def test_h2_a_refused_kill_does_not_hang_the_measuring_batch(monkeypatch, caplog):
    """@brief Positive: cancel, kill refused: the batch ends, the stop is raised, nothing waits forever."""
    started = []

    def popen(*args, **kwargs):
        started.append(_Unkillable(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr(subprocess, "Popen", popen)
    cancel = threading.Event()
    cancel.set()
    began = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="kicadgen.cad3d.kicad_models"):
        outcome, measured, stopped = km._measure_batch("freecadcmd", ["a.step"], 5, cancel)
    assert isinstance(stopped, km.MatchingCancelled)
    assert time.monotonic() - began < 10
    assert None not in started[0].waits
    assert "left behind" in caplog.text


def test_h2_an_undeletable_exchange_folder_is_logged_not_raised(monkeypatch, caplog):
    """@brief Positive: the straggler keeps a file open; the haul still comes back."""
    import shutil

    def locked(path, *args, **kwargs):
        raise PermissionError(32, "in use by another process", path)

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    monkeypatch.setattr(shutil, "rmtree", locked)
    with caplog.at_level(logging.WARNING, logger="kicadgen.cad3d.kicad_models"):
        outcome, measured, stopped = km._measure_batch("freecadcmd", ["a.step"], 5, None)
    assert outcome == "failed" and stopped is None
    assert "left behind" in caplog.text


def test_h2_an_ordinary_batch_removes_its_exchange_folder(fake_reader, monkeypatch, tmp_path):
    """@brief Negative: a child that exits by itself is reaped and its folder deleted."""
    import tempfile
    folders = []
    real = tempfile.mkdtemp

    def recording(*args, **kwargs):
        folders.append(real(*args, **kwargs))
        return folders[-1]

    monkeypatch.setattr(tempfile, "mkdtemp", recording)
    _, (a,) = _models(tmp_path, "a")
    outcome, measured, stopped = km._measure_batch(sys.executable, [str(a)], 60, None)
    assert outcome == "done" and measured[str(a)]["total"] == [1.6, 0.8, 0.45]
    assert folders and not Path(folders[0]).exists()


class _TalkingUnkillable:
    """@brief A logged child that keeps printing after its kill was refused."""

    pid = 999998
    returncode = None

    def __init__(self, command, **kwargs):
        self.waits = []
        self.closed = False
        self.stdout = self._lines()

    def _lines(self):
        for index in range(200):
            time.sleep(0.02)
            yield f"line {index}\n"

    def poll(self):
        return None

    def kill(self):
        pass

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout is None:
            raise AssertionError("unbounded wait on a child whose kill was refused")
        raise subprocess.TimeoutExpired("freecadcmd", timeout)


def test_h2_run_logged_abandons_a_child_whose_kill_was_refused(monkeypatch, tmp_path):
    """@brief Positive: the cancel is raised after a bounded reap, not after an endless wait."""
    started = []
    monkeypatch.setattr(process_log.subprocess, "Popen",
                        lambda command, **kw: started.append(_TalkingUnkillable(command)) or started[-1])
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    with pytest.raises(process_log.ProcessCancelled):
        process_log.run_logged(["freecadcmd"], tmp_path / "freecad.log", timeout=30,
                               cancel=cancel)
    assert started[0].waits and None not in started[0].waits


def test_h2_run_logged_still_waits_for_a_child_that_ends_by_itself(tmp_path):
    """@brief Negative: an ordinary run returns the child's own exit code and output."""
    result = process_log.run_logged([sys.executable, "-c", "print('ready')"],
                                    tmp_path / "freecad.log", timeout=60)
    assert result.returncode == 0 and "ready" in result.stdout


# ── H3x: persisted footprint hints ─────────────────────────────────────────

PADS = '''(pad "1" smd rect (at -1 0) (size 0.8 0.9) (layers "F.Cu"))
          (pad "2" smd rect (at 1 0) (size 0.8 0.9) (layers "F.Cu"))'''


@pytest.fixture
def footprint_tree(tmp_path, user_cache):
    """@brief A fake KiCad share with three footprints linking two models."""
    library = tmp_path / "3dmodels"
    (library / "A.3dshapes").mkdir(parents=True)
    pretty = tmp_path / "footprints" / "A.pretty"
    pretty.mkdir(parents=True)
    for name in ("R", "C"):
        (library / "A.3dshapes" / f"{name}.step").write_text("solid", encoding="utf-8")
        (pretty / f"{name}_fp.kicad_mod").write_text(
            f'(footprint "{name}_fp" {PADS} (model "${{KICAD10_3DMODEL_DIR}}/A.3dshapes/{name}.step"))',
            encoding="utf-8")
    (pretty / "None_fp.kicad_mod").write_text(f'(footprint "None_fp" {PADS})', encoding="utf-8")
    km.clear_footprint_index()
    yield library
    km.clear_footprint_index()


def _count_reads(monkeypatch):
    reads = []
    real = km._scan_link_hints
    monkeypatch.setattr(km, "_scan_link_hints", lambda path: reads.append(path.name) or real(path))
    return reads


def test_h3x_a_new_process_reads_no_unchanged_footprint(footprint_tree, monkeypatch):
    """@brief Positive: the second index of an unchanged library reads no footprint file."""
    reads = _count_reads(monkeypatch)
    model = footprint_tree / "A.3dshapes" / "R.step"
    first = km.footprint_sizes([model], footprint_tree)
    assert sorted(reads) == ["C_fp.kicad_mod", "None_fp.kicad_mod", "R_fp.kicad_mod"]
    km.clear_footprint_index()  # what a new process starts from
    reads.clear()
    assert km.footprint_sizes([model], footprint_tree) == first
    assert reads == []


def test_h3x_changed_added_and_removed_footprints_are_reindexed(footprint_tree, monkeypatch):
    """@brief Negative: only new or changed files are read; a removed one leaves the cache."""
    model = footprint_tree / "A.3dshapes" / "R.step"
    km.footprint_sizes([model], footprint_tree)
    pretty = footprint_tree.parent / "footprints" / "A.pretty"
    none_fp = pretty / "None_fp.kicad_mod"
    # The unrelated footprint now links R: rewritten with another size.
    none_fp.write_text(f'(footprint "None_fp" {PADS} (model "${{KICAD10_3DMODEL_DIR}}/A.3dshapes/R.step")) ',
                       encoding="utf-8")
    (pretty / "C_fp.kicad_mod").unlink()
    km.clear_footprint_index()
    reads = _count_reads(monkeypatch)
    sources = [Path(r["source"]).name for r in km.footprint_sizes([model], footprint_tree)[str(model)]]
    assert reads == ["None_fp.kicad_mod"]
    assert sorted(sources) == ["None_fp.kicad_mod", "R_fp.kicad_mod"]
    saved = model_size_cache.load(km._footprint_hint_paths())
    entries = next(iter(saved.values()))["entries"]
    assert not any("C_fp" in name for name in entries), "a removed footprint is dropped"


def test_h3x_a_long_rebuild_reports_progress_and_a_short_one_is_silent(footprint_tree, monkeypatch):
    """@brief Positive and negative: lines appear only once the interval has elapsed."""
    model = footprint_tree / "A.3dshapes" / "R.step"
    lines = []
    km.footprint_sizes([model], footprint_tree, log=lines.append)
    assert not [l for l in lines if "Indicizzazione" in l], "a quick scan says nothing"
    km.clear_footprint_index()
    for path in (footprint_tree.parent / "footprints" / "A.pretty").iterdir():
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    monkeypatch.setattr(km, "FOOTPRINT_INDEX_PROGRESS_S", 0.0)
    lines.clear()
    km.footprint_sizes([model], footprint_tree, log=lines.append)
    progress = [l for l in lines if "Indicizzazione" in l]
    assert progress and progress[-1].endswith("3 di 3 file letti.")


def test_h3x_a_corrupted_hint_cache_is_ignored_not_trusted(footprint_tree):
    """@brief Negative: malformed records are re-read; the result equals a cold scan."""
    model = footprint_tree / "A.3dshapes" / "R.step"
    expected = km.footprint_sizes([model], footprint_tree)
    path = km._footprint_hint_paths()[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    for record in data.values():
        record["entries"] = {name: "garbage" for name in record["entries"]}
    path.write_text(json.dumps(data), encoding="utf-8")
    km.clear_footprint_index()
    assert km.footprint_sizes([model], footprint_tree) == expected


# ── H4 / H5: one scope per model_candidates call ──────────────────────────

def _scope_library(tmp_path):
    root = tmp_path / "3dmodels"
    for name in ("Package_TO_SOT_SMD.3dshapes/SOT-223.step",
                 "Package_TO_SOT_SMD.3dshapes/SOT-223-8.step"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    return root


_SOT = {"name": "U1", "kind": "ic", "package": "SOT-223", "length_mm": 6.5,
        "width_mm": 3.5, "confidence": "high"}


def test_h5_a_direct_model_candidates_call_walks_each_root_once(monkeypatch, tmp_path, user_cache):
    """@brief Positive: the call opens its own scope; the index is parsed once and not rewritten."""
    root = _scope_library(tmp_path)
    walks, loads, saves = [], [], []
    real_walk, real_load = km._walk_model_files, model_size_cache.load
    monkeypatch.setattr(km, "_walk_model_files", lambda r: walks.append(r) or real_walk(r))
    monkeypatch.setattr(model_size_cache, "load", lambda paths: loads.append(list(paths)) or real_load(paths))
    monkeypatch.setattr(km, "save_index", lambda *a, **k: saves.append(1))
    monkeypatch.setattr(km, "measure_models", lambda paths, **kw: {})
    index = {km.index_key(p): {"total": [6.5, 3.5, 1.6], "body": [6.5, 3.5, 1.6]}
             for p in root.rglob("*.step")}
    user_cache.mkdir(parents=True, exist_ok=True)
    (user_cache / km.INDEX_FILENAME).write_text(json.dumps(index), encoding="utf-8")
    report = km.model_candidates(dict(_SOT), library=root)
    assert report["candidates"], report
    assert len(walks) == 1
    index_loads = [p for p in loads if any(str(x).endswith(km.INDEX_FILENAME) for x in p)]
    assert len(index_loads) == 1, "the size index is parsed once per call"
    assert saves == [], "nothing measured, nothing rewritten"


def test_h5_two_direct_calls_do_not_share_a_listing(monkeypatch, tmp_path, user_cache):
    """@brief Negative: the scope ends with the call; a later call sees a changed library."""
    root = _scope_library(tmp_path)
    walks = []
    real_walk = km._walk_model_files
    monkeypatch.setattr(km, "_walk_model_files", lambda r: walks.append(r) or real_walk(r))
    monkeypatch.setattr(km, "measure_models", lambda paths, **kw: {})
    km.model_candidates(dict(_SOT), library=root)
    km.model_candidates(dict(_SOT), library=root)
    assert len(walks) == 2


def test_h4_freecadcmd_is_searched_once_per_scope(monkeypatch, tmp_path):
    """@brief Positive inside a scope, negative outside: the search is per scope."""
    searches = []
    monkeypatch.setattr(step_generator, "find_freecadcmd", lambda: searches.append(1) or "freecadcmd")
    with km._listing_scope():
        assert km._find_freecadcmd() == km._find_freecadcmd() == "freecadcmd"
    assert len(searches) == 1
    km._find_freecadcmd()
    km._find_freecadcmd()
    assert len(searches) == 3


def test_h4_a_new_measurement_is_saved_and_seen_by_the_same_scope(monkeypatch, tmp_path, user_cache):
    """@brief Positive: an added measurement is written once and visible to later lookups."""
    library, (a,) = _models(tmp_path, "a")
    saves = []
    real_save = km.save_index
    monkeypatch.setattr(km, "save_index", lambda index, lib=None: saves.append(1) or real_save(index, lib))
    monkeypatch.setattr(km, "measure_models",
                        lambda paths, **kw: {str(p): {"total": [1, 2, 3], "body": [1, 2, 3]} for p in paths})
    with km._listing_scope():
        km.model_sizes([a], library)
        assert km.load_index(library)[km.index_key(a)]["total"] == [1, 2, 3]
        km.model_sizes([a], library)
    assert saves == [1]


# ── H6: the body section is computed once per file, reader and build ──────

class _Vec:
    def __init__(self, *values):
        self.values = values

    def __neg__(self):
        return _Vec(*(-v for v in self.values))


class _BoundBox:
    def __init__(self, x0, y0, z0, x1, y1, z1):
        self.XMin, self.YMin, self.ZMin, self.XMax, self.YMax, self.ZMax = x0, y0, z0, x1, y1, z1
        self.XLength, self.YLength, self.ZLength = x1 - x0, y1 - y0, z1 - z0


class _Solid:
    def __init__(self, box, slices):
        self.BoundBox = box
        self.slices = slices

    def slice(self, *args):
        self.slices.append(args)
        return ["wire"]


@pytest.fixture
def parts(monkeypatch, tmp_path):
    """@brief `freecad_parts` loaded with fake FreeCAD/Part and a private cache folder."""
    import importlib.util
    section = _BoundBox(-1.5, -2.0, 1.0, 1.5, 2.0, 1.0)
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(
        Vector=_Vec, BoundBox=_BoundBox, Version=lambda: ["1", "1", "4", "R1"], ActiveDocument=None))
    monkeypatch.setitem(sys.modules, "Part", SimpleNamespace(
        makeCompound=lambda shapes: SimpleNamespace(BoundBox=section)))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    spec = importlib.util.spec_from_file_location("review1029_parts", CAD_DIR / "freecad_parts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.set_cache_dirs([str(tmp_path / "cache")])
    return module


def _model_file(tmp_path, text="ISO-10303-21; model"):
    path = tmp_path / "M.step"
    path.write_text(text, encoding="utf-8")
    return path


def test_h6_the_section_is_sliced_once_then_replayed_identically(parts, tmp_path):
    """@brief Positive: second build replays the very same box without slicing."""
    path = _model_file(tmp_path)
    slices = []
    box = _BoundBox(-3, -2.5, 0, 3, 2.5, 1.75)
    first = parts._body_section_box(_Solid(box, slices), box, str(path), "import")
    second = parts._body_section_box(_Solid(box, slices), box, str(path), "import")
    assert len(slices) == 1
    assert [first.XMin, first.YMin, first.ZMin, first.XMax, first.YMax, first.ZMax] == \
           [second.XMin, second.YMin, second.ZMin, second.XMax, second.YMax, second.ZMax]


def test_h6_another_reader_or_a_rewritten_file_slices_again(parts, tmp_path):
    """@brief Negative: the section belongs to (file, reader, build); any change recomputes."""
    path = _model_file(tmp_path)
    slices = []
    box = _BoundBox(-3, -2.5, 0, 3, 2.5, 1.75)
    parts._body_section_box(_Solid(box, slices), box, str(path), "import")
    parts._body_section_box(_Solid(box, slices), box, str(path), "part_read")
    assert len(slices) == 2, "the Part.read fallback is another question"
    time.sleep(0.02)
    path.write_text("ISO-10303-21; model v2", encoding="utf-8")
    parts._body_section_box(_Solid(box, slices), box, str(path), "import")
    assert len(slices) == 3, "a rewritten file is sliced again"


def test_h6_a_corrupted_section_record_is_recomputed(parts, tmp_path):
    """@brief Negative: a record with a broken box is not trusted."""
    path = _model_file(tmp_path)
    slices = []
    box = _BoundBox(-3, -2.5, 0, 3, 2.5, 1.75)
    parts._body_section_box(_Solid(box, slices), box, str(path), "import")
    index = Path(parts._section_cache_paths()[0])
    data = json.loads(index.read_text(encoding="utf-8"))
    for record in data.values():
        record["box"] = [0, 0, 0, -1, 1, 1]
    index.write_text(json.dumps(data), encoding="utf-8")
    parts._body_section_box(_Solid(box, slices), box, str(path), "import")
    assert len(slices) == 2


# ── H7: the parameter file never leaks ────────────────────────────────────

def _params_files(folder):
    return sorted(p.name for p in Path(folder).glob("*.kicadgen_params"))


def test_h7_a_non_json_payload_is_a_cad_error_and_leaves_no_file(monkeypatch, tmp_path):
    """@brief Positive: an unserialisable value raises Cad3DError, no temporary file remains."""
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(Cad3DError):
        step_generator._run_freecad("Demo", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
                                    freecadcmd_path=sys.executable,
                                    extra_payload={"bad": object()})
    assert _params_files(tmp_path) == []


def test_h7_a_failed_write_removes_the_file(monkeypatch, tmp_path):
    """@brief Positive: the file exists but the write fails: it is removed, the error propagates."""
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    real = tempfile.NamedTemporaryFile

    class _Full:
        def __init__(self, handle):
            self._handle, self.name = handle, handle.name

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()
            return False

        def write(self, text):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", lambda **kw: _Full(real(**kw)))
    with pytest.raises(OSError):
        step_generator._run_freecad("Demo", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
                                    freecadcmd_path=sys.executable)
    assert _params_files(tmp_path) == []


_STEP_CHILD = """
import json, os, sys
data = json.load(open(sys.argv[-1], encoding="utf-8"))
staged = data["staging_paths"]["step"]
with open(staged, "w", encoding="utf-8") as fh:
    fh.write({text!r})
os.replace(staged, data["step_path"])
print("KICADGEN_STEP_OK:" + data["step_path"], flush=True)
"""


def _arm_step(monkeypatch, tmp_path, text):
    script = tmp_path / "fake_child.py"
    script.write_text(_STEP_CHILD.format(text=text), encoding="utf-8")
    monkeypatch.setattr(step_generator, "_SCRIPT_PATH", script)


def test_h7_an_ordinary_run_removes_its_parameter_file(monkeypatch, tmp_path):
    """@brief Negative: the normal path still writes the file for the child and removes it."""
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    _arm_step(monkeypatch, tmp_path, "ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")
    result = step_generator._run_freecad("Demo", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
                                         freecadcmd_path=sys.executable)
    assert result["step_path"].is_file()
    assert _params_files(tmp_path) == []


# ── H8: a truncated STEP is a failed run ──────────────────────────────────

def test_h8_a_step_without_its_trailer_fails_and_ships_nothing(monkeypatch, tmp_path):
    """@brief Positive: marker printed, file present, trailer missing: failure, nothing kept."""
    _arm_step(monkeypatch, tmp_path, "ISO-10303-21;\nHEADER;\nDATA;\n#1=CARTESIAN_POINT(")
    out = tmp_path / "out"
    with pytest.raises(Cad3DError) as error:
        step_generator._run_freecad("Demo", [], Body3DParams(10.0, 8.0, 2.0), out,
                                    freecadcmd_path=sys.executable)
    assert "END-ISO-10303-21" in str(error.value)
    shapes = out / "Demo.3dshapes"
    assert not (shapes / "Demo.step").exists()
    assert not [p for p in shapes.iterdir() if "partial" in p.name]


def test_h8_a_complete_step_with_trailing_whitespace_is_accepted(monkeypatch, tmp_path):
    """@brief Negative: blank lines after the trailer are tolerated."""
    _arm_step(monkeypatch, tmp_path, "ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\r\n\n  \n")
    result = step_generator._run_freecad("Demo", [], Body3DParams(10.0, 8.0, 2.0), tmp_path / "out",
                                         freecadcmd_path=sys.executable)
    assert result["step_path"].is_file()


def _child_function(name):
    tree = ast.parse((CAD_DIR / "freecad_build_body.py").read_text(encoding="utf-8"))
    wanted = [n for n in tree.body
              if (isinstance(n, ast.FunctionDef) and n.name == name)
              or (isinstance(n, ast.Assign)
                  and any(getattr(t, "id", "").startswith("_STEP_") for t in n.targets))]
    namespace = {"os": os}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<child>", "exec"), namespace)
    return namespace[name]


def test_h8_the_child_refuses_to_publish_a_truncated_export(tmp_path):
    """@brief Positive and negative for the child's own check, same rule as the packaging."""
    from kicadgen.services.kicad_library import step_file_is_complete
    check = _child_function("_step_export_complete")
    cases = {"cut.step": "ISO-10303-21;\nDATA;\n#1=", "whole.step": "ISO-10303-21;\nEND-ISO-10303-21;\n \n"}
    for name, text in cases.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
        assert check(str(tmp_path / name)) == step_file_is_complete(tmp_path / name)
    assert not check(str(tmp_path / "cut.step")) and check(str(tmp_path / "whole.step"))
    assert not check(str(tmp_path / "missing.step"))
    source = (CAD_DIR / "freecad_build_body.py").read_text(encoding="utf-8")
    guard, publish = source.index("if not _step_export_complete(step_staged)"), \
        source.index("_publish(step_staged, step_path)")
    assert guard < publish, "the check runs before the rename"


# ── H9: the FreeCAD log is capped ─────────────────────────────────────────

def test_h9_a_log_over_the_cap_keeps_its_tail_from_a_line_start(tmp_path):
    """@brief Positive: trimmed atomically, the newest lines kept whole."""
    log = tmp_path / "freecad.log"
    log.write_bytes(b"".join(f"old line {i:06d}\n".encode() for i in range(2000)))
    size = log.stat().st_size
    assert process_log.cap_log(log, limit=size - 1, keep=1000)
    text = log.read_text(encoding="utf-8")
    first, *rest = text.splitlines()
    assert first.startswith("[log trimmed:")
    assert rest[-1] == "old line 001999"
    assert all(line.startswith("old line ") and len(line) == len("old line 000000") for line in rest)
    assert log.stat().st_size <= 1000 + len(first) + 1
    assert [p.name for p in tmp_path.iterdir()] == ["freecad.log"]


def test_h9_a_log_under_the_cap_is_untouched(tmp_path):
    """@brief Negative: below the limit the file is not rewritten."""
    log = tmp_path / "freecad.log"
    log.write_text("one run\n", encoding="utf-8")
    before = log.stat().st_mtime_ns
    assert not process_log.cap_log(log, limit=1024, keep=512)
    assert log.read_text(encoding="utf-8") == "one run\n" and log.stat().st_mtime_ns == before
    assert not process_log.cap_log(tmp_path / "missing.log")


def test_h9_a_failed_replace_leaves_the_whole_log(monkeypatch, tmp_path):
    """@brief Negative: the file open elsewhere: nothing is lost, no temporary is left."""
    log = tmp_path / "freecad.log"
    log.write_text("x\n" * 600, encoding="utf-8")
    monkeypatch.setattr(process_log.os, "replace",
                        lambda *a: (_ for _ in ()).throw(PermissionError(32, "in use")))
    assert not process_log.cap_log(log, limit=100, keep=50)
    assert log.read_text(encoding="utf-8") == "x\n" * 600
    assert [p.name for p in tmp_path.iterdir()] == ["freecad.log"]


def test_h9_run_logged_caps_the_log_before_appending(monkeypatch, tmp_path):
    """@brief Integration: a run starts by trimming an oversized log, then appends to it."""
    log = tmp_path / "freecad.log"
    log.write_bytes(b"old\n" * (process_log.LOG_CAP_BYTES // 4 + 10))
    process_log.run_logged([sys.executable, "-c", "print('fresh run')"], log, timeout=60)
    assert log.stat().st_size < process_log.LOG_KEEP_BYTES + 4096
    assert log.read_text(encoding="utf-8").rstrip().splitlines()[-1].startswith("[")
    assert "fresh run" in log.read_text(encoding="utf-8")


# ── model_candidates(cancel=...): the running freecadcmd is killed ────────

def _record_popen(monkeypatch):
    """@brief Record every child the measuring batch launches."""
    children = []
    real = subprocess.Popen

    def recording(*args, **kwargs):
        process = real(*args, **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", recording)
    return children


def test_cancel_stops_model_candidates_and_kills_the_measuring_child(
        fake_reader, monkeypatch, tmp_path):
    """@brief Positive: a cancel set during the measuring kills freecadcmd promptly.

    The fake reader would work for 60 s on the model; the call must end with
    MatchingCancelled within seconds, and the recorded child must be dead
    (proved on its process handle, not by elapsed time alone).
    """
    root = _scope_library(tmp_path)
    flag = tmp_path / "hang.flag"
    monkeypatch.setenv("FAKE_HANG", "SOT-223.step")
    monkeypatch.setenv("FAKE_HANG_FLAG", str(flag))
    children = _record_popen(monkeypatch)
    cancel = threading.Event()

    def cancel_when_reading():
        deadline = time.monotonic() + 30
        while not flag.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        cancel.set()
    threading.Thread(target=cancel_when_reading, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(km.MatchingCancelled):
        km.model_candidates(dict(_SOT), library=root, cancel=cancel)
    assert flag.exists(), "the child was really reading when the cancel arrived"
    assert time.monotonic() - started < 30
    assert children and all(child.poll() is not None for child in children)


def test_a_cancel_channel_never_set_measures_as_without_one(fake_reader, monkeypatch, tmp_path):
    """@brief Negative: the same call with an idle channel measures and reports normally."""
    root = _scope_library(tmp_path)
    children = _record_popen(monkeypatch)
    report = km.model_candidates(dict(_SOT), library=root, cancel=threading.Event())
    assert report["discovery"] == "package", report
    # The child ran to its end and every candidate's measure reached the index.
    assert children and all(child.returncode == 0 for child in children)
    index = km.load_index(root)
    assert all(index.get(km.index_key(p)) for p in root.rglob("*.step"))


def test_a_cancel_during_a_footprint_rebuild_stops_it_and_keeps_what_was_read(
        footprint_tree, monkeypatch):
    """@brief Positive: the rebuild stops between two files; the next one resumes."""
    model = footprint_tree / "A.3dshapes" / "R.step"
    cancel = threading.Event()
    reads = []
    real = km._scan_link_hints

    def read_then_cancel(path):
        reads.append(path.name)
        cancel.set()  # the person gives up while the first file is read
        return real(path)
    monkeypatch.setattr(km, "_scan_link_hints", read_then_cancel)
    with pytest.raises(km.MatchingCancelled):
        km.footprint_sizes([model], footprint_tree, cancel=cancel)
    assert len(reads) == 1
    monkeypatch.setattr(km, "_scan_link_hints", lambda path: reads.append(path.name) or real(path))
    km.clear_footprint_index()
    sizes = km.footprint_sizes([model], footprint_tree)
    assert len(reads) == 3 and reads[0] not in reads[1:], "the file read before the stop is kept"
    assert [Path(r["source"]).name for r in sizes[str(model)]] == ["R_fp.kicad_mod"]


def test_an_idle_cancel_channel_leaves_the_footprint_rebuild_unchanged(footprint_tree):
    """@brief Negative: a channel never set gives the result of a call without one."""
    model = footprint_tree / "A.3dshapes" / "R.step"
    with_channel = km.footprint_sizes([model], footprint_tree, cancel=threading.Event())
    km.clear_footprint_index()
    assert km.footprint_sizes([model], footprint_tree) == with_channel


def test_h5_the_public_listing_scope_shares_one_walk_among_calls(monkeypatch, tmp_path, user_cache):
    """@brief Positive: inside `listing_scope` two calls walk the root once (negative: H5 above)."""
    root = _scope_library(tmp_path)
    walks = []
    real_walk = km._walk_model_files
    monkeypatch.setattr(km, "_walk_model_files", lambda r: walks.append(r) or real_walk(r))
    monkeypatch.setattr(km, "measure_models", lambda paths, **kw: {})
    with km.listing_scope():
        km.model_candidates(dict(_SOT), library=root)
        km.model_candidates(dict(_SOT), library=root)
    assert len(walks) == 1
