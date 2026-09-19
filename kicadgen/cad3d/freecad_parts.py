"""Forme 3D dei componenti montati su un modulo -- eseguito DENTRO freecadcmd.

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

import math

import FreeCAD
import Part

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
    return [
        e for e in shape.Edges
        if e.BoundBox.ZLength > max(e.BoundBox.XLength, e.BoundBox.YLength) + 1e-6
    ]


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
                print(f'KICADGEN_WARN: dettaglio reoforo ignorato ({exc}); terminale originale conservato')
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


def _connector(cx, cy, z0, length, width, height, color):
    """Guscio di connettore con l'apertura sulla faccia di accoppiamento.

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
    shell = _box(cx, cy, z0, length, width, height)
    wall = max(0.2, min(length, width, height) * 0.15)
    # Un package troppo piccolo per avere pareti resta pieno: meglio una
    # scatola onesta di un guscio degenere con facce che si compenetrano.
    if min(length, width, height) <= 2.5 * wall:
        return [(shell, color)]
    if length <= width:
        sign = -1.0 if cx < 0 else 1.0
        cavity = _box(cx + sign * wall, cy, z0 + wall,
                      length, width - 2 * wall, height - 2 * wall)
    else:
        sign = -1.0 if cy < 0 else 1.0
        cavity = _box(cx, cy + sign * wall, z0 + wall,
                      length - 2 * wall, width, height - 2 * wall)
    try:
        shell = shell.cut(cavity)
    except Exception:  # noqa: BLE001
        pass
    return [(shell, color)]


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
    "crystal": _crystal,
}

#: Elenco esposto allo schema di estrazione (kicadgen.services) -- tenerlo
#: allineato a BUILDERS; e' duplicato la' perche' questo modulo non e'
#: importabile fuori da FreeCAD.
KINDS = tuple(BUILDERS)


def build_component(component, z0):
    """Costruisce le parti di un componente descritto dal payload JSON.

    Optional ``terminal_geometry`` overrides kind/count-based approximations:
    ``{body_bbox: [l,t,r,b], terminals: [{bbox: [l,t,r,b]}],
    confidence: 'high'|'medium'}``. Boxes are normalized to the total local
    length/width envelope, X-right/Y-down, before the caller's rotation.
    Missing/None observations retain legacy behavior. Invalid non-None
    observations produce a warning and a generic envelope box.
    """
    kind = str(component.get("kind") or "generic")
    builder = BUILDERS.get(kind, _generic)
    color = component.get("color_hex") or "#303030"
    length = float(component["length_mm"])
    width = float(component["width_mm"])
    height = float(component["height_mm"])
    cx = float(component["x"])
    cy = float(component["y"])
    try:
        if component.get("terminal_geometry") is not None:
            return _observed_terminals(cx, cy, z0, length, width, height, color,
                                       component["terminal_geometry"])
        if kind == "ic":
            print(f"KICADGEN_WARN:componente {component.get('name')} (ic): "
                  "package sconosciuto, geometria generica senza terminali")
            return _generic(cx, cy, z0, length, width, height, color)
        if kind in BUILDERS and kind != "generic":
            print(f"KICADGEN_WARN:componente {component.get('name')} ({kind}): "
                  "geometria approssimata, terminal_geometry assente")
        pin_count = component.get("pin_count")
        pins_by_side = component.get("pins_by_side")
        if kind in {"ic_soic", "ic_qfn"} and (pin_count or pins_by_side):
            return _ic_observed_pins(cx, cy, z0, length, width, height, color, kind,
                                     pin_count=pin_count, pins_by_side=pins_by_side)
        return builder(cx, cy, z0, length, width, height, color)
    except Exception as exc:  # noqa: BLE001 - si ripiega sulla scatola
        print(f"KICADGEN_WARN:componente {component.get('name')} ({kind}) semplificato: {exc}")
        return _generic(cx, cy, z0, length, width, height, color)
