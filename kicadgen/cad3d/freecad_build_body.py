"""@brief Build and export colored board geometry inside headless FreeCAD.

It is not meant to be imported by ordinary code: it only runs through
`freecadcmd freecad_build_body.py <json_path>` (see step_generator.py),
where FreeCAD/Part/Draft are modules supplied by the FreeCAD environment
itself.

It reads a JSON file with the body (a box), the pins (cylinders) and the
post-processing annotations (fillets on edges, text printed on faces --
see kicadgen.cad3d.annotations for the accepted names), builds the
document and exports STEP (always) and VRML (best effort, not fatal when
it fails).

Every shipped export (STEP, WRL, preview JSON) is written under a staging
name and renamed onto its final name only after that export completed, so
a killed or failed run never leaves a truncated file under a final name.

Geometric classification of faces/edges: the body is always an
axis-aligned box (offset_x/y, length/width/height), so faces and edges are
classified by comparing the coordinates of the centre of mass (faces) or
of the midpoint (edges) with the known bounds of the box, within a
tolerance.
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
    """Warning for the parent process, which translates it into the user's language.

    In here (the FreeCAD interpreter) there is no translation catalog: the
    Italian TEMPLATE is sent with its values, as JSON, after the
    `KICADGEN_WARN:` marker; the parent formats it with `messages.tx`
    (`messages.child_warning_text`). A value that is not JSON becomes text.
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
        """@brief Prepare the heartbeat of one step.

        @param step Name of the step, printed in every beat.
        @param interval Seconds between beats; None = the job's interval.
        """
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
    """Classify a planar face of the box as one of the FACE_NAMES.

    Returns None for faces that cannot be classified: after a fillet the
    solid also contains cylindrical/toroidal blend surfaces, which match
    none of the 6 nominal faces of the box.
    """
    try:
        normal = face.normalAt(0, 0)
        com = face.CenterOfMass
    except Exception:  # noqa: BLE001 - surface cannot be evaluated: not classifiable
        return None
    if abs(normal.z) > 0.9:
        return "top" if com.z > z1 / 2 else "bottom"
    if abs(normal.x) > 0.9:
        return "left" if com.x < (x0 + x1) / 2 else "right"
    if abs(normal.y) > 0.9:
        return "front" if com.y < (y0 + y1) / 2 else "back"
    return None


