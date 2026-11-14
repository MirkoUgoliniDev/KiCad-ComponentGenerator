"""@brief Fixes from the real end-to-end runs of 2026-11-13 and the generality audit (main window and check side).

@details (1) the exported symbol is compared with the applied one as the
export writes it (graphics and metadata), metadata lines left out; (2) a
missing footprint is said in words, with the open question that explains it;
(3) the «name refers to another pad» notes and the 3D warnings are folded
into one visible line each, the detail kept in the project log file; (4) the
strip-header host lands start at KiCad's PinHeader_2.54 values; (5)
`split_pin_name` keeps groups of single characters or states in the name;
L2: names the user typed are never flagged as remarks. Positive and negative
cases each; KiCad is not needed (kicad-cli is faked as absent).
"""

from pathlib import Path

import pytest

from kicadgen.generators.symbol import (serialize_symbol, split_pin_name, symbol_inputs,
                                        symbol_pin_names_hidden, symbol_property_positions)
from kicadgen.generators.symbol import SymbolMetadata
from kicadgen.services import component_check as check
from test_kicad_library import _artifacts

ROWS = [{"pin": "1", "name": "GPIO1"}]


def _applied(rows=ROWS, description="Una descrizione"):
    """The applied symbol rendered as the window's export writes it (metadata included)."""
    pins, body = symbol_inputs(rows)
    return serialize_symbol("Demo", pins, footprint="KiCadGen:Demo", body=body,
                            properties=symbol_property_positions(rows),
                            pin_names_hidden=symbol_pin_names_hidden(rows),
                            metadata=SymbolMetadata(reference="U", description=description,
                                                    keywords="kw", datasheet="~",
                                                    fp_filters="Demo*"))


def test_a_fresh_export_is_not_reported_stale(tmp_path, monkeypatch):
    """@brief Positive (real runs): the file the export just wrote, metadata and all, is current.

    @note Negative: a really different symbol (another pin name) is stale, importance 2.
    """
    art = _artifacts(tmp_path)
    monkeypatch.setattr(check, "find_kicad_cli", lambda: None)
    applied = _applied()
    (art.component_dir / "Demo.kicad_sym").write_text(applied, encoding="utf-8")
    results = check.verify_component(art.component_dir, "Demo", ROWS, applied_symbol=applied)
    assert not any(r.code == "symbol_export_stale" for r in results)
    # Metadata edited after the export: still not «stale» (the KiCad gate ignores it too).
    results = check.verify_component(art.component_dir, "Demo", ROWS,
                                     applied_symbol=_applied(description="Altra descrizione"))
    assert not any(r.code == "symbol_export_stale" for r in results)
    other = [{"pin": "1", "name": "GPIO2"}]
    results = check.verify_component(art.component_dir, "Demo", other,
                                     applied_symbol=_applied(rows=other))
    stale = [r for r in results if r.code == "symbol_export_stale"]
    assert len(stale) == 1 and stale[0].importance == 2


def test_a_missing_footprint_is_said_in_words(tmp_path, monkeypatch):
    """@brief Positive: the open question explains the missing footprint, with its remedy.

    @note Negative: without a reason a plain sentence, never the operating system's errno.
    """
    art = _artifacts(tmp_path)
    art.footprint_path.unlink()
    monkeypatch.setattr(check, "find_kicad_cli", lambda: None)
    reason = "Nessun footprint: una domanda sulla disposizione dei pad è ancora aperta."
    results = check.verify_component(art.component_dir, "Demo", ROWS, footprint_missing=reason)
    missing = [r for r in results if r.code == "footprint_missing"]
    assert len(missing) == 1 and missing[0].detail == reason and "Rigenera footprint" in missing[0].remedy
    plain = check.verify_component(art.component_dir, "Demo", ROWS)
    detail = [r for r in plain if r.code == "footprint_missing"][0].detail
    assert "Errno" not in detail and "No such file" not in detail


def test_the_window_passes_the_open_question_and_the_applied_symbol(window):
    """@brief Positive: an open pad-layout question names the missing footprint; negative: none -> None."""
    window._extraction_questions = []
    assert window._footprint_missing_reason() is None
    window._extraction_questions = [{"key": "pad_layout:x", "field": "pad_layout", "text": "?",
                                     "options": [{"label": "a", "value": 1}]}]
    # CONTRACT CHANGED 2026-11-13 (pre-commit review): the block text of the question's own
    # field (FOOTPRINT_QUESTION_TEXTS), «posizioni dei pad» for a pad layout.
    assert "posizioni dei pad" in window._footprint_missing_reason()
    window._extraction_questions = []


def test_name_notes_are_folded_into_one_line(window, monkeypatch):
    """@brief Positive: several «name refers to another pad» notes -> one visible line with their
    pins, details in the project log file; negative: another note is logged as it is."""
    from kicadgen.generators.symbol import LocalizedText
    from kicadgen.generators.symbol_pins import NAME_REFERS_TO_OTHER_PAD
    written = []
    monkeypatch.setattr(window, "_write_log_line", lambda text: written.append(text))
    notes = [LocalizedText(NAME_REFERS_TO_OTHER_PAD, "x", number=str(n), name=f"IO{n + 1}",
                           other=str(n + 1)) for n in (3, 4, 5)]
    other = LocalizedText("Nomi dei pin presi dal disegno dei pad (da verificare): {pins}.", "x",
                          pins="7 A")
    before = len(window._log_lines)
    window._log_pinout_notes(notes + [other])
    visible = window._log_lines[before:]
    assert len([line for line in visible if "richiama" in line or "richiamano" in line]) == 1
    assert any("3-5" in line for line in visible)
    assert sum("richiama il pad" in line for line in written) == 3


