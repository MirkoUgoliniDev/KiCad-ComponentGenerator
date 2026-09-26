"""@brief Orchestrate 3D generation by running freecadcmd headless on a JSON job.

Orchestrazione della generazione 3D: chiama freecadcmd in headless.

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

from ..generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_THROUGH_HOLE, EDGE_LABELS, CastellatedModuleParams,
    continuous_pad, pad_positions, style_has_through_hole,
)
from ..generators.pin_header import PinHeaderParams, pin_positions
from .annotations import EdgeFilletAnnotation, FaceTextAnnotation
from .body_params import Body3DParams
from .process_log import ProcessCancelled, heartbeat_interval
from ..services.messages import CHILD_WARNING, child_warning_text, tx

DEFAULT_FONT_FILE = r"C:\Windows\Fonts\arial.ttf"

#: Secondi di SILENZIO tollerati dal figlio freecadcmd, non secondi di lavoro:
#: il conto riparte a ogni riga stampata (vedi process_log.run_logged). E' il
#: solo posto in cui il numero esiste; l'intervallo del battito dello script
#: FreeCAD si ricava da qui con `heartbeat_interval`, cosi' i due non possono
#: divergere.
FREECAD_SILENCE_TIMEOUT_S = 300

#: Testo unico dell'interruzione chiesta da chi guarda. Passa per `Cad3DError`,
#: cioe' per la STESSA via di uscita di ogni altro guasto della generazione 3D:
#: l'interfaccia ha gia' un percorso che riporta la finestra in quiete quando
#: il 3D non si completa, e un'interruzione e' un 3D non completato con un
#: motivo diverso, non un secondo meccanismo da mantenere in parallelo.
CAD_ABORT_MESSAGE = tx("Generazione 3D interrotta su richiesta dell'utente.")

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
              # Identita' della proposta e decisione di presenza: servono a
              # kicad_models._is_yolo_only_hypothesis per riconoscere per
              # struttura, non per nome, un'ipotesi del solo rilevatore.
              "_yolo_proposal_id", "_presence_reviewed_by_user",
              # Conteggi strutturali di reofori e terminali (3 reofori + aletta).
              "lead_count", "lead_count_complete",
              "terminal_layout", "terminal_count_basis", "_global_inventory_status", "class",
              # Riferimento asimmetrico osservato: il costruttore lo confronta
              # col baricentro del modello nativo per ricavare il verso.
              "landmark_feature", "landmark_side", "landmark_confidence")
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


def _is_finite_number(value) -> bool:
    """True for a finite int/float that is not a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def has_finite_geometry(component) -> bool:
    """@brief Tell whether a ComponentBox has finite position, size and rotation.

    @param component Object with x, y, length, width, height and rotation.
    @return False when any of them is missing, boolean, NaN or infinite.
    @note `ComponentBox.is_usable` only checks that the sizes are positive:
        NaN fails that test, but an infinite size and a NaN/inf position or
        rotation pass it and used to become invented coordinates (a NaN centre
        clamped to the board edge, a NaN rotation to the centre).
    """
    return all(_is_finite_number(getattr(component, field, None))
               for field in ("x", "y", "length", "width", "height", "rotation"))


