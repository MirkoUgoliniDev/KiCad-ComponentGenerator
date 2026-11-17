"""@brief Interactive 3D preview based on VTK/PyVista with a real z-buffer.

3D preview with VTK/PyVista: a renderer with a real, interactive z-buffer.

It replaces the matplotlib preview (kicadgen.gui.preview_3d), which uses
the painter's algorithm sorting the polygons by centroid: on flush
geometries -- the silkscreen texts lying on the body surface -- that order
goes wrong and "eats" pieces of the text. VTK does hidden-surface removal
per pixel, so the problem does not arise, and the view also turns with the
mouse.

If pyvista/pyvistaqt are not installed, main_window falls back to the
matplotlib version: the preview is a convenience, it must not be a hard
requirement.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple, Optional
from ..services.messages import tx

os.environ.setdefault("QT_API", "pyqt6")

import numpy as np
from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from pyvistaqt import QtInteractor

from ..cad3d.body_params import Body3DParams
from ..generators.pin_header import PinHeaderParams, pin_positions

#: Maximum movement, in pixels, for a right press to still count as a click
#: and not as the start of a drag zoom. It is the steady-hand tolerance,
#: not a threshold tied to any particular content.
_CLICK_TOLERANCE_PX = 4

_BG_COLOR = "#1e1e2e"
_APPROX_BODY_COLOR = "#89b4fa"
_APPROX_PIN_COLOR = "#f9e2af"
_LABEL_COLOR = "#f9e2af"


def _component_index(name):
    """Parse only FreeCAD component names; board bodies and pins are not rows."""
    match = re.fullmatch(r'Cmp([0-9]+)_.+', name) if isinstance(name, str) else None
    if match is None:
        return None
    try:
        index = int(match.group(1))
        return index if index <= 2_147_483_647 else None
    except ValueError:
        return None


def _component_row(part):
    """Prefer original photo rows over filtered CAD indices and legacy names.

    Only nonnegative JSON integers fitting the Qt int signal are accepted;
    booleans, numeric strings and floats are not row identifiers. Invalid or
    absent metadata falls back to the next source, never to render order.
    """
    for field in ('source_row', 'component_index'):
        value = part.get(field)
        if type(value) is int and 0 <= value <= 2_147_483_647:
            return value
    return _component_index(part.get('name'))


#: Prepared scenes kept in memory, least recently used dropped first. The
#: window shows one generated model at a time; two entries also cover going
#: back and forth between two files. The real 13 MB preview of a module (171
#: parts, 287k triangles) takes ~27 MB once prepared, so the cache stays small.
_MESH_CACHE_SIZE = 2


class _PreparedPart(NamedTuple):
    """One drawable part: normals already computed, row for picking."""
    mesh: object
    color: object
    row: Optional[int]


class _PreparedScene(NamedTuple):
    """Everything render_mesh needs from a preview JSON, ready to draw."""
    parts: tuple
    anchors: tuple


def _mesh_file_key(mesh_path) -> tuple:
    """@brief Identity of a preview file: resolved path, device, inode, size, mtime.

    @param mesh_path Path of the preview JSON.
    @return Hashable key; a rewritten file gets a new one.
    @throws OSError when the file cannot be inspected.
    @note Content is not hashed (hashing 13 MB at every tab switch is what we
        want to avoid). A rewrite that keeps size, inode and mtime_ns would go
        unnoticed; with nanosecond mtimes that needs a same-size rewrite within
        the filesystem timestamp resolution (2 s on FAT) -- regenerating a model
        takes much longer than that.
    """
    resolved = Path(mesh_path).resolve()
    stat = resolved.stat()
    return (str(resolved), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


#: Second and later pieces of one FreeCAD object exported with another colour
#: (`freecad_build_body.py`: «<name>_color<n>»): the first piece carries the label.
_COLOUR_PIECE = re.compile(r"_color\d+$")


def _part_pin_number(part) -> Optional[str]:
    """@brief The pin number a preview part is the contact of, as text, or None.

    @param part One entry of the preview JSON's `parts`.
    @return The declared number when the part carries a `pin` field; else the
        text after «Pin» in its object name; None for a part that is no contact.
    @note Pin numbers are text end to end (A1, EP, 001): the number is never
        turned into an integer, so «A1» gets its label and «001» is not
        shown as «1» (super review 2026-11-16, R4-2).
    @note FALLBACK, declared: the object name is what FreeCAD made of the
        requested «Pin<number>». FreeCAD rewrites a name that is no valid
        identifier («PinA-1» -> «PinA_1») and suffixes a duplicate
        («Pin1» -> «Pin001»), so a name can mislabel a pin. Only a `pin`
        field written by the FreeCAD child from the contact it built
        removes that; it is read first whenever present.
    """
    declared = part.get("pin")
    if isinstance(declared, (str, int)) and not isinstance(declared, bool):
        text = str(declared).strip()
        if text:
            return text
    name = str(part.get("name", ""))
    if not name.startswith("Pin") or len(name) <= 3 or _COLOUR_PIECE.search(name):
        return None
    return name[3:]


def _prepare_mesh_scene(mesh_path) -> _PreparedScene:
    """@brief Read a FreeCAD preview JSON and build the meshes to draw.

    @param mesh_path Path of the preview JSON (parts with vertices/triangles).
    @return Prepared scene: one entry per part with triangles, pin label anchors.
    @throws ValueError when the file has no parts or no triangles.
    @note Pure data work, nothing touches the render window: a failure leaves
        the scene on screen as it was.
    """
    import pyvista as pv

    with open(mesh_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    parts = data.get("parts")
    if not parts:
        raise ValueError("mesh vuota")

    prepared = []
    anchors = []
    for part in parts:
        vertices = np.asarray(part["vertices"], dtype=float)
        triangles = np.asarray(part.get("triangles", []), dtype=int)
        if len(vertices) == 0 or len(triangles) == 0:
            continue

        # The lead pieces are the contacts (see freecad_build_body.py):
        # their pin number labels them, anchored to the tip of the pin.
        pin = _part_pin_number(part)
        if pin is not None:
            label = part.get("label")
            if label:
                # Point chosen by whoever built the piece (e.g. beside the
                # half-hole of a module, not over it): the number does not cover the metal.
                point = _spaced_label(label, vertices)
                anchors.append((pin, *point))
            else:
                centre = vertices.mean(axis=0)
                anchors.append(
                    (pin, float(centre[0]), float(centre[1]),
                     float(vertices[:, 2].min()))
                )
        # PyVista wants the faces as [n_points, i, j, k, n_points, ...]
        faces = np.hstack([np.full((len(triangles), 1), 3), triangles]).ravel()
        mesh = pv.PolyData(vertices, faces)
        # Normals with a feature angle: sharp edges stay crisp, while fillets
        # and pin cylinders are smoothed and read as curved (with flat
        # shading the small fillets vanished into the black).
        mesh = mesh.compute_normals(
            feature_angle=30, split_vertices=True, consistent_normals=True,
        )
        prepared.append(_PreparedPart(mesh, part.get("color", _APPROX_BODY_COLOR),
                                      _component_row(part)))

    if not prepared:
        raise ValueError(tx("mesh senza triangoli"))
    return _PreparedScene(tuple(prepared), tuple(anchors))


class _MeshJobSignals(QObject):
    """@brief Carrier of a mesh preparation result from the pool thread to the viewer.

    @note Created on the GUI thread, so the viewer's bound slot receives the
        emission queued on the GUI thread: the pool thread never touches the
        widget.
    """
    #: (token, file key, prepared scene or None, error text or "").
    finished = pyqtSignal(int, object, object, str)


class _MeshPrepareJob(QRunnable):
    """@brief Parse a preview JSON and compute normals off the GUI thread."""

    def __init__(self, token: int, key: tuple, mesh_path):
        """@brief Snapshot one preparation request.

        @param token Request number; the viewer applies only its latest one.
        @param key File identity (`_mesh_file_key`) taken when the request was made.
        @param mesh_path Path of the preview JSON.
        """
        super().__init__()
        self.signals = _MeshJobSignals()
        self.token, self.key, self.mesh_path = token, key, mesh_path

    def run(self) -> None:
        """@brief QRunnable override: prepare the scene and report it, or the error, once."""
        try:
            scene, error = _prepare_mesh_scene(self.mesh_path), ""
        except BaseException as exc:  # noqa: BLE001 - see below
            # Reported to the viewer like every read error. BaseException:
            # an exception escaping a QRunnable terminates the process, and
            # without an answer the loading indicator would stay on.
            scene, error = None, str(exc) or type(exc).__name__
        self.signals.finished.emit(self.token, self.key, scene, error)


#: Pin label height, mm. The former fixed height (0.65) was tuned on the
#: 2.54 mm pin header: the fraction keeps that ratio, so headers draw as
#: before and sparser parts (a relay with pins 6 mm apart, unreadable at
#: 0.65; user, 2026-11-04) scale up with the room between their anchors.
#: Floor and cap are heuristics, declared: below the floor nothing is
#: legible, above the cap the label looks like a marking of the part.
_LABEL_HEIGHT_MIN_MM = 0.65
_LABEL_HEIGHT_MAX_MM = 2.0
_LABEL_HEIGHT_OF_GAP = _LABEL_HEIGHT_MIN_MM / 2.54


def _pin_label_height(points) -> float:
    """@brief Pin label height, mm, from the room between the nearest two anchors.

    @param points Anchor positions (N x 3, mm).
    @return `_LABEL_HEIGHT_OF_GAP` times the smallest anchor distance,
        clamped to [_LABEL_HEIGHT_MIN_MM, _LABEL_HEIGHT_MAX_MM]; the floor
        alone with fewer than two anchors.
    @note A view-only size: the label must stay readable on a sparse part
        (a relay) without touching its neighbour on a dense one (a header, a
        fine-pitch grid, which keeps the former fixed height).
    """
    nearest = _nearest_anchor_gap(points)
    if not np.isfinite(nearest):
        return _LABEL_HEIGHT_MIN_MM
    return min(_LABEL_HEIGHT_MAX_MM, max(_LABEL_HEIGHT_MIN_MM, _LABEL_HEIGHT_OF_GAP * nearest))


def _nearest_anchor_gap(points) -> float:
    """Smallest XY distance between two distinct label anchors, mm; inf with fewer than two.

    Coincident anchors (the same pin read twice, e.g. a FreeCAD duplicate
    renamed «Pin001») are ignored: a zero room would draw every label at
    scale 0 (review 2026-11-14, 3).
    """
    if len(points) < 2:
        return float("inf")
    xy = np.asarray(points, dtype=float)[:, :2]
    deltas = xy[:, None, :] - xy[None, :, :]
    distances = np.sqrt((deltas ** 2).sum(axis=2))
    distances[distances <= 1e-6] = np.inf
    return float(distances.min())


#: Bound on the lines one pin label may wrap onto. Not the rule: the lines
#: stop as soon as one more line no longer raises the label's scale (the room
#: between neighbouring pins makes extra lines cost more than they give).
#: The bound only limits the work on an absurdly long name.
_LABEL_MAX_LINES = 8
#: Share of the room between two neighbouring anchors that one label block may
#: fill along the row, so stacked lines never touch the next pin's label.
#: Heuristic, declared: it only keeps a visible gap; it is not a measure.
_LABEL_ROW_FILL = 0.8
#: Reference string for the glyph height: digits, capitals, a descender and a
#: bracket, the tallest a pin name can draw. Every label is scaled by this
#: one height, so «GND» and «1 (GND)» get the same glyph size (`height=` of
#: Text3D scales each label to its OWN box: «GND» drew 30% larger).
_LABEL_REFERENCE = "Ag(1"
#: Characters vtkVectorText cannot draw, written with ASCII it can: they used
#: to vanish («IN−» drew as «IN») or leave an empty mesh that crashed the view.
_LABEL_ASCII = {"−": "-", "–": "-", "—": "-", "µ": "u", "μ": "u", "Ω": "Ohm", "°": "deg",
                "±": "+/-", "×": "x", "’": "'", "‘": "'", "“": '"', "”": '"'}


def _ascii_label(label):
    """A pin label as vtkVectorText can draw it: printable ASCII on one line.

    Known symbols are spelled out (`_LABEL_ASCII`), accents dropped (é -> e),
    any other character removed; line breaks, tabs and runs of spaces become
    one space, since the layout decides the lines (review 2026-11-14, 4 and 7).
    """
    import unicodedata
    spelled = "".join(_LABEL_ASCII.get(char, char) for char in str(label))
    plain = unicodedata.normalize("NFKD", spelled).encode("ascii", "ignore").decode("ascii")
    return " ".join("".join(char if " " <= char <= "~" else " " for char in plain).split())


#: Text3D width probes, natural units: one entry per probe string, kept for
#: the session. Labels are ASCII (`_ascii_label`), so at most three probes per
#: printable character: a few hundred floats in all.
_TEXT_WIDTHS = {}


def _probe_width(text):
    """Drawn width of `text` in natural Text3D units, measured once."""
    if text not in _TEXT_WIDTHS:
        import pyvista as pv
        points = pv.Text3D(text, depth=0).points
        _TEXT_WIDTHS[text] = float(np.ptp(points[:, 0])) if len(points) else 0.0
    return _TEXT_WIDTHS[text]


def _text_width(line):
    """Drawn width of one line in natural Text3D units.

    vtkVectorText draws printable ASCII with fixed advances and no kerning,
    so a line's width is the sum of its characters' advances, measured once
    each between two «I» (the bearings cancel): exact against Text3D, without
    a mesh per candidate line (the wrapping tries hundreds; one Text3D each
    took 0.9 s on 44 names). Any other character is measured on the line.
    """
    line = line.strip()
    if len(line) < 2 or any(not (" " <= char <= "~") for char in line):
        return _probe_width(line) if line else 0.0
    bar, pair = _probe_width("I"), _probe_width("II")
    middle = sum(_probe_width("I" + char + "I") - pair for char in line[1:-1])
    return ((_probe_width(line[0] + "I") - bar) + middle
            + (_probe_width("I" + line[-1]) - bar) - (pair - 2 * bar))


def _label_tokens(label):
    """Break points of a pin name: after «/», before « (» and at spaces (kept in the pieces)."""
    pieces, current = [], ""
    for index, char in enumerate(label):
        if char == " " and current:
            pieces.append(current)
            current = char
            continue
        current += char
        if char == "/" and index + 1 < len(label):
            pieces.append(current)
            current = ""
    if current:
        pieces.append(current)
    return pieces


def _balanced_lines(pieces, count, width):
    """Join `pieces` into `count` lines (fewer when there are fewer pieces) with the narrowest widest line.

    @param pieces Break-point pieces of one name (`_label_tokens`).
    @param count Number of lines wanted.
    @param width Callable giving the drawn width of one line.
    @return (widest line width, lines), the lines stripped of edge spaces.
    """
    pieces = tuple(pieces)
    total = len(pieces)
    memo = {}

    def best(first, lines):
        key = (first, lines)
        if key not in memo:
            if lines == 1:
                line = "".join(pieces[first:]).strip()
                memo[key] = (width(line), (line,))
            else:
                choice = None
                for cut in range(first + 1, total - lines + 2):
                    line = "".join(pieces[first:cut]).strip()
                    rest = best(cut, lines - 1)
                    candidate = (max(width(line), rest[0]), (line,) + rest[1])
                    if choice is None or candidate[0] < choice[0]:
                        choice = candidate
                memo[key] = choice
        return memo[key]

    return best(0, max(1, min(count, total)))


_LINE_ADVANCE = []


def _line_advance():
    """Distance between two baselines of vtkVectorText, natural units (measured once)."""
    if not _LINE_ADVANCE:
        import pyvista as pv
        _LINE_ADVANCE.append(float(np.ptp(pv.Text3D("A\nA", depth=0).points[:, 1]))
                             - float(np.ptp(pv.Text3D("A", depth=0).points[:, 1])))
    return _LINE_ADVANCE[0]


def _block_mesh(lines, right=False):
    """One label block in natural units: its lines stacked, flush left or flush right.

    vtkVectorText aligns a multi-line string on the left only; each line is
    drawn alone and placed, so a block can be flush on the side of its own
    pin strip (review 2026-11-14, 6). Empty lines draw nothing.
    """
    import pyvista as pv
    from vtkmodules.vtkRenderingFreeType import vtkVectorText
    drawn = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        # The raw glyphs sit on their baseline (y = 0); Text3D would centre
        # each line on its own box, and a line without brackets or
        # descenders would then leave its baseline row.
        source = vtkVectorText()
        source.SetText(line)
        source.Update()
        mesh = pv.wrap(source.GetOutput()).copy()
        if not mesh.n_points:
            continue
        coords = mesh.points.copy()
        coords[:, 1] -= index * _line_advance()
        mesh.points = coords
        drawn.append(mesh)
    if not drawn:
        return pv.PolyData()
    edge = (max(float(mesh.points[:, 0].max()) for mesh in drawn) if right
            else min(float(mesh.points[:, 0].min()) for mesh in drawn))
    for mesh in drawn:
        coords = mesh.points.copy()
        coords[:, 0] += edge - (coords[:, 0].max() if right else coords[:, 0].min())
        mesh.points = coords
    return pv.merge(drawn, merge_points=False) if len(drawn) > 1 else drawn[0]


def _pin_label_geometry(points):
    """Reading frame of the pin labels: (along_x, across, across_axis, middle, gap, max_width).

    `max_width` is the room one label has across the board, mm: 42% of the
    distance between the outer anchors, less the gap to the strip.
    """
    span = np.ptp(points[:, :2], axis=0)
    along_x = span[0] >= span[1]
    across = span[1] if along_x else span[0]
    across_axis = 1 if along_x else 0
    middle = (points[:, across_axis].min() + points[:, across_axis].max()) / 2
    gap = min(.8, across * .08)
    return along_x, across, across_axis, middle, gap, max(1.0, across * .42 - gap)


def _pin_label_layout(points, labels):
    """Lines and the one shared scale of the pin labels.

    One glyph size for every label (user, 2026-11-14: «uniformiamo la
    dimensione delle scritte, badando che ci stiano»). Each name may wrap,
    at its own break points, onto balanced lines; for each number of lines
    the largest scale is the one at which the widest line fits the room
    across the board AND the block fits the room between neighbouring pins.
    The shared scale is the smallest of the names' best scales, so every
    label fits; each name then uses the fewest lines that reach it. Before,
    each label was shrunk alone, so long names became unreadable next to
    large short ones (0.13 against 0.65 mm on the ESP32 module).
    @note Fitting comes first: there is no legibility floor, because a floor
        would let labels overlap. One very tight pair of pins shrinks every
        label; the user can then read the names in the symbol or zoom in.

    @return dict: ``height`` (nominal glyph height, mm), ``fraction`` (shared
        scale, <= 1), ``scale`` (natural Text3D units -> mm), ``blocks``
        (lines of each label), ``texts`` (natural block meshes, flush left),
        ``max_width``, ``room`` and ``advance`` (mm).
    """
    import pyvista as pv
    height = _pin_label_height(points)
    max_width = _pin_label_geometry(points)[5]
    # Natural Text3D units -> mm, from the one reference height.
    unit = height / max(1e-9, float(np.ptp(pv.Text3D(_LABEL_REFERENCE, depth=0).points[:, 1])))
    # A block of n lines is (n - 1) line advances plus one glyph height along
    # the row (vtkVectorText advance measured, not assumed).
    advance = unit * _line_advance()
    room = _LABEL_ROW_FILL * _nearest_anchor_gap(points)

    def options(label):
        """(largest scale <= 1, lines) for 1, 2, ... lines, until one more line stops helping."""
        pieces = _label_tokens(label) or [label]
        found = []
        for count in range(1, min(_LABEL_MAX_LINES, len(pieces)) + 1):
            widest, lines = _balanced_lines(pieces, count, _text_width)
            by_width = min(1.0, max_width / (widest * unit)) if widest > 0 else 1.0
            by_height = min(1.0, room / ((len(lines) - 1) * advance + height))
            fit = min(by_width, by_height)
            if found and fit <= found[-1][0]:
                break
            found.append((fit, lines))
        return found

    choices = [options(_ascii_label(label)) for label in labels]
    shared = min((max(fit for fit, _lines in found) for found in choices), default=1.0)
    # Each name: the fewest lines that reach the shared scale.
    blocks = [next(lines for fit, lines in found if fit >= shared - 1e-9) for found in choices]
    texts = [_block_mesh(block) for block in blocks]
    # Final check on the real blocks, width AND height: the widths that chose
    # the breaks are a model (`_text_width`); the drawn block must fit.
    shown = [text for text in texts if text.n_points]
    drawn_width = max((float(np.ptp(text.points[:, 0])) for text in shown), default=0.0)
    drawn_height = max((float(np.ptp(text.points[:, 1])) for text in shown), default=0.0)
    if drawn_width > 0:
        shared = min(shared, max_width / (drawn_width * unit))
    if drawn_height > 0 and np.isfinite(room):
        shared = min(shared, room / (drawn_height * unit))
    return dict(height=height, fraction=shared, scale=unit * shared, blocks=blocks, texts=texts,
                max_width=max_width, room=room, advance=advance)


def _pin_text_mesh(points, labels):
    """View-only planar text, with one shared reading direction on the pin face.

    Sizes and lines come from `_pin_label_layout`: one glyph size for every
    label, each one fitting; a block is flush on the side of its own strip.
    """
    import pyvista as pv
    layout = _pin_label_layout(points, labels)
    along_x, across, across_axis, middle, gap, _max_width = _pin_label_geometry(points)
    # Text normal faces -Z, so names read correctly from the underside.
    horizontal = np.array([0., -1., 0.] if along_x else [1., 0., 0.])
    vertical = np.array([-1., 0., 0.] if along_x else [0., -1., 0.])
    scale = layout["scale"]
    pieces = []
    for point, block in zip(points, layout["blocks"]):
        inward = (1 if point[across_axis] < middle else -1) if across > 1e-6 else 0
        # Flush on the strip side: the text's right end points outward when
        # the reading direction runs from the middle towards this strip.
        text = _block_mesh(block, right=inward != 0 and horizontal[across_axis] * -inward > 0)
        if not text.n_points:
            # Nothing drawable (an empty name): no piece, never an empty mesh.
            continue
        coords = text.points.copy() * scale
        coords -= (coords.min(axis=0) + coords.max(axis=0)) / 2
        # Anchors already include an inward clearance. Bring the text 3 mm
        # closer to the strip without changing its size or reading direction.
        origin = point.copy()
        if inward:
            outward_adjustment = min(3.0, across * .225)
            origin[across_axis] += inward * (
                np.ptp(coords[:, 0]) / 2 + gap - outward_adjustment)
        text.points = (origin + coords[:, 0, None] * horizontal
                       + coords[:, 1, None] * vertical)
        pieces.append(text)
    if not pieces:
        return pv.PolyData()
    return pv.merge(pieces, merge_points=False)


def _spaced_label(label, vertices):
    """Keep legacy mesh labels away from the contact, without changing CAD."""
    point = np.asarray(label, dtype=float).copy()
    centre = (vertices.min(axis=0) + vertices.max(axis=0)) / 2
    direction = point[:2] - centre[:2]
    distance = np.linalg.norm(direction)
    if distance > 1e-6:
        point[:2] += 3.2 * direction / distance
    return point


#: Colours of the «copia formato» outlines (HANDOFF 158): the body whose
#: format was copied, and the bodies it was pasted on. The bodies keep their
#: own colours: a wire box around them says which is which without hiding
#: the colour the format carries.
from ..services.component_format import FORMAT_SOURCE_COLOR, FORMAT_TARGET_COLOR  # noqa: E402
#: Gap between a body and its outline, as a fraction of its largest extent,
#: with a floor so a tiny chip still shows a box and not a smear.
FORMAT_OUTLINE_GAP = 0.06
FORMAT_OUTLINE_MIN_GAP_MM = 0.2


class Preview3DVtk(QtInteractor):
    """@brief Interactive 3D view: the real generated mesh or the parametric approximation."""

    component_requested = pyqtSignal(int)
    #: Steady left click on a component while «copia formato» (format painter)
    #: is on (`format_painter_active`): the component's row.
    component_clicked = pyqtSignal(int)
    #: Right press on a component: row index and global point where to open
    #: the menu. The view does not know what can be done on a component --
    #: the window builds the entries, since it has the table and the models.
    component_menu_requested = pyqtSignal(int, object)
    #: Request token of `prepare_mesh`: the scene of that file is ready, and
    #: `render_mesh` on it now only builds actors (no parsing, no normals).
    mesh_prepared = pyqtSignal(int)
    #: Request token of `prepare_mesh` and the error text: the file could not
    #: be read or holds nothing drawable. The scene on screen is untouched.
    mesh_prepare_failed = pyqtSignal(int, str)
    #: Internal: immediate answers of `prepare_mesh` (file already prepared
    #: or not inspectable) take the same queued path as the pool results, so
    #: the caller always hears back after `prepare_mesh` has returned.
    _mesh_answer = pyqtSignal(int, object, object, str)

    #: Duration of the animated view change asked by the navigation cube, in
    #: ms; 0 or less jumps at once. 250 ms is the usual slicer/CAD feel.
    camera_animation_ms = 250
    #: Frame period of the animation (about 60 frames per second).
    CAMERA_FRAME_MS = 16

    def __init__(self, parent=None):
        """@brief Initialize the viewport and optional component picking.

        @param parent Optional Qt parent widget.
        """
        self._component_actors = {}
        #: Actors of every part of a component, by table row: the reverse of
        #: `_component_actors`, for the «copia formato» outlines (HANDOFF 158).
        self._row_actors = {}
        #: Outline actors of the «copia formato» mode now on screen.
        self._format_outlines = []
        self._right_press = None
        #: «copia formato» (format painter) mode (user, 2026-09-27): a steady
        #: left click on a component emits `component_clicked`; when off, the
        #: click stays only the start of a rotation, as always.
        self.format_painter_active = False
        self._left_press = None
        self._menu_served = False
        # Scene shown and scenes already prepared, by identity key of the file.
        self._mesh_cache = OrderedDict()
        self._shown_mesh_key = None
        self._mesh_anchors = ()
        # Preparation off the GUI thread (`prepare_mesh`): number of the
        # latest request, jobs in flight (they keep their signals alive until
        # the answer is delivered), viewer closed.
        self._prepare_token = 0
        self._prepare_jobs = {}
        #: Preparations in flight by file identity: a second request for the
        #: same file waits for the running job instead of parsing it again.
        self._prepare_in_flight = {}
        #: File identity of the latest request (see prepare_mesh).
        self._latest_mesh_key = None
        #: Set by closeEvent. NOT pyvista's `_closed`: pyvistaqt's close()
        #: returns early when that flag is already set, and the render window
        #: would never be released (block D review, 131).
        self._viewer_closed = False
        # Camera animation (navigation cube).
        self._camera_animation = None
        self._camera_timer = None
        self._camera_clock = time.monotonic
        super().__init__(parent)
        self._camera_timer = QTimer(self)
        self._camera_timer.setInterval(self.CAMERA_FRAME_MS)
        self._camera_timer.timeout.connect(self._on_camera_tick)
        self._mesh_answer.connect(self._on_mesh_job_finished, Qt.ConnectionType.QueuedConnection)
        self.setToolTip('Doppio clic su un componente: scegli modello KiCad e ruota il modello.\n'
                        'Tasto destro: elimina, ruota o cambia il modello.')
        self._configure_component_picking()
        self.set_background(_BG_COLOR)
        self.show_pin_numbers = True
        try:
            # Three-point lighting: it gives relief to the dark parts, where
            # the single frontal light flattens everything.
            self.enable_lightkit()
        except Exception:  # noqa: BLE001 - default lighting
            pass
        self._label_actor = None
        # Navigation cube at the top right (in place of the row of «Vista»
        # (view) buttons). It is a convenience: if VTK lacks the annotation
        # module the view works anyway, it is rotated with the mouse.
        self.view_cube = None
        self._cube_press = False
        try:
            from .view_cube import ViewCube
            self.view_cube = ViewCube(self.ren_win, self.renderer)
            self.view_cube.layout(self.width(), self.height())
        except Exception:  # noqa: BLE001 - the cube is an accessory
            self.view_cube = None
        # The labels update also when the user rotates with the mouse, not
        # only when pressing the view buttons.
        try:
            self.iren.add_observer("EndInteractionEvent", self._on_interaction_end)
        except Exception:  # noqa: BLE001 - without an interactor the labels stay static
            pass

    def _on_interaction_end(self, *_args) -> None:
        self._update_label_visibility()

    def _configure_component_picking(self):
        # Actor callbacks identify all subparts without replacing shaders or
        # adding a highlighted mesh. Left-button camera rotation stays enabled.
        try:
            self.enable_mesh_picking(callback=self._component_picked, use_actor=True,
                                     left_clicking=False, show=False, show_message=False)
        except (AttributeError, RuntimeError, ImportError):
            pass  # Optional interaction must not prevent rendering a model.

    def _component_actor_at(self, x, y):
        """@brief Actor under a point already expressed in VTK display coordinates."""
        try:
            from vtkmodules.vtkRenderingCore import vtkPropPicker
            picker = vtkPropPicker()
            if not picker.Pick(x, y, 0, self.renderer):
                return None
            return picker.GetActor()
        except (AttributeError, RuntimeError, ImportError):
            return None

    def _component_row_at_widget_point(self, x, y):
        """@brief Table row under a point in Qt coordinates.

        @param x Logical widget X, px.
        @param y Logical widget Y, px (from the top).
        @return The component row index under the point, or None when no
            component actor is there.
        @note The Y axis must be flipped: Qt counts from the top, VTK from the
            bottom. The scale ratio is needed on high-density monitors, where
            Qt's logical pixels are not those of the framebuffer.
        """
        try:
            ratio = float(self.devicePixelRatioF())
        except (AttributeError, TypeError, ValueError):
            ratio = 1.0
        actor = self._component_actor_at(x * ratio, (self.height() - y) * ratio)
        if actor is None:
            return None
        return self._component_actors.get(actor.GetAddressAsString(''))

    def _request_component_menu(self, position, global_position) -> bool:
        """@brief Emit the menu request, if there is a component under the point.

        @param position Cursor position in widget coordinates (QPoint).
        @param global_position Cursor position in screen coordinates, where
            the menu is to be opened.
        @return True when the request was emitted, so the caller knows it has
                already served this click and does not serve it a second time.
        """
        index = self._component_row_at_widget_point(position.x(), position.y())
        if index is None:
            return False
        self._menu_served = True
        self.component_menu_requested.emit(index, global_position)
        return True

    # ── Navigation cube ─────────────────────────────────────────────

    def set_view_cube_labels(self, labels: dict) -> None:
        """@brief Translate the face labels of the navigation cube.

        @param labels Mapping view name (top, bottom, front, back, right, left) -> text.
        """
        if self.view_cube is not None:
            self.view_cube.set_labels(labels)
            self.render()

    def view_direction(self, direction) -> None:
        """@brief Look at the model from a direction (edge or corner of the cube), at once.

        @param direction Vector pointing from the model towards the camera.
        @note Stops a running camera animation: the last request wins.
        """
        self.stop_camera_animation()
        norm = float(np.linalg.norm(direction))
        if norm <= 0:
            return
        unit = np.asarray(direction, dtype=float) / norm
        focal = np.asarray(self.camera.focal_point, dtype=float)
        distance = float(np.linalg.norm(np.asarray(self.camera.position) - focal)) or 1.0
        # With the camera almost vertical, up cannot be Z.
        up = (0.0, 0.0, 1.0) if abs(unit[2]) < 0.99 else (0.0, 1.0, 0.0)
        self.camera_position = [tuple(focal + unit * distance), tuple(focal), up]
        self.fit_view()

    def _cube_clicked(self, target) -> None:
        """Apply what a click on the cube selected: home, a face or a diagonal."""
        from .view_cube import FIT, ROLL_CCW, ROLL_CW, view_for_direction
        if target == "home":
            self.animate_to_view("iso")
            return
        if target == FIT:
            self.stop_camera_animation()
            self.fit_view()
            return
        if target in (ROLL_CW, ROLL_CCW):
            self.roll_view(clockwise=target == ROLL_CW)
            return
        view = view_for_direction(target)
        if view is not None:
            self.animate_to_view(view)
        else:
            self.animate_to_direction(target)

    def roll_view(self, clockwise: bool) -> None:
        """@brief Turn the view by 90 degrees about the viewing axis, animated.

        @param clockwise True to turn the image clockwise on screen.
        @note The target view-up is computed once, at the click: the last
            animation frame applies that absolute value, so it never rolls a
            second time from a camera that is already turned.
        """
        from .view_cube import rolled_up
        camera = self.camera
        position, focal = tuple(camera.GetPosition()), tuple(camera.GetFocalPoint())
        target = rolled_up(position, focal, tuple(camera.GetViewUp()), clockwise)

        def apply():
            camera.SetPosition(*position)
            camera.SetFocalPoint(*focal)
            camera.SetViewUp(*target)
            self.renderer.ResetCameraClippingRange()
            self._update_label_visibility()
            self.render()
        self._animate_camera(apply)

    # ── Camera animation ─────────────────────────────────────

    def animate_to_view(self, view: str) -> None:
        """@brief Turn the camera smoothly to a standard view.

        @param view Name accepted by show_view (iso, top, bottom, front, ...).
        @note The last frame is show_view(view) itself, so the final camera
            is exactly the one the immediate call gives, framing included.
        """
        self._animate_camera(lambda: self.show_view(view))

    def animate_to_direction(self, direction) -> None:
        """@brief Turn the camera smoothly to look from a direction.

        @param direction Vector pointing from the model towards the camera.
        @note The last frame is view_direction(direction) itself.
        """
        self._animate_camera(lambda: self.view_direction(direction))

    def is_camera_animating(self) -> bool:
        """@brief Whether an animated view change is in progress.

        @return True between `_animate_camera` and the last frame (or a stop).
        """
        return self._camera_animation is not None

    def stop_camera_animation(self) -> None:
        """@brief Stop an animated view change where it is (no jump to its end).

        @note Safe on a half-built widget: clear() can run before __init__ ends.
        """
        timer = getattr(self, "_camera_timer", None)
        if timer is not None:
            timer.stop()
        self._camera_animation = None

    def _camera_pose(self):
        from .view_cube import CameraPose
        camera = self.camera
        return CameraPose(tuple(camera.GetPosition()), tuple(camera.GetFocalPoint()),
                          tuple(camera.GetViewUp()), float(camera.GetParallelScale()),
                          float(camera.GetViewAngle()))

    def _set_camera_pose(self, pose) -> None:
        camera = self.camera
        camera.SetFocalPoint(*pose.focal)
        camera.SetPosition(*pose.position)
        camera.SetViewUp(*pose.up)
        camera.SetParallelScale(pose.parallel_scale)
        camera.SetViewAngle(pose.view_angle)
        self.renderer.ResetCameraClippingRange()

    @contextmanager
    def _rendering_suppressed(self):
        """Batch scene or camera changes into one final render."""
        previous = self.suppress_rendering
        self.suppress_rendering = True
        try:
            yield
        finally:
            self.suppress_rendering = previous

    def _pose_after(self, apply):
        """Camera pose that `apply` would produce, without showing it.

        The camera, the "camera already set" flag and the pin label
        visibility are restored afterwards; nothing is rendered meanwhile.
        """
        from vtkmodules.vtkRenderingCore import vtkCamera
        saved = vtkCamera()
        saved.DeepCopy(self.camera)
        camera_set = self.renderer.camera_set
        labels = self._label_actor.GetVisibility() if self._label_actor is not None else None
        try:
            with self._rendering_suppressed():
                apply()
            return self._camera_pose()
        finally:
            self.camera.DeepCopy(saved)
            self.renderer.camera_set = camera_set
            if labels is not None:
                self._label_actor.SetVisibility(labels)

    def _animate_camera(self, apply) -> None:
        """Start (or restart, from the current camera) an animation towards `apply`'s view."""
        self.stop_camera_animation()
        duration = float(self.camera_animation_ms or 0) / 1000.0
        if duration <= 0 or self._camera_timer is None:
            apply()
            return
        start = self._camera_pose()
        end = self._pose_after(apply)
        if all(np.allclose(a, b, rtol=1e-9, atol=1e-9) for a, b in zip(start, end)):
            apply()  # already there: nothing to animate, only the final framing
            return
        self._camera_animation = (start, end, apply, self._camera_clock(), duration)
        self._camera_timer.start()

    def _on_camera_tick(self) -> None:
        """One animation frame; the last one applies the exact target view."""
        from .view_cube import ease_in_out, interpolate_pose
        animation = self._camera_animation
        if animation is None or getattr(self, "_viewer_closed", False):
            self.stop_camera_animation()
            return
        start, end, apply, started, duration = animation
        progress = (self._camera_clock() - started) / duration
        if progress >= 1.0:
            self.stop_camera_animation()
            apply()
            return
        self._set_camera_pose(interpolate_pose(start, end, ease_in_out(progress)))
        if self._label_actor is not None:
            self._label_actor.SetVisibility(self.show_pin_numbers and self._viewing_from_below())
        self.render()

    def _on_cube(self, event) -> bool:
        """True when a mouse event falls on the cube or on the house."""
        if self.view_cube is None:
            return False
        point = event.position()
        return self.view_cube.contains(point.x(), point.y())

    def resizeEvent(self, event):  # noqa: N802 - Qt signature
        """@brief Keep the cube in the top-right corner at a fixed pixel size.

        @param event Qt resize event.
        """
        super().resizeEvent(event)
        if self.view_cube is not None:
            self.view_cube.layout(self.width(), self.height())

    def closeEvent(self, event):  # noqa: N802 - Qt signature
        """@brief Stop the camera animation before the render window goes away.

        @param event Qt close event.
        @note Mesh preparations still running in the pool finish on their own;
            their results are dropped (`_on_mesh_job_finished`), never drawn
            on a finalized render window.
        """
        self._viewer_closed = True
        self.stop_camera_animation()
        super().closeEvent(event)

    def wheelEvent(self, event):  # noqa: N802 - Qt signature
        """@brief A wheel zoom takes over the camera: stop a running view animation.

        @param event Qt wheel event, forwarded to VTK after the stop.
        """
        self.stop_camera_animation()
        super().wheelEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt signature
        """@brief Pointing-hand cursor over the cube and its buttons, with their tooltip.

        @param event Qt mouse move event, forwarded to VTK afterwards.
        """
        from PyQt6.QtWidgets import QToolTip
        if self._on_cube(event):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            point = event.position()
            text = self.view_cube.tooltip_at(point.x(), point.y())
            if text:
                QToolTip.showText(event.globalPosition().toPoint(), text, self)
            else:
                QToolTip.hideText()
        else:
            if self.format_painter_active:
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.unsetCursor()
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def set_view_cube_tooltips(self, tooltips: dict) -> None:
        """@brief Translate the tooltips of the buttons next to the navigation cube.

        @param tooltips Mapping "home", "fit", "roll_cw", "roll_ccw" -> text.
        """
        if self.view_cube is not None:
            self.view_cube.set_tooltips(tooltips)

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt signature
        """@brief Open the component under the cursor; on the cube, nothing more.

        @param event Qt mouse double-click event.
        @note Served here, from the Qt event, not by a VTK observer: the Qt
            interactor of VTK forwards a double click as a second
            LeftButtonPressEvent with repeat count 1 and never fires
            LeftButtonDoubleClickEvent (checked on VTK 9.7), so the observer
            used before was never called. Outside components the double click
            keeps its VTK meaning.
        """
        if self._on_cube(event):
            event.accept()
            return
        if (event.button() == Qt.MouseButton.LeftButton and not self.format_painter_active
                and self._serve_double_click(event.position())):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _serve_double_click(self, point) -> bool:
        """@brief Emit component_requested for the component under a Qt point.

        @param point Cursor position in widget coordinates (QPointF).
        @return True when a component was under the point and was requested.
        """
        index = self._component_row_at_widget_point(point.x(), point.y())
        if index is None:
            return False
        self.component_requested.emit(index)
        return True

    def mousePressEvent(self, event):  # noqa: N802 - Qt signature
        """@brief Serve clicks on the cube; remember where a right press starts.

        @param event Qt mouse press event.
        @note A left click on the cube never reaches VTK: it would also start
            a camera rotation, and the view would turn under the cursor.
            Outside the cube's hit area (and in the empty corners of its
            square) the mouse keeps its usual meaning, and any press there
            stops a running view animation.
        """
        if event.button() == Qt.MouseButton.LeftButton and self._on_cube(event):
            point = event.position()
            target = self.view_cube.hit(point.x(), point.y())
            if target is not None:
                self._cube_press = True
                event.accept()
                self._cube_clicked(target)
                return
        # Any other gesture takes the camera in hand: the cube animation
        # must not keep moving it under the mouse.
        self.stop_camera_animation()
        if event.button() == Qt.MouseButton.RightButton:
            self._right_press = event.pos()
            self._menu_served = False
        if event.button() == Qt.MouseButton.LeftButton:
            self._left_press = event.pos() if self.format_painter_active else None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt signature
        """@brief Open the menu on a still right click over a component.

        @param event Qt mouse release event.
        @note Qt synthesises `contextMenuEvent` from the system message, and
            over the native window VTK creates for itself it is not certain to
            arrive; the button release instead always passes through here,
            because VTK's Qt interactor is an ordinary QWidget that forwards
            it. The click is therefore served here, and `contextMenuEvent`
            remains for the keyboard menu key.
        @note VTK's right button is also the drag zoom: the menu opens only if
            the mouse did not move (within _CLICK_TOLERANCE_PX), otherwise a
            zoom was asked for and the menu would be a nuisance. A still left
            click emits `component_clicked` with the same tolerance.
        """
        if getattr(self, "_cube_press", False) and event.button() == Qt.MouseButton.LeftButton:
            # The click on the cube was already served on press: VTK did not
            # see the press and must not see the release either.
            self._cube_press = False
            event.accept()
            return
        left_start = getattr(self, "_left_press", None)
        self._left_press = None
        start = getattr(self, "_right_press", None)
        self._right_press = None
        super().mouseReleaseEvent(event)
        if (event.button() == Qt.MouseButton.LeftButton and left_start is not None
                and (event.pos() - left_start).manhattanLength() <= _CLICK_TOLERANCE_PX):
            # A steady click, not a rotation: the same tolerance as the menu.
            index = self._component_row_at_widget_point(event.pos().x(), event.pos().y())
            if index is not None:
                self.component_clicked.emit(index)
            return
        if event.button() != Qt.MouseButton.RightButton or start is None:
            return
        if (event.pos() - start).manhattanLength() > _CLICK_TOLERANCE_PX:
            return
        self._request_component_menu(event.pos(), event.globalPosition().toPoint())

    def contextMenuEvent(self, event):  # noqa: N802 - Qt signature
        """@brief Alternative route to the menu (dedicated key, or platforms that
        deliver this event instead of the button release).

        @param event Qt context menu event; accepted when the menu was already
            served by the release or is served here, forwarded to VTK otherwise.
        """
        if getattr(self, "_menu_served", False):
            # The click was already served by the release: a second menu
            # for the same gesture would only be a duplicate.
            self._menu_served = False
            event.accept()
            return
        if self._request_component_menu(event.pos(), event.globalPos()):
            event.accept()
            return
        # Outside the components the right button stays VTK's.
        super().contextMenuEvent(event)

    def _component_picked(self, actor):
        if actor is None:
            return
        index = self._component_actors.get(actor.GetAddressAsString(''))
        if index is not None:
            self.component_requested.emit(index)

    def clear(self) -> None:
        """@brief Clear actors and invalidate component picking for the old mesh.

        @note Also forgets which mesh file is shown (is_showing becomes False)
            and stops a camera animation; the prepared-mesh cache is kept, so
            showing the same file again does not re-read it.
        """
        self._component_actors.clear()
        self._row_actors = {}
        # The outline actors go with every other actor of the old mesh.
        self._format_outlines = []
        self._shown_mesh_key = None
        self._mesh_anchors = ()
        self.stop_camera_animation()
        self._pin_label_anchors = ()
        self._label_actor = None
        super().clear()

    # -- «Copia formato»: which body was copied, which ones were pasted -----

    def highlight_format_rows(self, source_row, target_rows=()) -> int:
        """@brief Outline the copied body in yellow and the pasted ones in green.

        @param source_row Table row of the body whose format was copied, or
            None for none.
        @param target_rows Table rows the format was pasted on, in order.
        @return Number of outlines drawn; a row with no actor on screen (not
            in this mesh) draws nothing and is not an error.
        @note Replaces the outlines of the previous call: the set on screen is
            always exactly the current source and targets. Bodies keep their
            colours; the outline is a wire box slightly larger than the body
            (FORMAT_OUTLINE_GAP), so it reads around any colour, the magenta
            of a generic box included.
        """
        import pyvista as pv
        self.clear_format_outlines(render=False)
        wanted = [(source_row, FORMAT_SOURCE_COLOR)] if source_row is not None else []
        wanted += [(row, FORMAT_TARGET_COLOR) for row in target_rows]
        drawn = 0
        for row, color in wanted:
            bounds = self._row_bounds(row)
            if bounds is None:
                continue
            x0, x1, y0, y1, z0, z1 = bounds
            gap = max(FORMAT_OUTLINE_MIN_GAP_MM,
                      FORMAT_OUTLINE_GAP * max(x1 - x0, y1 - y0, z1 - z0))
            box = pv.Box((x0 - gap, x1 + gap, y0 - gap, y1 + gap, z0 - gap, z1 + gap)).outline()
            actor = self.add_mesh(box, color=color, line_width=3, reset_camera=False,
                                  render=False, pickable=False)
            self._format_outlines.append(actor)
            drawn += 1
        self.render()
        return drawn

    def clear_format_outlines(self, render=True) -> None:
        """@brief Remove every «copia formato» outline; the bodies are untouched.

        @param render Repaint now; False when the caller repaints itself.
        """
        for actor in getattr(self, "_format_outlines", None) or []:
            self.remove_actor(actor, render=False)
        self._format_outlines = []
        if render:
            self.render()

    def _row_bounds(self, row):
        """World bounds (x0, x1, y0, y1, z0, z1) of every part of ``row``, or None."""
        actors = (getattr(self, "_row_actors", None) or {}).get(row) or []
        merged = None
        for actor in actors:
            bounds = actor.GetBounds()
            if bounds is None or len(bounds) != 6:
                continue
            if merged is None:
                merged = list(bounds)
            else:
                for axis in range(3):
                    merged[2 * axis] = min(merged[2 * axis], bounds[2 * axis])
                    merged[2 * axis + 1] = max(merged[2 * axis + 1], bounds[2 * axis + 1])
        return tuple(merged) if merged else None

    def set_pin_numbers(self, enabled: bool) -> None:
        """@brief Enable or disable the pin number labels (still shown only from the pin side).

        @param enabled True to show the labels when the camera looks from the
            pin side (negative Z); False hides them in every view.
        """
        self.show_pin_numbers = enabled
        self._update_label_visibility()

    def _viewing_from_below(self) -> bool:
        """True when the camera looks at the component from below (negative Z).

        The Z=0 plane is the soldering plane: the leads are below, the body
        above, so a camera at negative Z is looking at the pin side.
        """
        try:
            return float(self.camera.position[2]) < 0.0
        except Exception:  # noqa: BLE001 - camera not available
            return False

    def _update_label_visibility(self) -> None:
        """The numbers appear only from the pin side: from above the body covers
        them and they would only add confusion."""
        if self._label_actor is None:
            return
        self._label_actor.SetVisibility(self.show_pin_numbers and self._viewing_from_below())
        self.render()

    def _add_pin_labels(self, anchors) -> None:
        """Numbers centred on the anchors, visible only from the pin side."""
        self._pin_label_anchors = tuple(anchors)
        self._label_actor = None
        if not self.show_pin_numbers or not anchors:
            return
        import numpy as np

        points = np.array([[x, y, z] for _, x, y, z in anchors], dtype=float)
        names = getattr(self, '_pin_functions', {})
        labels = [f"{pin} ({names[str(pin)]})" if names.get(str(pin)) else str(pin)
                  for pin, _, _, _ in anchors]
        mesh = _pin_text_mesh(points, labels)
        if not mesh.n_points:
            # Nothing drawable: an empty mesh would raise inside add_mesh.
            return
        self._label_actor = self.add_mesh(
            mesh, color=_LABEL_COLOR,
            lighting=False, pickable=False, name='pin-labels',
            reset_camera=False, render=False,
        )

    def set_pin_functions(self, pinout) -> None:
        """@brief Update view-only pin functions without rebuilding CAD or moving the camera.

        @param pinout Pin rows as dicts with `pin` and optional `name`; None
            or empty clears the functions. Rows without `pin` are ignored.
        """
        names = {str(row['pin']).strip(): str(row.get('name') or '').strip()
                 for row in pinout or () if isinstance(row, dict) and 'pin' in row}
        if names == getattr(self, '_pin_functions', {}):
            return
        self._pin_functions = names
        anchors = getattr(self, '_pin_label_anchors', ())
        if getattr(self, '_label_actor', None) is not None:
            self.remove_actor(self._label_actor, reset_camera=False, render=False)
            self._label_actor = None
        if anchors:
            self._add_pin_labels(anchors)
            self._update_label_visibility()

    def _reset(self) -> None:
        self.clear()
        self.set_background(_BG_COLOR)

    def _finish(self) -> None:
        self.show_axes()
        # Three lights instead of the single frontal one: a curved surface lit
        # from the front renders almost flat, and on a dark case the fillets would vanish.
        self.enable_3_lights()
        self.show_view("iso")

    def fit_view(self) -> None:
        """@brief Frame the whole component with a margin after resynchronizing the render window size."""
        # VTK may have cached a render window size different from the current
        # one of the Qt widget -- it happens when the widget lives inside a
        # layout that changes size after the first render.
        # The renderer is not contained in the scrolling area of the settings.
        # Without resynchronising it,
        # reset_camera() frames with respect to the old area: the model
        # ends up centred in a box that is no longer the visible one,
        # and appears shifted with respect to the real window.
        try:
            ratio = self.devicePixelRatioF()
            self.window_size = (max(1, round(self.width() * ratio)),
                                max(1, round(self.height() * ratio)))
        except Exception:  # noqa: BLE001 - best-effort resynchronisation
            pass
        self.reset_camera()
        self.camera.zoom(0.85)  # without a margin the pins stay out of frame
        self._update_label_visibility()
        self.render()

    def show_view(self, view: str) -> None:
        """@brief Standard view at once: iso / top / bottom / front / back / left / right.

        @param view View name; anything else is the initial isometric view.
        @note Stops a running camera animation: the last request wins.
        """
        self.stop_camera_animation()
        if view == "top":
            self.view_xy()
        elif view == "bottom":
            self.view_xy(negative=True)
        elif view == "front":
            self.view_xz()
        elif view == "back":
            self.view_xz(negative=True)
        elif view == "right":
            self.view_yz()
        elif view == "left":
            self.view_yz(negative=True)
        else:
            self.view_isometric()
            self.camera.azimuth = -35
            self.camera.elevation = 10
        self.fit_view()

    def clear_preview(self) -> None:
        """@brief Empty the preview; the next render_mesh() redraws even the same file."""
        self._reset()
        self.render()

    def is_showing(self, mesh_path) -> bool:
        """@brief Whether this very file, unchanged, is the mesh on screen.

        Cheap (one stat call): the window uses it to decide whether entering
        the 3D tab needs a loading indicator.

        @param mesh_path Path of a preview JSON.
        @return True when render_mesh(mesh_path) would redraw nothing: the
            same resolved file, with the same size and modification time, is
            displayed and the scene was not cleared or replaced since.
        """
        if self._shown_mesh_key is None or not mesh_path:
            return False
        try:
            return _mesh_file_key(mesh_path) == self._shown_mesh_key
        except (OSError, TypeError, ValueError):
            return False

    def render_params(self, pin_params: PinHeaderParams, body: Body3DParams) -> None:
        """@brief Approximate preview before generation: a box and pin cylinders.

        @param pin_params Pin layout.
        @param body Body dimensions.
        @throws ValueError when the body parameters are invalid.
        @note Drawn with one render at the end, not one per pin.
        """
        import pyvista as pv

        body.validate()
        with self._rendering_suppressed():
            self._draw_params(pv, pin_params, body)
        self.render()

    def _draw_params(self, pv, pin_params, body) -> None:
        """Build the approximate scene; the caller renders once at the end."""
        self._reset()

        box = pv.Box(bounds=(
            body.offset_x_mm, body.offset_x_mm + body.length_mm,
            body.offset_y_mm, body.offset_y_mm + body.width_mm,
            0.0, body.height_mm,
        ))
        self.add_mesh(box, color=_APPROX_BODY_COLOR, opacity=0.65, specular=0.2,
                      reset_camera=False, render=False)

        radius = body.pin_diameter_mm / 2.0
        anchors = []
        for number, x, y in pin_positions(pin_params):
            pin = pv.Cylinder(
                center=(x, y, -body.pin_length_mm / 2.0), direction=(0, 0, 1),
                radius=radius, height=body.pin_length_mm,
            )
            self.add_mesh(pin, color=_APPROX_PIN_COLOR, specular=0.4,
                          reset_camera=False, render=False)
            anchors.append((number, x, y, -body.pin_length_mm))

        self._add_pin_labels(anchors)
        self._finish()

    def prepare_mesh(self, mesh_path) -> int:
        """@brief Prepare a preview JSON off the GUI thread, so `render_mesh` is cheap afterwards.

        @param mesh_path Path of the preview JSON.
        @return Token of this request; `mesh_prepared(token)` or
            `mesh_prepare_failed(token, error)` follows, always after this call
            has returned and always on the GUI thread.
        @note Latest request wins: a request made later supersedes this one,
            whose result is then dropped without any signal. Parsing and
            normals run in the global QThreadPool; the scene is stored in the
            prepared-mesh cache only when its request is still the latest.
        @note A file already shown or already prepared answers at once (queued),
            without starting a job.
        """
        self._prepare_token += 1
        token = self._prepare_token
        try:
            key = _mesh_file_key(mesh_path)
        except (OSError, TypeError, ValueError) as exc:
            self._latest_mesh_key = None
            self._mesh_answer.emit(token, None, None, str(exc) or type(exc).__name__)
            return token
        self._latest_mesh_key = key
        if key == self._shown_mesh_key or key in self._mesh_cache:
            self._mesh_answer.emit(token, key, None, "")
            return token
        if key in self._prepare_in_flight:
            # Same unchanged file already being parsed: its result answers
            # this (latest) request too, instead of a second parse.
            return token
        job = _MeshPrepareJob(token, key, mesh_path)
        self._prepare_jobs[token] = job
        self._prepare_in_flight[key] = job
        job.signals.finished.connect(self._on_mesh_job_finished)
        QThreadPool.globalInstance().start(job)
        return token

    def _on_mesh_job_finished(self, token: int, key, scene, error: str) -> None:
        """GUI-thread end of `prepare_mesh`: keep only the latest request's result."""
        job = self._prepare_jobs.pop(token, None)
        if job is not None and self._prepare_in_flight.get(key) is job:
            del self._prepare_in_flight[key]
        # The job answers the latest request when it was started for it, or
        # when the latest request asked for the same unchanged file.
        current = token == self._prepare_token or key == self._latest_mesh_key
        if getattr(self, "_viewer_closed", False) or not current:
            return  # superseded by a more recent request, or viewer closed
        token = self._prepare_token
        if error:
            self.mesh_prepare_failed.emit(token, error)
            return
        if scene is not None:
            self._mesh_cache[key] = scene
            self._mesh_cache.move_to_end(key)
            while len(self._mesh_cache) > _MESH_CACHE_SIZE:
                self._mesh_cache.popitem(last=False)
        self.mesh_prepared.emit(token)

    def render_mesh(self, mesh_path) -> None:
        """@brief Show the tessellated mesh written by FreeCAD, one colour per part.

        @param mesh_path Path of the preview JSON.
        @throws OSError when the file cannot be read.
        @throws ValueError when it holds no parts or no triangles; the scene on
            screen is then left untouched.
        @note Re-entering the 3D tab calls this every time. When the same
            unchanged file is already shown (see is_showing) nothing is rebuilt:
            scene and camera stay, only the pin labels follow the current
            setting. Otherwise the prepared meshes come from a small cache keyed
            by the file identity, so only a new or changed file is parsed.
        @note Synchronous: an uncached file is parsed here, on the calling
            thread. Callers that must not block the GUI call `prepare_mesh`
            first and render when `mesh_prepared` arrives.
        @note The scene is built with rendering suppressed and drawn once: a
            render per added part used to redraw the growing scene ~170 times
            for a real module.
        """
        key = _mesh_file_key(mesh_path)
        if key == self._shown_mesh_key:
            self._refresh_pin_labels()
            return
        scene = self._mesh_cache.get(key)
        if scene is None:
            scene = _prepare_mesh_scene(mesh_path)
            self._mesh_cache[key] = scene
            while len(self._mesh_cache) > _MESH_CACHE_SIZE:
                self._mesh_cache.popitem(last=False)
        else:
            self._mesh_cache.move_to_end(key)

        with self._rendering_suppressed():
            self._reset()
            for part in scene.parts:
                # high ambient: real modules are almost black (#1a1a1a) and with
                # diffuse light only, on a dark background, they became an
                # indistinguishable blot.
                actor = self.add_mesh(
                    part.mesh, color=part.color,
                    smooth_shading=True, specular=0.4, specular_power=20,
                    ambient=0.35, diffuse=0.75, reset_camera=False, render=False,
                )
                if part.row is not None:
                    self._component_actors[actor.GetAddressAsString('')] = part.row
                    # Tolerant of a view built without __init__ (the picking
                    # tests): the map is created on first use.
                    row_actors = getattr(self, "_row_actors", None)
                    if row_actors is None:
                        row_actors = self._row_actors = {}
                    row_actors.setdefault(part.row, []).append(actor)
            self._mesh_anchors = scene.anchors
            self._add_pin_labels(list(scene.anchors))
            self._finish()
        self._shown_mesh_key = key
        self.render()

    def _refresh_pin_labels(self) -> None:
        """Pin labels of the mesh on screen follow the current setting, no rebuild.

        Renders only when something changed: showing the tab repaints the
        widget anyway, and a full frame of a large module is not free.
        """
        changed = False
        if self.show_pin_numbers and self._label_actor is None and self._mesh_anchors:
            with self._rendering_suppressed():
                self._add_pin_labels(list(self._mesh_anchors))
            changed = True
        if self._label_actor is not None:
            visible = bool(self.show_pin_numbers and self._viewing_from_below())
            if bool(self._label_actor.GetVisibility()) != visible:
                self._label_actor.SetVisibility(visible)
                changed = True
        if changed:
            self.render()
