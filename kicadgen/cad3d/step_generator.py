"""Orchestrazione della generazione 3D: chiama freecadcmd in headless.

Costruisce un JSON con corpo+pin, lo passa a freecad_build_body.py (che gira
dentro l'interprete Python embedded di FreeCAD) via subprocess, e verifica
che i file di output siano stati effettivamente scritti.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Optional

from ..generators.castellated_module import CastellatedModuleParams, pad_positions
from ..generators.pin_header import PinHeaderParams, pin_positions
from .annotations import EdgeFilletAnnotation, FaceTextAnnotation
from .body_params import Body3DParams

DEFAULT_FONT_FILE = r"C:\Windows\Fonts\arial.ttf"

_SCRIPT_PATH = Path(__file__).parent / "freecad_build_body.py"

_CFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _safe_name(name: str, index: int) -> str:
    """Nome usabile come nome di oggetto FreeCAD (solo lettere, cifre, _)."""
    cleaned = "".join(c if c.isalnum() else "_" for c in (name or "").strip())
    return f"Cmp{index}_{cleaned}"[:40] if cleaned else f"Cmp{index}"


#: Distanza minima tenuta tra il bordo di un componente e il bordo del modulo.
COMPONENT_EDGE_MARGIN_MM = 0.2


def _match_evidence(component):
    """Copy only matching observations; evidence cannot replace CAD placement."""
    evidence = getattr(component, "match_evidence", None)
    if not isinstance(evidence, dict):
        return {}
    fields = ("presence_confidence", "class_confidence", "confidence",
              "_yolo_review_only", "_yolo_origin", "terminal_count",
              "terminal_layout", "terminal_count_basis", "_global_inventory_status", "class")
    return {key: evidence[key] for key in fields if key in evidence}


def _model_placement(component):
    """Keep only explicit finite native-model overrides; None means infer axes.

    Vi rientrano anche le scelte fatte a mano dal menu contestuale della
    vista 3D -- esclusione dal solido e correzione d'orientamento -- perche'
    devono arrivare al costruttore per entrambe le famiglie di componente,
    non solo per quella che capita di toccare per prima.
    """
    result = {}
    for field in ("model_rotation_deg", "model_z_offset_mm", "model_rotation_delta_deg"):
        value = getattr(component, field, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if not math.isfinite(value):
                    continue
            except OverflowError:
                continue
            # Una correzione di zero gradi non e' una scelta fatta: si omette,
            # come si omette un override di visibilita' non impostato. Gli
            # altri due campi restano com'erano per non cambiare un contratto
            # gia' in uso altrove.
            if field == "model_rotation_delta_deg" and not value:
                continue
            result[field] = value
    visible = getattr(component, "render_3d_override", None)
    if isinstance(visible, bool):
        result["render_3d_override"] = visible
        result["render_3d"] = visible
    return result


def _rectangle_axes(rotation: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Assi unitari locali, esatti per le rotazioni cardinali."""
    angle = rotation % 360
    if angle % 90 == 0:
        c, s = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[int(angle / 90)]
    else:
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return (c, s), (-s, c)


def clamp_component_position(
    x: float, y: float, length: float, width: float, rotation: float,
    body_length: float, body_width: float, margin: float = COMPONENT_EDGE_MARGIN_MM,
) -> tuple[float, float]:
    """Tiene il centro di un componente dentro il perimetro del modulo.

    Le stime di posizione dell'estrazione a volte cadono a cavallo del
    bordo (letto vicino al margine della foto, o con un componente grande
    vicino a un contatto): e' un vincolo geometrico generico, applicabile a
    qualunque componente di qualunque modulo, non una correzione per un
    pezzo particolare. Proietta il rettangolo ruotato sugli assi X/Y;
    a 90/270 gradi lunghezza e larghezza si scambiano.
    """
    (c, s), _ = _rectangle_axes(rotation)
    footprint_x = abs(c) * length + abs(s) * width
    footprint_y = abs(s) * length + abs(c) * width
    half_x = max(0.0, body_length / 2 - footprint_x / 2 - margin)
    half_y = max(0.0, body_width / 2 - footprint_y / 2 - margin)
    return max(-half_x, min(half_x, x)), max(-half_y, min(half_y, y))


