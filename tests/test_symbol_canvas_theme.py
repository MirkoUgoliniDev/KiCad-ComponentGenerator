"""@brief The «Simbolo elettrico» canvas starts with KiCad's light colours and remembers the choice (user, 2026-11-13).

@details Config key `symbol_canvas_theme`: missing or any value other than
"dark" gives the light (KiCad) canvas; toggling the theme button writes the
choice, and a tab bound to that configuration starts with it. The footprint
preview has no theme toggle of its own (its layers use KiCad's colours), so
there is nothing to align there. Positive and negative cases each.
"""

from kicadgen.gui.symbol_tab import CONFIG_CANVAS_THEME, SymbolTab


def test_a_fresh_configuration_starts_light(window):
    """@brief Positive: no saved choice -> KiCad colours, button checked."""
    assert CONFIG_CANVAS_THEME not in window._config or window._config[CONFIG_CANVAS_THEME] != "dark"
    assert window.symbol_tab._theme_key == "kicad"
    assert window.symbol_tab.theme_button.defaultAction().isChecked()


def test_toggling_to_dark_is_saved_and_restored(window, qapp):
    """@brief Positive: dark is written and a tab bound to it starts dark; negative: back to light."""
    saved = []
    window.symbol_tab.set_kicad_colors(False)
    assert window._config[CONFIG_CANVAS_THEME] == "dark"
    tab = SymbolTab(tx=window.i18n.text)
    try:
        tab.bind_settings(dict(window._config), lambda: saved.append(1))
        assert tab._theme_key == "dark" and saved == []  # restoring writes nothing
    finally:
        tab.deleteLater()
    window.symbol_tab.set_kicad_colors(True)
    assert window._config[CONFIG_CANVAS_THEME] == "light"


def test_only_an_explicit_dark_keeps_the_dark_canvas(qapp):
    """@brief Negative: an unknown value reads as light; positive: "dark" is dark."""
    for value, key in (("garbage", "kicad"), (None, "kicad"), ("dark", "dark")):
        tab = SymbolTab()
        try:
            tab.bind_settings({CONFIG_CANVAS_THEME: value} if value is not None else {})
            assert tab._theme_key == key, value
        finally:
            tab.deleteLater()
