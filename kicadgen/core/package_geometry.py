"""@brief Generic package and pad geometry, not limited to pin matrices.

Geometria generica dei package e dei loro pad.

Il modello non presume che i pin siano una matrice: la matrice dei pin header
e' solo un caso particolare. Le coordinate sono locali al package e viste
dall'alto, cioe' nello stesso sistema usato da KiCad.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Optional, Sequence
from ..services.messages import tx


PACKAGE_FAMILIES = {
    "unknown", "dip", "sip", "soic", "tssop", "qfn", "qfp", "module",
    "castellated", "custom",
}
PAD_TYPES = {"smd", "thru_hole", "np_thru_hole", "connect"}
# Custom pads require an anchor and primitives, which PinSpec cannot describe.
PAD_SHAPES = {"circle", "rect", "oval", "roundrect", "trapezoid"}

#: Declared pad-coordinate origins. Only "body_center" says where the body is;
#: for the others the body centre is estimated (see PackageGeometry.body_frame_offset).
ORIGINS = {"body_center", "pin1", "custom"}


def _finite(value: float, name: str, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(tx("{name} deve essere numerico", name=name))
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(tx("{name} deve essere numerico", name=name)) from exc
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(tx("{name} deve essere {value} valido",
                            name=name,
                            value='>' if positive else 'finito'))
    return value


@dataclass(frozen=True)
class PinSpec:
    """@brief Describe one package pin and its physical pad geometry."""
    number: str
    x_mm: float
    y_mm: float
    pad_length_mm: float
    pad_width_mm: float
    side: Optional[str] = None
    name: str = ""
    pad_type: str = "smd"
    shape: str = "roundrect"
    rotation_deg: float = 0.0
    drill_mm: Optional[float] = None

    def validate(self) -> None:
        """@brief Validate the object against its supported contract.

        @throws ValueError When a value is out of contract.
        @note An empty number is accepted only for a non-plated hole: KiCad
            writes mechanical holes with number "" (no net), while every
            electrical pad needs a number to be connected.
        """
        if not str(self.number).strip() and self.pad_type != "np_thru_hole":
            raise ValueError(tx("il numero del pin non puo' essere vuoto"))
        _finite(self.x_mm, "x_mm")
        _finite(self.y_mm, "y_mm")
        _finite(self.pad_length_mm, "pad_length_mm", True)
        _finite(self.pad_width_mm, "pad_width_mm", True)
        if self.pad_type not in PAD_TYPES:
            raise ValueError(tx("pad_type non valido: {pad_type}", pad_type=self.pad_type))
        if self.shape not in PAD_SHAPES:
            raise ValueError(tx("shape non valida: {shape}", shape=self.shape))
        if self.side is not None and self.side not in {"left", "right", "top", "bottom", "center"}:
            raise ValueError(tx("lato non valido: {side}", side=self.side))
        _finite(self.rotation_deg, "rotation_deg")
        if self.pad_type in {"thru_hole", "np_thru_hole"}:
            if self.drill_mm is None:
                raise ValueError(tx("il pin {number} richiede drill_mm", number=self.number))
            _finite(self.drill_mm, "drill_mm", True)
            if (self.pad_type == "thru_hole"
                    and float(self.drill_mm) >= min(float(self.pad_length_mm), float(self.pad_width_mm))):
                raise ValueError(tx("Il foro del pin {number} deve essere minore di entrambe le dimensioni del pad.",
                                    number=self.number))


@dataclass
class PackageGeometry:
    """@brief Describe a package body and its ordered pins."""
    name: str = "Package"
    family: str = "unknown"
    body_length_mm: Optional[float] = None
    body_width_mm: Optional[float] = None
    body_height_mm: Optional[float] = None
    origin: str = "body_center"
    source_view: str = "top"
    orientation_deg: float = 0.0
    mirrored: bool = False
    confidence: str = "low"
    needs_review: bool = True
    pins: list[PinSpec] = field(default_factory=list)

    def validate(self) -> None:
        """@brief Validate the object against its supported contract.

        @throws ValueError When a field is out of contract, two pads overlap,
            or pads sharing a number declare different pin names.
        @note Several pads may share one number, as KiCad allows (a thermal
            pad split into several lands, several GND pads): they are the same
            electrical pin. What is refused is a shared number with
            CONFLICTING non-empty names, the structural sign of a misread
            number rather than of a multi-pad pin.
        """
        if self.family not in PACKAGE_FAMILIES:
            raise ValueError(tx("famiglia package non supportata: {family}", family=self.family))
        if self.source_view not in {"top", "bottom"}:
            raise ValueError(tx("source_view deve essere top o bottom"))
        if self.origin not in ORIGINS:
            raise ValueError(tx("origin non supportata"))
        # bool("false") is True: only a real boolean may decide a mirror.
        if not isinstance(self.mirrored, bool):
            raise ValueError(tx("mirrored deve essere true o false"))
        _finite(self.orientation_deg, "orientation_deg")
        if self.confidence not in {"high", "medium", "low"}:
            raise ValueError(tx("confidence non valida"))
        for key in ("body_length_mm", "body_width_mm", "body_height_mm"):
            value = getattr(self, key)
            if value is not None:
                _finite(value, key, True)
        # Il modello esiste per descrivere pad espliciti: senza pad non c'e'
        # footprint da scrivere, e un .kicad_mod vuoto passerebbe per buono.
        if not self.pins:
            raise ValueError(tx("la geometria package non ha pad"))
        names: dict = {}
        outlines = []
        for pin in self.pins:
            pin.validate()
            name = str(pin.name or "").strip()
            known = names.get(pin.number)
            if known and name and known.casefold() != name.casefold():
                raise ValueError(tx("pin duplicato: {number}", number=pin.number))
            names[pin.number] = known or name
            # Rettangolo ruotato del pad (convenzione KiCad): un controllo su
            # rettangoli allineati agli assi rifiuterebbe pad di QFN ruotati
            # che non si toccano e accetterebbe pad ruotati che si toccano.
            outline = pad_outline(pin.x_mm, pin.y_mm, pin.pad_length_mm,
                                  pin.pad_width_mm, pin.rotation_deg)
            for other_pin, other in outlines:
                # Circular host-header lands can sit diagonally at a board
                # corner: their bounding squares may overlap while the copper
                # rings remain separated. Use the actual circles for this pair.
                # Keep the conservative polygon check for all other shapes.
                circles = all(p.shape == "circle" and
                              float(p.pad_length_mm) == float(p.pad_width_mm)
                              for p in (pin, other_pin))
                separation = (
                    math.hypot(float(pin.x_mm) - float(other_pin.x_mm),
                               float(pin.y_mm) - float(other_pin.y_mm))
                    - (float(pin.pad_length_mm) + float(other_pin.pad_length_mm)) / 2
                    if circles else polygon_separation(outline, other)
                )
                if separation <= 1e-6:
                    raise ValueError(
                        tx("pad sovrapposti tra i pin {other_number} e {number}",
                           other_number=other_pin.number,
                           number=pin.number)
                    )
            outlines.append((pin, outline))

    def body_frame_offset(self) -> tuple:
        """@brief Point of the pad coordinates taken as the body centre, mm.

        @return (x, y) in the source-view pad coordinates; (0, 0) when the
            origin is declared "body_center".
        @note DECLARED HEURISTIC. With origin "pin1" or "custom" the pads are
            given relative to a point that is not the body centre, and nothing
            in the geometry says where the body is relative to that point. The
            body is then assumed to be centred on the bounding box of the pad
            centres, which holds for the symmetric families (DIP, SOIC, TSSOP,
            QFN, QFP) and is only an estimate for asymmetric parts (connectors,
            parts with an offset tab). What would make it unnecessary: a
            declared body offset from the pad origin in the extraction schema.
            `frame_notes` states the estimate so it is never silent.
        """
        if self.origin == "body_center" or not self.pins:
            return (0.0, 0.0)
        # Mounting holes are not part of the pin field: an asymmetric hole
        # would drag the estimated body centre (all pads count only when the
        # geometry has nothing but holes).
        field = [pin for pin in self.pins if pin.pad_type != "np_thru_hole"] or self.pins
        xs = [float(pin.x_mm) for pin in field]
        ys = [float(pin.y_mm) for pin in field]
        return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)

    def frame_notes(self) -> list:
        """@brief Notes about estimated placement of the body relative to the pads.

        @return Localized notes; empty when the origin is the body centre.
        """
        if self.origin == "body_center":
            return []
        return [tx("Geometria package con origine «{origin}»: il centro del corpo e' "
                   "stimato al centro del campo dei pad (stima, da verificare sul "
                   "disegno quotato).", origin=self.origin)]

    def pins_seen_from_top(self) -> list[PinSpec]:
        """@brief Return the pins normalized from the source view to KiCad's top view.

        @return New PinSpec objects in footprint coordinates (mm, Y down) whose
            `rotation_deg` is the KiCad pad angle.
        @note Every angle follows KiCad's convention: degrees counter-clockwise
            as seen on screen, with Y pointing down, which is the direction
            KiCad rotates a pad written as `(at x y angle)` (verified with
            pcbnew 10: angle 30 turns the local +X axis to (cos30, -sin30)).
            Positions and pad angles are rotated by the SAME convention, so a
            pad keeps its alignment to its own position at any angle, not only
            at multiples of 90 degrees.
        @note A bottom view (and an explicit mirror) is a reflection across
            the Y axis: it negates X and, with it, every pad angle; the package
            orientation is applied after the reflection.
        @note The footprint origin is always the (possibly estimated) body
            centre: pads declared from pin 1 or a custom origin are first
            moved by `body_frame_offset`, so body outline, courtyard and 3D
            model, all drawn around (0, 0), line up with the pads.
        """
        self.validate()
        result = []
        angle = math.radians(self.orientation_deg % 360)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        flips = int(self.source_view == "bottom") + int(self.mirrored)
        shift_x, shift_y = self.body_frame_offset()
        for pin in self.pins:
            x, y = pin.x_mm, pin.y_mm
            if self.origin != "body_center":
                x, y = round(x - shift_x, 9), round(y - shift_y, 9)
            pad_angle = pin.rotation_deg
            if flips % 2:
                x, pad_angle = -x, -pad_angle
            rx, ry = _rotate_kicad(x, y, cos_a, sin_a)
            result.append(PinSpec(
                number=pin.number, x_mm=rx + 0.0, y_mm=ry + 0.0,
                pad_length_mm=pin.pad_length_mm, pad_width_mm=pin.pad_width_mm,
                side=pin.side, name=pin.name, pad_type=pin.pad_type,
                shape=pin.shape, rotation_deg=_normal_angle(pad_angle + self.orientation_deg),
                drill_mm=pin.drill_mm,
            ))
        return result


def _rotate_kicad(x: float, y: float, cos_a: float, sin_a: float) -> tuple:
    """Rotate (x, y) counter-clockwise on screen (Y down), as KiCad rotates a pad."""
    return (x * cos_a + y * sin_a, -x * sin_a + y * cos_a)


def _normal_angle(angle: float) -> float:
    """Map an angle in degrees to [0, 360) with no float noise and no -0."""
    value = round(float(angle) % 360.0, 9)
    return 0.0 if value in (0.0, 360.0) else value + 0.0


def pad_outline(x: float, y: float, size_x: float, size_y: float,
                rotation_deg: float = 0.0) -> list:
    """@brief Corners of a rotated rectangular pad, in mm.

    @param x Pad centre X, mm.
    @param y Pad centre Y, mm (KiCad axes, Y down).
    @param size_x Pad size along its local X, mm.
    @param size_y Pad size along its local Y, mm.
    @param rotation_deg KiCad pad angle (counter-clockwise on screen), degrees.
    @return Four (x, y) corners in order. For round or rounded shapes this is
        the bounding rectangle, a conservative envelope of the copper.
    """
    angle = math.radians(rotation_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    corners = []
    for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        rx, ry = _rotate_kicad(dx * size_x / 2, dy * size_y / 2, cos_a, sin_a)
        corners.append((x + rx, y + ry))
    return corners


def polygon_separation(first: Sequence, second: Sequence) -> float:
    """@brief Largest gap between two convex polygons along their edge normals, mm.

    @param first Corners of a convex polygon, in order.
    @param second Corners of a convex polygon, in order.
    @return A positive value when a separating axis exists (the polygons are
        apart by at least that much along it), zero when they touch, negative
        when they overlap (separating axis theorem).
    """
    best = -math.inf
    for polygon in (first, second):
        count = len(polygon)
        for i in range(count):
            (ax, ay), (bx, by) = polygon[i], polygon[(i + 1) % count]
            nx, ny = by - ay, ax - bx
            norm = math.hypot(nx, ny)
            if norm == 0:
                continue
            nx, ny = nx / norm, ny / norm
            proj_a = [px * nx + py * ny for px, py in first]
            proj_b = [px * nx + py * ny for px, py in second]
            gap = max(min(proj_b) - max(proj_a), min(proj_a) - max(proj_b))
            best = max(best, gap)
    return best


def package_from_mapping(data: dict) -> PackageGeometry:
    """@brief Build and validate a PackageGeometry from the AI tool JSON; raise ValueError when invalid.

    Costruisce e valida il modello dal JSON restituito dal tool AI.
    """
    if not isinstance(data, dict):
        raise ValueError(tx("geometria package non strutturata"))
    raw_pins = data.get("pins") or []
    pins = [PinSpec(
        number=str(p["number"]), x_mm=p["x_mm"], y_mm=p["y_mm"],
        pad_length_mm=p["pad_length_mm"], pad_width_mm=p["pad_width_mm"],
        side=p.get("side"), name=str(p.get("name") or ""),
        pad_type=p.get("pad_type", "smd"), shape=p.get("shape", "roundrect"),
        rotation_deg=p.get("rotation_deg", 0), drill_mm=p.get("drill_mm"),
    ) for p in raw_pins]
    geometry = PackageGeometry(
        name=str(data.get("name") or "Package"),
        family=data.get("family", "unknown"), body_length_mm=data.get("body_length_mm"),
        body_width_mm=data.get("body_width_mm"), body_height_mm=data.get("body_height_mm"),
        origin=data.get("origin", "body_center"), source_view=data.get("source_view", "top"),
        orientation_deg=data.get("orientation_deg", 0),
        # Optional in the schema: an explicit null means «not mirrored», as
        # before; only a non-boolean value is refused (validate).
        mirrored=False if data.get("mirrored") is None else data.get("mirrored"),
        confidence=data.get("confidence", "low"), needs_review=data.get("needs_review", True), pins=pins,
    )
    geometry.validate()
    return geometry
