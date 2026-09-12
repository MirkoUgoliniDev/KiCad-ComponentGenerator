"""Script eseguito DENTRO l'interprete Python embedded di FreeCAD (freecadcmd).

Non e' pensato per essere importato da codice normale: gira solo tramite
`freecadcmd freecad_build_body.py <percorso_json>` (vedi step_generator.py),
dove FreeCAD/Part/Draft sono moduli forniti dall'ambiente FreeCAD stesso.

Legge un file JSON con corpo (parallelepipedo), pin (cilindri) e le
annotazioni di post-elaborazione (fillet su spigoli, testo inciso su
facce -- vedi kicadgen.cad3d.annotations per i nomi ammessi), costruisce
il documento ed esporta STEP (sempre) e VRML (best-effort, non fatale se
fallisce).

Classificazione geometrica di facce/spigoli: il corpo e' sempre un
parallelepipedo assi-allineato (offset_x/y, length/width/height), quindi
si classificano confrontando le coordinate del centro di massa (facce) o
del punto medio (spigoli) con i bordi noti del box, entro una tolleranza.
"""

import json
import sys

import FreeCAD
import Part

try:
    import Draft
except ImportError:
    Draft = None

try:
    import Mesh
except ImportError:
    Mesh = None

TOL = 1e-3


def _face_name(face, x0, y0, x1, y1, z1):
    """Classifica una faccia planare del box in uno dei FACE_NAMES.

    Ritorna None per le facce non classificabili: dopo un fillet il solido
    contiene anche superfici cilindriche/toroidali di raccordo, che non
    corrispondono a nessuna delle 6 facce nominali del parallelepipedo.
    """
    try:
        normal = face.normalAt(0, 0)
        com = face.CenterOfMass
    except Exception:  # noqa: BLE001 - superficie non valutabile: non classificabile
        return None
    if abs(normal.z) > 0.9:
        return "top" if com.z > z1 / 2 else "bottom"
    if abs(normal.x) > 0.9:
        return "left" if com.x < (x0 + x1) / 2 else "right"
    if abs(normal.y) > 0.9:
        return "front" if com.y < (y0 + y1) / 2 else "back"
    return None


def _edge_name(edge, x0, y0, x1, y1, z1):
    """Classifica uno spigolo del box in uno degli EDGE_NAMES (vedi annotations.py).

    Ritorna None per gli spigoli non classificabili: gli archi generati da un
    fillet sono curve chiuse con un solo vertice, quindi non hanno i due
    estremi distinti su cui si basa la classificazione per direzione.
    """
    if len(edge.Vertexes) < 2:
        return None
    v1, v2 = edge.Vertexes[0].Point, edge.Vertexes[1].Point
    dx, dy, dz = abs(v1.x - v2.x), abs(v1.y - v2.y), abs(v1.z - v2.z)
    midx, midy, midz = (v1.x + v2.x) / 2, (v1.y + v2.y) / 2, (v1.z + v2.z) / 2

    if dz >= dx and dz >= dy:
        side1 = "front" if midy < (y0 + y1) / 2 else "back"
        side2 = "left" if midx < (x0 + x1) / 2 else "right"
        return f"{side1}-{side2}"
    if dx >= dy and dx >= dz:
        topbot = "top" if midz > z1 / 2 else "bottom"
        fb = "front" if midy < (y0 + y1) / 2 else "back"
        return f"{topbot}-{fb}"
    topbot = "top" if midz > z1 / 2 else "bottom"
    lr = "left" if midx < (x0 + x1) / 2 else "right"
    return f"{topbot}-{lr}"


def _apply_fillets(shape, fillets, bounds):
    """Applica i fillet raggruppandoli per raggio: gli spigoli con lo stesso
    raggio finiscono in un'unica makeFillet, cosi' vengono raccordati tutti
    sulla geometria ancora intatta (applicarli uno alla volta puo' fallire
    quando due spigoli adiacenti si incontrano in uno stesso vertice).

    Ogni gruppo e' isolato in try/except: un fillet impossibile (raggio
    troppo grande per la geometria) viene segnalato ma non fa fallire
    l'intera generazione dello STEP.
    """
    x0, y0, x1, y1, z1 = bounds

    by_radius = {}
    for f in fillets:
        by_radius.setdefault(f["radius_mm"], []).append(f["edge"])

    for radius, edge_names in by_radius.items():
        wanted = set(edge_names)
        target_edges = [
            e for e in shape.Edges
            if _edge_name(e, x0, y0, x1, y1, z1) in wanted
        ]
        missing = wanted - {
            _edge_name(e, x0, y0, x1, y1, z1) for e in shape.Edges
        }
        for name in sorted(missing):
            print(f"KICADGEN_WARN: spigolo '{name}' non trovato sulla geometria corrente")
        if not target_edges:
            continue
        try:
            shape = shape.makeFillet(radius, target_edges)
        except Exception as exc:  # noqa: BLE001 - segnalato, non fatale
            print(f"KICADGEN_WARN: fillet r={radius} su {sorted(wanted)} fallito: {exc}")
    return shape


