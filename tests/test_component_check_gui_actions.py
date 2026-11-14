"""@brief «Verifica componente» with the document pinout and its fixes; «Correggi» on KLC findings (2026-11-13).

@details The main window passes the check the document pinout, the side
labels at the window's current numbering, the user's typed names and the
«side_numbering» answer, plus two fixes run through the existing write paths
(model reference into the .kicad_mod, symbol export into the component
folder). A KLC finding whose `fix` id is FIX_NAME_SPACES gets a «Correggi»
button that renames the project, after a confirmation naming the new name,
through the one rename path. Positive and negative cases each.
"""

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QMessageBox, QPushButton

from kicadgen.services import klc_checks
from kicadgen.services.component_check import (ACTION_ANSWER_CONTACT_NC, ACTION_EDIT_PINOUT,
                                               ACTION_REEXPORT_SYMBOL, ACTION_SHOW_IN_KICAD)

MAIN_ANSWER = {
    "pinout": [{"pin": 5, "name": "GND"}, {"pin": 6, "name": "3V3"}],
    "castellated_sides": [{"edge": "top", "count": 2, "first_pin": 1, "reverse": False,
                           "pin_names": ["G", "3V3"]}],
}


class _DialogStub:
    """Stand-in of ComponentCheckDialog recording what the window gives it."""
    seen = {}

    def __init__(self, args, parent=None, tx=None, actions=None):
        # CONTRACT CHANGED 2026-11-14 (review P2): the window gives an args
        # BUILDER, called for every run; the stub calls it once.
        _DialogStub.seen = {"args": args() if callable(args) else args, "builder": args,
                            "actions": dict(actions or {})}

    def exec(self):
        return 0


@pytest.fixture
def check_window(window, monkeypatch):
    """The window with the check dialog stubbed, the file gates open, a castellated module."""
    from kicadgen.gui import component_check_dialog
    monkeypatch.setattr(component_check_dialog, "ComponentCheckDialog", _DialogStub)
    monkeypatch.setattr(window, "_footprint_pending_block", lambda: None)
    monkeypatch.setattr(window, "_footprint_alignment_block", lambda: None)
    monkeypatch.setattr(window, "_kicad_model_offset", lambda: (0.0, 0.0, 0.0))
    window.group_castellated.setChecked(True)
    return window


def test_the_check_receives_the_document_and_both_actions(check_window, monkeypatch):
    """@brief Positive: pinout, labels of the canonical sides, user names, decision, actions."""
    window = check_window
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: MAIN_ANSWER)
    window._sides.clear()
    window._add_side_row({"edge": "top", "count": 2, "first_pin": 5, "reverse": False,
                          "contact_style": "half_hole"})
    window._document_sides = window._canonical_document_sides(
        [{"edge": "top", "count": 2, "first_pin": 5, "reverse": False, "pin_names": ["G", "3V3"]}])
    window._user_pin_names = {"6": "VCC"}
    window._side_numbering_key = "side_numbering:abc"
    window._extraction_answers = {"side_numbering:abc": "renumber"}
    window._extraction_questions = []
    window._verify_component()
    document = _DialogStub.seen["args"]["document"]
    assert document["pinout"] == [{"pin": 5, "name": "GND"}, {"pin": 6, "name": "3V3"}]
    assert document["side_labels"] == [("5", "G"), ("6", "3V3")]
    assert document["user_names"] == {"6"}
    assert document["decision"] == "renumber" and document["open_question"] is False
    assert document["layout"] == "sides"
    # CONTRACT CHANGED 2026-11-13 (KiCad workflow review): only the symbol re-export
    # remains as a fix; the «link the STEP model» fix was removed.
    # CONTRACT CHANGED 2026-11-14 (NC/NO witness): a second fix asks the open
    # «contact_nc» question again and recomposes the symbol, a third opens the
    # Pinout table after «Nessuna delle due»; the dialog shows
    # its button only next to the check that names it (witness «asked»).
    # CONTRACT CHANGED 2026-11-14 (redesign): «Mostra in KiCad» opens the
    # check's preview in KiCad (a view, no re-check).
    assert set(_DialogStub.seen["actions"]) == {ACTION_REEXPORT_SYMBOL, ACTION_ANSWER_CONTACT_NC,
                                                ACTION_EDIT_PINOUT, ACTION_SHOW_IN_KICAD}


