"""@brief «REF**» and the value of the footprint can be dragged in the 2D preview and keep their place (user, 2026-10-13).

The preview draws the footprint's own Reference (silkscreen) and Value
(fabrication layer) texts where the file puts them; a drag drops them on
the preview grid, the choice is saved in the project (`footprint_texts`),
the preview is redrawn and the .kicad_mod is written again, like a pad edit.
A footprint without a choice keeps the positions of before.
"""
import re
from copy import deepcopy

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402

from kicadgen.core.footprint import (  # noqa: E402
    DEFAULT_TEXT_POSITIONS, TEXT_KEYS, Footprint, Pad, default_text_positions,
    validate_text_positions,
)
from kicadgen.gui.footprint_view import TEXT_KEY_ROLE  # noqa: E402


def _property_at(text, key):
    match = re.search(r'\(property "%s" "[^"]*"\s*\(at ([-\d.]+) ([-\d.]+)' % key, text)
    assert match, f"{key} property with a position"
    return float(match.group(1)), float(match.group(2))


# ── Model ────────────────────────────────────────────────────────────

def test_a_footprint_without_a_choice_keeps_the_positions_of_before():
    text = Footprint(name="X").render()
    assert _property_at(text, "Reference") == DEFAULT_TEXT_POSITIONS["Reference"]
    assert _property_at(text, "Value") == DEFAULT_TEXT_POSITIONS["Value"]


def test_place_texts_moves_only_the_given_text():
    fp = Footprint(name="X").place_texts({"Reference": (3.5, -4.25)})
    text = fp.render()
    assert _property_at(text, "Reference") == (3.5, -4.25)
    assert _property_at(text, "Value") == DEFAULT_TEXT_POSITIONS["Value"]
    assert Footprint(name="X").place_texts(None).reference_at == DEFAULT_TEXT_POSITIONS["Reference"]


@pytest.mark.parametrize("bad", [{"Datasheet": (0, 0)}, {"Value": (0,)}, {"Value": (0, float("inf"))},
                                 {"Value": (0, True)}, {"Value": (0, 501)}])
def test_place_texts_refuses_what_does_not_validate(bad):
    with pytest.raises(ValueError):
        Footprint(name="X").place_texts(bad)


def test_validate_keeps_the_well_formed_entries_and_drops_the_rest():
    raw = {"Reference": [1, -2], "Value": ["a", 0], "Footprint": [0, 0], "extra": None}
    assert validate_text_positions(raw) == {"Reference": (1.0, -2.0)}
    assert validate_text_positions(None) == {} and validate_text_positions([1, 2]) == {}
    assert validate_text_positions({"Value": [0.12345, 500]}) == {"Value": (0.1235, 500.0)}
    assert validate_text_positions({"Value": [0, 500.1]}) == {}


# ── Worker ───────────────────────────────────────────────────────────

def test_the_worker_writes_the_chosen_position_in_the_file(tmp_path, qapp):
    from kicadgen.generators.pin_header import PinHeaderParams
    from kicadgen.gui.workers import GenerateWorker
    params = PinHeaderParams(name="H", rows=2, cols=3, pitch=2.54, row_spacing=2.54, drill=1.0,
                             pad_diameter=1.7)
    worker = GenerateWorker(params, tmp_path)
    worker.text_positions = {"Value": (0.0, 6.5)}
    written = []
    worker.done.connect(written.append)
    worker.run()
    assert written, "the footprint was written"
    text = (tmp_path / "H.pretty" / "H.kicad_mod").read_text(encoding="utf-8")
    assert _property_at(text, "Value") == (0.0, 6.5)
    # CONTRACT CHANGED 2026-10-14 (decision D1): the text without a choice
    # takes the geometry-derived default, no longer the fixed (0, -2).
    from kicadgen.generators.pin_header import build_pin_header
    assert _property_at(text, "Reference") == default_text_positions(build_pin_header(params))["Reference"]


# ── Window ───────────────────────────────────────────────────────────

