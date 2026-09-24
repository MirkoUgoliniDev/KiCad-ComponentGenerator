"""@brief Isolated, offline STEP candidate review with optional CAD thumbnails."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

from PyQt6.QtCore import (
    QEvent, QObject, QPointF, QRunnable, QSignalBlocker, Qt, QThreadPool, QTimer, pyqtSignal,
)
from PyQt6.QtGui import QBrush, QColor, QIcon, QImage, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QToolButton, QVBoxLayout,
    QWidget,
)

from ..services.messages import tx
from ..services.model_library_search import filter_models, library_models

#: Pause after the last keystroke before the library filter runs, so typing a
#: word does not filter thousands of rows once per letter.
FILTER_DEBOUNCE_MS = 250

#: Body colour used when the component carries no sampled ``color_hex``; it is a
#: neutral light blue that stays legible on the dark preview background.
DEFAULT_BODY_COLOR = '#b0c7db'

#: Background of the preview. Shaded lightnesses are kept well above its own
#: lightness (~0.16) so a dark body is still separable from the canvas.
PREVIEW_BACKGROUND = '#202431'

#: Lambert floor: the fraction of the shading a face keeps where the light does
#: not reach it. Without a floor the faces turned away collapse to black and the
#: silhouette is lost against the dark background.
_AMBIENT = 0.28

#: Fixed light direction in the *eye* space the projection below produces
#: (screen x to the right, screen y downwards, third axis towards the viewer):
#: upper-left and slightly in front of the camera, the convention CAD viewers
#: use, so the top face of a box is the brightest and the two visible flanks
#: differ from it and from each other.
_LIGHT = (-0.398015, -0.597022, 0.696526)

#: Quantisation of the Lambert term. 64 steps are visually continuous, and they
#: let one QColor/QPen/QBrush triplet serve thousands of triangles instead of one
#: per triangle. Measured on the densest mesh the guards allow (a 100k-triangle
#: sphere, best of three runs on one machine, depth sort included): 609 ms with
#: this cache against 1462 ms shading without it, so the cache is worth 2.4x.
#: Shading is *not* free, though: the same mesh flat-filled with the pen and
#: brush hoisted out of the loop takes 329 ms, so per-face shading costs about
#: +85% over a flat fill (a flat fill that rebuilt QPen/QBrush per triangle
#: would take 856 ms, which is the cost this cache avoids, not the baseline).
_SHADE_STEPS = 64

#: Preview camera looking straight down -Z: the same top view as the photo crop
#: shown beside it, so the model can be compared with the photograph directly
#: and a local Z rotation turns the model in the image plane. Dialog default.
VIEW_PLAN = 'plan'

#: Preview camera from above the front-right corner, for inspecting heights.
VIEW_ISOMETRIC = 'isometric'

#: View the dialog opens with (see VIEW_PLAN for why).
DEFAULT_PREVIEW_VIEW = VIEW_PLAN

#: Orthographic cameras as three CAD-space rows: screen x (right), screen y
#: (down) and depth (towards the viewer). Both are proper rotations — right x
#: up = towards — so the image is never a mirror of the part: a positive
#: (counter-clockwise, seen from +Z) rotation reads counter-clockwise on screen
#: in both views, as it does in KiCad. CAD +Y points up the screen, the KiCad
#: 3D-model convention (footprint Y down = model Y up).
#: @note The isometric camera used before this table mapped screen x to x-y and
#: depth to x+y+z; right x up then equals *minus* the depth axis, i.e. the old
#: picture was the mirror image of the part, and a positive rotation turned
#: clockwise on screen. The camera now sits above (+1, -1, +1), the corner a
#: CAD isometric view uses, with +X to the lower right and +Y to the upper right.
_CAMERAS = {
    VIEW_PLAN: ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    VIEW_ISOMETRIC: ((0.70710678, 0.70710678, 0.0),
                     (0.40824829, -0.40824829, -0.81649658),
                     (0.57735027, -0.57735027, 0.57735027)),
}

#: Plan-view height cue: share of the Lambert shading a face keeps at the
#: lowest height of the mesh; it rises linearly to the full term at the top.
#: @note Seen from straight above every horizontal face has the same normal,
#: so Lambert alone paints the board and a shield standing on it in one tone
#: and the part collapses into a flat silhouette — for a near-square module
#: that silhouette barely changes under a 90-degree turn, which is exactly the
#: case the plan view exists for. Lightness therefore also encodes height
#: (higher is brighter, the usual reading of a top-lit relief). The value is a
#: legibility choice, not a measurement: low enough that the lowest face is
#: clearly darker than the highest, high enough that it stays well above the
#: ambient floor and the dark canvas.
_PLAN_HEIGHT_FLOOR = 0.45


def _face_intensity(a, b, c):
    """Lambert term of one triangle, from its own geometric normal.

    @param a First eye-space vertex as (screen x, screen y down, depth).
    @param b Second eye-space vertex.
    @param c Third eye-space vertex.
    @return Intensity in [_AMBIENT, 1]. A degenerate triangle (zero area,
    collinear or coincident vertices) has no normal at all, so it falls back to
    the ambient floor instead of producing a non-finite value.
    @note ``Shape.tessellate`` gives no guarantee that the winding order is
    consistent across faces, so the sign of the raw normal carries no
    information and a signed Lambert term would paint an arbitrary half of the
    object black. The normal is therefore oriented against the *camera* rather
    than against the winding: what the viewer sees of a closed solid is the
    side turned towards them, so flipping the normal into the eye-space view
    axis (+z, towards the viewer) recovers the outward normal of every visible
    face regardless of winding, and the Lambert term can stay signed. Taking
    the absolute value instead would light a face turned away from the light
    exactly like one turned towards it: an undercut — a connector shroud lip, a
    draft angle under an IC body, the chamfered underside of a shield, the far
    wall of a cavity — would read a whole tone too bright and its edge against
    the neighbouring flank would disappear.
    @note A face exactly edge-on to the camera (normal z component zero) keeps
    whatever sign its winding gave it, but it projects to zero area and paints
    nothing, so the render stays winding-independent.
    """
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx, ny, nz = uy*vz - uz*vy, uz*vx - ux*vz, ux*vy - uy*vx
    norm = math.sqrt(nx*nx + ny*ny + nz*nz)
    if not math.isfinite(norm) or norm <= 1e-12:
        return _AMBIENT
    if nz < 0.0:
        nx, ny, nz = -nx, -ny, -nz
    lambert = (nx*_LIGHT[0] + ny*_LIGHT[1] + nz*_LIGHT[2]) / norm
    if not math.isfinite(lambert):
        return _AMBIENT
    return _AMBIENT + (1.0 - _AMBIENT) * max(0.0, min(1.0, lambert))


def _shaded_color(base, intensity):
    """Shade one base colour by lightness only, keeping its hue and saturation.

    @param base QColor of the body; hue and saturation are preserved so the
    object reads as a single material instead of rainbow faces.
    @param intensity Lambert term from _face_intensity().
    @return QColor whose lightness follows the light, clamped to a band that
    stays visible over PREVIEW_BACKGROUND.
    @note The base lightness is first mapped into a legible mid band, so a nearly
    black or nearly white body still shows three distinct sides rather than
    saturating at one end of the scale.
    """
    hue, saturation, base_lightness, _ = QColor(base).getHslF()
    if not math.isfinite(intensity):
        intensity = _AMBIENT
    mid = 0.42 + 0.34 * base_lightness
    lightness = min(0.96, max(0.08, mid * (0.45 + 0.85 * intensity)))
    return QColor.fromHslF(max(0.0, hue), saturation, lightness)


def _number(value, default=0.0):
    try:
        result = float(value) if not isinstance(value, bool) else default
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _display_dimensions(value):
    """Round displayed mm to three decimals without altering stored evidence."""
    if isinstance(value, dict):
        return '; '.join(f'{key}: {_display_dimensions(item)}' for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return ' × '.join(_display_dimensions(item) for item in value)
    number = _number(value, None)
    if number is None:
        return '?'
    return f'{number:.3f}'.rstrip('0').rstrip('.') if round(number, 3) else '0'


def _step_file(path):
    try:
        return Path(path).suffix.lower() in ('.step', '.stp') and Path(path).is_file()
    except (TypeError, ValueError, OSError):
        return False


def _thumbnail(path, rotation, dimensions=None, color=None, view=VIEW_ISOMETRIC):
    """Tessellate STEP; automatic orientation uses the production CAD importer.

    @param path STEP file to preview.
    @param rotation Local Z override in degrees, or None for CAD axis inference.
    @param dimensions (length_mm, width_mm), required only when rotation is None.
    @param color Body colour of the component, typically the ``color_hex`` the
    photo pipeline sampled. When the caller knows no colour, DEFAULT_BODY_COLOR
    is used, so the preview still renders a coherent material.
    @param view VIEW_PLAN (top view, the dialog default) or VIEW_ISOMETRIC. The
    parameter default stays isometric so direct callers keep the historical
    camera; the dialog always passes the view explicitly.
    @return QImage of 340x340 pixels with the shape shaded per face.
    @throws ValueError When dimensions, mesh size or mesh coordinates are
    unusable, or when @p view is not a known camera.
    @note The rotation is applied about the model's own Z axis *before* the
    camera, then the result is fitted to the canvas. In the isometric view the
    camera looks down a 45-degree diagonal, so a 90-degree turn of a part that
    is mirror-symmetric about one of its own axes projects to the mirror image
    of the unrotated silhouette, and a 180-degree turn of a part symmetric
    about both axes to the very same picture: for a near-square module the two
    angles look alike. The plan view has no such ambiguity, the turn happens in
    the image plane, which is why it is the default.
    @note The triangles are painted back to front by centroid depth. That is an
    approximation, not a visibility solution: where solids overlap or
    interpenetrate — exactly what the automatic branch builds, since
    _library_model returns bodies stacked on and sunk into one another before
    Part.makeCompound joins them — a large far triangle can be painted over a
    nearer one. Per-face shading makes those errors *visible*: the wrongly
    ordered patch now carries a different tone instead of the same flat fill,
    so the reviewer can see a geometry error that is not in the STEP file.
    Measured against a per-pixel depth buffer of the same mesh (pixels covered
    by the part, lightness differing by more than 0.02): a single box 0.14%,
    which is the antialiased outline and the floor of the method; two stacked
    boxes 5.0%; a slab carrying four bodies 6.7%, with a worst lightness error
    of 0.119. No cheap replacement key is uniformly better — sorting by the
    farthest vertex and breaking ties on the centroid halves the slab case to
    3.1% but makes nested boxes worse, 8.2% to 10.2% — so the sort is left as
    it is and the limit recorded here. Removing it needs a depth buffer or a
    BSP split, neither of which belongs in a thumbnail.
    """
    from ..cad3d.step_generator import find_freecadcmd

    camera = _CAMERAS.get(view)
    if camera is None:
        raise ValueError(f'Unknown preview view: {view!r}')
    executable = find_freecadcmd()
    if rotation is None:
        length, width = dimensions or (None, None)
        length, width = _number(length, None), _number(width, None)
        if length is None or width is None or min(length, width) <= 0:
            raise ValueError('Automatic orientation requires positive component dimensions')
        component = dict(model_path=str(path), length_mm=length, width_mm=width)
        # Use the actual placement helper so body-section / total-envelope
        # comparisons and tie-breaking cannot diverge from generated CAD.
        cad_directory = str(Path(__file__).resolve().parents[1] / 'cad3d')
        geometry_source = (
            f'sys.path.insert(0, {cad_directory!r})\n'
            'from freecad_parts import _library_model\n'
            f'parts = _library_model({component!r}, 0, 0, 0, "#b0c7db")\n'
            'if not parts:\n'
            '    raise ValueError("Automatic model placement unavailable")\n'
            'shape = Part.makeCompound([shape for shape, _ in parts])\n')
        rotation = 0.0  # _library_model already applied inferred native rotation.
    else:
        geometry_source = f'shape = Part.read({str(path)!r})\n'
    with tempfile.TemporaryDirectory(prefix='kicad-candidate-') as directory:
        output = Path(directory) / 'mesh.json'
        script = Path(directory) / 'preview.py'
        # Paths are encoded as Python literals, never interpolated into shell code.
        script.write_text(
            'import Part, json, sys\n' + geometry_source +
            'vertices, triangles = shape.tessellate(0.15)\n'
            f'with open({str(output)!r}, "w") as stream:\n'
            '    json.dump({"vertices": [[v.x,v.y,v.z] for v in vertices], '
            '"triangles": triangles}, stream)\n', encoding='utf-8')
        subprocess.run([executable, str(script)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=25, check=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if output.stat().st_size > 20_000_000:
            raise ValueError('Preview mesh too large')
        mesh = json.loads(output.read_text(encoding='utf-8'))
    vertices, triangles = mesh['vertices'], mesh['triangles']
    if not vertices or not triangles or len(triangles) > 100_000:
        raise ValueError('Preview geometry unavailable or too complex')
    angle = math.radians(rotation)
    c, s = math.cos(angle), math.sin(angle)
    (rx, ry, rz), (dx, dy, dz), (tx, ty, tz) = camera
    points = []
    for x, y, z in vertices:
        # Local Z rotation of the model first, camera second: the rotation is a
        # property of the part, the camera only decides how it is looked at.
        x, y = c*x-s*y, s*x+c*y
        point = (rx*x + ry*y + rz*z, dx*x + dy*y + dz*z, tx*x + ty*y + tz*z)
        if not all(math.isfinite(v) for v in point):
            raise ValueError('Invalid mesh coordinates')
        points.append(point)
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymin, ymax = min(p[1] for p in points), max(p[1] for p in points)
    # Height cue, plan view only (see _PLAN_HEIGHT_FLOOR); the isometric view
    # already separates heights through the flanks it shows.
    height_cue = view == VIEW_PLAN
    depth_min = min(p[2] for p in points)
    depth_span = max(p[2] for p in points) - depth_min
    scale = 280 / max(xmax-xmin, ymax-ymin, 0.001)
    # Stripped here too, so the guarantee does not depend on which caller
    # sampled the colour.
    base = QColor(str(color).strip()) if color else QColor()
    if not base.isValid():
        base = QColor(DEFAULT_BODY_COLOR)
    image = QImage(340, 340, QImage.Format.Format_RGB32)
    image.fill(QColor(PREVIEW_BACKGROUND))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # One cached pen+brush per quantised shade: building a QColor per triangle
        # would dominate the cost on a 100k-triangle mesh.
        shades = {}
        # No back-face culling: the triangles are painter-sorted and the winding
        # order of the tessellation is not guaranteed, so a wrong cull would erase
        # the object entirely, which is worse than drawing hidden faces first.
        # The sort below is approximate; see the @note on the sort in _thumbnail.
        for face in sorted(triangles, key=lambda f: sum(points[i][2] for i in f)):
            if len(face) != 3:
                raise ValueError('Invalid mesh triangle')
            intensity = _face_intensity(*(points[i] for i in face))
            if height_cue:
                # A flat mesh has no height to encode: it keeps the full term.
                height = (sum(points[i][2] for i in face)/3 - depth_min)/depth_span \
                    if depth_span > 1e-9 else 1.0
                intensity = _AMBIENT + (intensity - _AMBIENT) * (
                    _PLAN_HEIGHT_FLOOR + (1.0 - _PLAN_HEIGHT_FLOOR) * height)
            step = int(intensity * _SHADE_STEPS)
            pair = shades.get(step)
            if pair is None:
                shade = _shaded_color(base, (step + 0.5) / _SHADE_STEPS)
                # Interior edges must vanish, but with antialiasing two coplanar
                # triangles leave a sub-pixel gap along their shared edge. The
                # outline is therefore drawn in the *same* colour as the fill: it
                # covers the seam without any wireframe, and unlike a geometric
                # overdraw it cannot distort the silhouette of thin features.
                pair = shades[step] = (QPen(shade), QBrush(shade))
            painter.setPen(pair[0])
            painter.setBrush(pair[1])
            painter.drawPolygon(QPolygonF([
                QPointF(170+(points[i][0]-(xmin+xmax)/2)*scale,
                        170+(points[i][1]-(ymin+ymax)/2)*scale) for i in face]))
    finally:
        painter.end()
    return image


class _RotationSpinBox(QDoubleSpinBox):
    """@brief Rotation field that reports *every* user interaction with it.

    @details ``valueChanged`` alone cannot express "the user reached for this
    knob": stepping at a range limit, or typing the value already shown, leaves
    the value untouched and emits nothing. The dialog needs those cases too,
    because it treats any interaction as the request for the explicit override.
    @note ``interacted`` is deliberately *not* emitted for programmatic
    ``setValue()``: the dialog only ever sets the value while building itself,
    before any signal is connected, so the restored state cannot claim the
    override on the user's behalf.
    """

    #: Emitted when the user stepped the value with the arrows, the up/down keys
    #: or the wheel, including a step the range limits swallowed.
    interacted = pyqtSignal()

    def stepBy(self, steps):
        """@brief Qt override: step the value, then report the interaction.

        @param steps Signed number of single steps requested by the user.
        @note Qt calls this only for user-driven stepping, so the signal cannot
        be raised by the dialog restoring a saved rotation.
        """
        super().stepBy(steps)
        self.interacted.emit()


class _Signals(QObject):
    ready = pyqtSignal(int, object)


class _Preview(QRunnable):
    def __init__(self, token, path, rotation, dimensions=None, color=None,
                 view=DEFAULT_PREVIEW_VIEW):
        super().__init__()
        self.signals = _Signals()
        self.token, self.path, self.rotation = token, path, rotation
        self.dimensions = dimensions
        self.color = color
        self.view = view

    def run(self):
        """@brief Generate an optional thumbnail without blocking the Qt event loop."""
        try:
            result = (_thumbnail(self.path, None, self.dimensions, self.color, self.view)
                      if self.rotation is None
                      else _thumbnail(self.path, self.rotation, None, self.color, self.view))
        except Exception:
            result = None
        self.signals.ready.emit(self.token, result)


class _Index(QRunnable):
    """@brief Walk the installed KiCad 3D libraries off the GUI thread."""

    def __init__(self, token):
        """@brief Prepare one library walk identified by a dialog token.

        @param token Value the dialog compares against its current index token.
        """
        super().__init__()
        self.signals = _Signals()
        self.token = token

    def run(self):
        """@brief Build the model index; a failed walk yields an empty index."""
        try:
            result = list(library_models())
        except Exception:
            result = []
        self.signals.ready.emit(self.token, result)


class ModelCandidatesDialog(QDialog):
    """@brief Review at most five ranked suggestions and confirm one STEP model.

    @details Besides the ranked candidates, a KiCad-style filter over the whole
    installed 3D library lists ``library/stem`` rows for typed fragments; a row
    chosen there is confirmed exactly like a manually chosen file. The library
    index is built once per process, off the GUI thread, on the first query.
    @note Inputs are read-only. Apply selected_selection() only after Accepted;
    Accepted with rejected status means preserve the generic placeholder.
    Rotation is a local native-model Z override in degrees, or None to retain
    CAD bounding-box axis inference. Explicit zero disables that inference.
    @note The rotation field is always enabled, in both modes: any interaction
    with it (a moved value, a typed value, even a step the range swallowed)
    clears the automatic tick and puts the explicit override in force, and the
    label beside the field says which mode is in charge. selected_selection()
    is unaffected: automatic still reports rotation None, explicit the value.
    @note A checkable tool button in the top-right corner of the preview picks
    the camera: released is the plan view (VIEW_PLAN, the default, comparable
    with the top-view photo crop), pressed the isometric one. It only changes
    how the model is looked at: the choice lives as long as the dialog, is never
    persisted and never enters selected_selection(). Re-rendering the model
    already shown (new angle, mode or view) keeps the previous picture until the
    new one arrives; a different model clears it at once.
    """

    def __init__(self, component: dict, match_report: dict, photo_crop=None, parent=None):
        """@brief Build an independent candidate review dialog.

        @param component Component metadata, dimensions in mm and optional
        model_selection. Saved selection rotation takes precedence over
        model_rotation_deg, including None for automatic axis inference. An
        optional color_hex shades the STEP preview with the observed body colour.
        @param match_report Ranked candidates with path, package, dimensions_mm,
        relative_xy_error and reasons; the first five distinct paths are shown.
        @param photo_crop Optional QImage, QPixmap, encoded image bytes or image path.
        @param parent Optional parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle('Scelta del modello STEP')
        self.resize(820, 650)
        self._selection = dict(status='rejected', path='', rotation_deg=0.0)
        self._token = 0
        self._closed = False
        self._workers = {}
        self._dimensions = (component.get('length_mm'), component.get('width_mm'))
        # The photo pipeline samples the body colour into color_hex; shading the
        # preview with it beats a fixed constant, and an unusable value falls back
        # to DEFAULT_BODY_COLOR instead of painting an invalid colour.
        # Whitespace around the value is stripped, as validate_hex_color already
        # does for the CAD annotations: ' #112233 ' is a colour the pipeline
        # observed, and silently falling back to the default would throw the
        # sampled body colour away over a stray space.
        declared_color = component.get('color_hex')
        declared_color = declared_color.strip() if isinstance(declared_color, str) else None
        self._body_color = (declared_color if declared_color
                            and QColor(declared_color).isValid() else None)
        saved = component.get('model_selection')
        saved = saved if isinstance(saved, dict) else {}
        saved_path = str(saved.get('path') or '')
        if not saved_path and match_report.get('status') in (
                'auto_matched', 'provisional_matched', 'library_matched'):
            saved_path = str(match_report.get('selected_path') or '')
        initial_rotation = saved.get('rotation_deg', component.get('model_rotation_deg'))
        self._candidates = []
        seen = set()
        for candidate in match_report.get('candidates', []) or []:
            if not isinstance(candidate, dict):
                continue
            path = str(candidate.get('path') or '')
            if not path or path in seen:
                continue
            seen.add(path)
            self._candidates.append(dict(candidate, path=path))
            if len(self._candidates) == 5:
                break
        if saved_path and not any(c['path'] == saved_path for c in self._candidates):
            existing = next((dict(c) for c in match_report.get('candidates', []) or []
                             if isinstance(c, dict) and str(c.get('path')) == saved_path),
                            dict(path=saved_path, package=tx('Modello attuale: {name}',
                                                             name=Path(saved_path).name)))
            self._candidates = [existing] + self._candidates[:4]
        layout = QVBoxLayout(self)
        title = QLabel(tx("{name} — dimensioni stimate L × W × H (mm): {dimensions}",
                          name=component.get('name') or tx('Componente'),
                          dimensions=_display_dimensions([component.get('length_mm'),
                                                          component.get('width_mm'),
                                                          component.get('height_mm')])))
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        images = QHBoxLayout()
        self.photo_label = QLabel('Ritaglio fotografico non disponibile')
        self.preview_label = QLabel('Seleziona un candidato per visualizzare la geometria STEP reale')
        for label in (self.photo_label, self.preview_label):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumSize(340, 220)
            label.setWordWrap(True)
        images.addWidget(self.photo_label)
        # Scelta della vista: pianta (predefinita, confrontabile con la foto a
        # sinistra, che e' una vista dall'alto) o isometrica (per ispezione).
        # Un solo pulsante a levetta nell'angolo in alto a destra dell'anteprima:
        # label e pulsante condividono la stessa cella di una griglia, cosi' il
        # pulsante resta agganciato all'angolo qualunque sia la dimensione.
        # La scelta vale per la vita del dialogo, senza persistenza.
        self._view = DEFAULT_PREVIEW_VIEW
        self._shown_path = None
        self._preview_image = None
        # La miniatura e' 340x340 ma il label puo' essere piu' basso: senza
        # adattarla, un pezzo ruotato di 90 gradi perdeva le estremita' tagliate.
        self.preview_label.installEventFilter(self)
        preview_box = QWidget()
        preview_grid = QGridLayout(preview_box)
        preview_grid.setContentsMargins(0, 0, 0, 0)
        preview_grid.addWidget(self.preview_label, 0, 0)
        self.view_button = QToolButton(preview_box)
        self.view_button.setObjectName('preview_view_toggle')
        self.view_button.setCheckable(True)
        self.view_button.setChecked(self._view == VIEW_ISOMETRIC)
        self.view_button.setAutoRaise(True)
        self.view_button.setIcon(QIcon(str(Path(__file__).resolve().parent
                                           / 'assets' / 'step-view-iso.svg')))
        self.view_button.setToolTip('Vista isometrica (per ispezione); rilasciato: '
                                    'vista in pianta, confrontabile con la foto')
        preview_grid.addWidget(self.view_button, 0, 0,
                               Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        self.view_button.raise_()
        self.view_button.toggled.connect(self._view_toggled)
        images.addWidget(preview_box)
        layout.addLayout(images)
        self._set_photo(photo_crop)
        # Library filter: the whole installed library, filtered KiCad-style by
        # typed fragments, so a model outside the five candidates needs no
        # folder browsing. The index is built lazily, on the first keystroke.
        self._library_models = None
        self._library_results = []
        self._index_token = 0
        self._index_worker = None
        filter_row = QHBoxLayout()
        self.library_filter = QLineEdit()
        self.library_filter.setObjectName('library_filter')
        self.library_filter.setPlaceholderText('Filtra la libreria KiCad: es. wroom usb-c sot23…')
        self.library_filter.setClearButtonEnabled(True)
        filter_row.addWidget(self.library_filter, 1)
        self.library_count_label = QLabel('')
        self.library_count_label.setTextFormat(Qt.TextFormat.PlainText)
        filter_row.addWidget(self.library_count_label)
        layout.addLayout(filter_row)
        self.library_list = QListWidget()
        self.library_list.setObjectName('library_list')
        self.library_list.setMaximumHeight(150)
        self.library_list.setVisible(False)
        layout.addWidget(self.library_list)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(FILTER_DEBOUNCE_MS)
        self._filter_timer.timeout.connect(self._apply_library_filter)
        self.library_filter.textChanged.connect(self._filter_text_changed)
        self.library_list.currentRowChanged.connect(self._library_row_changed)
        self.candidate_list = QListWidget()
        for candidate in self._candidates:
            self.candidate_list.addItem(str(candidate.get('package') or Path(candidate['path']).name))
        layout.addWidget(self.candidate_list)
        self.details_label = QLabel('Scegli un candidato, filtra la libreria KiCad, scegli un file STEP oppure conserva il segnaposto.')
        self.details_label.setTextFormat(Qt.TextFormat.PlainText)
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details_label)
        self.auto_rotation = QCheckBox('Automatico: deduci gli assi CAD dall’ingombro')
        self.auto_rotation.setChecked(_number(initial_rotation, None) is None)
        layout.addWidget(self.auto_rotation)
        self.rotation_hint = QLabel()
        self.rotation_hint.setWordWrap(True)
        layout.addWidget(self.rotation_hint)
        # Riga dedicata alla rotazione. Tenere manopola, stato e pulsanti sulla
        # stessa riga portava la larghezza minima del dialogo da 750 a 963 px:
        # lo stato deve stare accanto al campo, non costringere la finestra.
        rotation_row = QHBoxLayout()
        rotation_row.addWidget(QLabel('Rotazione Z locale del CAD nativo'))
        self.rotation = _RotationSpinBox()
        self.rotation.setRange(-360, 360)
        self.rotation.setDecimals(2)
        self.rotation.setSingleStep(90)
        self.rotation.setSuffix(' °')
        # Set before any connection: restoring a saved angle must not be mistaken
        # for the user claiming the explicit override.
        self.rotation.setValue(_number(initial_rotation))
        rotation_row.addWidget(self.rotation)
        # Which of the two modes is in charge, written beside the knob itself.
        # La frase lunga di rotation_hint non bastava: chi gira la manopola non
        # collega il campo a una spiegazione staccata, e nessuno passa il mouse
        # per un tooltip. Lo stato va letto dove si guarda, accanto al campo.
        self.rotation_state = QLabel()
        self.rotation_state.setObjectName('rotation_state')
        self.rotation_state.setTextFormat(Qt.TextFormat.PlainText)
        rotation_row.addWidget(self.rotation_state)
        rotation_row.addStretch(1)
        self._update_rotation_controls()
        layout.addLayout(rotation_row)
        controls = QHBoxLayout()
        self.manual_button = QPushButton('Scegli file STEP…')
        self.manual_button.clicked.connect(self._choose_file)
        controls.addWidget(self.manual_button)
        self.none_button = QPushButton('Nessuno corrisponde — conserva il segnaposto')
        self.none_button.clicked.connect(self._none_match)
        controls.addWidget(self.none_button)
        layout.addLayout(controls)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Usa il modello selezionato')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Annulla')
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.candidate_list.currentRowChanged.connect(self._candidate_changed)
        # Three routes into the same intent, because no single one covers it:
        # valueChanged for a value that really moved, textEdited for a value
        # typed over an identical one (0 over 0, the case that decides between
        # automatic and an explicit 0°), interacted for a step the range limits
        # swallowed. Any of them means "I am driving this rotation now".
        self.rotation.valueChanged.connect(self._rotation_value_changed)
        self.rotation.lineEdit().textEdited.connect(self._rotation_claimed)
        self.rotation.interacted.connect(self._rotation_claimed)
        self.auto_rotation.toggled.connect(self._rotation_mode_changed)
        if saved_path:
            row = next(i for i, c in enumerate(self._candidates) if c['path'] == saved_path)
            self.candidate_list.blockSignals(True)
            self.candidate_list.setCurrentRow(row)
            self.candidate_list.blockSignals(False)
            self._candidate_changed(row, preview=False)

    def showEvent(self, event):
        """@brief Start the restored selection preview only once the dialog is shown.

        @param event Qt show event.
        @note Construction never schedules CAD work; callers may replace preview
        hooks before showing the dialog, including in offscreen tests.
        """
        super().showEvent(event)
        if getattr(self, '_path', '') and not self._workers:
            self._start_preview()

    def _update_rotation_controls(self):
        """Keep the readable state and the hint aligned with the mode in force.

        @note The spin box is never disabled any more. A disabled field still
        looked usable — visible, with its arrows and a value — so turning it
        produced nothing and the reason lived in a separate sentence nobody
        connected to the knob. It now stays enabled, any interaction with it
        claims the explicit override (see _rotation_claimed), and the label
        beside it states which mode is actually in charge without hovering.
        """
        automatic = self.auto_rotation.isChecked()
        self.rotation_state.setText(tx('non in uso: comanda «Automatico»') if automatic
                                    else tx('in uso: override esplicito'))
        self.rotation_hint.setText(
            tx('L’anteprima usa lo stesso allineamento automatico della generazione CAD '
               '(ingombro totale e sezione del corpo). La rotazione nella foto è indipendente. '
               'Modificando la rotazione, «Automatico» si disattiva e l’override entra in vigore.')
            if automatic else
            tx('Override locale esplicito: anche 0° disattiva l’inferenza degli assi. '
               'La rotazione del componente nella foto resta indipendente.'))

    def _rotation_claimed(self, *_):
        """Hand the rotation to the explicit override the moment the user acts.

        @note Turning that knob has exactly one possible meaning: this angle,
        not the inferred one. Clearing «Automatico» here is what makes the
        gesture legible — the tick moves, the state label flips and the preview
        is rebuilt by _rotation_mode_changed — instead of being swallowed.
        Already explicit: nothing to claim, and the preview is left to
        _rotation_value_changed so a keystroke that does not move the value
        cannot restart a CAD run.
        """
        if self.auto_rotation.isChecked():
            self.auto_rotation.setChecked(False)

    def _rotation_value_changed(self, *_):
        """Rebuild the preview for a moved value, claiming the override first."""
        if self.auto_rotation.isChecked():
            # _rotation_mode_changed already rebuilds the preview with this
            # value; starting a second one here would only bump the token.
            self.auto_rotation.setChecked(False)
            return
        self._start_preview()

    def _rotation_mode_changed(self, *_):
        self._update_rotation_controls()
        self._start_preview()

    def _set_photo(self, crop):
        pixmap = QPixmap()
        if isinstance(crop, QPixmap):
            pixmap = crop
        elif isinstance(crop, QImage):
            pixmap = QPixmap.fromImage(crop)
        elif isinstance(crop, (bytes, bytearray)):
            pixmap.loadFromData(bytes(crop))
        elif isinstance(crop, (str, os.PathLike)):
            pixmap.load(str(crop))
        if not pixmap.isNull():
            self.photo_label.setPixmap(pixmap.scaled(340, 340, Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.SmoothTransformation))

    def _clear_selection(self, widget):
        """Deselect a list without re-entering its row handler."""
        blocker = QSignalBlocker(widget)
        try:
            widget.setCurrentRow(-1)
        finally:
            blocker.unblock()

    def _filter_text_changed(self, *_):
        # Coalesce keystrokes; an emptied field clears immediately so the
        # candidate list is not hidden behind stale rows for a quarter second.
        if self.library_filter.text().strip():
            self._filter_timer.start()
        else:
            self._filter_timer.stop()
            self._apply_library_filter()

    def _apply_library_filter(self, *_):
        """Fill the library list for the current query, indexing on first use."""
        query = self.library_filter.text().strip()
        if self._closed:
            return
        if not query:
            self._show_library_results([], 0, '')
            return
        if self._library_models is None:
            self.library_count_label.setText(tx('Indicizzazione della libreria…'))
            self._start_index()
            return
        if not self._library_models:
            self._show_library_results([], 0, tx('libreria KiCad non trovata'))
            return
        result = filter_models(self._library_models, query)
        if result.truncated:
            text = tx('primi {shown} di {total}', shown=len(result), total=result.total)
        elif result.total == 0:
            text = tx('nessun modello')
        elif result.total == 1:
            text = tx('1 modello')
        else:
            text = tx('{total} modelli', total=result.total)
        self._show_library_results(list(result), result.total, text)

    def _show_library_results(self, models, total, text):
        """Replace the library rows; the current row is intentionally dropped.

        A path already chosen from the library stays selected in the dialog
        state, exactly like a manually chosen file survives a cancelled picker.
        """
        blocker = QSignalBlocker(self.library_list)
        try:
            self.library_list.clear()
            self._library_results = list(models)
            for model in self._library_results:
                self.library_list.addItem(model.label)
        finally:
            blocker.unblock()
        self.library_list.setVisible(bool(self._library_results))
        self.library_count_label.setText(text)

    def _start_index(self):
        """Walk the library at most once per dialog, never on the GUI thread."""
        if self._index_worker is not None or self._closed:
            return
        self._index_token += 1
        worker = _Index(self._index_token)
        self._index_worker = worker
        worker.signals.ready.connect(self._index_ready)
        QThreadPool.globalInstance().start(worker)

    def _index_ready(self, token, models):
        self._index_worker = None
        if self._closed or token != self._index_token:
            return
        self._library_models = list(models)
        self._apply_library_filter()

    def _library_row_changed(self, row):
        if row < 0 or row >= len(self._library_results):
            return
        self._clear_selection(self.candidate_list)
        model = self._library_results[row]
        self._path = str(model.path)
        self.details_label.setText(tx('Dalla libreria KiCad: {path}\n'
                                      'Dimensioni e motivi di corrispondenza non disponibili.',
                                      path=self._path))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(_step_file(self._path))
        self._start_preview()

    def _candidate_changed(self, row, preview=True):
        if row < 0:
            return
        self._clear_selection(self.library_list)
        candidate = self._candidates[row]
        self._path = candidate['path']
        reasons = candidate.get('reasons') or []
        if isinstance(reasons, (list, tuple)):
            reasons = '\n'.join(str(reason) for reason in reasons)
        error = _number(candidate.get('relative_xy_error'), float('nan'))
        error_text = f'{error:.1%}' if math.isfinite(error) else tx('non disponibile')
        # Wording only: the two numbers are not peers. The model dimensions are
        # read off the CAD file, the envelope they are compared against is
        # reconstructed from the photograph, so the error belongs to the
        # estimate. Presenting both as flat facts made the reviewer distrust the
        # model instead of the measurement that is actually uncertain.
        dimensions = (_display_dimensions(candidate['dimensions_mm'])
                      if candidate.get('dimensions_mm') else tx('non disponibili'))
        self.details_label.setText(tx("{path}\nDimensioni misurate sul modello (mm): {dimensions}"
                                      "\nErrore relativo XY rispetto all’ingombro stimato dalla "
                                      "foto: {error}\nMotivi: {reasons}",
                                      path=self._path, dimensions=dimensions, error=error_text,
                                      reasons=reasons or tx('non disponibili')))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(_step_file(self._path))
        if preview:
            self._start_preview()

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, tx('Scegli modello STEP'), '',
                                              tx('Modelli STEP (*.step *.stp *.STEP *.STP)'))
        if not path:
            return
        self.candidate_list.setCurrentRow(-1)
        self._clear_selection(self.library_list)
        self._path = path
        self.details_label.setText(tx('STEP manuale: {path}\n'
                                      'Dimensioni e motivi di corrispondenza non disponibili.',
                                      path=path))
        self._start_preview()

    def eventFilter(self, watched, event):
        """@brief Qt override: refit the preview picture when its label is resized.

        @param watched Object the event is addressed to.
        @param event Qt event; only resizes of preview_label are acted upon.
        @return The base-class result: the event is never consumed here.
        """
        if (watched is getattr(self, 'preview_label', None)
                and event.type() == QEvent.Type.Resize
                and getattr(self, '_preview_image', None) is not None):
            self._fit_preview_image()
        return super().eventFilter(watched, event)

    def _fit_preview_image(self):
        """Show the last rendered picture whole inside the preview label.

        @note Only shrinks, never enlarges: the render is 340 px and blowing it
        up would only blur it. Aspect ratio is kept, so a turned part is shown
        turned, not squeezed.
        """
        pixmap = QPixmap.fromImage(self._preview_image)
        room = self.preview_label.contentsRect().size()
        if room.width() > 0 and room.height() > 0 and (
                pixmap.width() > room.width() or pixmap.height() > room.height()):
            pixmap = pixmap.scaled(room, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
        self.preview_label.setPixmap(pixmap)

    def _view_toggled(self, isometric):
        """Switch the preview camera and re-render with the rotation in force.

        @param isometric True for VIEW_ISOMETRIC, False for VIEW_PLAN.
        @note Without a selected model there is nothing to re-render: the choice
        is only remembered, and the invitation to pick a candidate stays shown.
        """
        self._view = VIEW_ISOMETRIC if isometric else VIEW_PLAN
        if getattr(self, '_path', ''):
            self._start_preview()

    def _start_preview(self, *_):
        self._token += 1
        path = getattr(self, '_path', '')
        valid = _step_file(path)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(valid)
        # Re-rendering the model already on screen (new angle, mode or view)
        # keeps its picture until the new one arrives: clearing it made the
        # preview flash «Caricamento…» on every step of the rotation. A
        # different model, or a file that is no longer usable, must not borrow
        # the old picture, so those still clear it.
        pixmap = self.preview_label.pixmap()
        keep = (valid and path == self._shown_path
                and pixmap is not None and not pixmap.isNull())
        if not keep:
            self._shown_path = None
            self._preview_image = None
            self.preview_label.clear()
            self.preview_label.setText(tx('Caricamento della geometria STEP reale…') if valid
                                       else tx('File STEP non disponibile'))
        # At most one process per dialog. Changes coalesce to the latest selection.
        if not valid or self._closed:
            self.preview_label.unsetCursor()
            return
        # The kept picture is stale until the new one lands; the busy cursor
        # over it says so without replacing it.
        self.preview_label.setCursor(Qt.CursorShape.BusyCursor)
        if self._workers:
            return
        worker = _Preview(self._token, self._path,
                          None if self.auto_rotation.isChecked() else self.rotation.value(),
                          self._dimensions, self._body_color, self._view)
        self._workers[self._token] = worker
        worker.signals.ready.connect(self._preview_ready)
        QThreadPool.globalInstance().start(worker)

    def _preview_ready(self, token, image):
        worker = self._workers.pop(token, None)
        if self._closed:
            return
        if token != self._token:
            self._start_preview()
            return
        self.preview_label.unsetCursor()
        if isinstance(image, QImage) and not image.isNull():
            self._preview_image = image
            self._fit_preview_image()
            # The token matched, so no selection change happened since the
            # worker started; its own path is still recorded for robustness.
            self._shown_path = worker.path if worker is not None else getattr(self, '_path', '')
        else:
            self._shown_path = None
            self._preview_image = None
            self.preview_label.setText(tx('Anteprima della geometria reale non disponibile.\n'
                                          'Puoi scegliere il file STEP oppure conservare il segnaposto.'))

    def _none_match(self):
        self._selection = dict(status='rejected', path='', rotation_deg=0.0)
        self.done(QDialog.DialogCode.Accepted)

    def accept(self):
        """@brief Confirm an existing STEP path and the selected native-model rotation."""
        if _step_file(getattr(self, '_path', '')):
            self._selection = dict(status='confirmed', path=self._path,
                                   rotation_deg=None if self.auto_rotation.isChecked() else float(self.rotation.value()))
            super().accept()

    def done(self, result):
        """@brief Close without waiting for CAD; discard late previews and cancelled choices.

        @param result Qt Accepted or Rejected dialog result.
        """
        self._closed = True
        self._token += 1
        self._index_token += 1
        self._filter_timer.stop()
        if result == QDialog.DialogCode.Rejected:
            self._selection = dict(status='rejected', path='', rotation_deg=0.0)
        super().done(result)

    def selected_selection(self) -> dict:
        """@brief Return a detached selection without changing either constructor input.

        @return Dict with status confirmed/rejected, path string and rotation_deg
        float for an explicit local override, or None for automatic CAD axis inference.
        @note Cancel returns rejected; callers must check exec() == Accepted before applying.
        """
        return dict(self._selection)
