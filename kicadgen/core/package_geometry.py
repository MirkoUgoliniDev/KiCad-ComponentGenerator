"""@brief Generic package and pad geometry, not limited to pin matrices.

Geometria generica dei package e dei loro pad.

Il modello non presume che i pin siano una matrice: la matrice dei pin header
e' solo un caso particolare. Le coordinate sono locali al package e viste
dall'alto, cioe' nello stesso sistema usato da KiCad.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Iterable, Optional, Sequence
from ..services.messages import tx


PACKAGE_FAMILIES = {
    "unknown", "dip", "sip", "soic", "tssop", "qfn", "qfp", "module",
    "castellated", "custom",
}
PAD_TYPES = {"smd", "thru_hole", "np_thru_hole", "connect"}
PAD_SHAPES = {"circle", "rect", "oval", "roundrect", "trapezoid", "custom"}


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
        """@brief Validate the object against its supported contract."""
        if not str(self.number).strip():
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
        """@brief Validate the object against its supported contract."""
        if self.family not in PACKAGE_FAMILIES:
            raise ValueError(tx("famiglia package non supportata: {family}", family=self.family))
        if self.source_view not in {"top", "bottom"}:
            raise ValueError(tx("source_view deve essere top o bottom"))
        if self.origin not in {"body_center", "pin1", "custom"}:
            raise ValueError(tx("origin non supportata"))
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
        numbers = set()
        outlines = []
        for pin in self.pins:
            pin.validate()
            if pin.number in numbers:
                raise ValueError(tx("pin duplicato: {number}", number=pin.number))
            numbers.add(pin.number)
            # Rettangolo ruotato del pad (convenzione KiCad): un controllo su
            # rettangoli allineati agli assi rifiuterebbe pad di QFN ruotati
            # che non si toccano e accetterebbe pad ruotati che si toccano.
            outline = pad_outline(pin.x_mm, pin.y_mm, pin.pad_length_mm,
                                  pin.pad_width_mm, pin.rotation_deg)
            for other_number, other in outlines:
                if polygon_separation(outline, other) <= 1e-6:
                    raise ValueError(
                        tx("pad sovrapposti tra i pin {other_number} e {number}",
                           other_number=other_number,
                           number=pin.number)
                    )
            outlines.append((pin.number, outline))

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
        """
        self.validate()
        result = []
        angle = math.radians(self.orientation_deg % 360)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        flips = int(self.source_view == "bottom") + int(bool(self.mirrored))
        for pin in self.pins:
            x, y = pin.x_mm, pin.y_mm
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
        orientation_deg=data.get("orientation_deg", 0), mirrored=data.get("mirrored", False),
        confidence=data.get("confidence", "low"), needs_review=data.get("needs_review", True), pins=pins,
    )
    geometry.validate()
    return geometry