def clamp_component_position(
    x: float, y: float, length: float, width: float, rotation: float,
    body_length: float, body_width: float, margin: float = COMPONENT_EDGE_MARGIN_MM,
) -> tuple[float, float]:
    """@brief Tiene il centro di un componente dentro il perimetro del modulo.

    Le stime di posizione dell'estrazione a volte cadono a cavallo del
    bordo (letto vicino al margine della foto, o con un componente grande
    vicino a un contatto): e' un vincolo geometrico generico, applicabile a
    qualunque componente di qualunque modulo, non una correzione per un
    pezzo particolare. Proietta il rettangolo ruotato sugli assi X/Y;
    a 90/270 gradi lunghezza e larghezza si scambiano.

    @return (x, y) clampati, in mm.
    @throws ValueError Se un valore non e' un numero finito: un NaN finiva
        spinto sul bordo e una rotazione NaN o una lunghezza infinita al
        centro, cioe' coordinate inventate.
    """
    if not all(_is_finite_number(value) for value in
               (x, y, length, width, rotation, body_length, body_width, margin)):
        raise ValueError("component position, size and rotation must be finite numbers")
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
    """@brief Return log warnings for pairs of components whose rotated outlines overlap.

    Coppie di componenti le cui sagome (gia' clampate, gia' ruotate) si
    sovrappongono, come avvisi pronti per il log.

    Le posizioni vengono dalla stima dell'estrazione, presa componente per
    componente: nulla impedisce a due stime indipendenti di finire una
    sopra l'altra. Non si tenta di risolverlo spostando qualcosa (sarebbe
    una scelta arbitraria, non geometria) -- si segnala la coppia, cosi'
    si corregge con «Modifica componenti…» invece di scoprirlo solo nel
    modello 3D finito.

    Il test degli assi separatori confronta i rettangoli orientati, evitando
    falsi positivi tra componenti diagonali con bounding box sovrapposte.
    """
    boxes = []
    for component in components:
        try:
            rotation = float(component.get("rotation_deg") or 0.0)
            length = float(component["length_mm"])
            width = float(component["width_mm"])
            x, y = float(component["x"]), float(component["y"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        # Un valore non finito non ha una sagoma da confrontare: NaN rendeva
        # falso ogni confronto e la coppia spariva senza avvisi. Il componente
        # e' gia' escluso a monte; qui non si inventa nulla.
        if not all(math.isfinite(value) for value in (rotation, length, width, x, y)):
            continue
        u, v = _rectangle_axes(rotation)
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
                    tx("componenti sovrapposti: '{name_a}' e '{name_b}' "
                       "({overlap_x:.2f}x{overlap_y:.2f}mm in comune) -- correggi posizione o "
                       "rotazione con «Modifica componenti…» nel tab Modello 3D.",
                       name_a=name_a,
                       name_b=name_b,
                       overlap_x=overlap_x,
                       overlap_y=overlap_y)
                )
    return warnings


def _hex_to_rgb(color: str) -> tuple[float, float, float]:
    try:
        h = color.lstrip("#")
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    except (ValueError, IndexError):
        return (0.5, 0.5, 0.5)


def colorize_step(step_text: str, colors: list) -> str:
    """@brief Add one colour per solid, in export order, to an AP214 STEP text.

    Aggiunge a uno STEP AP214 un colore per ogni solido, nell'ordine di export.

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
        raise ValueError(tx("nessun solido nello STEP"))
    if len(breps) != len(colors):
        raise ValueError(tx("{n_breps} solidi nello STEP ma {n_colors} colori",
                            n_breps=len(breps),
                            n_colors=len(colors)))
    context = re.search(r"#(\d+) = \( GEOMETRIC_REPRESENTATION_CONTEXT", step_text)
    if not context:
        raise ValueError(tx("contesto geometrico non trovato nello STEP"))
    if "END-ISO-10303-21;" not in step_text:
        raise ValueError(tx("STEP senza terminatore"))

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
    """@brief Application error: freecadcmd missing or 3D generation failed or cancelled.

    Errore applicativo: freecadcmd non trovato, o generazione fallita.
    """


def child_environment() -> dict:
    """@brief Environment for every freecadcmd child launched by this package.

    @return A copy of os.environ with PYTHONDONTWRITEBYTECODE=1.
    @note The FreeCAD scripts are imported from the program folder, which is
        read-only once installed: without this the child would try to write
        __pycache__ next to them (and leave stale bytecode in a checkout).
    """
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


def _output_text(value) -> str:
    """Decode subprocess output that may be None, bytes or str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _timeout_error(exc) -> "Cad3DError":
    """@brief Turn a FreeCAD watchdog timeout into the application's CAD error.

    @param exc subprocess.TimeoutExpired raised by run_logged or subprocess.run.
    @return Cad3DError carrying the last lines of the child's output, so the
        interface shows the FreeCAD tail instead of an "unexpected error".
    """
    output = _output_text(getattr(exc, "output", None)) + _output_text(getattr(exc, "stderr", None))
    tail = "\n".join(output.strip().splitlines()[-15:]) or tx("(nessun output)")
    return Cad3DError(tx("Generazione STEP interrotta: FreeCAD non ha prodotto output per "
                         "{timeout} s.\n\nOutput freecadcmd:\n{tail}",
                         timeout=getattr(exc, "timeout", FREECAD_SILENCE_TIMEOUT_S), tail=tail))


def find_freecadcmd() -> str:
    """@brief Locate freecadcmd via FREECAD_CMD_PATH or typical installation folders; raise Cad3DError if absent.

    Cerca freecadcmd: variabile d'ambiente FREECAD_CMD_PATH, poi le
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
        tx("freecadcmd non trovato. Installa FreeCAD, oppure imposta la variabile "
           "d'ambiente FREECAD_CMD_PATH con il percorso completo di "
           "freecadcmd.exe.")
    )


#: Margine fra la fine di una piazzola e il centro della sua sigla serigrafata,
#: oltre al raggio del foro: e' la convenzione con cui il 3D dispone le sigle
#: (e che contact_zones replica per mascherarle). Non cambia fra i tipi di
#: contatto: cambia solo dove finisce la piazzola.
SILK_LABEL_MARGIN_MM = 0.55


def castellated_contact_payload(params: CastellatedModuleParams, pin_color_hex: str):
    """@brief Describe every contact of a castellated module for the FreeCAD script.

    @param params Validated module description.
    @param pin_color_hex Colour of every plated feature (same metal everywhere).
    @return (notches, contacts, silk_texts, notes):
        `notches` are the cylinders subtracted from the board; `contacts` the
        plated features, one entry per contact; `silk_texts` the optional
        labels; `notes` human-readable lines about estimated quantities
        (Italian, for the log).
    @note No board silkscreen frame any more (user, 2026-09-26): it was a
        rectangle drawn inside the contacts, taken from no document or photo,
        and it read as a real feature of the board.
    @note Per contact style:
        * half_hole -- a notch of radius hole_diameter/2 at half_hole_inset,
          and an edge contact (sleeve + rectangular pads on both faces);
        * half_and_through -- the same, plus the closed hole's notch and, in
          the contact, a `through_hole` entry {x, y, radius, pad_diameter_mm}
          so the script plates it with a barrel and a ring on both faces. The
          entry is new: without it the hole was cut but never plated, and it
          rendered as a small dark dot instead of a gold ring. The contact
          also carries `stadium` {length_mm, width_mm}: ONE continuous gold
          pad per face, from the board edge inward to length_mm, width_mm
          wide (the same continuous_pad the footprint draws), which replaces
          the rectangular edge pads and the separate rings -- so there is no
          bare board between the half-hole and the through-hole;
        * through_hole -- NO edge notch (the board edge stays straight), the
          through-hole's notch, and a contact with edge="through" and
          board_edge set to the side, so the script builds barrel and rings
          and knows which way is "inside" for the label.
    @note Pure function: no FreeCAD, testable on its own.
    """
    half_length = params.body_length / 2
    half_width = params.body_width / 2
    radius = params.hole_diameter / 2
    # Il foro di un castellated non e' centrato sul bordo: e' un foro normale
    # forato vicino al bordo e poi tagliato via, quindi il suo centro sta
    # DENTRO la scheda di una frazione del diametro.
    inset = params.half_hole_inset
    drill_radius = params.thru_hole_drill / 2

    notches = []
    contacts = []
    silk_texts = []
    label_gap = radius + SILK_LABEL_MARGIN_MM
    for placement in pad_positions(params):
        if placement.style == CONTACT_THROUGH_HOLE:
            centre = (placement.x, placement.y)
            ring = params.thru_pad_diameter
            gap = drill_radius + SILK_LABEL_MARGIN_MM
            inward = {"left": (1, 0), "right": (-1, 0), "top": (0, 1), "bottom": (0, -1)}[placement.edge]
            label = (centre[0] + inward[0] * (ring / 2 + gap),
                     centre[1] + inward[1] * (ring / 2 + gap))
            notches.append({"x": centre[0], "y": centre[1], "radius": drill_radius})
            contacts.append({
                "x": centre[0], "y": centre[1], "radius": drill_radius,
                "number": placement.number, "edge": "through",
                "board_edge": placement.edge,
                "pad_length_mm": ring, "pad_width_mm": ring,
                "color_hex": pin_color_hex,
            })
        else:
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
            contact = {
                "x": centre[0], "y": centre[1], "radius": radius,
                "number": placement.number, "edge": placement.edge,
                "pad_length_mm": inner_len, "pad_width_mm": pad_w,
                "color_hex": pin_color_hex,
            }
            if placement.style == CONTACT_HALF_AND_THROUGH and params.has_thru_holes:
                # Il foro intero non tocca il bordo: e' l'altra meta' del
                # contatto, quella in cui si salda uno strip. Va tagliato E
                # metallizzato: stesso numero, stesso metallo, stesso solido.
                hole_x, hole_y = params.thru_hole_centre(placement)
                notches.append({"x": hole_x, "y": hole_y, "radius": drill_radius})
                contact["through_hole"] = {
                    "x": hole_x, "y": hole_y, "radius": drill_radius,
                    "pad_diameter_mm": params.thru_pad_diameter,
                }
                # Rame continuo dal bordo fino oltre la corona, come sulla
                # scheda reale: sostituisce piazzola di bordo e corone.
                # Stesse quote della piazzola del footprint (continuous_pad),
                # misurate dal bordo verso l'interno: il modulo non ha rame
                # oltre il proprio bordo, lo sbalzo e' della scheda madre.
                land = continuous_pad(params, placement)
                contact["stadium"] = {"length_mm": land.inner_reach, "width_mm": land.across}
                inward = {"left": (1, 0), "right": (-1, 0), "top": (0, 1), "bottom": (0, -1)}[placement.edge]
                edge_x = centre[0] - inward[0] * inset
                edge_y = centre[1] - inward[1] * inset
                depth = max(inner_len, land.inner_reach) + label_gap
                label = (edge_x + inward[0] * depth, edge_y + inward[1] * depth)
            contacts.append(contact)
        text = params.silk_labels.get(placement.number, "").strip()
        if text and params.show_silk_labels_3d:
            silk_texts.append({
                "x": label[0], "y": label[1], "text": text, "size_mm": 0.7,
                "angle": 0, "color_hex": params.silk_color_hex,
            })

    notes = []
    for side in params.sides:
        if side.contact_style != CONTACT_THROUGH_HOLE:
            continue
        label = EDGE_LABELS.get(side.edge, side.edge)
        value = params.through_hole_inset(side.edge)
        source = params.through_hole_inset_source(side.edge)
        if source == "fallback":
            notes.append(
                tx("Lato {label}: distanza dal bordo dei fori passanti non dichiarata, "
                   "stimata {value:.2f}mm (raggio della corona piu' la parete minima). E' "
                   "una stima per difetto: indica la distanza fra le file o la distanza "
                   "dal bordo per averla esatta.",
                   label=label,
                   value=value))
        elif source == "row_spacing":
            notes.append(
                tx("Lato {label}: fori passanti a {value:.2f}mm dal bordo, ricavati da "
                   "corpo {edge_extent}mm e distanza fra le file {row_spacing}mm.",
                   label=label,
                   value=value,
                   edge_extent=params.edge_extent(side.edge),
                   row_spacing=params.row_spacing))
    return notches, contacts, silk_texts, notes


def castellated_header_pins(params: CastellatedModuleParams):
    """@brief Where a male header can be soldered on a castellated module.

    @param params Validated module description.
    @return (pins, pitches, skipped): `pins` a list of [number, x, y] at the
        CLOSED through-hole centre of every contact that has one (footprint
        coordinates, mm, Y down); `pitches` the declared pitch (mm) of the
        side of each pin, aligned with `pins`; `skipped` the numbers of the
        contacts that have no closed through-hole (half-hole only).
    @note Rule (user decision, 2026-09-26): a strip is soldered only in a real
        through-hole -- contact style through_hole or half_and_through AND a
        declared hole (params.has_thru_holes: drill and ring > 0, the same
        condition under which the 3D cuts and plates the hole). A half-hole
        alone cannot hold a pin. The centre comes from
        params.thru_hole_centre(), the function the footprint and the 3D
        contacts already use, never from a second formula.
    @note A side with a single contact may declare no pitch (0): the
        reference 2.54 mm header is drawn, which is an estimate.
    """
    from .header_geometry import REFERENCE_PITCH_MM
    placements = iter(pad_positions(params))
    pins, pitches, skipped = [], [], []
    for side in params.sides:
        pitch = side.pitch if side.pitch > 0 else REFERENCE_PITCH_MM
        for _number in side.numbers():
            placement = next(placements)
            if style_has_through_hole(placement.style) and params.has_thru_holes:
                x, y = params.thru_hole_centre(placement)
                pins.append([placement.number, x, y])
                pitches.append(pitch)
            else:
                skipped.append(placement.number)
    return pins, pitches, skipped


def generate_castellated_3d_model(
    params: CastellatedModuleParams,
    body: Body3DParams,
    output_dir: Path,
    freecadcmd_path: Optional[str] = None,
    fillets: Optional[list] = None,
    face_texts: Optional[list] = None,
    font_file: str = DEFAULT_FONT_FILE,
    progress=None,
    model_progress=None,
    cancel=None,
) -> dict:
    """@brief Genera il 3D di un modulo castellated come PCB vero e proprio.

    `cancel` e' il canale di rinuncia (vedi `_run_freecad`): un
    `threading.Event` che chi ha avviato il lavoro mette a uno per fermarlo.

    Quello che si vede in un modulo reale, e che qui viene ricostruito:

    * il **PCB** -- substrato FR4 tra due strati di soldermask, spesso
      `pcb_thickness`, con i semi-fori tagliati sul bordo dei soli lati che
      li hanno (`CastellatedSide.contact_style`): un lato through_hole ha il
      bordo dritto;
    * i **contatti**: foro metallizzato (manicotto) piu' la piazzola dorata
      sulla faccia superiore e inferiore, cosi' si vedono da entrambi i lati;
      il foro passante e' metallizzato allo stesso modo, con una corona su
      entrambe le facce; un contatto half_and_through e' UNA piazzola dorata
      continua dal bordo fino oltre la corona, come sulla scheda reale (vedi
      castellated_contact_payload);
    * la **serigrafia**: sigla accanto a ogni contatto (da `silk_labels`) e
      cornice del bordo;
    * i **componenti** letti dalla foto, ognuno con la forma del suo tipo
      (SOIC con reofori, trasformatore raccordato, LED con lente...); la
      marcatura letta dalla foto NON viene stampata sopra, a meno che
      `body.show_markings_3d` non lo chieda: il percorso standard produce
      volumi generici senza scritte.

    Non ci sono pin cilindrici: i contatti sono semi-fori e fori passanti.
    Con `body.show_pins_3d` ogni contatto che ha un foro passante chiuso
    riceve uno strip maschio saldato sotto (vedi castellated_header_pins e
    header_geometry); i contatti a solo mezzo foro restano senza, e una nota
    lo dice quando la scheda ha entrambi i tipi.
    Le note del risultato dicono da dove viene la distanza dal bordo dei fori
    passanti, e se e' una stima.
    """
    params.validate()

    notches, contacts, silk_texts, inset_notes = castellated_contact_payload(
        params, body.pin_color_hex)

    # Strip maschi solo nei fori passanti veri (decisione dell'utente,
    # 2026-09-26). Senza pin da mostrare il payload resta quello di prima:
    # nessun pin e nessuno stile.
    header_pins, header_pitches, header_skipped = (
        castellated_header_pins(params) if getattr(body, "show_pins_3d", True) else ([], [], []))
    header_payload = ({"pin_style": "male_header", "header_pitches": header_pitches}
                      if header_pins else {})
    header_notes = []
    if header_pins and header_skipped:
        header_notes.append(tx(
            "Pin 3D: {count} contatti a solo mezzo foro ({numbers}) restano senza pin "
            "maschio, perche' uno strip si salda solo in un foro passante.",
            count=len(header_skipped), numbers=", ".join(header_skipped)))

    # Il corpo e' il PCB del modulo; l'altezza totale di `body` resta il
    # riferimento per il blocco componenti quando non c'e' la foto.
    pcb = replace(body, height_mm=params.pcb_thickness)
    component_height = body.height_mm - params.pcb_thickness

    components = []
    # Ogni valore cambiato qui (centro spostato dentro il modulo, altezza
    # stimata ridotta) e' una correzione di una stima: va detta, non fatta in
    # silenzio (regola del progetto sulle altezze stimate).
    placement_notes = []
    # Note di altezza ridotta in attesa dell'abbinamento ai modelli KiCad:
    # (dizionario del corpo, testo). Vedi sotto, dopo `attach_models`.
    height_notes = []
    for source_row, component in enumerate(params.components):
        if not has_finite_geometry(component):
            placement_notes.append(tx("Componente {name}: posizione, rotazione o quote non "
                                      "finite, escluso.", name=component.name))
            continue
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
            if abs(x - component.x) > 1e-9 or abs(y - component.y) > 1e-9:
                placement_notes.append(tx(
                    "Componente {name}: centro spostato da ({x0:.2f}, {y0:.2f}) a "
                    "({x1:.2f}, {y1:.2f}) mm per restare dentro il modulo.",
                    name=component.name, x0=component.x, y0=component.y, x1=x, y1=y))
        height = component.height
        height_note = None
        if 0 < component_height < component.height:
            height = component_height
            height_note = tx(
                "Componente {name}: altezza stimata {height:.2f} mm ridotta a {cap:.2f} mm "
                "(altezza del corpo meno lo spessore del PCB).",
                name=component.name, height=component.height, cap=component_height)
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
            "height_mm": height,
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
        if height_note:
            height_notes.append((components[-1], height_note))

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
        # Il canale si passa SOLO quando c'e', come a `run_logged`: una
        # sostituzione di `attach_models` scritta prima di questo parametro
        # non deve far fallire una generazione buona.
        attach_models(components, log=report_model, progress=model_progress,
                      **({"cancel": cancel} if cancel is not None else {}))
    except ProcessCancelled as exc:
        # La rinuncia NON e' un difetto della libreria 3D: se finisse nel ramo
        # sotto, verrebbe annunciata come «libreria non utilizzabile» e la
        # costruzione del solido partirebbe lo stesso, cioe' il pulsante
        # «Interrompi» non farebbe niente di visibile fino alla fine.
        raise Cad3DError(CAD_ABORT_MESSAGE) from exc
    except Exception as exc:  # noqa: BLE001 - la libreria e' un di piu', mai un requisito
        report_model(tx("Libreria 3D KiCad non utilizzabile ({exc}); si usano le forme "
                        "approssimate.",
                        exc=exc))
    # L'altezza ridotta vale solo per i corpi costruiti come forma
    # approssimata: un corpo che riceve un modello della libreria KiCad viene
    # importato con l'altezza del modello, quindi la nota lo darebbe per
    # schiacciato quando non lo e' (punto 66c, decisione dell'utente del
    # 2026-09-26). Se poi l'import del modello fallisce, FreeCAD ripiega sulla
    # forma approssimata con l'altezza ridotta; quel corpo e' gia' segnalato
    # come import fallito (magenta, avviso), non da questa nota.
    placement_notes.extend(note for rendered, note in height_notes
                           if not rendered.get("model_path"))

    # Rilevato PRIMA di chiamare FreeCAD: e' un controllo geometrico sui dati
    # gia' pronti, non serve costruire il solido per accorgersene, e cosi'
    # l'avviso arriva anche se la sovrapposizione manda in errore una delle
    # forme (fillet/taglio che si scontrano) piu' avanti nello script. DOPO
    # l'abbinamento, pero': e' li' che le ipotesi del solo rilevatore vengono
    # escluse dal 3D, e un corpo che non verra' costruito non si sovrappone a
    # nulla.
    overlap_warnings = find_overlapping_components(_rendered(components))

    from ..services.model_choices import save_candidate_report
    candidate_report = save_candidate_report(output_dir, components)
    result = _run_freecad(
        params.name, header_pins, pcb, output_dir,
        freecadcmd_path=freecadcmd_path, fillets=fillets, face_texts=face_texts,
        font_file=font_file, notches=notches, extra_boxes=extra_boxes,
        edge_contacts=contacts,
        progress=progress,
        cancel=cancel,
        extra_payload={
            "substrate": {"color_hex": params.substrate_color_hex, "mask_thickness_mm": 0.02},
            "silk_texts": silk_texts,
            "silk_color_hex": params.silk_color_hex,
            "components": components,
            **header_payload,
        },
    )
    result["warnings"] = overlap_warnings + result["warnings"]
    result["candidate_report_path"] = candidate_report
    _reconcile_model_imports(result, components, output_dir)
    # Annotazioni informative, distinte dagli avvisi: dicono quanti modelli
    # reali sono stati usati. Senza, l'esito della risoluzione non arriva
    # all'utente e il meccanismo sembra non essere entrato in funzione.
    result["notes"] = ([] if progress else model_notes) + list(result.get("notes", []))
    # La provenienza della distanza dal bordo dei fori passanti non e' un
    # dettaglio: quando e' una stima deve arrivare all'utente, sempre.
    result["notes"] = inset_notes + header_notes + placement_notes + result["notes"]
    return result


def _rendered(components):
    """Components that the FreeCAD script will actually build."""
    return [component for component in components
            if component.get("render_3d", True) is not False]

def generate_3d_model(
    pin_params: PinHeaderParams,
    body: Body3DParams,
    output_dir: Path,
    freecadcmd_path: Optional[str] = None,
    fillets: Optional[list] = None,
    face_texts: Optional[list] = None,
    font_file: str = DEFAULT_FONT_FILE,
    progress=None,
    model_progress=None,
    cancel=None,
) -> dict:
    """@brief Generate the STEP (and best-effort WRL) model of a pin header body in output_dir.

    Genera <Nome>.step (e best-effort <Nome>.wrl) in output_dir/<Nome>.3dshapes/.

    `fillets` e `face_texts` sono liste di EdgeFilletAnnotation/FaceTextAnnotation
    (kicadgen.cad3d.annotations) applicate al corpo prima dell'export: fillet
    per primi (in ordine), poi il testo inciso sulle facce risultanti.

    `cancel` e' il canale di rinuncia (vedi `_run_freecad`).

    Ritorna {"step_path": Path, "wrl_path": Path | None}.
    Solleva Cad3DError per problemi applicativi (freecadcmd assente, script
    FreeCAD fallito, interruzione chiesta dall'utente) con l'output di
    freecadcmd incluso per debug.
    """
    pin_params.validate()
    pins = [[pin, x, y] for pin, x, y in pin_positions(pin_params)]
    if body.populated_board or body.components:
        return _generate_populated_tht(pin_params, body, pins, output_dir,
            freecadcmd_path=freecadcmd_path, fillets=fillets, face_texts=face_texts,
            font_file=font_file, progress=progress, model_progress=model_progress,
            cancel=cancel)
    return _run_freecad(
        pin_params.name, pins, body, output_dir,
        freecadcmd_path=freecadcmd_path, fillets=fillets, face_texts=face_texts,
        font_file=font_file,
        progress=progress,
        cancel=cancel,
    )


def _generate_populated_tht(params, body, pins, output_dir, **options):
    """Build a populated PCB using matrix pin coordinates and centered photo boxes."""
    from .kicad_models import attach_models
    progress = options.get("progress")
    # Estratto, non letto: piu' avanti `options` viene inoltrato tale e quale
    # a _run_freecad, che non conosce questo callback e rifiuterebbe la chiamata.
    model_progress = options.pop("model_progress", None)
    notes = []
    def report(message):
        notes.append(message)
        if progress:
            progress(message)
    thickness = body.pcb_thickness_mm or 1.6
    if body.pcb_thickness_mm is None:
        report(tx("Spessore PCB stimato: 1.6 mm (non quotato)."))
    pcb = replace(body, height_mm=thickness)
    centre_x = body.offset_x_mm + body.length_mm / 2
    centre_y = body.offset_y_mm + body.width_mm / 2
    components = []
    for source_row, c in enumerate(body.components or []):
        if not has_finite_geometry(c):
            report(tx("Componente {name}: posizione, rotazione o quote non finite, escluso.",
                      name=c.name))
            continue
        if not c.is_usable():
            report(tx("Componente {name}: dimensioni non valide, escluso.", name=c.name))
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
    # `options` viaggia intatto fino a `_run_freecad`, che vuole ancora il
    # canale: qui si LEGGE soltanto. Stessa regola del sito castellated --
    # una sola richiesta copre misura dei candidati e costruzione del solido.
    cancel = options.get("cancel")
    try:
        attach_models(components, log=report, progress=model_progress,
                      **({"cancel": cancel} if cancel is not None else {}))
    except ProcessCancelled as exc:
        raise Cad3DError(CAD_ABORT_MESSAGE) from exc
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
                       "substrate": {"color_hex": "#2f3f1f", "mask_thickness_mm": .02},
                       # Una scheda popolata si collega con strip maschi saldati
                       # sotto (utente, 2026-09-26): pin quadrati e distanziale
                       # nero, non cilindri (vedi header_geometry).
                       "pin_style": "male_header"},
        **options)
    result["warnings"] = (find_overlapping_components(_rendered(components))
                          + result.get("warnings", []))
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
                          error=tx("Il file STEP selezionato non è stato importato; visualizzato un "
                                   "segnaposto."))
            component['model_match'] = report
            result.setdefault('warnings', []).append(f"{component.get('name')}: {report['error']}")
        if failed:
            from ..services.model_choices import save_candidate_report
            save_candidate_report(output_dir, components)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        result.setdefault('warnings', []).append(tx("Verifica importazione modelli non disponibile: {exc}", exc=exc))


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
    cancel=None,
) -> dict:
    """Costruisce il JSON e lancia freecadcmd; comune a tutti i generatori 3D.

    `pins` sono i cilindri da aggiungere sotto il corpo (vuoto per i moduli
    castellated), `notches` i cilindri da SOTTRARRE al corpo per ricavare i
    semi-fori sul bordo, `extra_boxes` i solidi da aggiungere sopra il corpo
    (per i moduli, il blocco che rappresenta i componenti montati sul PCB).

    `cancel` e' il canale di rinuncia: un oggetto con `is_set()`/`wait()`
    (tipicamente `threading.Event`) che chi ha avviato il lavoro mette a uno
    per chiedere di fermarsi. Il figlio freecadcmd viene ucciso dalla stessa
    macchina che lo uccide quando tace troppo a lungo (process_log.run_logged)
    e la chiamata esce con `Cad3DError(CAD_ABORT_MESSAGE)`, cioe' per la via
    con cui l'interfaccia riporta gia' la finestra in quiete.

    Lo STESSO canale copre le due meta' della fase 3D: l'abbinamento dei
    modelli di libreria, che precede questa chiamata, lo riceve da chi chiama
    (`attach_models` -> `model_candidates` -> `model_sizes` ->
    `kicad_models.measure_models`) e uccide il proprio figlio freecadcmd --
    quello che misura i candidati non ancora in cache -- invece di aspettare
    che il lotto finisca. Una rinuncia chiesta durante l'abbinamento esce
    quindi subito, per la stessa via (`Cad3DError(CAD_ABORT_MESSAGE)`), e
    questa funzione non avvia nemmeno il figlio del solido.
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
        # Le marcature restano nel payload dei componenti (servono a chi
        # abbina i modelli di libreria e a chi rilegge il JSON), ma lo
        # script FreeCAD le stampa solo se il corpo lo chiede esplicitamente.
        "show_markings_3d": bool(body.show_markings_3d),
        "show_pins_3d": bool(getattr(body, "show_pins_3d", True)),
    }
    payload.update(extra_payload or {})

    # Canale di trasporto del battito: lo script FreeCAD non puo' importare
    # questo modulo (gira nell'interprete embedded), quindi l'intervallo
    # viaggia nel JSON invece di essere riscritto a mano la' dentro. Scritto
    # DOPO l'update di extra_payload: un chiamante non deve poter scollegare
    # per sbaglio il battito dal watchdog.
    #
    # Lo stesso numero fa da budget alla misura del verso di un modello
    # nativo (freecad_parts._landmark_budget_s): una misura che stesse muta
    # piu' a lungo dell'intervallo fra due battiti potrebbe, da sola,
    # trasformare una corsa sana nel silenzio che il watchdog uccide. Una
    # manopola sola, quindi non possono divergere.
    payload["heartbeat_interval_s"] = heartbeat_interval(FREECAD_SILENCE_TIMEOUT_S)
    # Cartelle delle cache utente (indice dei versi dei modelli nativi): il
    # figlio non puo' importare kicadgen.app_paths, quindi le riceve qui, con
    # KICADGEN_DATA_DIR gia' applicato.
    from . import model_size_cache
    payload["cache_dirs"] = [str(folder) for folder in model_size_cache.user_cache_dirs()]

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
        if cancel is not None and cancel.is_set():
            # Rinuncia gia' chiesta durante l'abbinamento dei modelli: non si
            # avvia un processo che andrebbe ucciso al primo respiro.
            raise Cad3DError(CAD_ABORT_MESSAGE)
        if progress:
            from .process_log import ProcessCancelled, run_logged
            # freecadcmd, finito lo script, prova ad aprire come documento ogni
            # argomento in piu' -- cioe' il NOSTRO file di parametri -- e scrive
            # «File format not supported: <quel file>». E' atteso per
            # costruzione e innocuo: resta in freecad.log, ma non arriva alla
            # finestra, dove sembrava un errore (utente, 2026-09-26). Il
            # criterio e' il percorso esatto del file che passiamo noi, non il
            # testo di FreeCAD: una riga che nomina altri file passa sempre.
            params_names = {json_path, json_path.replace("\\", "/"), Path(json_path).name}
            user_progress = progress

            def progress(line, _forward=user_progress):  # noqa: F811 - filtro dichiarato sopra
                if "not supported" in str(line) and any(n in str(line) for n in params_names):
                    return
                _forward(line)
            # Secondi di SILENZIO, non di lavoro: il conto riparte a ogni riga
            # che FreeCAD stampa, e ogni passo lungo batte (vedi
            # heartbeat_interval_s nel payload qui sopra). Un limite sulla
            # durata complessiva uccideva il lavoro dei componenti successivi
            # per colpa del primo import lento, ed era tanto piu' probabile
            # quanto piu' grande era il modulo.
            try:
                # Il canale si passa SOLO quando c'e': cosi' la firma che
                # vedono i chiamanti non interattivi (e le loro sostituzioni
                # nei test) resta quella di prima.
                result = run_logged(
                    [freecadcmd, str(_SCRIPT_PATH), json_path],
                    output_dir / "freecad.log", progress=progress,
                    timeout=FREECAD_SILENCE_TIMEOUT_S, creationflags=_CFLAGS,
                    **({"cancel": cancel} if cancel is not None else {}),
                )
            except ProcessCancelled as exc:
                raise Cad3DError(CAD_ABORT_MESSAGE) from exc
            except subprocess.TimeoutExpired as exc:
                raise _timeout_error(exc) from exc
        else:
            # LIMITE DICHIARATO: senza `progress` nessuno legge le righe, quindi
            # qui il timeout resta quello che subprocess.run sa fare -- la
            # DURATA COMPLESSIVA -- e il battito non lo puo' salvare. E' la via
            # usata solo da script e prove non interattive, dove non c'e' una
            # modale da informare; l'interfaccia passa sempre `progress`. Per
            # renderlo superfluo servirebbe leggere lo stdout anche qui.
            # Il figlio scrive UTF-8 (stdout riconfigurato, avvisi JSON): il
            # codec della localita' (cp1252) ne storpiava le lettere accentate.
            try:
                result = subprocess.run(
                    [freecadcmd, str(_SCRIPT_PATH), json_path],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=FREECAD_SILENCE_TIMEOUT_S, creationflags=_CFLAGS,
                    env=child_environment(),
                )
            except subprocess.TimeoutExpired as exc:
                raise _timeout_error(exc) from exc
    finally:
        try:
            os.unlink(json_path)
        except OSError:
            pass

    output = (result.stdout or "") + (result.stderr or "")

    if "KICADGEN_STEP_OK" not in output or not step_path.is_file():
        tail = "\n".join(output.strip().splitlines()[-15:]) or tx("(nessun output)")
        raise Cad3DError(tx("Generazione STEP fallita.\n\nOutput freecadcmd:\n{tail}", tail=tail))

    # Avvisi dello script (fillet non applicabile, faccia non trovata...): la
    # generazione va comunque a buon fine, ma senza riportarli l'utente vedrebbe
    # un modello privo di un'annotazione senza capire perche'.
    warnings = [
        child_warning_text(line.split(CHILD_WARNING, 1)[1])
        for line in output.splitlines() if CHILD_WARNING in line
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
            warnings.append(tx("STEP esportato senza colori: {exc}", exc=exc))

    return {
        "step_path": step_path,
        "fcstd_path": fcstd_path if "KICADGEN_FCSTD_OK" in output and fcstd_path.is_file() else None,
        "wrl_path": wrl_path if "KICADGEN_WRL_OK" in output and wrl_path.is_file() else None,
        "mesh_path": mesh_path if "KICADGEN_MESH_OK" in output and mesh_path.is_file() else None,
        "warnings": warnings,
    }
