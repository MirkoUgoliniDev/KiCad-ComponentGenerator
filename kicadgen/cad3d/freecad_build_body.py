"""@brief Build and export colored board geometry inside headless FreeCAD.

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
import os
import shutil
import sys
import tempfile

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


def _place_text(text, size, x, y, z, angle, font_file, thickness=0.012,
                bounds=None, local_angle=0):
    """Place complete glyphs, optionally fitting their local rotated XY bounds.

    Bounds are the available component-local length/width in mm. XY scaling
    preserves aspect ratio and extrusion thickness; placement rotation follows
    fitting so arbitrary component rotations keep text within its envelope.
    """
    bold = None
    if font_file.lower().endswith("arial.ttf"):
        bold = font_file[:-len("arial.ttf")] + "arialbd.ttf"
    shape = _text_solid(text, size, thickness, font_file, bold)
    if shape is None:
        return None
    if local_angle:
        shape.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), local_angle)
    if bounds is not None:
        bb = shape.BoundBox
        factor = min(1.0, bounds[0] / bb.XLength, bounds[1] / bb.YLength)
        if factor < 1.0:
            # Rebuild at the fitted font size to retain analytic glyph curves
            # and the original extrusion depth (affine OCC conversion can
            # inflate B-spline bounds after rotation).
            shape = _text_solid(text, size * factor, thickness, font_file, bold)
            if shape is None:
                return None
            if local_angle:
                shape.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), local_angle)
        bb = shape.BoundBox
        shape.translate(FreeCAD.Vector(-(bb.XMin + bb.XMax) / 2,
                                       -(bb.YMin + bb.YMax) / 2, 0))
    if angle:
        shape.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), angle)
    shape.translate(FreeCAD.Vector(x, y, z))
    return shape


#: Rapporto di contrasto sotto il quale un corpo si confonde con cio' su cui
#: appoggia. E' la soglia WCAG per gli elementi grafici di grandi dimensioni
#: (3:1): non e' un numero scelto qui, ed e' esattamente la domanda che conta --
#: "questa forma si distingue dal fondo?".
SILK_RIM_MIN_CONTRAST = 3.0


def _relative_luminance(color):
    """@brief Luminanza relativa di un colore esadecimale, secondo WCAG.

    @param color Colore nella forma "#rrggbb".
    @return Luminanza in [0, 1]; 0 per un colore illeggibile.
    """
    try:
        raw = str(color).lstrip("#")
        channels = [int(raw[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    except (ValueError, IndexError, TypeError):
        return 0.0
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _needs_silk_rim(component_color, substrate_color):
    """@brief Serve una cornice chiara perche' il corpo non sparisca nel fondo?

    @param component_color Colore del corpo, letto dalla fotografia.
    @param substrate_color Colore su cui il corpo appoggia.
    @return True quando i due colori non si distinguono abbastanza.
    @note La cornice non e' decorazione: esiste perche' molti case reali sono
        neri quanto il PCB e senza un contorno chiaro sparirebbero nel modello
        anche con la geometria giusta. Disegnarla su TUTTI i componenti la
        metteva pero' anche attorno a un modulo bianco o a un connettore
        metallico, dove non aggiunge nulla e sporca la vista. La domanda giusta
        e' quella che la cornice risolve -- si distingue dal fondo? -- e la
        misura e' il contrasto, non il tipo di componente.
    """
    lighter = max(_relative_luminance(component_color), _relative_luminance(substrate_color))
    darker = min(_relative_luminance(component_color), _relative_luminance(substrate_color))
    return (lighter + 0.05) / (darker + 0.05) < SILK_RIM_MIN_CONTRAST


def _marking_to_print(component, data):
    """@brief Testo da stampare sopra un componente, o stringa vuota.

    @param component Voce di `components` del payload (campo `marking`).
    @param data Payload intero: porta il consenso `show_markings_3d`.
    @return La marcatura ripulita se il payload chiede di stamparla, altrimenti "".
    @note Il percorso standard ricostruisce ingombri meccanici generici,
        senza scritte: la marcatura letta dalla foto ("330?", "123", la
        stringa completa di un modulo radio) e' un'evidenza per l'abbinamento
        ai modelli di libreria e un'informazione per l'utente, non geometria.
        Un payload senza il campo -- come quelli scritti prima che il campo
        esistesse -- non stampa nulla, perche' l'assenza di una scelta non
        e' una scelta. Le sigle dei contatti del modulo (`silk_texts`) sono
        serigrafia del footprint, governata altrove, e non passano di qui.
    """
    if data.get("show_markings_3d", False) is not True:
        return ""
    return str(component.get("marking") or "").strip()


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
            try:
                if not isinstance(item.get("y"), bool):
                    item["y"] = -float(item["y"])
            except (TypeError, ValueError, OverflowError):
                pass  # Preserve invalid component data for build_component to skip.
            if "angle" in item:
                item["angle"] = -float(item["angle"])
            if "marking_angle" in item:
                item["marking_angle"] = -float(item["marking_angle"])
            if "rotation_deg" in item:
                try:
                    item["rotation_deg"] = -float(item["rotation_deg"])
                except (TypeError, ValueError, OverflowError):
                    pass  # Component validation emits the warning and skips it.
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


def _long_path(path):
    """@brief Rendi un percorso Windows immune al limite MAX_PATH.

    Il prefisso esteso disattiva il limite di 260 caratteri per le chiamate
    Win32; richiede un percorso assoluto con separatori nativi.
    """
    path = os.path.abspath(str(path))
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    return "\\\\?\\" + path


def _save_fcstd(doc, fcstd_path):
    """@brief Salva il documento FreeCAD aggirando MAX_PATH su Windows.

    `saveAs` apre il file con le API Win32 classiche e si ferma a 260
    caratteri, mentre l'export STEP passa: un componente dal nome lungo in
    una cartella annidata perdeva quindi il solo sorgente FreeCAD. Si tenta
    prima la via diretta -- dove funziona il comportamento non cambia -- e
    solo se fallisce si salva su un percorso corto e si copia a destinazione
    con il prefisso esteso, che quel limite non ce l'ha.
    """
    try:
        doc.saveAs(str(fcstd_path))
        return
    except Exception:
        if os.name != "nt":
            raise
    with tempfile.TemporaryDirectory() as staging:
        staged = os.path.join(staging, "modello.FCStd")
        doc.saveAs(staged)
        shutil.copyfile(staged, _long_path(fcstd_path))


def main():
    """@brief Run the public entry point for this workflow."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
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
    metadata = {}

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
                if edge == "through":
                    # Through contacts use an observed circular land diameter,
                    # independent of board-edge orientation or pad length.
                    if pad_w <= 2 * radius:
                        raise ValueError("through pad diameter must exceed hole diameter")
                    return Part.makeCylinder(pad_w / 2, pad_t, FreeCAD.Vector(cx, cy, z))
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
            if edge == "through":
                anchor = (cx, cy)
            elif edge == "left":
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
        # L'intervallo del battito arriva dal JSON, derivato dal timeout del
        # watchdog in kicadgen.cad3d.process_log: questo script gira
        # nell'interprete embedded di FreeCAD e non puo' importare quel modulo,
        # quindi il numero viaggia nei parametri invece di essere riscritto qui
        # -- due copie si scollegherebbero alla prima modifica e il watchdog
        # tornerebbe a uccidere import sani.
        freecad_parts.set_heartbeat_interval(data.get("heartbeat_interval_s"))
        for index, component in enumerate(components):
            base = _safe(component.get("name"), index)
            if component.get("render_3d", True) is False:
                print(f"KICADGEN_COMPONENT_SKIPPED:{base}; review-only hypothesis", flush=True)
                continue
            print(f"KICADGEN_COMPONENT_START:{index + 1}/{len(components)}:{base}", flush=True)
            try:
                built = freecad_parts.build_component(component, z1)
                if not built:
                    print(f"KICADGEN_COMPONENT_SKIPPED:{base}", flush=True)
                    continue
                rotation = float(component.get("rotation_deg") or 0.0)
                centre = FreeCAD.Vector(float(component["x"]), float(component["y"]), 0)
                # Le forme nascono con l'asse lungo parallelo a X e si ruotano
                # attorno al centro del componente: e' l'orientamento letto
                # dalla foto, non una deduzione dalle proporzioni.
                #
                # Tutto il blocco e' isolato: un componente con quote
                # degeneri (stima dell'estrazione fuori scala) non deve far
                # sparire quelli successivi ne' l'intero modello.
                for j, (shape, color) in enumerate(built):
                    if rotation:
                        shape.rotate(centre, FreeCAD.Vector(0, 0, 1), rotation)
                    obj = add(f"{base}_{j}", shape, color)
                    path = getattr(color, "model_path", None)
                    metadata[obj.Name] = {
                        "source": "kicad_library" if path else "approximation",
                        "model_path": path,
                        "native_colors": bool(getattr(color, "face_colors", None)),
                        "component_index": index,
                        "source_row": component.get("source_row", index),
                    }
                    if component.get("_model_import_failed"):
                        metadata[obj.Name]["model_import_failed"] = True
                        metadata[obj.Name]["model_import_error"] = component.get(
                            "_model_import_error", "Model import failed")
                print(f"KICADGEN_COMPONENT_OK:{base}; parts={len(built)}", flush=True)

                # Contorno serigrafico solo dove risolve il problema per cui
                # esiste: molti case reali sono neri quanto il PCB (colore
                # corretto, letto dalla foto) e senza una cornice chiara
                # sparirebbero nel modello anche con la geometria giusta. Un
                # modulo bianco o un guscio metallico si distinguono da soli,
                # e li' la cornice e' solo disturbo.
                if _needs_silk_rim(component.get("color_hex", body_color),
                                   (substrate or {}).get("color_hex", body_color)):
                    rim = _rim(component["x"], component["y"],
                               component["length_mm"] + 0.36, component["width_mm"] + 0.32,
                               silk_z, line=0.08)
                    if rotation:
                        rim.rotate(centre, FreeCAD.Vector(0, 0, 1), rotation)
                    silk_obj = add(f"{base}_silk", rim, silk_color)
                    metadata[silk_obj.Name] = {"source_row": component.get("source_row", index)}

                # Solo su richiesta esplicita del payload: vedi _marking_to_print.
                marking = _marking_to_print(component, data)
                if marking:
                    size = max(0.35, min(0.8, 0.32 * float(component["width_mm"]),
                                         0.9 * float(component["length_mm"]) / max(1, len(marking))))
                    ztop = z1 + float(component["height_mm"]) + 0.004
                    text_shape = _place_text(marking, size, component["x"], component["y"], ztop,
                                             rotation, font_file,
                                             bounds=(float(component["length_mm"]),
                                                     float(component["width_mm"])),
                                             local_angle=float(component.get("marking_angle", 0)))
                    if text_shape is not None:
                        mark_obj = add(f"{base}_mark", text_shape, component.get("marking_color_hex", "#c8c8c0"))
                        metadata[mark_obj.Name] = {"source_row": component.get("source_row", index)}
            except Exception as exc:  # noqa: BLE001 - un componente in meno non e' fatale
                print(f"KICADGEN_WARN:componente '{component.get('name')}' non generato: {exc}")

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

    step_colors_exported = False
    print(f"KICADGEN_EXPORT_START:{step_path}", flush=True)
    try:
        import Import
        styled = [(obj, getattr(color, "face_colors", None)
                   or [_hex_to_rgb(color)] * len(obj.Shape.Faces)) for obj, color in colored]
        Import.export(styled, step_path)
        step_colors_exported = True
        print(f"KICADGEN_STEP_COLORS_OK:{step_path}", flush=True)
    except Exception as exc:
        print(f"KICADGEN_WARN:export colori STEP fallito ({exc}); solo geometria", flush=True)
        Part.export(objects, step_path)
    print(f"KICADGEN_STEP_OK:{step_path}", flush=True)

    if fcstd_path:
        try:
            _save_fcstd(doc, fcstd_path)
            print(f"KICADGEN_FCSTD_OK:{fcstd_path}")
        except Exception as exc:  # noqa: BLE001 - sorgente FreeCAD e' best-effort
            print(f"KICADGEN_FCSTD_FAILED:{exc}")
            # Il marcatore sopra resta per chi legge l'output riga per riga,
            # ma solo le righe KICADGEN_WARN: arrivano all'utente: senza
            # questa il sorgente FreeCAD spariva senza che nessuno lo dicesse.
            print(f"KICADGEN_WARN:sorgente FreeCAD non salvata ({exc}); "
                  "STEP e anteprima non ne risentono", flush=True)

    tessellated = []
    for obj, color in colored:
        face_colors = getattr(color, "face_colors", None)
        if face_colors:
            groups = {}
            for face, rgba in zip(obj.Shape.Faces, face_colors):
                rgb = "#" + "".join(f"{round(max(0, min(1, c)) * 255):02x}" for c in rgba[:3])
                groups.setdefault(rgb, []).append(face)
            for rgb, faces in groups.items():
                tessellated.append((obj, rgb, Part.makeCompound(faces).tessellate(0.03)))
        else:
            tessellated.append((obj, color, obj.Shape.tessellate(0.03)))

    # Mesh tassellata: la GUI la usa per l'anteprima della geometria REALE, e il
    # generatore Python la usa per colorare lo STEP (un colore per solido, nello
    # stesso ordine di export).
    mesh_path = data.get("mesh_path")
    if mesh_path:
        try:
            parts = []
            counted = set()
            for obj, color, (verts, facets) in tessellated:
                parts.append({
                    "name": obj.Name if obj.Name not in counted else f"{obj.Name}_color{len(parts)}",
                    "color": color,
                    "solids": len(obj.Shape.Solids) if obj.Name not in counted else 0,
                    **metadata.get(obj.Name, {}),
                    "label": label_anchors.get(obj.Name),
                    "vertices": [[p.x, p.y, p.z] for p in verts],
                    "triangles": [list(f) for f in facets],
                })
                counted.add(obj.Name)
            with open(mesh_path, "w", encoding="utf-8") as fh:
                json.dump({"parts": parts, "step_colors_exported": step_colors_exported}, fh)
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
