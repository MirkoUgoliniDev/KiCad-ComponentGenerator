"""@brief Pure constraints for optional photo-derived lead/solder separation.

No package-specific proportions, names, pitch, hidden leads or height inference.
Shared by the photo service and the embedded FreeCAD renderer.
"""
import math


class LeadDetailRejected(ValueError):
    """@brief A photo lead detail refused by one of the constraints below.

    @note `reason` is a stable code (one of REJECTION_REASONS) that callers
        translate for the user; str() stays an English developer diagnostic.
        It is still a ValueError, so existing `except ValueError` callers and
        the embedded FreeCAD renderer keep working unchanged.
    """

    def __init__(self, reason, message):
        """@brief Store the stable reason code next to the English diagnostic.

        @param reason One of REJECTION_REASONS.
        @param message English developer diagnostic, returned by str().
        """
        super().__init__(message)
        self.reason = reason


#: Stable reason codes of LeadDetailRejected, in the order they are checked.
REJECTION_REASONS = ('invalid_rectangle', 'empty_rectangle', 'no_evidence',
                     'exceeds_envelope', 'not_one_face')


def rect(value):
    """@brief Validate a normalized, non-empty [x0, y0, x1, y1] rectangle inside 0..1.

    @param value Candidate rectangle as a sequence of four finite numbers in 0..1.
    @return The rectangle as a list.
    @throws LeadDetailRejected When it is malformed, out of range or empty.
    """
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) or not 0 <= v <= 1 for v in value)):
        raise LeadDetailRejected('invalid_rectangle', 'invalid normalized lead rectangle')
    if value[0] >= value[2] or value[1] >= value[3]:
        raise LeadDetailRejected('empty_rectangle', 'empty lead rectangle')
    return list(value)


def validated_lead_bbox(body, envelope, detail):
    """@brief Validate a photo-separated lead rectangle against the body and the terminal envelope.

    None means no detail. Invalid detail must preserve the original terminal.
    @param body Normalized body rectangle.
    @param envelope Normalized terminal envelope the lead must stay inside.
    @param detail The lead detail dict, or None.
    @return The validated lead rectangle, or None without detail.
    @throws LeadDetailRejected When the detail breaks a constraint (see `reason`).
    """
    if detail is None:
        return None
    if (not isinstance(detail, dict) or detail.get('confidence') != 'high'
            or detail.get('source') != 'photo_separation_v1'):
        raise LeadDetailRejected('no_evidence',
                                 'lead separation lacks explicit high-confidence evidence')
    body, envelope, lead = rect(body), rect(envelope), rect(detail.get('bbox'))
    eps = 1e-9
    # Reconcile floating-point round trips only, never image-sized gaps.
    for i in range(4):
        axis = i % 2
        limits = [body[axis],body[axis+2],envelope[axis],envelope[axis+2]]
        closest = min(limits,key=lambda v:abs(v-lead[i]))
        if abs(closest-lead[i]) <= eps:
            lead[i] = closest
    lead = rect(lead)
    if (lead[0] < envelope[0]-eps or lead[1] < envelope[1]-eps
            or lead[2] > envelope[2]+eps or lead[3] > envelope[3]+eps):
        raise LeadDetailRejected('exceeds_envelope',
                                 'separated lead exceeds original terminal envelope')
    # A lead must meet exactly one body face with nonzero overlap. Gaps and
    # corner-only contact must not turn into fabricated metal bridges.
    overlap_x = min(lead[2],body[2])-max(lead[0],body[0])
    overlap_y = min(lead[3],body[3])-max(lead[1],body[1])
    faces = [lead[0] < body[0]-eps and lead[2] >= body[0] and overlap_y > eps,
             lead[2] > body[2]+eps and lead[0] <= body[2] and overlap_y > eps,
             lead[1] < body[1]-eps and lead[3] >= body[1] and overlap_x > eps,
             lead[3] > body[3]+eps and lead[1] <= body[3] and overlap_x > eps]
    if sum(faces) != 1:
        raise LeadDetailRejected('not_one_face',
                                 'separated lead must meet exactly one body face')
    return lead


#: Largest gap, as a fraction of the target envelope side (normalized
#: target-frame units), between an observed terminal box and the body box
#: that a blurred junction can explain. Declared heuristic: a vision model
#: draws the tight body box on blurred, anti-aliased edges a few pixels inside
#: the real outline, so a truly attached lead can appear slightly detached.
#: The value was chosen by hand when photo terminal observation was introduced
#: (2026-09-13) and has NOT been calibrated on a measured set of crops.
#: Role since the model states the attachment of each terminal (`attachment`
#: of observe_package_terminals, T2 2026-10-16): a CONSISTENCY CHECK of that
#: statement -- an `attached` lead beyond it is a contradiction, drawn as
#: observed with a doubt, and it bounds the metal the renderer may add to
#: close a gap. It still decides alone only for answers without a usable
#: statement (`unclear`, or cached before the field): that fallback would
#: become unnecessary with crops sharp enough for the model to always see the
#: junction. Shared by the photo decoder and the embedded FreeCAD renderer so
#: that both apply the same bound (MEDIO 16, 2026-10-16).
TERMINAL_ATTACH_GAP_MAX = .06

