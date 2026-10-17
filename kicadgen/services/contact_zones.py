"""@brief Where the module's contacts are, according to what was declared.

The local detector regularly mistakes rows of pads for components: on the
test board it called them «switch», «battery», «clock». Filtering them by
class is useless -- the class the model assigns is exactly the wrong one --
and so is filtering by size, because a pad and a 0603 capacitor measure the
same.

But the position of the contacts is not an unknown: the extraction declares
it (sides, count, pitch, offset) and it is the same geometry the footprint
draws the pads with. A box that covers the COPPER of a declared contact, and
that is as small as the pitch, is that contact (contact_proposals).

Since 2026-09-27 (user: «regola generale, non una pezza», a general rule,
not a patch) the filter criterion is structural and no longer depends on the
depth of the strip: the copper of an edge contact reaches the board edge, so
a box on that pad TOUCHES the edge; a body mounted next to the contact does
not. Before, it was enough that the centre fell inside the strip, and when
the pad was not dimensioned the strip took the PITCH as its length: on a
side with a 5.08 mm pitch it was 6.7 mm deep and deleted the bodies mounted
near the contacts before any review. The pitch is no longer a fallback
length (_pad_length).

DECLARED HEURISTIC (not a physical rule). Position, pitch and pad length are
declared quantities; the margins by which the contact strip is widened are
not: the depth adds half a hole and _LABEL_GAP_MM for the silkscreen label,
and the tolerance along the edge is _BAND_TOLERANCE (half a pitch). They are
conventions: the first is how THIS program lays out the silkscreen, not how
the manufacturer of the part printed it; the second is half the pitch, that
is the boundary with the neighbouring contact. They were observed, not
measured, on the test board (castellated modules with a 2.54 mm pitch), where
the detector annotated pad and label together. A measurement of the real
silkscreen would make them superfluous (for instance the pad mask derived
from the photo, or the silkscreen dimensions in the mechanical drawing).
Since 27/09 the strip (depth_mm) serves only the mask of the edge
measurement (contact_rects_mm): the proposal filter uses the physical contact
with the copper, and a silkscreen label next to the contact is no longer
removed by position -- the AI discards it as silkscreen.
"""
from __future__ import annotations

import dataclasses
import math

from ..core.validation import finite_number, positive_number

from ..generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CONTACT_STYLES, CONTACT_THROUGH_HOLE,
    AssumedSize, CastellatedModuleParams, CastellatedSide,
)

#: How far a body may protrude beyond the contact band and still be a
#: contact: half a pad. Beyond that, it is a body sitting on top or beside.
#: HEURISTIC (see the module docstring): half a pitch is the boundary with
#: the neighbouring contact, not a measurement of the pad annotated by the detector.
_BAND_TOLERANCE = 0.5

#: Distance between the end of the pad and its silkscreen label, as the
#: footprint generator lays it out (`label_gap` in step_generator): half a
#: hole plus this margin. It was not chosen to make a case fit, but it stays
#: a HEURISTIC: it is the convention with which this program draws the
#: silkscreen, and that the labels printed on the real part sit roughly there
#: is an observation on the test board, not a measurement (see the module
#: docstring for what would make it unnecessary).
_LABEL_GAP_MM = 0.55


def _read(side, *names):
    """First non-None value among `names`, read from a dict or an object's attributes."""
    for name in names:
        value = side.get(name) if isinstance(side, dict) else getattr(side, name, None)
        if value is not None:
            return value
    return None


def _side_value(side, *names):
    """First positive finite value among `names`, read from a dict or an object."""
    for name in names:
        number = positive_number(_read(side, name))
        if number is not None:
            return number
    return None


#: Fields the installed CastellatedSide accepts: the per-side diameters came
#: later, and a side built with a field the class does not know would stop
#: the contact filter over an accessory datum.
_SIDE_FIELDS = frozenset(field.name for field in dataclasses.fields(CastellatedSide))


