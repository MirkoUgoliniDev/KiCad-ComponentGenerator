"""@brief Build mounted component geometry inside headless FreeCAD.

Non e' importabile da codice normale: lo carica freecad_build_body.py
(stessa cartella) mentre gira nell'interprete embedded di FreeCAD, dove
``FreeCAD`` e ``Part`` esistono.

Un modulo su PCB non si riconosce dalle scatole: si riconosce perche' il
SOIC ha i reofori a "L" sui lati, il trasformatore ha gli spigoli
raccordati, il LED ha una lente, il tantalio ha la striscia di polarita'.
Ogni ``kind`` qui sotto costruisce quella forma a partire dal solo
ingombro (lunghezza x larghezza x altezza) stimato dalla foto: i dettagli
non sono quotati dal datasheet e non pretendono di esserlo, servono a
rendere il pezzo leggibile a colpo d'occhio nel viewer 3D di KiCad.

Ogni builder ritorna una lista di (shape, color_hex): i pezzi restano
separati per poter avere ciascuno il proprio colore in STEP e VRML.
"""

import json
import math
import os
import sys
import threading
import time

import FreeCAD
import Part


def _warn(template, **values):
    """Avviso per il processo padre, che lo traduce nella lingua dell'utente.

    Qui dentro (interprete di FreeCAD) non c'e' il catalogo delle traduzioni:
    si manda il MODELLO italiano con i valori, in JSON, dopo il marcatore
    `KICADGEN_WARN:`; il padre lo formatta con `messages.tx`
    (`messages.child_warning_text`). Un valore non JSON diventa testo.
    """
    payload = {"t": template,
               "v": {key: value if value is None or isinstance(value, (bool, int, float, str))
                     else str(value) for key, value in values.items()}}
    print("KICADGEN_WARN:" + json.dumps(payload, ensure_ascii=False), flush=True)

V = FreeCAD.Vector

METAL = "#c8c8c8"
LEAD_PITCH_MM = 1.27  # passo tipico dei reofori SOIC


def _box(cx, cy, z0, length, width, height):
    """Parallelepipedo centrato in (cx, cy) con la base a z0."""
    return Part.makeBox(length, width, height, V(cx - length / 2, cy - width / 2, z0))


def _fillet(shape, radius, edges):
    """makeFillet che non fa fallire la generazione: un raccordo impossibile
    (raggio troppo grande per il pezzo) lascia lo spigolo vivo."""
    if radius <= 0 or not edges:
        return shape
    try:
        return shape.makeFillet(radius, edges)
    except Exception:  # noqa: BLE001 - spigolo vivo, non fatale
        return shape


def _vertical_edges(shape):
    return _edges_along(shape, 2)


def _edges_along(shape, axis):
    """Spigoli paralleli all'asse indicato (0=X, 1=Y, 2=Z).

    Raccordarli trasforma la sezione perpendicolare a quell'asse: su un
    prisma a base rettangolare con raggio pari a meta' del lato corto si
    ottiene una sezione a stadio, che e' la forma della bocca di un USB.
    """
    selected = []
    for edge in shape.Edges:
        box = edge.BoundBox
        spans = (box.XLength, box.YLength, box.ZLength)
        others = max(spans[(axis + 1) % 3], spans[(axis + 2) % 3])
        if spans[axis] > others + 1e-6:
            selected.append(edge)
    return selected


def _fillet_best(shape, radius, edges):
    """Raccordo che ripiega su un raggio minore invece che sullo spigolo vivo.

    Un raggio pari a meta' del lato corto e' esattamente il caso degenere:
    i due raccordi si incontrano in tangenza e OCC rifiuta l'operazione.
    Con `_fillet` il risultato era uno spigolo vivo, cioe' un foro squadrato
    al posto dell'asola -- un fallimento silenzioso proprio nel caso che
    interessa di piu'. Qui si riprova a scalare finche' non riesce.
    """
    for factor in (1.0, 0.96, 0.9, 0.8, 0.65, 0.5):
        candidate = _fillet(shape, radius * factor, edges)
        if candidate is not shape:
            return candidate
    return shape


def _upper_edges_along(shape, axis):
    """Solo i due spigoli lunghi in alto.

    Il guscio di un USB ha il fondo piatto -- appoggia sulla scheda -- e gli
    spigoli superiori molto stondati. Raccordare anche quelli in basso lo
    farebbe sembrare un tubo sospeso.
    """
    ztop = shape.BoundBox.ZMax
    return [e for e in _edges_along(shape, axis)
            if abs(e.BoundBox.ZMax - ztop) < 1e-6]


def _top_edges(shape):
    ztop = shape.BoundBox.ZMax
    return [
        e for e in shape.Edges
        if all(abs(v.Point.z - ztop) < 1e-6 for v in e.Vertexes)
    ]


def _rounded_box(cx, cy, z0, length, width, height, color, ratio=0.15, cap=0.6):
    """Box con spigoli verticali e superiori raccordati (case stampati, trasformatori)."""
    shape = _box(cx, cy, z0, length, width, height)
    radius = min(cap, ratio * min(length, width, height))
    shape = _fillet(shape, radius, _vertical_edges(shape))
    shape = _fillet(shape, radius * 0.8, _top_edges(shape))
    return [(shape, color)]


def _generic(cx, cy, z0, length, width, height, color):
    return [(_box(cx, cy, z0, length, width, height), color)]


