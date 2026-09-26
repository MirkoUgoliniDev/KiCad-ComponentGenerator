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


def male_header_boxes(pins, board_top_z: float, board_bottom_z: float = 0.0,
                      pitch=None):
    """@brief Boxes of a male header under each pin: one spacer cell and one pin.

    @param pins Sequence of (number, x, y) hole centres, mm.
    @param board_top_z Z of the board's top face, mm.
    @param board_bottom_z Z of the board's underside, mm (the soldering plane).
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
    if pitch is None or not float(pitch) > 0:
        pitch = header_pitch(pins)
    pitch = float(pitch)
    scale = pitch / REFERENCE_PITCH_MM
    spacer_h, long_tail = SPACER_HEIGHT_MM * scale, LONG_TAIL_MM * scale
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
    pins = list(pins)
    if pitches is None:
        return male_header_boxes(pins, board_top_z, board_bottom_z)
    pitches = list(pitches)
    if len(pitches) != len(pins):
        raise ValueError("pitches must be aligned with pins")
    groups = {}
    for pin, pitch in zip(pins, pitches):
        groups.setdefault(float(pitch), []).append(pin)
    boxes = []
    for pitch, group in groups.items():
        boxes.extend(male_header_boxes(group, board_top_z, board_bottom_z, pitch=pitch))
    return boxes