def _hole_size(side, data, field, *names):
    """Per-side hole size with its provenance: declared as a float, else AssumedSize.

    A size is declared only when something says it was read or derived from
    the documents: the side's `<name>_declared` flag (GUI table), the side's
    own `value_sources` or the extraction's `castellated_sides.<edge>.<field>`
    entry. An estimate, or a value with no provenance at all, stays an
    assumption the generator may reduce to fit, exactly as the footprint and
    the 3D treat it; a declared size that does not fit makes side_geometry
    refuse the side instead.
    """
    value = _side_value(side, field, *names)
    if value is None:
        return None
    flag = _read(side, field.replace('_mm', '_declared'))
    own = _read(side, 'value_sources')
    module = data.get('value_sources') if isinstance(data, dict) else None
    sources = (own.get(field) if isinstance(own, dict) else None,
               module.get(f"castellated_sides.{_read(side, 'edge')}.{field}")
               if isinstance(module, dict) else None)
    if flag is True or any(source in ('read', 'derived') for source in sources):
        return value
    return AssumedSize(value)


def _module_size(data, key):
    """Module-wide hole size with its provenance, same rule as _hole_size.

    Declared only when the extraction's top-level `value_sources` says the
    value was read or derived; otherwise an AssumedSize, so that a per-side
    estimate still outranks it and a read row spacing can reduce it.
    """
    value = positive_number(data.get(key))
    if value is None:
        return None
    sources = data.get('value_sources')
    if isinstance(sources, dict) and sources.get(key) in ('read', 'derived'):
        return value
    return AssumedSize(value)


def declared_module(data, length_mm, width_mm):
    """@brief The castellated module the footprint would draw from the declared quantities.

    @param data Extracted parameters (raw): castellated_sides with edge, count,
        pitch_mm, offset_mm, contact_style and the optional side-level hole sizes
        half_hole_diameter_mm (legacy hole_diameter_mm) and
        through_hole_drill_mm; module-wide drill_mm / pad_diameter_mm for the
        through-hole and row_spacing_mm.
    @param length_mm Board extent along X, in mm.
    @param width_mm Board extent along Y, in mm.
    @return (CastellatedModuleParams, list of (declared side, CastellatedSide))
        for the sides that carry a valid edge, count and pitch.
    @note Hole sizes and through-hole positions are then read per side with
        CastellatedModuleParams.side_geometry, the same rule the footprint and
        the 3D use: a side's declared size wins over the module's, an
        undeclared or merely estimated one is an assumed size (reduced to fit
        the pitch and the declared row spacing when needed, see _hole_size), and the through-hole of a half_and_through side
        sits where the declared row spacing puts it. Before this, the photo
        mask used module-wide values and a through-hole offset that ignored
        the row spacing, so it could disagree with the footprint it filters for.
    @note Tolerant of side descriptions that are objects rather than dicts
        (attributes pitch/offset/hole_diameter/... as CastellatedSide names
        them), and of an installed CastellatedSide without the side-level size
        fields: those sizes are then simply not passed.
    """
    pairs = []
    sides = data.get('castellated_sides') if isinstance(data, dict) else None
    for side in sides or []:
        if not isinstance(side, dict) and not hasattr(side, 'edge'):
            continue
        edge, count = _read(side, 'edge'), _read(side, 'count')
        pitch = _side_value(side, 'pitch_mm', 'pitch')
        if edge not in ('left', 'right', 'top', 'bottom') or pitch is None:
            continue
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            continue
        style = _read(side, 'contact_style')
        options = dict(
            contact_style=style if style in CONTACT_STYLES else CONTACT_HALF_AND_THROUGH,
            hole_diameter=_hole_size(side, data, 'half_hole_diameter_mm', 'hole_diameter_mm',
                                     'hole_diameter'),
            thru_hole_drill=_hole_size(side, data, 'through_hole_drill_mm', 'thru_hole_drill'),
            thru_pad_diameter=_side_value(side, 'thru_pad_diameter'))
        pairs.append((side, CastellatedSide(
            edge=edge, count=count, pitch=pitch,
            **{key: value for key, value in options.items() if key in _SIDE_FIELDS})))
    declared = data if isinstance(data, dict) else {}
    module = CastellatedModuleParams(
        body_length=length_mm, body_width=width_mm, sides=[built for _, built in pairs],
        # None = not declared: the generator uses its reference dimension.
        # Estimate or value without provenance = AssumedSize (see _module_size).
        thru_hole_drill=_module_size(declared, 'drill_mm'),
        thru_pad_diameter=_module_size(declared, 'pad_diameter_mm'),
        row_spacing=positive_number(declared.get('row_spacing_mm')))
    return module, pairs


