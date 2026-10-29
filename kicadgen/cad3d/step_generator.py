"""@brief Orchestrate 3D generation by running freecadcmd headless on a JSON job.

Builds a JSON with body+pins, passes it to freecad_build_body.py (which runs
inside FreeCAD's embedded Python interpreter) via subprocess, and checks that
the output files were actually written.
"""

from __future__ import annotations

import base64
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
    CONTACT_HALF_AND_THROUGH, CONTACT_THROUGH_HOLE, CastellatedModuleParams,
    continuous_pad, pad_positions, style_has_through_hole,
)
from ..generators.pin_header import PinHeaderParams, pin_positions
from ..core.package_geometry import PackageGeometry
from .annotations import FaceTextAnnotation
from .body_params import Body3DParams
from .header_reuse import HeaderModelReuse, REUSE_PROGRESS
from .measurement_uncertainty import (
    ASSUMED_SCALE_PX_PER_MM, SCALE_KEY, SOURCE_ASSUMED, SOURCE_DECLARED, UNCERTAINTY_KEY,
    component_edge_uncertainty, side_uncertainty_mm,
)
from ..core.output_names import validate_output_name
from .process_log import NO_WINDOW_FLAGS, ProcessCancelled, heartbeat_interval
from ..services.messages import CHILD_WARNING, child_warning_text, tx
from ..fsutil import replace_file
from ..core.validation import is_finite_number
from ..kicad_versions import newest_version_first, parse_version
from .lead_sides import local_lead_sides

DEFAULT_FONT_FILE = r"C:\Windows\Fonts\arial.ttf"

#: Seconds of SILENCE tolerated from the freecadcmd child, not seconds of work:
#: the count restarts at every printed line (see process_log.run_logged). It is
#: the only place where the number exists; the heartbeat interval of the
#: FreeCAD script is derived from here with `heartbeat_interval`, so the two
#: cannot diverge.
FREECAD_SILENCE_TIMEOUT_S = 300

#: Single text of the interruption requested by whoever is watching. It goes
#: through `Cad3DError`, i.e. through the SAME exit path as every other fault of
#: the 3D generation: the interface already has a path that brings the window
#: back to rest when the 3D does not complete, and an interruption is an
#: uncompleted 3D with a different reason, not a second mechanism to maintain
#: in parallel.
CAD_ABORT_MESSAGE = tx("Generazione 3D interrotta su richiesta dell'utente.")

_SCRIPT_PATH = Path(__file__).parent / "freecad_build_body.py"

_CFLAGS = NO_WINDOW_FLAGS


#: Minimum distance kept between a component's edge and the module's edge,
#: when the component is brought back inside (see `place_component`).
COMPONENT_EDGE_MARGIN_MM = 0.2


def _match_evidence(component):
    """Copy only matching observations; evidence cannot replace CAD placement."""
    evidence = getattr(component, "match_evidence", None)
    if not isinstance(evidence, dict):
        return {}
    fields = ("presence_confidence", "class_confidence", "confidence",
              "_smd_class", "_smd_rotation_deg", "_smd_rotation_confidence",
              "_smd_accepted", "_smd_body", "_smd_rotation_kind", "_smd_views_agree",
              "_smd_terminal_count",
              "_yolo_review_only", "_yolo_origin", "terminal_count",
              # Identity of the proposal and presence decision: they let
              # kicad_models._is_yolo_only_hypothesis recognise by
              # structure, not by name, a hypothesis of the detector alone.
              "_yolo_proposal_id", "_presence_reviewed_by_user",
              # Structural counts of leads and terminals (3 leads + tab).
              "lead_count", "lead_count_complete",
              "terminal_layout", "terminal_count_basis", "_global_inventory_status", "class",
              # Observed asymmetric reference: the builder compares it
              # with the centroid of the native model to derive the direction.
              "landmark_feature", "landmark_side", "landmark_confidence",
              # Reliability of the marking reading: a «low» marking does not
              # become a search key for the models (kicad_models).
              "marking_confidence",
              # Uncertainty of the photo measure (mm per edge) or scale
              # of the photo (px/mm): they decide whether two envelopes are the same
              # size (archive of the matches, kicad_models).
              UNCERTAINTY_KEY, SCALE_KEY,
              # Compact, transient OpenCV descriptor from the calibrated PCB
              # crop; never the source photo or a persisted project field.
              "_accepted_image_descriptor", "_category_source", "_category_status",
              "_ai_class_proposal", "_ai_class_confidence", "_terminal_status", "_crop_class_status", "_crop_terminal_review_required")
    result = {key: evidence[key] for key in fields if key in evidence}
    descriptor = result.get("_accepted_image_descriptor")
    if isinstance(descriptor, (bytes, bytearray, memoryview)):
        # This evidence is written into the FreeCAD job JSON.
        result["_accepted_image_descriptor"] = "base64:" + base64.b64encode(bytes(descriptor)).decode("ascii")
    return result


def _model_placement(component):
    """Keep only explicit finite native-model overrides; None means infer axes.

    The choices made by hand from the context menu of the 3D view --
    exclusion from the solid and orientation correction -- are included too,
    because they must reach the builder for both component families, not
    only for the one that happens to be touched first.
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
            # A correction of zero degrees is not a choice made: it is omitted,
            # as an unset visibility override is omitted. The
            # other two fields stay as they were so as not to change a contract
            # already in use elsewhere.
            if field == "model_rotation_delta_deg" and not value:
                continue
            result[field] = value
    visible = getattr(component, "render_3d_override", None)
    if isinstance(visible, bool):
        result["render_3d_override"] = visible
        result["render_3d"] = visible
    return result


def _rectangle_axes(rotation: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Local unit axes, exact for the cardinal rotations."""
    angle = rotation % 360
    if angle % 90 == 0:
        c, s = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[int(angle / 90)]
    else:
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return (c, s), (-s, c)


def has_finite_geometry(component) -> bool:
    """@brief Tell whether a ComponentBox has finite position, size and rotation.

    @param component Object with x, y, length, width, height and rotation.
    @return False when any of them is missing, boolean, NaN or infinite.
    @note `ComponentBox.is_usable` only checks that the sizes are positive:
        NaN fails that test, but an infinite size and a NaN/inf position or
        rotation pass it and used to become invented coordinates (a NaN centre
        clamped to the board edge, a NaN rotation to the centre).
    """
    return all(is_finite_number(getattr(component, field, None))
               for field in ("x", "y", "length", "width", "height", "rotation"))


def clamp_component_position(
    x: float, y: float, length: float, width: float, rotation: float,
    body_length: float, body_width: float, margin: float = COMPONENT_EDGE_MARGIN_MM,
) -> tuple[float, float]:
    """@brief Keep the centre of a component inside the perimeter of the module.

    The position estimates of the extraction sometimes fall across the edge
    (read close to the margin of the photo, or with a large component near a
    contact): this is a generic geometric constraint, applicable to any
    component of any module, not a correction for one particular part. It
    projects the rotated rectangle onto the X/Y axes; at 90/270 degrees
    length and width swap.

    @param x Centre X of the component, mm (origin at the module centre).
    @param y Centre Y of the component, mm.
    @param length Long side of the component, mm.
    @param width Short side of the component, mm.
    @param rotation Rotation on the module, degrees (any angle).
    @param body_length Module size along X, mm.
    @param body_width Module size along Y, mm.
    @param margin Clearance kept from the module edge, mm.
    @return (x, y) clamped, in mm.
    @throws ValueError When a value is not a finite number: a NaN used to end
        up pushed to the edge, and a NaN rotation or an infinite length at
        the centre, i.e. invented coordinates.
    """
    if not all(is_finite_number(value) for value in
               (x, y, length, width, rotation, body_length, body_width, margin)):
        raise ValueError("component position, size and rotation must be finite numbers")
    (c, s), _ = _rectangle_axes(rotation)
    footprint_x = abs(c) * length + abs(s) * width
    footprint_y = abs(s) * length + abs(c) * width
    half_x = max(0.0, body_length / 2 - footprint_x / 2 - margin)
    half_y = max(0.0, body_width / 2 - footprint_y / 2 - margin)
    return max(-half_x, min(half_x, x)), max(-half_y, min(half_y, y))


def component_overhang(
    x: float, y: float, length: float, width: float, rotation: float,
    body_length: float, body_width: float,
) -> tuple[float, float]:
    """@brief How far a component's rotated outline extends beyond the module edge, per axis.

    @param x Centre X of the component, mm (origin at the module centre).
    @param y Centre Y of the component, mm.
    @param length Long side of the component, mm.
    @param width Short side of the component, mm.
    @param rotation Rotation on the module, degrees (any angle).
    @param body_length Module size along X, mm.
    @param body_width Module size along Y, mm.
    @return (overhang_x, overhang_y) in mm, zero on an axis where the outline
        stays inside the module.
    @throws ValueError When a value is not a finite number.
    """
    if not all(is_finite_number(value) for value in
               (x, y, length, width, rotation, body_length, body_width)):
        raise ValueError("component position, size and rotation must be finite numbers")
    (c, s), _ = _rectangle_axes(rotation)
    footprint_x = abs(c) * length + abs(s) * width
    footprint_y = abs(s) * length + abs(c) * width
    return (max(0.0, abs(x) + footprint_x / 2 - body_length / 2),
            max(0.0, abs(y) + footprint_y / 2 - body_width / 2))


