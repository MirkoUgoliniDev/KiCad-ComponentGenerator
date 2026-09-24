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
