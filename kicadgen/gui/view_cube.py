"""@brief Clickable navigation cube for the VTK 3D preview, as in CAD and slicer tools.

The cube sits in the top-right corner of the viewport and turns with the model.
Clicking a face looks at that face; clicking near an edge or a corner looks
along the diagonal of that edge or corner; the house under the cube restores
the initial isometric view, and the two circular arrows beside it turn the
view by 90 degrees clockwise or anticlockwise about the viewing axis (user,
2026-09-27). It replaces the row of «Vista: Iso, Sopra, …»
buttons (user request, 2026-09-23).

@note The cube is drawn by its own VTK renderer on an overlay layer, so it
    survives `Plotter.clear()` (which only empties the main renderer) and never
    enters the main renderer's bounds, picking or `reset_camera`.
@note Hit testing is analytic: the click becomes a ray through the cube
    renderer's camera and is intersected with the unit box. It does not depend
    on a VTK picker, which behaves differently on assemblies and off-screen.
@note The module also holds the VTK-free camera interpolation used to animate
    the view change a click asks for (CameraPose, interpolate_pose), so that it
    can be tested without a render window.
"""
from __future__ import annotations

import math
import unicodedata
from typing import NamedTuple, Optional

import numpy as np

#: Half side of the cube in its own world units (vtkAnnotatedCubeActor is a unit cube).
HALF = 0.5

#: Fraction of the half side beyond which a click counts as "near the border".
#: With 0.7 the outer 15% band of every face selects the edge or corner view,
#: the rest of the face selects the face itself -- the convention of CAD cubes.
#: It is an interaction choice, not a measured threshold.
EDGE_FRACTION = 0.7

#: Cube side and margin in logical pixels (DPI scaling is applied by Qt).
#: 120 px after the user found 96 px too small (note 7, 2026-09-23).
CUBE_PX = 120
MARGIN_PX = 10
#: The house keeps the proportions of the first design (26 px under a 96 px
#: cube, 4 px apart), so it scales with the cube instead of being re-tuned.
HOME_PX = round(CUBE_PX * 26 / 96)
HOME_GAP_PX = round(CUBE_PX * 4 / 96)
#: The rotation arrows have the size of the house and sit on its row, one on
#: each side, two gaps away so that the three targets read as separate.
ROLL_PX = HOME_PX
ROLL_GAP_PX = 2 * HOME_GAP_PX
#: Hit results of the two rotation arrows (see ViewCube.hit).
ROLL_CW = "roll_cw"
ROLL_CCW = "roll_ccw"

#: Largest fraction of the face side a label may cover, in both directions.
#: 0.84 leaves an 8% border on each side, so the text never touches the edge
#: lines; it is a visual choice, not a measurement.
TEXT_FACE_FRACTION = 0.84
#: Upper bound of the label scale: short labels ("TOP") would otherwise grow
#: to fill the face and look shouted. 0.28 is the scale of the first design.
MAX_TEXT_SCALE = 0.28
#: Smallest legible cap height of a label, in logical pixels. A label so long
#: that fitting it would shrink every face below this is shortened instead
#: (see fit_face_labels). All real labels of the four UI languages fit well
#: above it: with the longest ones (SINISTRA, ARRIERE) capitals are ~15 px.
MIN_TEXT_PX = 9.0

#: Unit directions of the faces and the preview view each one maps to. The
#: preview conventions (pyvista): "front" looks from -Y, "right" from +X.
FACE_VIEWS = {
    (0, 0, 1): "top", (0, 0, -1): "bottom",
    (0, -1, 0): "front", (0, 1, 0): "back",
    (1, 0, 0): "right", (-1, 0, 0): "left",
}

#: Default face labels (Italian source; the window passes translated ones).
DEFAULT_LABELS = {"top": "SOPRA", "bottom": "SOTTO", "front": "FRONTE",
                  "back": "RETRO", "right": "DESTRA", "left": "SINISTRA"}

_FACE_COLOR = (0.36, 0.39, 0.49)
_EDGE_COLOR = (0.62, 0.66, 0.78)
_Z_TEXT_ROTATION = 90
_TEXT_COLOR = (0.93, 0.94, 0.97)
_HOME_COLOR = (0.80, 0.83, 0.93)


