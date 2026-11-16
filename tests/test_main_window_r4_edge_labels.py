"""@brief Round 4 of the 2026-11-15 review: board edges named in the interface language in the window's messages.

`EDGE_LABELS` holds the Italian SOURCE words of the edges; the window put
them raw into `tx` messages, so an English interface read «side sinistro».
The window now names an edge with `castellated_module.edge_label`, which
translates it. Positive: an English interface says «left». Negative: the
Italian interface keeps «sinistro», and an unknown edge key is shown as it
is, never invented.
"""

import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")

from kicadgen.gui import main_window as gui  # noqa: E402
from kicadgen.services import messages  # noqa: E402
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS  # noqa: E402

SIDE = {"edge": "left", "count": 4, "pitch_mm": 2.54}


def _english(monkeypatch):
    """Register a translator that answers from the catalog's English entries, as `I18n.raw` would."""
    def raw(key, default=None):
        return DEFAULT_TRANSLATIONS.get(key, {}).get("en", default)
    monkeypatch.setattr(messages, "_translator", raw)


def test_an_english_interface_names_the_edge_in_english(monkeypatch):
    """Positive: the side summary of the log says «left» in English, never the Italian source word."""
    _english(monkeypatch)
    text = gui.MainWindow._side_summary(SIDE)
    assert text == "side left: 4 contacts at 2.54 mm"
    assert "sinistro" not in text


def test_the_italian_interface_keeps_the_italian_word(monkeypatch):
    """Negative: no translator (the Italian sources) -> «lato sinistro»."""
    monkeypatch.setattr(messages, "_translator", None)
    assert gui.MainWindow._side_summary(SIDE) == "lato sinistro: 4 contatti a 2.54 mm"


def test_an_unknown_edge_key_is_shown_as_it_is(monkeypatch):
    """Negative: a key that is no edge is never translated into one."""
    _english(monkeypatch)
    assert gui.MainWindow._side_summary({**SIDE, "edge": "diagonal"}).startswith("side diagonal:")