def _edge_name(edge, x0, y0, x1, y1, z1):
    """Classify an edge of the box as one of the EDGE_NAMES (see annotations.py).

    Returns None for edges that cannot be classified: the arcs produced by a
    fillet are closed curves with a single vertex, so they lack the two
    distinct end points the direction-based classification relies on.
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


#: Box face -> (normal axis, side). Used to know which strip of which face
#: a fillet consumes.
_FACE_SIDES = {"left": ("x", 0), "right": ("x", 1), "front": ("y", 0),
               "back": ("y", 1), "bottom": ("z", 0), "top": ("z", 1)}


def _fillet_conflicts(fillets, extents):
    """Fillets the geometry cannot contain, with the reason.

    A fillet of radius r on the edge between faces F1 and F2 removes a strip
    r wide from each of them: on F2, on the F1 side, along the axis normal
    to F1 (and vice versa). On every face, the two fillets of the opposite
    sides along one axis must therefore add up to LESS than the extent of
    that axis; at an equal or larger sum the face disappears and OpenCascade
    only answers "BRep_API: command not done". A geometric constraint, no
    calibrated threshold.

    @param fillets Entries {"edge": name, "radius_mm": r}; names as EDGE_NAMES.
    @param extents {"x": length, "y": width, "z": height} of the box, mm.
    @return {edge_name: (face, axis, sum, extent)} for the edges involved in
        at least one violated constraint; empty when everything is buildable.
    """
    # (face, axis) -> [maximum radius low side, high side] and edges
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
    """Apply the fillets grouped by radius: edges sharing a radius go into a
    single makeFillet, so they are all blended on the still intact geometry
    (applying them one at a time can fail when two adjacent edges meet at
    the same vertex).

    Each group is isolated in try/except: an impossible fillet (radius too
    large for the geometry) is reported but does not fail the whole STEP
    generation.
    """
    x0, y0, x1, y1, z1 = bounds

    # Impossible fillets discarded BEFORE asking OpenCascade for them, with the
    # measured reason: the attempt would fail anyway with an opaque error and
    # would drag the good edges of the same radius into the failure.
    conflicts = _fillet_conflicts(fillets, {"x": x1 - x0, "y": y1 - y0, "z": z1})
    for name, (face, axis, total, extent) in sorted(conflicts.items()):
        _warn("raccordo su '{name}' non applicato: sulla faccia '{face}' i raccordi "
              "sommano {total:g} mm lungo {axis}, ma la faccia misura solo "
              "{extent:g} mm",
              name=name, face=face, total=total, axis=axis.upper(), extent=extent)
    fillets = [f for f in fillets if f["edge"] not in conflicts]

    groups = _fillet_groups(fillets)
    # The OpenCascade outcome depends on the order of the groups (audit 25/09:
    # r=0.5 on the top edge before r=3 on the corners made the corners fail;
    # in the reverse order both succeeded). The canonical order is tried first,
    # then the others, and the one applying the most edges is kept:
    # no calibrated rule, only the geometry OpenCascade accepts.
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


#: Beyond this number of radius groups not all permutations are tried
#: (4! = 24 attempts): only the canonical order and its reverse.
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
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            failed.append((radius, wanted, exc))
    return shape, applied, failed


#: Margin, in mm, kept between a face text and the edges of its face.
TEXT_FACE_MARGIN_MM = 0.2
#: Below this font size a printed text is a smudge: the fit still applies
#: (overflowing is worse) but the script says so.
TEXT_LEGIBLE_SIZE_MM = 0.3


def _build_face_text_solids(shape, texts, bounds, font_file):
    """Build the texts as thin solids LAID on the faces.

    The body is not engraved: each text is a separate layer of "paint" (like
    a silkscreen), returned on its own so it can have its own colour in the
    export. A small part of the thickness is sunk into the body to avoid
    z-fighting between the two surfaces in contact.
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

        # Same glyph builder as the silk labels: a space has no wire and a
        # glyph with a hole (0, 8, A) needs the bullseye face maker; building
        # Part.Face per glyph failed on both ("no wires in list", 2026-10-10).
        solid = _text_solid(t["text"], t["size_mm"], thickness + sink, font_file)
        if solid is None:
            _warn("scritta '{text}' non costruibile con il font {font}", text=t["text"],
                  font=font_file)
            continue
        # Fitted to the face it lies on, like the silk labels (`_place_text`
        # with bounds): a writing longer than the body was simply overflowing
        # (review B of 2026-10-10). Available extent along the reading
        # direction and the text's up direction, a margin on each side.
        face_box = face.BoundBox
        if abs(normal.z) > 0.9:
            available = (face_box.XLength, face_box.YLength)
        else:
            available = (max(face_box.XLength, face_box.YLength), face_box.ZLength)
        text_box = solid.BoundBox
        factor = min(1.0,
                     max(available[0] - 2 * TEXT_FACE_MARGIN_MM, 0.0) / max(text_box.XLength, 1e-9),
                     max(available[1] - 2 * TEXT_FACE_MARGIN_MM, 0.0) / max(text_box.YLength, 1e-9))
        if factor < 1.0:
            fitted = _text_solid(t["text"], t["size_mm"] * factor, thickness + sink, font_file)
            if fitted is None:
                continue
            solid = fitted
            if t["size_mm"] * factor < TEXT_LEGIBLE_SIZE_MM:
                _warn("scritta '{text}' ridotta a {size:.2f} mm per stare nella faccia "
                      "'{face}': poco leggibile", text=t["text"], size=t["size_mm"] * factor,
                      face=t["face"])

        # Centre the text block on the XY origin, with its base at Z=0.
        text_bbox = solid.BoundBox
        solid.translate(FreeCAD.Vector(
            -(text_bbox.XMin + text_bbox.XMax) / 2,
            -(text_bbox.YMin + text_bbox.YMax) / 2,
            -text_bbox.ZMin,
        ))

        # Rotation + placement on the face must be COMPOSED with the
        # centring just done: assigning .Placement directly would
        # overwrite it, leaving the text off centre (it spilled off the body).
        # The frame is explicit (normal, up = +Z): Rotation(Z, normal)
        # chose the minimal rotation, which flipped the text upside down on the
        # back face and set it vertical on the side ones.
        x_axis, y_axis, z_axis = _text_frame((normal.x, normal.y, normal.z))
        matrix = FreeCAD.Matrix(x_axis[0], y_axis[0], z_axis[0], 0,
                                x_axis[1], y_axis[1], z_axis[1], 0,
                                x_axis[2], y_axis[2], z_axis[2], 0,
                                0, 0, 0, 1)
        target = FreeCAD.Placement(matrix)
        anchor = center
        if t.get("x_mm") is not None and t.get("y_mm") is not None and abs(normal.z) > 0.9:
            # Label tied to a point of the top/bottom face (a pin number next
            # to its lead): same plane as the face, given XY.
            anchor = FreeCAD.Vector(float(t["x_mm"]), float(t["y_mm"]), center.z)
        target.Base = anchor - normal * sink
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
    """'#rrggbb' -> (r, g, b) in 0..1, falling back to grey when the value is dirty."""
    try:
        h = color.lstrip("#")
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except Exception:  # noqa: BLE001 - invalid colour: neutral grey
        return (0.5, 0.5, 0.5)