def classify_cube_point(point, half: float = HALF,
                        edge_fraction: float = EDGE_FRACTION) -> Optional[tuple]:
    """@brief Direction selected by a point on the cube surface.

    @param point (x, y, z) on (or very near) the cube surface.
    @param half Half side of the cube.
    @param edge_fraction Border band, as a fraction of the half side.
    @return Tuple of -1/0/1 components: one non-zero for a face, two for an
        edge, three for a corner; None for a point that is not on the cube.
    """
    try:
        coords = [float(value) for value in point]
    except (TypeError, ValueError):
        return None
    if len(coords) != 3 or not all(math.isfinite(value) for value in coords):
        return None
    limit = half * edge_fraction
    # The face that was hit is the axis at the surface; the other axes only
    # count when the point lies in their border band.
    main = max(range(3), key=lambda axis: abs(coords[axis]))
    if abs(coords[main]) < half * 0.999:
        return None
    return tuple(
        (1 if coords[axis] > 0 else -1)
        if axis == main or abs(coords[axis]) >= limit else 0
        for axis in range(3))


def ray_box_entry(near, far, half: float = HALF) -> Optional[tuple]:
    """@brief First intersection of the segment near->far with the box [-half, half]^3.

    @param near Point where the pick ray enters the view volume.
    @param far Point where it leaves.
    @return The entry point, or None when the ray misses the box.
    @note Slab method; parallel components are handled explicitly.
    """
    t_min, t_max = 0.0, 1.0
    for axis in range(3):
        origin = near[axis]
        delta = far[axis] - origin
        if abs(delta) < 1e-12:
            if origin < -half or origin > half:
                return None
            continue
        t0 = (-half - origin) / delta
        t1 = (half - origin) / delta
        if t0 > t1:
            t0, t1 = t1, t0
        t_min, t_max = max(t_min, t0), min(t_max, t1)
        if t_min > t_max:
            return None
    return tuple(near[axis] + (far[axis] - near[axis]) * t_min for axis in range(3))


def view_for_direction(direction) -> Optional[str]:
    """@brief Name of the standard view of a face direction, None for edges/corners."""
    return FACE_VIEWS.get(tuple(direction)) if direction else None


def vector_text_safe(text: str, fallback: str = "") -> str:
    """@brief Make a label drawable by vtkVectorText, which only has ASCII glyphs.

    vtkVectorText silently skips any character outside printable ASCII: in
    French "ARRIÈRE" would be drawn as "ARRIRE". Accented letters are therefore
    folded to their base letter (NFKD decomposition without combining marks),
    as French and Italian typography commonly do on capitals.

    @param text Label, already upper-cased.
    @param fallback Label used when a character has no ASCII base letter
        (e.g. a script without Latin letters): a readable word in another
        language is better than a word with holes.
    @return Printable-ASCII text.
    """
    folded = "".join(char for char in unicodedata.normalize("NFKD", str(text))
                     if not unicodedata.combining(char))
    if all(32 <= ord(char) < 127 for char in folded):
        return folded
    return fallback if fallback and fallback != text else "".join(
        char for char in folded if 32 <= ord(char) < 127)


def vector_text_extent(text: str) -> tuple:
    """@brief (width, height) of a text as vtkVectorText draws it, at scale 1.

    @param text Printable-ASCII text.
    @return Extent in vtkVectorText units; (0, 0) for text without glyphs.
    @note vtkAnnotatedCubeActor builds its face labels with this same source
        and centres them on the face, so width x FaceTextScale is exactly the
        space the label takes on a face of side 1.
    """
    from vtkmodules.vtkRenderingFreeType import vtkVectorText
    source = vtkVectorText()
    source.SetText(str(text))
    source.Update()
    output = source.GetOutput()
    if output.GetNumberOfPoints() == 0:
        return 0.0, 0.0
    x0, x1, y0, y1, _, _ = output.GetBounds()
    return max(x1 - x0, 0.0), max(y1 - y0, 0.0)


