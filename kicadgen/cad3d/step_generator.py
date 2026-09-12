"""Orchestrazione della generazione 3D: chiama freecadcmd in headless.

Costruisce un JSON con corpo+pin, lo passa a freecad_build_body.py (che gira
dentro l'interprete Python embedded di FreeCAD) via subprocess, e verifica
che i file di output siano stati effettivamente scritti.
"""

from __future__ import annotations

import glob
import json
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
        if text:
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
    for component in params.components:
        if not component.is_usable():
            continue
        components.append({
            "name": component.name, "kind": component.kind,
            "x": component.x, "y": component.y,
            "length_mm": component.length, "width_mm": component.width,
            "height_mm": min(component.height, component_height) if component_height > 0 else component.height,
            "color_hex": component.color_hex, "marking": component.marking,
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

    return _run_freecad(
        params.name, [], pcb, output_dir,
        freecadcmd_path=freecadcmd_path, fillets=fillets, face_texts=face_texts,
        font_file=font_file, notches=notches, extra_boxes=extra_boxes,
        edge_contacts=contacts,
        extra_payload={
            "substrate": {"color_hex": params.substrate_color_hex, "mask_thickness_mm": 0.02},
            "silk_texts": silk_texts,
            "silk_outline": silk_outline,
            "silk_color_hex": params.silk_color_hex,
            "components": components,
        },
    )

def generate_3d_model(
    pin_params: PinHeaderParams,
    body: Body3DParams,
    output_dir: Path,
    freecadcmd_path: Optional[str] = None,
    fillets: Optional[list] = None,
    face_texts: Optional[list] = None,
    font_file: str = DEFAULT_FONT_FILE,
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
    return _run_freecad(
        pin_params.name, pins, body, output_dir,
        freecadcmd_path=freecadcmd_path, fillets=fillets, face_texts=face_texts,
        font_file=font_file,
    )


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
        result = subprocess.run(
            [freecadcmd, str(_SCRIPT_PATH), json_path],
            capture_output=True, text=True, timeout=120,
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
    if "KICADGEN_MESH_OK" in output and mesh_path.is_file():
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