def _normalized_box(value):
    """Validate a local image rectangle without clipping or guessing coordinates."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("terminal_geometry bbox must contain four coordinates")
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or not 0 <= v <= 1 for v in value):
        raise ValueError("terminal_geometry bbox coordinates must be finite numbers in [0, 1]")
    left, top, right, bottom = value
    if left >= right or top >= bottom:
        raise ValueError("terminal_geometry bbox must have positive area")
    return tuple(value)


def _validated_terminal_geometry(geometry):
    """Validate the complete observation before constructing any CAD shapes."""
    if not isinstance(geometry, dict):
        raise ValueError("terminal_geometry must be an object")
    if geometry.get("confidence") not in ("high", "medium"):
        raise ValueError("terminal_geometry confidence must be high or medium")
    body = _normalized_box(geometry.get("body_bbox"))
    terminals = geometry.get("terminals")
    if not isinstance(terminals, list) or len(terminals) > 128:
        raise ValueError("terminal_geometry terminals must be a list of at most 128 boxes")
    boxes = []
    for terminal in terminals:
        if not isinstance(terminal, dict):
            raise ValueError("terminal_geometry terminal must be an object with bbox")
        envelope = _normalized_box(terminal.get("bbox"))
        detail = terminal.get('lead_detail')
        if detail is not None:
            try:
                try:
                    from kicadgen.cad3d.lead_geometry import validated_lead_bbox
                except ModuleNotFoundError:
                    from lead_geometry import validated_lead_bbox
                envelope = validated_lead_bbox(body, envelope, detail)
            except (ValueError,TypeError) as exc:
                _warn("dettaglio reoforo ignorato ({exc}); terminale originale conservato", exc=exc)
        boxes.append(envelope)
    return body, boxes


def _observed_terminals(cx, cy, z0, length, width, height, color, geometry):
    """Render observed boxes in the existing total envelope, with no pin inference.

    Rectangles use local X-right/Y-down coordinates. The caller has already
    converted the component centre to CAD Y-up and rotates all returned shapes
    once. Only the local Y offset is inverted here; axes are never swapped based
    on aspect ratio. Heights and small risers are conservative visual estimates.
    """
    body_bbox, terminal_boxes = _validated_terminal_geometry(geometry)

    def bounds(bbox):
        left, top, right, bottom = bbox
        return (cx + (left - 0.5) * length, cy + (0.5 - bottom) * width,
                cx + (right - 0.5) * length, cy + (0.5 - top) * width)

    def box_at(rect, base, thickness):
        left, bottom, right, top = rect
        return Part.makeBox(right - left, top - bottom, thickness,
                            V(left, bottom, base))

    body_bounds = bounds(body_bbox)
    standoff = min(0.12, height * 0.1)
    lead_t = min(0.08, height * 0.05)
    riser_h = min(height * 0.3, standoff + 0.1)
    parts = [(box_at(body_bounds, z0 + standoff, height - standoff), color)]
    for bbox in terminal_boxes:
        rect = bounds(bbox)
        foot = box_at(rect, z0, lead_t)
        left, bottom, right, top = rect
        # Raise only a small area where the observed terminal touches/overlaps
        # the body. Do not bridge a gap or extend metal beyond the observation.
        near_x = min(max((left + right) / 2, body_bounds[0]), body_bounds[2])
        near_y = min(max((bottom + top) / 2, body_bounds[1]), body_bounds[3])
        if left <= near_x <= right and bottom <= near_y <= top:
            dx = min(0.12, (right - left) * 0.5)
            dy = min(0.12, (top - bottom) * 0.5)
            rx = min(max(near_x - dx / 2, left), right - dx)
            ry = min(max(near_y - dy / 2, bottom), top - dy)
            riser = box_at((rx, ry, rx + dx, ry + dy), z0, riser_h)
            foot = foot.fuse(riser)
        parts.append((foot, METAL))
    return parts


def _ic_soic(cx, cy, z0, length, width, height, color):
    """Integrato con reofori a ala di gabbiano sui due lati lunghi.

    Il corpo e' sollevato di uno standoff e piu' stretto dell'ingombro totale,
    perche' l'ingombro letto dalla foto comprende i reofori. Il numero di
    reofori si ricava dal passo standard: per l'aspetto conta che ci siano,
    non che siano esattamente quelli del datasheet.
    """
    parts = []
    along_x = length >= width
    span = length if along_x else width      # lato con i reofori
    depth = width if along_x else length     # lato senza reofori
    lead_out = min(0.9, depth * 0.18)        # sporgenza del reoforo dal corpo
    body_depth = depth - 2 * lead_out
    standoff = min(0.12, height * 0.1)
    body_h = height - standoff
    body = _box(cx, cy, z0 + standoff, span if along_x else body_depth,
                body_depth if along_x else span, body_h)
    body = _fillet(body, min(0.15, body_h * 0.15), _top_edges(body))
    # Tacca del pin 1 nell'angolo, come sui package reali.
    dimple = Part.makeCylinder(min(0.35, depth * 0.08), 0.3,
                               V(cx - (span / 2 - 0.9 if along_x else body_depth / 2 - 0.6),
                                 cy - (body_depth / 2 - 0.6 if along_x else span / 2 - 0.9),
                                 z0 + height - 0.12), V(0, 0, 1))
    try:
        body = body.cut(dimple)
    except Exception:  # noqa: BLE001 - senza tacca
        pass
    parts.append((body, color))

    n_leads = max(2, min(14, int(round((span - 1.0) / LEAD_PITCH_MM))))
    lead_w = 0.42
    lead_t = 0.22
    start = -(n_leads - 1) * LEAD_PITCH_MM / 2
    riser_h = max(0.2, body_h * 0.55)
    for i in range(n_leads):
        along = start + i * LEAD_PITCH_MM
        for side in (-1, 1):
            edge = side * (body_depth / 2)
            if along_x:
                foot = Part.makeBox(lead_w, lead_out, lead_t,
                                    V(cx + along - lead_w / 2,
                                      cy + (edge if side > 0 else edge - lead_out), z0))
                riser = Part.makeBox(lead_w, lead_t, riser_h + standoff,
                                     V(cx + along - lead_w / 2,
                                       cy + (edge - lead_t if side > 0 else edge), z0))
            else:
                foot = Part.makeBox(lead_out, lead_w, lead_t,
                                    V(cx + (edge if side > 0 else edge - lead_out),
                                      cy + along - lead_w / 2, z0))
                riser = Part.makeBox(lead_t, lead_w, riser_h + standoff,
                                     V(cx + (edge - lead_t if side > 0 else edge),
                                       cy + along - lead_w / 2, z0))
            parts.append((foot.fuse(riser), METAL))
    return parts


def _ic_qfn(cx, cy, z0, length, width, height, color):
    body = _box(cx, cy, z0, length, width, height)
    dot = Part.makeCylinder(min(0.3, length * 0.08), 0.2,
                            V(cx - length / 2 + 0.6, cy - width / 2 + 0.6, z0 + height - 0.08),
                            V(0, 0, 1))
    try:
        body = body.cut(dot)
    except Exception:  # noqa: BLE001
        pass
    return [(body, color)]


def _ic_observed_pins(cx, cy, z0, length, width, height, color, kind, pin_count=None, pins_by_side=None):
    """IC body plus terminals counted from the photo, when supplied by AI."""
    parts = [(_box(cx, cy, z0, length, width, height), color)]
    sides = dict(pins_by_side or {})
    if not sides and pin_count:
        if kind == "ic_qfn":
            base = pin_count // 4
            sides = {s: base for s in ("left", "right", "top", "bottom")}
            for side in ("left", "right", "top", "bottom")[:pin_count % 4]:
                sides[side] += 1
        else:
            left = pin_count // 2
            sides = {"left": left, "right": pin_count - left}
    lead_t = min(0.18, max(0.08, height * 0.12))
    lead_out = min(0.8, max(0.3, min(length, width) * 0.16))
    metal_w = min(0.42, max(0.18, min(length, width) / 8))
    for side in ("left", "right", "top", "bottom"):
        count = int(sides.get(side, 0) or 0)
        if count <= 0:
            continue
        span = width if side in ("left", "right") else length
        pitch = span / (count + 1)
        for index in range(count):
            along = -span / 2 + (index + 1) * pitch
            if side == "left":
                pad = Part.makeBox(lead_out, metal_w, lead_t,
                                   V(cx - length / 2 - lead_out, cy + along - metal_w / 2, z0))
            elif side == "right":
                pad = Part.makeBox(lead_out, metal_w, lead_t,
                                   V(cx + length / 2, cy + along - metal_w / 2, z0))
            elif side == "top":
                pad = Part.makeBox(metal_w, lead_out, lead_t,
                                   V(cx + along - metal_w / 2, cy - width / 2 - lead_out, z0))
            else:
                pad = Part.makeBox(metal_w, lead_out, lead_t,
                                   V(cx + along - metal_w / 2, cy + width / 2, z0))
            parts.append((pad, METAL))
    return parts


def _led(cx, cy, z0, length, width, height, color):
    """LED SMD: base chiara e lente a cupola del colore del LED."""
    base_h = max(0.15, height * 0.4)
    base = _box(cx, cy, z0, length, width, base_h)
    parts = [(base, "#e6e6e0")]
    r = min(length, width) * 0.45
    if r > 0.05:
        centre_z = z0 + base_h
        sphere = Part.makeSphere(r, V(cx, cy, centre_z))
        clip = Part.makeBox(length, width, height - base_h + 1.0,
                            V(cx - length / 2, cy - width / 2, centre_z))
        try:
            dome = sphere.common(clip)
            if dome.Volume > 0:
                parts.append((dome, color if color else "#f2f2c8"))
        except Exception:  # noqa: BLE001 - LED senza lente
            pass
    return parts


def _tantalum(cx, cy, z0, length, width, height, color):
    """Condensatore al tantalio: case raccordato e striscia di polarita'."""
    parts = _rounded_box(cx, cy, z0, length, width, height, color, ratio=0.12, cap=0.35)
    stripe_len = max(0.2, length * 0.14)
    stripe = Part.makeBox(stripe_len, width * 0.9, 0.03,
                          V(cx + length / 2 - stripe_len - 0.05, cy - width * 0.45, z0 + height))
    parts.append((stripe, "#f0e6c8"))
    return parts


def _chip(cx, cy, z0, length, width, height, color):
    """Passivo chip (resistenza/condensatore): corpo + terminazioni metalliche."""
    cap = max(0.15, length * 0.2)
    body = _box(cx, cy, z0, length - 2 * cap + 0.02, width, height)
    parts = [(body, color)]
    for side in (-1, 1):
        x = cx + side * (length / 2 - cap / 2)
        parts.append((_box(x, cy, z0, cap, width, height), METAL))
    return parts


def _diode(cx, cy, z0, length, width, height, color):
    parts = _chip(cx, cy, z0, length, width, height, color or "#1c1c1c")
    band = Part.makeBox(max(0.15, length * 0.08), width, 0.03,
                        V(cx - length / 2 + length * 0.3, cy - width / 2, z0 + height))
    parts.append((band, "#e8e8e8"))
    return parts


def _diode_melf(cx, cy, z0, length, width, height, color):
    """Diodo MELF: cilindro di vetro sdraiato lungo X, cappucci metallici e banda.

    Il diametro e' la dimensione minore dell'ingombro; l'altezza dichiarata
    non conta, un cilindro sdraiato e' alto quanto il suo diametro.
    """
    along_x = length >= width
    span = length if along_x else width
    radius = min(width if along_x else length, height) / 2
    zc = z0 + radius
    axis = V(1, 0, 0) if along_x else V(0, 1, 0)
    start = V(cx - span / 2, cy, zc) if along_x else V(cx, cy - span / 2, zc)
    glass = Part.makeCylinder(radius * 0.96, span, start, axis)
    parts = [(glass, color or "#b01818")]
    cap = max(0.25, span * 0.2)
    for offset in (0.0, span - cap):
        base = V(cx - span / 2 + offset, cy, zc) if along_x else V(cx, cy - span / 2 + offset, zc)
        parts.append((Part.makeCylinder(radius, cap, base, axis), METAL))
    band_w = max(0.12, span * 0.08)
    band_at = cap + span * 0.08
    base = V(cx - span / 2 + band_at, cy, zc) if along_x else V(cx, cy - span / 2 + band_at, zc)
    parts.append((Part.makeCylinder(radius * 0.98, band_w, base, axis), "#1c1c1c"))
    return parts


def _coaxial_connector(cx, cy, z0, length, width, height, color):
    """Coassiale verticale: base squadrata piu' il barilotto di innesto.

    E' la forma del connettore d'antenna U.FL/IPEX che sta sul bordo dei
    moduli WiFi. Il cavo entra dall'alto, non di lato: dargli la bocca
    laterale degli altri connettori lo rendeva irriconoscibile e per giunta
    apriva il guscio nella direzione sbagliata.

    Il barilotto e' metallico a prescindere dal colore letto sulla foto:
    su un pezzo cosi' piccolo il colore campionato e' quasi sempre quello
    del PCB circostante.
    """
    base_height = height * 0.55
    base = _box(cx, cy, z0, length, width, base_height)
    base = _fillet(base, min(length, width) * 0.12, _vertical_edges(base))
    parts = [(base, color)]
    radius = min(length, width) * 0.34
    barrel = height - base_height
    if radius <= 0.05 or barrel <= 0.05:
        return [(_box(cx, cy, z0, length, width, height), color)]
    ring = Part.makeCylinder(radius, barrel, V(cx, cy, z0 + base_height))
    # Cavita' centrale: il contatto a molla si vede dentro l'anello.
    try:
        ring = ring.cut(Part.makeCylinder(radius * 0.55, barrel,
                                          V(cx, cy, z0 + base_height + barrel * 0.35)))
    except Exception:  # noqa: BLE001 - anello pieno, non fatale
        pass
    parts.append((ring, METAL))
    return parts


