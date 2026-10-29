"""@brief Build mounted component geometry inside headless FreeCAD.

It cannot be imported by ordinary code: freecad_build_body.py (same
folder) loads it while running inside FreeCAD's embedded interpreter,
where ``FreeCAD`` and ``Part`` exist.

A module on a PCB is not recognised by its boxes: it is recognised
because the SOIC has "L" shaped leads on its sides, the transformer has
rounded edges, the LED has a lens, the tantalum has its polarity stripe.
Every ``kind`` below builds that shape from the envelope alone (length x
width x height) estimated from the photograph: the details are not
dimensioned from a datasheet and do not claim to be, they serve to make
the part readable at a glance in KiCad's 3D viewer.

Every builder returns a list of (shape, color_hex): the pieces stay
separate so that each can have its own colour in STEP and VRML.
"""

import json
import math
import numbers
import os
import sys
import threading
import time

import FreeCAD
import Part


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


class _CheckFailed(ValueError):
    """@brief A validation written in this script failed.

    @note Its English text is a diagnostic for developers only and never
        reaches a warning: the catching site recognises the class and sends a
        dedicated Italian template of its own (`_warn`), which the parent
        translates in full. Embedding the English text in a translated
        template showed users of every language a half-English sentence.
        Errors of other classes (FreeCAD/OCC, I/O) carry text this script did
        not write and still travel as a value.
    @note `code` is a stable identifier of the failed check: the catching
        site picks the template from it, so the user keeps the detail of
        which check failed without reading English.
    """

    def __init__(self, code, message):
        """@brief Store the stable check code next to the English diagnostic.

        @param code Stable identifier of the failed check.
        @param message English developer diagnostic, returned by str().
        """
        super().__init__(message)
        self.code = code


V = FreeCAD.Vector

METAL = "#c8c8c8"
#: Lead pitch of the SOIC family (JEDEC MS-012/MS-013). Used ONLY
#: when the lead count is not declared: then the count is an
#: ESTIMATE from the body length (see `_soic_leads`) and the log says so.
LEAD_PITCH_MM = 1.27


def _box(cx, cy, z0, length, width, height):
    """Box centred at (cx, cy) with its base at z0."""
    return Part.makeBox(length, width, height, V(cx - length / 2, cy - width / 2, z0))


def _fillet(shape, radius, edges):
    """makeFillet that does not fail the generation: an impossible fillet
    (radius too large for the piece) leaves the edge sharp."""
    if radius <= 0 or not edges:
        return shape
    try:
        return shape.makeFillet(radius, edges)
    except Exception:  # noqa: BLE001 - sharp edge, not fatal
        return shape


def _vertical_edges(shape):
    return _edges_along(shape, 2)


def _edges_along(shape, axis):
    """Edges parallel to the given axis (0=X, 1=Y, 2=Z).

    Filleting them transforms the section perpendicular to that axis: on a
    prism with a rectangular base and a radius of half the short side the
    section becomes a stadium, which is the shape of a USB mouth.
    """
    selected = []
    for edge in shape.Edges:
        box = edge.BoundBox
        spans = (box.XLength, box.YLength, box.ZLength)
        others = max(spans[(axis + 1) % 3], spans[(axis + 2) % 3])
        if spans[axis] > others + 1e-6:
            selected.append(edge)
    return selected


def _fillet_best(shape, radius, edges):
    """Fillet that falls back to a smaller radius instead of a sharp edge.

    A radius of exactly half the short side is the degenerate case: the two
    blends meet tangentially and OCC rejects the operation. With `_fillet`
    the result was a sharp edge, i.e. a square hole instead of the slot -- a
    silent failure in precisely the case that matters most. Here the radius
    is scaled down until the fillet succeeds.
    """
    for factor in (1.0, 0.96, 0.9, 0.8, 0.65, 0.5):
        candidate = _fillet(shape, radius * factor, edges)
        if candidate is not shape:
            return candidate
    return shape


def _upper_edges_along(shape, axis):
    """Only the two long edges at the top.

    The shell of a USB has a flat bottom -- it rests on the board -- and
    strongly rounded upper edges. Filleting the lower ones too would make it
    look like a floating tube.
    """
    ztop = shape.BoundBox.ZMax
    return [e for e in _edges_along(shape, axis)
            if abs(e.BoundBox.ZMax - ztop) < 1e-6]


def _top_edges(shape):
    ztop = shape.BoundBox.ZMax
    return [
        e for e in shape.Edges
        if all(abs(v.Point.z - ztop) < 1e-6 for v in e.Vertexes)
    ]


def _rounded_box(cx, cy, z0, length, width, height, color, ratio=0.15, cap=0.6):
    """Box with filleted vertical and top edges (moulded cases, transformers)."""
    shape = _box(cx, cy, z0, length, width, height)
    radius = min(cap, ratio * min(length, width, height))
    shape = _fillet(shape, radius, _vertical_edges(shape))
    shape = _fillet(shape, radius * 0.8, _top_edges(shape))
    return [(shape, color)]


def _generic(cx, cy, z0, length, width, height, color):
    return [(_box(cx, cy, z0, length, width, height), color)]


