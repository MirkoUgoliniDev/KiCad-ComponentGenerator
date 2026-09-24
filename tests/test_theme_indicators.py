"""@brief Una casella accesa deve leggersi come accesa (utente, 2026-09-24).

Prima una casella accesa era solo un quadrato pieno di blu, senza segno di
spunta: l'utente ha chiesto di rendere predefinita una pausa che era gia'
accesa, perche' non si capiva. Positivo: l'indicatore acceso porta un'immagine
che esiste. Negativo: quello spento no.
"""
from pathlib import Path
import re

from kicadgen.gui import theme


def _block(selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", theme.APP_QSS)
    assert match, selector
    return match.group(1)


def test_a_checked_box_shows_a_check_mark_that_exists():
    checked = _block("QCheckBox::indicator:checked")
    image = re.search(r'image:\s*url\("([^"]+)"\)', checked)
    assert image and Path(image.group(1)).is_file()
    assert "check-mark.svg" in image.group(1)


def test_an_unchecked_box_has_no_mark():
    assert "image:" not in _block("QCheckBox::indicator")


def test_the_selected_tab_is_the_most_visible_one():
    """@brief Utente, 2026-09-24: all'avvio sembrava scelta la scheda KiCad.

    Criterio strutturale, non un colore preciso: la scheda scelta deve avere lo
    sfondo piu' chiaro di quelle non scelte, che a loro volta devono stare
    sopra le disabilitate. Negativo: una scheda non scelta non puo' essere piu'
    chiara della scelta (era il difetto).
    """
    import re
    from PyQt6.QtGui import QColor
    from kicadgen.gui import theme

    def background(selector):
        block = re.search(re.escape(selector) + r"\s*\{\{?([^}]*)\}", theme.TAB_QSS)
        value = re.search(r"background:\s*([^;]+);", block.group(1)).group(1).strip()
        value = {"{BASE}": theme.BASE, "{MANTLE}": theme.MANTLE,
                 "{SURFACE0}": theme.SURFACE0}.get(value, value)
        return QColor(value).lightness()

    selected = background("QTabBar::tab:selected")
    normal = background("QTabBar::tab ")
    disabled = background("QTabBar::tab:disabled")
    assert selected > normal > disabled
    assert "font-weight" in re.search(r"QTabBar::tab:selected\s*\{([^}]*)\}", theme.TAB_QSS).group(1)