def test_a_layout_without_sides_gets_no_document(check_window, monkeypatch):
    """@brief Negative (audit M1): a single part or a pin matrix -> `document` None, no side remedy."""
    window = check_window
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: MAIN_ANSWER)
    window.group_castellated.setChecked(False)
    window._verify_component()
    assert _DialogStub.seen["args"]["document"] is None


def _bottom_view_case(window, monkeypatch, canonical, pinout):
    """A module read on a BOTTOM view: raw answer as read, canonical sides as the footprint."""
    import copy
    from kicadgen.services import datasheet_extractor as de
    raw_sides = copy.deepcopy(canonical)
    de._mirror_castellated_sides(raw_sides)  # the view as the model read it
    raw = {"layout": "castellated", "contacts_view": "bottom", "castellated_sides": raw_sides,
           "pinout": [dict({"pin": entry[0], "name": entry[1]},
                           **({"off_side": True} if len(entry) > 2 and entry[2] else {}))
                      for entry in pinout]}
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: raw)
    window._sides.clear()
    for side in canonical:
        window._add_side_row(dict(side, contact_style="half_hole"))
    window._document_sides = window._canonical_document_sides(canonical)
    window._side_numbering_key = None
    window._extraction_questions = []
    return window._component_check_document()


_CANONICAL = [
    {"edge": "left", "count": 3, "first_pin": 1, "reverse": False, "pin_names": ["GND", "3V3", "IO3"]},
    {"edge": "right", "count": 3, "first_pin": 4, "reverse": False, "pin_names": ["IO4", "IO5", "IO6"]},
    # Reversed numbering: the labels are listed along the edge, so the first
    # one sits on the highest pad.
    {"edge": "top", "count": 2, "first_pin": 7, "reverse": True, "pin_names": ["IO8", "IO7"]},
]
_PINOUT = [(1, "GND"), (2, "3V3"), (3, "IO3"), (4, "IO4"), (5, "IO5"), (6, "IO6"),
           (7, "IO7"), (8, "IO8")]


def test_a_correct_module_read_on_a_bottom_view_has_no_document_finding(check_window, monkeypatch):
    """@brief Positive (audit H1): labels and numbering from ONE canonical source -> coherent.

    @note The raw answer's sides are mirrored (left/right swapped, top labels
        reversed): pairing them with the window's sides by edge name, as before,
        flagged every pad. The canonical pairing flags none.
    """
    from kicadgen.services.component_check import document_checks
    document = _bottom_view_case(check_window, monkeypatch, _CANONICAL, _PINOUT)
    pads = {str(n) for n, _name in _PINOUT}
    rows = [{"pin": str(n), "name": name} for n, name in _PINOUT]
    results = document_checks(document, rows, pads)
    assert [r.status for r in results] == ["OK"], [(r.code, r.detail) for r in results]


def test_a_real_mismatch_on_a_bottom_view_is_still_found(check_window, monkeypatch):
    """@brief Negative (audit H1): a wrong label and a pinout entry without a pad are reported
    on the right pads, whatever view the module was read on."""
    import copy
    from kicadgen.services.component_check import document_checks
    canonical = copy.deepcopy(_CANONICAL)
    canonical[0]["pin_names"] = ["GND", "5V", "IO3"]
    pinout = _PINOUT + [(9, "IO9")]
    document = _bottom_view_case(check_window, monkeypatch, canonical, pinout)
    pads = {str(n) for n, _name in _PINOUT}
    rows = [{"pin": str(n), "name": name} for n, name in _PINOUT]
    results = document_checks(document, rows, pads)
    label = [r for r in results if r.code == "document_side_label"]
    assert len(label) == 1 and label[0].pins == ("2",) and label[0].importance == 2
    # CONTRACT CHANGED 2026-11-13 (PIN round 3): entry 9 is on no side and nobody
    # DECLARED it off the sides, so it is «unexplained» (maybe an exposed pad,
    # maybe a missing pad): a WARN to fix before production, never a dropped pin
    # and never the final-review «off side» note reserved for declared entries.
    unexplained = [r for r in results if r.code == "document_unexplained"]
    assert len(unexplained) == 1 and unexplained[0].pins == ("9",)
    assert unexplained[0].status == "WARN" and unexplained[0].importance == 2
    assert not any(r.code in ("document_dropped", "document_off_side") for r in results)


