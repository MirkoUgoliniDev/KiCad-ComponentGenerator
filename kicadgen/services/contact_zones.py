"""@brief Dove stanno i contatti del modulo, secondo cio' che e' stato dichiarato.

Il rilevatore locale scambia regolarmente le file di piazzole per componenti:
sulla scheda di prova le ha chiamate «switch», «battery», «clock». Filtrarle
per classe non serve -- la classe che il modello assegna e' proprio quella
sbagliata -- e nemmeno per dimensione, perche' una piazzola e un condensatore
0603 misurano uguale.

Ma la posizione dei contatti non e' un'incognita: l'estrazione la dichiara
(lati, numero, passo, offset) ed e' la stessa geometria con cui il footprint
disegna le piazzole. Un riquadro che cade su un contatto dichiarato, e che e'
piccolo quanto il passo, e' quel contatto.

EURISTICA DICHIARATA (non una regola fisica). Posizione, passo e lunghezza
della piazzola sono quote dichiarate; non lo sono i margini con cui la
striscia del contatto viene allargata: la profondita' aggiunge mezzo foro e
_LABEL_GAP_MM per la sigla serigrafata, e la tolleranza lungo il bordo e'
_BAND_TOLERANCE (mezzo passo). Sono convenzioni: la prima e' come QUESTO
programma dispone la serigrafia, non come l'ha stampata il produttore del
pezzo; la seconda e' la meta' del passo, cioe' il confine con il contatto
vicino. Sono state osservate, non misurate, sulla scheda di prova (moduli
castellated a passo 2,54 mm), dove il rilevatore annotava piazzola e sigla
insieme. Le renderebbe superflue una misura della serigrafia reale (per
esempio la maschera delle piazzole ricavata dalla foto, o le quote della
serigrafia nel disegno meccanico). Fragilita' nota: un corpo montato a meno
di mezzo foro + 0,55 mm dalla fine della piazzola, piccolo quanto il passo,
cade nella striscia e viene tolto come se fosse un contatto.
"""
from __future__ import annotations

import dataclasses
import math

from ..generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CONTACT_STYLES, CONTACT_THROUGH_HOLE,
    AssumedSize, CastellatedModuleParams, CastellatedSide,
)

#: Quanto puo' sporgere un corpo oltre la banda dei contatti restando un
#: contatto: mezza piazzola. Oltre, e' un corpo che ci sta sopra o accanto.
#: EURISTICA (vedi docstring del modulo): mezzo passo e' il confine con il
#: contatto vicino, non una misura della piazzola annotata dal rilevatore.
_BAND_TOLERANCE = 0.5

#: Distanza fra la fine della piazzola e la sua sigla serigrafata, come la
#: dispone il generatore del footprint (`label_gap` in step_generator): mezzo
#: foro piu' questo margine. Non e' stato scelto per far tornare un caso, ma
#: resta un'EURISTICA: e' la convenzione con cui questo programma disegna la
#: serigrafia, e che le sigle stampate sul pezzo reale stiano grosso modo li'
#: e' un'osservazione sulla scheda di prova, non una misura (vedi docstring
#: del modulo per cosa la renderebbe superflua).
_LABEL_GAP_MM = 0.55


