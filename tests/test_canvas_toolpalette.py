"""@brief Shared tool grid (plan footprint-drawing-kicad A1) and the tintable glyph icon."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QPixmap

from kicadgen.gui.canvas_toolpalette import (ACTIVE_PROPERTY, OUTSIDE_KEY, SELECT_KEY, ToolGrid, select_arrow_icon,
                                             tool_grid_style)


def _icon(colour="#cdd6f4"):
    pixmap = QPixmap(16, 16)
    pixmap.fill(QColor(colour))
    return QIcon(pixmap)


def _grid():
    grid = ToolGrid(columns=3, icon_size=24, button_size=30)
    grid.add_select()
    grid.add_tool("line", _icon(), "Linea", "Linea")
    grid.add_tool("rect", _icon(), "Rettangolo", "Rettangolo")
    grid.add_tool("dimension", _icon(), "Quota", "Quota",
                  menu_items=[("dim_aligned", "Allineata", _icon("#ff0000")),
                              ("dim_radial", "Radiale", _icon("#00ff00"))])
    return grid


def _record(grid):
    seen = []
    grid.tool_triggered.connect(seen.append)
    return seen


def _checked(grid):
    return [key for key, button in grid.buttons().items() if button.isChecked()]


def test_select_comes_first_and_is_active_at_start(qapp):
    grid = _grid()
    assert grid.keys()[0] == SELECT_KEY
    assert _checked(grid) == [SELECT_KEY] and grid.active_key() is None


def test_select_added_after_tools_still_moves_to_the_front(qapp):
    grid = ToolGrid(columns=2)
    grid.add_tool("line", _icon(), "Linea")
    grid.add_select()
    assert grid.keys() == [SELECT_KEY, "line"]
    with pytest.raises(ValueError):
        grid.add_select()


def test_clicks_are_exclusive_and_emit_the_tool(qapp):
    grid = _grid()
    seen = _record(grid)
    grid.button("line").click()
    assert seen == ["line"] and _checked(grid) == ["line"]
    grid.button("rect").click()
    assert seen == ["line", "rect"] and _checked(grid) == ["rect"]
    assert grid.button("rect").property(ACTIVE_PROPERTY) is True
    assert grid.button("line").property(ACTIVE_PROPERTY) is False


def test_clicking_the_active_tool_or_select_goes_back_to_select(qapp):
    grid = _grid()
    seen = _record(grid)
    grid.button("line").click()
    grid.button("line").click()
    assert seen == ["line", ""] and _checked(grid) == [SELECT_KEY] and grid.active_key() is None
    grid.button("rect").click()
    grid.button(SELECT_KEY).click()
    assert seen[-2:] == ["rect", ""] and _checked(grid) == [SELECT_KEY]


def test_set_active_is_silent_and_unknown_keys_show_select(qapp):
    grid = _grid()
    seen = _record(grid)
    grid.set_active("rect")
    assert seen == [] and _checked(grid) == ["rect"] and grid.active_key() == "rect"
    grid.set_active(None)
    assert seen == [] and _checked(grid) == [SELECT_KEY]
    grid.set_active("not-a-tool")
    assert _checked(grid) == [SELECT_KEY]


def test_retinting_keeps_the_check_state_and_changes_the_icon(qapp):
    grid = _grid()
    grid.set_active("line")
    before = grid.button("line").icon().pixmap(16, 16).toImage()
    grid.set_icons({"line": _icon("#f38ba8"), "rect": _icon("#f38ba8"), "unknown": _icon()})
    assert _checked(grid) == ["line"]
    assert grid.button("line").icon().pixmap(16, 16).toImage() != before


def test_dropdown_remembers_the_last_variant(qapp):
    grid = _grid()
    seen = _record(grid)
    button = grid.button("dimension")
    assert button.popupMode() == button.ToolButtonPopupMode.MenuButtonPopup
    assert grid.current_variant("dimension") == "dim_aligned"
    button.click()
    assert seen == ["dim_aligned"]
    radial = [a for a in button.menu().actions() if a.data() == "dim_radial"][0]
    radial.trigger()
    assert seen[-1] == "dim_radial" and grid.current_variant("dimension") == "dim_radial"
    assert grid.active_key() == "dim_radial" and _checked(grid) == ["dimension"]
    # The button shows the chosen variant's icon (green), not the first one's (red).
    image = button.icon().pixmap(16, 16).toImage()
    assert image.pixelColor(8, 8) == QColor("#00ff00")
    # Back to Select, then the main part arms the remembered variant again.
    button.click()
    assert seen[-1] == ""
    button.click()
    assert seen[-1] == "dim_radial"


def test_set_active_with_a_variant_key_shows_its_dropdown_silently(qapp):
    grid = _grid()
    seen = _record(grid)
    grid.set_active("dim_radial")
    assert seen == [] and _checked(grid) == ["dimension"]
    assert grid.button("dim_radial") is grid.button("dimension")
    assert grid.current_variant("dimension") == "dim_radial"


def test_duplicate_or_empty_keys_are_refused(qapp):
    grid = _grid()
    for key in ("", "line", "dim_radial"):
        with pytest.raises(ValueError):
            grid.add_tool(key, _icon(), "x")


def test_buttons_never_take_the_keyboard_focus(qapp):
    grid = _grid()
    assert grid.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert all(b.focusPolicy() == Qt.FocusPolicy.NoFocus for b in grid.buttons().values())


def test_buttons_are_laid_out_in_the_given_columns(qapp):
    grid = _grid()
    layout = grid.layout()
    positions = [layout.getItemPosition(layout.indexOf(b))[:2] for b in grid.buttons().values()]
    assert positions == [(0, 0), (0, 1), (0, 2), (1, 0)]


def test_accent_reaches_the_active_style_and_an_invalid_one_falls_back(qapp):
    grid = _grid()
    grid.set_accent("#f9e2af")
    assert "border: 2px solid #f9e2af" in grid.styleSheet()
    assert "rgba(249, 226, 175, 0.35)" in grid.styleSheet()
    assert "#f9e2af" not in tool_grid_style("not a colour")


def test_select_arrow_icon_is_drawn_and_tintable(qapp):
    plain = select_arrow_icon(28).pixmap(28, 28).toImage()
    red = select_arrow_icon(28, stroke="#ff0000").pixmap(28, 28).toImage()
    assert any(plain.pixelColor(x, y).alpha() for x in range(28) for y in range(28))
    assert plain != red


def test_glyph_icon_default_is_unchanged_and_a_stroke_tints_it(qapp):
    from kicadgen.generators.symbol_glyphs import shape_tool_glyphs
    from kicadgen.gui.symbol_tab import glyph_icon
    glyph = shape_tool_glyphs()[0]
    default = glyph_icon(glyph, 28).pixmap(28, 28).toImage()
    assert glyph_icon(glyph, 28, stroke=None).pixmap(28, 28).toImage() == default
    # The theme text colour given explicitly is the default itself.
    assert glyph_icon(glyph, 28, stroke="#cdd6f4").pixmap(28, 28).toImage() == default
    assert glyph_icon(glyph, 28, stroke="#f38ba8").pixmap(28, 28).toImage() != default


# ── "Tool outside the grid" state (review S4) ──

def test_outside_key_checks_no_button_not_even_select(qapp):
    """Positive: the symbol wire or the ruler armed -> nothing checked, Select not highlighted."""
    grid = _grid()
    seen = _record(grid)
    grid.set_active("line")
    grid.set_active(OUTSIDE_KEY)
    assert _checked(grid) == [] and grid.outside_active() and grid.active_key() is None
    assert not grid.button(SELECT_KEY).property(ACTIVE_PROPERTY)
    assert seen == [], "set_active never emits"


def test_none_and_empty_still_mean_select(qapp):
    """Negative twin: None / "" keep meaning Select, and leave the outside state."""
    grid = _grid()
    for value in (None, SELECT_KEY):
        grid.set_active(OUTSIDE_KEY)
        grid.set_active(value)
        assert _checked(grid) == [SELECT_KEY] and not grid.outside_active()
        assert grid.button(SELECT_KEY).property(ACTIVE_PROPERTY)


def test_clicks_leave_the_outside_state(qapp):
    """A user click on Select or on a tool takes over from the outside tool."""
    grid = _grid()
    seen = _record(grid)
    grid.set_active(OUTSIDE_KEY)
    grid.button(SELECT_KEY).click()
    assert seen == [SELECT_KEY] and _checked(grid) == [SELECT_KEY] and not grid.outside_active()
    grid.set_active(OUTSIDE_KEY)
    grid.button("rect").click()
    assert seen[-1] == "rect" and _checked(grid) == ["rect"] and not grid.outside_active()


def test_outside_key_is_reserved(qapp):
    """No tool or variant may use the reserved key."""
    grid = ToolGrid()
    with pytest.raises(ValueError):
        grid.add_tool(OUTSIDE_KEY, _icon(), "x")
    with pytest.raises(ValueError):
        grid.add_tool("dim", _icon(), "Quota", menu_items=[(OUTSIDE_KEY, "x", _icon())])
