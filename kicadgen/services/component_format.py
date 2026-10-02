"""@brief «Copia formato» of a mounted component: copy its shape and model, paste them on others.

User request (2026-09-27): after fixing one component (size, type, KiCad
model), apply the same «format» to the identical components of the board
instead of correcting each one by hand. The position, the orientation on the
board, the name, the marking read on the photograph and the colour stay those
of each target (colour: user decision of the same day, it is photo data).

Pure data functions on the stored photo rows, no Qt and no FreeCAD, so the
3D view only chooses the rows and the rules can be tested alone.
"""
from __future__ import annotations

from copy import deepcopy

from ..cad3d.lead_sides import local_lead_sides

#: Fields copied as they are. Lengths are in the component's own frame
#: (length along its local X, width along its local Y, before the rotation),
#: so a body pasted on a rotated target keeps the target's orientation.
FORMAT_FIELDS = (
    "length_mm", "width_mm", "height_mm",
    "kind", "class", "_detected_kind", "package",
    "model_rotation_deg", "model_rotation_delta_deg", "model_z_offset_mm",
    "pin_count", "pin_count_confidence", "pin_count_source",
)


def copy_format(row: dict) -> dict:
    """@brief The format of one stored photo component.

    @param row Stored photo component (`MainWindow._photo_rows` entry).
    @return Dictionary with the FORMAT_FIELDS present in the row, the KiCad
        model as a CONFIRMED model_selection when the row has one (an explicit
        choice, or the path an automatic match attached), the observed leads
        in the component's local frame and the source name.
    @note The model becomes a confirmed choice because the user copies it on
        purpose from a component they judged right (user decision 2026-09-27).
    """
    fmt = {key: deepcopy(row[key]) for key in FORMAT_FIELDS if key in row}
    selection = row.get("model_selection")
    if isinstance(selection, dict) and selection.get("status") == "confirmed" and selection.get("path"):
        fmt["model_selection"] = deepcopy(selection)
    else:
        match = row.get("model_match") if isinstance(row.get("model_match"), dict) else {}
        path = match.get("selected_path") or row.get("model_path")
        if isinstance(path, str) and path.strip():
            fmt["model_selection"] = {"status": "confirmed", "path": path,
                                      "rotation_deg": row.get("model_rotation_deg")}
    sides = local_lead_sides(row.get("pins_by_side"), float(row.get("rotation_deg") or 0.0))
    if sides:
        fmt["lead_sides_local"] = sides
    fmt["source_name"] = str(row.get("name") or "").strip()
    return fmt


def paste_format(row: dict, fmt: dict) -> dict:
    """@brief A copy of `row` carrying the copied format.

    @param row Stored photo component that receives the format.
    @param fmt Result of copy_format.
    @return New row: every FORMAT_FIELDS key and the model take the source's
        value (a key the source lacks is removed, so the target becomes the
        same body and not a mix of the two); the observed leads are turned
        from the source's local frame into the target's photo frame. Position,
        rotation, name, marking and colour are untouched; the input is not
        modified. The target's SMD network identity and pose are withdrawn
        (smd_component_classifier.drop_classifier_identity).
    """
    result = deepcopy(row)
    for key in FORMAT_FIELDS:
        if key in fmt:
            result[key] = deepcopy(fmt[key])
        else:
            result.pop(key, None)
    if "model_selection" in fmt:
        result["model_selection"] = deepcopy(fmt["model_selection"])
    else:
        result.pop("model_selection", None)
    # La rete SMD aveva letto il corpo di DESTINAZIONE come un'altra cosa: il
    # formato incollato la smentisce, quindi identita' e posa della rete non
    # valgono piu' (audit 2026-10-02: un SOIC prendeva il verso da una
    # lettura «chip_R»).
    from .smd_component_classifier import drop_classifier_identity
    drop_classifier_identity(result)
    local = fmt.get("lead_sides_local")
    # Il verso inverso di local_lead_sides: dal riferimento del corpo alla
    # foto, con la rotazione del BERSAGLIO.
    sides = (local_lead_sides(local, -float(result.get("rotation_deg") or 0.0))
             if local else None)
    if sides:
        result["pins_by_side"] = sides
    else:
        result.pop("pins_by_side", None)
    if fmt.get("source_name"):
        result["_format_copied_from"] = fmt["source_name"]
    return result
