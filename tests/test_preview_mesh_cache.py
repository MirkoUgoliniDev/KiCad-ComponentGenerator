"""@brief Rientro nel tab 3D senza ricostruire la scena (nota 10 dell'utente).

Sulla scheda dell'utente l'anteprima JSON pesa ~13 MB e la finestra richiama
`render_mesh(path)` a ogni ingresso nel tab 3D. Il file mostrato e invariato
non va riletto ne' ridisegnato; un file nuovo o modificato si'. Una scena gia'
preparata (normali calcolate) si riusa dopo `clear_preview()`.
"""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("pyvistaqt")

from kicadgen.gui import preview_3d_vtk as module  # noqa: E402
from kicadgen.gui.preview_3d_vtk import Preview3DVtk  # noqa: E402

_TRIANGLE = dict(vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], triangles=[[0, 1, 2]])


def _write(path, names, shift=0.0, **extra):
    parts = []
    for name in names:
        part = dict(name=name, vertices=[[x + shift, y, z] for x, y, z in _TRIANGLE["vertices"]],
                    triangles=_TRIANGLE["triangles"], color="#123456")
        if name.startswith("Cmp"):
            part["source_row"] = int(name[3:name.index("_")]) + 10
        if name.startswith("Pin"):
            part["label"] = [0.5 + shift, 0.5, -0.1]
        part.update(extra)
        parts.append(part)
    path.write_text(json.dumps({"parts": parts}), encoding="utf-8")
    return path


@pytest.fixture
def view(qapp):
    app = qapp
    widget = Preview3DVtk()
    widget.resize(400, 300)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    app.processEvents()


@pytest.fixture
def counters(monkeypatch):
    """Conta quante volte si legge un file e quante mesh si aggiungono."""
    counts = {"prepare": 0, "add_mesh": 0}
    real_prepare = module._prepare_mesh_scene

    def prepare(path):
        counts["prepare"] += 1
        return real_prepare(path)

    real_add = Preview3DVtk.add_mesh

    def add_mesh(self, *args, **kwargs):
        counts["add_mesh"] += 1
        return real_add(self, *args, **kwargs)

    monkeypatch.setattr(module, "_prepare_mesh_scene", prepare)
    monkeypatch.setattr(Preview3DVtk, "add_mesh", add_mesh)
    return counts


def _rows_of_actors(view):
    return sorted(view._component_actors.values())


def test_the_same_unchanged_file_is_not_rebuilt(view, counters, tmp_path):
    path = _write(tmp_path / "m.preview.json", ["Cmp0_U1", "Cmp2_J1", "Pin1", "Body"])
    assert not view.is_showing(path)
    view.render_mesh(path)
    assert view.is_showing(path)
    assert counters == {"prepare": 1, "add_mesh": 4}
    actors = dict(view._component_actors)
    # L'utente ruota la vista, cambia tab e rientra: niente si ricostruisce.
    view.camera.azimuth = 70
    camera = view.camera_position
    view.render_mesh(path)
    view.render_mesh(str(path))          # stessa identita' anche come stringa
    assert counters == {"prepare": 1, "add_mesh": 4}
    assert view.camera_position == camera
    assert view._component_actors == actors


def test_a_changed_file_is_read_again(view, counters, tmp_path):
    """Negativo della scorciatoia: nuova dimensione o nuovo mtime -> si ricostruisce."""
    path = _write(tmp_path / "m.preview.json", ["Cmp0_U1", "Body"])
    view.render_mesh(path)
    _write(path, ["Cmp0_U1", "Cmp3_C1", "Body"])            # rigenerato: altro contenuto
    assert not view.is_showing(path)
    view.render_mesh(path)
    assert counters["prepare"] == 2
    assert _rows_of_actors(view) == [10, 13]
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))  # stesso contenuto
    assert not view.is_showing(path)
    view.render_mesh(path)
    assert counters["prepare"] == 3
    assert view.is_showing(path)


def test_after_clear_the_cached_scene_is_redrawn_without_reading_the_file(view, counters, tmp_path):
    path = _write(tmp_path / "m.preview.json", ["Cmp0_U1", "Cmp2_J1", "Pin1", "Pin2", "Body"])
    view.render_mesh(path)
    old_actors = set(view._component_actors)
    view.clear_preview()
    assert not view.is_showing(path)
    assert view._component_actors == {}
    view.render_mesh(path)
    assert counters["prepare"] == 1                  # dalla cache
    assert counters["add_mesh"] == 10                # ma la scena si ridisegna
    assert view.is_showing(path)
    # Il picking punta agli attori ora in scena (VTK puo' riusare gli
    # indirizzi di quelli rimossi: conta che ogni chiave sia un attore vivo).
    assert _rows_of_actors(view) == [10, 12]
    live = {actor.GetAddressAsString(''): actor for actor in view.renderer.actors.values()}
    assert set(view._component_actors) <= set(live)
    picked = []
    view.component_requested.connect(picked.append)
    for address in view._component_actors:
        view._component_picked(live[address])
    assert sorted(picked) == [10, 12]
    assert old_actors  # la prima scena aveva davvero attori mappati
    assert view._label_actor is not None