def _normalized_box(value):
    """Validate a local image rectangle without clipping or guessing coordinates."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise _CheckFailed("bbox_not_four", "terminal_geometry bbox must contain four coordinates")
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or not 0 <= v <= 1 for v in value):
        raise _CheckFailed("bbox_out_of_range",
                           "terminal_geometry bbox coordinates must be finite numbers in [0, 1]")
    left, top, right, bottom = value
    if left >= right or top >= bottom:
        raise _CheckFailed("bbox_empty", "terminal_geometry bbox must have positive area")
    return tuple(value)


def _warn_lead_detail(reason, name, terminal):
    """@brief Warn that a photo lead detail was ignored, with its reason translated.

    @param reason `lead_geometry.LeadDetailRejected.reason`, or None for a
        detail the validator could not even read (a TypeError).
    @param name Component name, for the user.
    @param terminal 1-based position of the terminal in the observation.
    @note One literal template per reason: the parent translates whole
        sentences only (`messages.child_warning_text`), so a reason passed as
        a value would stay in the language it was written in. The reasons
        mirror `services.photo_leads._rejection_text`.
    """
    if reason == "invalid_rectangle":
        _warn("componente {get}, terminale {terminal}: dettaglio reoforo ignorato (rettangolo "
              "del reoforo non valido); terminale originale conservato",
              get=name, terminal=terminal)
    elif reason == "empty_rectangle":
        _warn("componente {get}, terminale {terminal}: dettaglio reoforo ignorato (rettangolo "
              "del reoforo vuoto); terminale originale conservato",
              get=name, terminal=terminal)
    elif reason == "no_evidence":
        _warn("componente {get}, terminale {terminal}: dettaglio reoforo ignorato "
              "(separazione senza evidenza esplicita ad alta confidenza); terminale "
              "originale conservato", get=name, terminal=terminal)
    elif reason == "exceeds_envelope":
        _warn("componente {get}, terminale {terminal}: dettaglio reoforo ignorato (reoforo "
              "oltre l'ingombro del terminale originale); terminale originale conservato",
              get=name, terminal=terminal)
    elif reason == "not_one_face":
        _warn("componente {get}, terminale {terminal}: dettaglio reoforo ignorato (il reoforo "
              "deve toccare esattamente una faccia del corpo); terminale originale conservato",
              get=name, terminal=terminal)
    else:
        _warn("componente {get}, terminale {terminal}: dettaglio reoforo ignorato (dettaglio "
              "malformato); terminale originale conservato",
              get=name, terminal=terminal)


def _validated_terminal_geometry(geometry, name=None):
    """@brief Validate the complete observation before constructing any CAD shapes.

    @param geometry The `terminal_geometry` observation.
    @param name Component name, used only to say which part a warning is about.
    @return (body box, terminal boxes), normalized.
    @throws _CheckFailed With a stable `code` when the observation is malformed.
    @note An invalid lead detail does not fail the observation: the terminal
        keeps its original envelope and a warning names component, terminal
        (1-based position) and reason.
    """
    if not isinstance(geometry, dict):
        raise _CheckFailed("not_object", "terminal_geometry must be an object")
    if geometry.get("confidence") not in ("high", "medium"):
        raise _CheckFailed("low_confidence", "terminal_geometry confidence must be high or medium")
    body = _normalized_box(geometry.get("body_bbox"))
    terminals = geometry.get("terminals")
    if not isinstance(terminals, list) or len(terminals) > 128:
        raise _CheckFailed("terminals_not_list",
                           "terminal_geometry terminals must be a list of at most 128 boxes")
    boxes = []
    for position, terminal in enumerate(terminals, start=1):
        if not isinstance(terminal, dict):
            raise _CheckFailed("terminal_not_object",
                               "terminal_geometry terminal must be an object with bbox")
        envelope = _normalized_box(terminal.get("bbox"))
        detail = terminal.get('lead_detail')
        if detail is not None:
            try:
                envelope = _lead_geometry_module().validated_lead_bbox(body, envelope, detail)
            except (ValueError,TypeError) as exc:
                # lead_geometry's own validation: its reason code, never its
                # English text, reaches the user (see `_CheckFailed`).
                _warn_lead_detail(getattr(exc, "reason", None), name, position)
        boxes.append(envelope)
    return body, boxes


def _lead_geometry_module():
    """@brief Import the shared lead constraints from the package or from this folder.

    @return The `lead_geometry` module.
    @note Inside the embedded FreeCAD interpreter this folder is on sys.path
        but the `kicadgen` package is not; from pytest it is the opposite.
    """
    try:
        from kicadgen.cad3d import lead_geometry
    except ModuleNotFoundError:
        import lead_geometry
    return lead_geometry


def _terminal_attachment(geometry, body_bbox, terminal_boxes, lead_geometry, name=None):
    """@brief Attachment decision of each observed terminal, as taken by the photo decoder.

    @param geometry The `terminal_geometry` observation, already validated.
    @param body_bbox Normalized body rectangle (package-local frame).
    @param terminal_boxes Normalized terminal rectangles, in payload order.
    @param lead_geometry The shared `lead_geometry` module.
    @param name Component name, used only to say which part a warning is about.
    @return One bool per terminal: True when the lead must be anchored to the body.
    @note `attached: False` written by `photo_terminals.decode_terminal_geometry`
        is authoritative. `attached: True` is honoured only when the box is
        also within the shared tolerance (`is_attached` plus
        `ATTACH_FRAME_EPS`): the decoder never writes True beyond it, so a
        True there comes from a hand-edited or foreign payload, and honouring
        it would invent metal across the whole gap (R2T4); such a terminal is
        drawn as observed and a warning says why. A terminal without a
        boolean flag (a payload cached before the flag existed) is decided
        here, once, with the same tolerance; `ATTACH_FRAME_EPS` only absorbs
        the rounding of the 1 - x rotation applied after the decoder's check.
    @note A terminal carrying the decoder's `attachment_doubt` (the model saw
        it attached, the photo shows a gap beyond the tolerance) is drawn as
        observed and the doubt is repeated as a warning at each generation,
        so it reaches the user also on a project reopened later.
    """
    gap_max = lead_geometry.TERMINAL_ATTACH_GAP_MAX + lead_geometry.ATTACH_FRAME_EPS
    result = []
    for position, (terminal, bbox) in enumerate(zip(geometry["terminals"], terminal_boxes), 1):
        flag = terminal.get("attached")
        near = lead_geometry.is_attached(body_bbox, bbox, gap_max)
        if flag is True and not near:
            _warn("componente {get}, terminale {terminal}: indicato come attaccato ma lontano "
                  "dal corpo oltre la tolleranza; disegnato staccato, nessun metallo inventato",
                  get=name, terminal=position)
            result.append(False)
        elif type(flag) is bool:
            result.append(flag)
        else:
            result.append(near)
        if terminal.get("attachment_doubt") == "attached_with_gap":
            _warn("componente {get}, terminale {terminal}: il modello vede il reoforo attaccato "
                  "al corpo ma la foto mostra uno spazio oltre la tolleranza; disegnato "
                  "staccato, da verificare", get=name, terminal=position)
    return result


def _observed_terminals(cx, cy, z0, length, width, height, color, geometry, name=None):
    """@brief Render observed boxes in the existing total envelope, with no pin inference.

    @param cx, cy Component centre in CAD coordinates (Y up), mm.
    @param z0 Seating plane, mm.
    @param length, width, height Local envelope, mm.
    @param color Body colour (hex).
    @param geometry The `terminal_geometry` observation (normalized, X right / Y down).
    @param name Component name, for warnings only.
    @return List of (shape, colour): the body first, then one fused foot+riser
        for each attached terminal, or a lone foot for each detached terminal.
    @note Rectangles use local X-right/Y-down coordinates. The caller has
        already converted the component centre to CAD Y-up and rotates all
        returned shapes once. Only the local Y offset is inverted here; axes are
        never swapped based on aspect ratio. Heights and small risers are
        conservative visual estimates.
    @note Attachment invariant (MEDIO 16, R2T1): a terminal drawn attached
        has a riser sharing a FACE (or a volume) with the body, whatever the
        package, side or rotation; an edge-only contact is never drawn. The
        decision is the decoder's own, carried by each terminal's `attached`
        flag, and is not re-taken here (re-evaluating the tolerance on boxes
        already rotated by 1 - x drew islands at 180/270 degrees for a gap
        exactly at the threshold), except that a True beyond the tolerance is
        refused (see `_terminal_attachment`). The lead is anchored by
        `lead_geometry.face_contact_anchor`: the face it protrudes from most
        first, then the other faces it protrudes from; it is only enlarged
        toward the body, so its outer end stays where the photograph measured
        it. A terminal not attached gets a foot only: no bridge is invented.
    @note Neighbour invariant (R2T3): neither the lateral lengthening nor the
        closure along the face normal enters another lead, so anchoring never
        makes two feet intersect, also for two leads in a row along the
        normal. Leads are processed in order and each one is checked against
        the final rectangle of the leads already drawn and the observed
        rectangle of the others. A lateral growth cut short by a neighbour
        keeps the riser in the strip of the foot that faces the body. When no
        face can be reached without entering another lead, the terminal is
        drawn as observed (foot only) and a warning says so: never a lead
        accepted as attached that floats.
    """
    body_bbox, terminal_boxes = _validated_terminal_geometry(geometry, name)
    lead_geometry = _lead_geometry_module()
    attachment = _terminal_attachment(geometry, body_bbox, terminal_boxes, lead_geometry, name)

    def bounds(bbox):
        left, top, right, bottom = bbox
        return (cx + (left - 0.5) * length, cy + (0.5 - bottom) * width,
                cx + (right - 0.5) * length, cy + (0.5 - top) * width)

    def box_at(rect, base, thickness):
        left, bottom, right, top = rect
        return Part.makeBox(right - left, top - bottom, thickness,
                            V(left, bottom, base))

    body_bounds = bounds(body_bbox)
    standoff = min(0.12, height * 0.1)
    lead_t = min(0.08, height * 0.05)
    riser_h = min(height * 0.3, standoff + 0.1)
    parts = [(box_at(body_bounds, z0 + standoff, height - standoff), color)]
    # Final (possibly anchored) rectangles in mm, updated as leads are drawn,
    # so that every lateral growth is clamped against its neighbours.
    drawn = [bounds(bbox) for bbox in terminal_boxes]
    for index, attached in enumerate(attachment):
        rect = drawn[index]
        if not attached:
            # Island beyond the shared tolerance: the observed foot alone.
            parts.append((box_at(rect, z0, lead_t), METAL))
            continue
        # Riser footprint: a small area of the observed terminal, in mm.
        riser_size = (min(0.12, (rect[2] - rect[0]) * 0.5),
                      min(0.12, (rect[3] - rect[1]) * 0.5))
        if lead_geometry.anchor_face(body_bounds, rect) is None:
            # Inside the body outline: the riser at the terminal centre
            # already penetrates the body volume.
            riser = [(rect[i] + rect[i + 2]) / 2 - riser_size[i] / 2 for i in range(2)]
            riser = (riser[0], riser[1], riser[0] + riser_size[0], riser[1] + riser_size[1])
        else:
            others = drawn[:index] + drawn[index + 1:]
            anchor = lead_geometry.face_contact_anchor(body_bounds, rect, riser_size, others)
            if anchor is None:
                # No face reachable without entering another lead: the
                # observed foot alone, and the user is told (R2T1/R2T3).
                _warn("componente {get}, terminale {terminal}: reoforo attaccato senza spazio "
                      "per toccare una faccia del corpo senza entrare in un altro reoforo; "
                      "disegnato come in foto, staccato, da verificare",
                      get=name, terminal=index + 1)
                parts.append((box_at(rect, z0, lead_t), METAL))
                continue
            (axis, sign), rect = anchor
            drawn[index] = rect
            # Normal axis: the riser straddles the face, kept inside the foot.
            plane = body_bounds[axis if sign < 0 else axis + 2]
            n_lo = min(max(plane - riser_size[axis] / 2, rect[axis]),
                       rect[axis + 2] - riser_size[axis])
            # Lateral axis: inside the overlap between foot and face span,
            # positive by construction (face_contact_anchor); a neighbour may
            # have made it narrower than the riser footprint.
            lat = 1 - axis
            l_lo = max(rect[lat], body_bounds[lat])
            l_hi = min(rect[lat + 2], body_bounds[lat + 2])
            l_size = min(riser_size[lat], l_hi - l_lo)
            riser = [0, 0, 0, 0]
            riser[axis], riser[axis + 2] = n_lo, n_lo + riser_size[axis]
            riser[lat], riser[lat + 2] = l_lo, l_lo + l_size
        foot = box_at(rect, z0, lead_t)
        foot = foot.fuse(box_at(tuple(riser), z0, riser_h))
        parts.append((foot, METAL))
    return parts


def _soic_leads(span, lead_count=None):
    """@brief Leads on each side and pitch of a gull-wing body along its lead side.

    @param span Body length along the side carrying the leads, mm.
    @param lead_count Declared/observed TOTAL lead count (both sides), or None.
    @return (leads_per_side, pitch_mm, estimated) -- estimated is True when the
        count was not declared and comes from the body length.
    @note A declared even count >= 4 wins: the pitch is then span / leads_per_side,
        because a gull-wing body is about one pitch longer than the span of
        its lead centres (SOIC-8: 4 x 1.27 = 5.08 against 4.9 mm; TSSOP-20:
        10 x 0.65 = 6.5 against 6.5 mm). Without a count the SOIC pitch is
        assumed and the count follows from the same relation, uncapped (a
        wide SOIC-28 has 14 on each side, a long body more): an ESTIMATE, which
        the caller reports.
    """
    if (type(lead_count) is int and lead_count >= 4 and lead_count % 2 == 0
            and span > 0):
        per_side = lead_count // 2
        return per_side, span / per_side, False
    per_side = max(2, int(round(span / LEAD_PITCH_MM)))
    return per_side, LEAD_PITCH_MM, True


def _ic_soic(cx, cy, z0, length, width, height, color, marker=None, lead_count=None):
    """IC with gull-wing leads on the two long sides.

    `marker` is the local direction (Y up) of a pin-1 landmark OBSERVED in
    the photograph; without it the body stays plain.

    The body is lifted by a standoff and narrower than the total envelope,
    because the envelope read from the photograph includes the leads. The
    lead count and pitch come from the declared count (`lead_count`, total
    of the two sides) when there is one; otherwise they are an estimate from
    the body length with the SOIC pitch (see `_soic_leads`), and the caller
    reports it.
    """
    parts = []
    along_x = length >= width
    span = length if along_x else width      # side with the leads
    depth = width if along_x else length     # side without leads
    lead_out = min(0.9, depth * 0.18)        # lead protrusion from the body
    body_depth = depth - 2 * lead_out
    standoff = min(0.12, height * 0.1)
    body_h = height - standoff
    body = _box(cx, cy, z0 + standoff, span if along_x else body_depth,
                body_depth if along_x else span, body_h)
    body = _fillet(body, min(0.15, body_h * 0.15), _top_edges(body))
    # Pin-1 dimple only where the photograph showed a pin-1/polarity mark: a
    # corner chosen here would be an invented orientation (E10).
    body = _cut_marker(body, marker, cx, cy, span if along_x else body_depth,
                       body_depth if along_x else span, z0 + height)
    parts.append((body, color))

    n_leads, pitch, _ = _soic_leads(span, lead_count)
    # Lead width proportional to the pitch (0.42 mm at 1.27, as
    # before): at a fine pitch wide leads would merge into each other.
    lead_w = min(0.42, pitch * 0.35)
    lead_t = 0.22
    start = -(n_leads - 1) * pitch / 2
    riser_h = max(0.2, body_h * 0.55)
    for i in range(n_leads):
        along = start + i * pitch
        for side in (-1, 1):
            edge = side * (body_depth / 2)
            if along_x:
                foot = Part.makeBox(lead_w, lead_out, lead_t,
                                    V(cx + along - lead_w / 2,
                                      cy + (edge if side > 0 else edge - lead_out), z0))
                riser = Part.makeBox(lead_w, lead_t, riser_h + standoff,
                                     V(cx + along - lead_w / 2,
                                       cy + (edge - lead_t if side > 0 else edge), z0))
            else:
                foot = Part.makeBox(lead_out, lead_w, lead_t,
                                    V(cx + (edge if side > 0 else edge - lead_out),
                                      cy + along - lead_w / 2, z0))
                riser = Part.makeBox(lead_t, lead_w, riser_h + standoff,
                                     V(cx + (edge - lead_t if side > 0 else edge),
                                       cy + along - lead_w / 2, z0))
            parts.append((foot.fuse(riser), METAL))
    return parts


def _ic_qfn(cx, cy, z0, length, width, height, color, marker=None):
    """@brief Leadless IC body; a pin-1 dot only on an observed landmark side.

    @param marker Local (dx, dy) of an observed pin-1/polarity mark, or None.
    @return List of (shape, colour).
    """
    body = _box(cx, cy, z0, length, width, height)
    return [(_cut_marker(body, marker, cx, cy, length, width, z0 + height), color)]


def _cut_marker(body, marker, cx, cy, length, width, ztop):
    """@brief Sink a small pin-1 dimple towards an OBSERVED landmark side.

    @param body Solid to mark.
    @param marker Local unit direction (dx, dy), Y up, or None for no mark.
    @param cx Centre X of the marked face, mm.
    @param cy Centre Y of the marked face, mm.
    @param length Face extent along local X, mm.
    @param width Face extent along local Y, mm.
    @param ztop Z of the top face, mm.
    @return The marked solid, or @p body unchanged when there is no evidence
        or the dimple does not fit.
    @note A single observed side cannot name a corner, so the dimple is centred
        on that side instead of guessing one: the mark says "this end", which
        is all the photograph established.
    """
    if not marker:
        return body
    dx, dy = marker
    radius = min(0.35, 0.08 * min(length, width))
    reach = (length if dx else width) / 2 - 2.5 * radius
    if radius < 0.05 or reach <= 0:
        return body
    dimple = Part.makeCylinder(radius, 0.3, V(cx + dx * reach, cy + dy * reach, ztop - 0.12),
                               V(0, 0, 1))
    try:
        return body.cut(dimple)
    except Exception:  # noqa: BLE001 - no notch
        return body


def _end_marker(marker, along_x):
    """@brief Sign (+1/-1) of an observed marker lying on the long axis, or None.

    @param marker Local unit direction (dx, dy) or None.
    @param along_x True when the long axis is local X.
    @return +1 or -1 when the mark sits on one END of the part; None when there
        is no mark or it sits on a long side, where a band or stripe cannot be.
    """
    if not marker:
        return None
    value = marker[0] if along_x else marker[1]
    return (1 if value > 0 else -1) if value else None


def _end_strip(cx, cy, along_x, cross, offset, strip, ztop, thickness=0.03):
    """Thin strip across the part, centred `offset` from the centre on the long axis."""
    if along_x:
        return Part.makeBox(strip, cross, thickness,
                            V(cx + offset - strip / 2, cy - cross / 2, ztop))
    return Part.makeBox(cross, strip, thickness,
                        V(cx - cross / 2, cy + offset - strip / 2, ztop))


def _ic_observed_pins(cx, cy, z0, length, width, height, color, kind, pin_count=None,
                      pins_by_side=None, marker=None):
    """@brief IC body plus terminals counted from the photo, inside the envelope.

    @param cx Component centre X in the model frame (Y up), mm.
    @param cy Component centre Y in the model frame, mm.
    @param z0 Seating plane, mm.
    @param length Photographed envelope along local X, mm (leads included).
    @param width Photographed envelope along local Y, mm (leads included).
    @param height Estimated height, mm.
    @param color Body colour.
    @param kind "ic_soic" or "ic_qfn": only used to spread a bare pin_count.
    @param pin_count Observed total count, used when pins_by_side is absent.
    @param pins_by_side Observed counts per image side (left/right/top/bottom).
    @param marker Optional local (dx, dy) of an OBSERVED pin-1 landmark.
    @return List of (shape, colour).
    @note Side names are image sides (Y down). The caller has already negated
        Y and the rotation, so the image "top" is local +Y here, exactly as
        `_observed_terminals` inverts its local Y. Placing "top" pads at -Y
        put them on the opposite side of the body.
    @note The photographed rectangle already includes the leads (as for
        `_ic_soic`): the body shrinks on each side that carries leads and the
        pads stay inside length x width instead of growing the envelope.
    """
    sides = dict(pins_by_side or {})
    if not sides and pin_count:
        if kind == "ic_qfn":
            base = pin_count // 4
            sides = {s: base for s in ("left", "right", "top", "bottom")}
            for side in ("left", "right", "top", "bottom")[:pin_count % 4]:
                sides[side] += 1
        else:
            left = pin_count // 2
            sides = {"left": left, "right": pin_count - left}
    counts = {side: max(0, int(sides.get(side, 0) or 0))
              for side in ("left", "right", "top", "bottom")}
    lead_t = min(0.18, max(0.08, height * 0.12))
    # Lead projection is a visual estimate; capped at a quarter of the short
    # side so that two opposite rows always leave half of the body.
    lead_out = min(0.8, max(0.3, min(length, width) * 0.16), 0.25 * min(length, width))
    metal_w = min(0.42, max(0.18, min(length, width) / 8))
    x_min, x_max = cx - length / 2, cx + length / 2
    y_min, y_max = cy - width / 2, cy + width / 2
    body_left = x_min + (lead_out if counts["left"] else 0.0)
    body_right = x_max - (lead_out if counts["right"] else 0.0)
    # Image "top" is local +Y in this Y-up frame (see the note above).
    body_top = y_max - (lead_out if counts["top"] else 0.0)
    body_bottom = y_min + (lead_out if counts["bottom"] else 0.0)
    body = Part.makeBox(body_right - body_left, body_top - body_bottom, height,
                        V(body_left, body_bottom, z0))
    body = _cut_marker(body, marker, (body_left + body_right) / 2,
                       (body_bottom + body_top) / 2, body_right - body_left,
                       body_top - body_bottom, z0 + height)
    parts = [(body, color)]
    metal_w = min(metal_w, 0.8 * (body_top - body_bottom), 0.8 * (body_right - body_left))
    for side, count in counts.items():
        if count <= 0:
            continue
        vertical = side in ("left", "right")
        low, high = (body_bottom, body_top) if vertical else (body_left, body_right)
        pitch = (high - low) / (count + 1)
        for index in range(count):
            along = low + (index + 1) * pitch
            if side == "left":
                origin = V(x_min, along - metal_w / 2, z0)
            elif side == "right":
                origin = V(body_right, along - metal_w / 2, z0)
            elif side == "top":
                origin = V(along - metal_w / 2, body_top, z0)
            else:
                origin = V(along - metal_w / 2, y_min, z0)
            size = (lead_out, metal_w) if vertical else (metal_w, lead_out)
            parts.append((Part.makeBox(size[0], size[1], lead_t, origin), METAL))
    return parts


def _led(cx, cy, z0, length, width, height, color):
    """SMD LED: light base and dome lens in the LED's colour."""
    base_h = max(0.15, height * 0.4)
    base = _box(cx, cy, z0, length, width, base_h)
    parts = [(base, "#e6e6e0")]
    r = min(length, width) * 0.45
    if r > 0.05:
        centre_z = z0 + base_h
        sphere = Part.makeSphere(r, V(cx, cy, centre_z))
        clip = Part.makeBox(length, width, height - base_h + 1.0,
                            V(cx - length / 2, cy - width / 2, centre_z))
        try:
            dome = sphere.common(clip)
            if dome.Volume > 0:
                parts.append((dome, color if color else "#f2f2c8"))
        except Exception:  # noqa: BLE001 - LED without lens
            pass
    return parts


