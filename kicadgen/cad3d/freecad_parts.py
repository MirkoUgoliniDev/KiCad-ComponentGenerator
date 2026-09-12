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


def _connector(cx, cy, z0, length, width, height, color):
    shell = _box(cx, cy, z0, length, width, height)
    cavity = _box(cx, cy, z0 + height * 0.5, length * 0.8, width * 0.7, height)
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
    "connector": _connector,
    "crystal": _crystal,
}

#: Elenco esposto allo schema di estrazione (kicadgen.services) -- tenerlo
#: allineato a BUILDERS; e' duplicato la' perche' questo modulo non e'
#: importabile fuori da FreeCAD.
KINDS = tuple(BUILDERS)


def build_component(component, z0):
    """Costruisce le parti di un componente descritto dal payload JSON."""
    kind = str(component.get("kind") or "generic")
    builder = BUILDERS.get(kind, _generic)
    color = component.get("color_hex") or "#303030"
    length = float(component["length_mm"])
    width = float(component["width_mm"])
    height = float(component["height_mm"])
    cx = float(component["x"])
    cy = float(component["y"])
    try:
        return builder(cx, cy, z0, length, width, height, color)
    except Exception as exc:  # noqa: BLE001 - si ripiega sulla scatola
        print(f"KICADGEN_WARN:componente {component.get('name')} ({kind}) semplificato: {exc}")
        return _generic(cx, cy, z0, length, width, height, color)