#: Sovrapposizione minima, su entrambi gli assi, perche' due componenti
#: contino come sovrapposti (non solo adiacenti/a contatto).
OVERLAP_TOLERANCE_MM = 0.05


def find_overlapping_components(components: list) -> list[str]:
    """Coppie di componenti le cui sagome (gia' clampate, gia' ruotate) si
    sovrappongono, come avvisi pronti per il log.

    Le posizioni vengono dalla stima dell'estrazione, presa componente per
    componente: nulla impedisce a due stime indipendenti di finire una
    sopra l'altra. Non si tenta di risolverlo spostando qualcosa (sarebbe
    una scelta arbitraria, non geometria) -- si segnala la coppia, cosi'
    si corregge nella tabella componenti invece di scoprirlo solo nel
    modello 3D finito.

    Il test degli assi separatori confronta i rettangoli orientati, evitando
    falsi positivi tra componenti diagonali con bounding box sovrapposte.
    """
    boxes = []
    for component in components:
        rotation = float(component.get("rotation_deg") or 0.0)
        length = float(component["length_mm"])
        width = float(component["width_mm"])
        u, v = _rectangle_axes(rotation)
        x, y = float(component["x"]), float(component["y"])
        corners = [
            (x + a * length / 2 * u[0] + b * width / 2 * v[0],
             y + a * length / 2 * u[1] + b * width / 2 * v[1])
            for a, b in ((-1, -1), (-1, 1), (1, 1), (1, -1))
        ]
        boxes.append((component.get("name", "?"), corners, (u, v)))

    warnings = []
    for i in range(len(boxes)):
        name_a, corners_a, axes_a = boxes[i]
        for j in range(i + 1, len(boxes)):
            name_b, corners_b, axes_b = boxes[j]
            overlaps = []
            # X/Y mantengono le misure del messaggio per i casi cardinali;
            # gli assi locali eliminano i falsi positivi delle bounding box.
            for ux, uy in ((1.0, 0.0), (0.0, 1.0)) + axes_a + axes_b:
                pa = [x * ux + y * uy for x, y in corners_a]
                pb = [x * ux + y * uy for x, y in corners_b]
                overlaps.append(min(max(pa), max(pb)) - max(min(pa), min(pb)))
            if all(overlap > OVERLAP_TOLERANCE_MM for overlap in overlaps):
                overlap_x, overlap_y = overlaps[:2]
                warnings.append(
                    f"componenti sovrapposti: '{name_a}' e '{name_b}' "
                    f"({overlap_x:.2f}x{overlap_y:.2f}mm in comune) -- correggi "
                    "posizione o rotazione nella tabella componenti."
                )
    return warnings


def _hex_to_rgb(color: str) -> tuple[float, float, float]:
    try:
        h = color.lstrip("#")
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    except (ValueError, IndexError):
        return (0.5, 0.5, 0.5)


