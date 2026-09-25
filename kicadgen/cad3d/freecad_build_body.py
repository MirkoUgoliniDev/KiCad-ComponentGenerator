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
import math
import os
import shutil
import sys
import tempfile
import threading
import time

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

TOL = 1e-3

#: Seconds between two heartbeat lines. Fallback only: main() replaces it with
#: the `heartbeat_interval_s` of the params file, derived from the watchdog
#: timeout in kicadgen.cad3d.process_log (this script cannot import it).
_HEARTBEAT_INTERVAL_S = 60.0


class _StepHeartbeat:
    """@brief Keep the parent's silence watchdog quiet during one long step.

    @note Export, 0.03 mm tessellation, mesh dump, recompute, fillet
        permutations and per-hole booleans can each stay silent longer than the
        watchdog tolerates (process_log.run_logged kills the child after
        FREECAD_SILENCE_TIMEOUT_S without output), killing a healthy build.
        A daemon thread prints a KICADGEN_HEARTBEAT line every interval; the
        GUI treats it as a machine line, the watchdog as proof of life.
    @note The thread never touches FreeCAD: it only formats and prints a
        string. DECLARED LIMIT, as for freecad_parts._ImportHeartbeat: it can
        only run while the C++ call releases the GIL; `beat_if_due` is called
        from the Python loops of the same steps so they beat regardless.
    """

    def __init__(self, step, interval=None):
        self._step = str(step)
        self._interval = float(interval or _HEARTBEAT_INTERVAL_S)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._started = self._last = time.monotonic()

    def __enter__(self):
        self._started = self._last = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="kicadgen-step-heartbeat")
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        with self._lock:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(self._interval, 5.0))
            self._thread = None
        return False

    def beat_if_due(self):
        """@brief Beat from the working thread when an interval has elapsed."""
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
                sys.stdout.write(f"KICADGEN_HEARTBEAT:{self._step}; "
                                 f"elapsed={int(now - self._started)}s\n")
                sys.stdout.flush()
            except Exception:  # noqa: BLE001 - closed stdout: the beat is not the work
                self._stop.set()


def _set_heartbeat_interval(seconds):
    """Adopt a positive finite heartbeat interval from the params file."""
    global _HEARTBEAT_INTERVAL_S
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return _HEARTBEAT_INTERVAL_S
    if value > 0 and math.isfinite(value):
        _HEARTBEAT_INTERVAL_S = value
    return _HEARTBEAT_INTERVAL_S


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


#: Faccia del parallelepipedo -> (asse normale, lato). Serve a sapere quale
#: striscia di quale faccia consuma un raccordo.
_FACE_SIDES = {"left": ("x", 0), "right": ("x", 1), "front": ("y", 0),
               "back": ("y", 1), "bottom": ("z", 0), "top": ("z", 1)}