def test_a_declared_off_side_entry_is_only_a_final_review(check_window, monkeypatch):
    """@brief Positive: entry 9 declared off the sides («off_side» kept in the document) and
    missing from the footprint -> `document_off_side` importance 2 (L6, a missing electrical
    pad; CONTRACT CHANGED 2026-11-13, was 3); negative: no unexplained or dropped finding."""
    from kicadgen.services.component_check import document_checks
    pinout = _PINOUT + [(9, "EP", True)]
    document = _bottom_view_case(check_window, monkeypatch, _CANONICAL, pinout)
    assert any(entry.get("off_side") is True for entry in document["pinout"])
    pads = {str(n) for n, _name in _PINOUT}
    rows = [{"pin": str(n), "name": name} for n, name in _PINOUT]
    results = document_checks(document, rows, pads)
    off = [r for r in results if r.code == "document_off_side"]
    assert len(off) == 1 and off[0].pins == ("9",) and off[0].importance == 2
    assert not any(r.code in ("document_dropped", "document_unexplained") for r in results)


def test_without_printed_numbers_no_document_claim_is_made(check_window, monkeypatch):
    """@brief Positive (pin_numbers_printed False): the «numerazione convenzionale» note, never a
    dropped ERROR even when the sides do not cover the rows; the rows without a pad are listed
    by their CONVENTION numbers with a remedy that names no question. Negative: the same case
    with printed numbers blocks (a shifted numbering).

    @note CONTRACT CHANGED 2026-11-13 (audit HIGH 1 / MEDIUM 2): in convention mode the check
        compares the settled convention rows with the settled sides and runs the normal
        checks; before, it stopped at the note.
    """
    import copy
    from kicadgen.services.component_check import document_checks
    canonical = copy.deepcopy(_CANONICAL)
    canonical[0]["first_pin"] = 10
    _bottom_view_case(check_window, monkeypatch, canonical, _PINOUT)
    pads = {"10", "11", "12", "4", "5", "6", "7", "8"}
    names = dict((str(n), name) for n, name in _PINOUT)
    rows = [{"pin": pad, "name": names.get(pad, "")} for pad in sorted(pads)]
    check_window._pin_numbers_printed = False
    check_window._convention_pinout = check_window._valid_pinout_rows(
        [{"pin": n, "name": name} for n, name in _PINOUT])
    unprinted = check_window._component_check_document()
    assert unprinted["numbers_printed"] is False
    results = document_checks(unprinted, rows, pads)
    # CONTRACT CHANGED 2026-11-14 (practical test 5): the conventional numbering is
    # reviewed before production, in the Pinout table.
    assert results[0].code == "document_numbering_convention" and results[0].importance == 2
    assert results[0].action == ACTION_EDIT_PINOUT
    assert not any(r.code == "document_dropped" for r in results)
    lost = [r for r in results if r.code == "document_unexplained"]
    assert len(lost) == 1 and lost[0].pins == ("1", "2", "3") and lost[0].importance == 2
    assert "domanda" not in lost[0].remedy
    check_window._pin_numbers_printed = None  # not declared: read as printed
    printed = check_window._component_check_document()
    assert any(r.code == "document_dropped" and r.status == "ERROR"
               for r in document_checks(printed, rows, pads))


def _convention_case(window, monkeypatch, raw):
    """Apply the extractor's convention to `raw` and load the settled state into the window."""
    import copy
    from kicadgen.services import datasheet_extractor as de
    data = copy.deepcopy(raw)
    de._apply_numbering_convention(data)
    sides = de._castellated_sides(copy.deepcopy(data["castellated_sides"]))
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: copy.deepcopy(raw))
    window.group_castellated.setChecked(True)
    window._sides.clear()
    for side in sides:
        window._add_side_row(dict(side, contact_style="half_hole"))
    window._document_sides = window._canonical_document_sides(sides)
    window._pin_numbers_printed = False
    window._convention_pinout = window._valid_pinout_rows(data["pinout"])
    window._side_numbering_key = None
    window._extraction_questions = []
    return data, sides