def _number(value, minimum=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > minimum else None


def _read(side, *names):
    """First non-None value among `names`, read from a dict or an object's attributes."""
    for name in names:
        value = side.get(name) if isinstance(side, dict) else getattr(side, name, None)
        if value is not None:
            return value
    return None


def _side_value(side, *names):
    """First positive finite value among `names`, read from a dict or an object."""
    for name in names:
        number = _number(_read(side, name))
        if number is not None:
            return number
    return None


#: Campi che il CastellatedSide installato accetta: i diametri per lato sono
#: arrivati dopo, e un lato costruito con un campo che la classe non conosce
#: fermerebbe il filtro dei contatti per un dato accessorio.
_SIDE_FIELDS = frozenset(field.name for field in dataclasses.fields(CastellatedSide))


def _hole_size(side, data, field, *names):
    """Per-side hole size with its provenance: declared as a float, else AssumedSize.

    A size is declared only when something says it was read or derived from
    the documents: the side's `<name>_declared` flag (GUI table), the side's
    own `value_sources` or the extraction's `castellated_sides.<edge>.<field>`
    entry. An estimate, or a value with no provenance at all, stays an
    assumption the generator may reduce to fit, exactly as the footprint and
    the 3D treat it; a declared size that does not fit makes side_geometry
    refuse the side instead.
    """
    value = _side_value(side, field, *names)
    if value is None:
        return None
    flag = _read(side, field.replace('_mm', '_declared'))
    own = _read(side, 'value_sources')
    module = data.get('value_sources') if isinstance(data, dict) else None
    sources = (own.get(field) if isinstance(own, dict) else None,
               module.get(f"castellated_sides.{_read(side, 'edge')}.{field}")
               if isinstance(module, dict) else None)
    if flag is True or any(source in ('read', 'derived') for source in sources):
        return value
    return AssumedSize(value)


def _module_size(data, key):
    """Module-wide hole size with its provenance, same rule as _hole_size.

    Declared only when the extraction's top-level `value_sources` says the
    value was read or derived; otherwise an AssumedSize, so that a per-side
    estimate still outranks it and a read row spacing can reduce it.
    """
    value = _number(data.get(key))
    if value is None:
        return None
    sources = data.get('value_sources')
    if isinstance(sources, dict) and sources.get(key) in ('read', 'derived'):
        return value
    return AssumedSize(value)


def declared_module(data, length_mm, width_mm):
    """@brief The castellated module the footprint would draw from the declared quantities.

    @param data Extracted parameters (raw): castellated_sides with edge, count,
        pitch_mm, offset_mm, contact_style and the optional per-side hole sizes
        half_hole_diameter_mm (legacy hole_diameter_mm) and
        through_hole_drill_mm; module-wide drill_mm / pad_diameter_mm for the
        through-hole and row_spacing_mm.
    @param length_mm Board extent along X.
    @param width_mm Board extent along Y.
    @return (CastellatedModuleParams, list of (declared side, CastellatedSide))
        for the sides that carry a valid edge, count and pitch.
    @note Hole sizes and through-hole positions are then read per side with
        CastellatedModuleParams.side_geometry, the same rule the footprint and
        the 3D use: a side's declared size wins over the module's, an
        undeclared or merely estimated one is an assumed size (reduced to fit
        the pitch and the declared row spacing when needed, see _hole_size), and the through-hole of a half_and_through side
        sits where the declared row spacing puts it. Before this, the photo
        mask used module-wide values and a through-hole offset that ignored
        the row spacing, so it could disagree with the footprint it filters for.
    @note Tolerant of side descriptions that are objects rather than dicts
        (attributes pitch/offset/hole_diameter/... as CastellatedSide names
        them), and of an installed CastellatedSide without the per-side size
        fields: those sizes are then simply not passed.
    """
    pairs = []
    sides = data.get('castellated_sides') if isinstance(data, dict) else None
    for side in sides or []:
        if not isinstance(side, dict) and not hasattr(side, 'edge'):
            continue
        edge, count = _read(side, 'edge'), _read(side, 'count')
        pitch = _side_value(side, 'pitch_mm', 'pitch')
        if edge not in ('left', 'right', 'top', 'bottom') or pitch is None:
            continue
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            continue
        style = _read(side, 'contact_style')
        options = dict(
            contact_style=style if style in CONTACT_STYLES else CONTACT_HALF_AND_THROUGH,
            hole_diameter=_hole_size(side, data, 'half_hole_diameter_mm', 'hole_diameter_mm',
                                     'hole_diameter'),
            thru_hole_drill=_hole_size(side, data, 'through_hole_drill_mm', 'thru_hole_drill'),
            thru_pad_diameter=_side_value(side, 'thru_pad_diameter'))
        pairs.append((side, CastellatedSide(
            edge=edge, count=count, pitch=pitch,
            **{key: value for key, value in options.items() if key in _SIDE_FIELDS})))
    declared = data if isinstance(data, dict) else {}
    module = CastellatedModuleParams(
        body_length=length_mm, body_width=width_mm, sides=[built for _, built in pairs],
        # None = non dichiarato: il generatore usa la sua quota di riferimento.
        # Stima o valore senza provenienza = AssumedSize (vedi _module_size).
        thru_hole_drill=_module_size(declared, 'drill_mm'),
        thru_pad_diameter=_module_size(declared, 'pad_diameter_mm'),
        row_spacing=_number(declared.get('row_spacing_mm')))
    return module, pairs


def _geometry(module, built):
    """Side geometry, or None when the installed generator cannot give it."""
    resolve = getattr(module, 'side_geometry', None)
    if resolve is None:
        return None
    try:
        return resolve(built)
    except (TypeError, ValueError, ArithmeticError):
        return None


def _pad_length(side, data, pitch):
    """Declared land length of a side: the side's, the module's, else the pitch."""
    return (_side_value(side, 'pad_length_mm', 'pad_length')
            or _number(data.get('pad_length_mm')) or pitch)


def _continuous_pad_depth(side, data, pitch, geometry):
    """@brief Depth of the strip of a half_and_through side: continuous pad and label.

    @param side Declared side whose OBSERVED contact_style is half_and_through
        (an absent style is not assumed here: see contact_positions).
    @param data Extracted parameters (pad_length_mm).
    @param pitch Declared pitch of the side, the last-resort pad length.
    @param geometry SideGeometry of the side (declared_module + side_geometry).
    @return Millimetres from the board edge inward.
    @note The footprint and the 3D draw such a contact as ONE gold pad from the
        edge to beyond the through-hole ring (castellated_module.continuous_pad),
        so the strip must reach the far side of that ring, not just the end of
        the declared edge pad. Ring and its distance from the edge are the
        side's own (SideGeometry.thru_inset and ring, row spacing included),
        the label margin is the 3D generator's (half-hole radius plus
        _LABEL_GAP_MM). Without a through-hole (drill or ring zero) the
        contact is the edge pad alone.
    """
    pad_length = _pad_length(side, data, pitch)
    edge_band = pad_length + geometry.half_hole / 2 + _LABEL_GAP_MM
    if not geometry.has_thru_holes or geometry.thru_inset is None:
        return edge_band
    # Stesso margine della sigla della piazzola di bordo, contato dalla fine
    # del rame continuo quando questa sta piu' dentro.
    return edge_band + max(0.0, geometry.thru_inset + geometry.ring / 2 - pad_length)


def _half_hole_depth(side, data, pitch, geometry):
    """@brief Depth of the strip of a half_hole side: edge pad, half-hole and label.

    @param side Declared side whose OBSERVED contact_style is half_hole.
    @param data Extracted parameters (pad_length_mm).
    @param pitch Declared pitch of the side, the last-resort pad length.
    @param geometry SideGeometry of the side.
    @return Millimetres from the board edge inward: pad length plus half the
        side's half-hole plus _LABEL_GAP_MM, the label convention of the 3D
        generator (label_gap = half-hole radius + margin).
    """
    return _pad_length(side, data, pitch) + geometry.half_hole / 2 + _LABEL_GAP_MM


def _band_depth(side, data, pitch):
    """@brief Profondita' della striscia occupata dal contatto e dalla sua sigla.

    @param side Lato dichiarato, che puo' portare la lunghezza della piazzola.
    @param data Parametri estratti, per le quote comuni a tutti i lati.
    @param pitch Passo dichiarato del lato, usato come ripiego.
    @return Millimetri dal bordo verso l'interno.
    @note Il rilevatore non annota il centro del semi-foro: annota la piazzola
        dorata, che entra sotto il modulo, e la sigla stampata accanto. Una
        zona ridotta al solo centro del contatto non li prende, ed e'
        esattamente cio' che lasciava passare le file di piazzole. La
        profondita' si ricava dalle quote dichiarate piu' la convenzione di
        serigrafia del generatore, non da una misura sulla foto: e' quindi
        un'euristica, dichiarata come tale nella docstring del modulo.
    @note Usata quando il tipo di contatto NON e' osservato (e per le matrici):
        qui si prendono solo quote dichiarate, mai le misure assunte del
        generatore, perche' allargare la maschera su un'ipotesi toglierebbe
        dalla misura i corpi montati vicino al bordo. Il semi-foro dichiarato
        del lato viene prima di ogni altra quota trasversale.
    """
    pad_length = _pad_length(side, data, pitch)
    # Nei castellated il foro non e' quotato e drill_mm vale 0 per convenzione:
    # in quel caso la larghezza della piazzola e' la misura trasversale nota.
    hole = (_side_value(side, 'half_hole_diameter_mm', 'hole_diameter_mm')
            or _number(data.get('drill_mm'))
            or _side_value(side, 'pad_width_mm', 'pad_width')
            or _number(data.get('pad_diameter_mm')) or 0.0)
    return pad_length + hole / 2 + _LABEL_GAP_MM


def _through_hole_depth(geometry):
    """@brief Depth of the strip of a through_hole side: web, ring and label.

    @param geometry SideGeometry of a side with contact_style == "through_hole".
    @return Millimetres from the board edge inward.
    @note The contact is not an edge pad but a closed ring set back from the
        edge, with intact board in between. Its centre, ring and bore are the
        side's own, from the same rule the footprint uses
        (CastellatedModuleParams.side_geometry: declared row spacing, else its
        declared heuristic fallback). The strip ends where the label ends by
        this program's convention: ring edge plus half a bore plus
        _LABEL_GAP_MM, exactly as for the edge pads.
    """
    return geometry.thru_inset + geometry.ring / 2 + geometry.drill / 2 + _LABEL_GAP_MM


def contact_positions(data, length_mm, width_mm):
    """@brief Centri dei contatti dichiarati, in millimetri dal centro scheda.

    @param data Parametri estratti: layout e lati castellated, oppure matrice.
    @param length_mm Estensione della scheda lungo X.
    @param width_mm Estensione lungo Y.
    @return Lista di dizionari con x_mm, y_mm, pitch_mm, edge, depth_mm.
    @note Per una matrice servono anche body_offset_x_mm e body_offset_y_mm:
        la griglia sta dove la mettono gli offset dichiarati, non al centro
        della scheda (vedi _matrix_positions).
    @note Ritorna una lista vuota quando le quote dichiarate non bastano: senza
        di esse non si filtra nulla, perche' un filtro che indovina dove sono i
        contatti cancellerebbe componenti veri.
    """
    if not isinstance(data, dict):
        return []
    positions = []
    half_x, half_y = length_mm / 2, width_mm / 2
    module, pairs = declared_module(data, length_mm, width_mm)
    for side, built in pairs:
        edge, count, pitch = built.edge, built.count, float(built.pitch)
        offset = _number(_read(side, 'offset_mm', 'offset'), minimum=-math.inf) or 0.0
        # La striscia descrive il contatto che il lato offre davvero: la
        # piazzola di bordo (default, e tipi con semi-foro) oppure la corona
        # arretrata di un foro passante.
        # Solo un tipo OSSERVATO allarga la striscia. Un tipo assente viene
        # disegnato a valle come half_and_through, ma e' un'ipotesi: mascherare
        # la foto su un'ipotesi toglierebbe dalla misura i corpi montati vicino
        # al bordo (vedi il @note di contact_positions). Senza tipo resta la
        # striscia della piazzola di bordo dichiarata, come prima.
        style = _read(side, 'contact_style')
        geometry = _geometry(module, built) if style in CONTACT_STYLES else None
        if (geometry is not None and style == CONTACT_THROUGH_HOLE
                and geometry.thru_inset is not None):
            depth = _through_hole_depth(geometry)
        elif geometry is not None and style == CONTACT_HALF_AND_THROUGH:
            depth = _continuous_pad_depth(side, data, pitch, geometry)
        elif geometry is not None and style == CONTACT_HALF_HOLE:
            depth = _half_hole_depth(side, data, pitch, geometry)
        else:
            depth = _band_depth(side, data, pitch)
        start = -(count - 1) * pitch / 2 + offset
        for index in range(count):
            along = start + index * pitch
            common = dict(pitch_mm=pitch, edge=edge, depth_mm=depth)
            if edge == 'left':
                positions.append(dict(common, x_mm=-half_x, y_mm=along))
            elif edge == 'right':
                positions.append(dict(common, x_mm=half_x, y_mm=along))
            elif edge == 'top':
                positions.append(dict(common, x_mm=along, y_mm=-half_y))
            else:
                positions.append(dict(common, x_mm=along, y_mm=half_y))
    if positions:
        return positions
    return _matrix_positions(data, length_mm, width_mm)


def _matrix_positions(data, length_mm, width_mm):
    """@brief Centres of the pins of a declared through-hole matrix, board-centred.

    @param data Extracted parameters as the extraction returns them (raw):
        rows, cols, pitch_mm, row_spacing_mm, body_offset_x_mm,
        body_offset_y_mm and optionally pin_map, pin_map_view and
        populated_pins.
    @param length_mm Board extent along X.
    @param width_mm Board extent along Y.
    @return One entry per pin that exists, or [] when the grid cannot be
        placed from declared quantities.
    @note The grid is NOT centred on the board. Its placement is declared:
        body_offset_x_mm / body_offset_y_mm are the body's minimum X / Y
        measured from pin 1 and from the first row (the same quantities the
        footprint uses, pin_header.PinHeaderParams.body_offset_*). A module
        whose header sits along one edge would otherwise get phantom pins in
        the middle of the board, and real bodies there would be deleted
        before anyone reviews them. Without the offsets the position is
        unknown and, as for any missing quantity here, nothing is filtered.
    @note Pin positions come from pin_header.pin_positions, the single source
        the footprint and the 3D pins use: pin_map and populated_pins count
        exactly as they will be drawn, and an empty grid position is not a pin.
    """
    rows, cols = data.get('rows'), data.get('cols')
    pitch = _number(data.get('pitch_mm'))
    spacing = _number(data.get('row_spacing_mm'))
    offset_x = _number(data.get('body_offset_x_mm'), minimum=-math.inf)
    offset_y = _number(data.get('body_offset_y_mm'), minimum=-math.inf)
    if not (isinstance(rows, int) and not isinstance(rows, bool) and rows >= 1
            and isinstance(cols, int) and not isinstance(cols, bool) and cols >= 1
            and pitch is not None and (spacing is not None or rows == 1)
            and offset_x is not None and offset_y is not None):
        return []
    from ..generators.pin_header import PinHeaderParams, pin_positions
    try:
        mapping = None
        if data.get('pin_map'):
            mapping = {int(entry['pin']): (int(entry['row']), int(entry['col']))
                       for entry in data['pin_map']}
        populated = data.get('populated_pins')
        grid = PinHeaderParams(rows=rows, cols=cols, pitch=pitch, row_spacing=spacing,
                               populated_pins=set(int(p) for p in populated) if populated else None,
                               pin_map=mapping)
        grid.validate()
        pins = [(x, y) for _, x, y in pin_positions(grid)]
    except (TypeError, ValueError, KeyError):
        # Pinout non leggibile: dove stanno i pin non si sa, quindi non si filtra.
        return []
    # Il bordo sinistro della scheda (-length/2) sta a offset_x dal pin 1,
    # quello superiore (-width/2) a offset_y dalla prima fila.
    origin_x, origin_y = -length_mm / 2 - offset_x, -width_mm / 2 - offset_y
    # Un pin_map letto su una BOTTOM VIEW porta con se' colonne e offset X di
    # quella vista; l'estrazione li ribalta insieme (_pin_map_seen_from_top e
    # _offset_x_seen_from_top in datasheet_extractor), che equivale a
    # specchiare l'intero campo dei pin attorno al centro della scheda. Qui
    # arrivano i valori grezzi, quindi si applica la stessa riflessione.
    mirror = -1.0 if (data.get('pin_map') and data.get('pin_map_view') == 'bottom') else 1.0
    depth = _band_depth({}, data, pitch)
    return [dict(x_mm=mirror * (origin_x + x), y_mm=origin_y + y, pitch_mm=pitch,
                 edge='matrix', depth_mm=depth) for x, y in pins]


def contact_rects_mm(positions, length_mm, width_mm):
    """@brief Rettangoli (x0,y0,x1,y1) in mm occupati dai contatti di bordo.

    @param positions Uscita di contact_positions.
    @param length_mm Estensione della scheda lungo X.
    @param width_mm Estensione lungo Y.
    @return Lista di rettangoli in millimetri dal centro scheda.
    @note Serve a dire alla misura dei bordi dove NON guardare: la piazzola
        dorata ha un contorno netto quanto quello di un componente, e senza
        questa esclusione una misura buona viene sostituita dal bordo della
        piazzola vicina. La zona parte dalle quote dichiarate -- mezzo passo
        lungo il bordo, la profondita' della piazzola -- piu' il margine
        convenzionale della sigla (euristica dichiarata nella docstring del
        modulo), non una fascia scelta guardando la foto.
    @note Le matrici passanti restano fuori: li' un connettore sta proprio
        sopra i suoi pin, e mascherare la zona impedirebbe di misurarlo.
    """
    rects = []
    for contact in positions or ():
        edge = contact.get('edge')
        pitch = _number(contact.get('pitch_mm'))
        depth = _number(contact.get('depth_mm')) or pitch
        if edge not in ('left', 'right', 'top', 'bottom') or pitch is None:
            continue
        reach = pitch * _BAND_TOLERANCE
        x, y = contact['x_mm'], contact['y_mm']
        if edge == 'left':
            rects.append((x, y - reach, x + depth, y + reach))
        elif edge == 'right':
            rects.append((x - depth, y - reach, x, y + reach))
        elif edge == 'top':
            rects.append((x - reach, y, x + reach, y + depth))
        else:
            rects.append((x - reach, y - depth, x + reach, y))
    return rects


def contact_proposals(proposals, positions, length_mm, width_mm):
    """@brief Indici delle proposte che cadono su un contatto dichiarato.

    @param proposals Proposte del rilevatore, con `_box` normalizzato.
    @param positions Uscita di contact_positions.
    @param length_mm Estensione della scheda lungo X.
    @param width_mm Estensione lungo Y.
    @return Insieme degli indici da scartare.
    @note Tre modi di essere un contatto, tutti su quote dichiarate:
        1. il riquadro sta nella STRISCIA del contatto -- entro mezzo passo
           lungo il bordo e dentro la profondita' che il contatto occupa con la
           sua sigla -- ed e' piccolo quanto il passo. La striscia serve perche'
           il rilevatore annota la piazzola e la sigla, non il centro del
           semi-foro: senza profondita' le file di piazzole passavano quasi
           tutte.
        2. sui contatti di BORDO, un riquadro che ne copre due o piu' e resta
           dentro la striscia e' un pezzo di fila, non un corpo: su un modulo
           castellated non c'e' nulla montato sopra i contatti. Non vale per le
           matrici, dove un connettore sta proprio sopra i suoi pin.
        3. restano fuori i corpi piu' lunghi del passo che sporgono dalla
           striscia: quelli si giudicano guardandoli, non qui.
    """
    if not positions:
        return set()
    found = set()
    for index, proposal in enumerate(proposals):
        box = proposal.get('_box')
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in box)
        left, right = (x0 - 0.5) * length_mm, (x1 - 0.5) * length_mm
        top, bottom = (y0 - 0.5) * width_mm, (y1 - 0.5) * width_mm
        centre_x, centre_y = (left + right) / 2, (top + bottom) / 2
        long_side = max(right - left, bottom - top)
        for contact in positions:
            pitch = contact['pitch_mm']
            depth = contact.get('depth_mm') or pitch
            edge = contact['edge']
            reach = pitch * _BAND_TOLERANCE
            # Distanza lungo il bordo e profondita' dentro la scheda: su una
            # matrice non c'e' un bordo, e la striscia diventa un quadrato.
            if edge in ('left', 'right'):
                along = centre_y - contact['y_mm']
                deep = abs(centre_x - contact['x_mm'])
                # Quanto il riquadro entra nella scheda, misurato dal bordo:
                # e' la quota che distingue un pezzo di fila da un corpo
                # montato sul bordo, che nella striscia non ci sta.
                reach_in = max(abs(left - contact['x_mm']), abs(right - contact['x_mm']))
                span = (top, bottom)
                along_key = 'y_mm'
            elif edge in ('top', 'bottom'):
                along = centre_x - contact['x_mm']
                deep = abs(centre_y - contact['y_mm'])
                reach_in = max(abs(top - contact['y_mm']), abs(bottom - contact['y_mm']))
                span = (left, right)
                along_key = 'x_mm'
            else:
                along = max(abs(centre_x - contact['x_mm']), abs(centre_y - contact['y_mm']))
                deep, reach_in, span, along_key = 0.0, 0.0, None, None
            if deep > depth:
                continue
            if long_side <= pitch and abs(along) <= reach:
                found.add(index)
                break
            # Un riquadro CONTENUTO nella striscia che copre due o piu'
            # contatti e' un pezzo della fila. La contenzione e' il criterio
            # che salva un connettore montato sul bordo: quello entra nella
            # scheda molto piu' della striscia.
            if span is not None and reach_in <= depth:
                covered = sum(1 for other in positions
                              if other['edge'] == edge
                              and span[0] <= other[along_key] <= span[1])
                if covered >= 2:
                    found.add(index)
                    break
    return found