def _geometry(module, built):
    """Side geometry, or None when the installed generator cannot give it."""
    resolve = getattr(module, 'side_geometry', None)
    if resolve is None:
        return None
    try:
        return resolve(built)
    except (TypeError, ValueError, ArithmeticError):
        return None


def _pad_length(side, data, pitch):
    """Declared land length of a side: the side's, the module's, else 0 (not known).

    The pitch is NOT a land length: using it as the fallback made the strip of
    a wide-pitch side (5.08 mm) reach 6.7 mm into the board (user, 2026-09-27).
    `pitch` stays in the signature for the callers.
    """
    return (_side_value(side, 'pad_length_mm', 'pad_length')
            or positive_number(data.get('pad_length_mm')) or 0.0)


def _continuous_pad_depth(side, data, pitch, geometry):
    """@brief Depth of the strip of a half_and_through side: continuous pad and label.

    @param side Declared side whose OBSERVED contact_style is half_and_through
        (an absent style is not assumed here: see contact_positions).
    @param data Extracted parameters (pad_length_mm).
    @param pitch Declared pitch of the side, the last-resort pad length.
    @param geometry SideGeometry of the side (declared_module + side_geometry).
    @return Millimetres from the board edge inward.
    @note The footprint and the 3D draw such a contact as ONE gold pad from the
        edge to beyond the through-hole ring (castellated_module.continuous_pad),
        so the strip must reach the far side of that ring, not just the end of
        the declared edge pad. Ring and its distance from the edge are the
        side's own (SideGeometry.thru_inset and ring, row spacing included),
        the label margin is the 3D generator's (half-hole radius plus
        _LABEL_GAP_MM). Without a through-hole (drill or ring zero) the
        contact is the edge pad alone.
    """
    pad_length = _pad_length(side, data, pitch)
    edge_band = pad_length + geometry.half_hole / 2 + _LABEL_GAP_MM
    if not geometry.has_thru_holes or geometry.thru_inset is None:
        return edge_band
    # Same margin as the edge pad's label, counted from the end of the
    # continuous copper when that lies further in.
    return edge_band + max(0.0, geometry.thru_inset + geometry.ring / 2 - pad_length)


def _half_hole_depth(side, data, pitch, geometry):
    """@brief Depth of the strip of a half_hole side: edge pad, half-hole and label.

    @param side Declared side whose OBSERVED contact_style is half_hole.
    @param data Extracted parameters (pad_length_mm).
    @param pitch Declared pitch of the side, the last-resort pad length.
    @param geometry SideGeometry of the side.
    @return Millimetres from the board edge inward: pad length plus half the
        side's half-hole plus _LABEL_GAP_MM, the label convention of the 3D
        generator (label_gap = half-hole radius + margin).
    """
    return _pad_length(side, data, pitch) + geometry.half_hole / 2 + _LABEL_GAP_MM


def _band_depth(side, data, pitch):
    """@brief Depth of the strip occupied by the contact and its silkscreen label.

    @param side Declared side, which may carry the pad length.
    @param data Extracted parameters, holding the quantities shared by all sides.
    @param pitch Declared pitch of the side, in mm, used as the fallback.
    @return Millimetres from the board edge inward.
    @note The detector does not annotate the centre of the half-hole: it
        annotates the gold pad, which runs under the module, and the label
        printed next to it. A zone reduced to the contact centre alone does
        not catch them, and that is exactly what let the rows of pads through.
        The depth comes from the declared quantities plus the generator's
        silkscreen convention, not from a measurement on the photo: it is
        therefore a heuristic, declared as such in the module docstring.
    @note Used when the contact style is NOT observed (and for matrices):
        only declared quantities are taken here, never the generator's assumed
        sizes, because widening the mask on a hypothesis would remove from the
        measurement the bodies mounted near the edge. The side's declared
        half-hole comes before any other transverse quantity.
    """
    return _pad_length(side, data, pitch) + _declared_hole(side, data) / 2 + _LABEL_GAP_MM


