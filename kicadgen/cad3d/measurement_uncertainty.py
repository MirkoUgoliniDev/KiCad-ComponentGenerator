"""@brief Photographic measurement uncertainty shared by the 3D placement and model reuse.

Una misura presa da una fotografia calibrata vale quanto la risoluzione con
cui e' stata presa: lo stesso scarto di 0.3 mm e' rumore su una foto a 10
px/mm e un errore vero su una a 80 px/mm. Questo modulo trasforma cio' che si
sa della misura (un'incertezza dichiarata, oppure la scala della foto in
px/mm) in millimetri, e dichiara il ripiego quando non si sa nulla.

I due consumatori sono:

* `step_generator` -- decide se un corpo che sporge dal bordo della scheda
  sporge davvero (lasciato dov'e', con una nota) o se lo sforamento e'
  dentro l'errore di misura (riportato dentro);
* `services.accepted_models` -- decide se due ingombri fotografici (quello
  archiviato e quello nuovo) sono la stessa taglia.

@note Units: millimetres for lengths, pixels per millimetre for scales.
@note The fallback is an ESTIMATE and every caller that uses it says so in
    its notes (`source == SOURCE_ASSUMED`).
"""
from __future__ import annotations

import math
from typing import Optional

#: Incertezza di posizione di UN bordo misurato sulla foto, in pixel.
#:
#: Derivazione: il contorno di un corpo viene localizzato entro la crescita
#: della maschera dei bordi (sfocatura + dilatazione 3x3, vedi
#: `photo_refine.MORPH_GROWTH_PX`, 2 px per lato misurati su rettangoli
#: sintetici). Anche quando quella crescita viene sottratta, il valore resta
#: il limite di cio' che si puo' dire del bordo; un riquadro proposto dal
#: modello AI non e' piu' preciso di cosi'. E' un ripiego DICHIARATO: lo
#: renderebbe superfluo un'incertezza stimata sulla foto stessa (rumore di
#: bordo misurato), passata come `measurement_uncertainty_mm`.
#: Il valore e' ripetuto qui, non importato, perche' questo modulo gira anche
#: dove la pipeline fotografica (OpenCV) non viene caricata.
EDGE_UNCERTAINTY_PX = 2.0

#: Scala di ripiego quando la foto non e' nota, in px/mm.
#:
#: Derivazione: e' la risoluzione piu' grossolana alla quale l'inventario
#: fotografico si dichiara completo -- il lato corto di un passivo 0402
#: (0.5 mm) deve coprire almeno 12 px (`photo_inventory.MIN_PACKAGE_PIXELS`),
#: cioe' 24 px/mm; sotto, l'inventario avvisa gia' che e' incompleto. Usarla
#: come ripiego da' l'incertezza PIU' PICCOLA compatibile con un inventario
#: dichiarato completo: una foto piu' grossolana ha un errore piu' grande, e
#: in quel caso il ripiego lo sottostima (limite dichiarato).
ASSUMED_SCALE_PX_PER_MM = 24.0

#: L'incertezza viene da un valore dichiarato sul componente (mm).
SOURCE_DECLARED = "declared"
#: L'incertezza viene dalla scala della foto (px/mm).
SOURCE_PHOTO_SCALE = "photo_scale"
#: Nessun dato: ripiego dichiarato (ASSUMED_SCALE_PX_PER_MM).
SOURCE_ASSUMED = "assumed"

#: Chiave del componente con l'incertezza gia' in mm, per bordo.
UNCERTAINTY_KEY = "measurement_uncertainty_mm"
#: Chiave del componente con la scala della foto da cui e' stato misurato.
SCALE_KEY = "photo_scale_px_per_mm"


def _positive(value) -> Optional[float]:
    """Finite positive number, else None (booleans are not measurements)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


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
    declared = _positive(declared_mm)
    if declared is not None:
        return declared, SOURCE_DECLARED
    scale = _positive(scale_px_per_mm)
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

    scale = _positive(read(SCALE_KEY))
    if scale is None:
        scale = _positive(default_scale_px_per_mm)
    return edge_uncertainty_mm(declared_mm=read(UNCERTAINTY_KEY), scale_px_per_mm=scale)


def side_uncertainty_mm(edge_mm: float) -> float:
    """@brief Uncertainty of a measured side (two independent edges), in mm.

    @param edge_mm Uncertainty of one edge.
    @return 2 * edge_mm: a side is the distance between two edges, each
        known to within edge_mm (worst case, not quadrature, because the
        errors of a box proposal are not independent in practice).
    """
    return 2.0 * float(edge_mm)
