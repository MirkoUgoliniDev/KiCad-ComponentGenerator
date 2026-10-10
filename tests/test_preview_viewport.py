"""Regression checks for contact labels and the visible 3D viewport."""
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("pyvistaqt")
from kicadgen.gui.preview_3d_vtk import Preview3DVtk, _spaced_label


@pytest.mark.parametrize("direction", [(1, 0), (-1, 0), (0, 1), (0, -1)])
def test_legacy_contact_label_moves_away_from_metal(direction):
    vertices = np.array([[-.5, -.5, -.1], [.5, .5, 1.]])
    label = [direction[0] * 1.8, direction[1] * 1.8, -.1]
    original = list(label)
    moved = _spaced_label(label, vertices)
    # Approved legacy clearance is an additional 3.2 mm, not an absolute
    # coordinate of 3 mm. Preserve the direction and the original anchor.
    assert moved[:2] - np.array(label[:2]) == pytest.approx(np.array(direction) * 3.2)
    assert moved[2] == -.1
    assert label == original


@pytest.mark.parametrize("width,height,ratio", [(400, 200, 1), (230, 500, 1.5), (800, 300, 2)])
def test_fit_uses_current_widget_size_and_pixel_ratio(width, height, ratio):
    calls = []
    viewer = SimpleNamespace(
        width=lambda: width, height=lambda: height, devicePixelRatioF=lambda: ratio,
        reset_camera=lambda: calls.append("reset"),
        camera=SimpleNamespace(zoom=lambda value: calls.append(value)),
        _update_label_visibility=lambda: None, render=lambda: None,
    )
    Preview3DVtk.fit_view(viewer)
    assert viewer.window_size == (round(width * ratio), round(height * ratio))
    assert calls == ["reset", .85]


def test_planar_label_is_centered_on_its_anchor_without_moving_camera():
    captured = {}
    def add(mesh, **kwargs):
        captured['mesh'] = mesh
        captured.update(kwargs)
        return "actor"
    # Labels now are planar geometry, so their reading direction follows the
    # board instead of independently turning toward the camera as billboards.
    viewer = SimpleNamespace(show_pin_numbers=True, add_mesh=add)
    Preview3DVtk._add_pin_labels(viewer, [(1, 1., 2., -.1)])
    mesh = captured['mesh']
    assert mesh.n_points > 0
    assert mesh.center == pytest.approx((1., 2., -.1))
    assert mesh.points[:, 2] == pytest.approx(np.full(mesh.n_points, -.1))
    assert captured['reset_camera'] is False
    assert captured['pickable'] is False
    assert captured['lighting'] is False
    assert viewer._label_actor == 'actor'