def fit_face_labels(texts: dict, cube_px: float = CUBE_PX,
                    face_fraction: float = TEXT_FACE_FRACTION,
                    max_scale: float = MAX_TEXT_SCALE,
                    min_text_px: float = MIN_TEXT_PX) -> tuple:
    """@brief One text scale for all faces so that every label fits its face.

    The widest and the tallest label, measured on the real glyphs, must stay
    within face_fraction of the face side (the cube side is 1). One scale is
    shared by all faces, so the six labels read as one set.

    @param texts Mapping face name -> printable-ASCII label.
    @param cube_px Cube side on screen, logical px (for the legibility floor).
    @param face_fraction Largest covered fraction of the face side.
    @param max_scale Scale ceiling for short labels.
    @param min_text_px Legibility floor of the cap height, logical px.
    @return (labels, scale): labels possibly shortened with a trailing "." when
        one is so long that fitting it whole would make all faces illegible.
    """
    extents = {key: vector_text_extent(text) for key, text in texts.items()}
    tallest = max((height for _, height in extents.values()), default=0.0)
    # vtkVectorText capitals are about 1.1 units tall: the floor is the
    # scale at which they are min_text_px high on a cube of cube_px.
    floor = min_text_px / (max(cube_px, 1.0) * max(tallest, 1.0))
    limit = face_fraction / floor
    labels = dict(texts)
    for key, text in texts.items():
        width = extents[key][0]
        while width > limit and len(text) > 1:
            text = text[:-1]
            width = vector_text_extent(text.rstrip() + ".")[0]
        if text != texts[key]:
            labels[key] = text.rstrip() + "."
            extents[key] = vector_text_extent(labels[key])
    widest = max((width for width, _ in extents.values()), default=0.0)
    tallest = max((height for _, height in extents.values()), default=0.0)
    scale = max_scale
    for extent in (widest, tallest):
        if extent > 0:
            scale = min(scale, face_fraction / extent)
    return labels, scale


# ── Camera animation, VTK-free ───────────────────────────────────────


class CameraPose(NamedTuple):
    """@brief Camera state that a view change animates.

    @note The view angle is included because fit_view() zooms by changing it
        (perspective projection), so two views may differ in angle too.
    """
    position: tuple
    focal: tuple
    up: tuple
    parallel_scale: float
    view_angle: float = 30.0


