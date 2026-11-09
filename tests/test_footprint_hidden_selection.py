"""@brief A drawing selected before its layer is hidden is no longer a keyboard target (09/11 sera, L2).

Rule already used by the rubber band and by the pointer fallback of
`MainWindow._fp_targets`: never act through a hidden layer. Before the fix a
drawing selected and then hidden through the «Layer» panel stayed the target
of Delete, R, Ctrl+D, Ctrl+C and the arrows while invisible.
"""
from copy import deepcopy

import pytest

pytest.importorskip("PyQt6")

from kicadgen.core.footprint import validate_footprint_drawings  # noqa: E402


def _record(layer, x0=0.0):
    """A validated-able line record on `layer`."""
    return {"kind": "line", "layer": layer, "width": 0.12, "points": [[x0, 0.0], [x0 + 5.0, 5.0]]}


@pytest.fixture
def preview(window, monkeypatch):
    """The window with one F.SilkS line (index 0) and one F.Fab line (index 1) drawn; no file is written."""
    monkeypatch.setattr(window, "_generate", lambda **kwargs: True)
    monkeypatch.setattr(window, "_project_has_footprint", lambda: True)
    monkeypatch.setattr(window, "_save_config_safely", lambda: True)
    window.chk_grid.setChecked(False)
    window.footprint_drawings = validate_footprint_drawings([_record("F.SilkS"), _record("F.Fab", 10.0)])
    window._draw_preview()
    return window


def test_hiding_the_selected_drawings_layer_leaves_nothing_to_delete(preview):
    """Positive: select on F.SilkS, hide F.SilkS -> deselected, no target, Delete removes nothing."""
    window = preview
    window._select_footprint_drawing(0)
    assert window._fp_targets() == [0]
    before = deepcopy(window.footprint_drawings)
    window._on_preview_layer_toggled("F.SilkS", False)
    assert window._fp_selected_indices() == []
    assert window._fp_targets() == []
    window._delete_hovered_drawing()
    assert window.footprint_drawings == before
    # Showing the layer again does not reselect it.
    window._on_preview_layer_toggled("F.SilkS", True)
    assert window._fp_selected_indices() == []


def test_hiding_another_layer_keeps_the_selection_and_delete_acts(preview):
    """Negative: hiding F.Fab keeps the F.SilkS selection, and Delete removes it."""
    window = preview
    window._select_footprint_drawing(0)
    window._on_preview_layer_toggled("F.Fab", False)
    assert window._fp_selected_indices() == [0]
    assert window._fp_targets() == [0]
    window._delete_hovered_drawing()
    assert [d["layer"] for d in window.footprint_drawings] == ["F.Fab"]


def test_a_mixed_selection_keeps_only_the_visible_drawing(preview):
    """Both selected, F.Fab hidden: only the F.SilkS line stays selected and targeted."""
    window = preview
    window._select_footprint_drawing(0)
    window._select_footprint_drawing(1, additive=True)
    window._on_preview_layer_toggled("F.Fab", False)
    assert window._fp_selected_indices() == [0]
    assert window._fp_targets() == [0]


def test_targets_filter_a_hidden_selected_drawing_even_without_the_toggle(preview, monkeypatch):
    """`_fp_targets` itself never returns a hidden drawing (a layer hidden by the saved preference)."""
    window = preview
    window._select_footprint_drawing(0)
    monkeypatch.setitem(window._config, "preview_layers", {"F.SilkS": False})
    assert window._fp_targets() == []
    monkeypatch.setitem(window._config, "preview_layers", {"F.SilkS": True})
    assert window._fp_targets() == [0]