def test_a_convention_module_gets_the_normal_checks_in_one_frame(check_window, monkeypatch):
    """@brief Positive (audit HIGH 1, ESP32 of 12:27 without printed numbers): label placement and
    differences are found on the right convention pins (1 and 2 named «3V3» on «G» contacts).

    @note Negative: no dropped entry and no ERROR from the manufacturer's numbers.
    """
    from test_extractor_side_pinout import _esp32_1227
    from kicadgen.services import pinout_coherence as pc
    from kicadgen.services.component_check import document_checks
    data, sides = _convention_case(check_window, monkeypatch, _esp32_1227(pin_numbers_printed=False))
    document = check_window._component_check_document()
    rows = [{"pin": row["pin"], "name": row["name"]} for row in check_window._convention_pinout]
    pads = [str(n) for side in sides for n in pc.side_pad_numbers(side)]
    results = document_checks(document, rows, pads)
    assert results[0].code == "document_numbering_convention"
    flagged = {pin for r in results if r.code in ("document_label_placement", "document_side_label")
               for pin in r.pins}
    assert {"1", "2"} <= flagged
    assert all(r.importance == 2 for r in results
               if r.code in ("document_label_placement", "document_side_label"))
    assert not any(r.code == "document_dropped" for r in results)


def test_an_unattached_entry_is_named_by_its_convention_number(check_window, monkeypatch):
    """@brief Positive (audit MEDIUM 2): an entry the analysis numbered 30 and placed on no
    contact is listed as the convention's pin 7, with a remedy that names no question.

    @note Negative: the analysis' own number 30 never appears.
    """
    from kicadgen.services.component_check import document_checks
    raw = {"layout": "castellated", "pin_numbers_printed": False,
           "castellated_sides": [
               {"edge": "left", "count": 3, "first_pin": 1, "pitch_mm": 2.54,
                "pin_names": ["GND", "TX", "RX"]},
               {"edge": "right", "count": 3, "first_pin": 4, "pitch_mm": 2.54,
                "pin_names": ["3V3", "EN", "IO0"]}],
           "pinout": [{"pin": str(n), "name": name} for n, name in
                      enumerate(["GND", "TX", "RX", "3V3", "EN", "IO0"], start=1)]
           + [{"pin": "30", "name": "GND_PAD"}]}
    _data, _sides = _convention_case(check_window, monkeypatch, raw)
    document = check_window._component_check_document()
    rows = [{"pin": row["pin"], "name": row["name"]} for row in check_window._convention_pinout
            if row["pin"] != "7"]
    results = document_checks(document, rows, [str(i) for i in range(1, 7)])
    lost = [r for r in results if r.code == "document_unexplained"]
    assert len(lost) == 1 and lost[0].pins == ("7",)
    assert "30" not in lost[0].detail and "domanda" not in lost[0].remedy


def test_the_side_decision_accepts_the_new_answers():
    """@brief Positive: shift and off-side answers are decisions; negative: malformed values are not."""
    from kicadgen.gui.main_window import MainWindow
    for value in ("renumber", "keep_sides", "off_side_entries", "shift:+2", "shift:-11"):
        assert MainWindow._known_side_decision(value), value
    for value in ("shift:2", "shift:+x", "renumber ", None, 3, "ccw:top:b"):
        assert not MainWindow._known_side_decision(value), value


def test_a_shifted_numbering_on_a_bottom_view_blocks_use(check_window, monkeypatch):
    """@brief Negative (audit H1 + M1): the left side numbered after the pinout's end is a
    structural contradiction -> ERROR importance 1 on the entries without a pad."""
    import copy
    from kicadgen.services.component_check import document_checks
    canonical = copy.deepcopy(_CANONICAL)
    canonical[0]["first_pin"] = 10
    document = _bottom_view_case(check_window, monkeypatch, canonical, _PINOUT)
    pads = {"10", "11", "12", "4", "5", "6", "7", "8"}
    rows = [{"pin": pad, "name": ""} for pad in sorted(pads)]
    results = document_checks(document, rows, pads)
    dropped = [r for r in results if r.code == "document_dropped"]
    assert len(dropped) == 1 and dropped[0].status == "ERROR" and dropped[0].importance == 1
    assert dropped[0].pins == ("1", "2", "3")


def test_without_a_document_pinout_no_document_is_passed(check_window, monkeypatch):
    """@brief Negative: no pinout in the main answer -> `document` is None (no document check)."""
    window = check_window
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: {"castellated_sides": []})
    window._verify_component()
    assert _DialogStub.seen["args"]["document"] is None