def _build_face_text_solids(shape, texts, bounds, font_file):
    """Costruisce le scritte come solidi sottili APPOGGIATI sulle facce.

    Non incide il corpo: ogni scritta e' uno strato di "vernice" a se' stante
    (come una serigrafia), restituito separatamente cosi' da poter avere un
    colore proprio nell'export. Una minima parte dello spessore viene annegata
    nel corpo per evitare z-fighting tra le due superfici a contatto.
    """
    x0, y0, x1, y1, z1 = bounds
    solids = []
    for t in texts:
        matching = [f for f in shape.Faces if _face_name(f, x0, y0, x1, y1, z1) == t["face"]]
        if not matching:
            print(f"KICADGEN_WARN: faccia '{t['face']}' non trovata per il testo")
            continue
        face = max(matching, key=lambda f: f.Area)
        normal = face.normalAt(0, 0)
        center = face.CenterOfMass

        thickness = t["thickness_mm"]
        sink = min(thickness * 0.3, 0.02)

        wires = Part.makeWireString(t["text"], font_file, t["size_mm"], 0.0)
        text_face = Part.makeCompound([Part.Face(w) for w in wires])
        solid = text_face.extrude(FreeCAD.Vector(0, 0, thickness + sink))

        # Centra il blocco di testo sull'origine XY, con la base a Z=0.
        text_bbox = solid.BoundBox
        solid.translate(FreeCAD.Vector(
            -(text_bbox.XMin + text_bbox.XMax) / 2,
            -(text_bbox.YMin + text_bbox.YMax) / 2,
            -text_bbox.ZMin,
        ))

        # Rotazione + posizionamento sulla faccia vanno COMPOSTI con la
        # centratura appena fatta: assegnare direttamente .Placement la
        # sovrascriverebbe, lasciando la scritta fuori centro (sbordava dal corpo).
        target = FreeCAD.Placement(
            center - normal * sink,
            FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), normal),
        )
        solid.Placement = target.multiply(solid.Placement)

        solids.append((solid, t.get("color_hex", "#ffffff")))
    return solids


def _hex_to_rgb(color):
    """'#rrggbb' -> (r, g, b) in 0..1, con fallback grigio se il valore e' sporco."""
    try:
        h = color.lstrip("#")
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except Exception:  # noqa: BLE001 - colore non valido: grigio neutro
        return (0.5, 0.5, 0.5)


# KiCad interpreta i .wrl in unita' da 0.1 pollici, non in millimetri: le
# coordinate vanno divise per 2.54 o il modello appare 2.54 volte piu' grande
# nel viewer 3D. Lo .step resta invece in mm.
_VRML_SCALE = 1.0 / 25.4 * 10.0


