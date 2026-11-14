"""@brief Geometry of the male pin headers soldered under a populated board.

@details User request (2026-09-26, photo of a development board): when the 3D
model shows the pins used to plug the board in, they must look like the real
strips — square pins through a black plastic spacer under the board — not
smooth cylinders.

The header is assumed mounted from below, as on the photographed boards: the
long tail points down, the plastic spacer sits against the underside, the
short tail goes up through the board and shows on the top side.

Every size is a CATALOG value of the standard 2.54 mm male header, not a
measurement: spacer 2.5 mm, long tail 6.0 mm, short tail 3.0 mm, square pin
0.64 mm. For another pitch (2.0, 1.27 mm headers) the sizes scale with the
pitch, which is how those families are drawn; a vendor drawing would make
the estimate unnecessary.

Seating plane (user-confirmed, 2026-11-13): when a header is drawn under the
module, the plane KiCad puts on the host board's top face (the model's Z=0)
is the BOTTOM face of the black plastic spacer, not the module's underside:
the module sits on the spacer, the spacer on the host, the long tails go down
through the host. The boxes below are still built with the module underside
at `board_bottom_z`; header_seating_lift() gives the separate translation that
the FreeCAD script applies to the whole model, so the box contract stays the
same for every caller. The lift applies only when EVERY contact of the module
is header-mounted: a module that also has flat-soldered contacts (half holes)
is seated on those, and the caller passes no lift (see
step_generator.header_seating_plan).

This module does not import FreeCAD: it returns plain boxes, so it can be
checked by the tests and used by the FreeCAD script alike.
"""
import math

#: Pitch of the reference header, mm.
REFERENCE_PITCH_MM = 2.54
#: Catalog sizes of the reference header, mm (estimates, see module notes).
SPACER_HEIGHT_MM = 2.5
LONG_TAIL_MM = 6.0
SHORT_TAIL_MM = 3.0
PIN_SIDE_MM = 0.64
#: Minimum amount of pin shown above the board when the board is thicker
#: than the short tail (the tail would otherwise end inside the board).
MIN_TOP_PROTRUSION_MM = 0.3


def header_pitch(pins) -> float:
    """@brief Distance between neighbouring pins, the header's pitch.

    @param pins Sequence of (number, x, y) in mm.
    @return The smallest centre distance between two pins; the reference
        pitch when fewer than two pins exist.
    """
    points = [(float(x), float(y)) for _number, x, y in pins]
    best = math.inf
    for index, (x0, y0) in enumerate(points):
        for x1, y1 in points[index + 1:]:
            distance = math.hypot(x1 - x0, y1 - y0)
            if distance > 1e-6:
                best = min(best, distance)
    return REFERENCE_PITCH_MM if math.isinf(best) else best


def _resolved_pitch(pins, pitch) -> float:
    """Declared pitch when positive, otherwise the one measured on `pins`.

    The single rule shared by the boxes and the seating lift, so the spacer
    that is drawn and the spacer the model is lifted by cannot disagree.
    """
    if pitch is None or not float(pitch) > 0:
        return header_pitch(pins)
    return float(pitch)


def spacer_height(pitch: float) -> float:
    """@brief Height of the plastic spacer of a male header of this pitch, mm.

    @param pitch Header pitch, mm (positive).
    @return SPACER_HEIGHT_MM scaled by pitch / REFERENCE_PITCH_MM.
    @note A CATALOG estimate (see the module notes), not a measure: a vendor
        drawing of the actual header would replace it.
    """
    return SPACER_HEIGHT_MM * float(pitch) / REFERENCE_PITCH_MM


def _pitch_groups(pins, pitches):
    """Pins grouped by declared row pitch: [(pitch or None, pins)].

    None as `pitches` gives one group whose pitch is inferred from all the
    pins (the populated-board behaviour).
    """
    pins = list(pins)
    if pitches is None:
        return [(None, pins)]
    pitches = list(pitches)
    if len(pitches) != len(pins):
        raise ValueError("pitches must be aligned with pins")
    groups = {}
    for pin, pitch in zip(pins, pitches):
        groups.setdefault(float(pitch), []).append(pin)
    return list(groups.items())


def header_seating(pins, pitches=None) -> tuple:
    """@brief Seating lift of the drawn header and the pitch whose spacer sets it.

    @param pins Sequence of (number, x, y) of the pins that receive a header.
    @param pitches None, or the declared row pitch of every pin (see
        header_seating_lift()).
    @return (lift_mm, pitch_mm): header_seating_lift() and the pitch of the
        tallest spacer; (0.0, None) when no header is drawn.
    @throws ValueError When `pitches` is given with a length different from `pins`.
    """
    lift, chosen = 0.0, None
    for pitch, group in _pitch_groups(pins, pitches):
        if group:
            resolved = _resolved_pitch(group, pitch)
            height = spacer_height(resolved)
            if height > lift:
                lift, chosen = height, resolved
    return lift, chosen


