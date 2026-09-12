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


def main():
    json_path = sys.argv[2]
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    body = data["body"]
    pins = data["pins"]
    pin_diameter = data["pin_diameter_mm"]
    pin_length = data["pin_length_mm"]
    step_path = data["step_path"]
    wrl_path = data.get("wrl_path")
    fillets = data.get("fillets", [])
    face_texts = data.get("face_texts", [])
    font_file = data.get("font_file", r"C:\Windows\Fonts\arial.ttf")

    doc = FreeCAD.newDocument("kicadgen_body")

    x0, y0 = body["offset_x_mm"], body["offset_y_mm"]
    x1, y1, z1 = x0 + body["length_mm"], y0 + body["width_mm"], body["height_mm"]
    bounds = (x0, y0, x1, y1, z1)

    body_shape = Part.makeBox(
        body["length_mm"], body["width_mm"], body["height_mm"],
        FreeCAD.Vector(x0, y0, 0),
    )

    if fillets:
        body_shape = _apply_fillets(body_shape, fillets, bounds)

    body_color = body.get("color_hex", "#1a1a1a")
    pin_color = data.get("pin_color_hex", "#b0b0b0")

    # (oggetto FreeCAD, colore): ogni pezzo resta separato per poter avere
    # un colore proprio in VRML e nell'anteprima.
    colored = []

    body_obj = doc.addObject("Part::Feature", "Body")
    body_obj.Shape = body_shape
    colored.append((body_obj, body_color))

    for i, (text_solid, color) in enumerate(
        _build_face_text_solids(body_shape, face_texts, bounds, font_file)
    ):
        text_obj = doc.addObject("Part::Feature", f"Serigrafia{i}")
        text_obj.Shape = text_solid
        colored.append((text_obj, color))

    pin_radius = pin_diameter / 2.0
    for pin_num, x, y in pins:
        pin_obj = doc.addObject("Part::Feature", f"Pin{pin_num}")
        pin_obj.Shape = Part.makeCylinder(
            pin_radius, pin_length,
            FreeCAD.Vector(x, y, -pin_length),
            FreeCAD.Vector(0, 0, 1),
        )
        colored.append((pin_obj, pin_color))

    doc.recompute()
    objects = [obj for obj, _ in colored]

    Part.export(objects, step_path)
    print(f"KICADGEN_STEP_OK:{step_path}")

    tessellated = [(obj, color, obj.Shape.tessellate(0.05)) for obj, color in colored]

    # Mesh tassellata: la GUI la usa per mostrare in anteprima la geometria
    # REALE (fillet e serigrafie incluse), non l'approssimazione a
    # parallelepipedo costruita dai soli parametri.
    mesh_path = data.get("mesh_path")
    if mesh_path:
        try:
            parts = []
            for obj, color, (verts, facets) in tessellated:
                parts.append({
                    "name": obj.Name,
                    "color": color,
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
