"""@brief Test conservative CAD-body semantics independently of matcher integration."""

from copy import deepcopy
from pathlib import Path

import pytest

from kicadgen.cad3d.model_measurement import (
    is_standard_endcap_package,
    normalize_model_measurement,
)
from kicadgen.cad3d.kicad_models import DEFAULT_SIZE_TOLERANCE, size_distance


@pytest.mark.parametrize("name", [
    "R_0402_1005Metric", "C_0603_1608Metric.step", "LED_0805_2012Metric.step",
    "LED_1206_3216Metric_Castellated.step", "LED_01005_0402Metric",
    "R_008004_0201Metric", "led_0805_2012metric_castellated.STP",
    r"C:\KiCad\LED_SMD.3dshapes\LED_0805_2012Metric.step",
    "/models/Capacitor_SMD.3dshapes/C_0603_1608Metric.step",
    Path("Resistor_SMD.3dshapes/R_0402_1005Metric.step"),
])
def test_standard_endcap_names(name):
    assert is_standard_endcap_package(name)


@pytest.mark.parametrize("name", [
    "LED_RGB_1210.step", "LED_0805_2012Metric_SideView.step",
    "LED_0805_2012Metric_ReverseMount.step", "LED_0805_2012Metric_Array.step",
    "Vendor_LED_0805_2012Metric.step", "R_0603.step", "0805_2012Metric.step",
    "C_0603_1608Metric_4P.step", "SW_LED_0805_2012Metric.step",
    "Custom.3dshapes/LED_0805_2012Metric.step",
    "LED_THT.3dshapes/LED_0805_2012Metric.step",
    "Resistor_Array_SMD.3dshapes/R_0402_1005Metric.step",
    "Connector_USB.3dshapes/R_0402_1005Metric.step",
    "Capacitor_SMD.3dshapes/R_0402_1005Metric.step",
    "LED_0805_2012Metric.step.backup",
])
def test_nonstandard_or_contradictory_topologies_are_not_promoted(name):
    assert not is_standard_endcap_package(name)


@pytest.mark.parametrize("name,total,section", [
    ("R_0402_1005Metric", [1, .5, .35], [.9, .45, .35]),
    ("C_0603_1608Metric", [1.6, .8, .8], [1.3, .7, .8]),
    ("LED_0805_2012Metric", [2, 1.25, 1.1], [1.2488188976378232, 1.25, 1.1]),
    # Arbitrary test dimensions demonstrate that no nominal code-to-mm table is used.
    ("LED_0805_2012Metric_Castellated", [2.17, 1.39, .93], [.8, .7, .93]),
])
def test_endcaps_use_measured_total_not_code_or_section(name, total, section):
    raw = {"total": total, "body": section, "land_patterns": [{"size": [3, 2]}]}
    before = deepcopy(raw)
    result = normalize_model_measurement(name, raw)
    assert result["body"] == result["total"] == total
    assert result["body_method"] == "total_standard_endcap"
    assert result["section_65pct"] == section
    assert result["body"] is not result["total"]
    assert result["body"] is not total
    assert result["land_patterns"] == raw["land_patterns"]
    assert result["land_patterns"] is not raw["land_patterns"]
    assert raw == before
    assert normalize_model_measurement(name, result) == result


@pytest.mark.parametrize("suffix", ["", "_Castellated"])
def test_real_0805_d2_positive_but_1206_d1_negative(suffix):
    small = normalize_model_measurement("LED_0805_2012Metric" + suffix, {
        "total": [2, 1.25, 1.1], "body": [1.2488188976378232, 1.25, 1.1]})
    large = normalize_model_measurement("LED_1206_3216Metric" + suffix, {
        "total": [3.2, 1.6, 1.1], "body": [1.916666666666663, 1.6, 1.1]})
    assert size_distance(small["body"], 2.15646, 1.2027) <= DEFAULT_SIZE_TOLERANCE
    assert size_distance(large["body"], 2.01927, 1.89992) > DEFAULT_SIZE_TOLERANCE
    # The old optical slice caused the opposite decisions; keep both regressions.
    assert size_distance(small["section_65pct"], 2.15646, 1.2027) > DEFAULT_SIZE_TOLERANCE
    assert size_distance(large["section_65pct"], 2.01927, 1.89992) <= DEFAULT_SIZE_TOLERANCE


@pytest.mark.parametrize("name", [
    "LED_SMD.3dshapes/LED_RGB_1210.step",
    "LED_SMD.3dshapes/LED_0805_2012Metric_SideView.step",
    "LED_THT.3dshapes/OpticalPart.step",
    "Button_Switch_SMD.3dshapes/SW_SPST_Example.step",
    "RF_Module.3dshapes/ShieldedBoard.step",
    "Connector_USB.3dshapes/Socket.step",
    "SW_Example.step", "LED_Special.step", "Module_Example.step",
])
def test_nonuniform_packages_preserve_total_but_not_unproven_body(name):
    result = normalize_model_measurement(name, {"body": [2, 2, 4], "total": [7, 5, 4]})
    assert result["body"] is None
    assert result["total"] == [7, 5, 4]
    assert result["section_65pct"] == [2, 2, 4]
    assert result["body_method"] == "unresolved_nonuniform_package"
    assert normalize_model_measurement(name, result) == result


def test_other_packages_keep_explicitly_unverified_legacy_section():
    result = normalize_model_measurement("SOIC-8.step", {
        "body": [3.8, 4.8, 1.8], "total": [6, 5, 1.8]})
    assert result["body"] == result["section_65pct"] == [3.8, 4.8, 1.8]
    assert result["body_method"] == "section_65pct_unverified"
    assert normalize_model_measurement("SOIC-8.step", result) == result


@pytest.mark.parametrize("invalid", [None, [], [2], [2, 1, 1, 1], [0, 1], [-2, 1],
    [True, 1], [2, False], ["2", 1], [float("nan"), 1], [float("inf"), 1],
    [2, 1, -1], [2, 1, float("nan")], [10**500, 1], "2x1", {"x": 2, "y": 1}])
def test_invalid_total_never_promotes_the_old_section(invalid):
    result = normalize_model_measurement("LED_0805_2012Metric", {
        "total": invalid, "body": [1, 1, 1]})
    assert result["total"] is None and result["body"] is None
    assert result["body_method"] == "unavailable"
    assert result["section_65pct"] == [1, 1, 1]


@pytest.mark.parametrize("size", [[2, 1.25], (2, 1.25, 1.1)])
def test_legacy_sequence_is_total_and_can_normalize_without_filesystem(size):
    result = normalize_model_measurement("LED_0805_2012Metric", size)
    assert result["total"] == result["body"] == list(size)
    assert result["section_65pct"] is None
    assert normalize_model_measurement("LED_0805_2012Metric", result) == result


@pytest.mark.parametrize("size", [None, "not a reading", False, 1.0])
def test_missing_or_malformed_cache_is_unavailable(size):
    result = normalize_model_measurement("LED_0805_2012Metric", size)
    assert result["body"] is None and result["total"] is None