def _declared_hole(side, data):
    """Declared transverse size of a contact of unobserved style (0 when none is declared).

    In castellated modules the hole is not dimensioned and drill_mm is 0 by
    convention: in that case the pad width is the known transverse size.
    """
    return (_side_value(side, 'half_hole_diameter_mm', 'hole_diameter_mm')
            or positive_number(data.get('drill_mm'))
            or _side_value(side, 'pad_width_mm', 'pad_width')
            or positive_number(data.get('pad_diameter_mm')) or 0.0)


def _through_hole_depth(geometry):
    """@brief Depth of the strip of a through_hole side: web, ring and label.

    @param geometry SideGeometry of a side with contact_style == "through_hole".
    @return Millimetres from the board edge inward.
    @note The contact is not an edge pad but a closed ring set back from the
        edge, with intact board in between. Its centre, ring and bore are the
        side's own, from the same rule the footprint uses
        (CastellatedModuleParams.side_geometry: declared row spacing, else its
        declared heuristic fallback). The strip ends where the label ends by
        this program's convention: ring edge plus half a bore plus
        _LABEL_GAP_MM, exactly as for the edge pads.
    """
    return geometry.thru_inset + geometry.ring / 2 + geometry.drill / 2 + _LABEL_GAP_MM


def contact_positions(data, length_mm, width_mm):
    """@brief Centres of the declared contacts, in millimetres from the board centre.

    @param data Extracted parameters: layout and castellated sides, or a matrix.
    @param length_mm Board extent along X, in mm.
    @param width_mm Board extent along Y, in mm.
    @return List of dicts with x_mm, y_mm, pitch_mm, edge, depth_mm.
    @note A matrix also needs body_offset_x_mm and body_offset_y_mm: the grid
        sits where the declared offsets put it, not at the board centre (see
        _matrix_positions).
    @note Returns an empty list when the declared quantities are insufficient:
        without them nothing is filtered, because a filter that guesses where
        the contacts are would delete real components.
    """
    if not isinstance(data, dict):
        return []
    positions = []
    half_x, half_y = length_mm / 2, width_mm / 2
    module, pairs = declared_module(data, length_mm, width_mm)
    for side, built in pairs:
        edge, count, pitch = built.edge, built.count, float(built.pitch)
        offset = finite_number(_read(side, 'offset_mm', 'offset')) or 0.0
        # The strip describes the contact the side really offers: the edge
        # pad (default, and types with a half-hole) or the set-back ring of a
        # through hole.
        # Only an OBSERVED type widens the strip. A missing type is drawn
        # downstream as half_and_through, but that is an assumption: masking
        # the photo on an assumption would remove the bodies mounted near the
        # edge from the measurement (see the @note of contact_positions).
        # Without a type the declared edge-pad strip stays, as before.
        style = _read(side, 'contact_style')
        geometry = _geometry(module, built) if style in CONTACT_STYLES else None
        # Copper facts used by the filter (contact_proposals): whether the
        # contact reaches the edge, the known radius of its half-hole and, for
        # a set-back hole, where the ring lies.
        at_edge, edge_radius, ring_inset, ring_radius = True, 0.0, None, None
        if (geometry is not None and style == CONTACT_THROUGH_HOLE
                and geometry.thru_inset is not None):
            depth = _through_hole_depth(geometry)
            at_edge, ring_inset, ring_radius = False, geometry.thru_inset, geometry.ring / 2
        elif geometry is not None and style == CONTACT_HALF_AND_THROUGH:
            depth = _continuous_pad_depth(side, data, pitch, geometry)
            edge_radius = geometry.half_hole / 2
        elif geometry is not None and style == CONTACT_HALF_HOLE:
            depth = _half_hole_depth(side, data, pitch, geometry)
            edge_radius = geometry.half_hole / 2
        else:
            depth = _band_depth(side, data, pitch)
            edge_radius = _declared_hole(side, data) / 2
        start = -(count - 1) * pitch / 2 + offset
        for index in range(count):
            along = start + index * pitch
            common = dict(pitch_mm=pitch, edge=edge, depth_mm=depth,
                          copper_mm=max(0.0, depth - _LABEL_GAP_MM), at_edge=at_edge,
                          edge_radius_mm=edge_radius, ring_inset_mm=ring_inset,
                          ring_radius_mm=ring_radius)
            if edge == 'left':
                positions.append(dict(common, x_mm=-half_x, y_mm=along))
            elif edge == 'right':
                positions.append(dict(common, x_mm=half_x, y_mm=along))
            elif edge == 'top':
                positions.append(dict(common, x_mm=along, y_mm=-half_y))
            else:
                positions.append(dict(common, x_mm=along, y_mm=half_y))
    if positions:
        return positions
    return _matrix_positions(data, length_mm, width_mm)


