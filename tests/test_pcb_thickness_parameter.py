"""@brief The PCB thickness as a visible parameter of a board (HANDOFF point 132).

Contract:
- the value shown on the Component page is the one the board's 3D uses;
- its provenance is the one the model declared: a thickness the model
  estimated, or gave without provenance, is never called «from the documents»;
- a value typed by the user wins over every later analysis, until «Ripristina»
  brings back the last analysis proposal;
- provenance and proposal are saved with the project.

Each test pairs a positive and a negative case that differ for a structural
reason (declared provenance, who set the value, board or single component).
"""
import pytest

from test_extraction_defaults_gui import castellated, header, logs  # noqa: F401 (fixture)
from test_photo_inventory_gui import gui


def _select_type(window, key):
    index = window.combo_component_type.findData(key)
    assert index >= 0
    window.combo_component_type.setCurrentIndex(index)


# ── Provenance in the log and next to the field ─────────────────────────


@pytest.mark.parametrize("source, expected, forbidden", [
    ("read", "dai documenti: 0.80 mm", "stimato"),
    ("derived", "ricavato dai documenti: 0.80 mm", "stimato"),
    ("estimated", "stimato dall'AI, non letto dai documenti: 0.80 mm", "PCB dai documenti"),
    (None, "senza provenienza dichiarata: 0.80 mm", "PCB dai documenti"),
])
def test_the_log_states_the_declared_provenance(window, logs, source, expected, forbidden):
    """Positive and negative together: only a read or derived value is «from the documents»."""
    extra = {"value_sources": {"pcb_thickness_mm": source}} if source else {}
    window._on_extracted(castellated(pcb_thickness_mm=0.8, **extra))
    lines = [line for line in logs if "Spessore PCB" in line]
    assert any(expected in line for line in lines), lines
    assert not any(forbidden in line for line in lines), lines
    assert window.field_cast_pcb.value() == pytest.approx(0.8)


def test_the_label_next_to_the_field_names_the_provenance(window):
    window._on_extracted(castellated(pcb_thickness_mm=0.8,
                                     value_sources={"pcb_thickness_mm": "estimated"}))
    assert window.spin_pcb_thickness.value() == pytest.approx(0.8)
    assert window.label_pcb_thickness_source.text() == window.i18n.text(
        gui.PCB_THICKNESS_SOURCE_LABELS["estimated"])
    window._on_extracted(castellated())
    assert window.spin_pcb_thickness.value() == pytest.approx(gui.STANDARD_PCB_THICKNESS_MM)
    assert window.label_pcb_thickness_source.text() == window.i18n.text(
        gui.PCB_THICKNESS_SOURCE_LABELS["standard"])


# ── The user's value ─────────────────────────────────────────────────────


def test_a_value_typed_by_the_user_survives_a_new_analysis(window, logs):
    """Positive: the user's value stays; the log names the proposal not used."""
    window._on_extracted(castellated(pcb_thickness_mm=1.6,
                                     value_sources={"pcb_thickness_mm": "estimated"}))
    window.spin_pcb_thickness.setValue(1.0)
    assert window.field_cast_pcb.value() == pytest.approx(1.0)
    assert window._pcb_thickness_source == "user"
    window._on_extracted(castellated(pcb_thickness_mm=2.2,
                                     value_sources={"pcb_thickness_mm": "read"}))
    assert window.field_cast_pcb.value() == pytest.approx(1.0)
    assert window._current_castellated_params().pcb_thickness == pytest.approx(1.0)
    assert any("resta 1.00 mm" in line and "2.20 mm" in line for line in logs)
    assert window.btn_pcb_thickness_reset.isEnabled()


def test_a_value_the_analysis_set_is_replaced_by_the_next_analysis(window):
    """Negative: without the user's hand, the newer analysis wins."""
    window._on_extracted(castellated(pcb_thickness_mm=1.6,
                                     value_sources={"pcb_thickness_mm": "estimated"}))
    window._on_extracted(castellated(pcb_thickness_mm=2.2,
                                     value_sources={"pcb_thickness_mm": "read"}))
    assert window.field_cast_pcb.value() == pytest.approx(2.2)
    assert not window.btn_pcb_thickness_reset.isEnabled()


