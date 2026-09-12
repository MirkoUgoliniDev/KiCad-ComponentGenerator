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
    assert moved[:2] == pytest.approx(np.array(direction) * 3.)
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


def test_labels_are_centered_not_offset_over_contact():
    captured = {}
    def add(points, labels, **kwargs):
        captured.update(kwargs)
        return "actor"
    viewer = SimpleNamespace(show_pin_numbers=True, add_point_labels=add)
    Preview3DVtk._add_pin_labels(viewer, [(1, 1., 2., -.1)])
    assert captured["justification_horizontal"] == "center"
    assert captured["justification_vertical"] == "center"
