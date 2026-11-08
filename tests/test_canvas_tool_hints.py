"""@brief Unit tests of `kicadgen.gui.canvas_tool_hints` (pure, no Qt)."""
import ast
import pathlib

import pytest

from kicadgen.gui import canvas_tool_hints as hints
from kicadgen.gui.canvas_tool_hints import (ALL_HINTS, ANGLE_MODE_SHORTCUT, GENERIC_HINT, HINT_KEYS,
                                            PROPERTIES_SHORTCUT, SELECT_SHORTCUT, STEP_COUNTS,
                                            TOOL_SHORTCUTS, UNDO_POINT_SHORTCUT, step_hint, tooltip)

#: Keys the plan requires (A4, D4) plus the wire and the measuring tool.
REQUIRED = ("line", "polygon", "rectangle", "circle", "arc", "bezier", "text", "textbox", "dimension",
            "dimension_radial", "dimension_center", "dimension_leader", "wire", "measure")


def test_the_module_imports_no_qt():
    tree = ast.parse(pathlib.Path(hints.__file__).read_text(encoding="utf-8"))
    names = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    names += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not [n for n in names if "Qt" in n]


def test_every_required_key_is_known():
    assert set(REQUIRED) <= set(HINT_KEYS)


@pytest.mark.parametrize("key", REQUIRED)
def test_every_step_up_to_the_count_has_a_specific_hint(key):
    last = STEP_COUNTS.get(key, 4)
    for points in range(last + 1):
        text = step_hint(key, points)
        assert text and text != GENERIC_HINT and text in ALL_HINTS


@pytest.mark.parametrize("key", sorted(STEP_COUNTS))
def test_each_click_of_a_fixed_tool_has_its_own_text(key):
    texts = [step_hint(key, i) for i in range(STEP_COUNTS[key])]
    assert len(set(texts)) == len(texts)
    assert step_hint(key, STEP_COUNTS[key] + 5) == texts[-1]     # past the end: the last step


def test_unknown_key_gets_the_generic_hint():
    assert step_hint("lasso", 0) == GENERIC_HINT
    assert step_hint("line", 0) != GENERIC_HINT


def test_negative_point_count_is_the_first_step():
    assert step_hint("circle", -3) == step_hint("circle", 0) == "Clic sul centro"


def test_arc_order_is_start_end_mid():
    assert [step_hint("arc", i) for i in range(3)] == [
        "Clic sul punto iniziale dell'arco", "Clic sul punto finale dell'arco",
        "Clic su un punto intermedio dell'arco"]


def test_bezier_order_is_start_two_controls_end():
    texts = [step_hint("bezier", i) for i in range(4)]
    assert "iniziale" in texts[0] and "primo punto di controllo" in texts[1]
    assert "secondo punto di controllo" in texts[2] and "finale" in texts[3]


def test_line_mentions_closing_only_when_closable():
    assert "primo punto" not in step_hint("line", 3).split(";", 1)[1]
    assert "clic sul primo punto" in step_hint("line", 3, closable=True)
    assert step_hint("line", 1, closable=True) == step_hint("line", 1)   # cannot finish yet


def test_line_offers_finishing_from_two_points_not_before():
    assert "Invio" not in step_hint("line", 1)
    assert "Invio" in step_hint("line", 2)


def test_polygon_offers_closing_from_three_vertices_not_before():
    assert "chiudere il poligono" not in step_hint("polygon", 2)
    assert "chiudere il poligono" in step_hint("polygon", 3)


def test_open_tools_mention_backspace_once_a_point_exists():
    for key in ("line", "polygon", "wire"):
        assert "Backspace" not in step_hint(key, 0)
        assert "Backspace" in step_hint(key, 1)


def test_all_hints_are_unique_and_single_line():
    assert len(set(ALL_HINTS)) == len(ALL_HINTS)
    assert all("\n" not in text for text in ALL_HINTS)


def test_shortcuts_are_unique_and_do_not_clash_with_the_fixed_keys():
    values = list(TOOL_SHORTCUTS.values())
    assert len(set(values)) == len(values)
    fixed = {SELECT_SHORTCUT, PROPERTIES_SHORTCUT, ANGLE_MODE_SHORTCUT, UNDO_POINT_SHORTCUT}
    assert len(fixed) == 4 and not fixed & set(values)


def test_kicad_default_shortcuts():
    assert TOOL_SHORTCUTS["line"] == "Ctrl+Shift+L" and TOOL_SHORTCUTS["arc"] == "Ctrl+Shift+A"
    assert TOOL_SHORTCUTS["circle"] == "Ctrl+Shift+C" and TOOL_SHORTCUTS["polygon"] == "Ctrl+Shift+P"
    assert TOOL_SHORTCUTS["text"] == "Ctrl+Shift+T" and TOOL_SHORTCUTS["dimension"] == "Ctrl+Shift+H"
    assert TOOL_SHORTCUTS["measure"] == "Ctrl+Shift+M"
    assert ANGLE_MODE_SHORTCUT == "Shift+Space" and UNDO_POINT_SHORTCUT == "Backspace"


def test_tooltip_appends_the_shortcut_only_when_there_is_one():
    assert tooltip("Linea", "line") == "Linea (Ctrl+Shift+L)"
    assert tooltip("Filo", "wire") == "Filo"