def _matrix_positions(data, length_mm, width_mm):
    """@brief Centres of the pins of a declared through-hole matrix, board-centred.

    @param data Extracted parameters as the extraction returns them (raw):
        rows, cols, pitch_mm, row_spacing_mm, body_offset_x_mm,
        body_offset_y_mm and optionally pin_map, pin_map_view and
        populated_pins.
    @param length_mm Board extent along X.
    @param width_mm Board extent along Y.
    @return One entry per pin that exists, or [] when the grid cannot be
        placed from declared quantities.
    @note The grid is NOT centred on the board. Its placement is declared:
        body_offset_x_mm / body_offset_y_mm are the body's minimum X / Y
        measured from pin 1 and from the first row (the same quantities the
        footprint uses, pin_header.PinHeaderParams.body_offset_*). A module
        whose header sits along one edge would otherwise get phantom pins in
        the middle of the board, and real bodies there would be deleted
        before anyone reviews them. Without the offsets the position is
        unknown and, as for any missing quantity here, nothing is filtered.
    @note Pin positions come from pin_header.pin_positions, the single source
        the footprint and the 3D pins use: pin_map and populated_pins count
        exactly as they will be drawn, and an empty grid position is not a pin.
    """
    rows, cols = data.get('rows'), data.get('cols')
    pitch = positive_number(data.get('pitch_mm'))
    spacing = positive_number(data.get('row_spacing_mm'))
    offset_x = finite_number(data.get('body_offset_x_mm'))
    offset_y = finite_number(data.get('body_offset_y_mm'))
    if not (isinstance(rows, int) and not isinstance(rows, bool) and rows >= 1
            and isinstance(cols, int) and not isinstance(cols, bool) and cols >= 1
            and pitch is not None and (spacing is not None or rows == 1)
            and offset_x is not None and offset_y is not None):
        return []
    from ..generators.pin_header import PinHeaderParams, pin_positions
    try:
        mapping = None
        if data.get('pin_map'):
            mapping = {int(entry['pin']): (int(entry['row']), int(entry['col']))
                       for entry in data['pin_map']}
        populated = data.get('populated_pins')
        grid = PinHeaderParams(rows=rows, cols=cols, pitch=pitch, row_spacing=spacing,
                               populated_pins=set(int(p) for p in populated) if populated else None,
                               pin_map=mapping)
        grid.validate()
        pins = [(x, y) for _, x, y in pin_positions(grid)]
    except (TypeError, ValueError, KeyError):
        # Unreadable pinout: where the pins are is unknown, so nothing is filtered.
        return []
    # The left edge of the board (-length/2) lies offset_x from pin 1, the
    # top one (-width/2) offset_y from the first row.
    origin_x, origin_y = -length_mm / 2 - offset_x, -width_mm / 2 - offset_y
    # A pin_map read on a BOTTOM VIEW carries the columns and X offset of
    # that view; the extraction flips them together (_pin_map_seen_from_top
    # and _offset_x_seen_from_top in datasheet_extractor), which amounts to
    # mirroring the whole pin field around the board centre. Here the raw
    # values arrive, so the same reflection is applied.
    mirror = -1.0 if (data.get('pin_map') and data.get('pin_map_view') == 'bottom') else 1.0
    depth = _band_depth({}, data, pitch)
    return [dict(x_mm=mirror * (origin_x + x), y_mm=origin_y + y, pitch_mm=pitch,
                 edge='matrix', depth_mm=depth) for x, y in pins]


