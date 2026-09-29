"""@brief HANDOFF 158: the copied body and the pasted ones are outlined in the 3D view.

User, 2026-09-29: «il componente selezionato per la copia deve in qualche modo
evidenziarsi». While «Copia formato» is on, the copied body wears a yellow
wire box and every pasted body a green one; the bodies keep their colours,
the boxes go when the brush is switched off or the mesh is rebuilt.
No render window here (an OpenGL window next to Qt crashes the test process):
the view is exercised through its unbound methods on a stand-in, as the
picking tests do.
"""

from types import SimpleNamespace

import pytest

pytest.importorskip('pyvistaqt')
from kicadgen.gui.preview_3d_vtk import (
    FORMAT_OUTLINE_GAP, FORMAT_OUTLINE_MIN_GAP_MM, FORMAT_SOURCE_COLOR, FORMAT_TARGET_COLOR,
    Preview3DVtk,
)
from tests.test_photo_inventory_gui import gui  # noqa: F401  (window fixture)


def _actor(bounds):
    return SimpleNamespace(GetBounds=lambda: bounds)


def _viewer(row_actors):
    added, removed, renders = [], [], []
    viewer = SimpleNamespace(
        _row_actors=row_actors, _format_outlines=[],
        add_mesh=lambda mesh, **kw: added.append((mesh, kw)) or SimpleNamespace(name=len(added)),
        remove_actor=lambda actor, **kw: removed.append(actor),
        render=lambda: renders.append(1),
        clear_format_outlines=lambda render=True: Preview3DVtk.clear_format_outlines(viewer, render),
        _row_bounds=lambda row: Preview3DVtk._row_bounds(viewer, row),
    )
    return viewer, added, removed, renders


def test_source_yellow_targets_green_boxes_around_the_bodies():
    """@brief Positive: one box per row, colour by role, larger than the body by the gap."""
    viewer, added, removed, renders = _viewer({
        0: [_actor((0.0, 4.0, 0.0, 2.0, 0.0, 1.0)), _actor((4.0, 5.0, 0.0, 2.0, 0.0, 0.5))],
        3: [_actor((10.0, 11.0, 10.0, 10.5, 0.0, 0.3))],
    })
    assert Preview3DVtk.highlight_format_rows(viewer, 0, [3]) == 2
    (source_box, source_kw), (target_box, target_kw) = added
    assert source_kw['color'] == FORMAT_SOURCE_COLOR and target_kw['color'] == FORMAT_TARGET_COLOR
    assert source_kw['pickable'] is False and source_kw['render'] is False
    # Parts of the same row are merged, then padded by 6% of the largest extent.
    gap = FORMAT_OUTLINE_GAP * 5.0
    assert source_box.bounds == pytest.approx((-gap, 5.0 + gap, -gap, 2.0 + gap, -gap, 1.0 + gap))
    # A tiny body gets the floor gap, not a hairline.
    assert target_box.bounds == pytest.approx((10.0 - FORMAT_OUTLINE_MIN_GAP_MM, 11.0 + FORMAT_OUTLINE_MIN_GAP_MM,
                                               10.0 - FORMAT_OUTLINE_MIN_GAP_MM, 10.5 + FORMAT_OUTLINE_MIN_GAP_MM,
                                               -FORMAT_OUTLINE_MIN_GAP_MM, 0.3 + FORMAT_OUTLINE_MIN_GAP_MM))
    assert len(viewer._format_outlines) == 2 and renders == [1]


def test_a_new_call_replaces_the_old_boxes_and_clear_removes_them_all():
    """@brief The set on screen is always the current source and targets."""
    viewer, added, removed, renders = _viewer({0: [_actor((0, 1, 0, 1, 0, 1))],
                                              1: [_actor((2, 3, 0, 1, 0, 1))],
                                              2: [_actor((4, 5, 0, 1, 0, 1))]})
    Preview3DVtk.highlight_format_rows(viewer, 0, [1])
    first = list(viewer._format_outlines)
    Preview3DVtk.highlight_format_rows(viewer, 0, [1, 2])
    assert removed == first and len(viewer._format_outlines) == 3
    Preview3DVtk.clear_format_outlines(viewer)
    assert viewer._format_outlines == [] and len(removed) == 5
    assert renders == [1, 1, 1]


def test_rows_without_actors_and_no_source_draw_nothing():
    """@brief Negative: a row not in this mesh, or no source, is not an error."""
    viewer, added, removed, renders = _viewer({0: [_actor((0, 1, 0, 1, 0, 1))]})
    assert Preview3DVtk.highlight_format_rows(viewer, 7, [8]) == 0
    assert added == [] and viewer._format_outlines == []
    assert Preview3DVtk.highlight_format_rows(viewer, None, [0]) == 1
    assert added[0][1]['color'] == FORMAT_TARGET_COLOR


def _rows():
    return [dict(name=name, kind="generic", x_mm=x, y_mm=0.0, length_mm=3.0, width_mm=1.0,
                 height_mm=0.5, rotation_deg=0.0, color_hex="#FF00FF", marking="")
            for name, x in (("Q1", -10.0), ("Q2", 0.0), ("Q3", 10.0))]


def test_the_window_outlines_the_source_then_every_target_and_clears_on_switch_off(
        window, monkeypatch):  # noqa: F811
    """@brief End to end on the window, with the 3D view's outline calls recorded."""
    calls = []
    monkeypatch.setattr(window.preview_3d, "highlight_format_rows",
                        lambda source, targets: calls.append(("show", source, list(targets))),
                        raising=False)
    monkeypatch.setattr(window.preview_3d, "clear_format_outlines",
                        lambda render=True: calls.append(("clear",)), raising=False)
    monkeypatch.setattr(window, "_apply_photo_review", lambda components: None)
    window._set_photo_components(_rows())
    window.btn_format_painter_3d.setChecked(True)
    window._on_format_painter_click(1)          # copied
    window._on_format_painter_click(0)          # pasted
    window._on_format_painter_click(2)          # pasted
    window._on_format_painter_click(0)          # pasted again: listed once
    assert calls == [("show", 1, []), ("show", 1, [0]), ("show", 1, [0, 2]), ("show", 1, [0, 2])]
    window.btn_format_painter_3d.setChecked(False)
    assert calls[-1] == ("clear",)
    assert window._format_source_row is None and window._format_target_rows == []


def test_a_view_without_outlines_does_not_break_the_brush(window, monkeypatch):  # noqa: F811
    """@brief Negative: a stub view (no highlight methods) leaves the mode working."""
    for name in ("highlight_format_rows", "clear_format_outlines"):
        if hasattr(window.preview_3d, name):
            monkeypatch.delattr(window.preview_3d, name)
    monkeypatch.setattr(window, "_apply_photo_review", lambda components: None)
    window._set_photo_components(_rows())
    window.btn_format_painter_3d.setChecked(True)
    window._on_format_painter_click(1)
    window._on_format_painter_click(0)
    assert window._format_source_row == 1 and window._format_target_rows == [0]
    window.btn_format_painter_3d.setChecked(False)