def place_component(
    x: float, y: float, length: float, width: float, rotation: float,
    body_length: float, body_width: float, tolerance_mm: float,
    margin: float = COMPONENT_EDGE_MARGIN_MM,
) -> tuple[float, float, float, float]:
    """@brief Decide, axis by axis, whether an overhanging component is kept or moved inside.

    User decision (2026-09-26): a body sticking out of the edge stays where
    it was measured, with a note in the log; it is moved back inside (with
    `margin`) only when the overrun is smaller than the measurement
    uncertainty, i.e. when the overhang cannot be told apart from the error
    of the photo. A module with an antenna, a connector or a cantilevered
    button thus keeps its real geometry.

    @param x Measured centre X, mm.
    @param y Measured centre Y, mm.
    @param length Long side, mm.
    @param width Short side, mm.
    @param rotation Rotation on the module, degrees.
    @param body_length Module size along X, mm.
    @param body_width Module size along Y, mm.
    @param tolerance_mm Uncertainty of the overhang: an overhang up to this
        value is treated as measurement error.
    @param margin Clearance kept from the edge when a body is moved inside.
    @return (x, y, kept_overhang_x, kept_overhang_y): the centre to build and,
        for each axis, the overhang left in place (zero when none was kept).
    @throws ValueError When a value is not a finite number or the tolerance
        is negative.
    @note Each axis is decided on its own: a body can project 3 mm beyond one
        edge (kept) and 0.1 mm beyond the other (measurement error, moved).
        A body that does not overhang at all is treated as before: moved only
        to keep `margin` from the edge.
    """
    if not is_finite_number(tolerance_mm) or tolerance_mm < 0:
        raise ValueError("overhang tolerance must be a finite non-negative number")
    over_x, over_y = component_overhang(x, y, length, width, rotation,
                                        body_length, body_width)
    clamped_x, clamped_y = clamp_component_position(
        x, y, length, width, rotation, body_length, body_width, margin)
    keep_x, keep_y = over_x > tolerance_mm, over_y > tolerance_mm
    return (x if keep_x else clamped_x, y if keep_y else clamped_y,
            over_x if keep_x else 0.0, over_y if keep_y else 0.0)


def _uncertainty_basis(source: str) -> str:
    """Italian phrase saying where the overhang tolerance comes from."""
    if source == SOURCE_DECLARED:
        return tx("dichiarata dalla misura")
    if source == SOURCE_ASSUMED:
        return tx("stimata: scala della foto non nota, ipotizzati {scale:.0f} px/mm",
                  scale=ASSUMED_SCALE_PX_PER_MM)
    return tx("dalla scala della foto")


#: Minimum overlap, on both axes, for two components to
#: count as overlapping (not merely adjacent/touching).
OVERLAP_TOLERANCE_MM = 0.05


def find_overlapping_components(components: list) -> list[str]:
    """@brief Return log warnings for pairs of components whose rotated outlines overlap.

    The positions come from the extraction estimate, taken component by
    component: nothing prevents two independent estimates from ending up one
    on top of the other. No attempt is made to resolve it by moving
    something (that would be an arbitrary choice, not geometry) -- the pair
    is reported, so it gets corrected with «Modifica componenti…» instead of
    being discovered only in the finished 3D model.

    The separating-axes test compares the oriented rectangles, avoiding false
    positives between diagonal components with overlapping bounding boxes.

    @param components Component dictionaries with x, y, length_mm, width_mm
        and optional rotation_deg (already clamped, already rotated);
        entries with unusable numbers are skipped.
    @return Warning lines ready for the log, one for each overlapping pair.
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
        # A non-finite value has no outline to compare: NaN made
        # every comparison false and the pair vanished without warnings. The
        # component is already excluded upstream; nothing is invented here.
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
            # X/Y keep the measures of the message for the cardinal cases;
            # the local axes remove the false positives of the bounding boxes.
            for ux, uy in ((1.0, 0.0), (0.0, 1.0)) + axes_a + axes_b:
                pa = [x * ux + y * uy for x, y in corners_a]
                pb = [x * ux + y * uy for x, y in corners_b]
                overlaps.append(min(max(pa), max(pb)) - max(min(pa), min(pb)))
            if all(overlap > OVERLAP_TOLERANCE_MM for overlap in overlaps):
                overlap_x, overlap_y = overlaps[:2]
                warnings.append(
                    tx("componenti sovrapposti: '{name_a}' e '{name_b}' "
                       "({overlap_x:.2f}x{overlap_y:.2f}mm in comune) -- correggi posizione o "
                       "rotazione con «Modifica componenti…» nel tab Modello STEP.",
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
    """@brief Add one colour for each solid, in export order, to an AP214 STEP text.

    FreeCAD's `Part.export` writes the geometry only: without these
    presentation entities KiCad and the other CADs show everything in grey.
    For every MANIFOLD_SOLID_BREP the chain
    COLOUR_RGB -> FILL_AREA_STYLE -> ... -> STYLED_ITEM is added, plus a
    single MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION collecting
    them.

    @param step_text Full text of the AP214 STEP file written by FreeCAD.
    @param colors '#rrggbb' strings, one for each solid, in export order.
    @return The STEP text with the presentation entities inserted before the
        end of the data section.
    @throws ValueError When the count does not add up (empty geometry,
        partial export), the geometric context or the terminator is missing:
        the file stays as it is -- better grey than randomly coloured.
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


def _step_is_complete(path: Path) -> bool:
    """@brief Whether a STEP file ends with the ISO 10303-21 trailer.

    @param path STEP file the child reported as exported.
    @return True when the last non-blank bytes are ``END-ISO-10303-21;``;
        False as well when the file cannot be read.
    @note The rule is `kicad_library.step_file_is_complete`, the one the
        packaging already applies before shipping a STEP: one rule, so a
        model accepted here is never refused there.
    """
    from ..services.kicad_library import step_file_is_complete
    try:
        return step_file_is_complete(path)
    except OSError:
        return False