def _fillet_conflicts(fillets, extents):
    """Raccordi che la geometria non puo' contenere, con il motivo.

    Un raccordo di raggio r sullo spigolo fra le facce F1 e F2 toglie una
    striscia larga r a ciascuna delle due: su F2, dal lato di F1, lungo l'asse
    normale a F1 (e viceversa). Su ogni faccia, i due raccordi dei lati
    opposti lungo uno stesso asse devono quindi sommare MENO dell'estensione
    di quell'asse; a somma uguale o maggiore la faccia sparisce e OpenCascade
    risponde solo «BRep_API: command not done». Vincolo geometrico, nessuna
    soglia tarata.

    @param fillets Voci {"edge": nome, "radius_mm": r}; nomi come EDGE_NAMES.
    @param extents {"x": lunghezza, "y": larghezza, "z": altezza} del box, mm.
    @return {nome_spigolo: (faccia, asse, somma, estensione)} degli spigoli
        coinvolti in almeno un vincolo violato; vuoto se tutto e' costruibile.
    """
    # (faccia, asse) -> [raggio massimo lato basso, lato alto] e spigoli
    use, owners = {}, {}
    for fillet in fillets:
        name, radius = fillet["edge"], float(fillet["radius_mm"])
        faces = name.split("-")
        if len(faces) != 2 or not all(face in _FACE_SIDES for face in faces):
            continue
        for face, other in (faces, faces[::-1]):
            axis, side = _FACE_SIDES[other]
            slot = use.setdefault((face, axis), [0.0, 0.0])
            slot[side] = max(slot[side], radius)
            owners.setdefault((face, axis), set()).add(name)
    conflicts = {}
    for (face, axis), (low, high) in sorted(use.items()):
        extent = float(extents[axis])
        if low + high >= extent:
            for name in owners[(face, axis)]:
                conflicts.setdefault(name, (face, axis, low + high, extent))
    return conflicts


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

    # Raccordi impossibili scartati PRIMA di chiederli a OpenCascade, con il
    # motivo misurato: il tentativo fallirebbe comunque con un errore opaco e
    # trascinerebbe nel fallimento anche gli spigoli buoni dello stesso raggio.
    conflicts = _fillet_conflicts(fillets, {"x": x1 - x0, "y": y1 - y0, "z": z1})
    for name, (face, axis, total, extent) in sorted(conflicts.items()):
        _warn("raccordo su '{name}' non applicato: sulla faccia '{face}' i raccordi "
              "sommano {total:g} mm lungo {axis}, ma la faccia misura solo "
              "{extent:g} mm",
              name=name, face=face, total=total, axis=axis.upper(), extent=extent)
    fillets = [f for f in fillets if f["edge"] not in conflicts]

    groups = _fillet_groups(fillets)
    # L'esito di OpenCascade dipende dall'ordine dei gruppi (audit 25/09: r=0.5
    # sul bordo superiore prima di r=3 sugli angoli faceva fallire gli angoli;
    # nell'ordine inverso riuscivano entrambi). Si prova prima l'ordine
    # canonico, poi gli altri, e si tiene quello che applica piu' spigoli:
    # nessuna regola tarata, solo la geometria che OpenCascade accetta.
    best = None
    with _StepHeartbeat("fillets") as heartbeat:
        for order in _fillet_orders(groups):
            heartbeat.beat_if_due()
            result, applied, failed = _try_fillet_groups(shape, order, bounds)
            if best is None or applied > best[1]:
                best = (result, applied, failed)
            if not failed:
                break
    shape, _, failed = best if best is not None else (shape, 0, [])
    for radius, names, exc in failed:
        _warn("fillet r={radius} su {sorted} fallito: {exc}",
              radius=radius, sorted=sorted(names), exc=exc)
    return shape


def _fillet_groups(fillets):
    """Groups (radius, [edge names]) in canonical order: larger radius first,
    and at equal radius the vertical edges (corners seen from above) first."""
    by_radius = {}
    for f in fillets:
        by_radius.setdefault(float(f["radius_mm"]), []).append(f["edge"])

    def vertical(names):
        return any(not name.startswith(("top-", "bottom-")) for name in names)
    return sorted(by_radius.items(), key=lambda item: (-item[0], not vertical(item[1])))


#: Oltre questo numero di gruppi di raggio non si provano tutte le
#: permutazioni (4! = 24 tentativi): solo l'ordine canonico e il suo inverso.
_MAX_PERMUTED_FILLET_GROUPS = 4


def _fillet_orders(groups):
    """Canonical order first, then the other orders worth trying."""
    import itertools
    yield list(groups)
    if len(groups) <= 1:
        return
    if len(groups) <= _MAX_PERMUTED_FILLET_GROUPS:
        for order in itertools.permutations(groups):
            if list(order) != list(groups):
                yield list(order)
    else:
        yield list(reversed(groups))