#: Floating-point slack for re-deciding attachment on a payload that does not
#: carry the decoder's own decision (`attached` flag, see
#: `photo_terminals.decode_terminal_geometry`). Such a payload was rotated into
#: the package-local frame by 1 - x after the decoder accepted it, which can
#: move a gap that sat exactly at TERMINAL_ATTACH_GAP_MAX by about 1e-16. The
#: slack is many orders of magnitude below any photographic resolution, so it
#: only absorbs that rounding and does NOT widen the tolerance itself.
ATTACH_FRAME_EPS = 1e-9


def attachment_gaps(body, terminal):
    """@brief Axis-wise clearance between a terminal rectangle and the body rectangle.

    @param body Body rectangle (xmin, ymin, xmax, ymax), any consistent units.
    @param terminal Terminal rectangle in the same frame.
    @return (gap_x, gap_y): the empty distance along each axis, 0 when the
        projections touch or overlap on that axis.
    """
    gap_x = max(body[0] - terminal[2], terminal[0] - body[2], 0)
    gap_y = max(body[1] - terminal[3], terminal[1] - body[3], 0)
    return gap_x, gap_y


def is_attached(body, terminal, gap_max=TERMINAL_ATTACH_GAP_MAX):
    """@brief Decide whether an observed terminal belongs to the body it was observed with.

    @param body Normalized body rectangle (0..1 target frame).
    @param terminal Normalized terminal rectangle in the same frame.
    @param gap_max Largest tolerated clearance on each axis (TERMINAL_ATTACH_GAP_MAX).
    @return True when the clearance on both axes is within the tolerance.
    @note The criterion is purely geometric: it does not depend on the package
        name, the side or the number of terminals. A terminal farther than the
        tolerance on either axis is an island (a pad, a neighbour, a
        reflection) and must never be bridged to the body.
    """
    gap_x, gap_y = attachment_gaps(body, terminal)
    return gap_x <= gap_max and gap_y <= gap_max


def anchor_face(body, terminal):
    """@brief Body face an attached terminal is anchored to.

    @param body Body rectangle (xmin, ymin, xmax, ymax), in the units the
        protrusion must be compared in (millimetres in the renderer: a
        normalized frame would weight the two axes by the envelope aspect).
    @param terminal Terminal rectangle in the same frame.
    @return (axis, sign) with axis 0 for X and 1 for Y, sign -1 for the low
        face (left / near) and +1 for the high face, or None when the terminal
        does not protrude beyond any face (it lies inside the body outline).
    @note A lead leaves the body through one face and its outer end is the
        part the photograph measured, so the anchoring face is the one the
        terminal protrudes from the most. A corner-only observation protrudes
        equally from two faces: the tie is broken deterministically (X faces
        before Y faces); either choice yields a face-connected lead, which is
        the invariant that matters.
    """
    protrusions = [(body[0] - terminal[0], (0, -1)), (terminal[2] - body[2], (0, 1)),
                   (body[1] - terminal[1], (1, -1)), (terminal[3] - body[3], (1, 1))]
    best, face = max(protrusions, key=lambda item: item[0])
    return face if best > 0 else None