def _tantalum(cx, cy, z0, length, width, height, color, marker=None):
    """@brief Tantalum capacitor: rounded case, stripe only where observed.

    @param marker Local (dx, dy) of an observed polarity mark, or None.
    @return List of (shape, colour).
    @note The stripe used to sit at +X on every part: a polarity invented by
        the builder. It is now drawn only at the end the photograph marked.
    """
    parts = _rounded_box(cx, cy, z0, length, width, height, color, ratio=0.12, cap=0.35)
    along_x = length >= width
    span, cross = (length, width) if along_x else (width, length)
    end = _end_marker(marker, along_x)
    if end is not None:
        stripe_len = min(max(0.2, span * 0.14), span * 0.3)
        offset = end * (span / 2 - stripe_len / 2 - min(0.05, span * 0.05))
        parts.append((_end_strip(cx, cy, along_x, cross * 0.9, offset, stripe_len,
                                 z0 + height), "#f0e6c8"))
    return parts


def _chip_cap(span):
    """Termination length: visible on small parts, never more than 30% of the span."""
    return min(max(0.15, span * 0.2), span * 0.3)


def _chip(cx, cy, z0, length, width, height, color):
    """@brief Chip passive: body plus metal terminations on the two SHORT ends.

    @return List of (shape, colour).
    @note The terminations follow the long side, whichever local axis it is
        (as `_ic_soic` and `_diode_melf` already do): a chip whose length was
        read along local Y used to get its caps on the long sides.
    @note The termination is capped at 30% of the long side, so a tiny chip
        keeps a positive body instead of raising (a 0.25 mm part used to make
        `makeBox` fail and lose its terminations).
    """
    along_x = length >= width
    span, cross = (length, width) if along_x else (width, length)
    cap = _chip_cap(span)
    core = span - 2 * cap + min(0.02, span * 0.05)
    parts = [(_box(cx, cy, z0, core if along_x else cross, cross if along_x else core,
                   height), color)]
    for side in (-1, 1):
        offset = side * (span / 2 - cap / 2)
        if along_x:
            parts.append((_box(cx + offset, cy, z0, cap, cross, height), METAL))
        else:
            parts.append((_box(cx, cy + offset, z0, cross, cap, height), METAL))
    return parts


def _diode(cx, cy, z0, length, width, height, color, marker=None):
    """@brief SMD diode: chip body; cathode band only on the observed end.

    @param marker Local (dx, dy) of an observed polarity mark, or None.
    @return List of (shape, colour).
    """
    parts = _chip(cx, cy, z0, length, width, height, color or "#1c1c1c")
    along_x = length >= width
    span, cross = (length, width) if along_x else (width, length)
    end = _end_marker(marker, along_x)
    if end is not None:
        band_w = min(max(0.15, span * 0.08), span * 0.15)
        offset = end * max(0.0, span / 2 - _chip_cap(span) - band_w)
        parts.append((_end_strip(cx, cy, along_x, cross, offset, band_w, z0 + height),
                      "#e8e8e8"))
    return parts


def _diode_melf(cx, cy, z0, length, width, height, color, marker=None):
    """MELF diode: glass cylinder lying along X, metal caps and band.

    The diameter is the smaller dimension of the envelope; the declared
    height does not count, a lying cylinder is as tall as its diameter. The
    cathode band is drawn only from the end on which the photograph saw it
    (`marker`): always placing it at the same end invented the polarity.
    """
    along_x = length >= width
    span = length if along_x else width
    radius = min(width if along_x else length, height) / 2
    zc = z0 + radius
    axis = V(1, 0, 0) if along_x else V(0, 1, 0)
    start = V(cx - span / 2, cy, zc) if along_x else V(cx, cy - span / 2, zc)
    glass = Part.makeCylinder(radius * 0.96, span, start, axis)
    parts = [(glass, color or "#b01818")]
    cap = max(0.25, span * 0.2)
    for offset in (0.0, span - cap):
        base = V(cx - span / 2 + offset, cy, zc) if along_x else V(cx, cy - span / 2 + offset, zc)
        parts.append((Part.makeCylinder(radius, cap, base, axis), METAL))
    end = _end_marker(marker, along_x)
    if end is None:
        return parts
    band_w = max(0.12, span * 0.08)
    band_at = cap + span * 0.08
    if end > 0:
        band_at = span - band_at - band_w
    base = V(cx - span / 2 + band_at, cy, zc) if along_x else V(cx, cy - span / 2 + band_at, zc)
    parts.append((Part.makeCylinder(radius * 0.98, band_w, base, axis), "#1c1c1c"))
    return parts


def _coaxial_connector(cx, cy, z0, length, width, height, color):
    """Vertical coaxial: square base plus the mating barrel.

    It is the shape of the U.FL/IPEX antenna connector that sits on the edge
    of WiFi modules. The cable enters from the top, not from the side:
    giving it the side mouth of the other connectors made it unrecognisable
    and, on top of that, opened the shell in the wrong direction.

    The barrel is metal regardless of the colour read from the photograph:
    on such a small part the sampled colour is almost always that of the
    surrounding PCB.
    """
    base_height = height * 0.55
    base = _box(cx, cy, z0, length, width, base_height)
    base = _fillet(base, min(length, width) * 0.12, _vertical_edges(base))
    parts = [(base, color)]
    radius = min(length, width) * 0.34
    barrel = height - base_height
    if radius <= 0.05 or barrel <= 0.05:
        return [(_box(cx, cy, z0, length, width, height), color)]
    ring = Part.makeCylinder(radius, barrel, V(cx, cy, z0 + base_height))
    # Central cavity: the spring contact is visible inside the ring.
    try:
        ring = ring.cut(Part.makeCylinder(radius * 0.55, barrel,
                                          V(cx, cy, z0 + base_height + barrel * 0.35)))
    except Exception:  # noqa: BLE001 - solid ring, not fatal
        pass
    parts.append((ring, METAL))
    return parts


def _outward_sign(cx, cy, axis, rotation_deg, centre=(0.0, 0.0)):
    """Local verse that, AFTER the caller's rotation, faces away from the board.

    @param cx Component centre X, mm, in the unrotated model frame.
    @param cy Component centre Y, mm, in the same frame.
    @param axis Mating axis: 0 for local X, 1 for local Y.
    @param rotation_deg Rotation the caller applies afterwards, degrees.
    @param centre (x, y) of the board centre in the same system as
        `cx`/`cy`. The default (0, 0) only holds for boards centred on the
        origin (castellated modules); a board whose origin is on pin 1 of a
        connector (matrix generator) has its centre elsewhere, and measuring
        from the origin made edge connectors face inward.
    @return -1.0 or +1.0 along the local mating axis.

    The builder works in the unrotated local system: it is the caller that
    rotates the shape by `rotation_deg` about the component centre. If the
    verse were decided from the global position alone, a rotation of 180
    degrees would flip the mouth toward the inside of the board -- where
    the pins are, not the cable.

    Here the component position is projected on the mating axis as it will
    appear AFTER the rotation: the sign of that projection is the local
    verse to build.
    """
    angle = math.radians(rotation_deg or 0)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    rx, ry = cx - float(centre[0]), cy - float(centre[1])
    # Global image of the local X and Y axes after the rotation.
    projection = (rx * cos_a + ry * sin_a) if axis == 0 else (-rx * sin_a + ry * cos_a)
    return -1.0 if projection < 0 else 1.0


#: Observed references that tell from which side a connector mates:
#: the mouth itself or the side the cable exits from.
_MOUTH_FEATURES = ("connector_opening", "cable_exit")


def _observed_mouth_side(component):
    """@brief Photo-frame side of an OBSERVED connector mouth, or None.

    @param component Component payload carrying the landmark fields.
    @return left/right/top/bottom (photo frame, X right, Y down) when the
        photograph showed the opening or the cable exit with high/medium
        confidence; None otherwise.
    """
    if component.get("landmark_feature") not in _MOUTH_FEATURES:
        return None
    if component.get("landmark_confidence") not in ("high", "medium"):
        return None
    side = component.get("landmark_side")
    return side if side in _LANDMARK_DIRECTIONS else None


def _mouth_axis(observed_side, rotation_deg):
    """@brief Local (axis, sign) of the mouth from an observed photo-frame side.

    @param observed_side left/right/top/bottom in the photo frame.
    @param rotation_deg Rotation the caller applies after the builder, model
        frame (Y up), any angle.
    @return (0 or 1, +1.0 or -1.0), or None when the side is unknown or lies
        on a diagonal of the rotated body (no local face points there).
    """
    wanted = _LANDMARK_DIRECTIONS.get(observed_side)
    if wanted is None:
        return None
    gx, gy = wanted[0], -wanted[1]          # photo Y down -> model Y up
    angle = math.radians(rotation_deg or 0.0)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    local_x = gx * cos_a + gy * sin_a        # projection on the rotated local X
    local_y = -gx * sin_a + gy * cos_a       # projection on the rotated local Y
    if abs(abs(local_x) - abs(local_y)) < 1e-6:
        return None
    if abs(local_x) > abs(local_y):
        return 0, (1.0 if local_x > 0 else -1.0)
    return 1, (1.0 if local_y > 0 else -1.0)


def _connector(cx, cy, z0, length, width, height, color, rotation_deg=0, mating=None,
               board_centre=(0.0, 0.0), mouth_side=None):
    """Approximate an observed entry; unknown entry stays a generic envelope.

    Top-entry barrels and edge-entry shells are visual estimates, not observed
    connector identities. Mating alone does not establish barrel shape or
    entry polarity.

    `mouth_side` is the OBSERVED side of the opening (photo frame, see
    `_observed_mouth_side`): when present it decides both the mating axis and
    its verse. Without it the fallback below applies -- a DECLARED HEURISTIC
    written on a USB receptacle at the board edge: mating axis along the
    shorter horizontal side, mouth facing away from the board centre. It is
    wrong for a connector whose insertion depth is its longer side or that
    sits away from the edge; the caller reports when it is used.

    A connector mates sideways (USB, jack, shielded header): the mouth is on
    the face turned toward the outside of the board, not on the lid. Digging
    it from the top produced a box with a recess on top, which in the
    preview cannot be told from a generic package.

    The mating axis is the smaller horizontal dimension -- the insertion
    depth -- and the verse is the one moving away from the board centre,
    which for an edge connector is the edge itself. Walls of thickness
    `wall` remain on the bottom, flanks, roof and base: the piece is a
    shell, not a through tube.
    """
    # Dimensions cannot distinguish a top entry from an edge entry.
    if mating not in ("edge", "top"):
        _warn("connector mating sconosciuto; ingombro generico")
        return _generic(cx, cy, z0, length, width, height, color)
    if mating == "top":
        return _coaxial_connector(cx, cy, z0, length, width, height, color)

    shell = _box(cx, cy, z0, length, width, height)
    wall = max(0.2, min(length, width, height) * 0.15)
    # A package too small to have walls stays solid: better an
    # honest box than a degenerate shell with interpenetrating faces.
    if min(length, width, height) <= 2.5 * wall:
        return [(shell, color)]

    observed = _mouth_axis(mouth_side, rotation_deg) if mouth_side else None
    if observed is not None:
        axis, sign = observed
    else:
        axis = 0 if length <= width else 1
        sign = _outward_sign(cx, cy, axis, rotation_deg, board_centre)
    if axis == 0:
        mating = length
        cross_w, cross_h = width - 2 * wall, height - 2 * wall
        cavity = _box(cx + sign * wall, cy, z0 + wall, mating, cross_w, cross_h)
    else:
        mating = width
        cross_w, cross_h = length - 2 * wall, height - 2 * wall
        cavity = _box(cx, cy + sign * wall, z0 + wall, cross_w, mating, cross_h)

    # Slot-shaped mouth: filleting the edges parallel to the mating axis
    # with half the short side, the rectangular section becomes a stadium.
    # It is the profile of a USB, not a squared hole.
    cavity = _fillet_best(cavity, min(cross_w, cross_h) / 2, _edges_along(cavity, axis))
    # The shell has a flat bottom and a very rounded top: fillet only
    # the two long top edges. The radius is a large fraction of the
    # short side, otherwise it stays a box with chamfered corners.
    shell = _fillet_best(shell, min(cross_w, cross_h) * 0.45, _upper_edges_along(shell, axis))
    try:
        shell = shell.cut(cavity)
    except Exception:  # noqa: BLE001
        return [(_box(cx, cy, z0, length, width, height), color)]

    # A wide, low mouth is that of the USB family, which has the
    # contact tongue in the middle. It is a rule on the measured shape,
    # not on the component's name: a barrel jack does not get it.
    if max(cross_w, cross_h) < 2.5 * min(cross_w, cross_h):
        return [(shell, color)]
    shell = _dimples(shell, cx, cy, z0, axis, sign, mating, cross_w, height, wall)
    parts = [(shell, color)]
    tongue = _tongue(cx, cy, z0, axis, sign, mating, cross_w, cross_h, wall)
    if tongue is not None:
        parts.append((tongue, "#2a2a2a"))
    return parts


def _dimples(shell, cx, cy, z0, axis, sign, mating, cross_w, height, wall):
    """The two dimples stamped on the roof of the shell.

    They are the detail recognised at once on a USB receptacle. Shallow:
    they serve to break the reflection of the top face, not to reproduce a
    dimension. If the subtraction fails the roof stays smooth.
    """
    radius = min(cross_w * 0.10, mating * 0.10)
    depth = min(wall * 0.6, height * 0.08)
    if radius < 0.12 or depth < 0.03:
        return shell
    # Set back from the mouth, symmetric about the transverse axis.
    back = -sign * mating * 0.18
    for offset in (-cross_w * 0.26, cross_w * 0.26):
        px, py = ((cx + back, cy + offset) if axis == 0 else (cx + offset, cy + back))
        try:
            shell = shell.cut(Part.makeCylinder(radius, depth * 2,
                                                V(px, py, z0 + height - depth)))
        except Exception:  # noqa: BLE001 - smooth top, not fatal
            return shell
    return shell