def _try_fillet_groups(shape, groups, bounds):
    """Apply the groups in this order; return (shape, edges applied, failures)."""
    x0, y0, x1, y1, z1 = bounds
    applied, failed = 0, []
    for radius, edge_names in groups:
        wanted = set(edge_names)
        target_edges = [
            e for e in shape.Edges
            if _edge_name(e, x0, y0, x1, y1, z1) in wanted
        ]
        missing = wanted - {
            _edge_name(e, x0, y0, x1, y1, z1) for e in shape.Edges
        }
        for name in sorted(missing):
            _warn("spigolo '{name}' non trovato sulla geometria corrente", name=name)
        if not target_edges:
            continue
        try:
            shape = shape.makeFillet(radius, target_edges)
            applied += len(wanted)
        except Exception as exc:  # noqa: BLE001 - segnalato, non fatale
            failed.append((radius, wanted, exc))
    return shape, applied, failed


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
            _warn("faccia '{face}' non trovata per il testo", face=t['face'])
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
        # Il frame e' esplicito (normale, alto = +Z): Rotation(Z, normale)
        # sceglieva la rotazione minima, che capovolgeva la scritta sulla
        # faccia posteriore e la metteva in verticale su quelle laterali.
        x_axis, y_axis, z_axis = _text_frame((normal.x, normal.y, normal.z))
        matrix = FreeCAD.Matrix(x_axis[0], y_axis[0], z_axis[0], 0,
                                x_axis[1], y_axis[1], z_axis[1], 0,
                                x_axis[2], y_axis[2], z_axis[2], 0,
                                0, 0, 0, 1)
        target = FreeCAD.Placement(matrix)
        target.Base = center - normal * sink
        solid.Placement = target.multiply(solid.Placement)

        solids.append((solid, t.get("color_hex", "#ffffff")))
    return solids


