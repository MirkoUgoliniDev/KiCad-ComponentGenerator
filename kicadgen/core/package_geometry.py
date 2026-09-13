"""Geometria generica dei package e dei loro pad.

Il modello non presume che i pin siano una matrice: la matrice dei pin header
e' solo un caso particolare. Le coordinate sono locali al package e viste
dall'alto, cioe' nello stesso sistema usato da KiCad.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Iterable, Optional, Sequence


PACKAGE_FAMILIES = {
    "unknown", "dip", "sip", "soic", "tssop", "qfn", "qfp", "module",
    "castellated", "custom",
}
PAD_TYPES = {"smd", "thru_hole", "np_thru_hole", "connect"}
PAD_SHAPES = {"circle", "rect", "oval", "roundrect", "trapezoid", "custom"}


def _finite(value: float, name: str, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} deve essere numerico")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} deve essere numerico") from exc
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{name} deve essere {'>' if positive else 'finito'} valido")
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
            raise ValueError("il numero del pin non puo' essere vuoto")
        _finite(self.x_mm, "x_mm")
        _finite(self.y_mm, "y_mm")
        _finite(self.pad_length_mm, "pad_length_mm", True)
        _finite(self.pad_width_mm, "pad_width_mm", True)
        if self.pad_type not in PAD_TYPES:
            raise ValueError(f"pad_type non valido: {self.pad_type}")
        if self.shape not in PAD_SHAPES:
            raise ValueError(f"shape non valida: {self.shape}")
        if self.side is not None and self.side not in {"left", "right", "top", "bottom", "center"}:
            raise ValueError(f"lato non valido: {self.side}")
        _finite(self.rotation_deg, "rotation_deg")
        if self.pad_type in {"thru_hole", "np_thru_hole"}:
            if self.drill_mm is None:
                raise ValueError(f"il pin {self.number} richiede drill_mm")
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
            raise ValueError(f"famiglia package non supportata: {self.family}")
        if self.source_view not in {"top", "bottom"}:
            raise ValueError("source_view deve essere top o bottom")
        if self.origin not in {"body_center", "pin1", "custom"}:
            raise ValueError("origin non supportata")
        _finite(self.orientation_deg, "orientation_deg")
        if self.confidence not in {"high", "medium", "low"}:
            raise ValueError("confidence non valida")
        for key in ("body_length_mm", "body_width_mm", "body_height_mm"):
            value = getattr(self, key)
            if value is not None:
                _finite(value, key, True)
        numbers = set()
        rectangles = []
        for pin in self.pins:
            pin.validate()
            if pin.number in numbers:
                raise ValueError(f"pin duplicato: {pin.number}")
            numbers.add(pin.number)
            half_x, half_y = pin.pad_length_mm / 2, pin.pad_width_mm / 2
            rectangle = (pin.x_mm - half_x, pin.y_mm - half_y,
                         pin.x_mm + half_x, pin.y_mm + half_y)
            for other_number, other in rectangles:
                overlap_x = min(rectangle[2], other[2]) - max(rectangle[0], other[0])
                overlap_y = min(rectangle[3], other[3]) - max(rectangle[1], other[1])
                if overlap_x > 1e-6 and overlap_y > 1e-6:
                    raise ValueError(
                        f"pad sovrapposti tra i pin {other_number} e {pin.number}"
                    )
            rectangles.append((pin.number, rectangle))

    def pins_seen_from_top(self) -> list[PinSpec]:
        """Restituisce pin normalizzati dalla vista della fonte a quella KiCad."""
        self.validate()
        result = []
        angle = math.radians(self.orientation_deg % 360)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        for pin in self.pins:
            x, y = pin.x_mm, pin.y_mm
            if self.source_view == "bottom":
                x = -x
            if self.mirrored:
                x = -x
            rx, ry = x * cos_a - y * sin_a, x * sin_a + y * cos_a
            result.append(PinSpec(
                number=pin.number, x_mm=rx, y_mm=ry,
                pad_length_mm=pin.pad_length_mm, pad_width_mm=pin.pad_width_mm,
                side=pin.side, name=pin.name, pad_type=pin.pad_type,
                shape=pin.shape, rotation_deg=pin.rotation_deg + self.orientation_deg,
                drill_mm=pin.drill_mm,
            ))
        return result


def package_from_mapping(data: dict) -> PackageGeometry:
    """Costruisce e valida il modello dal JSON restituito dal tool AI."""
    if not isinstance(data, dict):
        raise ValueError("geometria package non strutturata")
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