class Cad3DError(Exception):
    """@brief Application error: freecadcmd missing or 3D generation failed or cancelled."""


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

    @param exc subprocess.TimeoutExpired raised by run_logged (silence watchdog).
    @return Cad3DError carrying the last lines of the child's output, so the
        interface shows the FreeCAD tail instead of an "unexpected error".
    """
    output = _output_text(getattr(exc, "output", None)) + _output_text(getattr(exc, "stderr", None))
    tail = "\n".join(output.strip().splitlines()[-15:]) or tx("(nessun output)")
    return Cad3DError(tx("Generazione STEP interrotta: FreeCAD non ha prodotto output per "
                         "{timeout} s.\n\nOutput freecadcmd:\n{tail}",
                         timeout=getattr(exc, "timeout", FREECAD_SILENCE_TIMEOUT_S), tail=tail))


_FOLDER_VERSION = re.compile(r"(\d+(?:\.\d+)*)\s*$")
#: Leading version of a folder named after FreeCAD, whatever follows it:
#: «FreeCAD 1.1-dev», «FreeCAD 1.1 RC», «FreeCAD_1.0.2-conda-Windows-x86_64».
_FREECAD_FOLDER_VERSION = re.compile(r"^freecad[\s_-]*v?(\d+(?:\.\d+)*)", re.IGNORECASE)


def freecad_folder_version(executable: str) -> str:
    """@brief Version written in the installation folder of a freecadcmd path.

    @param executable Path such as ``C:/Program Files/FreeCAD 1.1/bin/freecadcmd.exe``
        (backslashes accepted).
    @return The dotted number of the folder holding ``bin`` (``"1.1"``), or
        ``""`` when the folder carries none (``FreeCAD``, ``/usr``): such an
        installation is still usable but never preferred.
    @note A folder named after FreeCAD gives the number right after the name,
        and a suffix after it does not change the number: «FreeCAD 1.1-dev»
        and «FreeCAD 1.1 RC» are version 1.1, not unversioned folders ranked
        below 0.19. Whether the suffix marks a pre-release is a separate
        question, answered by `freecad_folder_is_prerelease`. Any other folder
        gives the number at its end, as before.
    """
    folder = Path(str(executable).replace("\\", "/")).parent.parent.name
    match = _FREECAD_FOLDER_VERSION.search(folder) or _FOLDER_VERSION.search(folder)
    return match.group(1) if match else ""


#: One word of a folder suffix that marks a pre-release build: «dev», «alpha»,
#: «beta», «rc», «pre», «preview» or a development build («nightly», «weekly»,
#: «snapshot»), optionally followed by digits («rc2»), or the short PEP 440
#: forms «a»/«b» followed by digits («b1»).
_PRERELEASE_WORD = re.compile(
    r"(?:dev|alpha|beta|rc|pre|preview|nightly|weekly|snapshot)\d*|[ab]\d+", re.IGNORECASE)


def freecad_folder_is_prerelease(executable: str) -> bool:
    """@brief Tell whether the installation folder of a freecadcmd path names a pre-release.

    @param executable Path of freecadcmd, as for `freecad_folder_version`.
    @return True when the folder is named after FreeCAD, carries a version
        and the text after that version contains a pre-release word
        («FreeCAD 1.1-dev», «FreeCAD 1.1 RC», «FreeCAD 1.0rc2»,
        «FreeCAD 1.1 beta»); False otherwise, including folders without a
        version.
    @note The suffix is split into alphanumeric words and each word is
        compared with an explicit list (`_PRERELEASE_WORD`): dev, alpha,
        beta, rc, pre, preview, and the development builds nightly, weekly
        and snapshot, each optionally followed by digits, and
        a/b followed by digits. Any other word is a packaging tag, not a
        pre-release: «FreeCAD_1.0.2-conda-Windows-x86_64» is a release. An
        unknown suffix therefore ranks as a release (user decision D4,
        2026-10-14).
    """
    folder = Path(str(executable).replace("\\", "/")).parent.parent.name
    match = _FREECAD_FOLDER_VERSION.search(folder)
    if not match:
        return False
    words = re.split(r"[^0-9A-Za-z]+", folder[match.end():])
    return any(_PRERELEASE_WORD.fullmatch(word) for word in words if word)


def _freecad_install_order(candidates: list) -> list:
    """@brief Order freecadcmd paths from the most to the least preferred.

    @param candidates freecadcmd paths found on the system.
    @return The paths, highest numeric folder version first; at equal
        version a release before a pre-release; then by path text, as
        `newest_version_first` breaks ties. Unversioned folders come last.
    @note The second sort is stable, so the text tie-break of
        `newest_version_first` survives among installations of the same
        version and the same release state (two pre-releases of one
        version, e.g. «1.1-dev» and «1.1 RC», keep the reverse text order:
        «FreeCAD 1.1-dev» before «FreeCAD 1.1 RC»).
    @note Trailing zero components are dropped before comparing
        (`_comparable_version`): «1.1.0» and «1.1» are the same version,
        otherwise «FreeCAD 1.1.0-dev» would outrank the release «FreeCAD 1.1».
    """
    ordered = newest_version_first(candidates, freecad_folder_version)
    ordered.sort(key=lambda path: (_comparable_version(freecad_folder_version(path)),
                                   not freecad_folder_is_prerelease(path)),
                 reverse=True)
    return ordered


def _comparable_version(name: str) -> tuple:
    """@brief Numeric version without trailing zero components, for equality tests.

    @param name Dotted version such as ``"1.1.0"``, or ``""``.
    @return ``(1, 1)`` for ``"1.1.0"`` and ``"1.1"``; ``()`` when the name is
        not a version, so unversioned folders still sort last.
    """
    version = list(parse_version(name) or ())
    while version and version[-1] == 0:
        version.pop()
    return tuple(version)


def find_freecadcmd() -> str:
    """@brief Locate freecadcmd via FREECAD_CMD_PATH or typical installation folders; raise Cad3DError if absent.

    @return Path of the chosen freecadcmd.
    @throws Cad3DError When no installation is found.
    @note Among several installations the highest NUMERIC folder version
        wins (``FreeCAD 1.10`` over ``FreeCAD 1.9`` over ``FreeCAD 0.21``):
        a text sort ranked ``1.9`` above ``1.10``. At equal version the
        official release beats a pre-release (``FreeCAD 1.1`` over
        ``FreeCAD 1.1-dev``), while a higher number still wins
        (``FreeCAD 1.2-dev`` over ``FreeCAD 1.1``). Folders without a version
        come last; remaining ties are broken by the path text
        (`_freecad_install_order`).

    The search order is the FREECAD_CMD_PATH environment variable first,
    then every installation found under the program roots (per machine and
    per user, `installed_programs.program_roots`) or declared by FreeCAD's
    «Uninstall» registry entries, which cover a folder the user chose; all
    of them are ranked together."""
    from ..services import installed_programs

    env_path = os.environ.get("FREECAD_CMD_PATH")
    if env_path and Path(env_path).is_file():
        return env_path

    candidates = []
    for base in installed_programs.program_roots():
        candidates.extend(glob.glob(f"{base}\\FreeCAD*\\bin\\freecadcmd.exe"))
    candidates.extend(installed_programs.executables_in(
        installed_programs.registered_install_dirs(r"FreeCAD\b"), "freecadcmd.exe"))
    for base in ("/usr/bin", "/usr/local/bin"):
        candidates.extend(glob.glob(f"{base}/freecadcmd"))

    candidates = installed_programs.unique_paths(candidates)
    if candidates:
        return _freecad_install_order(candidates)[0]

    raise Cad3DError(
        tx("freecadcmd non trovato. Installa FreeCAD, oppure imposta la variabile "
           "d'ambiente FREECAD_CMD_PATH con il percorso completo di "
           "freecadcmd.exe.")
    )


#: Margin between the end of a pad and the centre of its silkscreen label,
#: beyond the hole radius: it is the convention by which the 3D lays out the
#: labels (and which contact_zones replicates to mask them). It does not change
#: between contact types: only where the pad ends changes.
SILK_LABEL_MARGIN_MM = 0.55


def castellated_contact_payload(params: CastellatedModuleParams, pin_color_hex: str):
    """@brief Describe every contact of a castellated module for the FreeCAD script.

    @param params Validated module description.
    @param pin_color_hex Colour of every plated feature (same metal everywhere).
    @return (notches, contacts, silk_texts, notes):
        `notches` are the cylinders subtracted from the board; `contacts` the
        plated features, one entry per contact; `silk_texts` the optional
        labels; `notes` human-readable lines about estimated quantities
        (Italian, for the log): params.geometry_notes().
    @note Hole sizes and through-hole positions are those of each side
        (CastellatedModuleParams.side_geometry): each side may declare its own
        half-hole, drill and ring, and an assumed size too large for the pitch
        is reduced there (and reported in `notes`), never here.
    @note No board silkscreen frame any more (user, 2026-09-26): it was a
        rectangle drawn inside the contacts, taken from no document or photo,
        and it read as a real feature of the board.
    @note For each contact style:
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

    notches = []
    contacts = []
    silk_texts = []
    for placement in pad_positions(params):
        # Hole dimensions of the contact's SIDE: the sides can have different ones.
        geometry = params.contact_geometry(placement)
        radius = geometry.half_hole / 2
        # A castellated hole is not centred on the edge: it is a normal hole
        # drilled near the edge and then cut away, so its centre lies
        # INSIDE the board by a fraction of the diameter.
        inset = geometry.half_hole_inset
        drill_radius = geometry.drill / 2
        label_gap = radius + SILK_LABEL_MARGIN_MM
        if placement.style == CONTACT_THROUGH_HOLE:
            centre = (placement.x, placement.y)
            ring = geometry.ring
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
            # Part of the pad that lies inside the board: it is the visible golden pad.
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
            if placement.style == CONTACT_HALF_AND_THROUGH and geometry.has_thru_holes:
                # The whole hole does not touch the edge: it is the other half of the
                # contact, the one into which a strip is soldered. It must be cut AND
                # plated: same number, same metal, same solid.
                hole_x, hole_y = params.thru_hole_centre(placement)
                notches.append({"x": hole_x, "y": hole_y, "radius": drill_radius})
                contact["through_hole"] = {
                    "x": hole_x, "y": hole_y, "radius": drill_radius,
                    "pad_diameter_mm": geometry.ring,
                }
                # Continuous copper from the edge to past the annular ring, as on the
                # real board: it replaces the edge pad and the rings.
                # Same dimensions as the footprint pad (continuous_pad),
                # measured from the edge inwards: the module has no copper
                # beyond its own edge, the overhang belongs to the motherboard.
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

    # The notes on the estimated dimensions (edge distance of the through holes,
    # holes reduced to fit the pitch) are written by the footprint generator:
    # they are the same dimensions, so a single source.
    notes = params.geometry_notes()
    return notches, contacts, silk_texts, notes


def header_side_pitch(side) -> tuple:
    """@brief Pitch of the male header drawn under one castellated side.

    @param side `CastellatedSide` of the module.
    @return (pitch_mm, assumed): the declared pitch and False, or the
        reference 2.54 mm header pitch and True when the side declares none
        (a single contact may have pitch 0: there is no neighbour to measure).
    @note The one place where the substitution is decided: the pins and the
        note that declares the estimate both read it, so they cannot disagree.
    """
    from .header_geometry import REFERENCE_PITCH_MM
    if side.pitch > 0:
        return side.pitch, False
    return REFERENCE_PITCH_MM, True


