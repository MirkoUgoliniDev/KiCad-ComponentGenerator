"""@brief Dove stanno i contatti del modulo, secondo cio' che e' stato dichiarato.

Il rilevatore locale scambia regolarmente le file di piazzole per componenti:
sulla scheda di prova le ha chiamate «switch», «battery», «clock». Filtrarle
per classe non serve -- la classe che il modello assegna e' proprio quella
sbagliata -- e nemmeno per dimensione, perche' una piazzola e un condensatore
0603 misurano uguale.

Ma la posizione dei contatti non e' un'incognita: l'estrazione la dichiara
(lati, numero, passo, offset) ed e' la stessa geometria con cui il footprint
disegna le piazzole. Un riquadro che cade su un contatto dichiarato, e che e'
piccolo quanto il passo, e' quel contatto. Nessuna soglia scelta a mano:
i due criteri sono entrambi quote dichiarate.
"""
from __future__ import annotations

import math

#: Quanto puo' sporgere un corpo oltre la banda dei contatti restando un
#: contatto: mezza piazzola. Oltre, e' un corpo che ci sta sopra o accanto.
_BAND_TOLERANCE = 0.5


def _number(value, minimum=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > minimum else None


def contact_positions(data, length_mm, width_mm):
    """@brief Centri dei contatti dichiarati, in millimetri dal centro scheda.

    @param data Parametri estratti: layout e lati castellated, oppure matrice.
    @param length_mm Estensione della scheda lungo X.
    @param width_mm Estensione lungo Y.
    @return Lista di dizionari con x_mm, y_mm, pitch_mm, edge.
    @note Ritorna una lista vuota quando le quote dichiarate non bastano: senza
        di esse non si filtra nulla, perche' un filtro che indovina dove sono i
        contatti cancellerebbe componenti veri.
    """
    if not isinstance(data, dict):
        return []
    positions = []
    half_x, half_y = length_mm / 2, width_mm / 2
    for side in (data.get('castellated_sides') or []):
        if not isinstance(side, dict):
            continue
        edge = side.get('edge')
        count = side.get('count')
        pitch = _number(side.get('pitch_mm'))
        if edge not in ('left', 'right', 'top', 'bottom'):
            continue
        if isinstance(count, bool) or not isinstance(count, int) or count < 1 or pitch is None:
            continue
        offset = _number(side.get('offset_mm'), minimum=-math.inf) or 0.0
        start = -(count - 1) * pitch / 2 + offset
        for index in range(count):
            along = start + index * pitch
            if edge == 'left':
                positions.append(dict(x_mm=-half_x, y_mm=along, pitch_mm=pitch, edge=edge))
            elif edge == 'right':
                positions.append(dict(x_mm=half_x, y_mm=along, pitch_mm=pitch, edge=edge))
            elif edge == 'top':
                positions.append(dict(x_mm=along, y_mm=-half_y, pitch_mm=pitch, edge=edge))
            else:
                positions.append(dict(x_mm=along, y_mm=half_y, pitch_mm=pitch, edge=edge))
    if positions:
        return positions
    # Matrice passante: i pin stanno su una griglia, e anche qui il passo e la
    # distanza fra le file sono dichiarati.
    rows, cols = data.get('rows'), data.get('cols')
    pitch = _number(data.get('pitch_mm'))
    spacing = _number(data.get('row_spacing_mm'))
    if (isinstance(rows, int) and not isinstance(rows, bool) and rows >= 1
            and isinstance(cols, int) and not isinstance(cols, bool) and cols >= 1
            and pitch is not None and (spacing is not None or rows == 1)):
        spacing = spacing or 0.0
        for row in range(rows):
            for col in range(cols):
                positions.append(dict(
                    x_mm=(col - (cols - 1) / 2) * pitch,
                    y_mm=(row - (rows - 1) / 2) * spacing,
                    pitch_mm=pitch, edge='matrix'))
    return positions


def contact_proposals(proposals, positions, length_mm, width_mm):
    """@brief Indici delle proposte che cadono su un contatto dichiarato.

    @param proposals Proposte del rilevatore, con `_box` normalizzato.
    @param positions Uscita di contact_positions.
    @param length_mm Estensione della scheda lungo X.
    @param width_mm Estensione lungo Y.
    @return Insieme degli indici da scartare.
    @note Due criteri, entrambi su quote dichiarate: il centro del riquadro sta
        entro mezzo passo dal centro di un contatto, e il lato lungo del
        riquadro non supera il passo. Il secondo serve a non cancellare un
        corpo vero che si trova sopra la fila -- un connettore lungo attraversa
        piu' passi, una piazzola no.
    """
    if not positions:
        return set()
    found = set()
    for index, proposal in enumerate(proposals):
        box = proposal.get('_box')
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in box)
        centre_x = ((x0 + x1) / 2 - 0.5) * length_mm
        centre_y = ((y0 + y1) / 2 - 0.5) * width_mm
        long_side = max((x1 - x0) * length_mm, (y1 - y0) * width_mm)
        for contact in positions:
            pitch = contact['pitch_mm']
            if long_side > pitch:
                continue
            reach = pitch * _BAND_TOLERANCE
            if (abs(centre_x - contact['x_mm']) <= reach
                    and abs(centre_y - contact['y_mm']) <= reach):
                found.add(index)
                break
    return found
