"""@brief Normalize CAD envelope semantics without reading files or measuring shapes.

The legacy CAD probe calls a horizontal section at 65% of the model height
``body``. That section can be a lens, actuator or shield rather than the full
package. This module labels that provenance and repairs only a narrow, declared
end-termination topology. It never derives dimensions from a package code.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from os import PathLike
from pathlib import PurePosixPath


# These complete KiCad identifiers describe individual rectangular end-terminated
# chips, not arbitrary LED/passive parts. Metric is required: vendor suffixes,
# arrays, RGB, side-view and reverse-mount variants are deliberately not matched.
# This is a naming-contract assumption, not verification of a custom STEP's
# contents. The plated endcaps count as part of the photographic chip body;
# they are not projecting leads. Numeric tokens select no nominal dimensions.
_ENDCAP_NAME = re.compile(
    r"(?P<family>R|C|LED)_\d{4,6}_\d{4}Metric(?:_Castellated)?", re.IGNORECASE
)
_ENDCAP_FOLDERS = {
    "R": "resistor_smd", "C": "capacitor_smd", "LED": "led_smd"
}


def _parts(path):
    # Pure parsing must also handle Windows paths on a non-Windows test host.
    parsed = PurePosixPath(str(path).replace("\\", "/"))
    name = parsed.name
    if parsed.suffix.lower() in (".step", ".stp", ".wrl"):
        name = name[: -len(parsed.suffix)]
    return name, parsed.parent.name.lower()


def is_standard_endcap_package(path: str | PathLike[str]) -> bool:
    """@brief Recognize the narrow standard single-chip endcap naming contract.

    @param path Model path or basename; no filesystem access is performed.
    @return True for a complete R/C/LED Metric identifier with optional Castellated.
    @note A canonical .3dshapes folder must agree with the declared family.
        Bare names and paths outside .3dshapes folders use the basename contract.
        Arbitrary custom .3dshapes libraries are not certified by their filenames.
    """
    name, folder = _parts(path)
    match = _ENDCAP_NAME.fullmatch(name)
    if not match:
        return False
    if folder.endswith(".3dshapes"):
        return folder.removesuffix(".3dshapes") == _ENDCAP_FOLDERS[match["family"].upper()]
    return True


def _dimensions(value):
    """Accept independent finite positive XY or XYZ measurements, never strings."""
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        return None
    try:
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in value):
            return None
        return [float(v) for v in value]
    except (ValueError, OverflowError):
        # JSON permits integers too large for a finite CAD floating-point value.
        return None


def _section_is_not_body(path):
    """Assemblies/optics/actuators have no body guarantee from one Z section."""
    name, folder = _parts(path)
    folder = folder.removesuffix(".3dshapes")
    return (folder.startswith(("led_", "button_switch", "connector_", "module"))
            or folder == "rf_module"
            or bool(re.match(r"(?:LED|SW|Module)[_-]", name, re.IGNORECASE)))


def normalize_model_measurement(path: str | PathLike[str], size: object) -> dict:
    """@brief Copy raw or legacy cached measurements into explicit envelope semantics.

    @param path Model path used only for structural naming classification.
    @param size Legacy total XY/XYZ sequence or mapping containing total/body readings.
    @return Independent mapping preserving metadata and adding body_method. Standard
        endcaps use a copy of total as body. Special LEDs, switches, connectors and
        modules have body=None. Other packages retain the legacy section, explicitly
        labelled section_65pct_unverified; this is not a proven body measurement.
    @note Apply to both freshly measured and cached data before dimensional matching.
        section_65pct preserves the diagnostic section but must not enter the normal
        list of interchangeable body/total readings. No cache writes are performed.
        This function is idempotent and never alters dimensions to fit a photograph.
        Callers must not treat an unverified body_method as independent evidence for
        automatic selection. A changed measurement contract needs cache migration or
        an index-version change at integration time, not in this helper.
    """
    result = deepcopy(size) if isinstance(size, dict) else {"total": deepcopy(size)}
    total = _dimensions(result.get("total"))
    old_body = _dimensions(result.get("body"))
    # Keep the original section across repeated normalization; do not relabel a
    # normalized endcap's full envelope as a newly measured horizontal section.
    section = _dimensions(result.get("section_65pct"))
    if "body_method" not in result and "section_65pct" not in result:
        section = old_body
    result["total"] = total
    result["section_65pct"] = section
    if is_standard_endcap_package(path):
        result["body"] = list(total) if total is not None else None
        result["body_method"] = "total_standard_endcap" if total is not None else "unavailable"
    elif _section_is_not_body(path):
        result["body"] = None
        result["body_method"] = "unresolved_nonuniform_package"
    else:
        result["body"] = section
        result["body_method"] = "section_65pct_unverified" if section is not None else "unavailable"
    return result
