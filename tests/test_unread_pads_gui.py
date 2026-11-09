"""@brief The window writes and draws no footprint while the extraction left pad positions unread (PADNULL H1).

Audit of 2026-11-09 (H1): after the extractor dropped explicit pads whose
positions were not read (`ExtractedParams.footprint_pads_dropped`), the window
still wrote a pin header from stale or default matrix fields through
«Genera» / «Rigenera footprint», drew it in the preview and listed its pads
as symbol pins; the automatic block sent the user to «Rigenera footprint».
Contract: no footprint from any field until a reading confirms the pads
(«Rileggi solo le posizioni dei pad», a «pad_layout» answer, a new analysis),
the refusal names the unread pads and the remedy, and survives a reopen.

Offline: the paid workers never start (`tests.ui_flow_helpers`), FreeCAD is
absent, every dialog is replaced. The extraction results are built by the
real extractor validation from synthetic answers (no network).
"""
from PyQt6.QtWidgets import QMessageBox

from kicadgen.gui import main_window as gui
from kicadgen.services.datasheet_extractor import _params_from_tool_arguments, single_answer_to_data
from tests.test_unread_pad_positions import _answer, _asked, _castellated, _pads, _unread_smd_pin
from tests.ui_flow_helpers import header_extraction, prepare, run_extraction

#: Label of the existing remedy, exactly as the tools menu shows it.
REMEDY = "Rileggi solo le posizioni dei pad"


def _footprint(window, name):
    return window._project_dir() / f"{name}.pretty" / f"{name}.kicad_mod"


def _single_tht(window, monkeypatch, tmp_path, name):
    """A fresh single-THT project; returns the list the warnings append (title, text) to."""
    prepare(window, monkeypatch, tmp_path, name)
    combo = window.combo_component_type
    combo.setCurrentIndex(combo.findData("single_tht"))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda _parent, title, text, *a, **k: warnings.append((title, text)))
    return warnings


def _dropped(unread=("5",)):
    """A real extraction result whose explicit pads were dropped for unread positions."""
    params = _params_from_tool_arguments(single_answer_to_data(_answer(_pads(unread=unread))))
    assert params.footprint_pads_dropped() and params.package_geometry is None
    return params


def _assert_blocked(text, pads="5"):
    assert f"pad {pads} " in text and REMEDY in text


# -- positive: every footprint path refuses ---------------------------------------------

def test_dropped_pads_block_the_automatic_generation_with_the_remedy(window, monkeypatch, tmp_path):  # noqa: F811
    warnings = _single_tht(window, monkeypatch, tmp_path, "RLY")
    run_extraction(window, _dropped())
    assert not _footprint(window, "RLY").exists()
    assert window._unread_pads == ["5"]
    blocked = [text for title, text in warnings if title == "Generazione automatica non avviata"]
    assert blocked, warnings
    _assert_blocked(blocked[-1])
    # The «Piazzole» remedy would send the user to fields holding no positions.
    assert "Rigenera footprint" not in blocked[-1]


def test_dropped_pads_block_genera_and_rigenera_footprint(window, monkeypatch, tmp_path):  # noqa: F811
    warnings = _single_tht(window, monkeypatch, tmp_path, "RLY")
    run_extraction(window, _dropped(unread=("1", "2", "3", "4", "5")))
    warnings.clear()
    assert window._generate() is False                       # «Genera»
    assert window._generate(footprint_only=True) is False    # «Rigenera footprint»
    assert not _footprint(window, "RLY").exists()
    assert [title for title, _text in warnings] == ["Footprint non generato"] * 2
    _assert_blocked(warnings[0][1], pads="1, 2, 3, 4, 5")


def test_dropped_pads_draw_no_preview_and_no_symbol_pins(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path, "RLY")
    run_extraction(window, _dropped())
    window._draw_preview()
    assert window._preview_params() is None
    assert not [item for item in window.preview_scene.items() if item.data(0x504144)]
    label = window.label_preview_footprint_block
    assert not label.isHidden()
    _assert_blocked(label.text())
    # The symbol never gets pins from the matrix fields meanwhile.
    try:
        window._symbol_pad_ids()
    except ValueError as exc:
        _assert_blocked(str(exc))
    else:
        raise AssertionError("symbol pads built from the matrix fields")