def contact_rects_mm(positions, length_mm, width_mm):
    """@brief Rectangles (x0,y0,x1,y1) in mm occupied by the edge contacts.

    @param positions Output of contact_positions.
    @param length_mm Board extent along X, in mm.
    @param width_mm Board extent along Y, in mm.
    @return List of rectangles in millimetres from the board centre.
    @note It tells the edge measurement where NOT to look: the gold pad has
        an outline as sharp as a component's, and without this exclusion a
        good measurement gets replaced by the edge of the neighbouring pad.
        The zone starts from the declared quantities -- half a pitch along
        the edge, the pad depth -- plus the conventional label margin
        (heuristic declared in the module docstring), not a band chosen by
        looking at the photo.
    @note Through-hole matrices stay out: there a connector sits right on
        top of its pins, and masking the zone would prevent measuring it.
    """
    rects = []
    for contact in positions or ():
        edge = contact.get('edge')
        pitch = positive_number(contact.get('pitch_mm'))
        depth = positive_number(contact.get('depth_mm')) or pitch
        if edge not in ('left', 'right', 'top', 'bottom') or pitch is None:
            continue
        reach = pitch * _BAND_TOLERANCE
        x, y = contact['x_mm'], contact['y_mm']
        if edge == 'left':
            rects.append((x, y - reach, x + depth, y + reach))
        elif edge == 'right':
            rects.append((x - depth, y - reach, x, y + reach))
        elif edge == 'top':
            rects.append((x - reach, y, x + reach, y + depth))
        else:
            rects.append((x - reach, y - depth, x + reach, y))
    return rects


def contact_proposals(proposals, positions, length_mm, width_mm, edge_tolerance_mm=None):
    """@brief Indices of the proposals that cover the copper of a declared contact.

    @param proposals Detector proposals, with a normalized `_box`.
    @param positions Output of contact_positions.
    @param length_mm Board extent along X, in mm.
    @param width_mm Board extent along Y, in mm.
    @param edge_tolerance_mm Measurement uncertainty of an edge on the photo,
        in mm (measurement_uncertainty.edge_uncertainty_mm); None counts as 0.
    @return Set of the indices to discard.
    @note Structural rule (user, 2026-09-27: «regola generale» -- a general
        rule), the same on every board, with no strip depth:
        1. EDGE contact (half-hole, double contact, unobserved style): the
           copper reaches the board edge, so the false box TOUCHES the edge
           -- within the measurement uncertainty plus the known half-hole
           radius -- stays within half a pitch along the edge and is as small
           as the pitch. A body mounted next to the contact does not touch
           the edge there, and stays for the review;
        2. set-back THROUGH-HOLE contact: the box contains (within the
           uncertainty) the centre of the ring and is as small as the pitch;
        3. PIECE OF A ROW: it touches the edge (or contains the rings), covers
           two or more contacts and does not enter the board beyond the known
           copper (`copper_mm`, from the declared quantities): a connector
           mounted on the edge enters much further and stays;
        4. through-hole MATRIX: unchanged, the small box on the pin.
    @note Silkscreen labels next to the contacts are no longer removed here:
        by position they cannot be told apart from a small body mounted right
        there, while the AI inventory discards them as silkscreen.
    """
    if not positions:
        return set()
    tolerance = float(edge_tolerance_mm) if (
        isinstance(edge_tolerance_mm, (int, float)) and not isinstance(edge_tolerance_mm, bool)
        and math.isfinite(edge_tolerance_mm) and edge_tolerance_mm > 0) else 0.0
    inward_of = {'left': (1, 0), 'right': (-1, 0), 'top': (0, 1), 'bottom': (0, -1)}
    found = set()
    for index, proposal in enumerate(proposals):
        box = proposal.get('_box')
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in box)
        left, right = (x0 - 0.5) * length_mm, (x1 - 0.5) * length_mm
        top, bottom = (y0 - 0.5) * width_mm, (y1 - 0.5) * width_mm
        centre_x, centre_y = (left + right) / 2, (top + bottom) / 2
        long_side = max(right - left, bottom - top)
        for contact in positions:
            pitch = contact['pitch_mm']
            edge = contact['edge']
            reach = pitch * _BAND_TOLERANCE
            if edge not in inward_of:
                along = max(abs(centre_x - contact['x_mm']), abs(centre_y - contact['y_mm']))
                if long_side <= pitch and along <= reach:
                    found.add(index)
                    break
                continue
            vertical = edge in ('left', 'right')
            along_key = 'y_mm' if vertical else 'x_mm'
            along = (centre_y if vertical else centre_x) - contact[along_key]
            span = (top, bottom) if vertical else (left, right)
            # Distance of the box side facing the edge from the edge itself,
            # and how far the box enters the board.
            outer = {'left': left - contact['x_mm'], 'right': contact['x_mm'] - right,
                     'top': top - contact['y_mm'], 'bottom': contact['y_mm'] - bottom}[edge]
            reach_in = {'left': right - contact['x_mm'], 'right': contact['x_mm'] - left,
                        'top': bottom - contact['y_mm'], 'bottom': contact['y_mm'] - top}[edge]
            if contact.get('at_edge', True):
                on_copper = outer <= tolerance + (contact.get('edge_radius_mm') or 0.0)
            else:
                dx, dy = inward_of[edge]
                inset = contact.get('ring_inset_mm') or 0.0
                ring_x, ring_y = contact['x_mm'] + dx * inset, contact['y_mm'] + dy * inset
                gap_x = max(left - ring_x, ring_x - right, 0.0)
                gap_y = max(top - ring_y, ring_y - bottom, 0.0)
                on_copper = math.hypot(gap_x, gap_y) <= tolerance
            if not on_copper:
                continue
            if long_side <= pitch and abs(along) <= reach:
                found.add(index)
                break
            copper = contact.get('copper_mm') or 0.0
            if reach_in <= copper + tolerance:
                covered = sum(1 for other in positions
                              if other['edge'] == edge
                              and span[0] <= other[along_key] <= span[1])
                if covered >= 2:
                    found.add(index)
                    break
    return found