def _write_vrml(path, tessellated):
    """Scrive un VRML97 con un Shape colorato per ogni pezzo (corpo, serigrafie, pin).

    Mesh.export() di FreeCAD scriverebbe tutto con un unico grigio di default:
    i colori si perdono perche' in modalita' headless non esistono i ViewObject
    da cui l'export prende l'aspetto. Qui il file lo scriviamo direttamente.
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#VRML V2.0 utf8\n")
        fh.write("# Generato da KiCadGen-Tool (coordinate in unita' 0.1 inch, come da convenzione KiCad)\n\n")
        for obj, color, (verts, facets) in tessellated:
            r, g, b = _hex_to_rgb(color)
            fh.write(f"# {obj.Name}\nShape {{\n")
            fh.write("  appearance Appearance { material Material { ")
            fh.write(f"diffuseColor {r:.3f} {g:.3f} {b:.3f} specularColor 0.1 0.1 0.1 shininess 0.3 ")
            fh.write("} }\n")
            fh.write("  geometry IndexedFaceSet {\n    coord Coordinate { point [\n")
            for p in verts:
                fh.write(
                    f"      {p.x * _VRML_SCALE:.5f} {p.y * _VRML_SCALE:.5f} {p.z * _VRML_SCALE:.5f},\n"
                )
            fh.write("    ] }\n    coordIndex [\n")
            for a, b_, c in facets:
                fh.write(f"      {a},{b_},{c},-1,\n")
            fh.write("    ]\n  }\n}\n\n")


def _slab(outline, z_from, z_to, bounds):
    """Fetta orizzontale del prisma del PCB, tra due quote Z.

    Substrato e soldermask sono lo STESSO contorno (raccordi e semi-fori
    compresi) tagliato a quote diverse: cosi' i tre strati combaciano sempre.
    """
    x0, y0, x1, y1, _ = bounds
    clip = Part.makeBox(x1 - x0 + 4, y1 - y0 + 4, z_to - z_from,
                        FreeCAD.Vector(x0 - 2, y0 - 2, z_from))
    return outline.common(clip)


def _text_solid(text, size, thickness, font_file, bold_font=None):
    """Scritta come compound di solidi sottili, centrata sull'origine XY, base a Z=0."""
    wires = None
    for font in (bold_font, font_file):
        if not font:
            continue
        try:
            wires = Part.makeWireString(text, font, size, 0.0)
            break
        except Exception:  # noqa: BLE001 - font mancante: si prova il successivo
            continue
    if wires is None:
        return None
    solids = []
    for glyph in wires:
        if not glyph:
            continue
        try:
            face = Part.makeFace(glyph, "Part::FaceMakerBullseye")
        except Exception:  # noqa: BLE001 - glifo non chiudibile: si salta
            continue
        solids.append(face.extrude(FreeCAD.Vector(0, 0, thickness)))
    if not solids:
        return None
    shape = Part.makeCompound(solids)
    bb = shape.BoundBox
    shape.translate(FreeCAD.Vector(-(bb.XMin + bb.XMax) / 2, -(bb.YMin + bb.YMax) / 2, -bb.ZMin))
    return shape


def _place_text(text, size, x, y, z, angle, font_file, thickness=0.012):
    bold = None
    if font_file.lower().endswith("arial.ttf"):
        bold = font_file[:-len("arial.ttf")] + "arialbd.ttf"
    shape = _text_solid(text, size, thickness, font_file, bold)
    if shape is None:
        return None
    if angle:
        shape.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), angle)
    shape.translate(FreeCAD.Vector(x, y, z))
    return shape


def _rim(cx, cy, length, width, z, line=0.12, thickness=0.008):
    """Cornice serigrafica rettangolare (contorno di un componente o del modulo)."""
    outer = Part.makeBox(length, width, thickness,
                         FreeCAD.Vector(cx - length / 2, cy - width / 2, z))
    inner = Part.makeBox(length - 2 * line, width - 2 * line, thickness + 0.02,
                         FreeCAD.Vector(cx - length / 2 + line, cy - width / 2 + line, z - 0.01))
    return outer.cut(inner)


def _safe(name, index):
    cleaned = "".join(c if c.isalnum() else "_" for c in str(name or "").strip())
    return f"Cmp{index}_{cleaned}"[:40] if cleaned else f"Cmp{index}"


_FB_SWAP = {"front": "back", "back": "front"}


def _swap_front_back(name):
    return "-".join(_FB_SWAP.get(part, part) for part in str(name).split("-"))