def _text_items(window):
    """The text groups by key (their position is the anchor)."""
    return {item.data(TEXT_KEY_ROLE): item for item in window.preview_scene.items()
            if item.data(TEXT_KEY_ROLE) is not None and item.parentItem() is None}


def _text_of(group):
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    return next(c for c in group.childItems() if isinstance(c, QGraphicsSimpleTextItem)).text()


@pytest.fixture
def preview(window, monkeypatch):
    """The window with its preview drawn, project saves and regenerations recorded instead of run."""
    saves, generations = [], []
    real_save = window._save_project_config
    # Recorded AND done: the close guard compares the saved state with the live one.
    monkeypatch.setattr(window, "_save_project_config",
                        lambda force=False: saves.append(force) or real_save(force=force))
    monkeypatch.setattr(window, "_generate", lambda **kwargs: generations.append(kwargs) or True)
    # The .kicad_mod rewrite runs on the next event-loop turn; these tests
    # check what is rewritten, so the turn is run right away.
    monkeypatch.setattr(window, "_schedule_footprint_rewrite",
                        lambda: window._generate(footprint_only=True))
    window._draw_preview()
    assert _text_items(window), "the preview must draw the texts for these tests"
    return window, saves, generations


def test_both_texts_are_drawn_at_their_default_position(preview):
    window, _saves, _generations = preview
    items = _text_items(window)
    assert set(items) == set(TEXT_KEYS)
    assert _text_of(items["Reference"]) == "REF**"
    assert _text_of(items["Value"]) == (window._component_name() or "Value")
    # CONTRACT CHANGED 2026-10-14 (decision D1): the preview shows the same
    # geometry-derived default the generator writes, not the fixed positions.
    from kicadgen.core.package_geometry import PackageGeometry
    from kicadgen.generators.castellated_module import CastellatedModuleParams, build_castellated_module
    from kicadgen.generators.package import build_package
    from kicadgen.generators.pin_header import build_pin_header
    params = window._preview_params()
    built = (build_package(params) if isinstance(params, PackageGeometry) else
             build_castellated_module(params) if isinstance(params, CastellatedModuleParams)
             else build_pin_header(params))
    defaults = default_text_positions(built)
    assert defaults != DEFAULT_TEXT_POSITIONS
    for key, item in items.items():
        assert (item.pos().x(), item.pos().y()) == pytest.approx(tuple(v * 20 for v in defaults[key]))
        # The anchor is visible: a cross of two lines centred on it, and the
        # text centred on it too.
        from PyQt6.QtWidgets import QGraphicsLineItem
        lines = [c for c in item.childItems() if isinstance(c, QGraphicsLineItem)]
        assert len(lines) == 2
        assert all(line.line().center().isNull() for line in lines)
        centre = item.sceneBoundingRect().center()
        assert centre.x() == pytest.approx(item.pos().x(), abs=1) and centre.y() == pytest.approx(item.pos().y(), abs=1)
        assert "Ancoraggio" in item.toolTip() and all(c.toolTip() == item.toolTip() for c in item.childItems())


def test_a_drop_snaps_to_the_preview_grid_saves_redraws_and_rewrites(preview):
    window, saves, generations = preview
    window.chk_grid.setChecked(True)
    window.combo_grid_unit.setCurrentIndex(window.combo_grid_unit.findData("mm"))
    # Texts are graphic elements: they take the drawing grid, 1/10 of the
    # 1 mm grid by default; with divisor 1 they take the main grid.
    window._on_footprint_text_moved("Reference", 3.43 * 20, -4.58 * 20)
    assert window.footprint_text_positions == {"Reference": (3.4, -4.6)}
    assert saves == [True] and generations == [{"footprint_only": True}]
    anchor = _text_items(window)["Reference"].pos()
    assert (anchor.x(), anchor.y()) == (pytest.approx(68), pytest.approx(-92))
    assert window._project_state()["footprint_texts"] == {"Reference": [3.4, -4.6]}
    window.combo_fine_grid.setCurrentIndex(window.combo_fine_grid.findData(1))
    window._on_footprint_text_moved("Reference", 3.43 * 20, -4.58 * 20)
    assert window.footprint_text_positions == {"Reference": (3.0, -5.0)}


