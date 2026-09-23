"""@brief Clickable navigation cube for the VTK 3D preview, as in CAD and slicer tools.

The cube sits in the top-right corner of the viewport and turns with the model.
Clicking a face looks at that face; clicking near an edge or a corner looks
along the diagonal of that edge or corner; the house under the cube restores
the initial isometric view. It replaces the row of «Vista: Iso, Sopra, …»
buttons (user request, 2026-09-23).

@note The cube is drawn by its own VTK renderer on an overlay layer, so it
    survives `Plotter.clear()` (which only empties the main renderer) and never
    enters the main renderer's bounds, picking or `reset_camera`.
@note Hit testing is analytic: the click becomes a ray through the cube
    renderer's camera and is intersected with the unit box. It does not depend
    on a VTK picker, which behaves differently on assemblies and off-screen.
"""
from __future__ import annotations

import math
from typing import Optional

#: Half side of the cube in its own world units (vtkAnnotatedCubeActor is a unit cube).
HALF = 0.5

#: Fraction of the half side beyond which a click counts as "near the border".
#: With 0.7 the outer 15% band of every face selects the edge or corner view,
#: the rest of the face selects the face itself -- the convention of CAD cubes.
#: It is an interaction choice, not a measured threshold.
EDGE_FRACTION = 0.7

#: Cube side and margin in logical pixels (DPI scaling is applied by Qt).
CUBE_PX = 96
MARGIN_PX = 10
HOME_PX = 26
HOME_GAP_PX = 4

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

        self.set_labels(labels or DEFAULT_LABELS)
        main_renderer.AddObserver("StartEvent", self._sync)

    # ── Aspetto ─────────────────────────────────────────────────────

    def set_labels(self, labels: dict) -> None:
        """@brief Write the face labels and scale them to fit the longest one.

        @param labels Mapping view name -> text (top, bottom, front, back, right, left).
        """
        texts = {key: str(labels.get(key) or DEFAULT_LABELS[key]).upper()
                 for key in DEFAULT_LABELS}
        self.cube.SetZPlusFaceText(texts["top"])
        self.cube.SetZMinusFaceText(texts["bottom"])
        self.cube.SetYMinusFaceText(texts["front"])
        self.cube.SetYPlusFaceText(texts["back"])
        self.cube.SetXPlusFaceText(texts["right"])
        self.cube.SetXMinusFaceText(texts["left"])
        # vtkVectorText glyphs are about 0.8 wide per unit of scale: the
        # longest label must stay inside the face with a small border.
        longest = max(len(text) for text in texts.values())
        self.cube.SetFaceTextScale(min(0.28, 0.85 / (0.8 * max(longest, 1))))

    # ── Geometria sullo schermo ─────────────────────────────────────

    def layout(self, width: float, height: float) -> None:
        """@brief Place cube and house in the top-right corner of a widget.

        @param width Widget width in logical pixels.
        @param height Widget height in logical pixels.
        """
        width, height = max(float(width), 1.0), max(float(height), 1.0)
        self._size = (width, height)
        cube = self.cube_rect()
        home = self.home_rect()
        for renderer, (x, y, w, h) in ((self.renderer, cube), (self.home_renderer, home)):
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

    @staticmethod
    def _inside(rect, x, y) -> bool:
        left, top, w, h = rect
        return left <= x <= left + w and top <= y <= top + h

    def contains(self, x: float, y: float) -> bool:
        """@brief Whether a logical widget point lies on the cube square or the house."""
        return self._inside(self.cube_rect(), x, y) or self._inside(self.home_rect(), x, y)

    # ── Clic ────────────────────────────────────────────────────────

    def hit(self, x: float, y: float):
        """@brief What a click at a logical widget point selects.

        @return "home", a direction tuple (see classify_cube_point), or None
            when the click is outside the cube and the house.
        """
        if self._inside(self.home_rect(), x, y):
            return "home"
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