def _flip_y(data):
    """Porta il payload dal sistema del footprint (Y verso il basso, come in
    KiCad) a quello del modello 3D (Y verso l'alto).

    KiCad mostra i modelli con l'asse Y invertito rispetto al footprint: un
    modello costruito con le coordinate del footprint appare specchiato nel
    viewer 3D -- invisibile sulle griglie simmetriche, evidente su un modulo
    con lati diversi e scritte. Le forme sono simmetriche e non cambiano; si
    negano le posizioni Y, gli angoli, e si scambiano i nomi front/back di
    fillet e testi (che l'utente sceglie guardando l'anteprima 2D).
    """
    body = data["body"]
    body["offset_y_mm"] = -(body["offset_y_mm"] + body["width_mm"])
    for f in data.get("fillets", []):
        f["edge"] = _swap_front_back(f["edge"])
    for t in data.get("face_texts", []):
        t["face"] = _swap_front_back(t["face"])
    for key in ("notches", "edge_contacts", "silk_texts", "components"):
        for item in data.get(key, []) or []:
            item["y"] = -item["y"]
            if "angle" in item:
                item["angle"] = -float(item["angle"])
            if "marking_angle" in item:
                item["marking_angle"] = -float(item["marking_angle"])
            if "rotation_deg" in item:
                item["rotation_deg"] = -float(item["rotation_deg"])
            if item.get("edge") in _FB_SWAP:
                item["edge"] = _FB_SWAP[item["edge"]]
    for c in data.get("edge_contacts", []):
        # top/bottom del modulo (bordi orizzontali) si scambiano come front/back
        c["edge"] = {"top": "bottom", "bottom": "top"}.get(c["edge"], c["edge"])
    rim = data.get("silk_outline")
    if rim:
        rim["y"] = -rim["y"]
    for box in data.get("extra_boxes", []):
        box["y"] = -(box["y"] + box["width_mm"])
    data["pins"] = [[n, x, -y] for n, x, y in data.get("pins", [])]
    return data


