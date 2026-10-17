"""@brief L'anteprima 2D del footprint disegna i contatti come li scrive il .kicad_mod.

Con la piazzola continua dei contatti «mezzo foro + foro passante» (decisione
dell'utente del 2026-09-23) l'anteprima non puo' piu' disegnare per tutti il
rettangolo di bordo: mostrerebbe un footprint diverso da quello scritto. Tre
forme, le stesse del generatore: stadio con foro decentrato e mezzo foro,
corona rotonda del foro passante, rettangolo del solo mezzo foro.
"""
import pytest

pytest.importorskip("PyQt6")
pytest.importorskip("pyvistaqt")
from PyQt6.QtWidgets import QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsRectItem

from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE, continuous_pads,
    pad_positions,
)
from test_contact_style import dev_kit


def _shapes(window, params):
    from kicadgen.gui.footprint_view import TEXT_KEY_ROLE
    window._draw_preview_castellated(params)
    # The REF**/Value groups (their anchor square is a rect) are not pads.
    items = [i for i in window.preview_scene.items() if i.data(TEXT_KEY_ROLE) is None]
    return ([i for i in items if isinstance(i, QGraphicsPathItem)],
            [i for i in items if isinstance(i, QGraphicsEllipseItem)],
            [i for i in items if isinstance(i, QGraphicsRectItem)])


def test_continuous_contacts_are_drawn_as_stadiums_with_their_drill(window):
    params = dev_kit(CONTACT_HALF_AND_THROUGH)
    pads = list(continuous_pads(params))
    assert pads, "il caso di prova deve avere piazzole continue"
    paths, ellipses, _ = _shapes(window, params)
    assert len(paths) == len(pads)
    # Un solo foro per contatto: il mezzo foro e' del bordo del modulo, non
    # della piazzola sulla scheda ospite che il footprint descrive.
    assert len(ellipses) == len(pads)
    # Lo stadio copre i due centri dei fori (scala 20 px/mm dell'anteprima).
    pad, path = pads[0], None
    for candidate in paths:
        if candidate.path().contains(
                candidate.mapFromScene(pad.drill_x * 20, pad.drill_y * 20)):
            path = candidate
            break
    assert path is not None
    assert path.path().contains(path.mapFromScene(pad.half_hole_x * 20, pad.half_hole_y * 20))


def test_through_hole_contacts_are_round_rings_not_edge_rectangles(window):
    params = dev_kit(CONTACT_THROUGH_HOLE)
    paths, ellipses, rects = _shapes(window, params)
    count = len(list(pad_positions(params)))
    assert not paths
    assert len(ellipses) == 2 * count      # corona + foro
    # Il contorno del corpo e' l'unico rettangolo rimasto.
    assert len([r for r in rects if r.brush().style().name != "NoBrush"]) == 0


def test_half_hole_contacts_keep_the_edge_rectangle(window):
    """Negativo: il solo mezzo foro resta la piazzola rettangolare di bordo."""
    params = dev_kit(CONTACT_HALF_HOLE)
    paths, ellipses, rects = _shapes(window, params)
    count = len(list(pad_positions(params)))
    assert not paths and not ellipses
    filled = [r for r in rects if r.brush().style().name != "NoBrush"]
    assert len(filled) == count
