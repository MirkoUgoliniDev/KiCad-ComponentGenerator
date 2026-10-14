"""@brief Derive host-board lands without changing the photographed module geometry."""

from dataclasses import replace
from math import isfinite

from ..core.package_geometry import PackageGeometry, PinSpec, pad_outline, polygon_separation
from ..services.messages import tx
from .castellated_module import CastellatedModuleParams, pad_positions, MIN_COPPER_GAP_MM
from .pin_header import PinHeaderParams, pad_copper_gap


def _land_gap(pin, other) -> float:
    """Free copper between two square-sized host lands, mm, from their real shape.

    Round lands ("circle", or "oval" with equal sides, which KiCad draws as a
    circle) and axis-aligned square lands use the exact geometry shared with
    the header generator (`pad_copper_gap`). A square land rotated by an angle
    that is not a multiple of 90 degrees falls back to the separating-axis gap
    of the rotated outlines, a lower bound of the true distance: it can only
    refuse more, never accept touching copper.
    """
    def is_round(p):
        return p.shape in {"circle", "oval"}

    def axis_aligned(p):
        return is_round(p) or float(p.rotation_deg) % 90.0 == 0.0

    if axis_aligned(pin) and axis_aligned(other):
        return pad_copper_gap(
            float(pin.x_mm), float(pin.y_mm), "circle" if is_round(pin) else "rect",
            float(pin.pad_length_mm) / 2,
            float(other.x_mm), float(other.y_mm), "circle" if is_round(other) else "rect",
            float(other.pad_length_mm) / 2)
    return polygon_separation(
        pad_outline(pin.x_mm, pin.y_mm, pin.pad_length_mm, pin.pad_width_mm, pin.rotation_deg),
        pad_outline(other.x_mm, other.y_mm, other.pad_length_mm, other.pad_width_mm,
                    other.rotation_deg))


def _validate_header_clearance(pins):
    """Reject host lands, as emitted (overrides applied), closer than MIN_COPPER_GAP_MM."""
    for index, pin in enumerate(pins):
        for other in pins[index + 1:]:
            if _land_gap(pin, other) < MIN_COPPER_GAP_MM - 1e-9:
                raise ValueError(tx("Le piazzole degli header sono troppo grandi per la distanza fra i contatti."))


def header_footprint_params(params, pad_diameter=1.7, drill=1.0, shape="circle",
                           pad_overrides=None):
    """@brief Build independent host lands at the module's closed-hole centres.

    @param params Physical module parameters, never modified.
    @param pad_diameter Host copper diameter in mm, chosen by the user.
    @param drill Host drill diameter in mm, chosen for the header pins.
    @param shape Host copper shape (circle or square/rect).
    @return Independent parameters used only by footprint export and 2D preview.
    @throws ValueError For missing closed holes, invalid sizes or overlapping lands.
    """
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not isfinite(v) or v <= 0 for v in (pad_diameter, drill)):
        raise ValueError(tx("Dimensione pad e foro degli header devono essere positivi."))
    if shape not in {"circle", "rect"}:
        raise ValueError(tx("Forma pad header non valida."))
    if drill >= pad_diameter:
        raise ValueError(tx("Il foro degli header deve essere minore del lato/dimensione del pad."))
    if isinstance(params, PinHeaderParams):
        result = replace(params, pad_diameter=pad_diameter, drill=drill,
                         pad_shape=shape, pad_overrides=dict(pad_overrides or {}))
        result.validate()
        return result
    if isinstance(params, PackageGeometry):
        params.validate()
        electrical = [pin for pin in params.pins if pin.pad_type != "np_thru_hole"]
        if not electrical:
            raise ValueError(tx("Il montaggio con header richiede una scheda con fori passanti."))
        for pin in electrical:
            if pin.pad_type != "thru_hole":
                raise ValueError(tx("Il contatto {number} non ha un foro passante per gli header.",
                                    number=pin.number))
        # Preserve the declared source frame, numbering and mechanical holes.
        # Only already-drilled electrical lands receive the host dimensions.
        result_pins = []
        for pin in params.pins:
            if pin.pad_type == "np_thru_hole":
                result_pins.append(pin)
                continue
            override = (pad_overrides or {}).get(str(pin.number), {})
            size = float(override.get("size", pad_diameter))
            result_pins.append(replace(
                pin, pad_length_mm=size, pad_width_mm=size,
                shape=override.get("shape", shape), drill_mm=drill))
        _validate_header_clearance([pin for pin in result_pins if pin.pad_type != "np_thru_hole"])
        result = replace(params, pins=result_pins)
        result.validate()
        return result
    if not isinstance(params, CastellatedModuleParams):
        raise ValueError(tx("Il montaggio con header richiede una scheda con fori passanti."))
    params.validate()
    pins = []
    for placement in pad_positions(params):
        geometry = params.contact_geometry(placement)
        if not geometry.has_thru_holes:
            raise ValueError(tx("Il contatto {number} non ha un foro passante per gli header.",
                                number=placement.number))
        x, y = params.thru_hole_centre(placement)
        override = (pad_overrides or {}).get(str(placement.number), {})
        size = float(override.get("size", pad_diameter))
        pins.append(PinSpec(number=placement.number, x_mm=x, y_mm=y,
                            pad_length_mm=size, pad_width_mm=size,
                            side=placement.edge, name=params.silk_labels.get(placement.number, ""),
                            pad_type="thru_hole", shape=override.get("shape", shape),
                            drill_mm=drill))
    _validate_header_clearance(pins)
    result = PackageGeometry(name=params.name, family="module",
                             body_length_mm=params.body_length,
                             body_width_mm=params.body_width,
                             confidence="high", needs_review=False, pins=pins)
    result.validate()
    return result
