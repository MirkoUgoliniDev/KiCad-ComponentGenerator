"""@brief Grey hints drawn in one opaque grey (user, 2026-10-17).

The designator hint «U» read darker than the footprint-filter hint «RS485*»
although both had the same colour: Qt drew hints in the text colour at 50 %
alpha, and on the thin strokes of a single letter that alpha added to the
edge antialiasing. `theme.APP_QSS` now gives every input an opaque hint
colour (`SUBTEXT`).
"""
import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtGui import QColor  # noqa: E402
from PyQt6.QtWidgets import QLineEdit, QPlainTextEdit, QWidget, QVBoxLayout  # noqa: E402

from kicadgen.gui.theme import APP_QSS, SUBTEXT, SURFACE0, TEXT  # noqa: E402


def _peak(widget):
    """Colour of the glyph pixel farthest from the field background."""
    image = widget.grab().toImage()
    background = QColor(SURFACE0)
    best, distance = background, -1
    for x in range(4, min(image.width() - 4, 120)):
        for y in range(4, image.height() - 4):
            colour = image.pixelColor(x, y)
            d = (abs(colour.red() - background.red()) + abs(colour.green() - background.green())
                 + abs(colour.blue() - background.blue()))
            if d > distance:
                best, distance = colour, d
    return best.name()


@pytest.fixture
def themed(qapp):
    host = QWidget()
    host.setStyleSheet(APP_QSS)
    QVBoxLayout(host)
    host.resize(320, 160)
    yield host
    host.deleteLater()


@pytest.mark.parametrize("hint", ["U", "RS485*"])
def test_a_hint_is_drawn_in_the_opaque_secondary_grey(themed, hint):
    """@brief Positive: the hint's ink reaches SUBTEXT itself, not a 50 % blend of TEXT."""
    field = QLineEdit()
    field.setPlaceholderText(hint)
    themed.layout().addWidget(field)
    themed.show()
    assert _peak(field) == QColor(SUBTEXT).name()


def test_a_multi_line_hint_follows_the_same_grey(themed):
    field = QPlainTextEdit()
    field.setPlaceholderText("Note")
    themed.layout().addWidget(field)
    themed.show()
    assert _peak(field.viewport()) == QColor(SUBTEXT).name()


def test_typed_text_keeps_the_main_text_colour(themed):
    """@brief Negative: only the hint changes; a typed «U» is still TEXT."""
    field = QLineEdit()
    field.setText("U")
    themed.layout().addWidget(field)
    themed.show()
    assert _peak(field) == QColor(TEXT).name()