def colorize_step(step_text: str, colors: list) -> str:
    """Aggiunge a uno STEP AP214 un colore per ogni solido, nell'ordine di export.

    `Part.export` di FreeCAD scrive la sola geometria: senza queste entita' di
    presentazione KiCad e gli altri CAD mostrano tutto in grigio. Per ogni
    MANIFOLD_SOLID_BREP si aggiunge la catena
    COLOUR_RGB -> FILL_AREA_STYLE -> ... -> STYLED_ITEM, e un'unica
    MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION che le raccoglie.

    `colors` sono stringhe '#rrggbb', una per solido. Se il conteggio non
    torna (geometria vuota, export parziale) si solleva ValueError e il file
    resta com'e': meglio grigio che colorato a caso.
    """
    breps = re.findall(r"#(\d+) = MANIFOLD_SOLID_BREP", step_text)
    if not breps:
        raise ValueError("nessun solido nello STEP")
    if len(breps) != len(colors):
        raise ValueError(f"{len(breps)} solidi nello STEP ma {len(colors)} colori")
    context = re.search(r"#(\d+) = \( GEOMETRIC_REPRESENTATION_CONTEXT", step_text)
    if not context:
        raise ValueError("contesto geometrico non trovato nello STEP")
    if "END-ISO-10303-21;" not in step_text:
        raise ValueError("STEP senza terminatore")

    last = max(int(n) for n in re.findall(r"#(\d+)\s*=", step_text))
    entities = []

    def entity(text: str) -> int:
        nonlocal last
        last += 1
        entities.append(f"#{last} = {text};")
        return last

    styled = []
    for brep, color in zip(breps, colors):
        r, g, b = _hex_to_rgb(color)
        rgb = entity(f"COLOUR_RGB('',{r:.4f},{g:.4f},{b:.4f})")
        fill = entity(f"FILL_AREA_STYLE_COLOUR('',#{rgb})")
        area = entity(f"FILL_AREA_STYLE('',(#{fill}))")
        surf = entity(f"SURFACE_STYLE_FILL_AREA(#{area})")
        side = entity(f"SURFACE_SIDE_STYLE('',(#{surf}))")
        usage = entity(f"SURFACE_STYLE_USAGE(.BOTH.,#{side})")
        style = entity(f"PRESENTATION_STYLE_ASSIGNMENT((#{usage}))")
        styled.append(entity(f"STYLED_ITEM('',(#{style}),#{brep})"))
    entity(
        "MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION('',("
        + ",".join(f"#{n}" for n in styled) + f"),#{context[1]})"
    )

    marker = "ENDSEC;\nEND-ISO-10303-21;"
    if marker not in step_text:
        marker = "ENDSEC;\r\nEND-ISO-10303-21;"
    return step_text.replace(marker, "\n".join(entities) + "\n" + marker, 1)


class Cad3DError(Exception):
    """Errore applicativo: freecadcmd non trovato, o generazione fallita."""


def find_freecadcmd() -> str:
    """Cerca freecadcmd: variabile d'ambiente FREECAD_CMD_PATH, poi le
    installazioni tipiche di Windows sotto Program Files."""
    env_path = os.environ.get("FREECAD_CMD_PATH")
    if env_path and Path(env_path).is_file():
        return env_path

    candidates = []
    for base in (r"C:\Program Files", r"C:\Program Files (x86)"):
        candidates.extend(glob.glob(f"{base}\\FreeCAD*\\bin\\freecadcmd.exe"))
    for base in ("/usr/bin", "/usr/local/bin"):
        candidates.extend(glob.glob(f"{base}/freecadcmd"))

    if candidates:
        candidates.sort(reverse=True)  # preferisce la versione con path/nome piu' alto
        return candidates[0]

    raise Cad3DError(
        "freecadcmd non trovato. Installa FreeCAD, oppure imposta la variabile "
        "d'ambiente FREECAD_CMD_PATH con il percorso completo di freecadcmd.exe."
    )