def anchored_terminal(body, terminal, face, lateral_min, obstacles=()):
    """@brief Extend a terminal rectangle until it shares a real face with the body.

    @param body Body rectangle (xmin, ymin, xmax, ymax).
    @param terminal Terminal rectangle in the same frame and units.
    @param face (axis, sign) returned by anchor_face.
    @param lateral_min Smallest overlap, along the face, that the lead root
        must share with the face span (the riser footprint in the renderer).
    @param obstacles Rectangles of the other observed leads, same frame and
        units. The lateral lengthening stops at the first one it would enter,
        so anchoring never makes two leads intersect. An obstacle that already
        overlaps the terminal as observed is not caused by the growth and is
        ignored here.
    @return A new rectangle; the terminal is only ever enlarged, and only
        toward the body: its outer end (the edge away from the face) and its
        lateral edge away from the face span are kept where the photo put them.
        When a neighbour leaves no room, the lateral overlap with the face can
        stay below lateral_min (even zero), and the closure along the normal
        is not checked against other leads: face_contact_anchor verifies the
        result and tries another face, or refuses the anchoring.
    @note Two gaps are closed. Along the face normal the inner edge is pulled
        onto the face when it stops short (an overlap is kept as observed:
        the metal under the body is invisible). Along the face the root is
        lengthened toward the face span until the overlap reaches
        lateral_min, capped at the face span itself, so a corner-only contact
        becomes a face contact instead of a line.
    """
    axis, sign = face
    result = list(terminal)
    lo, hi = axis, axis + 2
    if sign < 0:
        result[hi] = max(result[hi], body[lo])
    else:
        result[lo] = min(result[lo], body[hi])
    lat_lo, lat_hi = 1 - axis, 3 - axis
    span_lo, span_hi = body[lat_lo], body[lat_hi]
    need = min(lateral_min, span_hi - span_lo)
    overlap = min(result[lat_hi], span_hi) - max(result[lat_lo], span_lo)
    if overlap < need:
        # Only obstacles beside the lead (sharing part of its normal extent)
        # can be entered by a lateral growth; touching edges are not overlap.
        beside = [o for o in obstacles
                  if min(o[hi], result[hi]) - max(o[lo], result[lo]) > _OVERLAP_EPS
                  and not _overlaps(o, terminal)]
        if result[lat_lo] + result[lat_hi] < span_lo + span_hi:
            target = span_lo + need
            for other in beside:
                if other[lat_lo] >= result[lat_hi] - _OVERLAP_EPS:
                    target = min(target, other[lat_lo])
            result[lat_hi] = max(result[lat_hi], target)
        else:
            target = span_hi - need
            for other in beside:
                if other[lat_hi] <= result[lat_lo] + _OVERLAP_EPS:
                    target = max(target, other[lat_hi])
            result[lat_lo] = min(result[lat_lo], target)
    return result


def contact_faces(body, terminal):
    """@brief Body faces a terminal protrudes from, the preferred one first.

    @param body Body rectangle (xmin, ymin, xmax, ymax), millimetres in the
        renderer (see anchor_face for why not a normalized frame).
    @param terminal Terminal rectangle in the same frame.
    @return List of (axis, sign) faces with a positive protrusion, ordered by
        decreasing protrusion; ties keep the anchor_face order (X faces
        before Y faces, low before high). The first entry is anchor_face().
    """
    protrusions = [(body[0] - terminal[0], (0, -1)), (terminal[2] - body[2], (0, 1)),
                   (body[1] - terminal[1], (1, -1)), (terminal[3] - body[3], (1, 1))]
    ranked = sorted(enumerate(protrusions), key=lambda item: (-item[1][0], item[0]))
    return [face for _, (amount, face) in ranked if amount > 0]


def face_contact_anchor(body, terminal, riser_size, obstacles=()):
    """@brief Anchor an attached terminal so that it shares a real FACE with the body.

    @param body Body rectangle (xmin, ymin, xmax, ymax), millimetres.
    @param terminal Observed terminal rectangle, same frame and units.
    @param riser_size (size along X, size along Y) of the riser footprint:
        the lateral overlap wanted along a Y face is riser_size[0], along an
        X face riser_size[1].
    @param obstacles Rectangles of the other leads (already anchored or as
        observed), same frame and units.
    @return (face, rectangle) for the first face of contact_faces() whose
        anchored rectangle (anchored_terminal) reaches the face, overlaps the
        face span by a positive length and enters no obstacle; None when no
        face allows that.
    @note A partial lateral overlap (a neighbour stopped the growth before
        the full riser footprint) is accepted: the riser then sits inside the
        strip of the foot that faces the body, still on a face. An edge-only
        or corner-only contact is never accepted: it is the floating lead of
        R2T1. Closing the gap along the normal is refused when it would enter
        another lead (two leads in a row along the face normal, R2T3).
        None means the caller must draw the terminal as observed and say so:
        no metal is ever invented inside another lead.
    """
    for face in contact_faces(body, terminal):
        axis, sign = face
        rect = anchored_terminal(body, terminal, face, riser_size[1 - axis], obstacles)
        if any(_overlaps(rect, other) and not _overlaps(terminal, other) for other in obstacles):
            continue
        lat = 1 - axis
        overlap = min(rect[lat + 2], body[lat + 2]) - max(rect[lat], body[lat])
        plane = body[axis] if sign < 0 else body[axis + 2]
        reaches = (rect[axis + 2] >= plane - _OVERLAP_EPS if sign < 0
                   else rect[axis] <= plane + _OVERLAP_EPS)
        if overlap > _OVERLAP_EPS and reaches:
            return face, rect
    return None


#: Overlap below this size (same units as the rectangles) counts as touching.
_OVERLAP_EPS = 1e-9


def _overlaps(a, b):
    """Return True when two rectangles share a positive area (touching is not overlap)."""
    return (min(a[2], b[2]) - max(a[0], b[0]) > _OVERLAP_EPS
            and min(a[3], b[3]) - max(a[1], b[1]) > _OVERLAP_EPS)