def test_an_open_question_is_flagged_and_a_stale_answer_is_ignored(check_window, monkeypatch):
    """@brief Positive: an open question is flagged and an answer of ANOTHER digest is not read.

    @note CONTRACT CHANGED 2026-11-13 (coordinator): the decision is the answer of the
        exact question key of the current analysis, never «any saved side_numbering answer».
    """
    window = check_window
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: MAIN_ANSWER)
    window._side_numbering_key = "side_numbering:current"
    window._extraction_questions = [{"key": "side_numbering:current", "field": "side_numbering"}]
    window._extraction_answers = {"side_numbering:older": "keep_sides"}
    window._verify_component()
    document = _DialogStub.seen["args"]["document"]
    assert document["open_question"] is True and document["decision"] is None


def test_without_a_question_in_the_current_analysis_no_decision_is_read(check_window, monkeypatch):
    """@brief Negative: the current analysis asked no side-numbering question -> decision None,
    whatever answers older analyses saved."""
    window = check_window
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: MAIN_ANSWER)
    window._side_numbering_key = None
    window._extraction_questions = []
    window._extraction_answers = {"side_numbering:older": "renumber"}
    window._verify_component()
    assert _DialogStub.seen["args"]["document"]["decision"] is None


def test_the_current_key_is_taken_from_the_applied_analysis_and_saved(window):
    """@brief Positive: applying an analysis records the key of its question (asked or not) and the
    project keeps it; negative: an analysis without the question clears it."""
    from types import SimpleNamespace
    question = {"key": "side_numbering:d1", "field": "side_numbering", "text": "?",
                "options": [{"label": "a", "value": "renumber"}, {"label": "b", "value": "keep_sides"}]}
    window._extraction_answers = {"side_numbering:d1": "keep_sides"}
    window._review_extraction_questions(SimpleNamespace(questions=[question]))
    assert window._side_numbering_key == "side_numbering:d1"
    state = window._project_state()
    assert state["side_numbering_key"] == "side_numbering:d1"
    window._side_numbering_key = None
    window._apply_project_state(state)
    assert window._side_numbering_key == "side_numbering:d1"
    window._review_extraction_questions(SimpleNamespace(questions=[]))
    assert window._side_numbering_key is None


def test_every_run_reads_fresh_arguments(check_window, monkeypatch):
    """@brief Positive: the dialog gets an args BUILDER; a later run reads the window as it is then.

    CONTRACT CHANGED 2026-11-14 (review P2): the arguments used to be a dict
    patched only after a successful fix, so «Ripeti la verifica» after a change
    made elsewhere (e.g. «Solo nome principale» in the Pinout table) reused
    stale ones. Negative: two runs never share one dict.
    """
    window = check_window
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: None)
    monkeypatch.setattr(window, "_reexport_symbol_action", lambda: "fatto")
    window._verify_component()
    builder = _DialogStub.seen["builder"]
    assert callable(builder) and _DialogStub.seen["actions"][ACTION_REEXPORT_SYMBOL]() == "fatto"
    first = builder()
    monkeypatch.setattr(window.symbol_tab, "rows", lambda: [{"pin": "1", "name": "FRESH"}])
    second = builder()
    assert second is not first and second["rows"] == [{"pin": "1", "name": "FRESH"}]


def _footprint_files(window, step=True):
    """Write a minimal .kicad_mod (and the STEP) of the current component."""
    name = window._component_name()
    folder = window._project_dir() / f"{name}.pretty"
    folder.mkdir(parents=True, exist_ok=True)
    footprint = folder / f"{name}.kicad_mod"
    footprint.write_text(f'(footprint "{name}"\n\t(layer "F.Cu")\n)\n', encoding="utf-8")
    if step:
        shapes = window._project_dir() / f"{name}.3dshapes"
        shapes.mkdir(parents=True, exist_ok=True)
        (shapes / f"{name}.step").write_text("ISO-10303-21;\nEND-ISO-10303-21;\n")
    return footprint


def test_the_link_step_model_fix_no_longer_exists(window):
    """@brief Negative (CONTRACT CHANGED 2026-11-13, KiCad workflow review): no window method
    writes a model reference into the component-folder footprint any more."""
    assert not hasattr(window, "_link_step_model_action")