def _tongue(cx, cy, z0, axis, sign, mating, cross_w, cross_h, wall):
    """Contact tongue, set back from the mouth.

    It does not reach the rim: it is seen at the bottom of the slot, as on
    the real part. Returns None when the cavity is too thin to host it
    without touching the walls.
    """
    thickness = cross_h * 0.34 if cross_w >= cross_h else cross_w * 0.34
    depth = mating * 0.55
    if thickness < 0.08 or depth <= wall:
        return None
    inset = wall  # how far the tongue stays back from the mouth plane
    # The direction is that of the mouth: the tongue faces the slot,
    # it does not hide at the bottom of the cavity.
    centre = sign * (mating / 2 - inset - depth / 2)
    span = (cross_w if cross_w >= cross_h else cross_h) * 0.72
    if axis == 0:
        return _box(cx + centre, cy, z0 + wall + (cross_h - thickness) / 2,
                    depth, span, thickness)
    return _box(cx, cy + centre, z0 + wall + (cross_h - thickness) / 2,
                span, depth, thickness)


def _shade(color, factor):
    """Lighter or darker variant of the same colour.

    It serves to tell two parts of the same piece apart (base and button)
    without inventing a colour the photograph never showed.
    """
    text = str(color or "").lstrip("#")
    if len(text) != 6:
        return color
    try:
        channels = [int(text[i:i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        return color
    return "#" + "".join(f"{min(255, max(0, round(c * factor))):02x}" for c in channels)


def _tactile_switch(cx, cy, z0, length, width, height, color):
    """Micro switch: square base plus the protruding cylindrical button.

    It is the shape recognised at a glance in the preview -- BOOT and RESET
    on a development board -- and that a plain box loses. The cylinder is
    centred: off-centre buttons exist, but they cannot be deduced from the
    rectangle measured on the photograph.
    """
    radius = min(length, width) * 0.32
    travel = height * 0.38
    # Without the button the base must take the whole height: a piece that
    # shrinks would falsify the envelope measured on the photo.
    if radius <= 0.05 or travel <= 0.05:
        return [(_box(cx, cy, z0, length, width, height), color)]
    base = _box(cx, cy, z0, length, width, height - travel)
    base = _fillet(base, min(length, width) * 0.10, _vertical_edges(base))
    plunger = Part.makeCylinder(radius, travel, V(cx, cy, z0 + height - travel))
    return [(base, color), (plunger, _shade(color, 1.45))]


def _transformer(cx, cy, z0, length, width, height, color):
    return _rounded_box(cx, cy, z0, length, width, height, color, ratio=0.12, cap=0.6)


def _crystal(cx, cy, z0, length, width, height, color):
    return _rounded_box(cx, cy, z0, length, width, height, color or "#b8b8b8",
                        ratio=0.3, cap=0.8)


def _electrolytic(cx, cy, z0, length, width, height, color):
    """@brief Upright can on a square plastic base, inside the measured envelope.

    @return [(base, colour), (can, colour)]; a plain box when the envelope is
        too flat or too small for the two parts.
    @note The can diameter is the short side of the measured box and the base
        takes a tenth of the height: proportions of an SMD electrolytic, not a
        measurement. The envelope (length x width x height) is never exceeded.
    """
    radius = min(length, width) / 2
    base_h = height * 0.1
    if radius <= 0.2 or height - base_h <= 0.2 or base_h <= 0.02:
        return [(_box(cx, cy, z0, length, width, height), color)]
    base = _box(cx, cy, z0, length, width, base_h)
    can = Part.makeCylinder(radius * 0.98, height - base_h, V(cx, cy, z0 + base_h))
    return [(base, "#202020"), (can, color)]


BUILDERS = {
    "generic": _generic,
    # An unidentified IC does not establish a package or terminal layout.
    "ic": _generic,
    "ic_soic": _ic_soic,
    "ic_qfn": _ic_qfn,
    "transformer": _transformer,
    "module": _transformer,
    "led": _led,
    "capacitor_tantalum": _tantalum,
    "capacitor": _chip,
    "resistor": _chip,
    "inductor": _transformer,
    "diode": _diode,
    "diode_melf": _diode_melf,
    "connector": _connector,
    "switch": _tactile_switch,
    "crystal": _crystal,
    # Types added to the extraction vocabulary (point 84 medium 13). Until
    # they have a shape of their own they use the closest existing shape,
    # WITHOUT inventing terminals: a QFP/BGA stays a flat body (like the QFN,
    # with the pin 1 reference only if observed); a SOT does not use the SOIC
    # leads, which on 3-6 leads at a 0.65-0.95 pitch would be invented; a
    # package with a tab, a relay, a display, a battery, a buzzer, a trimmer
    # and a pin-header connector stay the measured envelope (_connector without
    # an observed mating falls back to the block anyway); the shield is a
    # filleted box like the module. Declared approximations, to be
    # replaced with dedicated shapes.
    "ic_sot": _generic,
    "ic_qfp": _ic_qfn,
    "ic_bga": _ic_qfn,
    "power_tab": _generic,
    "shield_can": _transformer,
    "capacitor_electrolytic": _electrolytic,
    "inductor_power": _transformer,
    "pin_header": _generic,
    "socket_header": _generic,
    "relay": _generic,
    "potentiometer": _generic,
    "crystal_can": _crystal,
    "battery": _generic,
    "display": _generic,
    "buzzer": _generic,
}

#: List exposed to the extraction schema (kicadgen.services) -- keep it
#: aligned with BUILDERS; it is duplicated there because this module cannot
#: be imported outside FreeCAD.
KINDS = tuple(BUILDERS)


class _ModelColor(str):
    """Keep the existing hex-color contract while carrying native face styles."""

    def __new__(cls, fallback, face_colors, path):
        value = super().__new__(cls, fallback)
        value.face_colors = face_colors
        value.model_path = str(path)
        return value


#: Interval between two heartbeats, in seconds. It is ONLY a fallback for the
#: case where the parameter file does not carry the `heartbeat_interval_s`
#: field (old payload, manual test of the script): the good value is the one
#: derived from the watchdog timeout in
#: `kicadgen.cad3d.process_log.heartbeat_interval` and it arrives from there
#: through `set_heartbeat_interval`. This module runs in FreeCAD's embedded
#: interpreter and cannot import that module, so the number travels in the
#: JSON instead of being duplicated here.
_HEARTBEAT_INTERVAL_S = 60.0


def set_heartbeat_interval(seconds):
    """@brief Adopt the heartbeat period derived from the watchdog's timeout.

    @param seconds Value of `heartbeat_interval_s` from the params file; None,
        zero or a non-numeric value leaves the fallback in place.
    @return The interval in seconds that will actually be used.
    @note Called once by freecad_build_body.main(), before the component
        loop. A non-positive or non-finite interval is not accepted: a beat
        every zero seconds would fill the pipe and starve the very work it
        is meant to announce.
    """
    global _HEARTBEAT_INTERVAL_S
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return _HEARTBEAT_INTERVAL_S
    if value > 0 and math.isfinite(value):
        _HEARTBEAT_INTERVAL_S = value
    return _HEARTBEAT_INTERVAL_S


def _model_file_name(path):
    """Name of the model file, whatever the separator in the path.

    `os.path.basename` only knows the separator of the system it runs on,
    but the path comes from the KiCad library and travels in the params
    file: a Windows path read on a POSIX system (or the other way round)
    would stay whole and the line would be truncated by the modal exactly in
    the part the user recognises. It is cut on both separators.
    """
    text = str(path or "").rstrip("/\\")
    for separator in ("/", "\\"):
        text = text.rpartition(separator)[2] or text
    return text or str(path)


def _elapsed_phrase(seconds):
    """Human-readable duration: "45 s", "5 min 12 s"."""
    whole = int(max(0.0, seconds))
    if whole < 60:
        return f"{whole} s"
    return f"{whole // 60} min {whole % 60} s"


def heartbeat_line(model_name, seconds):
    """@brief Compose the one line that keeps the watchdog quiet AND informs the user.

    @param model_name File name of the model being imported (never a full path).
    @param seconds Seconds elapsed since the import started.
    @return An Italian sentence, ready to print on the child's stdout.
    @note ONE mechanism for two purposes. The line must (a) break the
        silence, so the run_logged watchdog does not mistake a slow import
        for a stalled process, and (b) explain to the user why the bar is
        stuck at "12/52". A `KICADGEN_*` marker would only do (a): the modal
        discards it as a machine line (analysis_overlay.is_machine_line), so
        the user would keep looking at a frozen bar.
    @note The form is chosen to pass the modal's filters AS THEY ARE,
        without touching them: no upper-case marker at the start, no
        absolute path (of which only the file name would remain), no
        "key=value", no spaced " : ", no semicolon (which
        `main_window.per_item_detail` reads as an item-by-item report) and
        no parentheses with counts. What remains is the FILE NAME, which is
        the part the user recognises, and the elapsed time, which is the
        reason they are waiting. The tests verify it by importing those two
        functions.
    """
    return (f"Importazione del modello STEP {model_name} ancora in corso da "
            f"{_elapsed_phrase(seconds)}, un modello di libreria grande puo' "
            f"richiedere minuti.")


class _ImportHeartbeat:
    """@brief Announce that a silent library import is still alive.

    @note Why it exists: `Import.insert` and `Part.read` parse a STEP
        assembly without printing ANYTHING. On a real run a shielded RF
        module kept stdout silent for five minutes and the watchdog killed a
        healthy process at the thirteenth component out of fifty-two,
        throwing away the AI calls already paid for as well. The fix is not
        to raise the threshold -- that would be a threshold calibrated on
        the observed case, and a larger model would trip it again -- but to
        make the watchdog's premise true again: whoever works, speaks.
    @note The thread is a daemon and NEVER touches FreeCAD, Part or the
        document: it formats a string and prints it. FreeCAD's APIs are not
        thread-safe, so this is the only thing it may do while the main
        thread is inside the OCCT parser.
    @note DECLARED LIMIT, not verifiable without FreeCAD: a beat only goes
        out if the Python interpreter runs, i.e. if the import's C++ call
        releases the GIL. If it did not, the silence would return and the
        watchdog would start killing healthy imports again. That is why the
        beat is not entrusted to the timer alone: `beat_if_due` is also
        called at the Python points crossed during the import (the copy of
        the assembly's pieces), which beat regardless.
    """

    def __init__(self, path, interval=None):
        """@brief Prepare the heartbeat of one model import.

        @param path Model file being imported (only its name is printed).
        @param interval Seconds between beats; None = the job's interval.
        """
        self._name = _model_file_name(path)
        self._interval = float(interval) if interval else _HEARTBEAT_INTERVAL_S
        self._stop = threading.Event()
        # The lock serves a precise promise: after stop() no line comes out
        # any more. Without it, a heartbeat already inside print would appear
        # AFTER the end-of-import marker and the log would tell a lie.
        self._lock = threading.Lock()
        self._thread = None
        self._started = 0.0
        self._last = 0.0

    def start(self):
        """@brief Start beating; safe to call once for each import.

        @return This heartbeat, so `with _ImportHeartbeat(...) as hb` binds it.
        """
        self._started = self._last = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="kicadgen-import-heartbeat")
        self._thread.start()
        return self

    def stop(self):
        """@brief Stop beating for good; idempotent and safe from any state."""
        with self._lock:
            self._stop.set()
        thread = self._thread
        if thread is not None:
            # wait() returns at once when the event is set, so this
            # join does not wait an interval. The cap is a safety net
            # independent of the interval: generation must not stop
            # because of the heartbeat even with a very long interval.
            thread.join(timeout=min(self._interval, 5.0))
            self._thread = None

    def beat_if_due(self):
        """@brief Emit a beat from the working thread when one is due.

        @note Needed at the long Python points of the import: they beat even
            if the timer cannot run. It does not print early, so it does not
            double the lines when the timer works.
        """
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
                # A single write, text and newline together: with stdout
                # line-buffered it is what makes the heartbeat indivisible
                # with respect to the lines printed by the worker thread. print()
                # writes the text and the newline separately, and the two lines
                # could interleave halfway.
                sys.stdout.write(heartbeat_line(self._name, now - self._started) + "\n")
                sys.stdout.flush()
            except Exception:  # noqa: BLE001 - stdout closed: the heartbeat is not the work
                self._stop.set()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        # Holds also when the import raises: the heartbeat must not outlive
        # the step it was announcing.
        self.stop()
        return False


def _read_library_parts(path, color, heartbeat=None):
    """Copy shapes and face colors out of an isolated headless import document."""
    import Import

    previous = FreeCAD.ActiveDocument
    doc = FreeCAD.newDocument("kicadgen_library_import")
    try:
        imported = Import.insert(str(path), doc.Name, merge=False)
        if not imported:
            raise _CheckFailed("no_colored_shapes", "headless importer returned no colored shapes")
        parts = []
        for obj, colors in imported:
            # Copying the shapes of a large assembly is as slow as reading it:
            # it is Python, so here the heartbeat does not depend on the parser's GIL.
            if heartbeat is not None:
                heartbeat.beat_if_due()
            shape = obj.Shape.copy()
            if shape.isNull() or not shape.Solids or not shape.isValid():
                raise _CheckFailed("non_solid_geometry",
                                  "library model contains invalid or non-solid geometry")
            # Imported assembly objects may have placements inherited from a parent.
            shape.Placement = obj.getGlobalPlacement()
            colors = list(colors)
            if len(colors) == 1:
                colors *= len(shape.Faces)
            if len(colors) != len(shape.Faces):
                raise _CheckFailed("face_colour_mismatch",
                                  "native face-color count does not match geometry")
            parts.append((shape, _ModelColor(color, colors, path)))
        return parts
    finally:
        FreeCAD.closeDocument(doc.Name)
        if previous is not None:
            FreeCAD.setActiveDocument(previous.Name)


def _native_axis_angle(references, length, width):
    """@brief Decide which model axis must end up along the photographed length.

    @param references Bounding boxes to compare (full solid, and the section
        through the body when it is available): a lead envelope can have a
        different long axis from the plastic body.
    @param length Photographed envelope along X, in mm.
    @param width Photographed envelope along Y, in mm.
    @return (angle in degrees, the reference that matched) with angle 0 or 90.
    @note The axis is decided by WHICH side is the longer one, never by a sum
        of ratios. A sum of ratios is not scale invariant: with a photographic
        envelope inflated by 28% it flipped the choice with a 1% margin and put
        the model's long axis across the board's short one, which moves an
        asymmetric feature from a short edge to a long edge. Reading the sides
        in order is also the reading that kicad_models.size_distance already
        used to call that model compatible: before this, the two stages
        disagreed in silence and nothing reported it.
    """
    measured_long, measured_short = sorted((length, width), reverse=True)
    measured_long_on_x = length >= width
    choices = []
    for reference in references:
        model_long_on_x = reference.XLength >= reference.YLength
        model_long = max(reference.XLength, reference.YLength)
        model_short = min(reference.XLength, reference.YLength)
        # Same residual as the matcher: the worse of the two sides, so a good
        # fit on one axis cannot pay for a bad one on the other.
        score = max(abs(model_long - measured_long) / measured_long,
                    abs(model_short - measured_short) / measured_short)
        choices.append((score, 0 if model_long_on_x == measured_long_on_x else 90,
                        reference))
    score, angle, reference = min(choices, key=lambda choice: choice[0])
    return angle, reference


#: Fraction of volume not surviving a half turn below which the solid
#: is symmetric: what remains is numerical residue of the boolean, not a
#: feature. On a truly symmetric body the residue tends to zero by itself,
#: so the threshold separates noise from signal and is not cut to fit a
#: piece: the module it was tested on sits at 15.9%, an order of
#: magnitude above.
NATIVE_ASYMMETRY_MIN_FRACTION = 0.01

#: How much the dominant direction must stand out from the other for the
#: feature to be attributed to a SIDE. When equal the direction points to the
#: diagonal, i.e. to a corner, and a corner is not compared with a declared side.
NATIVE_ASYMMETRY_DOMINANCE = 2.0

#: How far the residue must be shifted from the centre, as a fraction of the
#: half side, for it to indicate an end. A residue that exists but sits at the
#: centre means the asymmetry is BALANCED between the two ends and does not
#: tell them apart: measured on a real SOT-223, where the residue weighs 4.9% of
#: the volume but splits into two identical lobes (0.9156 mm3 on one side and
#: 0.9156 on the other). Without this check a hundredth of a micron of noise
#: would become a side.
#: For comparison, on the same files: shielded module 0.181; USB-C
#: receptacle 0.514; 0603 chip exactly 0.
NATIVE_ASYMMETRY_MIN_OFFSET = 0.05

#: Which way each declarable side faces, in the footprint frame
#: (X right, Y down), which is also the frame in which the model describes the
#: photograph.
_LANDMARK_DIRECTIONS = {"right": (1, 0), "left": (-1, 0),
                        "bottom": (0, 1), "top": (0, -1)}


def _dominant_direction(dx, dy):
    """@brief Scale-invariant part of the rule: cardinal verse, or None.

    @param dx Offset of the centroid along X, in any unit.
    @param dy Offset along Y, in the SAME unit.
    @return Cardinal (dx, dy), or None when the two components are
        equivalent (the feature lies in a corner) or there is no offset.
    @note Isolated because it is the only part of the decision that does not
        change when both components are multiplied by the same positive
        factor. That is what allows reading it on the centroid of the whole
        solid, which costs nothing, instead of on that of the residue, which
        costs a boolean: the two offsets differ exactly by the factor
        total_volume/residue_volume (see `_cheap_landmark_verdict`).
    """
    major, minor = (abs(dx), abs(dy)) if abs(dx) >= abs(dy) else (abs(dy), abs(dx))
    if not major > 0:
        return None
    if major < NATIVE_ASYMMETRY_DOMINANCE * minor:
        return None
    return (1 if dx > 0 else -1, 0) if abs(dx) >= abs(dy) else (0, 1 if dy > 0 else -1)


def _asymmetry_direction(fraction, dx, dy):
    """@brief Translate the asymmetry measurement into a side, or into "unknown".

    @param fraction Volume that does not survive the half turn, over the total volume.
    @param dx Offset of the residue centroid along X, in half-sides.
    @param dy Offset along Y, in half-sides.
    @return Cardinal (dx, dy), or None when the measurement does not
        attribute the feature to a single side.
    @note Deliberately separated from the geometry: no OpenCascade is needed
        here, and the deciding rule can be tested on its own.
    """
    if not (fraction >= NATIVE_ASYMMETRY_MIN_FRACTION):
        return None
    if max(abs(dx), abs(dy)) < NATIVE_ASYMMETRY_MIN_OFFSET:
        return None
    return _dominant_direction(dx, dy)


def _landmark_budget_s():
    """@brief Seconds a single orientation measurement may spend before giving up.

    @return The budget in seconds, equal to the heartbeat interval.
    @note It is NOT a new number: it is the interval between two beats, which
        in turn is a fraction of the watchdog's silence timeout
        (`step_generator.FREECAD_SILENCE_TIMEOUT_S` through
        `process_log.heartbeat_interval`). A single knob, so the two cannot
        diverge.
    @note Why that very interval: the boolean subtraction is a C++ call that
        may not release the GIL, so while it runs NO beat may come out. The
        heartbeat interval is already the project's answer to the question
        "how long may a step stay silent before it has to prove it is
        alive": a measurement that fits within that interval cannot, by
        itself, turn a healthy run into a silence the watchdog mistakes for
        a hang.
    @note DECLARED HEURISTIC in the choice of the fraction only: that the
        budget is ONE interval rather than two or half does not follow from
        a measurement. It is not calibrated on the observed case -- the
        module that triggered the fix takes minutes, so it blows through any
        sensible fraction of the timeout -- and it is tied to the watchdog,
        not to the part. An interruptible measurement would make it
        superfluous: if OpenCascade offered a `BRepAlgoAPI` with
        progress/cancel, the limit would be the user's giving up and not a
        time decided here.
    """
    return _HEARTBEAT_INTERVAL_S


class _MeasurementExpired(Exception):
    """The measurement budget ran out: the verse stays unmeasured."""


def _expired(deadline):
    """True when the budget ran out; a None deadline means no limit."""
    return deadline is not None and time.monotonic() >= deadline


def _check_deadline(deadline):
    if _expired(deadline):
        raise _MeasurementExpired()


def _reference_frame(reference):
    """Centre and half-sides of the reference, i.e. the unit the offsets are measured in."""
    centre_x = (reference.XMin + reference.XMax) / 2
    centre_y = (reference.YMin + reference.YMax) / 2
    return (centre_x, centre_y,
            max(reference.XLength / 2, 1e-9), max(reference.YLength / 2, 1e-9))


#: Sentinel: the zero-cost measure is not enough, the boolean is needed.
_UNDECIDED = object()


def _cheap_landmark_verdict(shape, reference):
    """@brief Decide the verse without a boolean, when the identity allows it.

    @param shape Solid (or compound) already read from the library file.
    @param reference Envelope on which the axis was chosen.
    @return The cardinal verse, None, or `_UNDECIDED` when the free
        measurement alone is not enough to decide.
    @note EXACT IDENTITY, not an approximation. Let A be the solid, rho the
        half turn about the reference centre, C = A intersected with rhoA and
        R = A minus rhoA the residue. C is invariant under rho, so its
        centroid lies on the rotation axis; from the sum of moments
        (A = C + R) it follows that
            (c_R - centre) = (V_A / V_R) * (c_A - centre).
        The offset of the residue is therefore the offset of the WHOLE solid
        multiplied by a positive factor: sign and dominance between the two
        axes -- i.e. everything that decides WHICH side -- read identically
        on the centroid of the whole solid, which FreeCAD computes without
        a boolean.
    @note Two scale-dependent comparisons remain, and with f = V_R/V_A and
        `major` the larger offset of the whole solid they close like this:
        - from c_A - centre = f * (c_R - centre) and from c_R falling inside
          the solid's envelope it follows that f >= major / reach, where
          `reach` is how far the solid's envelope extends from the reference
          centre, in half-sides. If major >= MIN_FRACTION * reach then f is
          above the fraction threshold without measuring it;
        - f <= 1 always, so if major >= MIN_OFFSET then major / f is above
          the offset threshold without measuring it.
        Together: verse decided at zero cost. Conversely, if
        major < MIN_FRACTION * MIN_OFFSET no f can satisfy both conditions,
        and the answer is "unknown" without a boolean. In between lies the
        band where f must really be measured.
    @note A shape that cannot report its own centroid or envelope (mixed
        compounds, and the fake solids of the tests) does not lose the
        measurement: it goes back to the boolean, which is what happened
        before.
    """
    try:
        centre_x, centre_y, half_x, half_y = _reference_frame(reference)
        centre = shape.CenterOfMass
        box = shape.BoundBox
        dx = (centre.x - centre_x) / half_x
        dy = (centre.y - centre_y) / half_y
        reach = max(abs(box.XMax - centre_x), abs(centre_x - box.XMin)) / half_x
        reach = max(reach, max(abs(box.YMax - centre_y),
                               abs(centre_y - box.YMin)) / half_y)
    except Exception:  # noqa: BLE001 - without a centroid the boolean is paid
        return _UNDECIDED
    direction = _dominant_direction(dx, dy)
    if direction is None:
        return None
    major = max(abs(dx), abs(dy))
    if major < NATIVE_ASYMMETRY_MIN_FRACTION * NATIVE_ASYMMETRY_MIN_OFFSET:
        return None
    if (major >= NATIVE_ASYMMETRY_MIN_OFFSET
            and major >= NATIVE_ASYMMETRY_MIN_FRACTION * max(reach, 1e-9)):
        return direction
    return _UNDECIDED


def _boxes_overlap(first, second):
    """Do two envelopes touch? If not, the solids cannot intersect."""
    return (first.XMin <= second.XMax and second.XMin <= first.XMax
            and first.YMin <= second.YMax and second.YMin <= first.YMax
            and first.ZMin <= second.ZMax and second.ZMin <= first.ZMax)


def _residue_pieces(shape, flipped):
    """@brief Split the subtraction into individually interruptible pieces.

    @param shape Original solid or compound.
    @param flipped Its copy rotated by half a turn.
    @return List of (solid, cutter) with cutter None when the solid survives
        whole, or None when the shape cannot be decomposed.
    @note Two reasons, neither an approximation: (a) a single `cut` on a
        whole assembly is ONE C++ call that Python cannot interrupt, so no
        time limit could stop it, while a sum of solid-by-solid subtractions
        is checked between one piece and the next; (b) a solid whose
        envelope touches no mirrored image survives whole, and there the
        boolean is not executed at all. On an assembly whose pieces are
        mostly far apart this removes most of the work.
    @note DECLARED ASSUMPTION: the solids of the assembly do not
        interpenetrate, otherwise the sum of the volumes counts the shared
        regions twice. It is the same assumption `shape.Volume` of a
        compound already makes today, so the comparison with the fraction
        threshold stays consistent.
    """
    try:
        solids = list(shape.Solids)
        mirrored = list(flipped.Solids)
    except Exception:  # noqa: BLE001 - shape cannot be decomposed
        return None
    if len(solids) < 2:
        return None
    pieces = []
    for solid in solids:
        try:
            box = solid.BoundBox
            cutters = [other for other in mirrored
                       if _boxes_overlap(box, other.BoundBox)]
        except Exception:  # noqa: BLE001 - without a bounding box nothing is pruned
            return None
        pieces.append((solid, Part.makeCompound(cutters) if cutters else None))
    return pieces


def _half_turn_residue(shape, reference, deadline=None, heartbeat=None):
    """@brief Volume and centroid of what does NOT survive a half turn.

    @param shape Solid (or compound) already read from the library file.
    @param reference Envelope on which the axis was chosen.
    @param deadline Instant (time.monotonic) past which the measurement gives up.
    @param heartbeat Heartbeat to keep alive between one piece and the next.
    @return (volume fraction, X offset, Y offset) in half-sides, or None.
    @throws _MeasurementExpired When the budget runs out with the measurement incomplete.
    """
    try:
        centre_x, centre_y, half_x, half_y = _reference_frame(reference)
    except Exception:  # noqa: BLE001 - riferimento inutilizzabile
        return None
    try:
        volume = shape.Volume
        flipped = shape.copy()
        flipped.rotate(V(centre_x, centre_y, 0), V(0, 0, 1), 180)
        pieces = _residue_pieces(shape, flipped)
        if pieces is None:
            # A single piece: the subtraction is indivisible, so the budget
            # can only prevent STARTING it. Declared limit: a call
            # already started runs to the end.
            _check_deadline(deadline)
            residual = shape.cut(flipped)
            total = residual.Volume
            centre = residual.CenterOfMass
            x, y = centre.x, centre.y
        else:
            total = moment_x = moment_y = 0.0
            for solid, cutter in pieces:
                if heartbeat is not None:
                    heartbeat.beat_if_due()
                _check_deadline(deadline)
                piece = solid if cutter is None else solid.cut(cutter)
                piece_volume = piece.Volume
                if piece_volume > 0:
                    centre = piece.CenterOfMass
                    total += piece_volume
                    moment_x += piece_volume * centre.x
                    moment_y += piece_volume * centre.y
            if not total > 0:
                return None
            x, y = moment_x / total, moment_y / total
    except _MeasurementExpired:
        raise
    except Exception:  # noqa: BLE001 - a failed boolean does not stop the solid
        return None
    if not volume > 0:
        return None
    return total / volume, (x - centre_x) / half_x, (y - centre_y) / half_y


def _native_landmark_direction(shape, reference, deadline=None, heartbeat=None):
    """@brief On which side lies the material that tells one end from the other.

    @param shape Solid (or compound) already read from the library file.
    @param reference Envelope used to lay it, i.e. the reference on which
        the axis was chosen.
    @param deadline Instant (time.monotonic) past which the measurement gives up.
    @param heartbeat Heartbeat to keep alive during the measurement.
    @return (dx, dy) with a single nonzero component, or None when the solid
        is symmetric, the feature lies in a corner, or the measurement cost
        more than the budget.
    @throws _MeasurementExpired When the budget runs out with the measurement incomplete.
    @note The asymmetry is, by definition, what does NOT survive a half
        turn: the solid is rotated by 180 degrees about the centre of its
        envelope, subtracted from itself, and the residue is exactly the
        material that tells the two ends apart. The centroid of the WHOLE
        solid is not enough -- on a shielded module the symmetric mass of
        the PCB and the shield dilutes the connector down to 1.5%, below any
        honest threshold -- while the residue of that same module weighs
        15.9%. A defining criterion, not a calibrated one: no file name, no
        measurement taken from a particular part.
    @note The boolean is paid only when truly needed: `_cheap_landmark_verdict`
        closes the cases where the identity between the residue offset and
        the whole-solid offset already decides by itself, and the caller
        replays the measurement from the cache when that file has already
        been measured.
    """
    try:
        if not shape.Volume > 0:
            return None
    except Exception:  # noqa: BLE001 - a solid that cannot tell its volume
        return None
    verdict = _cheap_landmark_verdict(shape, reference)
    if verdict is not _UNDECIDED:
        return verdict
    measured = _half_turn_residue(shape, reference, deadline, heartbeat)
    if measured is None:
        return None
    return _asymmetry_direction(*measured)


#: Name of the index of direction measures. A file separate from the envelope
#: one (`kicad_models.INDEX_FILENAME`) because the two measures have different
#: lifetimes: the envelope is measured by the parent process during matching,
#: the direction is measured by FreeCAD during construction. Same atomic
#: write mechanism and same key recipe.
LANDMARK_INDEX_FILENAME = "kicadgen-landmark-index.json"

#: Sentinel: the cache has nothing to say about this file.
_LANDMARK_MISS = object()


#: Cache folders chosen by the parent process (`model_size_cache.user_cache_dirs`),
#: the preferred one first. Empty = only the historical location. It comes from
#: the parameter file because this module, inside FreeCAD, cannot import
#: `kicadgen.app_paths`.
_CACHE_DIRS = []


def set_cache_dirs(folders):
    """@brief Adopt the cache folders resolved by the parent process.

    @param folders Iterable of folder paths, preferred first; anything that is
        not a non-empty string is ignored.
    @return The folders actually adopted.
    @note Called by freecad_build_body.main() with the `cache_dirs` field of
        the params file, so KICADGEN_DATA_DIR reaches the FreeCAD child too.
    """
    global _CACHE_DIRS
    try:
        _CACHE_DIRS = [str(folder) for folder in folders or ()
                       if isinstance(folder, str) and folder.strip()]
    except TypeError:
        _CACHE_DIRS = []
    return list(_CACHE_DIRS)


def _legacy_cache_dir():
    """Historic location: %LOCALAPPDATA%\\kicadgen (or ~/.cache/kicadgen)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "kicadgen")


def _landmark_cache_paths():
    """Where the index of the verses already measured lives.

    In the user's cache and not next to the model: KiCad's 3D library is
    often installed system-wide and read-only, and a service file in each
    folder would litter it. The key carries the file identity and the
    reference, so a single shared index cannot confuse two models.

    The preferred folder is the user data folder chosen by the parent
    (KICADGEN_DATA_DIR included); the historic location stays last, so an
    index already written there is still read and migrates to the new one
    at the first save (model_size_cache merges the sources and writes to
    the first).
    """
    folders = list(_CACHE_DIRS) + [_legacy_cache_dir()]
    paths = []
    for folder in folders:
        path = os.path.join(folder, LANDMARK_INDEX_FILENAME)
        if path not in paths:
            paths.append(path)
    return paths


def _landmark_store():
    """The store of the measurements, or None when it cannot be reached.

    The same module serves the parent process (as a package) and FreeCAD's
    embedded interpreter (as a module next to this one), as `lead_geometry`
    already does: without the cache the measurement is redone, not broken.
    """
    try:
        try:
            from kicadgen.cad3d import model_size_cache
        except ImportError:
            import model_size_cache
        return model_size_cache
    except Exception:  # noqa: BLE001 - without a cache it is measured again
        return None


def _landmark_reference_signature(reference):
    """The reference is part of the question, so it is part of the key.

    The half turn is about the CENTRE of the reference: measuring on the
    total envelope or on the body section are two different questions with
    two different answers, and one must never be replayed in place of the
    other.
    """
    return ",".join(f"{float(value):.6f}" for value in
                    (reference.XMin, reference.XMax, reference.YMin, reference.YMax))


def _landmark_cache_entry(path, reference):
    """(key, file identity, reference signature), or None."""
    store = _landmark_store()
    if store is None:
        return None
    try:
        identity = store.file_identity(path)
        signature = _landmark_reference_signature(reference)
    except Exception:  # noqa: BLE001 - without an identity nothing is replicated
        return None
    if not identity:
        return None
    return f"{identity}|{signature}", identity, signature


def _landmark_direction_value(value):
    """Verse read from the cache, or `_LANDMARK_MISS` when it is not a valid verse."""
    if value is None:
        return None
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(item in (-1, 0, 1) and not isinstance(item, bool) for item in value)
            and sum(1 for item in value if item) == 1):
        return (int(value[0]), int(value[1]))
    return _LANDMARK_MISS


def _cached_landmark(entry, budget):
    """@brief Replay a landmark already measured for THIS file and reference.

    @param entry Triple from `_landmark_cache_entry`.
    @param budget Budget of the measurement about to be made, in seconds.
    @return (verse,) when the cache answers, `_LANDMARK_MISS` otherwise.
    @note The record carries identity and reference inside itself too, not
        only in the key: so a corrupted or badly merged index cannot make a
        model inherit the measurement of another.
    @note A budget give-up is remembered with the budget it had: same file
        and a budget no larger, it is not worth paying minutes again to
        reach the same give-up; a more generous budget retries.
    """
    store = _landmark_store()
    if store is None or entry is None:
        return _LANDMARK_MISS
    key, identity, signature = entry
    try:
        record = store.load(_landmark_cache_paths()).get(key)
    except Exception:  # noqa: BLE001 - unreadable cache: measure again
        return _LANDMARK_MISS
    if not isinstance(record, dict):
        return _LANDMARK_MISS
    if record.get("model") != identity or record.get("reference") != signature:
        return _LANDMARK_MISS
    status = record.get("status")
    if status == "measured":
        direction = _landmark_direction_value(record.get("direction"))
        return _LANDMARK_MISS if direction is _LANDMARK_MISS else (direction,)
    if status == "budget_exceeded":
        try:
            spent = float(record.get("budget_s"))
        except (TypeError, ValueError):
            return _LANDMARK_MISS
        if math.isfinite(spent) and budget <= spent:
            return (None,)
    return _LANDMARK_MISS


def _store_landmark(entry, direction, status, budget):
    """Record the measurement (or the give-up) as a measurement OF THAT file."""
    store = _landmark_store()
    if store is None or entry is None:
        return
    key, identity, signature = entry
    try:
        store.save({key: {
            "model": identity,
            "reference": signature,
            "status": status,
            "direction": list(direction) if direction else None,
            "budget_s": float(budget),
        }}, _landmark_cache_paths())
    except Exception:  # noqa: BLE001 - an unwritten cache is not a fault
        pass


def landmark_budget_line(model_name, seconds):
    """@brief Compose the line that says the verse was NOT measured, and why.

    @param model_name File name of the model (never a full path).
    @param seconds Budget the measurement was given, in seconds.
    @return An Italian sentence, ready to print on the child's stdout.
    @note Same form as the heartbeat line and for the same reason: it must
        reach both the log and the progress modal. A `KICADGEN_*` marker
        would end up in the log only (analysis_overlay.is_machine_line), and
        the user would never know why that component stayed oriented by the
        envelope alone.
    """
    return (f"Verso del modello STEP {model_name} non misurato entro "
            f"{_elapsed_phrase(seconds)}, il componente resta orientato "
            f"secondo il solo ingombro.")


def _cached_landmark_direction(shape, reference, path, heartbeat=None):
    """@brief Measure the verse once per model file, and never longer than the budget.

    @param shape Solid (or compound) already read from the library file.
    @param reference Envelope on which the axis was chosen.
    @param path Path of the library file: it identifies the measurement.
    @param heartbeat Heartbeat to keep alive during the measurement.
    @return The cardinal verse, or None when it cannot be attributed.
    @note The cache is consulted BEFORE the expensive work, not after: the
        verse of a file is deterministic, so it is paid once only. The key
        is path + size + date of the file (plus the reference), so a
        rewritten file is measured again by itself.
    @note Exceeding the budget is not a failure: it is the "unknown" outcome
        `_verse_correction` already provides for, so the generation goes on
        with the component oriented by the envelope alone.
    """
    budget = _landmark_budget_s()
    entry = _landmark_cache_entry(path, reference)
    cached = _cached_landmark(entry, budget)
    if cached is not _LANDMARK_MISS:
        return cached[0]
    try:
        direction = _native_landmark_direction(
            shape, reference, time.monotonic() + budget, heartbeat)
    except _MeasurementExpired:
        print(landmark_budget_line(_model_file_name(path), budget), flush=True)
        _store_landmark(entry, None, "budget_exceeded", budget)
        return None
    _store_landmark(entry, direction, "measured", budget)
    return direction


#: Height of the body section of a library model, as a fraction of its Z
#: extent: above the standoff only the plastic body remains (see the same
#: choice, and its verification, in `kicad_models._MEASURE_SCRIPT`).
BODY_SECTION_FRACTION = 0.65

#: Index of the body sections already computed, beside the landmark index
#: (2026-10-29 review, H6). The planar slice of a large assembly costs from
#: tenths of a second to minutes and its result is a pure function of the
#: file, of the reader that produced the shapes and of the FreeCAD build, so
#: it is paid once per such triple instead of at every solid build.
SECTION_INDEX_FILENAME = "kicadgen-section-index-v1.json"


def _section_cache_paths():
    """Section index files: the landmark index folders, another file name."""
    return [os.path.join(os.path.dirname(path), SECTION_INDEX_FILENAME)
            for path in _landmark_cache_paths()]


def _freecad_build():
    """FreeCAD version and build of this interpreter as one string, or None."""
    try:
        return "-".join(str(part) for part in FreeCAD.Version()[:4])
    except Exception:  # noqa: BLE001 - no build, no cache: the slice is computed
        return None


def _section_cache_entry(path, reader):
    """(key, record fields) identifying the section of `path` read by `reader`, or None.

    Not the matching's measure in `kicad_models._MEASURE_SCRIPT`, on purpose:
    that one intersects a slab of 2 % of the height, a different reference
    (on a SOIC-8 body with drafted sides its X side is 3.750 mm, the slice
    gives 3.714 mm), so it cannot be replayed in place of this one.
    """
    store = _landmark_store()
    build = _freecad_build()
    if store is None or build is None:
        return None
    try:
        identity = store.file_identity(path)
    except Exception:  # noqa: BLE001 - without an identity nothing is replicated
        return None
    if not identity:
        return None
    fields = {"model": identity, "reader": str(reader),
              "fraction": BODY_SECTION_FRACTION, "freecad": build}
    key = "|".join((identity, fields["reader"], repr(BODY_SECTION_FRACTION), build))
    return key, fields


def _cached_section(entry):
    """@brief Replay a body section already computed for THIS file, reader and build.

    @param entry Pair from `_section_cache_entry`.
    @return (BoundBox,) or (None,) for a section that was empty, or
        `_LANDMARK_MISS` when the index has nothing valid to say.
    @note The record carries its identity fields inside itself, not only in
        the key, and the box must be six finite numbers with min <= max:
        a corrupted or badly merged index is recomputed, never trusted.
    """
    store = _landmark_store()
    if store is None or entry is None:
        return _LANDMARK_MISS
    key, fields = entry
    try:
        record = store.load(_section_cache_paths()).get(key)
    except Exception:  # noqa: BLE001 - unreadable cache: compute again
        return _LANDMARK_MISS
    if not isinstance(record, dict) or any(record.get(k) != v for k, v in fields.items()):
        return _LANDMARK_MISS
    if record.get("status") == "empty":
        return (None,)
    values = record.get("box")
    if (record.get("status") != "measured" or not isinstance(values, list) or len(values) != 6
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v) for v in values)
            or any(values[i] > values[i + 3] for i in range(3))):
        return _LANDMARK_MISS
    return (FreeCAD.BoundBox(*values),)


def _store_section(entry, box):
    """Record a computed section (or its absence) as a property of that file and reader."""
    store = _landmark_store()
    if store is None or entry is None:
        return
    key, fields = entry
    record = dict(fields)
    if box is None:
        record["status"] = "empty"
    else:
        record["status"] = "measured"
        record["box"] = [float(box.XMin), float(box.YMin), float(box.ZMin),
                         float(box.XMax), float(box.YMax), float(box.ZMax)]
    try:
        store.save({key: record}, _section_cache_paths())
    except Exception:  # noqa: BLE001 - an unwritten cache is not a fault
        pass


def _body_section_box(combined, box, path, reader):
    """@brief Bounding box of the planar body section of a library model, computed once.

    @param combined Compound of the model's shapes, as read by @p reader.
    @param box `combined.BoundBox`.
    @param path Library file the shapes were read from: it identifies the section.
    @param reader Which reader produced @p combined ("import" or "part_read"):
        the two may yield different shapes, so they are different questions.
    @return The section's BoundBox, or None when the section is empty or
        the slice failed.
    @note The cache is consulted BEFORE the slice. A failed slice is not
        remembered: it is retried at the next build.
    """
    entry = _section_cache_entry(path, reader)
    cached = _cached_section(entry)
    if cached is not _LANDMARK_MISS:
        return cached[0]
    try:
        section = combined.slice(V(0, 0, 1), box.ZMin + box.ZLength * BODY_SECTION_FRACTION)
        result = Part.makeCompound(section).BoundBox if section else None
    except Exception:  # noqa: BLE001 - no section: the total envelope alone decides
        return None
    _store_section(entry, result)
    return result


def _cardinal_quarters(angle):
    """@brief Quarter turns of a cardinal angle, or None for any other angle.

    @param angle Rotation in degrees (any sign, any number of turns).
    @return 0..3, or None when @p angle is not finite or not a multiple of 90.
    """
    try:
        value = float(angle or 0.0)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    quarters = value / 90.0
    nearest = round(quarters)
    if abs(quarters - nearest) > 1e-6:
        return None
    return int(nearest) % 4


def _local_side_direction(observed_side, component_rotation=0.0):
    """@brief Direction of an observed box side in the component's LOCAL frame.

    @param observed_side left/right/top/bottom in the photo frame (X right,
        Y down), as `photo_global_inventory` declares `landmark_side`.
    @param component_rotation Rotation the caller applies to the built shapes,
        in degrees, in the model frame (Y up: the payload angle already
        negated by `freecad_build_body._flip_y`).
    @return Cardinal (dx, dy) in the local Y-up frame, or None when the side is
        unknown or the rotation is not a multiple of 90 degrees.
    @note Two conversions, in this order: the photo's Y grows downwards, the
        model's upwards (flip); then the component rotation, which the caller
        applies AFTER the builder, is undone. A side of a box rotated by 30
        degrees is not a cardinal direction in its own frame, so no answer is
        better than a rounded one.
    """
    wanted = _LANDMARK_DIRECTIONS.get(observed_side)
    if wanted is None:
        return None
    quarters = _cardinal_quarters(component_rotation)
    if quarters is None:
        return None
    return _rotated((wanted[0], -wanted[1]), -90 * quarters)


def _verse_correction(native_direction, inferred, observed_side, component_rotation=0.0):
    """@brief Rotation that brings the native landmark where it was observed.

    @param native_direction (dx, dy) of the centroid in the model frame, or None.
    @param inferred Axis angle already chosen on the envelope, 0 or 90 degrees.
    @param observed_side Side declared by the observation: left/right/top/bottom,
        in the GLOBAL frame of the photograph (X right, Y down).
    @param component_rotation Rotation the caller applies to the component
        AFTERWARDS, in degrees, in the model frame (Y up).
    @return 0 or 180 degrees to add, or None when it cannot be established.
    @note Only 0 and 180 are tried on purpose: the axis was already decided
        by the envelope measurement, and a 90-degree correction would
        contradict it. If neither 0 nor 180 brings the landmark to the
        observed side, the two data contradict each other and nothing is
        corrected: an unverified verse is better than an invented one.
    @note The observed side is global, the comparison is local: without
        removing the component rotation, at 90/180/270 degrees the landmark
        ended up on the wrong side or the correction was discarded. A
        non-cardinal rotation has no cardinal sides in the local frame: None.
    """
    if native_direction is None:
        return None
    wanted = _local_side_direction(observed_side, component_rotation)
    if wanted is None:
        return None
    for extra in (0, 180):
        if _rotated(native_direction, inferred + extra) == wanted:
            return extra
    return None


#: Within this relative gap two envelope readings are equally supported by
#: the measure. It is the same margin as kicad_models
#: (NAME_TIEBREAK_MARGIN, a test keeps them equal): this module runs inside
#: FreeCAD and does not import the matcher.
AXIS_TIE_MARGIN = 0.05


def _axis_undetermined(reference, length, width):
    """@brief Whether the measured envelope cannot tell the model's two axes apart.

    @param reference Bounding box of the model chosen by _native_axis_angle.
    @param length Photographed envelope along X, in mm.
    @param width Photographed envelope along Y, in mm.
    @return True when the model laid along either axis fits the measurement
        with worst-side errors within AXIS_TIE_MARGIN of each other: the
        "which side is longer" choice of _native_axis_angle is then noise
        (a near-square SOT-23-5 measured 3.43 x 3.17 mm fits at 15.5% one way
        and 18.4% the other).
    """
    try:
        sides = (float(reference.XLength), float(reference.YLength))
        length, width = float(length), float(width)
    except (AttributeError, TypeError, ValueError):
        return False
    if not all(math.isfinite(v) and v > 0 for v in (*sides, length, width)):
        return False

    def error(along_x, along_y):
        return max(abs(along_x - length) / length, abs(along_y - width) / width)
    straight, turned = error(*sides), error(sides[1], sides[0])
    return abs(straight - turned) <= AXIS_TIE_MARGIN


def _classifier_verse(component, inferred, axis_free=False):
    """@brief Degrees that bring a library model to the pose the SMD network read.

    @param component Component payload in the model frame (after _flip_y):
        `rotation_deg` is already negated there, while `_smd_rotation_deg`
        keeps the network's KiCad convention (counter-clockwise as seen from
        above), which is also counter-clockwise in the Y-up model frame.
    @param inferred Axis angle already chosen by comparing the model with the
        measured envelope, in degrees.
    @param axis_free True when the measurement could not decide the axis
        (_axis_undetermined): the network's pose may then also turn the
        model by 90 or 270 degrees.
    @return 0 or 180 (also 90 or 270 with `axis_free`) to add to `inferred`,
        or None when the network did not accept the body, its class has no
        verse (`_smd_rotation_kind` other than "full"), its four rotated views
        disagreed on the pose (`_smd_views_agree`), it gave no pose, or its
        pose disagrees with an axis the measurement did decide.
    @note Only the VERSE is taken from the network, as for an observed
        landmark (_verse_correction): the axis stays the measurement's. A pose
        that contradicts the measured axis is not applied -- an unverified
        verse is better than an invented one. The pose assumes the library
        model is native to its footprint (KiCad's standard libraries are);
        it was checked on photographs of SOIC-8, SOT-23, SOT-23-5, MiniMELF
        and chip LEDs (HANDOFF, 2026-10-02).
    """
    # Only a real pose ("full": the class has a direction) and only if all four
    # rotated views of the network chose it: an average of disagreeing
    # views is a coin toss, not a direction (audit 2026-10-02, SOD-123F
    # 0.54/0.46). For the "axis" classes the network gives only the axis.
    if (component.get("_smd_accepted") is not True
            or component.get("_smd_rotation_kind") != "full"
            or component.get("_smd_views_agree") is not True):
        return None
    pose = component.get("_smd_rotation_deg")
    if isinstance(pose, bool) or not isinstance(pose, (int, float)) or not math.isfinite(pose):
        return None
    try:
        body = float(component.get("rotation_deg") or 0.0)
    except (TypeError, ValueError):
        return None
    for extra in ((0, 180, 90, 270) if axis_free else (0, 180)):
        difference = (body + inferred + extra - float(pose)) % 360.0
        if min(difference, 360.0 - difference) < 1e-6:
            return extra
    return None


def _rotated(direction, angle):
    """Rotate a cardinal direction by a multiple of 90 degrees."""
    dx, dy = direction
    for _ in range(int(round(angle / 90)) % 4):
        dx, dy = -dy, dx
    return (dx, dy)


def _placement_angle(component, inferred):
    """@brief Compose the inferred axis with the corrections that apply to it.

    @param component Component mapping carrying the optional overrides.
    @param inferred Axis angle deduced by comparing the model with the measure.
    @return Total rotation around Z, in degrees.
    @note Only an explicit user selection is absolute: it names a model AND the
        angle to place it at, so it replaces the deduction. The other two are
        deltas and add to it -- the automatic one corrects the VERSE of an
        asymmetric feature, the manual one is the 3D context menu. Writing the
        automatic correction as an absolute angle used to erase the axis: a
        module whose long axis needed 90 degrees ended up at 180, keeping the
        wrong axis, and no value between 0 and 180 could put it right.
    """
    return (float(component.get("model_rotation_deg", inferred))
            + float(component.get("model_rotation_delta_deg") or 0))


def _library_model(component, cx, cy, z0, color):
    """Import resolved STEP geometry and styles, or return None on failure.

    Native Z=0 stays at the PCB datum, including pins below it. Local
    model_rotation_deg overrides inferred body/total axis alignment; the caller
    then rotates the component once. Side lengths cannot determine 180-degree
    polarity or square-model orientation.

    The path arrives already resolved and checked against the measured
    envelope: here the file is read and laid in the right place. The model
    is oriented by the convention of its footprint, so it is centred in plan
    on the component and rested with its base on the board; the rotation is
    applied by the caller, as for every other shape.

    An unreadable file does not stop the generation: the synthetic shape,
    approximate but always available, is used instead.
    """
    path = component.get("model_path")
    if not path:
        return None
    print(f"KICADGEN_IMPORT_START:{path}", flush=True)
    try:
        # The heartbeat covers the WHOLE silent stretch: reading the file (both the
        # path with native colours and the Part.read fallback) and the geometric
        # analysis that follows, where a slice and a boolean on a large
        # assembly also cost minutes without printing anything. It leaves the
        # `with` -- so it stops -- both when the import ends and when it
        # raises, before the outcome marker is printed.
        with _ImportHeartbeat(path) as heartbeat:
            try:
                parts = _read_library_parts(path, color, heartbeat)
                reader = "import"
            except Exception as exc:
                code = getattr(exc, "code", None) if isinstance(exc, _CheckFailed) else None
                if code == "face_colour_mismatch":
                    _warn("colori nativi {path} non disponibili (il numero dei colori delle "
                          "facce non corrisponde alla geometria); si conserva la geometria "
                          "con colore fotografico", path=path)
                elif code == "non_solid_geometry":
                    _warn("colori nativi {path} non disponibili (il modello contiene "
                          "geometria non valida o non solida); si conserva la geometria con "
                          "colore fotografico", path=path)
                elif code == "no_colored_shapes":
                    _warn("colori nativi {path} non disponibili (l'importazione non ha "
                          "restituito forme colorate); si conserva la geometria con colore "
                          "fotografico", path=path)
                else:
                    _warn("colori nativi {path} non disponibili ({exc}); si conserva la "
                       "geometria con colore fotografico",
                       path=path,
                       exc=exc)
                shape = Part.read(str(path))
                if shape.isNull() or not shape.Solids or not shape.isValid():
                    raise _CheckFailed("no_valid_solids", "library model has no valid solids")
                parts = [(shape, _ModelColor(color, None, path))]
                reader = "part_read"
            heartbeat.beat_if_due()
            combined = Part.makeCompound([shape for shape, _ in parts])
            box = combined.BoundBox
            # Compare unsorted axes of both supported observations. A lead
            # envelope can have a different long axis from the plastic body
            # (e.g. SOT-223).
            references = [box]
            # The slice sits on the same silent stretch and on a large assembly
            # it is costly too: it does NOT however have a budget like the direction
            # measure, and the difference is not an oversight. The direction has an
            # «non lo so» (I don't know) outcome already provided for (the component
            # stays oriented by the envelope, as it was before the measure
            # existed), whereas the axis does not: giving it up would place the
            # model rotated by 90 degrees, which is a real error and not a
            # renunciation. Balancing them the same way would require a
            # second honest outcome for the axis, which does not exist today.
            # It is computed once per file, reader and FreeCAD build, and
            # replayed afterwards (2026-10-29 review, H6).
            section_box = _body_section_box(combined, box, path, reader)
            if section_box is not None:
                references.append(section_box)
            length, width = float(component["length_mm"]), float(component["width_mm"])
            inferred, reference = _native_axis_angle(references, length, width)
            # The direction is not derived from the envelope: the two ends of a
            # rectangle measure the same. The model's asymmetric material is compared
            # with the side on which the photograph saw the reference.
            # The measure can cost a boolean, so it is done only if there is an
            # observed side: without one, there would be nothing to compare it with.
            # A file's direction is deterministic: it is measured once, with
            # a budget, and later runs read it back from the cache.
            observed_side = component.get("landmark_side")
            heartbeat.beat_if_due()
            verse = _verse_correction(
                _cached_landmark_direction(combined, reference, path, heartbeat)
                if observed_side else None,
                inferred, observed_side, component.get("rotation_deg") or 0.0)
            if verse:
                print(f"KICADGEN_VERSE:{path}; landmark="
                      f"{component.get('landmark_feature')} on "
                      f"{component.get('landmark_side')}; "
                      f"rotated {verse:g} deg over the inferred axis", flush=True)
            if verse is None:
                # No observed reference: the direction is given by the KiCad pose
                # of the local SMD network, if it recognised the body (user,
                # 2026-10-02: «riconosciuto package ed orientamento» (package and
                # orientation recognised)).
                verse = _classifier_verse(
                    component, inferred,
                    axis_free=_axis_undetermined(reference, length, width))
                if verse is not None and component.get("model_rotation_deg") is None:
                    print(f"KICADGEN_SMD_VERSE:{path}; pose="
                          f"{component.get('_smd_rotation_deg')}; "
                          f"rotated {verse:g} deg over the inferred axis", flush=True)
            inferred += verse or 0
            # The manual correction is a DELTA, not an absolute angle:
            # the starting angle is deduced here by FreeCAD comparing the
            # model with the measure, and the interface does not know it. "Rotate by
            # 90 degrees" is therefore added to whatever the base is, deduced or
            # already forced.
            angle = _placement_angle(component, inferred)
            dz = float(component.get("model_z_offset_mm", 0))
            if not math.isfinite(angle) or not math.isfinite(dz):
                raise _CheckFailed("placement_not_finite", "non-finite library placement")
            centre = V((reference.XMin + reference.XMax) / 2,
                       (reference.YMin + reference.YMax) / 2, 0)
            for shape, _ in parts:
                shape.translate(-centre)
                shape.rotate(V(0, 0, 0), V(0, 0, 1), angle)
                # KiCad models use Z=0 as the PCB seating plane. Do not lift
                # through-hole pins or board locks by aligning their lowest point.
                shape.translate(V(cx, cy, z0 + dz))
        print(f"KICADGEN_IMPORT_OK:{path}; parts={len(parts)}; "
              f"local_rotation_deg={angle:g}", flush=True)
        component["_rendered_local_rotation_deg"] = angle
        return parts
    except Exception as exc:  # noqa: BLE001 - fall back to the synthetic shape
        component["_model_import_error"] = _safe_import_error(exc)
        code = getattr(exc, "code", None) if isinstance(exc, _CheckFailed) else None
        if code == "placement_not_finite":
            _warn("modello {path} non caricabile (posizionamento non finito); si usa la "
                  "forma approssimata", path=path)
        elif code == "no_valid_solids":
            _warn("modello {path} non caricabile (nessun solido valido); si usa la forma "
                  "approssimata", path=path)
        else:
            _warn("modello {path} non caricabile ({exc}); si usa la forma approssimata",
               path=path,
               exc=exc)
        return None


#: Landmark features that establish polarity or pin 1 (enum of
#: `photo_global_inventory.LANDMARK_FEATURES`). Only these may draw a stripe,
#: band or dimple on an approximate shape; a connector opening or a chamfer
#: tells the ends apart but says nothing about polarity.
_POLARITY_FEATURES = ("polarity_marker", "pin1_marker")

#: Builders that can draw an observed polarity/pin-1 mark.
_MARKED_KINDS = ("ic_soic", "ic_qfn", "ic_qfp", "ic_bga", "capacitor_tantalum", "diode",
                 "diode_melf")

#: Kinds whose body can carry ORDINARY leads counted on the photograph
#: (`lead_sides_local`). The shape follows the observed leads whatever the
#: function of the part (user, 2026-09-27: SOT transistors of unknown function
#: rendered as bare blocks). Excluded on purpose: ic_qfn/ic_bga (leadless,
#: they keep their own pad rendering), passives and diodes (end caps, not
#: leads), connectors and modules (their own builders), power_tab (the tab is
#: not an ordinary lead and would be lost).
OBSERVED_LEAD_KINDS = ("ic", "ic_sot", "ic_soic", "ic_qfp", "generic")


def _raw_side_total(sides):
    """@brief Total of the lead counts observed on each side, independent of any frame.

    @param sides {side: count} as observed (image frame), or anything else.
    @return The sum of the valid nonnegative integer counts, or None when
        there is none: a count does not depend on which side a lead is on.
    """
    if not isinstance(sides, dict):
        return None
    # Integral by abstract type, as lead_sides._integral: numpy integers from
    # the photo pipeline count, booleans and floats do not.
    total = sum(int(count) for count in sides.values()
                if isinstance(count, numbers.Integral) and not isinstance(count, bool)
                and count > 0)
    return total or None


def _recomputed_local_sides(sides, model_rotation):
    """@brief Local lead sides of a payload that lacks them, as the payload writer computes them.

    @param sides Observed {side: count} in the image frame (`pins_by_side`).
    @param model_rotation `rotation_deg` of the payload after `_flip_y`
        (model frame, the negated footprint angle), or anything else.
    @return `lead_sides.local_lead_sides(sides, footprint angle)`, or None
        when the angle is not a finite real number or that function returns None.
    """
    if (isinstance(model_rotation, bool) or not isinstance(model_rotation, numbers.Real)
            or not math.isfinite(model_rotation)):
        return None
    try:
        from kicadgen.cad3d.lead_sides import local_lead_sides
    except ModuleNotFoundError:
        from lead_sides import local_lead_sides
    return local_lead_sides(sides, -float(model_rotation))


def _local_sides_or_count(component, kind):
    """@brief Lead counts for each side usable by a builder that draws in the LOCAL frame.

    @param component Payload of one component.
    @param kind Rendered kind, for the warning.
    @return `lead_sides_local` (the observed sides already moved into the
        builder's frame by `lead_sides.local_lead_sides`) when leads were
        observed on each side (`pins_by_side`) and their local sides are known,
        else None. Without an observation nothing is drawn for each side, as
        before: the local sides only say WHERE observed leads go.
    @note `pins_by_side` names IMAGE sides: passing it to a builder that
        draws before the caller's rotation put the leads of a turned body on
        the wrong sides (a QFN photographed at 90 degrees with leads on the
        image top/bottom got them on its local top/bottom, i.e. image
        left/right). A payload without the conversion (written before
        `lead_sides_local` existed) gets it here, from the image sides and the
        same `local_lead_sides` the payload writer uses: exact at any multiple
        of 90 degrees, the nearest quarter turn otherwise. Only when that
        function cannot map the sides either -- an angle exactly between two
        quarter turns, or sides that are no clean observation -- the image
        sides say nothing about the local ones: the caller falls back to the
        total count, spread by the builder's own rule, and a warning says so.
    @note The payload reaches this script after `freecad_build_body._flip_y`,
        which negates `rotation_deg`; the writer converted the sides with the
        footprint-frame angle, so the conversion here uses its negation.
    """
    raw = component.get("pins_by_side")
    if not _raw_side_total(raw):
        return None
    local = component.get("lead_sides_local")
    if isinstance(local, dict) and local:
        return local
    local = _recomputed_local_sides(raw, component.get("rotation_deg"))
    if local:
        return local
    # Written as a literal on purpose: the catalog coverage test collects the
    # templates of `_warn` calls from the source.
    _warn("componente {get} ({kind}) semplificato: lati dei reofori osservati non "
          "riportabili nel riferimento del corpo; reofori distribuiti dal solo conteggio",
          get=component.get("name"), kind=kind)
    return None


def _observed_marker(component):
    """@brief Local direction of an OBSERVED polarity/pin-1 mark, or None.

    @param component Component payload (model frame, after `_flip_y`).
    @return (dx, dy) in the local Y-up frame, or None without photographic
        evidence, with a low confidence or with a non-cardinal rotation.
    @note Without this the builders drew stripes, bands and dimples in a fixed
        place: a polarity nobody observed, shown as if it were geometry.
    """
    if component.get("landmark_feature") not in _POLARITY_FEATURES:
        return None
    if component.get("landmark_confidence") not in ("high", "medium"):
        return None
    return _local_side_direction(component.get("landmark_side"),
                                 component.get("rotation_deg") or 0.0)


def _declared_lead_count(component):
    """Total ordinary lead count declared by the observation, or None.

    `lead_count` counts ordinary leads only (the inventory schema; a tab is in
    `terminal_count`), and is used only when marked complete: a partial count
    would draw a body with too few leads as if it were measured.
    """
    count = component.get("lead_count")
    if type(count) is int and count > 0 and component.get("lead_count_complete") is True:
        return count
    return None


def _board_centre(component):
    """(x, y) of the board centre in the model frame; (0, 0) when not supplied."""
    value = component.get("board_centre_xy")
    try:
        x, y = (float(value[0]), float(value[1]))
    except (TypeError, ValueError, IndexError, KeyError, OverflowError):
        return (0.0, 0.0)
    if not (math.isfinite(x) and math.isfinite(y)):
        return (0.0, 0.0)
    return (x, y)


def _safe_import_error(exc):
    """Keep import diagnostics bounded, printable and JSON-safe without a traceback."""
    try:
        message = str(exc)
    except Exception:
        message = "Unable to read exception message"
    return (type(exc).__name__ + ": " + " ".join(
        "".join(c if c.isprintable() else " " for c in message).split()))[:500]


def _warn_terminal_geometry(code, name, kind):
    """@brief Warn that a terminal_geometry observation was refused, with its reason translated.

    @param code `_CheckFailed.code` raised by `_validated_terminal_geometry`
        or `_normalized_box`.
    @param name Component name.
    @param kind Rendered kind.
    @note One literal template per code (see `_warn_lead_detail`). A code
        without a template of its own -- none exists today -- gets the
        reason-less sentence rather than an English one.
    """
    if code == "bbox_not_four":
        _warn("componente {get} ({kind}) semplificato: terminal_geometry non valida (un "
              "rettangolo non ha quattro coordinate); si usa l'ingombro", get=name, kind=kind)
    elif code == "bbox_out_of_range":
        _warn("componente {get} ({kind}) semplificato: terminal_geometry non valida (coordinate "
              "di un rettangolo non finite o fuori da [0, 1]); si usa l'ingombro",
              get=name, kind=kind)
    elif code == "bbox_empty":
        _warn("componente {get} ({kind}) semplificato: terminal_geometry non valida (un "
              "rettangolo ha area nulla); si usa l'ingombro", get=name, kind=kind)
    elif code == "not_object":
        _warn("componente {get} ({kind}) semplificato: terminal_geometry non valida "
              "(l'osservazione non e' un oggetto); si usa l'ingombro", get=name, kind=kind)
    elif code == "low_confidence":
        _warn("componente {get} ({kind}) semplificato: terminal_geometry non valida "
              "(confidenza diversa da alta o media); si usa l'ingombro", get=name, kind=kind)
    elif code == "terminals_not_list":
        _warn("componente {get} ({kind}) semplificato: terminal_geometry non valida (i "
              "terminali non sono un elenco di al massimo 128 rettangoli); si usa "
              "l'ingombro", get=name, kind=kind)
    elif code == "terminal_not_object":
        _warn("componente {get} ({kind}) semplificato: terminal_geometry non valida (un "
              "terminale non e' un oggetto con bbox); si usa l'ingombro", get=name, kind=kind)
    else:
        _warn("componente {get} ({kind}) semplificato: osservazione terminal_geometry "
              "non valida; si usa l'ingombro", get=name, kind=kind)


def build_component(component, z0):
    """@brief Build shapes, skipping invalid envelopes without raising.

    @param component Component payload with local dimensions in mm, CAD centre,
        optional mating observation and resolved STEP path. model_rotation_deg
        is a local CAD Z rotation before the caller's component rotation.
    @param z0 PCB seating plane in mm; native model Z=0 is placed here.
    @return List of (shape, hex color); imported colors also carry face styles.
    @note Failed model imports mark _model_import_failed and _model_import_error
        on the payload and produce a magenta approximation. Retry clears old markers.

    Optional ``terminal_geometry`` overrides kind/count-based approximations:
    ``{body_bbox: [l,t,r,b], terminals: [{bbox: [l,t,r,b]}],
    confidence: 'high'|'medium'}``. Boxes are normalized to the total local
    length/width envelope, X-right/Y-down, before the caller's rotation.
    Missing/None observations retain legacy behavior. Invalid non-None
    observations produce a warning and a generic envelope box.
    """
    component.pop("_model_import_failed", None)
    component.pop("_model_import_error", None)
    kind = str(component.get("kind") or "generic")
    builder = BUILDERS.get(kind, _generic)
    color = component.get("color_hex") or "#303030"
    # One dedicated template per reason: the reason is part of the translated
    # sentence, never an English value inside it (see `_CheckFailed`).
    keys = ("length_mm", "width_mm", "height_mm", "x", "y")
    missing = [key for key in keys if key not in component]
    if missing:
        _warn("componente {get} ignorato: geometria non valida (manca il campo {field})",
              get=component.get('name'), field=missing[0])
        return []
    values = [component[key] for key in keys] + [z0, component.get("rotation_deg") or 0]
    if any(isinstance(value, bool) for value in values):
        _warn("componente {get} ignorato: geometria non valida (valore booleano)",
              get=component.get('name'))
        return []
    try:
        length, width, height, cx, cy, z0, rotation = map(float, values)
    except (TypeError, ValueError, OverflowError):
        _warn("componente {get} ignorato: geometria non valida (valore non numerico o "
              "fuori scala)", get=component.get('name'))
        return []
    if not all(math.isfinite(value) for value in (length, width, height, cx, cy, z0, rotation)):
        _warn("componente {get} ignorato: geometria non valida (valore non finito)",
              get=component.get('name'))
        return []
    if min(length, width, height) <= 0:
        _warn("componente {get} ignorato: geometria non valida (dimensioni non positive)",
              get=component.get('name'))
        return []
    try:
        # Real model from the KiCad library, already resolved and verified by the
        # caller: it is real geometry, it beats any approximated shape.
        try:
            real = _library_model(component, cx, cy, z0, color)
        except Exception as exc:
            if not component.get("model_path"):
                raise
            component["_model_import_error"] = _safe_import_error(exc)
            real = None
        if real is not None:
            return real
        if component.get("model_path"):
            component["_model_import_failed"] = True
            component.setdefault("_model_import_error", "Model import produced no usable solids")
            color = component["color_hex"] = "#FF00FF"
            return _generic(cx, cy, z0, length, width, height, color)
        if component.get("terminal_geometry") is not None:
            return _observed_terminals(cx, cy, z0, length, width, height, color,
                                       component["terminal_geometry"],
                                       name=component.get("name"))
        observed = component.get("lead_sides_local")
        connector_hint = kind == "generic" and (
            component.get("_detected_kind") == "connector"
            or component.get("mating") in ("edge", "top"))
        if observed and kind in OBSERVED_LEAD_KINDS and not connector_hint:
            # Leads counted on the photo, already brought into the local frame:
            # the shape comes from observation, not from the package name.
            marker = _observed_marker(component) if kind in _MARKED_KINDS else None
            return _ic_observed_pins(cx, cy, z0, length, width, height, color, kind,
                                     pins_by_side=observed, marker=marker)
        if kind == "ic":
            _warn("componente {get} (ic): package sconosciuto, geometria generica senza "
               "terminali",
               get=component.get('name'))
            return _generic(cx, cy, z0, length, width, height, color)
        if kind == "connector" or (kind == "generic" and (
                component.get("_detected_kind") == "connector"
                or component.get("mating") in ("edge", "top"))):
            if component.get("mating") in ("edge", "top"):
                _warn("componente {get}: mating osservato; dettagli connettore approssimati",
                   get=component.get('name'))
            # The only builder whose orientation depends on where the
            # component sits: the mouth must face off the board AFTER the
            # rotation applied by the caller, not before. The mouth side
            # observed in the photo wins over the estimate.
            mouth_side = _observed_mouth_side(component)
            if component.get("mating") == "edge" and mouth_side is None:
                _warn("componente {get}: lato della bocca non osservato; stimato sul lato "
                      "corto, verso l'esterno della scheda",
                      get=component.get('name'))
            return _connector(cx, cy, z0, length, width, height, color,
                              rotation_deg=float(component.get("rotation_deg") or 0),
                              mating=component.get("mating"),
                              board_centre=_board_centre(component),
                              mouth_side=mouth_side)
        if kind in BUILDERS and kind != "generic":
            _warn("componente {get} ({kind}): geometria approssimata, terminal_geometry "
               "assente",
               get=component.get('name'),
               kind=kind)
        pin_count = component.get("pin_count")
        pins_by_side = _local_sides_or_count(component, kind)
        if pins_by_side is None and not pin_count:
            pin_count = _raw_side_total(component.get("pins_by_side"))
        marker = _observed_marker(component) if kind in _MARKED_KINDS else None
        if kind in {"ic_soic", "ic_qfn"} and (pin_count or pins_by_side):
            return _ic_observed_pins(cx, cy, z0, length, width, height, color, kind,
                                     pin_count=pin_count, pins_by_side=pins_by_side,
                                     marker=marker)
        if kind == "ic_soic":
            lead_count = _declared_lead_count(component)
            if _soic_leads(max(length, width), lead_count)[2]:
                _warn("componente {get} (ic_soic): numero di reofori non dichiarato, "
                      "stimato dalla lunghezza del corpo con passo {pitch} mm",
                      get=component.get('name'), pitch=LEAD_PITCH_MM)
            return builder(cx, cy, z0, length, width, height, color, marker=marker,
                           lead_count=lead_count)
        if kind in _MARKED_KINDS:
            return builder(cx, cy, z0, length, width, height, color, marker=marker)
        return builder(cx, cy, z0, length, width, height, color)
    except Exception as exc:  # noqa: BLE001 - fall back to the box
        if isinstance(exc, _CheckFailed):
            # On this path only the terminal_geometry validators raise it:
            # `_library_model` handles its own checks and returns None.
            _warn_terminal_geometry(exc.code, component.get('name'), kind)
        else:
            _warn("componente {get} ({kind}) semplificato: {exc}",
               get=component.get('name'),
               kind=kind,
               exc=exc)
        try:
            return _generic(cx, cy, z0, length, width, height, color)
        except Exception as fallback_exc:
            _warn("componente {get} ignorato: anche la forma generica non e' costruibile "
                  "({fallback_exc})",
               get=component.get('name'),
               fallback_exc=fallback_exc)
            return []
