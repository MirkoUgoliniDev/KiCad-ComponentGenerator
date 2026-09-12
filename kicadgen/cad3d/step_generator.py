"""Orchestrazione della generazione 3D: chiama freecadcmd in headless.

Costruisce un JSON con corpo+pin, lo passa a freecad_build_body.py (che gira
dentro l'interprete Python embedded di FreeCAD) via subprocess, e verifica
che i file di output siano stati effettivamente scritti.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from ..generators.pin_header import PinHeaderParams, pin_positions
from .annotations import EdgeFilletAnnotation, FaceTextAnnotation
from .body_params import Body3DParams

DEFAULT_FONT_FILE = r"C:\Windows\Fonts\arial.ttf"

_SCRIPT_PATH = Path(__file__).parent / "freecad_build_body.py"

_CFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


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
    body.validate()
    fillets = fillets or []
    face_texts = face_texts or []
    for f in fillets:
        f.validate()
    for t in face_texts:
        t.validate()

    freecadcmd = freecadcmd_path or find_freecadcmd()

    shapes_dir = output_dir / f"{pin_params.name}.3dshapes"
    shapes_dir.mkdir(parents=True, exist_ok=True)
    step_path = shapes_dir / f"{pin_params.name}.step"
    wrl_path = shapes_dir / f"{pin_params.name}.wrl"
    mesh_path = shapes_dir / f"{pin_params.name}.preview.json"

    pins = [[pin, x, y] for pin, x, y in pin_positions(pin_params)]
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
        "pin_diameter_mm": body.pin_diameter_mm,
        "pin_length_mm": body.pin_length_mm,
        "pin_color_hex": body.pin_color_hex,
        "step_path": str(step_path),
        "wrl_path": str(wrl_path),
        "mesh_path": str(mesh_path),
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

    return {
        "step_path": step_path,
        "wrl_path": wrl_path if "KICADGEN_WRL_OK" in output and wrl_path.is_file() else None,
        "mesh_path": mesh_path if "KICADGEN_MESH_OK" in output and mesh_path.is_file() else None,
        "warnings": warnings,
    }