def test_reexport_symbol_uses_the_applied_symbol_writer(window, monkeypatch):
    """@brief Positive: the applied writer runs without a file dialog; negative: pending edits refuse."""
    written = []
    monkeypatch.setattr(window, "_symbol_pad_mismatch", lambda: "")
    monkeypatch.setattr(window, "_save_applied_electrical_symbol",
                        lambda path=None, *, quiet=False: written.append(quiet) or Path("S.kicad_sym"))
    monkeypatch.setattr(window.symbol_tab, "has_pending_edits", lambda: False)
    monkeypatch.setattr(window.symbol_tab, "confirm_export_review", lambda: True)
    assert "S.kicad_sym" in window._reexport_symbol_action() and written == [True]
    monkeypatch.setattr(window.symbol_tab, "has_pending_edits", lambda: True)
    with pytest.raises(RuntimeError):
        window._reexport_symbol_action()
    assert written == [True]
    monkeypatch.setattr(window.symbol_tab, "has_pending_edits", lambda: False)
    monkeypatch.setattr(window, "_save_applied_electrical_symbol", lambda path=None, *, quiet=False: None)
    with pytest.raises(RuntimeError):
        window._reexport_symbol_action()


def test_reexport_asks_the_same_review_question_as_the_manual_export(window, monkeypatch):
    """@brief Positive: declining the review question writes nothing (KiCad workflow review A6)."""
    written = []
    monkeypatch.setattr(window, "_symbol_pad_mismatch", lambda: "")
    monkeypatch.setattr(window.symbol_tab, "has_pending_edits", lambda: False)
    monkeypatch.setattr(window, "_save_applied_electrical_symbol",
                        lambda path=None, *, quiet=False: written.append(1) or Path("S.kicad_sym"))
    asked = []
    monkeypatch.setattr(window.symbol_tab, "confirm_export_review", lambda: asked.append(1) or False)
    with pytest.raises(RuntimeError):
        window._reexport_symbol_action()
    assert asked == [1] and written == []


FIXABLE = klc_checks.Finding("footprint_name_spaces", "warning", "Nome con spazi",
                             fix=klc_checks.FIX_NAME_SPACES, fix_value="MY_PART")
PLAIN = klc_checks.Finding("description_short", "warning", "Descrizione corta")


def _fix_buttons(window):
    """The «Correggi» buttons of the KLC list, by row."""
    buttons = {}
    for row in range(window.klc_list.count()):
        holder = window.klc_list.itemWidget(window.klc_list.item(row))
        found = holder.findChildren(QPushButton) if holder is not None else []
        buttons[row] = found
    return buttons


def test_correggi_is_offered_only_for_a_finding_with_a_fix(window, monkeypatch):
    """@brief Positive: the fixable row has «Correggi»; negative: the other row has none."""
    monkeypatch.setattr(window, "_klc_findings", lambda: [FIXABLE, PLAIN])
    window._refresh_klc_findings()
    buttons = _fix_buttons(window)
    assert [len(buttons[row]) for row in range(window.klc_list.count())] == [1, 0]
    assert buttons[0][0].text() == "Correggi"
    assert buttons[0][0].property("klc_fix") == klc_checks.FIX_NAME_SPACES


@pytest.mark.parametrize("answer, renamed", [(QMessageBox.StandardButton.Yes, ["MY_PART"]),
                                             (QMessageBox.StandardButton.No, [])])
def test_correggi_renames_through_the_rename_path_after_confirmation(window, monkeypatch, answer, renamed):
    """@brief Positive: «Sì» renames to the proposed name; negative: «No» renames nothing."""
    monkeypatch.setattr(window, "_klc_findings", lambda: [FIXABLE])
    window._refresh_klc_findings()
    # The metadata group is enabled once the component has its footprint.
    _footprint_files(window)
    window._refresh_output_actions()
    assert _fix_buttons(window)[0][0].isEnabled()
    # The finding computed from the project's CURRENT name (A4 re-checks it).
    current = window._component_name()
    finding = klc_checks.Finding("footprint_name_spaces", "warning", "Nome con spazi",
                                 fix=klc_checks.FIX_NAME_SPACES,
                                 fix_value=klc_checks.proposed_name_without_spaces(current))
    monkeypatch.setattr(window, "_klc_findings", lambda: [finding])
    window._refresh_klc_findings()
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or answer)
    calls = []
    monkeypatch.setattr(window, "_apply_project_rename", lambda name: calls.append(name) or name)
    _fix_buttons(window)[0][0].click()
    assert len(asked) == 1 and finding.fix_value in asked[0]
    # A5: the confirmation says what happens to copies already installed.
    assert "nome vecchio" in asked[0]
    assert calls == [finding.fix_value for _name in renamed]