def _outward_sign(cx, cy, axis, rotation_deg):
    """Verso locale che, DOPO la rotazione del chiamante, guarda fuori scheda.

    Il builder lavora nel sistema locale non ruotato: e' il chiamante che
    ruota la forma di `rotation_deg` attorno al centro del componente. Se il
    verso si decidesse dalla sola posizione globale, una rotazione di 180
    gradi ribalterebbe la bocca verso l'interno della scheda -- dove stanno i
    pin, non il cavo.

    Qui si proietta la posizione del componente sull'asse di innesto cosi'
    come apparira' DOPO la rotazione: il segno di quella proiezione e' il
    verso locale da costruire.
    """
    angle = math.radians(rotation_deg or 0)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    # Immagine globale degli assi locali X e Y dopo la rotazione.
    projection = (cx * cos_a + cy * sin_a) if axis == 0 else (-cx * sin_a + cy * cos_a)
    return -1.0 if projection < 0 else 1.0


def _connector(cx, cy, z0, length, width, height, color, rotation_deg=0, mating=None):
    """Approximate an observed entry; unknown entry stays a generic envelope.

    Top-entry barrels and edge-entry shells are visual estimates, not observed
    connector identities. The short-axis/outward direction below remains a
    legacy heuristic from the USB example; an observed entry vector would
    replace it. Mating alone does not establish barrel shape or entry polarity.

    Un connettore si innesta lateralmente (USB, jack, header schermato): la
    bocca sta sulla faccia rivolta verso l'esterno della scheda, non sul
    coperchio. Scavarla dall'alto produceva una scatola con un incasso
    superiore, che in anteprima non si distingue da un package generico.

    L'asse di innesto e' la dimensione orizzontale minore -- la profondita'
    di inserimento -- e il verso e' quello che si allontana dal centro della
    scheda, che per un connettore da bordo e' il bordo stesso. Restano
    pareti di spessore `wall` su fondo, fianchi, cielo e base: il pezzo e'
    un guscio, non un tubo passante.
    """
    # Dimensions cannot distinguish a top entry from an edge entry.
    if mating not in ("edge", "top"):
        _warn("connector mating sconosciuto; ingombro generico")
        return _generic(cx, cy, z0, length, width, height, color)
    if mating == "top":
        return _coaxial_connector(cx, cy, z0, length, width, height, color)

    shell = _box(cx, cy, z0, length, width, height)
    wall = max(0.2, min(length, width, height) * 0.15)
    # Un package troppo piccolo per avere pareti resta pieno: meglio una
    # scatola onesta di un guscio degenere con facce che si compenetrano.
    if min(length, width, height) <= 2.5 * wall:
        return [(shell, color)]

    axis = 0 if length <= width else 1
    sign = _outward_sign(cx, cy, axis, rotation_deg)
    if axis == 0:
        mating = length
        cross_w, cross_h = width - 2 * wall, height - 2 * wall
        cavity = _box(cx + sign * wall, cy, z0 + wall, mating, cross_w, cross_h)
    else:
        mating = width
        cross_w, cross_h = length - 2 * wall, height - 2 * wall
        cavity = _box(cx, cy + sign * wall, z0 + wall, cross_w, mating, cross_h)

    # Bocca ad asola: raccordando gli spigoli paralleli all'asse di innesto
    # con meta' del lato corto, la sezione rettangolare diventa uno stadio.
    # E' il profilo di un USB, non un foro squadrato.
    cavity = _fillet_best(cavity, min(cross_w, cross_h) / 2, _edges_along(cavity, axis))
    # Il guscio ha il fondo piatto e il cielo molto stondato: raccordare solo
    # i due spigoli lunghi superiori. Il raggio e' una frazione grande del
    # lato corto, altrimenti resta una scatola con gli angoli smussati.
    shell = _fillet_best(shell, min(cross_w, cross_h) * 0.45, _upper_edges_along(shell, axis))
    try:
        shell = shell.cut(cavity)
    except Exception:  # noqa: BLE001
        return [(_box(cx, cy, z0, length, width, height), color)]

    # Una bocca larga e bassa e' quella della famiglia USB, che ha la
    # linguetta dei contatti al centro. E' una regola sulla forma misurata,
    # non sul nome del componente: un jack cilindrico non la riceve.
    if max(cross_w, cross_h) < 2.5 * min(cross_w, cross_h):
        return [(shell, color)]
    shell = _dimples(shell, cx, cy, z0, axis, sign, mating, cross_w, height, wall)
    parts = [(shell, color)]
    tongue = _tongue(cx, cy, z0, axis, sign, mating, cross_w, cross_h, wall)
    if tongue is not None:
        parts.append((tongue, "#2a2a2a"))
    return parts


def _dimples(shell, cx, cy, z0, axis, sign, mating, cross_w, height, wall):
    """Le due bugne stampate sul cielo del guscio.

    Sono il dettaglio che si riconosce subito su un ricettacolo USB. Poco
    profonde: servono a spezzare il riflesso della faccia superiore, non a
    riprodurre una quota. Se la sottrazione fallisce resta il cielo liscio.
    """
    radius = min(cross_w * 0.10, mating * 0.10)
    depth = min(wall * 0.6, height * 0.08)
    if radius < 0.12 or depth < 0.03:
        return shell
    # Arretrate rispetto alla bocca, simmetriche sull'asse trasversale.
    back = -sign * mating * 0.18
    for offset in (-cross_w * 0.26, cross_w * 0.26):
        px, py = ((cx + back, cy + offset) if axis == 0 else (cx + offset, cy + back))
        try:
            shell = shell.cut(Part.makeCylinder(radius, depth * 2,
                                                V(px, py, z0 + height - depth)))
        except Exception:  # noqa: BLE001 - cielo liscio, non fatale
            return shell
    return shell


def _tongue(cx, cy, z0, axis, sign, mating, cross_w, cross_h, wall):
    """Linguetta dei contatti, arretrata rispetto alla bocca.

    Non arriva a filo: si vede in fondo all'asola, come nel pezzo reale.
    Ritorna None quando la cavita' e' troppo sottile per ospitarla senza
    toccare le pareti.
    """
    thickness = cross_h * 0.34 if cross_w >= cross_h else cross_w * 0.34
    depth = mating * 0.55
    if thickness < 0.08 or depth <= wall:
        return None
    inset = wall  # quanto la linguetta resta indietro rispetto al filo bocca
    # Il verso e' quello della bocca: la linguetta si affaccia sull'asola,
    # non si nasconde in fondo alla cavita'.
    centre = sign * (mating / 2 - inset - depth / 2)
    span = (cross_w if cross_w >= cross_h else cross_h) * 0.72
    if axis == 0:
        return _box(cx + centre, cy, z0 + wall + (cross_h - thickness) / 2,
                    depth, span, thickness)
    return _box(cx, cy + centre, z0 + wall + (cross_h - thickness) / 2,
                span, depth, thickness)