def header_seating_lift(pins, pitches=None) -> float:
    """@brief Z translation that puts the drawn header's spacer bottom on Z=0, mm.

    @param pins Sequence of (number, x, y) of the pins that receive a header;
        empty when no header is drawn.
    @param pitches None, or the declared row pitch of every pin, aligned with
        `pins`, exactly as given to male_header_boxes_by_row().
    @return 0.0 when no header is drawn (the module stays flat-soldered with
        its PCB underside at Z=0); otherwise the tallest spacer among the drawn
        rows, from spacer_height() and the same pitch rule as the boxes.
    @throws ValueError When `pitches` is given with a length different from `pins`.
    @note User-confirmed seating plane (2026-11-13): KiCad places the model's
        Z=0 on the host board's top face, which is where the spacer's bottom
        rests. With rows of different pitches the tallest spacer touches the
        host and the shorter ones stay above it: nothing sinks into the host.
        The height is the catalogue estimate of spacer_height().
    @note Geometry only: whether the module really rests on its headers (every
        contact header-mounted) is decided by the caller, see
        step_generator.header_seating_plan().
    """
    return header_seating(pins, pitches)[0]


def male_header_boxes(pins, board_top_z: float, board_bottom_z: float = 0.0,
                      pitch=None):
    """@brief Boxes of a male header under each pin: one spacer cell and one pin.

    @param pins Sequence of (number, x, y) hole centres, mm.
    @param board_top_z Z of the board's top face, mm.
    @param board_bottom_z Z of the board's underside, mm. The boxes are built
        against it; the seating plane on the host is lower by
        header_seating_lift(), applied by the caller as a separate step.
    @param pitch Declared pitch of these pins, mm. None (or not positive)
        infers it with header_pitch(). A caller that knows the pitch of each
        row (castellated sides) passes it: the smallest distance between
        pins of DIFFERENT rows (e.g. two corner contacts) is not a pitch.
    @return List of dicts {"name", "role", "x", "y", "z", "dx", "dy", "dz"}:
        role "spacer" (plastic) or "pin" (metal); (x, y, z) is the minimum
        corner. Pin boxes are named "HeaderPin<number>", NOT "Pin<number>":
        the plated contact of the same hole already owns "Pin<number>", FreeCAD
        renamed the duplicate "Pin001", and the 3D preview reads any
        "Pin" + digits as a contact to label -- the pin numbers appeared twice.
    @note One spacer cell per pin, as wide as the pitch: neighbouring cells
        touch and read as the continuous plastic bar of a strip, whatever
        the rows look like, with no guess about how pins group into strips.
    """
    pitch = _resolved_pitch(pins, pitch)
    scale = pitch / REFERENCE_PITCH_MM
    spacer_h, long_tail = spacer_height(pitch), LONG_TAIL_MM * scale
    short_tail, side = SHORT_TAIL_MM * scale, PIN_SIDE_MM * scale
    spacer_z = board_bottom_z - spacer_h
    pin_bottom = spacer_z - long_tail
    pin_top = max(board_bottom_z + short_tail, board_top_z + MIN_TOP_PROTRUSION_MM)
    boxes = []
    for number, x, y in pins:
        x, y = float(x), float(y)
        boxes.append(dict(name=f"Header{number}", role="spacer",
                          x=x - pitch / 2, y=y - pitch / 2, z=spacer_z,
                          dx=pitch, dy=pitch, dz=spacer_h))
        boxes.append(dict(name=f"HeaderPin{number}", role="pin",
                          x=x - side / 2, y=y - side / 2, z=pin_bottom,
                          dx=side, dy=side, dz=pin_top - pin_bottom))
    return boxes


def male_header_boxes_by_row(pins, board_top_z: float, board_bottom_z: float = 0.0,
                             pitches=None):
    """@brief Male header boxes for pins whose rows may have different pitches.

    @param pins Sequence of (number, x, y) hole centres, mm.
    @param board_top_z Z of the board's top face, mm.
    @param board_bottom_z Z of the board's underside, mm.
    @param pitches None, or a sequence aligned with `pins` giving the declared
        pitch (mm) of the row each pin belongs to.
    @return The boxes of male_header_boxes(): with no `pitches` exactly that
        call (pitch inferred from all pins, the populated-board behaviour);
        otherwise one call per distinct declared pitch, so every row is drawn
        with its own pitch and never with a distance measured across rows.
    @throws ValueError When `pitches` is given with a length different from `pins`.
    """
    boxes = []
    for pitch, group in _pitch_groups(pins, pitches):
        boxes.extend(male_header_boxes(group, board_top_z, board_bottom_z, pitch=pitch))
    return boxes