def test_correggi_refuses_a_finding_about_an_older_name(window, monkeypatch):
    """@brief Negative (A4): the project name changed since the check -> no question, no rename."""
    assert FIXABLE.fix_value != window._component_name()
    asked, calls, infos = [], [], []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: infos.append(a[2]))
    monkeypatch.setattr(window, "_apply_project_rename", lambda name: calls.append(name) or name)
    assert window._apply_klc_fix(FIXABLE) is False
    assert asked == [] and calls == [] and infos and "cambiato" in infos[0]


def test_a_finding_is_fixed_by_its_id_never_by_its_text(window, monkeypatch):
    """@brief Negative: a finding whose text talks of spaces but carries no fix id renames nothing."""
    calls = []
    monkeypatch.setattr(window, "_apply_project_rename", lambda name: calls.append(name) or name)
    lookalike = klc_checks.Finding("footprint_name_spaces", "warning", "contiene spazi")
    assert window._apply_klc_fix(lookalike) is False and calls == []


def _extractor_runs(tmp_path, monkeypatch):
    """A fresh analysis with its open «side_numbering» question, then the same documents
    analysed again with the saved answer «keep_sides» (offline, faked model)."""
    from test_extractor_side_pinout import _run, _shifted, _side_questions
    from kicadgen.services import datasheet_extractor as de
    monkeypatch.setattr(de, "api_key", lambda: "offline")
    monkeypatch.setattr(de, "check_credit", lambda **kw: None)
    fresh, _call, _lines = _run(tmp_path, monkeypatch, [_shifted("devkit"), _shifted("devkit")])
    (question,) = _side_questions(fresh)
    again, _call, _lines = _run(tmp_path, monkeypatch, [_shifted("devkit"), _shifted("devkit")],
                                answers={question["key"]: "keep_sides"})
    return fresh, again, question


def _load_sides(window, params):
    """Put the analysis' sides into the window, as the apply does (labels kept canonical)."""
    window.group_castellated.setChecked(True)
    window._sides.clear()
    for side in params.castellated_sides:
        window._add_side_row(dict(side, contact_style="through_hole"))
    window._document_sides = window._canonical_document_sides(params.castellated_sides)


def test_a_silently_reapplied_keep_sides_is_the_users_decision(window, monkeypatch, tmp_path):
    """@brief Positive: the extractor re-applied a saved «keep_sides» without asking ->
    decision «keep_sides» and no importance-1 document error («answer the question»).

    @note CONTRACT CHANGED 2026-11-13 (audit H1): a shift needs positive evidence; the
        fixture keeps a real shift, so the same situation WITHOUT the decision still blocks
        (only a shifted numbering does).
    """
    from kicadgen.services.component_check import document_checks
    _fresh, again, question = _extractor_runs(tmp_path, monkeypatch)
    window._extraction_answers = {question["key"]: "keep_sides"}
    window._review_extraction_questions(again)
    assert window._side_numbering_key == question["key"]
    _load_sides(window, again)
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: again.raw_main)
    document = window._component_check_document()
    assert document["decision"] == "keep_sides" and document["open_question"] is False
    rows = [{"pin": str(row["pin"]), "name": row["name"]} for row in again.pinout]
    results = document_checks(document, rows, None)
    assert results and not [r for r in results if r.status == "ERROR" and r.importance == 1]
    # The same situation without the decision is a blocking error: the decision is
    # what turns it into the user's choice, not a lack of contradictions.
    unknown = document_checks(dict(document, decision=None), rows, None)
    assert [r for r in unknown if r.status == "ERROR" and r.importance == 1]