def generate_castellated_3d_model(
    params: CastellatedModuleParams,
    body: Body3DParams,
    output_dir: Path,
    freecadcmd_path: Optional[str] = None,
    fillets: Optional[list] = None,
    face_texts: Optional[list] = None,
    font_file: str = DEFAULT_FONT_FILE,
    progress=None,
) -> dict:
    """Genera il 3D di un modulo castellated come PCB vero e proprio.

    Quello che si vede in un modulo reale, e che qui viene ricostruito:

    * il **PCB** -- substrato FR4 tra due strati di soldermask, spesso
      `pcb_thickness`, con i semi-fori tagliati sul bordo;
    * i **contatti**: foro metallizzato (manicotto) piu' la piazzola dorata
      sulla faccia superiore e inferiore, cosi' si vedono da entrambi i lati;
    * la **serigrafia**: sigla accanto a ogni contatto (da `silk_labels`) e
      cornice del bordo;
    * i **componenti** letti dalla foto, ognuno con la forma del suo tipo
      (SOIC con reofori, trasformatore raccordato, LED con lente...) e
      l'eventuale marcatura stampata sopra.

    Non ci sono pin cilindrici: i contatti sono i semi-fori.
    """
    params.validate()

    half_length = params.body_length / 2
    half_width = params.body_width / 2
    radius = params.hole_diameter / 2
    # Il foro di un castellated non e' centrato sul bordo: e' un foro normale
    # forato vicino al bordo e poi tagliato via, quindi il suo centro sta
    # DENTRO la scheda di una frazione del diametro.
    inset = params.hole_inset if params.hole_inset is not None else round(radius * 0.76, 3)

    notches = []
    contacts = []
    silk_texts = []
    label_gap = radius + 0.55
    for placement in pad_positions(params):
        pad_len = placement.width if placement.edge in ("left", "right") else placement.height
        pad_w = placement.height if placement.edge in ("left", "right") else placement.width
        # Parte del pad che sta dentro la scheda: e' la piazzola dorata visibile.
        inner_len = max(0.6, pad_len - params.pad_overhang)
        if placement.edge == "left":
            centre = (-half_length + inset, placement.y)
            label = (-half_length + inner_len + label_gap, placement.y)
        elif placement.edge == "right":
            centre = (half_length - inset, placement.y)
            label = (half_length - inner_len - label_gap, placement.y)
        elif placement.edge == "top":
            centre = (placement.x, -half_width + inset)
            label = (placement.x, -half_width + inner_len + label_gap)
        else:
            centre = (placement.x, half_width - inset)
            label = (placement.x, half_width - inner_len - label_gap)
        notches.append({"x": centre[0], "y": centre[1], "radius": radius})
        contacts.append({
            "x": centre[0], "y": centre[1], "radius": radius,
            "number": placement.number, "edge": placement.edge,
            "pad_length_mm": inner_len, "pad_width_mm": pad_w,
            "color_hex": body.pin_color_hex,
        })
        text = params.silk_labels.get(placement.number, "").strip()
        if text and params.show_silk_labels_3d:
            silk_texts.append({
                "x": label[0], "y": label[1], "text": text, "size_mm": 0.7,
                "angle": 0, "color_hex": params.silk_color_hex,
            })

    # Cornice serigrafica: rientra dai bordi con i contatti quanto basta a
    # non passare sopra piazzole e sigle.
    edges_used = {c["edge"] for c in contacts}
    inset_x = (params.pad_length + 1.6) if edges_used & {"left", "right"} else 0.4
    inset_y = (params.pad_length + 1.6) if edges_used & {"top", "bottom"} else 0.4
    silk_outline = None
    if params.body_length - 2 * inset_x > 2 and params.body_width - 2 * inset_y > 2:
        silk_outline = {
            "x": 0.0, "y": 0.0,
            "length_mm": params.body_length - 2 * inset_x,
            "width_mm": params.body_width - 2 * inset_y,
        }

    # Il corpo e' il PCB del modulo; l'altezza totale di `body` resta il
    # riferimento per il blocco componenti quando non c'e' la foto.
    pcb = replace(body, height_mm=params.pcb_thickness)
    component_height = body.height_mm - params.pcb_thickness

    components = []
    for source_row, component in enumerate(params.components):
        if not component.is_usable():
            continue
        # An observed edge-mating connector can legitimately project beyond
        # the PCB. Do not move its measured centre to fit the entire envelope.
        if component.mating == "edge":
            x, y = component.x, component.y
        else:
            x, y = clamp_component_position(
                component.x, component.y, component.length, component.width,
                component.rotation, params.body_length, params.body_width,
            )
        render_kind = component.kind
        if render_kind == "generic" and component.detected_kind:
            render_kind = component.detected_kind
        if (component.detected_kind and render_kind in {"ic_soic", "ic_qfn"}
                and not component.terminal_geometry):
            # A verified family still does not establish terminal count/pitch.
            # Manually selected legacy packages have no photographic hint.
            render_kind = "ic"
        components.append({
            "source_row": source_row,
            "name": component.name,
            "kind": render_kind,
            "x": x, "y": y,
            "length_mm": component.length, "width_mm": component.width,
            "height_mm": min(component.height, component_height) if component_height > 0 else component.height,
            "color_hex": component.color_hex, "marking": component.marking,
            "rotation_deg": component.rotation,
            "pin_count": component.pin_count,
            "pin_count_confidence": component.pin_count_confidence,
            "pin_count_source": component.pin_count_source,
            "pins_by_side": component.pins_by_side,
            "terminal_geometry": component.terminal_geometry,
            "package": component.package,
            "detected_kind": component.detected_kind,
            "mating": component.mating,
            "bbox_basis": component.bbox_basis,
            "package_evidence": component.package_evidence,
            "model_selection": component.model_selection,
            "model_match": component.model_match,
            **_model_placement(component),
            **_match_evidence(component),
        })

    extra_boxes = []
    inset_c = params.component_inset
    if (not components and component_height > 0
            and params.body_length > 2 * inset_c and params.body_width > 2 * inset_c):
        # Senza l'elenco dei componenti resta il blocco unico: approssima
        # l'ingombro, che e' comunque meglio di una lastra nuda.
        extra_boxes.append({
            "name": "Componenti",
            "x": -params.body_length / 2 + inset_c,
            "y": -params.body_width / 2 + inset_c,
            "z": params.pcb_thickness,
            "length_mm": params.body_length - 2 * inset_c,
            "width_mm": params.body_width - 2 * inset_c,
            "height_mm": component_height,
            "color_hex": params.component_color_hex,
        })

    # Rilevato PRIMA di chiamare FreeCAD: e' un controllo geometrico sui dati
    # gia' pronti, non serve costruire il solido per accorgersene, e cosi'
    # l'avviso arriva anche se la sovrapposizione manda in errore una delle
    # forme (fillet/taglio che si scontrano) piu' avanti nello script.
    overlap_warnings = find_overlapping_components(components)

    # Modelli 3D reali dalla libreria di KiCad, quando il package e' noto e
    # l'ingombro lo conferma. La risoluzione avviene QUI e non dentro
    # freecad_parts: misurare un modello richiede a sua volta FreeCAD, e non
    # si puo' lanciarlo da dentro l'istanza che sta gia' costruendo il solido.
    model_notes = []
    def report_model(message):
        model_notes.append(message)
        if progress:
            progress(message)
    try:
        from .kicad_models import attach_models
        attach_models(components, log=report_model)
    except Exception as exc:  # noqa: BLE001 - la libreria e' un di piu', mai un requisito
        report_model(f"Libreria 3D KiCad non utilizzabile ({exc}); "
                           "si usano le forme approssimate.")

    from ..services.model_choices import save_candidate_report
    candidate_report = save_candidate_report(output_dir, components)
    result = _run_freecad(
        params.name, [], pcb, output_dir,
        freecadcmd_path=freecadcmd_path, fillets=fillets, face_texts=face_texts,
        font_file=font_file, notches=notches, extra_boxes=extra_boxes,
        edge_contacts=contacts,
        progress=progress,
        extra_payload={
            "substrate": {"color_hex": params.substrate_color_hex, "mask_thickness_mm": 0.02},
            "silk_texts": silk_texts,
            "silk_outline": silk_outline,
            "silk_color_hex": params.silk_color_hex,
            "components": components,
        },
    )
    result["warnings"] = overlap_warnings + result["warnings"]
    result["candidate_report_path"] = candidate_report
    _reconcile_model_imports(result, components, output_dir)
    # Annotazioni informative, distinte dagli avvisi: dicono quanti modelli
    # reali sono stati usati. Senza, l'esito della risoluzione non arriva
    # all'utente e il meccanismo sembra non essere entrato in funzione.
    result["notes"] = ([] if progress else model_notes) + list(result.get("notes", []))
    return result