def ease_in_out(t: float) -> float:
    """@brief Cubic ease-in-out (smoothstep) of a progress in [0, 1]."""
    t = min(max(float(t), 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def _unit(vector) -> Optional[np.ndarray]:
    vector = np.asarray(vector, dtype=float)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-12 and math.isfinite(norm) else None


def _rotate(vector, axis, angle: float) -> np.ndarray:
    """Rodrigues rotation of a vector about a unit axis."""
    vector = np.asarray(vector, dtype=float)
    cos, sin = math.cos(angle), math.sin(angle)
    return (vector * cos + np.cross(axis, vector) * sin
            + axis * float(np.dot(axis, vector)) * (1.0 - cos))


def _screen_up(direction, up) -> np.ndarray:
    """View-up made orthogonal to the view direction (VTK does not require it)."""
    candidate = _unit(np.asarray(up, dtype=float)
                      - direction * float(np.dot(up, direction)))
    if candidate is not None:
        return candidate
    # Up parallel to the direction: any perpendicular works, take the world
    # axis least aligned with the direction so the choice is deterministic.
    axis = np.eye(3)[int(np.argmin(np.abs(direction)))]
    return _unit(axis - direction * float(np.dot(axis, direction)))


#: Below this sine of the angle between start and end directions, the two are
#: treated as parallel or opposite: the cross product that gives the rotation
#: axis would be numerical noise.
_PARALLEL_SINE = 1e-6


def _swing(start_dir, end_dir, start_up):
    """Rotation axis and angle that take start_dir to end_dir on a great circle.

    Opposite directions (top -> bottom) have no unique great circle: the axis
    is then the start view-up, so the camera orbits sideways around the model
    and the up vector stays constant -- a deterministic choice that never
    turns the picture upside down half-way.
    """
    cross = np.cross(start_dir, end_dir)
    sine = float(np.linalg.norm(cross))
    cosine = float(np.dot(start_dir, end_dir))
    if sine > _PARALLEL_SINE:
        return cross / sine, math.atan2(sine, cosine)
    if cosine > 0:
        return start_up, 0.0
    return start_up, math.pi


def _signed_angle(a, b, axis) -> float:
    """Angle from a to b about axis, in (-pi, pi]; exactly opposite -> +pi."""
    angle = math.atan2(float(np.dot(np.cross(a, b), axis)), float(np.dot(a, b)))
    return math.pi if angle <= -math.pi + 1e-9 else angle


def interpolate_pose(start: CameraPose, end: CameraPose, s: float) -> CameraPose:
    """@brief Camera pose at fraction s of the way from start to end.

    The view direction moves on the great circle between the two directions
    (spherical interpolation), the view-up is carried along by the same
    rotation and then turned about the view direction by a growing share of
    the residual roll. The up vector is built perpendicular to the direction
    at every step, so it can never become parallel to it and flip.
    The focal point moves linearly, the distance geometrically (a zoom feels
    uniform when it changes by the same ratio per frame).

    @param start Pose at s = 0.
    @param end Pose at s = 1.
    @param s Fraction in [0, 1] (apply the easing before calling).
    @return Interpolated pose; at s = 1 it equals end up to rounding.
    """
    s = min(max(float(s), 0.0), 1.0)
    focal0, focal1 = (np.asarray(pose.focal, dtype=float) for pose in (start, end))
    offset0 = np.asarray(start.position, dtype=float) - focal0
    offset1 = np.asarray(end.position, dtype=float) - focal1
    dir0, dir1 = _unit(offset0), _unit(offset1)
    if dir0 is None or dir1 is None:
        # Camera on its focal point: nothing to turn, just move.
        position = (1 - s) * np.asarray(start.position) + s * np.asarray(end.position)
        return CameraPose(tuple(position), tuple((1 - s) * focal0 + s * focal1),
                          tuple(end.up if s >= 1 else start.up),
                          (1 - s) * start.parallel_scale + s * end.parallel_scale,
                          (1 - s) * start.view_angle + s * end.view_angle)
    dist0, dist1 = float(np.linalg.norm(offset0)), float(np.linalg.norm(offset1))
    up0, up1 = _screen_up(dir0, start.up), _screen_up(dir1, end.up)
    axis, angle = _swing(dir0, dir1, up0)
    roll = _signed_angle(_rotate(up0, axis, angle), up1, dir1)
    direction = _unit(_rotate(dir0, axis, angle * s))
    up = _unit(_rotate(_rotate(up0, axis, angle * s), direction, roll * s))
    focal = (1 - s) * focal0 + s * focal1
    distance = dist0 * (dist1 / dist0) ** s
    return CameraPose(tuple(focal + direction * distance), tuple(focal), tuple(up),
                      (1 - s) * float(start.parallel_scale) + s * float(end.parallel_scale),
                      (1 - s) * float(start.view_angle) + s * float(end.view_angle))


def rolled_up(position, focal, up, clockwise: bool) -> tuple:
    """@brief View-up vector after turning the view by 90 degrees about the viewing axis.

    @param position Camera position.
    @param focal Camera focal point.
    @param up Current view-up vector.
    @param clockwise True to turn the image clockwise on screen.
    @return The new view-up, unit length, orthogonal to the viewing direction.
    @note Turning the image clockwise brings what was on the left to the top,
        so the new up is the old screen-left (minus the screen-right vector);
        anticlockwise, the new up is the old screen-right.
    """
    forward = _unit(np.asarray(focal, dtype=float) - np.asarray(position, dtype=float))
    upv = _unit(up)
    if forward is None or upv is None:
        return tuple(up)
    right = _unit(np.cross(forward, upv))
    if right is None:
        return tuple(up)
    new_up = -right if clockwise else right
    return tuple(float(c) for c in new_up)


def _roll_arrow_polydata(clockwise: bool):
    """Circular arrow in normalised viewport coordinates, as convex cells.

    A 270 degree arc (quads, each convex: the 2D mapper does not triangulate
    concave polygons) with a triangular head at its end. The clockwise arrow
    is the mirror image of the anticlockwise one.
    """
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    centre, outer, inner = 0.5, 0.36, 0.22
    start, sweep, steps = math.radians(-45), math.radians(270), 24
    points, cells = vtkPoints(), vtkCellArray()

    def add(x, y):
        if clockwise:
            x = 1.0 - x
        return points.InsertNextPoint(x, y, 0.0)

    ring = []
    for i in range(steps + 1):
        a = start + sweep * i / steps
        ring.append((add(centre + outer * math.cos(a), centre + outer * math.sin(a)),
                     add(centre + inner * math.cos(a), centre + inner * math.sin(a))))
    for (o0, i0), (o1, i1) in zip(ring, ring[1:]):
        cells.InsertNextCell(4)
        for index in (o0, o1, i1, i0):
            cells.InsertCellPoint(index)
    # Punta: triangolo tangente all'arco nel suo punto finale.
    end = start + sweep
    mid = (outer + inner) / 2
    half = (outer - inner) * 1.25
    tip_a = end + math.radians(40)
    head = (add(centre + (mid + half) * math.cos(end), centre + (mid + half) * math.sin(end)),
            add(centre + (mid - half) * math.cos(end), centre + (mid - half) * math.sin(end)),
            add(centre + mid * math.cos(tip_a), centre + mid * math.sin(tip_a)))
    cells.InsertNextCell(3)
    for index in head:
        cells.InsertCellPoint(index)
    data = vtkPolyData()
    data.SetPoints(points)
    data.SetPolys(cells)
    return data


def _house_polydata():
    """House outline in normalised viewport coordinates (roof, walls, door)."""
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    outline = [(0.12, 0.50), (0.50, 0.88), (0.88, 0.50), (0.76, 0.50), (0.76, 0.12),
               (0.58, 0.12), (0.58, 0.36), (0.42, 0.36), (0.42, 0.12), (0.24, 0.12),
               (0.24, 0.50)]
    points = vtkPoints()
    for x, y in outline:
        points.InsertNextPoint(x, y, 0.0)
    polygon = vtkCellArray()
    polygon.InsertNextCell(len(outline))
    for index in range(len(outline)):
        polygon.InsertCellPoint(index)
    data = vtkPolyData()
    data.SetPoints(points)
    data.SetPolys(polygon)
    return data


class ViewCube:
    """@brief Overlay cube and home button attached to a pyvista/VTK render window."""

    def __init__(self, render_window, main_renderer, labels: Optional[dict] = None):
        """@brief Create the overlay renderers and keep them in sync with the main camera.

        @param render_window vtkRenderWindow shared with the main renderer.
        @param main_renderer Renderer whose camera the cube follows.
        @param labels Optional face labels keyed by view name (see DEFAULT_LABELS).
        """
        from vtkmodules.vtkRenderingAnnotation import vtkAnnotatedCubeActor
        from vtkmodules.vtkRenderingCore import (
            vtkActor2D, vtkCoordinate, vtkPolyDataMapper2D, vtkRenderer)

        self.main_renderer = main_renderer
        self.render_window = render_window
        self._size = (1, 1)
        render_window.SetNumberOfLayers(max(2, render_window.GetNumberOfLayers()))

        self.cube = vtkAnnotatedCubeActor()
        self.cube.GetCubeProperty().SetColor(*_FACE_COLOR)
        # Le facce devono leggersi anche di taglio: luce ambiente alta.
        self.cube.GetCubeProperty().SetAmbient(0.45)
        self.cube.GetCubeProperty().SetDiffuse(0.7)
        self.cube.GetCubeProperty().SetEdgeVisibility(True)
        self.cube.GetCubeProperty().SetEdgeColor(*_EDGE_COLOR)
        self.cube.GetCubeProperty().SetLineWidth(1.5)
        # I contorni del testo producevano un tratteggio lungo i bordi delle facce.
        self.cube.SetTextEdgesVisibility(False)
        # Scritta della faccia SOPRA leggibile con il fronte in basso, come la
        # vista dall'alto della finestra (Y in su sullo schermo).
        self.cube.SetZFaceTextRotation(_Z_TEXT_ROTATION)
        for name in ("XPlus", "XMinus", "YPlus", "YMinus", "ZPlus", "ZMinus"):
            face = getattr(self.cube, f"Get{name}FaceProperty")()
            face.SetColor(*_TEXT_COLOR)
            face.SetAmbient(1.0)
            face.SetDiffuse(0.0)
        self.renderer = vtkRenderer()
        self.renderer.SetLayer(1)
        self.renderer.InteractiveOff()
        self.renderer.AddActor(self.cube)
        self.renderer.GetActiveCamera().ParallelProjectionOn()
        render_window.AddRenderer(self.renderer)

        mapper = vtkPolyDataMapper2D()
        mapper.SetInputData(_house_polydata())
        coordinate = vtkCoordinate()
        coordinate.SetCoordinateSystemToNormalizedViewport()
        mapper.SetTransformCoordinate(coordinate)
        self.home_actor = vtkActor2D()
        self.home_actor.SetMapper(mapper)
        self.home_actor.GetProperty().SetColor(*_HOME_COLOR)
        self.home_renderer = vtkRenderer()
        self.home_renderer.SetLayer(1)
        self.home_renderer.InteractiveOff()
        self.home_renderer.AddViewProp(self.home_actor)
        render_window.AddRenderer(self.home_renderer)

        # Frecce di rotazione ai lati della casetta, disegnate come lei.
        self.roll_renderers = {}
        for key, clockwise in ((ROLL_CCW, False), (ROLL_CW, True)):
            arrow_mapper = vtkPolyDataMapper2D()
            arrow_mapper.SetInputData(_roll_arrow_polydata(clockwise))
            arrow_coordinate = vtkCoordinate()
            arrow_coordinate.SetCoordinateSystemToNormalizedViewport()
            arrow_mapper.SetTransformCoordinate(arrow_coordinate)
            arrow = vtkActor2D()
            arrow.SetMapper(arrow_mapper)
            arrow.GetProperty().SetColor(*_HOME_COLOR)
            renderer = vtkRenderer()
            renderer.SetLayer(1)
            renderer.InteractiveOff()
            renderer.AddViewProp(arrow)
            render_window.AddRenderer(renderer)
            self.roll_renderers[key] = renderer

        self.set_labels(labels or DEFAULT_LABELS)
        main_renderer.AddObserver("StartEvent", self._sync)

    # ── Aspetto ─────────────────────────────────────────────────────

    def set_labels(self, labels: dict) -> None:
        """@brief Write the face labels and scale them so that all of them fit.

        @param labels Mapping view name -> text (top, bottom, front, back, right, left).
        @note Labels are upper-cased and folded to ASCII (vector_text_safe);
            the texts actually drawn are kept in `self.labels`.
        """
        texts = {key: vector_text_safe(str(labels.get(key) or DEFAULT_LABELS[key]).upper(),
                                       DEFAULT_LABELS[key])
                 for key in DEFAULT_LABELS}
        texts, scale = fit_face_labels(texts)
        self.labels = texts
        self.cube.SetZPlusFaceText(texts["top"])
        self.cube.SetZMinusFaceText(texts["bottom"])
        self.cube.SetYMinusFaceText(texts["front"])
        self.cube.SetYPlusFaceText(texts["back"])
        self.cube.SetXPlusFaceText(texts["right"])
        self.cube.SetXMinusFaceText(texts["left"])
        # The scale comes from the measured glyphs, not from a character
        # count: "FRONT" and "RIGHT" overflowed with the old estimate.
        self.cube.SetFaceTextScale(scale)

    # ── Geometria sullo schermo ─────────────────────────────────────

    def layout(self, width: float, height: float) -> None:
        """@brief Place cube and house in the top-right corner of a widget.

        @param width Widget width in logical pixels.
        @param height Widget height in logical pixels.
        """
        width, height = max(float(width), 1.0), max(float(height), 1.0)
        self._size = (width, height)
        placed = [(self.renderer, self.cube_rect()), (self.home_renderer, self.home_rect())]
        placed += [(renderer, self.roll_rect(key))
                   for key, renderer in getattr(self, "roll_renderers", {}).items()]
        for renderer, (x, y, w, h) in placed:
            # VTK counts Y from the bottom, Qt from the top.
            renderer.SetViewport(max(0.0, x / width), max(0.0, 1 - (y + h) / height),
                                 min(1.0, (x + w) / width), min(1.0, 1 - y / height))

    def cube_rect(self) -> tuple:
        """@brief (x, y, w, h) of the cube square in logical widget pixels."""
        width, _ = self._size
        return (width - MARGIN_PX - CUBE_PX, MARGIN_PX, CUBE_PX, CUBE_PX)

    def home_rect(self) -> tuple:
        """@brief (x, y, w, h) of the home button in logical widget pixels."""
        x, y, w, h = self.cube_rect()
        return (x + (w - HOME_PX) / 2, y + h + HOME_GAP_PX, HOME_PX, HOME_PX)

    def roll_rect(self, key: str) -> tuple:
        """@brief (x, y, w, h) of a rotation arrow in logical widget pixels.

        @param key ROLL_CCW (left of the house) or ROLL_CW (right of it).
        """
        x, y, w, h = self.home_rect()
        offset = -(ROLL_GAP_PX + ROLL_PX) if key == ROLL_CCW else w + ROLL_GAP_PX
        return (x + offset, y + (h - ROLL_PX) / 2, ROLL_PX, ROLL_PX)

    @staticmethod
    def _inside(rect, x, y) -> bool:
        left, top, w, h = rect
        return left <= x <= left + w and top <= y <= top + h

    def contains(self, x: float, y: float) -> bool:
        """@brief Whether a logical widget point lies on the cube, the house or an arrow."""
        return (self._inside(self.cube_rect(), x, y) or self._inside(self.home_rect(), x, y)
                or any(self._inside(self.roll_rect(key), x, y) for key in (ROLL_CW, ROLL_CCW)))

    # ── Clic ────────────────────────────────────────────────────────

    def hit(self, x: float, y: float):
        """@brief What a click at a logical widget point selects.

        @return "home", ROLL_CW, ROLL_CCW, a direction tuple (see
            classify_cube_point), or None when the click is outside the cube,
            the house and the arrows.
        """
        if self._inside(self.home_rect(), x, y):
            return "home"
        for key in (ROLL_CW, ROLL_CCW):
            if self._inside(self.roll_rect(key), x, y):
                return key
        left, top, w, h = self.cube_rect()
        if not self._inside((left, top, w, h), x, y):
            return None
        self._sync()
        # Normalised position inside the cube viewport, Y upwards, -1..1.
        u = (x - left) / w * 2 - 1
        v = 1 - (y - top) / h * 2
        camera = self.renderer.GetActiveCamera()
        position = camera.GetPosition()
        focal = camera.GetFocalPoint()
        up = camera.GetViewUp()
        forward = [f - p for f, p in zip(focal, position)]
        norm = math.sqrt(sum(c * c for c in forward)) or 1.0
        forward = [c / norm for c in forward]
        right = [forward[1] * up[2] - forward[2] * up[1],
                 forward[2] * up[0] - forward[0] * up[2],
                 forward[0] * up[1] - forward[1] * up[0]]
        rnorm = math.sqrt(sum(c * c for c in right)) or 1.0
        right = [c / rnorm for c in right]
        true_up = [right[1] * forward[2] - right[2] * forward[1],
                   right[2] * forward[0] - right[0] * forward[2],
                   right[0] * forward[1] - right[1] * forward[0]]
        scale = camera.GetParallelScale()
        # Parallel projection: the ray is the view direction through the
        # point of the image plane under the cursor (square viewport).
        centre = [p + scale * (u * r + v * t) for p, r, t in zip(position, right, true_up)]
        far = [c + f * norm * 2 for c, f in zip(centre, forward)]
        entry = ray_box_entry(centre, far)
        return classify_cube_point(entry) if entry is not None else None

    # ── Sincronizzazione ────────────────────────────────────────────

    def _sync(self, *_args) -> None:
        """Turn the cube camera with the main camera, at a fixed distance."""
        main = self.main_renderer.GetActiveCamera()
        position, focal = main.GetPosition(), main.GetFocalPoint()
        direction = [p - f for p, f in zip(position, focal)]
        norm = math.sqrt(sum(c * c for c in direction))
        if norm <= 0 or not math.isfinite(norm):
            return
        camera = self.renderer.GetActiveCamera()
        camera.SetFocalPoint(0.0, 0.0, 0.0)
        camera.SetPosition(*(4.0 * c / norm for c in direction))
        camera.SetViewUp(*main.GetViewUp())
        # sqrt(3)/2 is the half diagonal: the cube stays whole at any angle.
        camera.SetParallelScale(0.9)
        self.renderer.ResetCameraClippingRange()