def test_a_fresh_analysis_leaves_the_question_open_and_no_decision(window, monkeypatch, tmp_path):
    """@brief Negative: no saved answer -> the question stays open (back), decision None."""
    from kicadgen.gui import main_window as gui
    fresh, _again, question = _extractor_runs(tmp_path, monkeypatch)
    window._extraction_answers = {}
    monkeypatch.setattr(window, "_ask_extraction_question", lambda q: gui.QUESTION_BACK)
    window._review_extraction_questions(fresh)
    assert window._side_numbering_key == question["key"]
    _load_sides(window, fresh)
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: fresh.raw_main)
    document = window._component_check_document()
    assert document["decision"] is None and document["open_question"] is True


def test_a_rename_follows_the_automatic_footprint_filter():
    """@brief Positive (A2): the automatic filter token is renamed; negative: a typed filter stays."""
    from kicadgen.gui.main_window import fp_filter_pattern, renamed_symbol_text
    text = ('(kicad_symbol_lib (symbol "OLD NAME" (property "Value" "OLD NAME")'
            ' (property "ki_fp_filters" "OLD?NAME* Conn_*")))')
    renamed = renamed_symbol_text(text, "OLD NAME", "NEW_NAME")
    assert '(property "ki_fp_filters" "NEW_NAME* Conn_*")' in renamed
    typed = text.replace("OLD?NAME* Conn_*", "Custom_*")
    assert '"ki_fp_filters" "Custom_*"' in renamed_symbol_text(typed, "OLD NAME", "NEW_NAME")
    assert fp_filter_pattern("MY PART") == "MY?PART*" and " " not in fp_filter_pattern("A B  C")
    assert fp_filter_pattern("") == ""


def test_the_default_footprint_filter_is_one_token(window):
    """@brief Positive (A2): the default filter is `fp_filter_pattern` of the name (one token,
    see test_a_rename_follows_the_automatic_footprint_filter); negative: a typed filter wins."""
    from kicadgen.gui.main_window import fp_filter_pattern
    window.field_fp_filters.setText("")
    assert window._effective_fp_filters() == fp_filter_pattern(window._component_name())
    window.field_fp_filters.setText("Typed_*")
    assert window._effective_fp_filters() == "Typed_*"
    window.field_fp_filters.setText("")


def test_stale_previews_lose_every_file_of_the_old_name(window, tmp_path):
    """@brief Positive (A3): schematic, symbol copy, footprint copy and 3D image of the old name
    are removed; negative: files of the new name stay, and an open schematic keeps everything."""
    from kicadgen.services.kicad_library import PREVIEW_DIR_NAME, kicad_lock_file
    folder = tmp_path / PREVIEW_DIR_NAME
    (folder / "KiCadGen_Preview.pretty").mkdir(parents=True)
    old = [folder / "OLD-preview.kicad_pcb", folder / "OLD-preview.kicad_sch", folder / "OLD.kicad_sym",
           folder / "KiCadGen_Preview.pretty" / "OLD.kicad_mod", folder / "OLD-preview-3d.png"]
    new = folder / "NEW-preview.kicad_sch"
    for path in old + [new]:
        path.write_text("x")
    lock = kicad_lock_file(folder / "OLD-preview.kicad_sch")
    lock.write_text("lock")
    window._remove_stale_previews(tmp_path, "OLD")
    assert all(path.exists() for path in old), "an open schematic keeps the old preview"
    lock.unlink()
    window._remove_stale_previews(tmp_path, "OLD")
    assert not any(path.exists() for path in old) and new.exists()


def test_a_rename_refused_by_an_open_file_says_to_close_kicad(window, monkeypatch, tmp_path):
    """@brief Positive (A7): a PermissionError while renaming names KiCad as the likely holder.

    @note Negative: another failure (an existing target) does not mention KiCad.
    """
    logged = []
    monkeypatch.setattr(window, "_append_log", lambda text, *a, **k: logged.append(str(text)))
    old = window._component_name()
    window._project_dir().mkdir(parents=True, exist_ok=True)

    def refuse(self, target):
        raise PermissionError(13, "access denied", str(self))
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", refuse)
        assert window._rename_project_folder("NUOVO_NOME") == old
    assert any("chiudi KiCad" in line for line in logged), logged
    logged.clear()
    (window._project_dir().parent / "ESISTENTE").mkdir()
    assert window._rename_project_folder("ESISTENTE") == old
    assert logged and not any("chiudi KiCad" in line for line in logged)