def generate_3d_model(
    pin_params: PinHeaderParams,
    body: Body3DParams,
    output_dir: Path,
    freecadcmd_path: Optional[str] = None,
    fillets: Optional[list] = None,
    face_texts: Optional[list] = None,
    font_file: str = DEFAULT_FONT_FILE,
    progress=None,
) -> dict:
    """Genera <Nome>.step (e best-effort <Nome>.wrl) in output_dir/<Nome>.3dshapes/.

    `fillets` e `face_texts` sono liste di EdgeFilletAnnotation/FaceTextAnnotation
    (kicadgen.cad3d.annotations) applicate al corpo prima dell'export: fillet
    per primi (in ordine), poi il testo inciso sulle facce risultanti.

    Ritorna {"step_path": Path, "wrl_path": Path | None}.
    Solleva Cad3DError per problemi applicativi (freecadcmd assente, script
    FreeCAD fallito) con l'output di freecadcmd incluso per debug.
    """
    pin_params.validate()
    pins = [[pin, x, y] for pin, x, y in pin_positions(pin_params)]
    if body.populated_board or body.components:
        return _generate_populated_tht(pin_params, body, pins, output_dir,
            freecadcmd_path=freecadcmd_path, fillets=fillets, face_texts=face_texts,
            font_file=font_file, progress=progress)
    return _run_freecad(
        pin_params.name, pins, body, output_dir,
        freecadcmd_path=freecadcmd_path, fillets=fillets, face_texts=face_texts,
        font_file=font_file,
        progress=progress,
    )


