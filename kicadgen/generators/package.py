"""Generatore di footprint KiCad da coordinate esplicite dei pad."""

from __future__ import annotations

from ..core.footprint import Footprint, FpCircle, FpRect, Pad
from ..core.package_geometry import PackageGeometry


def build_package(geometry: PackageGeometry, name: str | None = None) -> Footprint:
    geometry.validate()
    pins = geometry.pins_seen_from_top()
    attr = "through_hole" if any(p.pad_type == "thru_hole" for p in pins) else "smd"
    footprint_name = name or geometry.name
    fp = Footprint(name=footprint_name, description=f"{geometry.family} package generico", tags=geometry.family, attr=attr)
    if geometry.body_length_mm and geometry.body_width_mm:
        x, y = geometry.body_length_mm / 2, geometry.body_width_mm / 2
        fp.add_graphic(FpRect(start=(-x, -y), end=(x, y), layer="F.SilkS"))
    for pin in pins:
        layers = ["*.Cu", "*.Mask"] if pin.pad_type == "thru_hole" else ["F.Cu", "F.Paste", "F.Mask"]
        fp.add_pad(Pad(
            number=pin.number, pad_type=pin.pad_type, shape=pin.shape,
            at=(pin.x_mm, pin.y_mm), size=(pin.pad_length_mm, pin.pad_width_mm),
            layers=layers, rotation=pin.rotation_deg, drill=pin.drill_mm,
            roundrect_rratio=0.25 if pin.shape == "roundrect" else None,
        ))
    return fp