# KiCad reads .wrl files in units of 0.1 inch, not millimetres: the
# coordinates must be divided by 2.54 or the model appears 2.54 times larger
# in the 3D viewer. The .step instead stays in mm.
_VRML_SCALE = 1.0 / 25.4 * 10.0


def _write_vrml(path, tessellated):
    """Write a VRML97 file with one coloured Shape for each piece (body, silkscreen, pins).

    FreeCAD's Mesh.export() would write everything in a single default grey:
    the colours are lost because in headless mode there are no ViewObjects
    from which the export takes the appearance. Here the file is written
    directly.
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
    """Horizontal slice of the PCB prism, between two Z levels.

    Substrate and soldermask are the SAME outline (fillets and half-holes
    included) cut at different levels: the three layers always match.
    """
    x0, y0, x1, y1, _ = bounds
    clip = Part.makeBox(x1 - x0 + 4, y1 - y0 + 4, z_to - z_from,
                        FreeCAD.Vector(x0 - 2, y0 - 2, z_from))
    return outline.common(clip)


def _text_solid(text, size, thickness, font_file, bold_font=None):
    """Text as a compound of thin solids, centred on the XY origin, base at Z=0."""
    wires = None
    for font in (bold_font, font_file):
        if not font:
            continue
        try:
            wires = Part.makeWireString(text, font, size, 0.0)
            break
        except Exception:  # noqa: BLE001 - missing font: try the next one
            continue
    if wires is None:
        return None
    solids = []
    for glyph in wires:
        if not glyph:
            continue
        try:
            face = Part.makeFace(glyph, "Part::FaceMakerBullseye")
        except Exception:  # noqa: BLE001 - glyph cannot be closed: skip it
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


def _marking_to_print(component, data):
    """@brief Text to print on top of a component, or the empty string.

    @param component Entry of `components` in the payload (field `marking`).
    @param data Whole payload: it carries the `show_markings_3d` consent.
    @return The cleaned marking when the payload asks to print it, else "".
    @note The standard path rebuilds generic mechanical envelopes, with no
        writing: the marking read from the photograph ("330?", "123", the
        full string of a radio module) is evidence for matching library
        models and information for the user, not geometry. A payload
        without the field -- such as those written before the field existed
        -- prints nothing, because the absence of a choice is not a choice.
        The module's contact labels (`silk_texts`) are footprint silkscreen,
        governed elsewhere, and do not pass through here.
    """
    if data.get("show_markings_3d", False) is not True:
        return ""
    return str(component.get("marking") or "").strip()


def _safe(name, index):
    cleaned = "".join(c if c.isalnum() else "_" for c in str(name or "").strip())
    return f"Cmp{index}_{cleaned}"[:40] if cleaned else f"Cmp{index}"


_FB_SWAP = {"front": "back", "back": "front"}


def _body_local_point(x, y, body_turn):
    """@brief Bring a model-frame point into the frame of a turned body.

    @param x Point X in the model frame (Y up, after _flip_y), mm.
    @param y Point Y in the model frame, mm.
    @param body_turn Body `rotation_deg` after _flip_y, degrees; the body is
        turned by -body_turn about Z through the origin.
    @return (x, y) in the body's axis-aligned frame: turning it by -body_turn
        gives back the input point.
    """
    angle = math.radians(body_turn)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return (x * cos_a - y * sin_a, x * sin_a + y * cos_a)


def _swap_front_back(name):
    return "-".join(_FB_SWAP.get(part, part) for part in str(name).split("-"))


def _flip_y(data):
    """Bring the payload from the footprint system (Y down, as in KiCad) to
    the 3D model system (Y up).

    KiCad shows models with the Y axis inverted with respect to the
    footprint: a model built with footprint coordinates appears mirrored in
    the 3D viewer -- invisible on symmetric grids, obvious on a module with
    different sides and writing. Shapes are symmetric and do not change; Y
    positions and angles are negated. The front/back names of fillets and
    texts are NOT touched: since convention 2 (2026-09-25) they are already
    those of the 3D model (front = the -Y side, the «Fronte» face of the
    cube), the same ones with which `_edge_name` and `_face_name` classify
    the solid below.
    """
    body = data["body"]
    body["offset_y_mm"] = -(body["offset_y_mm"] + body["width_mm"])
    # A body turned about the footprint origin (single package with an
    # orientation) is reflected like an extra box: the angle changes sign
    # with the axis. Absent means 0, so older payloads are unchanged.
    if "rotation_deg" in body:
        body["rotation_deg"] = -float(body["rotation_deg"])
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
        # top/bottom of the module (horizontal edges) swap like front/back
        c["edge"] = {"top": "bottom", "bottom": "top"}.get(c["edge"], c["edge"])
        # The side of a through hole and the closed hole of a double contact
        # are geometry like the rest: they must be mirrored with it, otherwise the
        # label would end up facing outwards and the hole on the wrong side.
        if "board_edge" in c:
            c["board_edge"] = {"top": "bottom", "bottom": "top"}.get(c["board_edge"], c["board_edge"])
        through = c.get("through_hole")
        if isinstance(through, dict) and "y" in through:
            through["y"] = -float(through["y"])
    for box in data.get("extra_boxes", []):
        box["y"] = -(box["y"] + box["width_mm"])
        # A box turned about Z is reflected like a component: the angle
        # changes sign with the axis (review B of 2026-10-10: an SMD terminal
        # at 30 degrees came out at -30, the mirror image of its pad).
        if "rotation_deg" in box:
            try:
                box["rotation_deg"] = -float(box["rotation_deg"])
            except (TypeError, ValueError, OverflowError):
                pass
    # Anchored face texts (pin numbers beside the leads) are positions in the
    # footprint system like the pins: same reflection, or the label would
    # land on the far side of the body (seen on the TEL8 try, 2026-10-10).
    for t in data.get("face_texts", []) or []:
        if isinstance(t.get("y_mm"), (int, float)) and not isinstance(t.get("y_mm"), bool):
            t["y_mm"] = -float(t["y_mm"])
    data["pins"] = [[n, x, -y] for n, x, y in data.get("pins", [])]
    return data


#: Direction towards the inside of the board for each side, in the 3D
#: model's frame (Y up, after _flip_y).
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
        # Second argument: stable reason code the contact loop translates
        # (a plain ValueError, so tests that lift this function stay valid).
        raise ValueError("through pad diameter must exceed hole diameter", "pad_not_wider_than_hole")
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
            # Inwards past the annular ring, not over the hole: the number
            # would cover exactly the metal that is needed to recognise it.
            reach = pad_w / 2 + 0.8
            label = [cx + inward[0] * reach, cy + inward[1] * reach]
    else:
        # Half hole: sleeve cut by the edge (clip on the intact outline)
        # plus the rectangular pad on both faces.
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
            # The closed hole of a double contact: it used to be only cut,
            # so a dark dot showed instead of a golden annular ring.
            tx, ty = float(through["x"]), float(through["y"])
            tr = float(through["radius"])
            found, holes = _through_features(tx, ty, tr, float(through["pad_diameter_mm"]),
                                             z1, plating, pad_t, clip=False)
            if isinstance(stadium, dict):
                # Only the barrel: the rings would sit inside the continuous
                # pad, on the same faces (see @note).
                found = [t for t in found if t["z0"] == 0.0 and t["z1"] == z1]
            tubes += found
            drills += holes
            # The edge pad can reach over the hole: it must be drilled
            # there too, or it would cover the mouth of the through hole.
            bores.append((tx, ty, tr))
        if isinstance(stadium, dict):
            # A single golden pad from the edge to past the annular ring, on
            # both faces: no bare board between half hole and hole.
            nx, ny = _INWARD[edge]
            ex = x0 if edge == "left" else x1 if edge == "right" else cx
            ey = y0 if edge == "top" else y1 if edge == "bottom" else cy
            s_len = float(stadium["length_mm"])
            s_r = float(stadium["width_mm"]) / 2
            if s_len < s_r:
                raise ValueError("stadium shorter than its round end", "stadium_too_short")
            for z in (z1, -pad_t):
                stadiums.append(dict(x=ex, y=ey, nx=nx, ny=ny, length=s_len, r=s_r,
                                     z0=z, z1=z + pad_t))
            pad_len = max(pad_len, s_len)
        # The number goes past the end of the pad, not at a fixed distance
        # from the hole: pads can have different lengths per side.
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
    """@brief Make a Windows path immune to the MAX_PATH limit.

    The extended prefix disables the 260-character limit for Win32 calls; it
    requires an absolute path with native separators.

    @param path Path to protect; made absolute first.
    @return The path with the Win32 extended-length prefix on Windows,
        unchanged elsewhere or when it already carries the prefix.
    """
    path = os.path.abspath(str(path))
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    return "\\\\?\\" + path


def _staging_path(data, key, final):
    """@brief Path where one export is written before being published.

    @param data Job JSON; ``staging_paths[key]`` is set by step_generator.
    @param key ``"step"``, ``"wrl"``, ``"mesh"`` or ``"fcstd"``.
    @param final Final path of that export.
    @return The parent's staging path, or a sibling with ``.kicadgen-partial``
        before the extension (kept: FreeCAD picks the STEP exporter from it).
    """
    staged = (data.get("staging_paths") or {}).get(key)
    if staged:
        return staged
    root, ext = os.path.splitext(str(final))
    return root + ".kicadgen-partial" + ext


def _publish(staged, final):
    """@brief Atomically rename a finished export onto its final name.

    @note The rename is the only way an export reaches its final name, so a
        kill during the write leaves at most a staging file, never a
        truncated file that looks current. Transient Windows refusals
        (antivirus, indexer) are retried briefly, as kicadgen.fsutil does.
    """
    pause = 0.05
    for attempt in range(5):
        try:
            os.replace(staged, final)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(pause)
            pause *= 2


def _discard(path):
    """Remove a staging file after a failed export; cleanup never raises."""
    try:
        os.remove(path)
    except OSError:
        pass


def _save_fcstd(doc, fcstd_path):
    """@brief Save the FreeCAD document working around MAX_PATH on Windows.

    `saveAs` opens the file with the classic Win32 APIs and stops at 260
    characters, while the STEP export goes through: a component with a long
    name in a nested folder therefore lost only its FreeCAD source. The
    direct way is tried first -- where it works the behaviour does not
    change -- and only when it fails the document is saved to a short path
    and copied to its destination with the extended prefix, which has no
    such limit.

    @param doc FreeCAD document to save.
    @param fcstd_path Final `.FCStd` path.
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


