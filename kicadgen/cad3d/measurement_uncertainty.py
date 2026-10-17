"""@brief Photographic measurement uncertainty shared by the 3D placement and model reuse.

A measurement taken from a calibrated photograph is worth as much as the
resolution it was taken with: the same 0.3 mm deviation is noise on a photo
at 10 px/mm and a real error on one at 80 px/mm. This module turns what is
known about the measurement (a declared uncertainty, or the photo scale in
px/mm) into millimetres, and declares the fallback when nothing is known.

The two consumers are:

* `step_generator` -- decides whether a body sticking out of the board edge
  really sticks out (left where it is, with a note) or whether the overrun
  is within the measurement error (moved back inside);
* `services.accepted_models` -- decides whether two photographic envelopes
  (the archived one and the new one) are the same size.

@note Units: millimetres for lengths, pixels per millimetre for scales.
@note The fallback is an ESTIMATE and every caller that uses it says so in
    its notes (`source == SOURCE_ASSUMED`).
"""
from __future__ import annotations

from ..core.validation import positive_number

#: Position uncertainty of ONE edge measured on the photo, in pixels.
#:
#: Derivation: a body's outline is located within the growth of the edge
#: mask (blur + 3x3 dilation, see `photo_refine.MORPH_GROWTH_PX`, 2 px per
#: side measured on synthetic rectangles). Even when that growth is
#: subtracted, the value remains the limit of what can be said about the
#: edge; a box proposed by the AI model is no more precise than that. It is
#: a DECLARED fallback: it would be made unnecessary by an uncertainty
#: estimated on the photo itself (measured edge noise), passed as
#: `measurement_uncertainty_mm`.
#: The value is repeated here, not imported, because this module also runs
#: where the photo pipeline (OpenCV) is not loaded.
EDGE_UNCERTAINTY_PX = 2.0

#: Fallback scale when the photo is not known, in px/mm.
#:
#: Derivation: it is the coarsest resolution at which the photo inventory
#: declares itself complete -- the short side of an 0402 passive
#: (0.5 mm) must cover at least 12 px (`photo_inventory.MIN_PACKAGE_PIXELS`),
#: i.e. 24 px/mm; below that, the inventory already warns it is incomplete.
#: Using it as a fallback gives the SMALLEST uncertainty compatible with an
#: inventory declared complete: a coarser photo has a larger error, and
#: in that case the fallback underestimates it (declared limit).
ASSUMED_SCALE_PX_PER_MM = 24.0

#: The uncertainty comes from a value declared on the component (mm).
SOURCE_DECLARED = "declared"
#: The uncertainty comes from the photo scale (px/mm).
SOURCE_PHOTO_SCALE = "photo_scale"
#: Nessun dato: ripiego dichiarato (ASSUMED_SCALE_PX_PER_MM).
SOURCE_ASSUMED = "assumed"

#: Component key holding the uncertainty already in mm, per edge.
UNCERTAINTY_KEY = "measurement_uncertainty_mm"
#: Component key holding the scale of the photo it was measured from.
SCALE_KEY = "photo_scale_px_per_mm"


def edge_uncertainty_mm(*, declared_mm=None, scale_px_per_mm=None) -> tuple[float, str]:
    """@brief Uncertainty of the position of one measured edge, in mm.

    @param declared_mm Uncertainty already expressed in mm (per edge), when a
        measurement declares it. Wins over the scale.
    @param scale_px_per_mm Scale of the calibrated photograph the edge was
        measured on.
    @return (uncertainty_mm, source) with source one of SOURCE_DECLARED,
        SOURCE_PHOTO_SCALE, SOURCE_ASSUMED.
    @note Invalid values (NaN, infinite, zero, negative, booleans) are treated
        as missing, never as a measurement.
    """
    declared = positive_number(declared_mm)
    if declared is not None:
        return declared, SOURCE_DECLARED
    scale = positive_number(scale_px_per_mm)
    if scale is not None:
        return EDGE_UNCERTAINTY_PX / scale, SOURCE_PHOTO_SCALE
    return EDGE_UNCERTAINTY_PX / ASSUMED_SCALE_PX_PER_MM, SOURCE_ASSUMED


def component_edge_uncertainty(component, default_scale_px_per_mm=None) -> tuple[float, str]:
    """@brief Edge uncertainty of one component, from its own fields or a board-wide scale.

    @param component Mapping (or object) that may carry UNCERTAINTY_KEY or
        SCALE_KEY, directly or inside `match_evidence`.
    @param default_scale_px_per_mm Scale of the board photograph, used when
        the component does not carry its own.
    @return (uncertainty_mm, source) as `edge_uncertainty_mm`.
    """
    def read(key):
        if isinstance(component, dict):
            value = component.get(key)
            evidence = component.get("match_evidence")
        else:
            value = getattr(component, key, None)
            evidence = getattr(component, "match_evidence", None)
        if value is None and isinstance(evidence, dict):
            value = evidence.get(key)
        return value

    scale = positive_number(read(SCALE_KEY))
    if scale is None:
        scale = positive_number(default_scale_px_per_mm)
    return edge_uncertainty_mm(declared_mm=read(UNCERTAINTY_KEY), scale_px_per_mm=scale)


def side_uncertainty_mm(edge_mm: float) -> float:
    """@brief Uncertainty of a measured side (two independent edges), in mm.

    @param edge_mm Uncertainty of one edge.
    @return 2 * edge_mm: a side is the distance between two edges, each
        known to within edge_mm (worst case, not quadrature, because the
        errors of a box proposal are not independent in practice).
    """
    return 2.0 * float(edge_mm)
