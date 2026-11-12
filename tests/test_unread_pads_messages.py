"""@brief Messages that embed the unread-pads block (09/11 sera, L1 and L3).

L1: with unread pad positions and an old .kicad_mod on disk, the KiCad tab's
stale-file check wrapped the block in «Footprint non verificabile: ... Correggili
e rigenera il footprint», advice the block itself contradicts (no field can fix
unread positions). The block is now shown alone; an ordinary invalid footprint
keeps the «correct and regenerate» sentence.
L3: the symbol editor's blocking message (a composed text handed to
`SymbolTab.set_error`) stayed in the old language after a language switch.

Offline: the paid workers never start (`tests.ui_flow_helpers`).
"""
from tests.test_unread_pads_gui import REMEDY, _footprint, _single_tht
from tests.ui_flow_helpers import header_extraction, run_extraction


def _header_on_disk(window, monkeypatch, tmp_path):
    """A single-THT project with a header footprint written on disk."""
    _single_tht(window, monkeypatch, tmp_path, "HDR")
    params = header_extraction()
    # What every real analysis keeps: the main answer a partial re-read
    # starts from. Without it the re-read entry is disabled and, since
    # review F2-3 (2026-11-11), the block no longer names it as a remedy.
    params.raw_main = {"name": "HDR", "_candidates": {}}
    run_extraction(window, params)
    assert _footprint(window, "HDR").exists()
    assert window._footprint_alignment_block() is None


# -- L1 ----------------------------------------------------------------------------------

def test_the_stale_check_shows_the_unread_pads_block_alone(window, monkeypatch, tmp_path):  # noqa: F811
    """Positive: with unread pads the reason is the block, with its remedy and no «rigenera» advice."""
    _header_on_disk(window, monkeypatch, tmp_path)
    window._unread_pads = ["5"]
    code, reason = window._footprint_alignment_block()
    assert code == "footprint_invalid"
    assert reason == window._unread_pads_block()
    assert REMEDY in reason
    assert "rigenera" not in reason.lower() and "Correggili" not in reason


def test_an_ordinary_invalid_footprint_keeps_the_regenerate_advice(window, monkeypatch, tmp_path):  # noqa: F811
    """Negative: invalid fields without unread pads keep «Correggili e rigenera»."""
    _header_on_disk(window, monkeypatch, tmp_path)

    def invalid():
        raise ValueError("passo non valido")

    monkeypatch.setattr(window, "_footprint_inputs_now", invalid)
    code, reason = window._footprint_alignment_block()
    assert code == "footprint_invalid"
    assert "passo non valido" in reason and "Correggili e rigenera" in reason


# -- L3 ----------------------------------------------------------------------------------

def _switch(window, language):
    window._on_language_changed(window.language_combo.findData(language))


def test_the_symbol_block_follows_a_language_switch(window, monkeypatch, tmp_path):  # noqa: F811
    """Positive: the symbol editor's block is rewritten in English after the switch."""
    _header_on_disk(window, monkeypatch, tmp_path)
    window._unread_pads = ["5"]
    window._refresh_symbol_tab()
    assert REMEDY in window.symbol_tab._error
    try:
        _switch(window, "en")
        english = window.i18n.text(REMEDY)
        assert english != REMEDY
        assert english in window.symbol_tab._error and REMEDY not in window.symbol_tab._error
        assert window.symbol_tab._error == window._unread_pads_block()
    finally:
        _switch(window, "it")
    assert REMEDY in window.symbol_tab._error


def test_no_symbol_block_sets_nothing_on_a_language_switch(window, monkeypatch, tmp_path):  # noqa: F811
    """Negative: without a refusal the switch sets no error on the symbol editor."""
    _header_on_disk(window, monkeypatch, tmp_path)
    window._refresh_symbol_tab()
    calls = []
    monkeypatch.setattr(window.symbol_tab, "set_error", lambda message: calls.append(message))
    try:
        _switch(window, "en")
    finally:
        _switch(window, "it")
    assert calls == [] and window.symbol_tab._error == ""