def _text_frame(normal):
    """@brief Right-handed frame for text lying on a face with this outward normal.

    @param normal Outward face normal (x, y, z); need not be unit length.
    @return (x_axis, y_axis, z_axis) unit tuples: z_axis is the normal, y_axis
        the text "up", x_axis the reading direction.
    @note Up is +Z on every side face, so the text reads upright and
        left-to-right to someone looking at that face from outside. On the top
        and bottom faces up is +Y (the back of the part): top text reads along
        +X seen from above, bottom text along -X seen from below.
    """
    nx, ny, nz = (float(v) for v in normal)
    norm = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    nx, ny, nz = nx / norm, ny / norm, nz / norm
    up = (0.0, 1.0, 0.0) if abs(nz) > 0.9 else (0.0, 0.0, 1.0)
    # x = up x n
    x = (up[1] * nz - up[2] * ny, up[2] * nx - up[0] * nz, up[0] * ny - up[1] * nx)
    length = math.sqrt(sum(c * c for c in x)) or 1.0
    x = tuple(c / length for c in x)
    # y = n x x (exactly `up` projected on the face)
    y = (ny * x[2] - nz * x[1], nz * x[0] - nx * x[2], nx * x[1] - ny * x[0])
    return x, y, (nx, ny, nz)


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
    negano le posizioni Y e gli angoli. I nomi front/back di fillet e testi NON
    si toccano: dalla convenzione 2 (2026-09-25) sono gia' quelli del modello
    3D (front = lato -Y, la faccia «Fronte» del cubo), gli stessi con cui
    `_edge_name` e `_face_name` classificano il solido qui sotto.
    """
    body = data["body"]
    body["offset_y_mm"] = -(body["offset_y_mm"] + body["width_mm"])
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
        # Il lato di un foro passante e il foro chiuso di un contatto doppio
        # sono geometria come il resto: vanno riflessi con lui, altrimenti la
        # sigla finirebbe verso l'esterno e il foro sul lato sbagliato.
        if "board_edge" in c:
            c["board_edge"] = {"top": "bottom", "bottom": "top"}.get(c["board_edge"], c["board_edge"])
        through = c.get("through_hole")
        if isinstance(through, dict) and "y" in through:
            through["y"] = -float(through["y"])
    rim = data.get("silk_outline")
    if rim:
        rim["y"] = -rim["y"]
    for box in data.get("extra_boxes", []):
        box["y"] = -(box["y"] + box["width_mm"])
    data["pins"] = [[n, x, -y] for n, x, y in data.get("pins", [])]
    return data


#: Direzione verso l'interno della scheda per ogni lato, nel sistema del
#: modello 3D (Y verso l'alto, dopo _flip_y).
_INWARD = {"left": (1.0, 0.0), "right": (-1.0, 0.0), "top": (0.0, 1.0), "bottom": (0.0, -1.0)}


def _through_features(x, y, radius, pad_diameter, z1, plating, pad_t, clip):
    """@brief Plated barrel and annular rings of one closed through-hole.

    @param x Hole centre X, mm.
    @param y Hole centre Y, mm.
    @param radius Finished bore radius, mm (the notch cut into the board).
    @param pad_diameter Outer diameter of the annular ring, mm.
    @param z1 Board thickness, mm.
    @param plating Barrel wall thickness, mm.
    @param pad_t Ring thickness, mm.
    @param clip Whether the barrel is clipped to the board outline.
    @return (tubes, drills): tube specs {x, y, r_in, r_out, z0, z1, clip}
        for the barrel (through the full thickness) and one ring per face,
        and the cylinder (x, y, r) to drill out of the board.
    @throws ValueError When the ring is not wider than the bore.
    @note The rings sit ON the soldermask (z1..z1+pad_t on top,
        -pad_t..0 underneath), never inside it, so the mask cannot hide them;
        their inner radius equals the bore, so the bore stays open.
        The board is drilled to the barrel's OUTER radius: the barrel then
        fills the gap instead of sharing its inner wall with the substrate,
        where two coincident faces of different colours flicker.
    """
    if pad_diameter <= 2 * radius:
        raise ValueError("through pad diameter must exceed hole diameter")
    tubes = [
        dict(x=x, y=y, r_in=radius, r_out=radius + plating, z0=0.0, z1=z1, clip=clip),
        dict(x=x, y=y, r_in=radius, r_out=pad_diameter / 2, z0=z1, z1=z1 + pad_t, clip=False),
        dict(x=x, y=y, r_in=radius, r_out=pad_diameter / 2, z0=-pad_t, z1=0.0, clip=False),
    ]
    return tubes, [(x, y, radius + plating)]


def _contact_features(contact, bounds, plating, pad_t):
    """@brief Describe every plated feature of one contact, without FreeCAD.

    @param contact Entry of `edge_contacts` (already in 3D coordinates).
    @param bounds (x0, y0, x1, y1, z1) of the board.
    @param plating Barrel wall thickness, mm.
    @param pad_t Pad/ring thickness, mm.
    @return Dict with `tubes` (barrels and rings), `boxes` (rectangular edge
        pads {x0, y0, x1, y1, z0, z1}), `stadiums` (continuous pads, see
        below), `bores` (x, y, r) every box and stadium is cut by so no pad
        covers a hole, `drills` (x, y, r) to remove from the board before it
        is sliced, and `label` [x, y, z] where the GUI writes the contact
        number.
    @throws ValueError On a through-hole whose ring is not wider than its bore.
    @note Everything belongs to ONE contact and becomes one solid named after
        its number: a half_and_through contact is one net with two features,
        not two contacts. Kept free of FreeCAD so the geometry handed to it
        can be tested with plain Python.
    @note A contact carrying `stadium` {length_mm, width_mm} gets ONE
        continuous gold pad per face instead of the rectangular edge pad and
        the two rings of its through-hole: spec {x, y, nx, ny, length, r, z0,
        z1} = starting on the board edge at (x, y), running inward along
        (nx, ny) for `length`, 2 * r wide, with a round inner end of radius r
        (a stadium whose outer half-disc is cut away by the board edge). The
        through-hole keeps only its barrel: its rings would lie inside the
        stadium, on the same faces, and two coincident gold faces flicker.
    """
    x0, y0, x1, y1, z1 = bounds
    cx, cy, radius = float(contact["x"]), float(contact["y"]), float(contact["radius"])
    edge = contact.get("edge")
    pad_len = float(contact.get("pad_length_mm", 2.4))
    pad_w = float(contact.get("pad_width_mm", 1.8))
    tubes, boxes, bores, drills, stadiums = [], [], [], [], []
    stadium = contact.get("stadium")
    if edge == "through":
        # Through contacts use an observed circular land diameter,
        # independent of board-edge orientation or pad length.
        found, holes = _through_features(cx, cy, radius, pad_w, z1, plating, pad_t, clip=True)
        tubes += found
        drills += holes
        inward = _INWARD.get(contact.get("board_edge"))
        if inward is None:
            label = [cx, cy]
        else:
            # Verso l'interno oltre la corona, non sopra il foro: il numero
            # coprirebbe proprio il metallo che serve a riconoscerlo.
            reach = pad_w / 2 + 0.8
            label = [cx + inward[0] * reach, cy + inward[1] * reach]
    else:
        # Semi-foro: manicotto tagliato dal bordo (clip sul contorno intatto)
        # piu' la piazzola rettangolare su entrambe le facce.
        tubes.append(dict(x=cx, y=cy, r_in=radius, r_out=radius + plating,
                          z0=0.0, z1=z1, clip=True))
        drills.append((cx, cy, radius + plating))
        bores.append((cx, cy, radius))
        for z in ((z1, -pad_t) if not isinstance(stadium, dict) else ()):
            if edge in ("left", "right"):
                px = x0 + pad_len / 2 if edge == "left" else x1 - pad_len / 2
                boxes.append(dict(x0=px - pad_len / 2, y0=cy - pad_w / 2,
                                  x1=px + pad_len / 2, y1=cy + pad_w / 2, z0=z, z1=z + pad_t))
            else:
                py = y0 + pad_len / 2 if edge == "top" else y1 - pad_len / 2
                boxes.append(dict(x0=cx - pad_w / 2, y0=py - pad_len / 2,
                                  x1=cx + pad_w / 2, y1=py + pad_len / 2, z0=z, z1=z + pad_t))
        through = contact.get("through_hole")
        if isinstance(through, dict):
            # Il foro chiuso di un contatto doppio: prima era solo tagliato,
            # quindi si vedeva un puntino scuro invece di una corona dorata.
            tx, ty = float(through["x"]), float(through["y"])
            tr = float(through["radius"])
            found, holes = _through_features(tx, ty, tr, float(through["pad_diameter_mm"]),
                                             z1, plating, pad_t, clip=False)
            if isinstance(stadium, dict):
                # Solo la canna: le corone starebbero dentro la piazzola
                # continua, sulle stesse facce (vedi @note).
                found = [t for t in found if t["z0"] == 0.0 and t["z1"] == z1]
            tubes += found
            drills += holes
            # La piazzola di bordo puo' arrivare sopra il foro: va forata
            # anche li', o coprirebbe la bocca del foro passante.
            bores.append((tx, ty, tr))
        if isinstance(stadium, dict):
            # Un'unica piazzola dorata dal bordo fino oltre la corona, su
            # entrambe le facce: niente scheda scoperta fra semi-foro e foro.
            nx, ny = _INWARD[edge]
            ex = x0 if edge == "left" else x1 if edge == "right" else cx
            ey = y0 if edge == "top" else y1 if edge == "bottom" else cy
            s_len = float(stadium["length_mm"])
            s_r = float(stadium["width_mm"]) / 2
            if s_len < s_r:
                raise ValueError("stadium shorter than its round end")
            for z in (z1, -pad_t):
                stadiums.append(dict(x=ex, y=ey, nx=nx, ny=ny, length=s_len, r=s_r,
                                     z0=z, z1=z + pad_t))
            pad_len = max(pad_len, s_len)
        # Il numero va oltre la fine della piazzola, non a distanza fissa
        # dal foro: le piazzole possono avere lunghezze diverse per lato.
        clearance = pad_len + 0.8
        if edge == "left":
            label = [x0 + clearance, cy]
        elif edge == "right":
            label = [x1 - clearance, cy]
        elif edge == "top":
            label = [cx, y0 + clearance]
        else:
            label = [cx, y1 - clearance]
    return {"tubes": tubes, "boxes": boxes, "stadiums": stadiums, "bores": bores,
            "drills": drills, "label": [label[0], label[1], -pad_t]}


def _covered_by_drill(notch, drills):
    """@brief True when a notch lies inside the drilling of a plated barrel.

    @param notch Payload notch {x, y, radius}.
    @param drills (x, y, r) cylinders from _contact_features.
    @return True when some drill shares its centre and is at least as wide,
        so cutting the notch as well would only cost another boolean.
    """
    try:
        x, y, radius = float(notch["x"]), float(notch["y"]), float(notch["radius"])
    except (KeyError, TypeError, ValueError):
        return False
    return any(abs(x - dx) < 1e-9 and abs(y - dy) < 1e-9 and dr >= radius
               for dx, dy, dr in drills)


def _contact_shape(features, uncut_body):
    """Turn _contact_features specs into one FreeCAD compound."""
    pieces = []
    for tube in features["tubes"]:
        height = tube["z1"] - tube["z0"]
        outer = Part.makeCylinder(tube["r_out"], height,
                                  FreeCAD.Vector(tube["x"], tube["y"], tube["z0"]),
                                  FreeCAD.Vector(0, 0, 1))
        inner = Part.makeCylinder(tube["r_in"], height + 2.0,
                                  FreeCAD.Vector(tube["x"], tube["y"], tube["z0"] - 1.0),
                                  FreeCAD.Vector(0, 0, 1))
        piece = outer.cut(inner)
        if tube.get("clip"):
            piece = piece.common(uncut_body)
        pieces.append(piece)
    for spec in features["boxes"]:
        box = Part.makeBox(spec["x1"] - spec["x0"], spec["y1"] - spec["y0"],
                           spec["z1"] - spec["z0"],
                           FreeCAD.Vector(spec["x0"], spec["y0"], spec["z0"]))
        for bx, by, br in features["bores"]:
            box = box.cut(Part.makeCylinder(br, spec["z1"] - spec["z0"] + 2.0,
                                            FreeCAD.Vector(bx, by, spec["z0"] - 1.0),
                                            FreeCAD.Vector(0, 0, 1)))
        pieces.append(box)
    for spec in features.get("stadiums", []):
        pad = _stadium_solid(spec)
        for bx, by, br in features["bores"]:
            pad = pad.cut(Part.makeCylinder(br, spec["z1"] - spec["z0"] + 2.0,
                                            FreeCAD.Vector(bx, by, spec["z0"] - 1.0),
                                            FreeCAD.Vector(0, 0, 1)))
        pieces.append(pad)
    return Part.makeCompound([piece for piece in pieces if piece.Volume > 0])


def _stadium_solid(spec):
    """Extrude one continuous pad spec from _contact_features into a FreeCAD solid.

    Outline: straight sides from the board edge to the centre of the round
    end, then a half-circle of radius r; the edge side is a straight line, so
    the pad ends exactly on the board edge where the half-hole opens.
    """
    ex, ey, nx, ny = spec["x"], spec["y"], spec["nx"], spec["ny"]
    r, length, z = spec["r"], spec["length"], spec["z0"]
    tx, ty = -ny, nx
    reach = length - r

    def point(along_n, along_t):
        return FreeCAD.Vector(ex + nx * along_n + tx * along_t,
                              ey + ny * along_n + ty * along_t, z)

    edges = []
    if reach > 1e-9:
        edges.append(Part.LineSegment(point(0.0, r), point(reach, r)).toShape())
    edges.append(Part.Arc(point(reach, r), point(length, 0.0), point(reach, -r)).toShape())
    if reach > 1e-9:
        edges.append(Part.LineSegment(point(reach, -r), point(0.0, -r)).toShape())
    edges.append(Part.LineSegment(point(0.0, -r), point(0.0, r)).toShape())
    face = Part.Face(Part.Wire(edges))
    return face.extrude(FreeCAD.Vector(0, 0, spec["z1"] - spec["z0"]))


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
    """@brief Build body, pins, substrate, fillets and texts inside FreeCAD from the JSON job and export them."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    json_path = sys.argv[2]
    with open(json_path, "r", encoding="utf-8") as f:
        data = _flip_y(json.load(f))
    _set_heartbeat_interval(data.get("heartbeat_interval_s"))

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

    # Metallizzazioni dei contatti, descritte PRIMA di forare e affettare la
    # scheda: ogni canna va forata nel substrato al suo raggio esterno (vedi
    # _through_features), e dopo il taglio in strati non si puo' piu'.
    plating = data.get("plating_thickness_mm", 0.06)
    pad_t = 0.025
    plated = []
    for contact in data.get("edge_contacts", []):
        try:
            plated.append((contact, _contact_features(contact, bounds, plating, pad_t)))
        except Exception as exc:  # noqa: BLE001 - un contatto in meno non e' fatale
            _warn("contatto {get} non generato: {exc}", get=contact.get('number'), exc=exc)
    drills = [drill for _, features in plated for drill in features["drills"]]

    # Semi-fori e fori (moduli castellated): cilindri di cui resta nel solido
    # solo la parte interna. Dopo i fillet, cosi' i raccordi d'angolo non si
    # sovrappongono al taglio. Un foro gia' coperto dalla foratura della sua
    # canna non si taglia due volte: ogni taglio booleano costa, e la scheda
    # ne ha uno per foro come prima.
    cutters = [n for n in data.get("notches", []) if not _covered_by_drill(n, drills)]
    cutters += [{"x": x, "y": y, "radius": r} for x, y, r in drills]
    cut_heartbeat = _StepHeartbeat("holes").__enter__()
    # Ogni iterazione intercetta le proprie eccezioni, quindi l'uscita qui
    # sotto si raggiunge sempre: il battito non sopravvive al passo.
    for notch in cutters:
        cut_heartbeat.beat_if_due()
        try:
            x, y, radius = notch["x"], notch["y"], notch["radius"]
            cutter = Part.makeCylinder(
                radius, body["height_mm"] + 2.0,
                FreeCAD.Vector(x, y, -1.0),
                FreeCAD.Vector(0, 0, 1),
            )
            body_shape = body_shape.cut(cutter)
        except Exception as exc:  # noqa: BLE001 - un semi-foro in meno non e' fatale
            _warn("semi-foro non applicato in ({get}, {get2}): {exc}",
               get=notch.get('x'),
               get2=notch.get('y'),
               exc=exc)
    cut_heartbeat.__exit__(None, None, None)

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

    # Contatti: manicotto metallizzato nel foro + piazzola sulla faccia
    # superiore e inferiore, tutto dello stesso metallo (ENIG dorato o stagno),
    # per i semi-fori come per i fori passanti. Senza, i contatti non si
    # vedrebbero ne' da sopra ne' da sotto.
    for contact, features in plated:
        try:
            shape = _contact_shape(features, uncut_body)
            add(f"Pin{contact['number']}", shape, contact.get("color_hex", pin_color))
            label_anchors[f"Pin{contact['number']}"] = features["label"]
        except Exception as exc:  # noqa: BLE001 - un contatto in meno non e' fatale
            _warn("contatto {get} non generato: {exc}", get=contact.get('number'), exc=exc)

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
            _warn("serigrafia '{get}' non generata: {exc}", get=item.get('text'), exc=exc)
    rim = data.get("silk_outline")
    if rim:
        try:
            add("Silk_outline", _rim(rim["x"], rim["y"], rim["length_mm"], rim["width_mm"],
                                     silk_z, line=0.1), silk_color)
        except Exception as exc:  # noqa: BLE001
            _warn("cornice serigrafica non generata: {exc}", exc=exc)

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
        # Cartelle di cache risolte dal padre (KICADGEN_DATA_DIR compreso).
        freecad_parts.set_cache_dirs(data.get("cache_dirs"))
        # Centro della scheda nel frame del modello: i connettori da bordo
        # guardano fuori scheda rispetto a QUESTO punto, non all'origine, che
        # nel generatore a matrice sta sul pin 1 e non al centro.
        board_centre = [(x0 + x1) / 2, (y0 + y1) / 2]
        for component in components:
            if isinstance(component, dict):
                component.setdefault("board_centre_xy", board_centre)
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
                _warn("componente '{get}' non generato: {exc}", get=component.get('name'), exc=exc)

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

    with _StepHeartbeat("recompute"):
        doc.recompute()
    objects = [obj for obj, _ in colored]

    step_colors_exported = False
    print(f"KICADGEN_EXPORT_START:{step_path}", flush=True)
    with _StepHeartbeat("step_export"):
        try:
            import Import
            styled = [(obj, getattr(color, "face_colors", None)
                       or [_hex_to_rgb(color)] * len(obj.Shape.Faces)) for obj, color in colored]
            Import.export(styled, step_path)
            step_colors_exported = True
            print(f"KICADGEN_STEP_COLORS_OK:{step_path}", flush=True)
        except Exception as exc:
            _warn("export colori STEP fallito ({exc}); solo geometria", exc=exc)
            Part.export(objects, step_path)
    print(f"KICADGEN_STEP_OK:{step_path}", flush=True)

    if fcstd_path:
        try:
            with _StepHeartbeat("fcstd_save"):
                _save_fcstd(doc, fcstd_path)
            print(f"KICADGEN_FCSTD_OK:{fcstd_path}")
        except Exception as exc:  # noqa: BLE001 - sorgente FreeCAD e' best-effort
            print(f"KICADGEN_FCSTD_FAILED:{exc}")
            # Il marcatore sopra resta per chi legge l'output riga per riga,
            # ma solo le righe KICADGEN_WARN: arrivano all'utente: senza
            # questa il sorgente FreeCAD spariva senza che nessuno lo dicesse.
            _warn("sorgente FreeCAD non salvata ({exc}); STEP e anteprima non ne "
               "risentono",
               exc=exc)

    tessellated = []
    with _StepHeartbeat("tessellation") as heartbeat:
        for obj, color in colored:
            heartbeat.beat_if_due()
            face_colors = getattr(color, "face_colors", None)
            if face_colors:
                groups = {}
                for face, rgba in zip(obj.Shape.Faces, face_colors):
                    rgb = "#" + "".join(f"{round(max(0, min(1, c)) * 255):02x}" for c in rgba[:3])
                    groups.setdefault(rgb, []).append(face)
                for rgb, faces in groups.items():
                    heartbeat.beat_if_due()
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
            with _StepHeartbeat("mesh_json") as mesh_heartbeat:
                for obj, color, (verts, facets) in tessellated:
                    mesh_heartbeat.beat_if_due()
                    parts.append({
                        "name": (obj.Name if obj.Name not in counted
                                 else f"{obj.Name}_color{len(parts)}"),
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
            with _StepHeartbeat("vrml_export"):
                _write_vrml(wrl_path, tessellated)
            print(f"KICADGEN_WRL_OK:{wrl_path}")
        except Exception as exc:  # noqa: BLE001 - export VRML e' best-effort
            print(f"KICADGEN_WRL_FAILED:{exc}")


main()