def _generate_populated_tht(params, body, pins, output_dir, **options):
    """Build a populated PCB using matrix pin coordinates and centered photo boxes."""
    from .kicad_models import attach_models
    progress = options.get("progress")
    notes = []
    def report(message):
        notes.append(message)
        if progress:
            progress(message)
    thickness = body.pcb_thickness_mm or 1.6
    if body.pcb_thickness_mm is None:
        report("Spessore PCB stimato: 1.6 mm (non quotato).")
    pcb = replace(body, height_mm=thickness)
    centre_x = body.offset_x_mm + body.length_mm / 2
    centre_y = body.offset_y_mm + body.width_mm / 2
    components = []
    for source_row, c in enumerate(body.components or []):
        if not c.is_usable():
            report(f"Componente {c.name}: dimensioni non valide, escluso.")
            continue
        kind = c.kind
        if kind == "generic" and c.detected_kind:
            kind = c.detected_kind
        if c.detected_kind and kind in {"ic_soic", "ic_qfn"} and not c.terminal_geometry:
            kind = "ic"
        components.append({
            "source_row": source_row,
            "name": c.name, "kind": kind, "package": c.package, "mating": c.mating,
            "detected_kind": c.detected_kind,
            "bbox_basis": c.bbox_basis, "package_evidence": c.package_evidence,
            "model_selection": c.model_selection, "model_match": c.model_match,
            "render_3d": True,
            "x": centre_x + c.x, "y": centre_y + c.y,
            "length_mm": c.length, "width_mm": c.width, "height_mm": c.height,
            "rotation_deg": c.rotation, "color_hex": c.color_hex, "marking": c.marking,
            "pin_count": c.pin_count, "pins_by_side": c.pins_by_side,
            "pin_count_confidence": c.pin_count_confidence,
            "pin_count_source": c.pin_count_source,
            "terminal_geometry": c.terminal_geometry,
            **_model_placement(c),
            **_match_evidence(c),
        })
    attach_models(components, log=report)
    from ..services.model_choices import save_candidate_report
    candidate_report = save_candidate_report(output_dir, components)
    holes = [{"x": x, "y": y, "radius": params.drill / 2} for _, x, y in pins]
    contacts = [dict(hole, number=str(pin), edge="through",
                     pad_width_mm=params.pad_diameter, pad_length_mm=params.pad_diameter,
                     color_hex=body.pin_color_hex)
                for (pin, _, _), hole in zip(pins, holes)]
    result = _run_freecad(params.name, pins, pcb, output_dir,
        notches=holes, edge_contacts=contacts,
        extra_payload={"components": components,
                       "substrate": {"color_hex": "#2f3f1f", "mask_thickness_mm": .02}},
        **options)
    result["warnings"] = find_overlapping_components(components) + result.get("warnings", [])
    result["notes"] = [] if progress else notes
    result["candidate_report_path"] = candidate_report
    _reconcile_model_imports(result, components, output_dir)
    return result