def test_the_parametric_preview_invalidates_the_shown_mesh(view, tmp_path):
    from kicadgen.cad3d.body_params import Body3DParams
    from kicadgen.generators.pin_header import PinHeaderParams
    path = _write(tmp_path / "m.preview.json", ["Cmp0_U1", "Body"])
    view.render_mesh(path)
    view.render_params(PinHeaderParams(), Body3DParams(length_mm=10, width_mm=3, height_mm=2))
    assert not view.is_showing(path)
    assert view._component_actors == {}


def test_pin_numbers_turned_on_later_appear_without_rebuilding(view, counters, tmp_path):
    path = _write(tmp_path / "m.preview.json", ["Pin1", "Pin2", "Body"])
    view.set_pin_numbers(False)
    view.render_mesh(path)
    assert view._label_actor is None
    view.set_pin_numbers(True)
    view.render_mesh(path)                           # come fa la finestra al cambio
    assert counters["prepare"] == 1 and counters["add_mesh"] == 3
    assert view._label_actor is not None


def test_a_broken_file_leaves_the_scene_on_screen(view, tmp_path):
    good = _write(tmp_path / "good.preview.json", ["Cmp0_U1", "Body"])
    view.render_mesh(good)
    actors = dict(view._component_actors)
    empty = tmp_path / "empty.preview.json"
    empty.write_text(json.dumps({"parts": [dict(name="Body", vertices=[], triangles=[])]}))
    with pytest.raises(ValueError):
        view.render_mesh(empty)
    assert view.is_showing(good)
    assert view._component_actors == actors
    with pytest.raises(OSError):
        view.render_mesh(tmp_path / "missing.preview.json")
    assert view.is_showing(good)


@pytest.mark.parametrize("value", [None, "", 0])
def test_is_showing_is_false_for_missing_or_unusable_paths(view, tmp_path, value):
    path = _write(tmp_path / "m.preview.json", ["Body"])
    view.render_mesh(path)
    assert not view.is_showing(value)
    assert not view.is_showing(tmp_path / "other.preview.json")


def test_the_cache_keeps_only_the_most_recent_scenes(view, counters, tmp_path):
    paths = [_write(tmp_path / f"m{i}.preview.json", ["Body"], shift=i) for i in range(3)]
    for path in paths:
        view.render_mesh(path)
    assert len(view._mesh_cache) == module._MESH_CACHE_SIZE
    view.render_mesh(paths[-2])                      # ancora in cache
    assert counters["prepare"] == 3
    view.render_mesh(paths[0])                       # scartato: si rilegge
    assert counters["prepare"] == 4


def test_drawing_does_not_modify_the_cached_meshes(view, tmp_path):
    """La stessa PolyData si riusa: add_mesh non deve cambiarla."""
    path = _write(tmp_path / "m.preview.json", ["Cmp0_U1", "Body"])
    view.render_mesh(path)
    scene = next(iter(view._mesh_cache.values()))
    before = [(part.mesh.n_points, sorted(part.mesh.point_data.keys()),
               part.mesh.points.copy()) for part in scene.parts]
    view.clear_preview()
    view.render_mesh(path)
    after = [(part.mesh.n_points, sorted(part.mesh.point_data.keys()), part.mesh.points)
             for part in scene.parts]
    for (n0, keys0, points0), (n1, keys1, points1) in zip(before, after):
        assert (n0, keys0) == (n1, keys1)
        assert (points0 == points1).all()


def test_a_multi_part_mesh_is_drawn_with_one_render(view, tmp_path, monkeypatch):
    """Prima ogni add_mesh ridisegnava la scena: ~170 frame per un modulo reale."""
    from pyvista.plotting.plotter import BasePlotter
    effective = []
    real_render = BasePlotter.render

    def render(self):
        if not self._suppress_rendering:
            effective.append(1)
        return real_render(self)

    monkeypatch.setattr(BasePlotter, "render", render)
    names = [f"Cmp{i}_P" for i in range(12)] + ["Pin1", "Body"]
    view.render_mesh(_write(tmp_path / "m.preview.json", names))
    assert len(effective) == 1
    # Negativo: il rientro con lo stesso file non ridisegna nulla.
    view.render_mesh(tmp_path / "m.preview.json")
    assert len(effective) == 1