def test_without_the_grid_the_drop_snaps_to_a_tenth_of_a_millimetre(preview):
    window, _saves, _generations = preview
    window.chk_grid.setChecked(False)
    window._on_footprint_text_moved("Value", 1.23 * 20, 4.56 * 20)
    assert window.footprint_text_positions == {"Value": (1.2, 4.6)}


def test_dragging_the_text_with_the_mouse_reports_the_drop(preview):
    window, _saves, _generations = preview
    view = window.preview_view
    view.resize(800, 600)
    window.chk_grid.setChecked(False)
    item = _text_items(window)["Reference"]
    start_scene = item.pos()
    start = view.mapFromScene(start_scene)
    end = view.mapFromScene(start_scene + QPointF(100, 40))

    def mouse(kind, pos, button=Qt.MouseButton.LeftButton):
        return QMouseEvent(kind, QPointF(pos), QPointF(view.viewport().mapToGlobal(pos)), button,
                           button, Qt.KeyboardModifier.NoModifier)

    view.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, start))
    view.mouseMoveEvent(mouse(QEvent.Type.MouseMove, end))
    view.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, end))
    expected = ((start_scene.x() + 100) / 20, (start_scene.y() + 40) / 20)
    chosen = window.footprint_text_positions["Reference"]
    assert chosen[0] == pytest.approx(expected[0], abs=0.11) and chosen[1] == pytest.approx(expected[1], abs=0.11)


def test_a_left_press_on_the_background_still_pans(preview):
    window, _saves, _generations = preview
    view = window.preview_view
    far = view.mapFromScene(QPointF(-5000, -5000))
    event = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(far), QPointF(view.viewport().mapToGlobal(far)),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(event)
    assert view._pan is not None and view._text_drag is None
    view._pan = None


def test_the_default_position_comes_back_and_a_failed_save_keeps_the_previous_choice(preview, monkeypatch):
    window, saves, generations = preview
    window._on_footprint_text_moved("Value", 0, 100)
    assert window.footprint_text_positions == {"Value": (0.0, 5.0)}
    assert window._set_footprint_text_position("Value", None)
    assert window.footprint_text_positions == {} and len(generations) == 2
    assert window._set_footprint_text_position("Value", None), "already default: nothing to do"
    assert len(saves) == 2, "no save when nothing changed"
    from PyQt6.QtWidgets import QMessageBox
    monkeypatch.setattr(window, "_save_project_config", lambda force=False: False)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    assert not window._set_footprint_text_position("Value", (1.0, 1.0))
    assert window.footprint_text_positions == {}, "the previous choice stays when the project is not written"
    assert len(generations) == 2, "nothing rewritten"


def test_an_unknown_key_is_ignored(preview):
    window, saves, _generations = preview
    assert not window._set_footprint_text_position("Datasheet", (1.0, 1.0))
    assert saves == []


def test_the_choice_survives_the_project_file_and_malformed_entries_are_dropped(window):
    state = deepcopy(window._project_state())
    state["footprint_texts"] = {"Reference": [2.5, -7], "Value": "nope", "Other": [0, 0]}
    window._apply_project_state(state)
    assert window.footprint_text_positions == {"Reference": (2.5, -7.0)}
    state["footprint_texts"] = None
    window._apply_project_state(state)
    assert window.footprint_text_positions == {}


def test_the_generation_worker_receives_the_positions(window, monkeypatch):
    from kicadgen.gui import main_window as gui
    from test_package_project_gui import acquire
    acquire(window)
    started = []
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda worker: started.append(worker))
    window.footprint_text_positions = {"Value": (0.0, 9.0)}
    window._generate(footprint_only=True)
    assert started and started[0].text_positions == {"Value": (0.0, 9.0)}
    started[0].text_positions["Value"] = (1.0, 1.0)
    assert window.footprint_text_positions == {"Value": (0.0, 9.0)}, "the worker got a copy"