def test_the_folded_template_is_the_one_the_notes_carry():
    """@brief Structural: the note `pinout_on_pads` really writes carries NAME_REFERS_TO_OTHER_PAD,
    so the window's folding by template cannot drift from the producer."""
    from kicadgen.generators.symbol_pins import NAME_REFERS_TO_OTHER_PAD, pinout_on_pads
    placed = pinout_on_pads([{"pin": "1", "name": "PIN 2"}], ["1", "2"])
    assert [note.template for note in placed.notes] == [NAME_REFERS_TO_OTHER_PAD]


def test_3d_warnings_are_folded_per_run(window, monkeypatch):
    """@brief Positive: three warnings of one run -> one visible line with the count; negative:
    a second flush without warnings adds nothing."""
    written = []
    monkeypatch.setattr(window, "_write_log_line", lambda text: written.append(text))
    monkeypatch.setattr(window, "_cad_result_is_current", lambda: True)
    before = len(window._log_lines)
    for name in ("U1", "U2", "U3"):
        window._on_cad3d_warning(f"componente {name} (ic): package sconosciuto")
    assert len(window._log_lines) == before and len(written) == 3
    window._flush_cad3d_warnings()
    window._flush_cad3d_warnings()
    added = window._log_lines[before:]
    assert len(added) == 1 and "3" in added[0]


def test_strip_header_lands_start_at_kicad_pin_header_values(window):
    """@brief Positive: a fresh project and a project without saved values get pad 1.7 / drill
    1.0 mm (KiCad PinHeader_2.54); negative: a project's saved values are kept."""
    window._restore_mounting_state({"mode": "headers"})
    assert (window.spin_header_pad.value(), window.spin_header_drill.value()) == (1.7, 1.0)
    window._restore_mounting_state({"mode": "headers", "pad_diameter": 1.3, "drill": 0.5})
    assert (window.spin_header_pad.value(), window.spin_header_drill.value()) == (1.3, 0.5)
    window._restore_mounting_state({"mode": "direct"})


@pytest.mark.parametrize("name", ["PA0 (I/O)", "X (N/C)", "MODE (H/L)", "EN (ON/OFF)",
                                  "RESET (HIGH/LOW)"])
def test_a_group_of_characters_or_states_stays_in_the_name(name):
    """@brief Negative (audit M1/L4): single characters or state words are not other functions."""
    assert split_pin_name(name) == (name, ())


@pytest.mark.parametrize("name, expected", [
    ("GPIO4 (ADC1_3, TOUCH4)", ("GPIO4", ("ADC1_3", "TOUCH4"))),
    ("GPIO4 (ADC1_3/RTC/TOUCH4)", ("GPIO4", ("ADC1_3", "RTC", "TOUCH4"))),
    ("D0 (RX)", ("D0", ("RX",))),
])
def test_identifier_groups_are_still_split(name, expected):
    """@brief Positive: groups of signal identifiers are still alternates."""
    assert split_pin_name(name) == expected


def test_a_name_the_user_typed_is_never_flagged_as_a_remark():
    """@brief Positive (audit L2): an untyped prose-like name is flagged; negative: the same name
    typed by the user is not."""
    from kicadgen.services.component_check import document_checks
    rows = [{"pin": "1", "name": "GND (labelled G, between pads)"}]
    document = {"pinout": [{"pin": "1", "name": "GND"}], "user_names": set()}
    assert any(r.code == "document_name_remark" for r in document_checks(document, rows, {"1"}))
    document["user_names"] = {"1"}
    assert not any(r.code == "document_name_remark" for r in document_checks(document, rows, {"1"}))


def test_the_extract_worker_forwards_the_saved_answers(qapp, monkeypatch):
    """@brief Positive (overnight review 2026-11-14, root cause of answers asked again): the
    project's saved answers reach the extractor as `answers=`; negative: without answers no
    keyword is passed."""
    from kicadgen.gui import main_window as gui
    from kicadgen.gui.workers import ExtractWorker
    seen = []

    def fake_extract(doc_paths, *, answers=None, **kwargs):
        seen.append({"answers": answers, "given": "answers" in kwargs or answers is not None})
        return "params"
    monkeypatch.setattr(gui, "extract_pin_header_params", fake_extract)
    saved = {"contacts_view:abc": "bottom"}
    worker = ExtractWorker([], answers=saved)
    saved["contacts_view:abc"] = "top"  # a later change of the project's dict
    worker.run()
    assert seen[-1]["answers"] == {"contacts_view:abc": "bottom"}, "the snapshot taken at start"
    worker = ExtractWorker([])
    worker.run()
    assert seen[-1]["answers"] is None and seen[-1]["given"] is False