def _export_fcstd(doc, fcstd_path, staged):
    """@brief Save the FreeCAD source under its staging name and publish it.

    @param doc FreeCAD document to save.
    @param fcstd_path Final `.FCStd` path.
    @param staged Staging path (same folder, so the rename is atomic).
    @note Same contract as the STEP, WRL and preview (2026-10-27 review,
        H8): the final name is reached only by a rename after the save
        returned, so a kill during the save leaves at most a staging file
        and a failed save leaves NO file -- the parent deleted the previous
        source before the run and removes anything without the success
        marker. Best effort: a failure is a warning, never a failed job,
        because the STEP and the preview do not depend on it.
    """
    try:
        with _StepHeartbeat("fcstd_save"):
            _save_fcstd(doc, staged)
            # Extended-length paths on both sides: saveAs may have needed
            # them, and the rename must not fail where the save succeeded.
            _publish(_long_path(staged), _long_path(fcstd_path))
        print(f"KICADGEN_FCSTD_OK:{fcstd_path}")
    except Exception as exc:  # noqa: BLE001 - FreeCAD source is best-effort
        _discard(_long_path(staged))
        print(f"KICADGEN_FCSTD_FAILED:{exc}")
        # The marker above stays for whoever reads the output line by line,
        # but only the KICADGEN_WARN: lines reach the user: without
        # this one the FreeCAD source disappeared without anyone saying so.
        _warn("sorgente FreeCAD non salvata ({exc}); STEP e anteprima non ne "
              "risentono",
              exc=exc)