def _shade(color, factor):
    """Variante piu' chiara o piu' scura dello stesso colore.

    Serve a staccare due parti dello stesso pezzo (base e pulsante) senza
    inventare un colore che la foto non ha mai mostrato.
    """
    text = str(color or "").lstrip("#")
    if len(text) != 6:
        return color
    try:
        channels = [int(text[i:i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        return color
    return "#" + "".join(f"{min(255, max(0, round(c * factor))):02x}" for c in channels)


def _tactile_switch(cx, cy, z0, length, width, height, color):
    """Micro-switch: base squadrata piu' il pulsante cilindrico che sporge.

    E' la forma che si riconosce a colpo d'occhio in anteprima -- BOOT e
    RESET su una scheda di sviluppo -- e che una scatola liscia perde.
    Il cilindro e' centrato: il pulsante decentrato esiste, ma non si
    deduce dal rettangolo misurato sulla foto.
    """
    radius = min(length, width) * 0.32
    travel = height * 0.38
    # Senza il pulsante la base deve prendersi tutta l'altezza: un pezzo che
    # rimpicciolisce falserebbe l'ingombro misurato sulla foto.
    if radius <= 0.05 or travel <= 0.05:
        return [(_box(cx, cy, z0, length, width, height), color)]
    base = _box(cx, cy, z0, length, width, height - travel)
    base = _fillet(base, min(length, width) * 0.10, _vertical_edges(base))
    plunger = Part.makeCylinder(radius, travel, V(cx, cy, z0 + height - travel))
    return [(base, color), (plunger, _shade(color, 1.45))]


def _transformer(cx, cy, z0, length, width, height, color):
    return _rounded_box(cx, cy, z0, length, width, height, color, ratio=0.12, cap=0.6)


def _crystal(cx, cy, z0, length, width, height, color):
    return _rounded_box(cx, cy, z0, length, width, height, color or "#b8b8b8",
                        ratio=0.3, cap=0.8)


BUILDERS = {
    "generic": _generic,
    # An unidentified IC does not establish a package or terminal layout.
    "ic": _generic,
    "ic_soic": _ic_soic,
    "ic_qfn": _ic_qfn,
    "transformer": _transformer,
    "module": _transformer,
    "led": _led,
    "capacitor_tantalum": _tantalum,
    "capacitor": _chip,
    "resistor": _chip,
    "inductor": _transformer,
    "diode": _diode,
    "diode_melf": _diode_melf,
    "connector": _connector,
    "switch": _tactile_switch,
    "crystal": _crystal,
}

#: Elenco esposto allo schema di estrazione (kicadgen.services) -- tenerlo
#: allineato a BUILDERS; e' duplicato la' perche' questo modulo non e'
#: importabile fuori da FreeCAD.
KINDS = tuple(BUILDERS)


class _ModelColor(str):
    """Keep the existing hex-color contract while carrying native face styles."""

    def __new__(cls, fallback, face_colors, path):
        value = super().__new__(cls, fallback)
        value.face_colors = face_colors
        value.model_path = str(path)
        return value


#: Intervallo fra due battiti, in secondi. E' SOLO un ripiego per il caso in cui
#: il file dei parametri non porti il campo `heartbeat_interval_s` (payload
#: vecchio, prova manuale dello script): il valore buono e' quello derivato dal
#: timeout del watchdog in `kicadgen.cad3d.process_log.heartbeat_interval` e
#: arriva da li' tramite `set_heartbeat_interval`. Questo modulo gira
#: nell'interprete embedded di FreeCAD e non puo' importare quel modulo, quindi
#: il numero viaggia nel JSON invece di essere duplicato qui.
_HEARTBEAT_INTERVAL_S = 60.0


def set_heartbeat_interval(seconds):
    """@brief Adopt the heartbeat period derived from the watchdog's timeout.

    @param seconds Value of `heartbeat_interval_s` from the params file; None,
        zero or a non-numeric value leaves the fallback in place.
    @return The interval in seconds that will actually be used.
    @note Chiamata una volta da freecad_build_body.main(), prima del ciclo dei
        componenti. Non si accetta un intervallo non positivo o non finito: un
        battito ogni zero secondi riempirebbe la pipe e affamerebbe proprio il
        lavoro che deve annunciare.
    """
    global _HEARTBEAT_INTERVAL_S
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return _HEARTBEAT_INTERVAL_S
    if value > 0 and math.isfinite(value):
        _HEARTBEAT_INTERVAL_S = value
    return _HEARTBEAT_INTERVAL_S


def _model_file_name(path):
    """Nome del file del modello, qualunque sia il separatore nel percorso.

    `os.path.basename` conosce solo il separatore del sistema su cui gira, ma
    il percorso arriva dalla libreria KiCad e viaggia nel file dei parametri:
    un percorso Windows letto su un sistema POSIX (o al contrario) resterebbe
    intero e la riga finirebbe accorciata dalla modale proprio nella parte che
    l'utente riconosce. Si taglia su entrambi i separatori.
    """
    text = str(path or "").rstrip("/\\")
    for separator in ("/", "\\"):
        text = text.rpartition(separator)[2] or text
    return text or str(path)


def _elapsed_phrase(seconds):
    """Durata leggibile da una persona: "45 s", "5 min 12 s"."""
    whole = int(max(0.0, seconds))
    if whole < 60:
        return f"{whole} s"
    return f"{whole // 60} min {whole % 60} s"


def heartbeat_line(model_name, seconds):
    """@brief Compose the one line that keeps the watchdog quiet AND informs the user.

    @param model_name File name of the model being imported (never a full path).
    @param seconds Seconds elapsed since the import started.
    @return An Italian sentence, ready to print on the child's stdout.
    @note UN SOLO meccanismo per due scopi. La riga deve (a) rompere il
        silenzio, cosi' il watchdog di run_logged non scambia un import lento
        per un processo fermo, e (b) spiegare all'utente perche' la barra e'
        ferma su "12/52". Un marcatore `KICADGEN_*` farebbe solo (a): la modale
        lo scarta come riga macchina (analysis_overlay.is_machine_line), quindi
        l'utente continuerebbe a guardare una barra congelata.
    @note La forma e' scelta per attraversare i filtri della modale COSI' COME
        SONO, senza toccarli: niente marcatore in maiuscolo in testa, niente
        percorso assoluto (di cui resterebbe solo il nome del file), niente
        "chiave=valore", niente " : " spaziato, niente punto e virgola (che
        `main_window.per_item_detail` legge come rendiconto per-elemento) e
        niente parentesi con conteggi. Resta il NOME DEL FILE, che e' la parte
        che l'utente riconosce, e il tempo trascorso, che e' la ragione per cui
        sta aspettando. I test lo verificano importando quelle due funzioni.
    """
    return (f"Importazione del modello 3D {model_name} ancora in corso da "
            f"{_elapsed_phrase(seconds)}, un modello di libreria grande puo' "
            f"richiedere minuti.")


class _ImportHeartbeat:
    """@brief Announce that a silent library import is still alive.

    @note Perche' esiste: `Import.insert` e `Part.read` parsano un assieme STEP
        senza stampare NULLA. Su una corsa reale un modulo RF schermato ha
        tenuto lo stdout muto per cinque minuti e il watchdog ha ucciso un
        processo sano al tredicesimo componente su cinquantadue, buttando via
        anche le chiamate AI gia' pagate. La correzione non e' alzare la
        soglia -- sarebbe la soglia tarata sul caso visto, e un modello piu'
        grande la rifarebbe scattare -- ma rendere di nuovo vera la premessa
        del watchdog: chi lavora, parla.
    @note Il thread e' un demone e NON tocca mai FreeCAD, Part o il documento:
        formatta una stringa e la stampa. Le API di FreeCAD non sono
        thread-safe, quindi questa e' la sola cosa che puo' fare mentre il
        thread principale e' dentro il parser OCCT.
    @note LIMITE DICHIARATO, non verificabile senza FreeCAD: un battito parte
        solo se l'interprete Python gira, cioe' se la chiamata C++ dell'import
        rilascia il GIL. Se non lo rilasciasse, il silenzio tornerebbe e il
        watchdog ricomincerebbe a uccidere import sani. Per questo il battito
        non e' affidato al solo timer: `beat_if_due` viene chiamato anche nei
        punti Python attraversati durante l'import (la copia dei pezzi
        dell'assieme), che battono comunque.
    """

    def __init__(self, path, interval=None):
        self._name = _model_file_name(path)
        self._interval = float(interval) if interval else _HEARTBEAT_INTERVAL_S
        self._stop = threading.Event()
        # Il lock serve a una promessa precisa: dopo stop() non esce piu' una
        # riga. Senza, un battito gia' entrato in print apparirebbe DOPO il
        # marcatore di fine import e il log racconterebbe una bugia.
        self._lock = threading.Lock()
        self._thread = None
        self._started = 0.0
        self._last = 0.0

    def start(self):
        """@brief Start beating; safe to call once per import."""
        self._started = self._last = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="kicadgen-import-heartbeat")
        self._thread.start()
        return self

    def stop(self):
        """@brief Stop beating for good; idempotent and safe from any state."""
        with self._lock:
            self._stop.set()
        thread = self._thread
        if thread is not None:
            # wait() torna subito quando l'evento e' settato, quindi questo
            # join non aspetta un intervallo. Il tetto e' una rete di sicurezza
            # indipendente dall'intervallo: la generazione non deve fermarsi
            # per colpa del battito nemmeno con un intervallo lunghissimo.
            thread.join(timeout=min(self._interval, 5.0))
            self._thread = None

    def beat_if_due(self):
        """@brief Emit a beat from the working thread when one is due.

        @note Serve nei punti Python lunghi dell'import: battono anche se il
            timer non riesce a girare. Non stampa in anticipo, quindi non
            raddoppia le righe quando il timer funziona.
        """
        if time.monotonic() - self._last >= self._interval:
            self._beat()

    def _loop(self):
        while not self._stop.wait(self._interval):
            self._beat()

    def _beat(self):
        with self._lock:
            if self._stop.is_set():
                return
            now = time.monotonic()
            self._last = now
            try:
                # Una sola write, testo e a capo insieme: con lo stdout a
                # buffer di riga e' cio' che rende il battito indivisibile
                # rispetto alle righe stampate dal thread di lavoro. print()
                # scrive il testo e l'a capo separatamente, e le due righe si
                # potrebbero intrecciare a meta'.
                sys.stdout.write(heartbeat_line(self._name, now - self._started) + "\n")
                sys.stdout.flush()
            except Exception:  # noqa: BLE001 - stdout chiuso: il battito non e' il lavoro
                self._stop.set()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        # Vale anche quando l'import solleva: il battito non deve sopravvivere
        # al passo che stava annunciando.
        self.stop()
        return False


def _read_library_parts(path, color, heartbeat=None):
    """Copy shapes and face colors out of an isolated headless import document."""
    import Import

    previous = FreeCAD.ActiveDocument
    doc = FreeCAD.newDocument("kicadgen_library_import")
    try:
        imported = Import.insert(str(path), doc.Name, merge=False)
        if not imported:
            raise ValueError("headless importer returned no colored shapes")
        parts = []
        for obj, colors in imported:
            # Copiare le forme di un assieme grande e' lento quanto leggerlo:
            # e' Python, quindi qui il battito non dipende dal GIL del parser.
            if heartbeat is not None:
                heartbeat.beat_if_due()
            shape = obj.Shape.copy()
            if shape.isNull() or not shape.Solids or not shape.isValid():
                raise ValueError("library model contains invalid or non-solid geometry")
            # Imported assembly objects may have placements inherited from a parent.
            shape.Placement = obj.getGlobalPlacement()
            colors = list(colors)
            if len(colors) == 1:
                colors *= len(shape.Faces)
            if len(colors) != len(shape.Faces):
                raise ValueError("native face-color count does not match geometry")
            parts.append((shape, _ModelColor(color, colors, path)))
        return parts
    finally:
        FreeCAD.closeDocument(doc.Name)
        if previous is not None:
            FreeCAD.setActiveDocument(previous.Name)


def _native_axis_angle(references, length, width):
    """@brief Decide which model axis must end up along the photographed length.

    @param references Bounding boxes to compare (full solid, and the section
        through the body when it is available): a lead envelope can have a
        different long axis from the plastic body.
    @param length Photographed envelope along X, in mm.
    @param width Photographed envelope along Y, in mm.
    @return (angle in degrees, the reference that matched) with angle 0 or 90.
    @note The axis is decided by WHICH side is the longer one, never by a sum
        of ratios. A sum of ratios is not scale invariant: with a photographic
        envelope inflated by 28% it flipped the choice with a 1% margin and put
        the model's long axis across the board's short one, which moves an
        asymmetric feature from a short edge to a long edge. Reading the sides
        in order is also the reading that kicad_models.size_distance already
        used to call that model compatible: before this, the two stages
        disagreed in silence and nothing reported it.
    """
    measured_long, measured_short = sorted((length, width), reverse=True)
    measured_long_on_x = length >= width
    choices = []
    for reference in references:
        model_long_on_x = reference.XLength >= reference.YLength
        model_long = max(reference.XLength, reference.YLength)
        model_short = min(reference.XLength, reference.YLength)
        # Same residual as the matcher: the worse of the two sides, so a good
        # fit on one axis cannot pay for a bad one on the other.
        score = max(abs(model_long - measured_long) / measured_long,
                    abs(model_short - measured_short) / measured_short)
        choices.append((score, 0 if model_long_on_x == measured_long_on_x else 90,
                        reference))
    score, angle, reference = min(choices, key=lambda choice: choice[0])
    return angle, reference


#: Frazione di volume che non sopravvive a mezzo giro sotto la quale il solido
#: e' simmetrico: quel che resta e' residuo numerico del boolean, non una
#: feature. Su un corpo davvero simmetrico il residuo tende a zero da solo,
#: quindi la soglia separa il rumore dal segnale e non taglia su misura un
#: pezzo: il modulo su cui e' stata provata sta al 15,9%, un ordine di
#: grandezza sopra.
NATIVE_ASYMMETRY_MIN_FRACTION = 0.01

#: Quanto la direzione dominante deve staccare l'altra perche' la feature si
#: possa attribuire a un LATO. A parita' la direzione punta alla diagonale,
#: cioe' a un angolo, e un angolo non si confronta con un lato dichiarato.
NATIVE_ASYMMETRY_DOMINANCE = 2.0

#: Quanto il residuo deve essere spostato dal centro, in frazione di semilato,
#: perche' indichi un capo. Un residuo che c'e' ma sta al centro significa che
#: l'asimmetria e' BILANCIATA fra i due capi e non li distingue: misurato su un
#: SOT-223 reale, dove il residuo pesa il 4,9% del volume ma si divide in due
#: lobi identici (0,9156 mm3 da una parte e 0,9156 dall'altra). Senza questo
#: controllo un centesimo di micron di rumore diventerebbe un lato.
#: Per confronto, sugli stessi file: modulo schermato 0,181; ricettacolo
#: USB-C 0,514; chip 0603 esattamente 0.
NATIVE_ASYMMETRY_MIN_OFFSET = 0.05

#: Da che parte guarda ogni lato dichiarabile, nel frame del footprint
#: (X a destra, Y in basso), che e' anche quello in cui il modello descrive la
#: fotografia.
_LANDMARK_DIRECTIONS = {"right": (1, 0), "left": (-1, 0),
                        "bottom": (0, 1), "top": (0, -1)}


def _dominant_direction(dx, dy):
    """@brief Parte scale-invariante della regola: verso cardinale, o None.

    @param dx Scarto del baricentro lungo X, in una qualunque unita'.
    @param dy Scarto lungo Y, nella STESSA unita'.
    @return (dx, dy) cardinale, oppure None quando le due componenti si
        equivalgono (la feature sta in un angolo) o non c'e' scarto.
    @note Isolata perche' e' l'unica parte della decisione che non cambia se
        si moltiplicano entrambe le componenti per uno stesso fattore
        positivo. E' cio' che permette di leggerla sul baricentro del solido
        intero, che costa nulla, invece che su quello del residuo, che costa
        un boolean: i due scarti differiscono esattamente per il fattore
        volume_totale/volume_residuo (vedi `_cheap_landmark_verdict`).
    """
    major, minor = (abs(dx), abs(dy)) if abs(dx) >= abs(dy) else (abs(dy), abs(dx))
    if not major > 0:
        return None
    if major < NATIVE_ASYMMETRY_DOMINANCE * minor:
        return None
    return (1 if dx > 0 else -1, 0) if abs(dx) >= abs(dy) else (0, 1 if dy > 0 else -1)


def _asymmetry_direction(fraction, dx, dy):
    """@brief Traduci la misura dell'asimmetria in un lato, o in un "non lo so".

    @param fraction Volume che non sopravvive al mezzo giro, sul volume totale.
    @param dx Scarto del baricentro del residuo lungo X, sul semilato.
    @param dy Scarto lungo Y, sul semilato.
    @return (dx, dy) cardinale, oppure None quando la misura non attribuisce
        la feature a un lato solo.
    @note Separata dalla geometria apposta: qui non serve OpenCascade, e la
        regola che decide si puo' provare da sola.
    """
    if not (fraction >= NATIVE_ASYMMETRY_MIN_FRACTION):
        return None
    if max(abs(dx), abs(dy)) < NATIVE_ASYMMETRY_MIN_OFFSET:
        return None
    return _dominant_direction(dx, dy)


def _landmark_budget_s():
    """@brief Seconds a single orientation measurement may spend before giving up.

    @return Il budget in secondi, uguale all'intervallo del battito.
    @note NON e' un numero nuovo: e' l'intervallo fra due battiti, che a sua
        volta e' una frazione del timeout di silenzio del watchdog
        (`step_generator.FREECAD_SILENCE_TIMEOUT_S` tramite
        `process_log.heartbeat_interval`). Una sola manopola, quindi le due
        cose non possono divergere.
    @note Perche' proprio quell'intervallo: la sottrazione booleana e' una
        chiamata C++ che puo' non rilasciare il GIL, quindi mentre gira
        potrebbe non uscire NESSUN battito. L'intervallo del battito e' gia'
        la risposta del progetto alla domanda «quanto a lungo un passo puo'
        restare muto prima di dover dimostrare di essere vivo»: una misura
        che sta dentro quell'intervallo non puo', da sola, trasformare una
        corsa sana in un silenzio che il watchdog scambia per un blocco.
    @note EURISTICA DICHIARATA nella sola scelta della frazione: che il
        budget sia UN intervallo e non due o mezzo non deriva da una misura.
        Non e' tarato sul caso osservato -- il modulo che ha scatenato la
        correzione impiega minuti, quindi sfonda qualunque frazione sensata
        del timeout -- ed e' legato al watchdog, non al pezzo. Lo renderebbe
        superfluo una misura interrompibile: se OpenCascade offrisse un
        `BRepAlgoAPI` con progress/cancel, il limite sarebbe la rinuncia
        dell'utente e non un tempo deciso qui.
    """
    return _HEARTBEAT_INTERVAL_S


class _MeasurementExpired(Exception):
    """Il budget della misura e' finito: il verso resta non misurato."""


def _expired(deadline):
    """Vero quando il budget e' finito. deadline None = nessun limite."""
    return deadline is not None and time.monotonic() >= deadline


def _check_deadline(deadline):
    if _expired(deadline):
        raise _MeasurementExpired()


def _reference_frame(reference):
    """Centro e semilati del riferimento, cioe' l'unita' di misura degli scarti."""
    centre_x = (reference.XMin + reference.XMax) / 2
    centre_y = (reference.YMin + reference.YMax) / 2
    return (centre_x, centre_y,
            max(reference.XLength / 2, 1e-9), max(reference.YLength / 2, 1e-9))


#: Sentinella: la misura a costo zero non basta, serve il boolean.
_UNDECIDED = object()


def _cheap_landmark_verdict(shape, reference):
    """@brief Decidi il verso senza boolean, quando l'identita' lo consente.

    @param shape Solido (o compound) gia' letto dal file di libreria.
    @param reference Ingombro su cui e' stato scelto l'asse.
    @return Il verso cardinale, None, oppure `_UNDECIDED` quando la sola
        misura gratuita non basta a decidere.
    @note IDENTITA' ESATTA, non un'approssimazione. Sia A il solido, rho il
        mezzo giro attorno al centro del riferimento, C = A intersecato rhoA e
        R = A meno rhoA il residuo. C e' invariante per rho, quindi il suo
        baricentro sta sull'asse di rotazione; dalla somma dei momenti
        (A = C + R) segue
            (c_R - centro) = (V_A / V_R) * (c_A - centro).
        Lo scarto del residuo e' quindi lo scarto del solido INTERO
        moltiplicato per un fattore positivo: segno e dominanza fra i due assi
        -- cioe' tutto cio' che decide QUALE lato -- si leggono identici sul
        baricentro del solido intero, che FreeCAD calcola senza boolean.
    @note Restano due confronti che dipendono dalla scala, e con f = V_R/V_A
        e `major` lo scarto maggiore del solido intero si chiudono cosi':
        - da c_A - centro = f * (c_R - centro) e dal fatto che c_R cade dentro
          l'ingombro del solido segue f >= major / reach, dove `reach` e'
          quanto l'ingombro del solido si estende dal centro del riferimento,
          in semilati. Se major >= MIN_FRACTION * reach allora f e' sopra la
          soglia di frazione senza misurarla;
        - f <= 1 sempre, quindi se major >= MIN_OFFSET allora major / f e'
          sopra la soglia di scarto senza misurarla.
        Insieme: verso deciso a costo zero. Al contrario, se
        major < MIN_FRACTION * MIN_OFFSET nessun f puo' soddisfare entrambe le
        condizioni, e la risposta e' «non lo so» senza boolean. In mezzo resta
        la banda in cui f va misurato davvero.
    @note Una forma che non sa dire il proprio baricentro o il proprio
        ingombro (compound misti, e i solidi finti delle prove) non perde la
        misura: si torna al boolean, che e' cio' che accadeva prima.
    """
    try:
        centre_x, centre_y, half_x, half_y = _reference_frame(reference)
        centre = shape.CenterOfMass
        box = shape.BoundBox
        dx = (centre.x - centre_x) / half_x
        dy = (centre.y - centre_y) / half_y
        reach = max(abs(box.XMax - centre_x), abs(centre_x - box.XMin)) / half_x
        reach = max(reach, max(abs(box.YMax - centre_y),
                               abs(centre_y - box.YMin)) / half_y)
    except Exception:  # noqa: BLE001 - senza baricentro si paga il boolean
        return _UNDECIDED
    direction = _dominant_direction(dx, dy)
    if direction is None:
        return None
    major = max(abs(dx), abs(dy))
    if major < NATIVE_ASYMMETRY_MIN_FRACTION * NATIVE_ASYMMETRY_MIN_OFFSET:
        return None
    if (major >= NATIVE_ASYMMETRY_MIN_OFFSET
            and major >= NATIVE_ASYMMETRY_MIN_FRACTION * max(reach, 1e-9)):
        return direction
    return _UNDECIDED


def _boxes_overlap(first, second):
    """Due ingombri si toccano? Se no, i solidi non possono intersecarsi."""
    return (first.XMin <= second.XMax and second.XMin <= first.XMax
            and first.YMin <= second.YMax and second.YMin <= first.YMax
            and first.ZMin <= second.ZMax and second.ZMin <= first.ZMax)


def _residue_pieces(shape, flipped):
    """@brief Spezza la sottrazione in pezzi singolarmente interrompibili.

    @param shape Solido o compound originale.
    @param flipped La sua copia ruotata di mezzo giro.
    @return Lista di (solido, tagliente) con tagliente None quando il solido
        sopravvive intero, oppure None quando la forma non e' scomponibile.
    @note Due ragioni, e nessuna delle due e' un'approssimazione: (a) un solo
        `cut` su un assieme intero e' UNA chiamata C++ che Python non puo'
        interrompere, quindi nessun limite di tempo la potrebbe fermare,
        mentre una somma di sottrazioni per solido si controlla fra un pezzo e
        l'altro; (b) un solido il cui ingombro non tocca nessuna immagine
        speculare sopravvive per intero, e li' il boolean non si esegue
        affatto. Su un assieme dove i pezzi sono per lo piu' distanti fra loro
        questo toglie la maggior parte del lavoro.
    @note ASSUNZIONE DICHIARATA: i solidi dell'assieme non si compenetrano,
        altrimenti la somma dei volumi conta due volte le zone comuni. E'
        la stessa assunzione che `shape.Volume` di un compound fa gia' oggi,
        quindi il confronto con la soglia di frazione resta coerente.
    """
    try:
        solids = list(shape.Solids)
        mirrored = list(flipped.Solids)
    except Exception:  # noqa: BLE001 - forma non scomponibile
        return None
    if len(solids) < 2:
        return None
    pieces = []
    for solid in solids:
        try:
            box = solid.BoundBox
            cutters = [other for other in mirrored
                       if _boxes_overlap(box, other.BoundBox)]
        except Exception:  # noqa: BLE001 - senza ingombro non si pota
            return None
        pieces.append((solid, Part.makeCompound(cutters) if cutters else None))
    return pieces


def _half_turn_residue(shape, reference, deadline=None, heartbeat=None):
    """@brief Volume e baricentro di cio' che NON sopravvive a mezzo giro.

    @param shape Solido (o compound) gia' letto dal file di libreria.
    @param reference Ingombro su cui e' stato scelto l'asse.
    @param deadline Istante (time.monotonic) oltre il quale si rinuncia.
    @param heartbeat Battito da tenere vivo fra un pezzo e l'altro.
    @return (frazione di volume, scarto X, scarto Y) in semilati, o None.
    @throws _MeasurementExpired Quando il budget finisce a misura incompleta.
    """
    try:
        centre_x, centre_y, half_x, half_y = _reference_frame(reference)
    except Exception:  # noqa: BLE001 - riferimento inutilizzabile
        return None
    try:
        volume = shape.Volume
        flipped = shape.copy()
        flipped.rotate(V(centre_x, centre_y, 0), V(0, 0, 1), 180)
        pieces = _residue_pieces(shape, flipped)
        if pieces is None:
            # Un pezzo solo: la sottrazione e' indivisibile, quindi il budget
            # puo' soltanto impedire di INIZIARLA. Limite dichiarato: una
            # chiamata gia' partita arriva in fondo.
            _check_deadline(deadline)
            residual = shape.cut(flipped)
            total = residual.Volume
            centre = residual.CenterOfMass
            x, y = centre.x, centre.y
        else:
            total = moment_x = moment_y = 0.0
            for solid, cutter in pieces:
                if heartbeat is not None:
                    heartbeat.beat_if_due()
                _check_deadline(deadline)
                piece = solid if cutter is None else solid.cut(cutter)
                piece_volume = piece.Volume
                if piece_volume > 0:
                    centre = piece.CenterOfMass
                    total += piece_volume
                    moment_x += piece_volume * centre.x
                    moment_y += piece_volume * centre.y
            if not total > 0:
                return None
            x, y = moment_x / total, moment_y / total
    except _MeasurementExpired:
        raise
    except Exception:  # noqa: BLE001 - un boolean fallito non ferma il solido
        return None
    if not volume > 0:
        return None
    return total / volume, (x - centre_x) / half_x, (y - centre_y) / half_y


def _native_landmark_direction(shape, reference, deadline=None, heartbeat=None):
    """@brief Da che parte sta il materiale che distingue un capo dall'altro.

    @param shape Solido (o compound) gia' letto dal file di libreria.
    @param reference Ingombro usato per posarlo, cioe' il riferimento su cui e'
        stato scelto l'asse.
    @param deadline Istante (time.monotonic) oltre il quale si rinuncia.
    @param heartbeat Battito da tenere vivo durante la misura.
    @return (dx, dy) con una sola componente non nulla, oppure None quando il
        solido e' simmetrico, la feature sta in un angolo, o la misura e'
        costata piu' del budget.
    @throws _MeasurementExpired Quando il budget finisce a misura incompleta.
    @note L'asimmetria e', per definizione, cio' che NON sopravvive a mezzo
        giro: si ruota il solido di 180 gradi attorno al centro del suo
        ingombro, lo si sottrae da se' stesso, e il residuo e' esattamente il
        materiale che distingue i due capi. Il baricentro del solido INTERO non
        basta -- su un modulo schermato la massa simmetrica del PCB e dello
        schermo diluisce il connettore fino all'1,5%, sotto qualunque soglia
        onesta -- mentre il residuo di quello stesso modulo pesa il 15,9%.
        Criterio definitorio, non tarato: nessun nome di file, nessuna misura
        presa da un pezzo particolare.
    @note Il boolean si paga solo quando serve davvero: `_cheap_landmark_verdict`
        chiude i casi in cui l'identita' fra scarto del residuo e scarto del
        solido intero decide gia' da sola, e il chiamante replica la misura
        dalla cache quando quel file e' gia' stato misurato.
    """
    try:
        if not shape.Volume > 0:
            return None
    except Exception:  # noqa: BLE001 - un solido che non sa dire il volume
        return None
    verdict = _cheap_landmark_verdict(shape, reference)
    if verdict is not _UNDECIDED:
        return verdict
    measured = _half_turn_residue(shape, reference, deadline, heartbeat)
    if measured is None:
        return None
    return _asymmetry_direction(*measured)


#: Nome dell'indice delle misure di verso. File separato da quello degli
#: ingombri (`kicad_models.INDEX_FILENAME`) perche' le due misure hanno vite
#: diverse: l'ingombro lo misura il processo padre durante l'abbinamento, il
#: verso lo misura FreeCAD durante la costruzione. Stesso meccanismo di
#: scrittura atomica e stessa ricetta di chiave.
LANDMARK_INDEX_FILENAME = "kicadgen-landmark-index.json"

#: Sentinella: la cache non ha nulla da dire su questo file.
_LANDMARK_MISS = object()


def _landmark_cache_paths():
    """Dove vive l'indice dei versi gia' misurati.

    Nella cache dell'utente e non accanto al modello: la libreria 3D di KiCad
    e' spesso installata di sistema e sola lettura, e un file di servizio per
    cartella la sporcherebbe. La chiave porta identita' del file e
    riferimento, quindi un solo indice condiviso non puo' confondere due
    modelli.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return [os.path.join(base, "kicadgen", LANDMARK_INDEX_FILENAME)]


def _landmark_store():
    """Il magazzino delle misure, o None se non e' raggiungibile.

    Lo stesso modulo serve il processo padre (come pacchetto) e l'interprete
    embedded di FreeCAD (come modulo accanto a questo), come gia' fa
    `lead_geometry`: senza la cache la misura si rifa', non si rompe.
    """
    try:
        try:
            from kicadgen.cad3d import model_size_cache
        except ImportError:
            import model_size_cache
        return model_size_cache
    except Exception:  # noqa: BLE001 - senza cache si rimisura
        return None


def _landmark_reference_signature(reference):
    """Il riferimento fa parte della domanda, quindi fa parte della chiave.

    Il mezzo giro e' attorno al CENTRO del riferimento: misurare sull'ingombro
    totale o sulla sezione del corpo sono due domande diverse con due risposte
    diverse, e una non deve mai essere replayata al posto dell'altra.
    """
    return ",".join(f"{float(value):.6f}" for value in
                    (reference.XMin, reference.XMax, reference.YMin, reference.YMax))


def _landmark_cache_entry(path, reference):
    """(chiave, identita' del file, firma del riferimento), o None."""
    store = _landmark_store()
    if store is None:
        return None
    try:
        identity = store.file_identity(path)
        signature = _landmark_reference_signature(reference)
    except Exception:  # noqa: BLE001 - senza identita' non si replica nulla
        return None
    if not identity:
        return None
    return f"{identity}|{signature}", identity, signature


def _landmark_direction_value(value):
    """Verso letto dalla cache, o `_LANDMARK_MISS` se non e' un verso valido."""
    if value is None:
        return None
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(item in (-1, 0, 1) and not isinstance(item, bool) for item in value)
            and sum(1 for item in value if item) == 1):
        return (int(value[0]), int(value[1]))
    return _LANDMARK_MISS


def _cached_landmark(entry, budget):
    """@brief Replay a landmark already measured for THIS file and reference.

    @param entry Tripla di `_landmark_cache_entry`.
    @param budget Budget della misura che si starebbe per fare, in secondi.
    @return (verso,) quando la cache risponde, `_LANDMARK_MISS` altrimenti.
    @note Il record porta identita' e riferimento anche al proprio interno, non
        solo nella chiave: cosi' un indice corrotto o fuso male non puo' far
        ereditare a un modello la misura di un altro.
    @note Una rinuncia per budget si ricorda con il budget che aveva: stesso
        file e budget non maggiore, non vale la pena ripagare minuti per
        arrivare alla stessa rinuncia; un budget piu' generoso ritenta.
    """
    store = _landmark_store()
    if store is None or entry is None:
        return _LANDMARK_MISS
    key, identity, signature = entry
    try:
        record = store.load(_landmark_cache_paths()).get(key)
    except Exception:  # noqa: BLE001 - cache illeggibile: si rimisura
        return _LANDMARK_MISS
    if not isinstance(record, dict):
        return _LANDMARK_MISS
    if record.get("model") != identity or record.get("reference") != signature:
        return _LANDMARK_MISS
    status = record.get("status")
    if status == "measured":
        direction = _landmark_direction_value(record.get("direction"))
        return _LANDMARK_MISS if direction is _LANDMARK_MISS else (direction,)
    if status == "budget_exceeded":
        try:
            spent = float(record.get("budget_s"))
        except (TypeError, ValueError):
            return _LANDMARK_MISS
        if math.isfinite(spent) and budget <= spent:
            return (None,)
    return _LANDMARK_MISS


def _store_landmark(entry, direction, status, budget):
    """Registra la misura (o la rinuncia) come misura DI QUEL file."""
    store = _landmark_store()
    if store is None or entry is None:
        return
    key, identity, signature = entry
    try:
        store.save({key: {
            "model": identity,
            "reference": signature,
            "status": status,
            "direction": list(direction) if direction else None,
            "budget_s": float(budget),
        }}, _landmark_cache_paths())
    except Exception:  # noqa: BLE001 - una cache non scritta non e' un guasto
        pass


def landmark_budget_line(model_name, seconds):
    """@brief Compose the line that says the verse was NOT measured, and why.

    @param model_name File name of the model (never a full path).
    @param seconds Budget the measurement was given, in seconds.
    @return An Italian sentence, ready to print on the child's stdout.
    @note Stessa forma della riga del battito e per la stessa ragione: deve
        arrivare sia nel log sia nella modale di avanzamento. Un marcatore
        `KICADGEN_*` finirebbe nel solo log (analysis_overlay.is_machine_line),
        e l'utente non saprebbe mai perche' quel componente e' rimasto
        orientato dal solo ingombro.
    """
    return (f"Verso del modello 3D {model_name} non misurato entro "
            f"{_elapsed_phrase(seconds)}, il componente resta orientato "
            f"secondo il solo ingombro.")


def _cached_landmark_direction(shape, reference, path, heartbeat=None):
    """@brief Measure the verse once per model file, and never longer than the budget.

    @param shape Solido (o compound) gia' letto dal file di libreria.
    @param reference Ingombro su cui e' stato scelto l'asse.
    @param path Percorso del file di libreria: identifica la misura.
    @param heartbeat Battito da tenere vivo durante la misura.
    @return Il verso cardinale, oppure None quando non si puo' attribuire.
    @note La cache si consulta PRIMA del lavoro caro, non dopo: il verso di un
        file e' deterministico, quindi si paga una volta sola. La chiave e'
        percorso + dimensione + data del file (piu' il riferimento), quindi un
        file riscritto si rimisura da solo.
    @note Sforare il budget non e' un guasto: e' l'esito «non lo so» che
        `_verse_correction` gia' prevede, quindi la generazione prosegue con il
        componente orientato dal solo ingombro.
    """
    budget = _landmark_budget_s()
    entry = _landmark_cache_entry(path, reference)
    cached = _cached_landmark(entry, budget)
    if cached is not _LANDMARK_MISS:
        return cached[0]
    try:
        direction = _native_landmark_direction(
            shape, reference, time.monotonic() + budget, heartbeat)
    except _MeasurementExpired:
        print(landmark_budget_line(_model_file_name(path), budget), flush=True)
        _store_landmark(entry, None, "budget_exceeded", budget)
        return None
    _store_landmark(entry, direction, "measured", budget)
    return direction


def _verse_correction(native_direction, inferred, observed_side):
    """@brief Rotazione che porta il riferimento nativo dove lo si e' osservato.

    @param native_direction (dx, dy) del baricentro nel frame del modello, o None.
    @param inferred Angolo dell'asse gia' scelto sull'ingombro, 0 o 90 gradi.
    @param observed_side Lato dichiarato dall'osservazione: left/right/top/bottom.
    @return 0 o 180 gradi da sommare, oppure None se non si puo' stabilire.
    @note Si cercano solo 0 e 180 di proposito: l'asse l'ha gia' deciso la
        misura dell'ingombro, e una correzione di 90 lo smentirebbe. Se ne' 0
        ne' 180 portano il riferimento dalla parte osservata, i due dati si
        contraddicono e non si corregge nulla: meglio un verso non verificato
        che uno inventato.
    """
    wanted = _LANDMARK_DIRECTIONS.get(observed_side)
    if native_direction is None or wanted is None:
        return None
    # Il costruttore lavora con Y verso l'alto, l'osservazione con Y verso il
    # basso: senza questo ribaltamento "sopra" e "sotto" si scambierebbero.
    wanted = (wanted[0], -wanted[1])
    for extra in (0, 180):
        if _rotated(native_direction, inferred + extra) == wanted:
            return extra
    return None


def _rotated(direction, angle):
    """Ruota una direzione cardinale di un multiplo di 90 gradi."""
    dx, dy = direction
    for _ in range(int(round(angle / 90)) % 4):
        dx, dy = -dy, dx
    return (dx, dy)


def _placement_angle(component, inferred):
    """@brief Compose the inferred axis with the corrections that apply to it.

    @param component Component mapping carrying the optional overrides.
    @param inferred Axis angle deduced by comparing the model with the measure.
    @return Total rotation around Z, in degrees.
    @note Only an explicit user selection is absolute: it names a model AND the
        angle to place it at, so it replaces the deduction. The other two are
        deltas and add to it -- the automatic one corrects the VERSE of an
        asymmetric feature, the manual one is the 3D context menu. Writing the
        automatic correction as an absolute angle used to erase the axis: a
        module whose long axis needed 90 degrees ended up at 180, keeping the
        wrong axis, and no value between 0 and 180 could put it right.
    """
    return (float(component.get("model_rotation_deg", inferred))
            + float(component.get("model_rotation_delta_deg") or 0))


def _library_model(component, cx, cy, z0, color):
    """Import resolved STEP geometry and styles, or return None on failure.

    Native Z=0 stays at the PCB datum, including pins below it. Local
    model_rotation_deg overrides inferred body/total axis alignment; the caller
    then rotates the component once. Side lengths cannot determine 180-degree
    polarity or square-model orientation.

    Il percorso arriva gia' risolto e verificato contro l'ingombro misurato:
    qui si legge il file e lo si posa al posto giusto. Il modello e'
    orientato secondo la convenzione del suo footprint, quindi si centra in
    pianta sul componente e si appoggia con la base sulla scheda; la
    rotazione la applica il chiamante, come per tutte le altre forme.

    Un file illeggibile non ferma la generazione: si torna alla forma
    sintetica, che e' approssimata ma sempre disponibile.
    """
    path = component.get("model_path")
    if not path:
        return None
    print(f"KICADGEN_IMPORT_START:{path}", flush=True)
    try:
        # Il battito copre TUTTO il tratto muto: la lettura del file (sia la
        # via con i colori nativi sia il ripiego Part.read) e l'analisi
        # geometrica che segue, dove uno slice e un boolean su un assieme
        # grande costano anche loro minuti senza stampare nulla. Esce dal
        # `with` -- quindi smette -- sia quando l'import finisce sia quando
        # solleva, prima che venga stampato il marcatore di esito.
        with _ImportHeartbeat(path) as heartbeat:
            try:
                parts = _read_library_parts(path, color, heartbeat)
            except Exception as exc:
                _warn("colori nativi {path} non disponibili ({exc}); si conserva la "
                   "geometria con colore fotografico",
                   path=path,
                   exc=exc)
                shape = Part.read(str(path))
                if shape.isNull() or not shape.Solids or not shape.isValid():
                    raise ValueError("library model has no valid solids")
                parts = [(shape, _ModelColor(color, None, path))]
            heartbeat.beat_if_due()
            combined = Part.makeCompound([shape for shape, _ in parts])
            box = combined.BoundBox
            # Compare unsorted axes of both supported observations. A lead
            # envelope can have a different long axis from the plastic body
            # (e.g. SOT-223).
            references = [box]
            try:
                # Lo slice sta sullo stesso tratto muto e su un assieme grande
                # costa anch'esso: NON ha pero' un budget come la misura del
                # verso, e la differenza non e' una svista. Il verso ha un
                # esito «non lo so» gia' previsto (il componente resta
                # orientato dall'ingombro, com'era prima che la misura
                # esistesse), mentre l'asse no: rinunciarvi poserebbe il
                # modello ruotato di 90 gradi, che e' un errore vero e non una
                # rinuncia. Bilanciarli allo stesso modo richiederebbe un
                # secondo esito onesto per l'asse, che oggi non c'e'.
                section = combined.slice(V(0, 0, 1), box.ZMin + box.ZLength * 0.65)
                if section:
                    references.append(Part.makeCompound(section).BoundBox)
            except Exception:
                pass
            length, width = float(component["length_mm"]), float(component["width_mm"])
            inferred, reference = _native_axis_angle(references, length, width)
            # Il verso non si ricava dall'ingombro: due capi di un rettangolo
            # misurano uguale. Si confronta il materiale asimmetrico del
            # modello con il lato su cui la fotografia ha visto il riferimento.
            # La misura puo' costare un boolean, quindi si fa solo se c'e' un
            # lato osservato: senza, non ci sarebbe nulla con cui confrontarla.
            # Il verso di un file e' deterministico: si misura una volta, con
            # un budget, e le corse successive lo rileggono dalla cache.
            observed_side = component.get("landmark_side")
            heartbeat.beat_if_due()
            verse = _verse_correction(
                _cached_landmark_direction(combined, reference, path, heartbeat)
                if observed_side else None,
                inferred, observed_side)
            if verse:
                print(f"KICADGEN_VERSE:{path}; landmark="
                      f"{component.get('landmark_feature')} on "
                      f"{component.get('landmark_side')}; "
                      f"rotated {verse:g} deg over the inferred axis", flush=True)
            inferred += verse or 0
            # La correzione manuale e' un DELTA, non un angolo assoluto:
            # l'angolo di partenza lo deduce qui FreeCAD confrontando il
            # modello con la misura, e l'interfaccia non lo conosce. "Ruota di
            # 90 gradi" si somma quindi a qualunque sia la base, dedotta o gia'
            # forzata.
            angle = _placement_angle(component, inferred)
            dz = float(component.get("model_z_offset_mm", 0))
            if not math.isfinite(angle) or not math.isfinite(dz):
                raise ValueError("non-finite library placement")
            centre = V((reference.XMin + reference.XMax) / 2,
                       (reference.YMin + reference.YMax) / 2, 0)
            for shape, _ in parts:
                shape.translate(-centre)
                shape.rotate(V(0, 0, 0), V(0, 0, 1), angle)
                # KiCad models use Z=0 as the PCB seating plane. Do not lift
                # through-hole pins or board locks by aligning their lowest point.
                shape.translate(V(cx, cy, z0 + dz))
        print(f"KICADGEN_IMPORT_OK:{path}; parts={len(parts)}; "
              f"local_rotation_deg={angle:g}", flush=True)
        return parts
    except Exception as exc:  # noqa: BLE001 - si ripiega sulla forma sintetica
        component["_model_import_error"] = _safe_import_error(exc)
        _warn("modello {path} non caricabile ({exc}); si usa la forma approssimata",
           path=path,
           exc=exc)
        return None


def _safe_import_error(exc):
    """Keep import diagnostics bounded, printable and JSON-safe without a traceback."""
    try:
        message = str(exc)
    except Exception:
        message = "Unable to read exception message"
    return (type(exc).__name__ + ": " + " ".join(
        "".join(c if c.isprintable() else " " for c in message).split()))[:500]


def build_component(component, z0):
    """@brief Build shapes, skipping invalid envelopes without raising.

    @param component Component payload with local dimensions in mm, CAD centre,
        optional mating observation and resolved STEP path. model_rotation_deg
        is a local CAD Z rotation before the caller's component rotation.
    @param z0 PCB seating plane in mm; native model Z=0 is placed here.
    @return List of (shape, hex color); imported colors also carry face styles.
    @note Failed model imports mark _model_import_failed and _model_import_error
        on the payload and produce a magenta approximation. Retry clears old markers.

    Optional ``terminal_geometry`` overrides kind/count-based approximations:
    ``{body_bbox: [l,t,r,b], terminals: [{bbox: [l,t,r,b]}],
    confidence: 'high'|'medium'}``. Boxes are normalized to the total local
    length/width envelope, X-right/Y-down, before the caller's rotation.
    Missing/None observations retain legacy behavior. Invalid non-None
    observations produce a warning and a generic envelope box.
    """
    component.pop("_model_import_failed", None)
    component.pop("_model_import_error", None)
    kind = str(component.get("kind") or "generic")
    builder = BUILDERS.get(kind, _generic)
    color = component.get("color_hex") or "#303030"
    try:
        values = [component[key] for key in ("length_mm", "width_mm", "height_mm", "x", "y")]
        values += [z0, component.get("rotation_deg") or 0]
        if any(isinstance(value, bool) for value in values):
            raise ValueError("boolean geometry value")
        length, width, height, cx, cy, z0, rotation = map(float, values)
        if not all(math.isfinite(value) for value in (length, width, height, cx, cy, z0, rotation)):
            raise ValueError("non-finite geometry value")
        if min(length, width, height) <= 0:
            raise ValueError("dimensions must be positive")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        _warn("componente {get} skipped: invalid geometry ({exc})",
           get=component.get('name'),
           exc=exc)
        return []
    try:
        # Modello reale della libreria KiCad, gia' risolto e verificato dal
        # chiamante: e' geometria vera, batte qualunque forma approssimata.
        try:
            real = _library_model(component, cx, cy, z0, color)
        except Exception as exc:
            if not component.get("model_path"):
                raise
            component["_model_import_error"] = _safe_import_error(exc)
            real = None
        if real is not None:
            return real
        if component.get("model_path"):
            component["_model_import_failed"] = True
            component.setdefault("_model_import_error", "Model import produced no usable solids")
            color = component["color_hex"] = "#FF00FF"
            return _generic(cx, cy, z0, length, width, height, color)
        if component.get("terminal_geometry") is not None:
            return _observed_terminals(cx, cy, z0, length, width, height, color,
                                       component["terminal_geometry"])
        if kind == "ic":
            _warn("componente {get} (ic): package sconosciuto, geometria generica senza "
               "terminali",
               get=component.get('name'))
            return _generic(cx, cy, z0, length, width, height, color)
        if kind == "connector" or (kind == "generic" and (
                component.get("_detected_kind") == "connector"
                or component.get("mating") in ("edge", "top"))):
            if component.get("mating") in ("edge", "top"):
                _warn("componente {get}: mating osservato; dettagli connettore approssimati",
                   get=component.get('name'))
            # Unico builder il cui orientamento dipende da dove si trova il
            # componente: la bocca deve guardare fuori scheda DOPO la
            # rotazione applicata dal chiamante, non prima.
            return _connector(cx, cy, z0, length, width, height, color,
                              rotation_deg=float(component.get("rotation_deg") or 0),
                              mating=component.get("mating"))
        if kind in BUILDERS and kind != "generic":
            _warn("componente {get} ({kind}): geometria approssimata, terminal_geometry "
               "assente",
               get=component.get('name'),
               kind=kind)
        pin_count = component.get("pin_count")
        pins_by_side = component.get("pins_by_side")
        if kind in {"ic_soic", "ic_qfn"} and (pin_count or pins_by_side):
            return _ic_observed_pins(cx, cy, z0, length, width, height, color, kind,
                                     pin_count=pin_count, pins_by_side=pins_by_side)
        return builder(cx, cy, z0, length, width, height, color)
    except Exception as exc:  # noqa: BLE001 - si ripiega sulla scatola
        _warn("componente {get} ({kind}) semplificato: {exc}",
           get=component.get('name'),
           kind=kind,
           exc=exc)
        try:
            return _generic(cx, cy, z0, length, width, height, color)
        except Exception as fallback_exc:
            _warn("componente {get} skipped: generic fallback failed ({fallback_exc})",
               get=component.get('name'),
               fallback_exc=fallback_exc)
            return []