def test_the_refusal_survives_a_project_reopen(window, monkeypatch, tmp_path):  # noqa: F811
    warnings = _single_tht(window, monkeypatch, tmp_path, "RLY")
    run_extraction(window, _dropped())
    state = window._project_state()
    assert state["unread_pads"] == ["5"]
    window._switch_project("OTHER", window._general_project_state)
    assert window._unread_pads is None and "unread_pads" not in window._project_state()
    window._switch_project("RLY", state)
    assert window._unread_pads == ["5"]
    warnings.clear()
    assert window._generate() is False
    assert not _footprint(window, "RLY").exists()
    _assert_blocked(warnings[-1][1])


def test_a_malformed_saved_value_is_no_block(window):
    """The saved key is validated: no invented pad numbers from a damaged file."""
    assert gui.MainWindow._valid_unread_pads(["5", " ", 3, None]) == ["5"]
    assert gui.MainWindow._valid_unread_pads("5") is None
    assert gui.MainWindow._valid_unread_pads([]) is None


# -- negative: what has its pads generates -----------------------------------------------

def test_a_header_with_numeric_matrix_generates(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path, "HDR")
    run_extraction(window, header_extraction())
    assert window._unread_pads is None and window._unread_pads_block() == ""
    assert _footprint(window, "HDR").exists()


def test_a_single_part_with_every_pad_read_is_not_blocked(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path, "RLY")
    params = _params_from_tool_arguments(single_answer_to_data(_answer(_pads())))
    assert not params.footprint_pads_dropped()
    run_extraction(window, params)
    assert window._unread_pads is None
    assert window.package_geometry is not None and window._preview_params() is not None


def test_castellated_optional_pads_dropped_do_not_block(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, "MOD")
    params = _params_from_tool_arguments(_castellated([_unread_smd_pin()]))
    assert not params.footprint_pads_dropped()
    run_extraction(window, params)
    assert window._unread_pads is None and window._unread_pads_block() == ""
    assert "Nessun footprint: le posizioni dei pad" not in window._log_plain_text()


# -- the block lifts once the positions are confirmed ------------------------------------

def test_a_pad_layout_answer_lifts_the_block(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path, "RLY")
    params, question = _asked()
    chosen = question["options"][1]["value"]
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question", lambda self, q: chosen)
    run_extraction(window, params)
    assert window._unread_pads is None and window.package_geometry is not None
    assert window._preview_params() is not None


def test_torna_indietro_on_the_pad_layout_question_keeps_the_block(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path, "RLY")
    params, _question = _asked()
    monkeypatch.setattr(gui.MainWindow, "_ask_extraction_question",
                        lambda self, q: gui.QUESTION_BACK)
    run_extraction(window, params)
    assert window._unread_pads == ["5"] and window._generate() is False
    assert not _footprint(window, "RLY").exists()


def test_a_successful_reread_lifts_the_block(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path, "RLY")
    run_extraction(window, _dropped())
    assert window._generate() is False
    reread = _params_from_tool_arguments(single_answer_to_data(_answer(_pads())))
    window._on_reread_done(reread)
    window._on_extract_finished()
    assert window._unread_pads is None and "unread_pads" not in window._project_state()
    assert window._generate() is True
    assert _footprint(window, "RLY").exists()


def test_a_reread_that_still_misses_pads_keeps_the_block(window, monkeypatch, tmp_path):  # noqa: F811
    _single_tht(window, monkeypatch, tmp_path, "RLY")
    run_extraction(window, _dropped())
    window._on_reread_done(_dropped(unread=("4",)))
    window._on_extract_finished()
    assert window._unread_pads == ["4"] and window._generate() is False
    assert not _footprint(window, "RLY").exists()