def rows_over_edge_contacts(boxes, positions, length_mm, width_mm, edge_tolerance_mm=None):
    """@brief Indexes of boxes that lie along a row of declared EDGE contacts.

    @param boxes Normalized boxes (x0, y0, x1, y1), 0..1 over the board.
    @param positions Output of contact_positions.
    @param length_mm Board extent along X.
    @param width_mm Board extent along Y.
    @param edge_tolerance_mm Edge measurement uncertainty of the photo, in mm.
    @return Set of indexes whose box touches the board edge at a declared edge
        contact (within the uncertainty plus the known half-hole radius) and
        spans two or more contacts of that edge.
    @note No depth limit, on purpose. contact_proposals removes a detector box
        only when it stays within the known copper; beyond it the box may be a
        connector mounted on the edge. For a body the model added WITHOUT any
        detector box, the depth is the model's own guess and cannot separate a
        real edge connector from the row of pads it was told not to count
        (HANDOFF 126, G4: two «connectors» 4.5 and 3 mm deep over bare
        castellations). The caller turns this into a doubt, never a deletion.
    @note Through-hole contacts are exempt: a header soldered in them is a
        legitimate body over the contacts.
    """
    tolerance = float(edge_tolerance_mm) if (
        isinstance(edge_tolerance_mm, (int, float)) and not isinstance(edge_tolerance_mm, bool)
        and math.isfinite(edge_tolerance_mm) and edge_tolerance_mm > 0) else 0.0
    edge_contacts = [c for c in positions or ()
                     if c.get('edge') in ('left', 'right', 'top', 'bottom')
                     and c.get('at_edge', True)]
    found = set()
    for index, box in enumerate(boxes):
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in box)
        left, right = (x0 - 0.5) * length_mm, (x1 - 0.5) * length_mm
        top, bottom = (y0 - 0.5) * width_mm, (y1 - 0.5) * width_mm
        for edge in ('left', 'right', 'top', 'bottom'):
            row = [c for c in edge_contacts if c['edge'] == edge]
            if len(row) < 2:
                continue
            vertical = edge in ('left', 'right')
            span = (top, bottom) if vertical else (left, right)
            covered = [c for c in row if span[0] <= c['y_mm' if vertical else 'x_mm'] <= span[1]]
            if len(covered) < 2:
                continue
            reach = max(c.get('edge_radius_mm') or 0.0 for c in covered) + tolerance
            line = covered[0]['x_mm' if vertical else 'y_mm']
            outer = {'left': left - line, 'right': line - right,
                     'top': top - line, 'bottom': line - bottom}[edge]
            if outer <= reach:
                found.add(index)
                break
    return found