def header_pitch_notes(params: CastellatedModuleParams) -> list:
    """@brief Notes for every side whose header pitch is the reference, not a declared one.

    @param params Validated module description.
    @return One translated note per side drawn with the assumed pitch, naming
        the side and the pitch; empty when every side declares its pitch.
    @note Project invariant: an estimate that reaches the model is recorded in
        the notes (2026-10-27 review, H7). The note is emitted only for sides
        that actually receive a header pin, so a board drawn without pins
        says nothing about a pitch nobody used.
    """
    placements = iter(pad_positions(params))
    notes = []
    for side in params.sides:
        pitch, assumed = header_side_pitch(side)
        drawn = False
        for _number in side.numbers():
            placement = next(placements)
            if (style_has_through_hole(placement.style)
                    and params.contact_geometry(placement).has_thru_holes):
                drawn = True
        if assumed and drawn:
            notes.append(tx(
                "Pin 3D: il lato {edge} non dichiara un passo (un solo contatto): lo strip "
                "maschio e' disegnato al passo di riferimento {pitch} mm, una stima.",
                edge=side.edge, pitch=f"{pitch:g}"))
    return notes


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
        declared hole on the contact's side (SideGeometry.has_thru_holes:
        drill and ring > 0, the same condition under which the 3D cuts and
        plates the hole). A half-hole
        alone cannot hold a pin. The centre comes from
        params.thru_hole_centre(), the function the footprint and the 3D
        contacts already use, never from a second formula.
    @note A side with a single contact may declare no pitch (0): the
        reference 2.54 mm header is drawn, which is an estimate.
    """
    placements = iter(pad_positions(params))
    pins, pitches, skipped = [], [], []
    for side in params.sides:
        pitch, _assumed = header_side_pitch(side)
        for _number in side.numbers():
            placement = next(placements)
            if (style_has_through_hole(placement.style)
                    and params.contact_geometry(placement).has_thru_holes):
                x, y = params.thru_hole_centre(placement)
                pins.append([placement.number, x, y])
                pitches.append(pitch)
            else:
                skipped.append(placement.number)
    return pins, pitches, skipped


def component_payloads(components, board_length: float, board_width: float,
                       height_cap: float, *, centre=(0.0, 0.0),
                       photo_scale_px_per_mm: Optional[float] = None):
    """@brief Turn ComponentBox rows into the FreeCAD component payload, one policy for every board.

    @param components ComponentBox-like objects, coordinates in mm relative to
        the board centre (X right, Y down), rotation in degrees (any angle).
    @param board_length Board size along X, mm.
    @param board_width Board size along Y, mm.
    @param height_cap Maximum component height above the PCB, mm (body
        height minus PCB thickness); ignored when not > 0.
    @param centre Board centre in the payload frame, mm; added to every
        component position after placement.
    @param photo_scale_px_per_mm Calibrated photo scale, used for the
        overhang tolerance when a component carries none of its own.
    @return (payloads, notes, height_notes): payload dictionaries (with
        `source_row`, the index in `components`), localized notes about
        excluded, moved or overhanging bodies, and (payload, note) pairs
        for heights reduced to the cap, to be reported only for bodies
        built as approximate shapes (see attach_library_models).
    @note Excluded components are always noted, never dropped silently. An
        edge-mating connector keeps its measured centre (it may project).
        A reduced height is an estimate corrected, so it is reported.
    """
    payloads, notes, height_notes = [], [], []
    for source_row, component in enumerate(components):
        if not has_finite_geometry(component):
            notes.append(tx("Componente {name}: posizione, rotazione o quote non "
                            "finite, escluso.", name=component.name))
            continue
        if not component.is_usable():
            notes.append(tx("Componente {name}: dimensioni non valide, escluso.",
                            name=component.name))
            continue
        # An observed edge-mating connector can legitimately project beyond
        # the PCB. Do not move its measured centre to fit the entire envelope.
        if component.mating == "edge":
            x, y = component.x, component.y
        else:
            # The uncertainty of the overhang is that of two edges measured
            # on the same photo: the body edge and the board edge.
            edge_mm, source = component_edge_uncertainty(component, photo_scale_px_per_mm)
            tolerance = side_uncertainty_mm(edge_mm)
            x, y, kept_x, kept_y = place_component(
                component.x, component.y, component.length, component.width,
                component.rotation, board_length, board_width, tolerance,
            )
            if abs(x - component.x) > 1e-9 or abs(y - component.y) > 1e-9:
                notes.append(tx(
                    "Componente {name}: centro spostato da ({x0:.2f}, {y0:.2f}) a "
                    "({x1:.2f}, {y1:.2f}) mm per restare dentro il modulo.",
                    name=component.name, x0=component.x, y0=component.y, x1=x, y1=y))
            if kept_x or kept_y:
                notes.append(tx(
                    "Componente {name}: sporge dal bordo del modulo di {overhang:.2f} mm, "
                    "piu' dell'incertezza di misura ({tolerance:.2f} mm, {basis}): "
                    "lasciato dove e' stato misurato.",
                    name=component.name, overhang=max(kept_x, kept_y),
                    tolerance=tolerance, basis=_uncertainty_basis(source)))
        height = component.height
        height_note = None
        if 0 < height_cap < component.height:
            height = height_cap
            height_note = tx(
                "Componente {name}: altezza stimata {height:.2f} mm ridotta a {cap:.2f} mm "
                "(altezza del corpo meno lo spessore del PCB).",
                name=component.name, height=component.height, cap=height_cap)
        render_kind = component.kind
        if render_kind == "generic" and component.detected_kind:
            render_kind = component.detected_kind
        if (component.detected_kind and render_kind in {"ic_soic", "ic_qfn"}
                and not component.terminal_geometry):
            # A verified family still does not establish terminal count/pitch.
            # Manually selected legacy packages have no photographic hint.
            render_kind = "ic"
        payloads.append({
            "source_row": source_row,
            "name": component.name,
            "kind": render_kind,
            "x": centre[0] + x, "y": centre[1] + y,
            "length_mm": component.length, "width_mm": component.width,
            "height_mm": height,
            "color_hex": component.color_hex, "marking": component.marking,
            "rotation_deg": component.rotation,
            "pin_count": component.pin_count,
            "pin_count_confidence": component.pin_count_confidence,
            "pin_count_source": component.pin_count_source,
            "pins_by_side": component.pins_by_side,
            # Same sides in the builder's local frame (long axis on
            # X, before the rotation): it is from here that the leads are drawn.
            "lead_sides_local": local_lead_sides(component.pins_by_side, component.rotation),
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
            height_notes.append((payloads[-1], height_note))
    return payloads, notes, height_notes


def attach_library_models(components, height_notes, notes, report, *,
                          model_progress=None, cancel=None) -> None:
    """@brief Attach KiCad library models to component payloads, falling back to approximate shapes.

    @param components Payloads from component_payloads, updated in place.
    @param height_notes (payload, note) pairs from component_payloads.
    @param notes List that receives the height notes still relevant.
    @param report Callback for model-matching messages.
    @param model_progress Optional progress callback of the matching.
    @param cancel Optional threading.Event that aborts the work.
    @throws Cad3DError With CAD_ABORT_MESSAGE when the user cancels.
    @note The library is an extra, never a requirement: any other error is
        reported and every body is built as an approximate shape. The
        resolution runs here, not inside FreeCAD: measuring a model needs its
        own FreeCAD process, which cannot be started from inside the one
        building the solid.
    @note A reduced height is reported only for bodies without a library
        model: a body that receives one is imported at the model's height
        (point 66c, user decision of 2026-09-26). If that import then fails,
        FreeCAD falls back to the capped shape, and the body is already
        flagged as a failed import.
    """
    try:
        from .kicad_models import attach_models
        # The channel is passed ONLY when present, as for `run_logged`: a
        # replacement of `attach_models` written before this parameter
        # must not make a good generation fail.
        attach_models(components, log=report, progress=model_progress,
                      **({"cancel": cancel} if cancel is not None else {}))
    except ProcessCancelled as exc:
        # The give-up is NOT a defect of the 3D library: if it ended in the branch
        # below, it would be announced as «libreria non utilizzabile» (library not
        # usable) and the construction of the solid would start anyway.
        raise Cad3DError(CAD_ABORT_MESSAGE) from exc
    except Exception as exc:  # noqa: BLE001 - the library is an extra, never a requirement
        report(tx("Libreria 3D KiCad non utilizzabile ({exc}); si usano le forme "
                  "approssimate.",
                  exc=exc))
    notes.extend(note for rendered, note in height_notes
                 if not rendered.get("model_path"))


def reused_height_notes(height_notes, reused) -> list:
    """@brief Height notes still relevant when the model choices come from a snapshot.

    @param height_notes (payload, note) pairs from `component_payloads` of
        THIS run (fresh payloads, without model paths).
    @param reused Component payloads loaded from `HeaderModelReuse`, with the
        model paths chosen by the run that saved them.
    @return The notes `attach_library_models` would have kept: a body with no
        library model in the snapshot keeps its "reduced estimated height"
        note; one that received a model is imported at the model's height and
        gets none.
    @note Fresh and reused payloads are paired by `source_row` (the index in
        the component list, stable because the snapshot is valid only for the
        same components), falling back to the position in the list. A fresh
        body with no counterpart keeps its note: a correction of an estimate
        is never dropped for lack of evidence that it no longer applies.
    """
    by_row = {}
    for index, component in enumerate(reused or []):
        if isinstance(component, dict):
            by_row.setdefault(component.get("source_row", index), component)
    kept = []
    for fresh, note in height_notes:
        match = by_row.get(fresh.get("source_row"))
        if not (isinstance(match, dict) and match.get("model_path")):
            kept.append(note)
    return kept


def resolve_or_reuse_models(reuse, components, height_notes, notes, report, *,
                            progress=None, model_progress=None, cancel=None) -> list:
    """@brief Attach library models, or reuse the snapshot of a headers-only change.

    @param reuse HeaderModelReuse of this generation.
    @param components Fresh payloads from `component_payloads`.
    @param height_notes (payload, note) pairs from `component_payloads`.
    @param notes List receiving the height notes still relevant.
    @param report Callback for model-resolution messages.
    @param progress Optional progress callback; receives REUSE_PROGRESS on reuse.
    @param model_progress Optional progress callback of the matching.
    @param cancel Optional threading.Event that aborts the matching.
    @return The payloads to build: the fresh ones, now resolved, or the reused ones.
    @throws Cad3DError With CAD_ABORT_MESSAGE when the user cancels the matching.
    @note Both paths produce the same notes: on reuse the model-resolution
        messages saved with the snapshot are reported again, and the height
        notes are filtered by the reused model paths (`reused_height_notes`),
        so a headers-only regeneration never silently loses the
        "estimated height reduced" correction.
    """
    previous = reuse.load()
    if previous is None:
        attach_library_models(components, height_notes, notes, report,
                              model_progress=model_progress, cancel=cancel)
        return components
    if progress:
        progress(REUSE_PROGRESS)
    for message in reuse.notes:
        report(message)
    notes.extend(reused_height_notes(height_notes, previous))
    return previous


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
    photo_scale_px_per_mm: Optional[float] = None,
) -> dict:
    """@brief Generate the 3D model of a castellated module as a real PCB.

    What is seen on a real module, and what is reconstructed here:

    * the **PCB** -- FR4 substrate between two solder-mask layers,
      `pcb_thickness` thick, with the half-holes cut on the edge of the sides
      that have them only (`CastellatedSide.contact_style`): a through_hole
      side has a straight edge;
    * the **contacts**: plated hole (sleeve) plus the gold pad on the top and
      bottom faces, so they are visible from both sides; the through-hole is
      plated the same way, with a ring on both faces; a half_and_through
      contact is ONE continuous gold pad from the edge to beyond the ring,
      as on the real board (see castellated_contact_payload);
    * the **silkscreen**: label next to each contact (from `silk_labels`) and
      the edge frame;
    * the **components** read from the photo, each with the shape of its
      type (SOIC with leads, filleted transformer, LED with lens...); the
      marking read from the photo is NOT printed on top, unless
      `body.show_markings_3d` asks for it: the standard path produces generic
      volumes without writing.

    There are no cylindrical pins: the contacts are half-holes and
    through-holes. With `body.show_pins_3d` every contact that has a closed
    through-hole receives a male header strip soldered underneath (see
    castellated_header_pins and header_geometry); half-hole-only contacts
    stay without one, and a note says so when the board has both kinds.
    The notes of the result say where the edge distance of the through-holes
    comes from, and whether it is an estimate.

    @param params Validated castellated module description (contacts, sides,
        components, PCB thickness).
    @param body Body parameters: total height, colours and the
        show_pins_3d/show_markings_3d choices; the height of the PCB itself
        is `params.pcb_thickness`.
    @param output_dir Project directory receiving the `.3dshapes` folder.
    @param freecadcmd_path Explicit freecadcmd executable; None looks it up.
    @param fillets EdgeFilletAnnotation list applied to the board before the
        export, or None.
    @param face_texts FaceTextAnnotation list printed on the board faces, or
        None.
    @param font_file TrueType font used for every text solid.
    @param progress Optional callable receiving one status line at a time.
    @param model_progress Optional callback(done, total) of the KiCad model
        matching of the components.
    @param cancel Cancellation channel (see `_run_freecad`): a
        `threading.Event` that whoever started the job sets to stop it.
    @param photo_scale_px_per_mm Scale of the calibrated photo the component
        positions come from, when known: the measurement uncertainty is
        derived from it (see `measurement_uncertainty`). A component carrying
        its own (`measurement_uncertainty_mm` or `photo_scale_px_per_mm` in
        its evidence) uses that one. With neither, a declared fallback is
        used and the note says so. A body sticking out of the edge by more
        than the uncertainty stays where it was measured (note in the log);
        one sticking out by less is moved back inside (see
        `place_component`). Edge-mating connectors (`mating == "edge"`) are
        never moved.
    @return The `_run_freecad` result ({"step_path", "wrl_path", ...}) with
        the placement, height, pin and matching notes appended.
    @throws Cad3DError When freecadcmd is missing, the FreeCAD script fails
        or the job is stopped on request.
    """
    params.validate()

    notches, contacts, silk_texts, inset_notes = castellated_contact_payload(
        params, body.pin_color_hex)

    # Male strips only in real through holes (user decision,
    # 2026-09-26). With no pins to show the payload stays as before:
    # no pins and no style.
    header_pins, header_pitches, header_skipped = (
        castellated_header_pins(params) if getattr(body, "show_pins_3d", True) else ([], [], []))
    header_payload = ({"pin_style": "male_header", "header_pitches": header_pitches}
                      if header_pins else {})
    # An assumed pitch is an estimate: said in the notes, never silent.
    header_notes = header_pitch_notes(params) if header_pins else []
    if header_pins and header_skipped:
        header_notes.append(tx(
            "Pin 3D: {count} contatti a solo mezzo foro ({numbers}) restano senza pin "
            "maschio, perche' uno strip si salda solo in un foro passante.",
            count=len(header_skipped), numbers=", ".join(header_skipped)))

    # The body is the module's PCB; the total height of `body` stays the
    # reference for the component block when there is no photo.
    pcb = replace(body, height_mm=params.pcb_thickness)
    component_height = body.height_mm - params.pcb_thickness

    # Every value changed here (centre moved inside the module, estimated
    # height reduced) is a correction of an estimate: it must be stated, not
    # done silently (project rule on estimated heights).
    components, placement_notes, height_notes = component_payloads(
        params.components, params.body_length, params.body_width, component_height,
        photo_scale_px_per_mm=photo_scale_px_per_mm)

    extra_boxes = []
    inset_c = params.component_inset
    if (not components and component_height > 0
            and params.body_length > 2 * inset_c and params.body_width > 2 * inset_c):
        # Without the component list the single block remains: it approximates
        # the envelope, which is still better than a bare slab.
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

    # Real 3D models from the KiCad library, when the package is known and
    # the envelope confirms it (see `attach_library_models`).
    model_notes = []
    def report_model(message):
        model_notes.append(message)
        if progress:
            progress(message)
    reuse = HeaderModelReuse(output_dir, params, body,
        [fillets, face_texts, font_file, photo_scale_px_per_mm])
    components = resolve_or_reuse_models(
        reuse, components, height_notes, placement_notes, report_model,
        progress=progress, model_progress=model_progress, cancel=cancel)

    # Detected BEFORE calling FreeCAD: it is a geometric check on data
    # already prepared, there is no need to build the solid to notice it, and so
    # the warning arrives even if the overlap makes one of the shapes fail
    # (clashing fillet/cut) further on in the script. AFTER the
    # matching, though: that is where the hypotheses of the detector alone are
    # excluded from the 3D, and a body that will not be built overlaps
    # nothing.
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
    # Informative annotations, distinct from warnings: they tell how many real
    # models were used. Without them, the outcome of the resolution does not
    # reach the user and the mechanism seems not to have kicked in.
    result["notes"] = ([] if progress else model_notes) + list(result.get("notes", []))
    # The provenance of the edge distance of the through holes is not a
    # detail: when it is an estimate it must reach the user, always.
    result["notes"] = inset_notes + header_notes + placement_notes + result["notes"]
    reuse.save(components, result, notes=model_notes)
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

    Writes <Name>.step (and best-effort <Name>.wrl) in output_dir/<Name>.3dshapes/.

    @param pin_params Pin grid of the header; validated here.
    @param body Body dimensions, colours and the populated-board options.
    @param output_dir Project directory receiving the `.3dshapes` folder.
    @param freecadcmd_path Explicit freecadcmd executable; None looks it up.
    @param fillets EdgeFilletAnnotation list (kicadgen.cad3d.annotations)
        applied to the body before the export, first and in order.
    @param face_texts FaceTextAnnotation list laid on the resulting faces
        after the fillets.
    @param font_file TrueType font used for every text solid.
    @param progress Optional callable receiving one status line at a time.
    @param model_progress Optional callback(done, total) of the KiCad model
        matching, used only for a populated board.
    @param cancel Cancellation channel (see `_run_freecad`).
    @return {"step_path": Path, "wrl_path": Path | None}.
    @throws Cad3DError For application problems (freecadcmd missing, FreeCAD
        script failed, interruption requested by the user), with the
        freecadcmd output included for debugging.
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


def generate_single_package_3d_model(
    package: PackageGeometry,
    body: Body3DParams,
    output_dir: Path,
    freecadcmd_path: Optional[str] = None,
    fillets: Optional[list] = None,
    face_texts: Optional[list] = None,
    font_file: str = DEFAULT_FONT_FILE,
    progress=None,
    model_progress=None,
    cancel=None,
    photo_scale_px_per_mm=None,
) -> dict:
    """@brief Build a generic single-part solid directly from its package geometry.

    @param package Explicit pad geometry of the part (THT and/or SMD pins).
    @param body Colours and fallback dimensions when the package gives none.
    @param output_dir Project directory receiving the `.3dshapes` folder.
    @param freecadcmd_path Explicit freecadcmd executable; None looks it up.
    @param fillets EdgeFilletAnnotation list applied to the body, or None.
    @param face_texts FaceTextAnnotation list laid on the body faces, or None.
    @param font_file TrueType font used for every text solid.
    @param progress Optional callable receiving one status line at a time.
    @param model_progress Accepted for signature compatibility with the other
        generators; this path performs no model matching.
    @param cancel Cancellation channel (see `_run_freecad`).
    @param photo_scale_px_per_mm Accepted for signature compatibility; a
        single part has no photographic placement to tolerate.
    @return The `_run_freecad` result with a generic-model note appended.
    @note The body box turns with the package: `orientation_deg` (KiCad
        convention, any angle) is applied to the body about the footprint
        origin, exactly as `pins_seen_from_top` applies it to the pads and
        the footprint applies it to the F.Fab outline. Pin-number labels are
        placed in the body's own frame and turn with it. At 0 degrees the
        FreeCAD payload is the previous one, field for field.

    This path intentionally makes no KiCad library, archive, or model-match
    calls. Through-hole leads are cylindrical approximations; SMD terminals
    are thin boxes sized from their land geometry until source documents give
    more precise terminal dimensions.
    """
    pins = package.pins_seen_from_top()
    length = package.body_length_mm or body.length_mm
    width = package.body_width_mm or body.width_mm
    height = package.body_height_mm or body.height_mm
    centered = replace(body, length_mm=length, width_mm=width, height_mm=height,
                       offset_x_mm=-length / 2, offset_y_mm=-width / 2,
                       components=[], populated_board=False, show_pins_3d=True)
    leads = []
    terminals = []
    drills = []
    for pin in pins:
        if pin.pad_type == "thru_hole":
            drills.append(float(pin.drill_mm))
            leads.append([str(pin.number), pin.x_mm, pin.y_mm])
        elif pin.pad_type == "smd":
            terminals.append({
                "name": f"Terminal_{pin.number}",
                "x": pin.x_mm - pin.pad_length_mm / 2,
                "y": pin.y_mm - pin.pad_width_mm / 2,
                # On the board surface, under the body: a terminal below
                # z = 0 would sit inside the PCB in KiCad's viewer (review B).
                "z": 0.0,
                "length_mm": pin.pad_length_mm,
                "width_mm": pin.pad_width_mm,
                "height_mm": SMD_TERMINAL_THICKNESS_MM,
                "rotation_deg": pin.rotation_deg,
                "color_hex": body.pin_color_hex,
            })
    if leads:
        # One lead diameter for the whole part (the FreeCAD payload has one):
        # an ESTIMATE from the SMALLEST hole, so a mounting hole never widens
        # every lead and no lead is wider than its hole. LEAD_OF_DRILL_FRACTION
        # and LEAD_MAX_DIAMETER_MM are declared in this module.
        centered = replace(centered, pin_diameter_mm=min(
            min(drills) * LEAD_OF_DRILL_FRACTION, LEAD_MAX_DIAMETER_MM))
    texts = list(face_texts or [])
    turn = _normal_degrees(package.orientation_deg)
    if body.pin_numbers_3d and leads:
        # The labels lie on the body, which is described in its own frame
        # and turned by FreeCAD: place them from the leads seen in that frame.
        texts.extend(pin_number_labels(
            [(n, *body_frame_point(x, y, turn)) for n, x, y in leads], centered))
    if progress:
        progress(tx("Modello STEP generico del componente: nessuna ricerca nelle librerie KiCad."))
    result = _run_freecad(
        package.name or "Package", leads, centered, output_dir,
        freecadcmd_path=freecadcmd_path, fillets=fillets, face_texts=texts,
        font_file=font_file, extra_boxes=terminals, progress=progress, cancel=cancel,
        body_rotation_deg=turn,
    )
    result.setdefault("notes", []).append(tx(
        "Corpo package generico. Terminali THT cilindrici e terminali SMD ricavati dai pad: "
        "verificare quote e forma sui documenti del componente."))
    return result


def _normal_degrees(angle) -> float:
    """Map an angle to [0, 360) degrees, with exact 0 for every full turn and no -0."""
    value = round(float(angle) % 360.0, 9)
    return 0.0 if value in (0.0, 360.0) else value + 0.0


def body_frame_point(x: float, y: float, turn_deg: float) -> tuple[float, float]:
    """@brief Bring a footprint point into the frame of a body turned by `turn_deg`.

    @param x Point X in footprint coordinates (Y down), mm.
    @param y Point Y in footprint coordinates, mm.
    @param turn_deg Body turn about the origin, KiCad convention (degrees
        counter-clockwise on screen), any angle.
    @return (x, y) in the body's axis-aligned frame, mm: the inverse of the
        rotation `PackageGeometry.pins_seen_from_top` applies, so turning the
        result by `turn_deg` gives the input back. 0 returns the input as is.
    """
    if not turn_deg:
        return x, y
    angle = math.radians(turn_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    # Inverse of (x cos + y sin, -x sin + y cos): rotate by -turn_deg.
    return (round(x * cos_a - y * sin_a, 9) + 0.0, round(x * sin_a + y * cos_a, 9) + 0.0)


#: Lead diameter of a generic through-hole part as a fraction of its hole:
#: an ESTIMATE (a hole is usually the lead plus 0.2-0.3 mm) used only to draw
#: the lead cylinders of a part whose documents give no lead size.
LEAD_OF_DRILL_FRACTION = 0.65
#: Largest lead diameter drawn from a hole, in mm: beyond it the hole is a
#: mounting hole, not a lead's.
LEAD_MAX_DIAMETER_MM = 0.8
#: Thickness, in mm, of the box standing for an SMD terminal on the board.
SMD_TERMINAL_THICKNESS_MM = 0.04
#: Glyph width of the FreeCAD font as a fraction of the font size: an
#: ESTIMATE used only to keep a label clear of its pin and inside the body
#: (Arial digits are about 0.55 of the size; 0.65 leaves a margin). A label
#: is silkscreen, so a few tenths of a millimetre either way change nothing.
GLYPH_WIDTH_FRACTION = 0.65
#: Gap, in mm, between the edge of a lead and its printed number.
PIN_LABEL_GAP_MM = 0.25
#: Largest font size, in mm, of a pin number on the bottom face.
PIN_LABEL_MAX_SIZE_MM = 1.0


#: Below this font size a pin number is a smudge; a label is still placed
#: (the user asked for numbers) but never smaller.
PIN_LABEL_MIN_SIZE_MM = 0.3


def pin_number_labels(leads, body: Body3DParams) -> list:
    """@brief Pin numbers on the bottom face, one beside each lead of any single part.

    @param leads (number, x_mm, y_mm) of every lead, footprint coordinates.
    @param body Body dimensions, offset and lead diameter.
    @return FaceTextAnnotation list on face «bottom», one per lead, centred
        beside it; empty without leads.
    @note Placement rule, the same for every part: the label moves from the
        lead along the axis with fewer distinct lead coordinates (Y for a
        DIP-style module and for a single row, X for a single column),
        towards the body centre, or towards the farther edge when the lead
        lies on the centre line. The room is measured to the NEAREST obstacle
        in that direction: the midpoint to the next line of leads, or the
        body edge (review B of 2026-10-10: measured to the centre, a centred
        single row always got the smallest size). The font size is the
        largest of PIN_LABEL_MAX_SIZE_MM that keeps the longest number, at
        GLYPH_WIDTH_FRACTION, within that room AND within the spacing of the
        neighbouring leads on the other axis, so labels never overlap each
        other; never below PIN_LABEL_MIN_SIZE_MM. The label is then clamped
        inside the body outline. Numbers only, no names.
    """
    leads = [(number, float(x), float(y)) for number, x, y in leads]
    if not leads:
        return []
    xs = sorted({round(x, 6) for _, x, _ in leads})
    ys = sorted({round(y, 6) for _, _, y in leads})
    along_y = len(ys) <= len(xs)
    lines = ys if along_y else xs                 # lead lines along the travel axis
    across = xs if along_y else ys                # neighbours on the other axis
    across_gap = min(b - a for a, b in zip(across, across[1:])) if len(across) > 1 else None
    low = body.offset_y_mm if along_y else body.offset_x_mm
    high = low + (body.width_mm if along_y else body.length_mm)
    centre = (low + high) / 2
    lead_r = body.pin_diameter_mm / 2
    longest = max(len(str(number)) for number, _, _ in leads)
    width_per_mm = GLYPH_WIDTH_FRACTION * longest   # label width per mm of size
    labels = []
    for number, x, y in leads:
        c = y if along_y else x
        if abs(c - centre) < 1e-6:
            direction = 1.0 if high - c >= c - low else -1.0
        else:
            direction = 1.0 if c < centre else -1.0
        beyond = [v for v in lines if (v - c) * direction > 1e-6]
        limit = ((c + min(beyond, key=lambda v: abs(v - c))) / 2 if beyond
                 else (high if direction > 0 else low))
        room = abs(limit - c) - lead_r - PIN_LABEL_GAP_MM
        # Extent along the travel axis: the label height when moving along
        # Y, its width when moving along X; the other extent must fit
        # between neighbouring leads.
        travel_per_mm = 1.0 if along_y else width_per_mm
        across_per_mm = width_per_mm if along_y else 1.0
        size = min(PIN_LABEL_MAX_SIZE_MM, room / travel_per_mm)
        if across_gap is not None:
            size = min(size, (across_gap - 2 * PIN_LABEL_GAP_MM) / across_per_mm)
        size = max(PIN_LABEL_MIN_SIZE_MM, size)
        half_w = width_per_mm * size / 2
        half_h = size / 2
        if along_y:
            ax, ay = x, y + direction * (lead_r + PIN_LABEL_GAP_MM + half_h)
        else:
            ax, ay = x + direction * (lead_r + PIN_LABEL_GAP_MM + half_w), y
        ax = min(max(ax, body.offset_x_mm + half_w), body.offset_x_mm + body.length_mm - half_w)
        ay = min(max(ay, body.offset_y_mm + half_h), body.offset_y_mm + body.width_mm - half_h)
        labels.append(FaceTextAnnotation(face="bottom", text=str(number), size_mm=round(size, 3),
                                         x_mm=round(ax, 3), y_mm=round(ay, 3)))
    return labels


def pin_number_face_texts(pin_params: PinHeaderParams, body: Body3DParams) -> list:
    """@brief Pin numbers of a pin grid on the bottom face (see `pin_number_labels`).

    @param pin_params Validated pin grid of the part (positions, pitch, numbering).
    @param body Body dimensions and offset; the pin diameter is the lead's.
    @return FaceTextAnnotation list, one per populated pin; empty without pins.
    """
    return pin_number_labels(list(pin_positions(pin_params)), body)


def generate_single_tht_3d_model(
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
    photo_scale_px_per_mm=None,
) -> dict:
    """@brief Build a simple single THT component directly, without library matching.

    @param pin_params Pin grid of the part; validated here.
    @param body Body dimensions, colours and the `pin_numbers_3d` choice.
    @param output_dir Project directory receiving the `.3dshapes` folder.
    @param freecadcmd_path Explicit freecadcmd executable; None looks it up.
    @param fillets EdgeFilletAnnotation list applied to the body, or None.
    @param face_texts Texts on the faces (the top-face writing from the GUI);
        the pin numbers of `pin_number_face_texts` are appended when
        `body.pin_numbers_3d` is set.
    @param font_file TrueType font used for every text solid.
    @param progress Optional callable receiving one status line at a time.
    @param model_progress Accepted for signature compatibility with the other
        generators; this path performs no model matching.
    @param cancel Cancellation channel (see `_run_freecad`).
    @param photo_scale_px_per_mm Accepted for signature compatibility; a
        single part has no photographic placement to tolerate.
    @return The `_run_freecad` result with a generic-model note appended.
    """
    pin_params.validate()
    pins = [[pin, x, y] for pin, x, y in pin_positions(pin_params)]
    texts = list(face_texts or [])
    if body.pin_numbers_3d:
        texts.extend(pin_number_face_texts(pin_params, body))
    if progress:
        progress(tx("Modello STEP THT generico del componente: nessuna ricerca nelle librerie KiCad."))
    result = _run_freecad(
        pin_params.name, pins, body, output_dir,
        freecadcmd_path=freecadcmd_path, fillets=fillets, face_texts=texts,
        font_file=font_file, progress=progress, cancel=cancel,
    )
    result.setdefault("notes", []).append(tx(
        "Modello THT generico: verificare posizione e dimensioni dei reofori sui documenti del componente."))
    return result


def _generate_populated_tht(params, body, pins, output_dir, **options):
    """Build a populated PCB using matrix pin coordinates and centred photo boxes.

    Components go through the same `component_payloads` and
    `attach_library_models` as the castellated module: same edge placement,
    same height cap (body height minus PCB thickness), same notes for excluded
    bodies, and the same fallback to approximate shapes when the KiCad library
    cannot be used. Component coordinates are relative to the board centre;
    the PCB box starts at (offset_x_mm, offset_y_mm).
    """
    progress = options.get("progress")
    # Extracted, not read: further on `options` is forwarded as is
    # to _run_freecad, which does not know this callback and would reject the call.
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
    centre = (body.offset_x_mm + body.length_mm / 2, body.offset_y_mm + body.width_mm / 2)
    components, placement_notes, height_notes = component_payloads(
        body.components or [], body.length_mm, body.width_mm, body.height_mm - thickness,
        centre=centre)
    # `options` travels intact up to `_run_freecad`, which still wants the
    # channel: here it is ONLY READ. Same rule as the castellated site --
    # a single request covers the measure of the candidates and the construction
    # of the solid.
    reuse = HeaderModelReuse(output_dir, params, body,
        [options.get('fillets'), options.get('face_texts'), options.get('font_file')])
    # Only the model-resolution messages go into the snapshot: the thickness
    # note above is recomputed on every run and must not be replayed twice.
    model_notes = []

    def report_model(message):
        model_notes.append(message)
        report(message)
    components = resolve_or_reuse_models(
        reuse, components, height_notes, placement_notes, report_model,
        progress=progress, model_progress=model_progress, cancel=options.get("cancel"))
    overlap_warnings = find_overlapping_components(_rendered(components))
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
                       # A populated board connects through male strips soldered
                       # underneath (user, 2026-09-26): square pins and black
                       # spacer, not cylinders (see header_geometry).
                       "pin_style": "male_header",
                       # Missing positions do not change the physical header pitch.
                       "header_pitches": [params.pitch] * len(pins)},
        **options)
    result["warnings"] = overlap_warnings + result.get("warnings", [])
    result["notes"] = ([] if progress else notes) + placement_notes + list(result.get("notes", []))
    result["candidate_report_path"] = candidate_report
    _reconcile_model_imports(result, components, output_dir)
    reuse.save(components, result, notes=model_notes)
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
        rendered = {}
        for part in mesh.get('parts', []):
            if not isinstance(part, dict) or part.get('source') != 'kicad_library':
                continue
            angle = part.get('model_local_rotation_deg')
            if isinstance(angle, (int, float)) and not isinstance(angle, bool) and math.isfinite(angle):
                rendered[part.get('source_row', part.get('component_index'))] = part
        updated = False
        for index, component in enumerate(components):
            part = rendered.get(component.get('source_row', index))
            if (part and component.get('source_row', index) not in failed
                    and part.get('model_path') == component.get('model_path')):
                report = dict(component.get('model_match') or {})
                report['rendered_local_rotation_deg'] = part['model_local_rotation_deg']
                report['rendered_manual_delta_deg'] = float(component.get('model_rotation_delta_deg') or 0)
                component['model_match'] = report
                updated = True
            if component.get('source_row', index) not in failed:
                continue
            report = dict(component.get('model_match') or {})
            report.update(status='import_failed', selected_path=None,
                          error=tx("Il file STEP selezionato non è stato importato; visualizzato un "
                                   "segnaposto."))
            component['model_match'] = report
            result.setdefault('warnings', []).append(f"{component.get('name')}: {report['error']}")
        if failed or updated:
            from ..services.model_choices import save_candidate_report
            save_candidate_report(output_dir, components)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        result.setdefault('warnings', []).append(tx("Verifica importazione modelli non disponibile: {exc}", exc=exc))


#: Marker inserted before the extension of a staged output. The extension is
#: kept because FreeCAD chooses the STEP exporter from it.
STAGING_MARKER = ".kicadgen-partial"


def staging_path(final: Path) -> Path:
    """@brief Name under which an output is written before being published.

    @param final Final path of a STEP, WRL or preview JSON output.
    @return Sibling path (same folder, so the rename is atomic) with
        STAGING_MARKER inserted before the extension, e.g.
        ``X.kicadgen-partial.step``.
    """
    return final.with_name(f"{final.stem}{STAGING_MARKER}{final.suffix}")


def _published_outputs(step_path: Path, wrl_path: Path, mesh_path: Path,
                       fcstd_path: Optional[Path] = None) -> dict:
    """Map each 3D output of a run to its (final, staging) paths.

    The FreeCAD source (.FCStd) is staged like the shipped exports when the
    caller names it (2026-10-27 review, H8): written straight to its final
    name, a kill left a truncated file and a failed save left the previous
    run's file looking current.
    """
    outputs = [("step", step_path), ("wrl", wrl_path), ("mesh", mesh_path)]
    if fcstd_path is not None:
        outputs.append(("fcstd", fcstd_path))
    return {key: (final, staging_path(final)) for key, final in outputs}


def _remove_output(path: Path, *, quiet: bool = False) -> None:
    """Delete one output file if present.

    @throws Cad3DError When the file exists and cannot be removed (for
        example held open by another program) and `quiet` is False: running
        FreeCAD anyway would leave the old file looking like the new result.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        if not quiet:
            raise Cad3DError(tx("Impossibile rimuovere il vecchio file 3D {path}: {exc}",
                                path=path, exc=exc)) from exc