def main():
    """@brief Build body, pins, substrate, fillets and texts inside FreeCAD from the JSON job and export them.

    @return None: the results are the exported files (STEP always, WRL and
        preview mesh best effort) named by the job, published from staging
        names only after each export completed.
    """
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
    substrate = data.get("substrate")  # only for modules on a PCB
    silk_color = data.get("silk_color_hex", "#e8e8e0")

    doc = FreeCAD.newDocument("kicadgen_body")

    x0, y0 = body["offset_x_mm"], body["offset_y_mm"]
    x1, y1, z1 = x0 + body["length_mm"], y0 + body["width_mm"], body["height_mm"]
    bounds = (x0, y0, x1, y1, z1)
    # Body turn about the footprint origin (Z axis), model frame after
    # _flip_y. The body, its fillets and its face texts are built in the
    # body's own axis-aligned frame (where faces and edges are classified by
    # `bounds`) and turned at the end; holes given in the footprint frame are
    # brought into that frame first. 0 leaves every step exactly as before.
    body_turn = float(body.get("rotation_deg", 0.0) or 0.0)

    body_shape = Part.makeBox(
        body["length_mm"], body["width_mm"], body["height_mm"],
        FreeCAD.Vector(x0, y0, 0),
    )
    # The intact box is used to clip the platings: after the half-hole
    # cuts, that space no longer exists in the solid.
    uncut_body = body_shape.copy()

    if fillets:
        body_shape = _apply_fillets(body_shape, fillets, bounds)

    # Contact platings, described BEFORE drilling and slicing the
    # board: every barrel must be drilled into the substrate at its outer
    # radius (see _through_features), and after the cut into layers that is
    # no longer possible.
    plating = data.get("plating_thickness_mm", 0.06)
    pad_t = 0.025
    plated = []
    for contact in data.get("edge_contacts", []):
        try:
            plated.append((contact, _contact_features(contact, bounds, plating, pad_t)))
        except Exception as exc:  # noqa: BLE001 - one contact less is not fatal
            # The reasons written by this script have a stable code: translated
            # text. FreeCAD/OCC errors stay in the text with {exc}.
            reason = exc.args[1] if isinstance(exc, ValueError) and len(exc.args) == 2 else None
            if reason == "pad_not_wider_than_hole":
                _warn("contatto {get} non generato: piazzola non piu' larga del foro",
                      get=contact.get('number'))
            elif reason == "stadium_too_short":
                _warn("contatto {get} non generato: piazzola allungata piu' corta della sua "
                      "estremita' tonda", get=contact.get('number'))
            else:
                _warn("contatto {get} non generato: {exc}", get=contact.get('number'), exc=exc)
    drills = [drill for _, features in plated for drill in features["drills"]]

    # Half holes and holes (castellated modules): cylinders of which only the
    # inner part stays in the solid. After the fillets, so the corner fillets do
    # not overlap the cut. A hole already covered by the drilling of its
    # barrel is not cut twice: every boolean cut is costly, and the board
    # has one per hole as before.
    cutters = [n for n in data.get("notches", []) if not _covered_by_drill(n, drills)]
    cutters += [{"x": x, "y": y, "radius": r} for x, y, r in drills]
    if body_turn:
        cutters = [dict(n, **dict(zip(("x", "y"), _body_local_point(n["x"], n["y"], body_turn))))
                   for n in cutters]
    cut_heartbeat = _StepHeartbeat("holes").__enter__()
    # Every iteration catches its own exceptions, so the exit below
    # is always reached: the heartbeat does not outlive the step.
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
        except Exception as exc:  # noqa: BLE001 - one half hole less is not fatal
            _warn("semi-foro non applicato in ({get}, {get2}): {exc}",
               get=notch.get('x'),
               get2=notch.get('y'),
               exc=exc)
    cut_heartbeat.__exit__(None, None, None)

    body_color = body.get("color_hex", "#1a1a1a")
    pin_color = data.get("pin_color_hex", "#b0b0b0")

    # (FreeCAD object, colour): every piece stays separate so it can have
    # its own colour in STEP, VRML and preview.
    colored = []
    # Point where the GUI writes a contact's number: next to the hole,
    # towards the inside of the board, not over the hole where it would cover
    # the metal that is needed to recognise it.
    label_anchors = {}
    metadata = {}

    def turned(shape):
        """Turn a piece built in the body frame into the footprint frame."""
        if body_turn:
            shape.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), -body_turn)
        return shape

    def add(name, shape, color):
        obj = doc.addObject("Part::Feature", name)
        obj.Shape = shape
        colored.append((obj, color))
        return obj

    if substrate:
        # Real PCB: FR4 substrate between two thin soldermask layers. Seen
        # edge-on it reads as a board, not as a painted block.
        mask_t = float(substrate.get("mask_thickness_mm", 0.02))
        add("Bottom_mask", turned(_slab(body_shape, 0.0, mask_t, bounds)), body_color)
        add("FR4_substrate", turned(_slab(body_shape, mask_t, z1 - mask_t, bounds)),
            substrate.get("color_hex", "#2f3f1f"))
        add("Body", turned(_slab(body_shape, z1 - mask_t, z1, bounds)), body_color)
    else:
        # Face texts are classified on the body before it turns: build them
        # first, then turn the body (a copy keeps the classification intact).
        add("Body", turned(body_shape.copy()) if body_turn else body_shape, body_color)

    for i, (text_solid, color) in enumerate(
        _build_face_text_solids(body_shape, face_texts, bounds, font_file)
    ):
        add(f"Serigrafia{i}", turned(text_solid), color)

    # Contacts: plated sleeve in the hole + pad on the top and bottom
    # face, all of the same metal (golden ENIG or tin),
    # for half holes as for through holes. Without them, the contacts would be
    # visible neither from above nor from below.
    for contact, features in plated:
        try:
            shape = _contact_shape(features, uncut_body)
            add(f"Pin{contact['number']}", shape, contact.get("color_hex", pin_color))
            label_anchors[f"Pin{contact['number']}"] = features["label"]
        except Exception as exc:  # noqa: BLE001 - one contact less is not fatal
            _warn("contatto {get} non generato: {exc}", get=contact.get('number'), exc=exc)

    # Module silkscreen: labels next to the contacts. No frame around the
    # edge (user, 2026-09-26): it was a rectangle that no data describes.
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

    # Components mounted on the module: shape by type (see freecad_parts).
    components = data.get("components", [])
    if components:
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import freecad_parts
        # The heartbeat interval comes from the JSON, derived from the watchdog
        # timeout in kicadgen.cad3d.process_log: this script runs in FreeCAD's
        # embedded interpreter and cannot import that module,
        # so the number travels in the parameters instead of being rewritten here
        # -- two copies would drift apart at the first change and the watchdog
        # would go back to killing healthy imports.
        freecad_parts.set_heartbeat_interval(data.get("heartbeat_interval_s"))
        # Cache folders resolved by the parent (KICADGEN_DATA_DIR included).
        freecad_parts.set_cache_dirs(data.get("cache_dirs"))
        # Centre of the board in the model's frame: edge connectors
        # face off the board relative to THIS point, not to the origin, which
        # in the matrix generator sits on pin 1 and not at the centre.
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
                # The shapes are born with the long axis parallel to X and are rotated
                # around the component's centre: it is the orientation read
                # from the photo, not a deduction from the proportions.
                #
                # The whole block is isolated: a component with degenerate
                # dimensions (extraction estimate out of scale) must not make
                # the following ones nor the whole model disappear.
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
                    if path and component.get("_rendered_local_rotation_deg") is not None:
                        metadata[obj.Name]["model_local_rotation_deg"] = component["_rendered_local_rotation_deg"]
                    if component.get("_model_import_failed"):
                        metadata[obj.Name]["model_import_failed"] = True
                        metadata[obj.Name]["model_import_error"] = component.get(
                            "_model_import_error", "Model import failed")
                print(f"KICADGEN_COMPONENT_OK:{base}; parts={len(built)}", flush=True)

                # No silkscreen outline around the components (user,
                # 2026-09-27, like the board frame on 26/09): it was a
                # rectangle that no document or photo describes, and in the
                # model it read as real silkscreen of the board.

                # Only on explicit request of the payload: see _marking_to_print.
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
            except Exception as exc:  # noqa: BLE001 - one component less is not fatal
                _warn("componente '{get}' non generato: {exc}", get=component.get('name'), exc=exc)

    # Generic extra solids (box with its own colour).
    for i, box in enumerate(data.get("extra_boxes", [])):
        extra_shape = Part.makeBox(
            box["length_mm"], box["width_mm"], box["height_mm"],
            FreeCAD.Vector(box["x"], box["y"], box.get("z", 0)),
        )
        rotation = float(box.get("rotation_deg", 0.0))
        if rotation:
            centre = FreeCAD.Vector(
                box["x"] + box["length_mm"] / 2,
                box["y"] + box["width_mm"] / 2,
                box.get("z", 0) + box["height_mm"] / 2,
            )
            extra_shape.rotate(centre, FreeCAD.Vector(0, 0, 1), -rotation)
        add(box.get("name", f"Extra{i}"), extra_shape,
            box.get("color_hex", "#1a1a1a"))

    pin_radius = pin_diameter / 2.0
    # Switched off on request (Body3DParams.show_pins_3d): the holes remain.
    shown_pins = pins if data.get("show_pins_3d", True) else []
    if shown_pins and data.get("pin_style") == "male_header":
        # Male strip under a populated board: square pins and plastic
        # spacer, catalogue dimensions (see header_geometry).
        # `os` must be imported here: further down the function imports it again
        # (components), so for Python it is a local variable and without
        # this line it would not exist yet.
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import header_geometry
        # `header_pitches` (castellated modules) gives the declared pitch of
        # every pin, aligned with `pins`: without it, the pitch is derived from
        # the pins.
        for box in header_geometry.male_header_boxes_by_row(
                shown_pins, board_top_z=z1, pitches=data.get("header_pitches")):
            add(box["name"], Part.makeBox(
                box["dx"], box["dy"], box["dz"],
                FreeCAD.Vector(box["x"], box["y"], box["z"]),
            ), pin_color if box["role"] == "pin" else "#151515")
        shown_pins = []
    for pin_num, x, y in shown_pins:
        add(f"Pin{pin_num}", Part.makeCylinder(
            pin_radius, pin_length,
            FreeCAD.Vector(x, y, -pin_length),
            FreeCAD.Vector(0, 0, 1),
        ), pin_color)

    with _StepHeartbeat("recompute"):
        doc.recompute()
    objects = [obj for obj, _ in colored]

    step_colors_exported = False
    step_staged = _staging_path(data, "step", step_path)
    print(f"KICADGEN_EXPORT_START:{step_path}", flush=True)
    with _StepHeartbeat("step_export"):
        try:
            import Import
            styled = [(obj, getattr(color, "face_colors", None)
                       or [_hex_to_rgb(color)] * len(obj.Shape.Faces)) for obj, color in colored]
            Import.export(styled, step_staged)
            step_colors_exported = True
        except Exception as exc:
            _warn("export colori STEP fallito ({exc}); solo geometria", exc=exc)
            _discard(step_staged)
            Part.export(objects, step_staged)
    # Published only now that the export returned: a kill during the write
    # leaves the staging file, which the parent deletes.
    _publish(step_staged, step_path)
    if step_colors_exported:
        print(f"KICADGEN_STEP_COLORS_OK:{step_path}", flush=True)
    print(f"KICADGEN_STEP_OK:{step_path}", flush=True)

    if fcstd_path:
        _export_fcstd(doc, fcstd_path, _staging_path(data, "fcstd", fcstd_path))

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

    # Tessellated mesh: the GUI uses it for the preview of the REAL geometry,
    # and the Python generator uses it to colour the STEP (one colour per
    # solid, in the same export order).
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
                mesh_staged = _staging_path(data, "mesh", mesh_path)
                with open(mesh_staged, "w", encoding="utf-8") as fh:
                    json.dump({"parts": parts, "step_colors_exported": step_colors_exported}, fh)
                _publish(mesh_staged, mesh_path)
            print(f"KICADGEN_MESH_OK:{mesh_path}")
        except Exception as exc:  # noqa: BLE001 - preview is best-effort
            _discard(_staging_path(data, "mesh", mesh_path))
            print(f"KICADGEN_MESH_FAILED:{exc}")

    if wrl_path:
        wrl_staged = _staging_path(data, "wrl", wrl_path)
        try:
            with _StepHeartbeat("vrml_export"):
                _write_vrml(wrl_staged, tessellated)
                _publish(wrl_staged, wrl_path)
            print(f"KICADGEN_WRL_OK:{wrl_path}")
        except Exception as exc:  # noqa: BLE001 - VRML export is best-effort
            # No WRL at all rather than a partial one: the parent removed the
            # previous WRL before the run, and the footprint references the
            # STEP, so the library simply ships without a WRL.
            _discard(wrl_staged)
            print(f"KICADGEN_WRL_FAILED:{exc}")


main()
