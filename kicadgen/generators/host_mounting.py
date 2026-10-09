"""@brief Derive host-board lands without changing the photographed module geometry."""

from dataclasses import replace
from math import hypot, isfinite

from ..core.package_geometry import PackageGeometry, PinSpec
from ..services.messages import tx
from .castellated_module import CastellatedModuleParams, pad_positions, MIN_COPPER_GAP_MM
from .pin_header import PinHeaderParams


def header_footprint_params(params, pad_diameter=1.7, drill=1.0):
    """@brief Build independent host lands at the module's closed-hole centres.

    @param params Physical module parameters, never modified.
    @param pad_diameter Host copper diameter in mm, chosen by the user.
    @param drill Host drill diameter in mm, chosen for the header pins.
    @return Independent parameters used only by footprint export and 2D preview.
    @throws ValueError For missing closed holes, invalid sizes or overlapping lands.
    """
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not isfinite(v) or v <= 0 for v in (pad_diameter, drill)):
        raise ValueError(tx("Diametro pad e foro degli header devono essere positivi."))
    if drill >= pad_diameter:
        raise ValueError(tx("Il foro degli header deve essere minore del diametro del pad."))
    if isinstance(params, PinHeaderParams):
        result = replace(params, pad_diameter=pad_diameter, drill=drill)
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
        pins.append(PinSpec(number=placement.number, x_mm=x, y_mm=y,
                            pad_length_mm=pad_diameter, pad_width_mm=pad_diameter,
                            side=placement.edge, name=params.silk_labels.get(placement.number, ""),
                            pad_type="thru_hole", shape="circle", drill_mm=drill))
    for index, pin in enumerate(pins):
        for other in pins[index + 1:]:
            if hypot(pin.x_mm - other.x_mm, pin.y_mm - other.y_mm) < pad_diameter + MIN_COPPER_GAP_MM - 1e-9:
                raise ValueError(tx("Le piazzole degli header sono troppo grandi per la distanza fra i contatti."))
    result = PackageGeometry(name=params.name, family="module",
                             body_length_mm=params.body_length,
                             body_width_mm=params.body_width,
                             confidence="high", needs_review=False, pins=pins)
    result.validate()
    return result