def _reconcile_model_imports(result, components, output_dir):
    """A selected file is not an imported solid: expose runtime fallback in the JSON."""
    mesh_path = result.get('mesh_path')
    if not mesh_path or not Path(mesh_path).is_file():
        return
    try:
        mesh = json.loads(Path(mesh_path).read_text(encoding='utf-8'))
        failed = {part.get('source_row', part.get('component_index'))
                  for part in mesh.get('parts', []) if isinstance(part, dict)
                  and part.get('model_import_failed')}
        for index, component in enumerate(components):
            if component.get('source_row', index) not in failed:
                continue
            report = dict(component.get('model_match') or {})
            report.update(status='import_failed', selected_path=None,
                          error='Il file STEP selezionato non è stato importato; visualizzato un segnaposto.')
            component['model_match'] = report
            result.setdefault('warnings', []).append(f"{component.get('name')}: {report['error']}")
        if failed:
            from ..services.model_choices import save_candidate_report
            save_candidate_report(output_dir, components)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        result.setdefault('warnings', []).append(f'Verifica importazione modelli non disponibile: {exc}')


def _run_freecad(
    name: str,
    pins: list,
    body: Body3DParams,
    output_dir: Path,
    freecadcmd_path: Optional[str] = None,
    fillets: Optional[list] = None,
    face_texts: Optional[list] = None,
    font_file: str = DEFAULT_FONT_FILE,
    notches: Optional[list] = None,
    extra_boxes: Optional[list] = None,
    edge_contacts: Optional[list] = None,
    extra_payload: Optional[dict] = None,
    progress=None,
) -> dict:
    """Costruisce il JSON e lancia freecadcmd; comune a tutti i generatori 3D.

    `pins` sono i cilindri da aggiungere sotto il corpo (vuoto per i moduli
    castellated), `notches` i cilindri da SOTTRARRE al corpo per ricavare i
    semi-fori sul bordo, `extra_boxes` i solidi da aggiungere sopra il corpo
    (per i moduli, il blocco che rappresenta i componenti montati sul PCB).
    """
    body.validate()
    fillets = fillets or []
    face_texts = face_texts or []
    for f in fillets:
        f.validate()
    for t in face_texts:
        t.validate()

    freecadcmd = freecadcmd_path or find_freecadcmd()

    shapes_dir = output_dir / f"{name}.3dshapes"
    shapes_dir.mkdir(parents=True, exist_ok=True)
    step_path = shapes_dir / f"{name}.step"
    wrl_path = shapes_dir / f"{name}.wrl"
    mesh_path = shapes_dir / f"{name}.preview.json"
    fcstd_path = shapes_dir / f"{name}.FCStd"
    payload = {
        "body": {
            "length_mm": body.length_mm,
            "width_mm": body.width_mm,
            "height_mm": body.height_mm,
            "offset_x_mm": body.offset_x_mm,
            "offset_y_mm": body.offset_y_mm,
            "color_hex": body.color_hex,
        },
        "pins": pins,
        "notches": list(notches or []),
        "extra_boxes": list(extra_boxes or []),
        "edge_contacts": list(edge_contacts or []),
        "pin_diameter_mm": body.pin_diameter_mm,
        "pin_length_mm": body.pin_length_mm,
        "pin_color_hex": body.pin_color_hex,
        "step_path": str(step_path),
        "wrl_path": str(wrl_path),
        "mesh_path": str(mesh_path),
        "fcstd_path": str(fcstd_path),
        "fillets": [{"edge": f.edge, "radius_mm": f.radius_mm} for f in fillets],
        "face_texts": [
            {
                "face": t.face, "text": t.text, "size_mm": t.size_mm,
                "thickness_mm": t.thickness_mm, "color_hex": t.color_hex,
            }
            for t in face_texts
        ],
        "font_file": font_file,
    }
    payload.update(extra_payload or {})

    # Suffisso non ".json": freecadcmd tenta di aprire come documento ogni
    # argomento extra con estensione che riconosce come formato importabile
    # (incluso .json, per via dell'importer FEM YAML/JSON), causando
    # un'eccezione rumorosa (innocua, ma da evitare) dopo l'esecuzione dello script.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicadgen_params", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(payload, tmp)
        json_path = tmp.name

    try:
        if progress:
            from .process_log import run_logged
            result = run_logged(
                [freecadcmd, str(_SCRIPT_PATH), json_path],
                output_dir / "freecad.log", progress=progress,
                timeout=300, creationflags=_CFLAGS,
            )
        else:
            result = subprocess.run(
                [freecadcmd, str(_SCRIPT_PATH), json_path],
                capture_output=True, text=True, timeout=300,
                creationflags=_CFLAGS,
            )
    finally:
        try:
            os.unlink(json_path)
        except OSError:
            pass

    output = (result.stdout or "") + (result.stderr or "")

    if "KICADGEN_STEP_OK" not in output or not step_path.is_file():
        tail = "\n".join(output.strip().splitlines()[-15:]) or "(nessun output)"
        raise Cad3DError(f"Generazione STEP fallita.\n\nOutput freecadcmd:\n{tail}")

    # Avvisi dello script (fillet non applicabile, faccia non trovata...): la
    # generazione va comunque a buon fine, ma senza riportarli l'utente vedrebbe
    # un modello privo di un'annotazione senza capire perche'.
    warnings = [
        line.split("KICADGEN_WARN:", 1)[1].strip()
        for line in output.splitlines() if "KICADGEN_WARN:" in line
    ]

    # Colori nello STEP: Part.export scrive la sola geometria, e KiCad
    # mostrerebbe tutto grigio. Si aggiungono le entita' AP214 a partire
    # dai colori della mesh, che elenca i pezzi nello stesso ordine.
    if ("KICADGEN_STEP_COLORS_OK" not in output
            and "KICADGEN_MESH_OK" in output and mesh_path.is_file()):
        try:
            with open(mesh_path, "r", encoding="utf-8") as fh:
                parts = json.load(fh).get("parts", [])
            colors = [
                part.get("color", "#808080")
                for part in parts for _ in range(int(part.get("solids", 1)))
            ]
            step_text = step_path.read_text(encoding="utf-8", errors="replace")
            step_path.write_text(colorize_step(step_text, colors), encoding="utf-8")
        except (OSError, ValueError) as exc:
            warnings.append(f"STEP esportato senza colori: {exc}")

    return {
        "step_path": step_path,
        "fcstd_path": fcstd_path if "KICADGEN_FCSTD_OK" in output and fcstd_path.is_file() else None,
        "wrl_path": wrl_path if "KICADGEN_WRL_OK" in output and wrl_path.is_file() else None,
        "mesh_path": mesh_path if "KICADGEN_MESH_OK" in output and mesh_path.is_file() else None,
        "warnings": warnings,
    }
