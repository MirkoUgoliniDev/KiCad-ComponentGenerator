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