def _discard_staging(published: dict) -> None:
    """Remove leftover staging files; never raises (cleanup only)."""
    for _, staged in published.values():
        _remove_output(staged, quiet=True)


def _discard_outputs(published: dict, *, quiet: bool = False) -> None:
    """Remove final and staging files of the STEP, WRL and preview JSON.

    @note Contract of a 3D run: the previous outputs are deleted right before
        freecadcmd starts, and deleted again if the run fails or is
        cancelled, so the folder only ever holds outputs whose export
        completed in the latest run. They are deleted, not moved aside: an
        old model is never a valid result for the current footprint, so there
        is nothing worth restoring. The FreeCAD source (.FCStd) follows the
        same rule when `published` names it: a stale source from an earlier
        run must not sit next to a newer STEP. Every file is attempted before
        the first error is raised.
    """
    first_error = None
    for final, staged in published.values():
        for path in (final, staged):
            try:
                _remove_output(path, quiet=quiet)
            except Cad3DError as exc:
                first_error = first_error or exc
    if first_error is not None:
        raise first_error


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
    body_rotation_deg: float = 0.0,
) -> dict:
    """Build the JSON and launch freecadcmd; shared by every 3D generator.

    `body_rotation_deg` turns the body box, its fillets and its face texts
    about the footprint origin (Z axis), KiCad convention (degrees
    counter-clockwise on screen, footprint Y down), any angle. The body and
    its texts are described in the body's own axis-aligned frame; pins,
    extra boxes and notches stay in the footprint frame. 0 (the default)
    writes no field, so the payload and the solid are exactly the previous
    ones. Edge contacts are classified on the axis-aligned board edges and
    cannot be combined with a turned body (ValueError).

    `pins` are the cylinders to add under the body (empty for castellated
    modules), `notches` the cylinders to SUBTRACT from the body to carve the
    half-holes on the edge, `extra_boxes` the solids to add on top of the
    body (for modules, the block representing the components mounted on the
    PCB).

    `cancel` is the cancellation channel: an object with `is_set()`/`wait()`
    (typically `threading.Event`) that whoever started the job sets to ask
    it to stop. The freecadcmd child is killed by the same machinery that
    kills it when it stays silent too long (process_log.run_logged) and the
    call exits with `Cad3DError(CAD_ABORT_MESSAGE)`, i.e. through the path
    by which the interface already brings the window back to rest.

    Every call runs the child through `process_log.run_logged`, also without
    `progress` and without `cancel` (plain script callers): the timeout is
    always FREECAD_SILENCE_TIMEOUT_S seconds of SILENCE, restarted by every
    line and heartbeat, and on expiry the whole process tree is killed
    (contract changed 2026-10-27, H5: before, that path used subprocess.run,
    an overall-duration cap that killed only the direct child).

    The SAME channel covers both halves of the 3D phase: the matching of the
    library models, which precedes this call, receives it from the caller
    (`attach_models` -> `model_candidates` -> `model_sizes` ->
    `kicad_models.measure_models`) and kills its own freecadcmd child -- the
    one measuring the candidates not yet in cache -- instead of waiting for
    the batch to finish. A cancellation requested during the matching thus
    exits immediately, through the same path (`Cad3DError(CAD_ABORT_MESSAGE)`),
    and this function does not even start the child of the solid.

    Output contract (see `_discard_outputs`): right before freecadcmd starts,
    the previous ``<name>.step``, ``.wrl`` and ``.preview.json`` are deleted;
    the child writes each export under `staging_path` and renames it only
    once that export succeeded; if the run fails, times out or is cancelled,
    whatever it published is deleted again. A WRL or preview without its
    success marker is deleted too. So a 3D file present after this call is
    always a complete export of the latest run.
    """
    body.validate()
    if not is_finite_number(body_rotation_deg):
        raise ValueError(f"body_rotation_deg must be a finite number, got {body_rotation_deg!r}")
    body_turn = _normal_degrees(body_rotation_deg)
    if body_turn and edge_contacts:
        raise ValueError("a turned body cannot carry edge contacts")
    fillets = fillets or []
    face_texts = face_texts or []
    for f in fillets:
        f.validate()
    for t in face_texts:
        t.validate()

    # SAFETY BOUNDARY: the name is joined to output_dir below; a name holding
    # a separator or ".." would write outside it (2026-10-27 review, H4).
    # Same rule as the interface, through the shared helper.
    validate_output_name(name)
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
            # Only a real turn is written: a 0-degree payload stays the old one.
            **({"rotation_deg": body_turn} if body_turn else {}),
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
                "x_mm": t.x_mm, "y_mm": t.y_mm,
            }
            for t in face_texts
        ],
        "font_file": font_file,
        # The markings stay in the component payload (they serve whoever
        # matches the library models and whoever rereads the JSON), but the
        # FreeCAD script prints them only if the body explicitly asks for it.
        "show_markings_3d": bool(body.show_markings_3d),
        "show_pins_3d": bool(getattr(body, "show_pins_3d", True)),
    }
    payload.update(extra_payload or {})

    # Transport channel of the heartbeat: the FreeCAD script cannot import
    # this module (it runs in the embedded interpreter), so the interval
    # travels in the JSON instead of being rewritten by hand in there. Written
    # AFTER the update of extra_payload: a caller must not be able to
    # disconnect the heartbeat from the watchdog by mistake.
    #
    # The same number is the budget of the direction measure of a native
    # model (freecad_parts._landmark_budget_s): a measure that stayed silent
    # longer than the interval between two heartbeats could, on its own,
    # turn a healthy run into the silence the watchdog kills. A single
    # knob, so they cannot diverge.
    payload["heartbeat_interval_s"] = heartbeat_interval(FREECAD_SILENCE_TIMEOUT_S)
    # Staging names of the three shipped outputs: the child writes each one
    # there and renames it only after that export succeeded (see
    # _published_outputs). Written after extra_payload for the same reason as
    # the heartbeat: no caller may point the child back at the final names.
    published = _published_outputs(step_path, wrl_path, mesh_path, fcstd_path)
    payload["staging_paths"] = {key: str(staged) for key, (_, staged) in published.items()}
    # User cache folders (index of the native model directions): the
    # child cannot import kicadgen.app_paths, so it receives them here, with
    # KICADGEN_DATA_DIR already applied.
    from . import model_size_cache
    payload["cache_dirs"] = [str(folder) for folder in model_size_cache.user_cache_dirs()]

    # Suffix other than ".json": freecadcmd tries to open as a document every
    # extra argument with an extension it recognises as an importable format
    # (including .json, because of the FEM YAML/JSON importer), causing
    # a noisy exception (harmless, but to be avoided) after the script runs.
    #
    # Serialised BEFORE the file exists (2026-10-29 review, H7): a value that
    # is not JSON used to surface as a bare TypeError and leave the temporary
    # file behind. Now it is a Cad3DError, and a write failure removes the file.
    try:
        params_text = json.dumps(payload)
    except (TypeError, ValueError) as exc:
        raise Cad3DError(tx("Parametri del modello STEP non validi: {exc}", exc=exc)) from exc
    json_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".kicadgen_params", delete=False, encoding="utf-8"
        ) as tmp:
            json_path = tmp.name
            tmp.write(params_text)
    except BaseException:
        if json_path is not None:
            try:
                os.unlink(json_path)
            except OSError:
                pass
        raise

    launched = False
    try:
        try:
            if cancel is not None and cancel.is_set():
                # Give-up already requested during the model matching: do not
                # start a process that would be killed at its first breath.
                raise Cad3DError(CAD_ABORT_MESSAGE)
            # From here on the previous outputs are gone: whatever happens to this
            # run, no older STEP/WRL/preview can be mistaken for its result.
            _discard_outputs(published)
            launched = True
            # EVERY caller goes through run_logged: with or without a listener,
            # with or without a cancel channel. Reading the child's lines is
            # what makes the silence watchdog restart at every heartbeat, the
            # cancel work and the whole process tree die on expiry; the old
            # subprocess.run branch for plain script callers could only cap
            # the OVERALL duration and killed only the direct child (review
            # 2026-10-27, H5, completed 2026-10-27). The child's environment is
            # run_logged's default, the same copy of os.environ with
            # PYTHONDONTWRITEBYTECODE=1 that `child_environment` builds, and it
            # decodes the child's UTF-8 output (reconfigured stdout, JSON
            # warnings) with errors="replace".
            from .process_log import ProcessCancelled, run_logged
            # freecadcmd, once the script is done, tries to open as a document every
            # extra argument -- i.e. OUR parameter file -- and writes
            # «File format not supported: <that file>». It is expected by
            # construction and harmless: it stays in freecad.log, but it does not reach
            # the window, where it looked like an error (user, 2026-09-26). The
            # criterion is the exact path of the file we pass, not the
            # FreeCAD text: a line naming other files always passes.
            params_names = {json_path, json_path.replace("\\", "/"), Path(json_path).name}
            # Without a listener (script callers) the lines are read and logged
            # all the same: nobody is notified, but the watchdog still sees them.
            user_progress = progress or (lambda line: None)

            def progress(line, _forward=user_progress):  # noqa: F811 - filter declared above
                """Forward a child line, holding back FreeCAD's complaint about our params file."""
                if "not supported" in str(line) and any(n in str(line) for n in params_names):
                    return
                _forward(line)
            # Seconds of SILENCE, not of work: the count restarts at every line
            # FreeCAD prints, and every long step beats (see
            # heartbeat_interval_s in the payload above). A limit on the
            # overall duration killed the work of the following components
            # because of the first slow import, and it was all the more likely
            # the larger the module was.
            try:
                # The channel is passed ONLY when present: so the signature
                # seen by non-interactive callers (and their replacements
                # in the tests) stays as before.
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
        finally:
            try:
                os.unlink(json_path)
            except OSError:
                pass

        output = (result.stdout or "") + (result.stderr or "")

        if "KICADGEN_STEP_OK" not in output or not step_path.is_file():
            tail = "\n".join(output.strip().splitlines()[-15:]) or tx("(nessun output)")
            raise Cad3DError(tx("Generazione STEP fallita.\n\nOutput freecadcmd:\n{tail}", tail=tail))
        # The marker and the file are not enough (2026-10-29 review, H8): a
        # STEP cut short -- a full disk, a writer killed after the rename --
        # still exists. The ISO 10303-21 trailer is the last thing a writer
        # emits, so its absence is a structural proof of truncation; such a
        # run fails like any other and the handler below discards its outputs.
        if not _step_is_complete(step_path):
            tail = "\n".join(output.strip().splitlines()[-15:]) or tx("(nessun output)")
            raise Cad3DError(tx("Generazione STEP fallita: il file STEP e' incompleto (manca "
                                "la chiusura END-ISO-10303-21;).\n\nOutput freecadcmd:\n{tail}",
                                tail=tail))

        # Script warnings (fillet not applicable, face not found...): the
        # generation still succeeds, but without reporting them the user would see
        # a model lacking a feature without understanding why.
        warnings = [
            child_warning_text(line.split(CHILD_WARNING, 1)[1])
            for line in output.splitlines() if CHILD_WARNING in line
        ]

        # Colours in the STEP: Part.export writes only the geometry, and KiCad
        # would show everything grey. The AP214 entities are added starting
        # from the mesh colours, which list the pieces in the same order.
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
                # Staged and renamed like the child's exports: a failure or a
                # kill while writing leaves the complete, uncoloured STEP.
                staged_step = published["step"][1]
                staged_step.write_text(colorize_step(step_text, colors), encoding="utf-8")
                replace_file(staged_step, step_path)
            # Colouring is cosmetic: ANY ordinary error here (a preview JSON
            # that is not an object, a part that is not a dict, a non-numeric
            # "solids") leaves the complete uncoloured STEP and a warning. It
            # must never reach the handler below, which deletes the outputs.
            # BaseException (cancel, KeyboardInterrupt) still propagates.
            except Exception as exc:  # noqa: BLE001 - only the colours may fail
                warnings.append(tx("STEP esportato senza colori: {exc}", exc=exc))

        # An optional output without its success marker is not a result of
        # this run, even if a file with its name exists (e.g. the child died
        # between the rename and the print): it must not ship.
        for key, marker in (("wrl", "KICADGEN_WRL_OK"), ("mesh", "KICADGEN_MESH_OK"),
                            ("fcstd", "KICADGEN_FCSTD_OK")):
            if marker not in output:
                _remove_output(published[key][0], quiet=True)

        return {
            "step_path": step_path,
            "fcstd_path": fcstd_path if "KICADGEN_FCSTD_OK" in output and fcstd_path.is_file() else None,
            "wrl_path": wrl_path if "KICADGEN_WRL_OK" in output and wrl_path.is_file() else None,
            "mesh_path": mesh_path if "KICADGEN_MESH_OK" in output and mesh_path.is_file() else None,
            "warnings": warnings,
        }
    except BaseException:
        # Failed, cancelled or killed after launch: remove whatever the
        # child published, so the folder never holds a 3D file that could
        # be mistaken for the result of this run (see _discard_outputs).
        if launched:
            _discard_outputs(published, quiet=True)
        raise
    finally:
        # A staging file is never a result, not even after a clean run.
        _discard_staging(published)