def main():
    json_path = sys.argv[2]
    with open(json_path, "r", encoding="utf-8") as f:
        data = _flip_y(json.load(f))

    body = data["body"]
    pins = data["pins"]
    pin_diameter = data["pin_diameter_mm"]
    pin_length = data["pin_length_mm"]
    step_path = data["step_path"]
    wrl_path = data.get("wrl_path")
    fcstd_path = data.get("fcstd_path")
    fillets = data.get("fillets", [])
    face_texts = data.get("face_texts", [])
    font_file = data.get("font_file", r"C:\Windows\Fonts\arial.ttf")
    substrate = data.get("substrate")  # solo per i moduli su PCB
    silk_color = data.get("silk_color_hex", "#e8e8e0")

    doc = FreeCAD.newDocument("kicadgen_body")

    x0, y0 = body["offset_x_mm"], body["offset_y_mm"]
    x1, y1, z1 = x0 + body["length_mm"], y0 + body["width_mm"], body["height_mm"]
    bounds = (x0, y0, x1, y1, z1)

    body_shape = Part.makeBox(
        body["length_mm"], body["width_mm"], body["height_mm"],
        FreeCAD.Vector(x0, y0, 0),
    )
    # Il box intatto serve a ritagliare le metallizzazioni: dopo i tagli dei
    # semi-fori, nel solido quello spazio non c'e' piu'.
    uncut_body = body_shape.copy()

    if fillets:
        body_shape = _apply_fillets(body_shape, fillets, bounds)

    # Semi-fori metallizzati sul bordo (moduli castellated): cilindri di cui
    # resta nel solido solo la parte interna. Dopo i fillet, cosi' i raccordi
    # d'angolo non si sovrappongono al taglio.
    for notch in data.get("notches", []):
        try:
            cutter = Part.makeCylinder(
                notch["radius"], body["height_mm"] + 2.0,
                FreeCAD.Vector(notch["x"], notch["y"], -1.0),
                FreeCAD.Vector(0, 0, 1),
            )
            body_shape = body_shape.cut(cutter)
        except Exception as exc:  # noqa: BLE001 - un semi-foro in meno non e' fatale
            print(f"KICADGEN_WARN:semi-foro non applicato in "
                  f"({notch.get('x')}, {notch.get('y')}): {exc}")

    body_color = body.get("color_hex", "#1a1a1a")
    pin_color = data.get("pin_color_hex", "#b0b0b0")

    # (oggetto FreeCAD, colore): ogni pezzo resta separato per poter avere
    # un colore proprio in STEP, VRML e anteprima.
    colored = []
    # Punto in cui la GUI scrive il numero di un contatto: accanto al foro,
    # verso l'interno della scheda, non sopra al foro dove coprirebbe il
    # metallo che serve a riconoscerlo.
    label_anchors = {}

    def add(name, shape, color):
        obj = doc.addObject("Part::Feature", name)
        obj.Shape = shape
        colored.append((obj, color))
        return obj

    if substrate:
        # PCB vero: substrato FR4 tra due strati sottili di soldermask. Visto
        # di taglio si legge come una scheda, non come un blocco verniciato.
        mask_t = float(substrate.get("mask_thickness_mm", 0.02))
        add("Bottom_mask", _slab(body_shape, 0.0, mask_t, bounds), body_color)
        add("FR4_substrate", _slab(body_shape, mask_t, z1 - mask_t, bounds),
            substrate.get("color_hex", "#2f3f1f"))
        add("Body", _slab(body_shape, z1 - mask_t, z1, bounds), body_color)
    else:
        add("Body", body_shape, body_color)

    for i, (text_solid, color) in enumerate(
        _build_face_text_solids(body_shape, face_texts, bounds, font_file)
    ):
        add(f"Serigrafia{i}", text_solid, color)

    # Contatti dei semi-fori: manicotto metallizzato nel foro + piazzola sulla
    # faccia superiore e inferiore, tutto dello stesso metallo (ENIG dorato o
    # stagno). Senza, i contatti non si vedrebbero ne' da sopra ne' da sotto.
    plating = data.get("plating_thickness_mm", 0.06)
    pad_t = 0.025
    for contact in data.get("edge_contacts", []):
        try:
            cx, cy, radius = contact["x"], contact["y"], contact["radius"]
            hole = Part.makeCylinder(radius, z1 + 2.0, FreeCAD.Vector(cx, cy, -1.0),
                                     FreeCAD.Vector(0, 0, 1))
            sleeve = (Part.makeCylinder(radius + plating, z1, FreeCAD.Vector(cx, cy, 0),
                                        FreeCAD.Vector(0, 0, 1))
                      .cut(hole).common(uncut_body))
            pieces = [sleeve]
            edge = contact.get("edge")
            pad_len = float(contact.get("pad_length_mm", 2.4))
            pad_w = float(contact.get("pad_width_mm", 1.8))

            def pad_box(z):
                if edge in ("left", "right"):
                    px = x0 + pad_len / 2 if edge == "left" else x1 - pad_len / 2
                    return Part.makeBox(pad_len, pad_w, pad_t,
                                        FreeCAD.Vector(px - pad_len / 2, cy - pad_w / 2, z))
                py = y0 + pad_len / 2 if edge == "top" else y1 - pad_len / 2
                return Part.makeBox(pad_w, pad_len, pad_t,
                                    FreeCAD.Vector(cx - pad_w / 2, py - pad_len / 2, z))

            pieces.append(pad_box(z1).cut(hole))
            pieces.append(pad_box(-pad_t).cut(hole))
            shape = Part.makeCompound([piece for piece in pieces if piece.Volume > 0])
            add(f"Pin{contact['number']}", shape, contact.get("color_hex", pin_color))
            # Il numero va oltre la fine della piazzola, non a distanza fissa
            # dal foro: le piazzole possono avere lunghezze diverse per lato.
            clearance = pad_len + 0.8
            if edge == "left":
                anchor = (x0 + clearance, cy)
            elif edge == "right":
                anchor = (x1 - clearance, cy)
            elif edge == "top":
                anchor = (cx, y0 + clearance)
            else:
                anchor = (cx, y1 - clearance)
            label_anchors[f"Pin{contact['number']}"] = [anchor[0], anchor[1], -pad_t]
        except Exception as exc:  # noqa: BLE001 - un contatto in meno non e' fatale
            print(f"KICADGEN_WARN:contatto {contact.get('number')} non generato: {exc}")

    # Serigrafia del modulo: sigle accanto ai contatti e cornice del bordo.
    silk_z = z1 + 0.004
    for i, item in enumerate(data.get("silk_texts", [])):
        try:
            shape = _place_text(item["text"], float(item.get("size_mm", 0.7)),
                                item["x"], item["y"], silk_z,
                                float(item.get("angle", 0)), font_file)
            if shape is not None:
                add(f"Silk_{i}", shape, item.get("color_hex", silk_color))
        except Exception as exc:  # noqa: BLE001
            print(f"KICADGEN_WARN:serigrafia '{item.get('text')}' non generata: {exc}")
    rim = data.get("silk_outline")
    if rim:
        try:
            add("Silk_outline", _rim(rim["x"], rim["y"], rim["length_mm"], rim["width_mm"],
                                     silk_z, line=0.1), silk_color)
        except Exception as exc:  # noqa: BLE001
            print(f"KICADGEN_WARN:cornice serigrafica non generata: {exc}")

    # Componenti montati sul modulo: forma per tipo (vedi freecad_parts).
    components = data.get("components", [])
    if components:
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import freecad_parts
        for index, component in enumerate(components):
            base = _safe(component.get("name"), index)
            rotation = float(component.get("rotation_deg") or 0.0)
            centre = FreeCAD.Vector(component["x"], component["y"], 0)
            # Le forme nascono con l'asse lungo parallelo a X e si ruotano
            # attorno al centro del componente: e' l'orientamento letto dalla
            # foto, non una deduzione dalle proporzioni.
            for j, (shape, color) in enumerate(freecad_parts.build_component(component, z1)):
                if rotation:
                    shape.rotate(centre, FreeCAD.Vector(0, 0, 1), rotation)
                add(f"{base}_{j}", shape, color)
            kind = component.get("kind", "generic")
            if kind in ("capacitor", "resistor", "diode", "diode_melf", "led", "capacitor_tantalum"):
                try:
                    rim = _rim(component["x"], component["y"],
                               component["length_mm"] + 0.36, component["width_mm"] + 0.32,
                               silk_z, line=0.08)
                    if rotation:
                        rim.rotate(centre, FreeCAD.Vector(0, 0, 1), rotation)
                    add(f"{base}_silk", rim, silk_color)
                except Exception:  # noqa: BLE001
                    pass
            marking = (component.get("marking") or "").strip()
            if marking:
                try:
                    size = max(0.35, min(0.8, 0.32 * float(component["width_mm"]),
                                         0.9 * float(component["length_mm"]) / max(1, len(marking))))
                    ztop = z1 + float(component["height_mm"]) + 0.004
                    shape = _place_text(marking, size, component["x"], component["y"], ztop,
                                        rotation + float(component.get("marking_angle", 0)), font_file)
                    if shape is not None:
                        add(f"{base}_mark", shape, component.get("marking_color_hex", "#c8c8c0"))
                except Exception as exc:  # noqa: BLE001
                    print(f"KICADGEN_WARN:marcatura '{marking}' non generata: {exc}")

    # Solidi aggiuntivi generici (box con colore proprio).
    for i, box in enumerate(data.get("extra_boxes", [])):
        add(box.get("name", f"Extra{i}"), Part.makeBox(
            box["length_mm"], box["width_mm"], box["height_mm"],
            FreeCAD.Vector(box["x"], box["y"], box.get("z", 0)),
        ), box.get("color_hex", "#1a1a1a"))

    pin_radius = pin_diameter / 2.0
    for pin_num, x, y in pins:
        add(f"Pin{pin_num}", Part.makeCylinder(
            pin_radius, pin_length,
            FreeCAD.Vector(x, y, -pin_length),
            FreeCAD.Vector(0, 0, 1),
        ), pin_color)

    doc.recompute()
    objects = [obj for obj, _ in colored]

    Part.export(objects, step_path)
    print(f"KICADGEN_STEP_OK:{step_path}")

    if fcstd_path:
        try:
            doc.saveAs(fcstd_path)
            print(f"KICADGEN_FCSTD_OK:{fcstd_path}")
        except Exception as exc:  # noqa: BLE001 - sorgente FreeCAD e' best-effort
            print(f"KICADGEN_FCSTD_FAILED:{exc}")

    tessellated = [(obj, color, obj.Shape.tessellate(0.03)) for obj, color in colored]

    # Mesh tassellata: la GUI la usa per l'anteprima della geometria REALE, e il
    # generatore Python la usa per colorare lo STEP (un colore per solido, nello
    # stesso ordine di export).
    mesh_path = data.get("mesh_path")
    if mesh_path:
        try:
            parts = []
            for obj, color, (verts, facets) in tessellated:
                parts.append({
                    "name": obj.Name,
                    "color": color,
                    "solids": len(obj.Shape.Solids),
                    "label": label_anchors.get(obj.Name),
                    "vertices": [[p.x, p.y, p.z] for p in verts],
                    "triangles": [list(f) for f in facets],
                })
            with open(mesh_path, "w", encoding="utf-8") as fh:
                json.dump({"parts": parts}, fh)
            print(f"KICADGEN_MESH_OK:{mesh_path}")
        except Exception as exc:  # noqa: BLE001 - anteprima e' best-effort
            print(f"KICADGEN_MESH_FAILED:{exc}")

    if wrl_path:
        try:
            _write_vrml(wrl_path, tessellated)
            print(f"KICADGEN_WRL_OK:{wrl_path}")
        except Exception as exc:  # noqa: BLE001 - export VRML e' best-effort
            print(f"KICADGEN_WRL_FAILED:{exc}")


main()