def test_showing_a_value_is_not_the_user_typing_it(window):
    """Negative: a programmatic value (analysis, project) never marks the field as the user's."""
    window.field_cast_pcb.setValue(1.2)
    assert window.spin_pcb_thickness.value() == pytest.approx(1.2)
    assert window._pcb_thickness_source != "user"


def test_reset_goes_back_to_the_last_analysis_proposal(window):
    window._on_extracted(castellated(pcb_thickness_mm=2.2,
                                     value_sources={"pcb_thickness_mm": "read"}))
    window.spin_pcb_thickness.setValue(1.0)
    window.btn_pcb_thickness_reset.click()
    assert window.field_cast_pcb.value() == pytest.approx(2.2)
    assert window._pcb_thickness_source == "read"


def test_reset_without_an_analysis_goes_to_the_standard(window):
    window.spin_pcb_thickness.setValue(1.0)
    window.btn_pcb_thickness_reset.click()
    assert window.field_cast_pcb.value() == pytest.approx(gui.STANDARD_PCB_THICKNESS_MM)
    assert window._pcb_thickness_source == "standard"


def test_typing_a_thickness_invalidates_the_3d_on_screen(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, "_invalidate_generated_mesh", lambda: calls.append(1))
    window.spin_pcb_thickness.setValue(1.0)
    assert calls


# ── The 3D uses the value on the page ────────────────────────────────────


def test_a_populated_board_builds_its_pcb_with_the_page_value(window):
    """Positive: board -> the field; the extracted value alone is not used."""
    _select_type(window, "aggregate_tht")
    window.group_3d.setChecked(True)
    window._extracted_pcb_thickness = 2.2
    window.field_cast_pcb.setValue(1.0)
    assert window._current_body_params().pcb_thickness_mm == pytest.approx(1.0)


def test_a_single_component_keeps_the_value_of_its_documents(window):
    """Negative: a single component has no board on the page; its documents decide."""
    _select_type(window, "single_tht")
    window.group_3d.setChecked(True)
    window._extracted_pcb_thickness = None
    window.field_cast_pcb.setValue(1.0)
    assert window._current_body_params().pcb_thickness_mm is None


def test_the_row_is_shown_for_boards_only(window):
    _select_type(window, "aggregate_tht")
    assert window._ai_form.isRowVisible(window._row_pcb_thickness)
    _select_type(window, "single_tht")
    assert not window._ai_form.isRowVisible(window._row_pcb_thickness)


# ── Saved with the project ───────────────────────────────────────────────


def test_provenance_and_proposal_are_saved_and_restored(window):
    window._on_extracted(castellated(pcb_thickness_mm=2.2,
                                     value_sources={"pcb_thickness_mm": "read"}))
    window.spin_pcb_thickness.setValue(1.0)
    state = window._project_state()
    assert state["pcb_thickness"] == {"source": "user", "analysis": [2.2, "read"]}
    window._pcb_thickness_source, window._analysis_pcb_thickness = "standard", None
    window._apply_project_state(state)
    assert window._pcb_thickness_source == "user"
    assert window._analysis_pcb_thickness == (2.2, "read")
    assert window.field_cast_pcb.value() == pytest.approx(1.0)


@pytest.mark.parametrize("saved, expected", [
    (None, ("project", None)),
    ({"source": "user", "analysis": [1.2, "estimated"]}, ("user", (1.2, "estimated"))),
    ({"source": "nonsense", "analysis": [True, "read"]}, ("project", None)),
    ({"source": "read", "analysis": [float("nan"), "read"]}, ("read", None)),
    ({"source": "read", "analysis": [-1, "read"]}, ("read", None)),
])
def test_a_saved_state_is_read_defensively(saved, expected):
    assert gui._saved_pcb_thickness(saved) == expected
