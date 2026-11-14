"""@brief Fixes of the pre-commit adversarial review (2026-11-13), window/check/library side.

@details (1) a manual side edit re-derives the labelled sides of the
component check; (2) answers that move pads mark the STEP stale; (3) the
missing-footprint reason names the open question of its own field; (4) a
preview schematic open in the Schematic Editor no longer stops the board
refresh; (5) the 3D image window closes on a project switch; (6) the symbol
tab's logged notes reset on a new load; (7) the pinout tooltip names «/» too;
(8) option tooltips never read a label as markup; (10) the removed
«link STEP model» fix leaves no constant behind. Positive and negative cases.
"""

from types import SimpleNamespace

from kicadgen.services import component_check
from kicadgen.services.component_check import document_checks


def _labelled_module(window):
    """A castellated module with two labelled sides, consistent with its pinout."""
    window.group_castellated.setChecked(True)
    window._sides.clear()
    window._add_side_row({"edge": "left", "count": 2, "first_pin": 1, "reverse": False,
                          "contact_style": "half_hole"})
    window._add_side_row({"edge": "right", "count": 2, "first_pin": 3, "reverse": False,
                          "contact_style": "half_hole"})
    window._document_sides = window._canonical_document_sides([
        {"edge": "left", "count": 2, "first_pin": 1, "reverse": False, "pin_names": ["GND", "TX"]},
        {"edge": "right", "count": 2, "first_pin": 3, "reverse": False, "pin_names": ["RX", "VCC"]}])


def test_a_manual_flip_moves_the_labels_with_the_numbers(window):
    """@brief Positive (1): reversing a side by hand pairs its labels with the new numbers;
    negative: a side whose count changed loses its labels (no check rather than a wrong one)."""
    _labelled_module(window)
    window._sides[0]["reverse"] = True
    window._on_side_edited()
    left = next(side for side in window._document_sides if side["edge"] == "left")
    assert left["reverse"] is True and left["pin_names"] == ["GND", "TX"]
    window._sides[1]["count"] = 3
    window._on_side_edited()
    right = next(side for side in window._document_sides if side["edge"] == "right")
    assert right["pin_names"] == []


def test_a_manual_flip_gives_no_false_label_warning(window, monkeypatch):
    """@brief Positive (1): the pinout follows the flipped side -> no «Etichette dei lati» WARN."""
    _labelled_module(window)
    window._sides[0]["reverse"] = True
    window._on_side_edited()
    pinout = [{"pin": "1", "name": "TX"}, {"pin": "2", "name": "GND"},
              {"pin": "3", "name": "RX"}, {"pin": "4", "name": "VCC"}]
    monkeypatch.setattr(window, "_main_answer_for_reread", lambda: {"pinout": pinout})
    window._extraction_questions = []
    document = window._component_check_document()
    rows = [dict(row) for row in pinout]
    results = document_checks(document, rows, {"1", "2", "3", "4"})
    assert not any(r.code in ("document_side_label", "document_label_placement") for r in results)


def test_answers_that_move_pads_mark_the_step_stale(window, monkeypatch):
    """@brief Positive (2): a contact type or row spacing answer that changes something
    invalidates the STEP; negative: an answer that changes nothing does not."""
    _labelled_module(window)
    calls = []
    monkeypatch.setattr(window, "_invalidate_generated_mesh", lambda: calls.append(1))
    updated = SimpleNamespace(castellated_sides=[{"edge": "left", "contact_style": "through_hole"}])
    window._apply_answered_castellated_choice(updated, "contact_style")
    assert calls == [1]
    window._apply_answered_castellated_choice(updated, "contact_style")
    assert calls == [1], "the same style again changes nothing"
    window._declared_row_spacing = None
    window._apply_answered_castellated_choice(SimpleNamespace(row_spacing_mm=22.86), "row_spacing")
    assert calls == [1, 1]
    window._apply_answered_castellated_choice(SimpleNamespace(row_spacing_mm=22.86), "row_spacing")
    assert calls == [1, 1]


def test_the_missing_footprint_reason_names_the_open_question(window):
    """@brief Positive (3): each footprint question gives its own block text; negative: none -> None."""
    for field, words in (("pad_layout", "posizioni dei pad"), ("row_spacing", "distanza fra le file"),
                         ("contact_style", "tipo di contatto"),
                         ("side_numbering", "numerazione dei contatti")):
        window._extraction_questions = [{"key": f"{field}:x", "field": field, "text": "?",
                                         "options": [{"label": "a", "value": 1}]}]
        assert words in window._footprint_missing_reason(), field
    window._extraction_questions = []
    assert window._footprint_missing_reason() is None


def test_the_render_window_closes_on_a_project_switch(window):
    """@brief Positive (5): an open 3D image window is closed and forgotten, a pending render
    becomes stale; negative: with no window the switch still works."""
    closed = []
    window._render_window = SimpleNamespace(close=lambda: closed.append(1))
    serial = getattr(window, "_preview_render_serial", 0)
    state = window._project_state()
    name = window._component_name()
    window._switch_project(name, state)
    assert closed == [1] and window._render_window is None
    assert window._preview_render_serial > serial
    window._switch_project(name, state)
    assert closed == [1]


def test_the_symbol_notes_reset_on_a_new_load(window):
    """@brief Positive (6): a fresh load forgets the notes already logged, so they are logged again."""
    window.symbol_tab._logged_pin_notes = {"una nota"}
    window.symbol_tab.set_data("ALTRO", [], force=True)
    assert "_logged_pin_notes" not in window.symbol_tab.__dict__ or \
        "una nota" not in window.symbol_tab._logged_pin_notes


def test_the_pinout_tooltip_names_the_slash_separator():
    """@brief Positive (7): the switch tooltip says «/» separates functions too, in every language."""
    from kicadgen.gui.pinout_dialog import PRIMARY_NAMES_TIP
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    assert '"/"' in PRIMARY_NAMES_TIP
    entry = DEFAULT_TRANSLATIONS["ui:" + PRIMARY_NAMES_TIP]
    assert all("/" in entry[language] for language in ("it", "en", "fr", "de"))


def test_an_option_tooltip_never_reads_a_label_as_markup(window, monkeypatch):
    """@brief Positive (8): a label with «<» or «&» becomes escaped rich text; negative: a plain
    label stays exactly as written."""
    from kicadgen.gui import main_window as gui
    seen = {}

    def run_modal(box):
        seen["tips"] = [b.toolTip() for b in box.buttons()]
        box.escapeButton().click()
    monkeypatch.setattr(window, "_run_modal", run_modal)
    question = {"key": "contacts_view:x", "field": "contacts_view", "text": "?",
                "options": [{"label": "pin «<A&B>»", "value": "top"},
                            {"label": "18 mm", "value": "bottom"}]}
    assert window._ask_extraction_question(window._valid_extraction_questions([question])[0]) \
        is gui.QUESTION_BACK
    assert seen["tips"][0] == "<qt>pin «&lt;A&amp;B&gt;»</qt>"
    assert seen["tips"][1] == "18 mm"


def test_the_link_step_model_constant_is_gone():
    """@brief Negative (10): no dead action id is left in the check or its dialog."""
    from kicadgen.gui import component_check_dialog
    assert not hasattr(component_check, "ACTION_LINK_STEP_MODEL")
    # CONTRACT CHANGED 2026-11-14 (NC/NO witness): the dialog gained two LIVE
    # fixes (ask the open «contact_nc» question, open the Pinout table). The
    # contract is «no dead action id», not «only the re-export»: every
    # labelled action is one the check can name, and the removed one is gone.
    # CONTRACT CHANGED 2026-11-14 (redesign): the view «Mostra in KiCad» and the
    # hand-offs (KiCad tab, Symbol tab, AI suggestion of the types) are live too.
    live = {component_check.ACTION_REEXPORT_SYMBOL, component_check.ACTION_ANSWER_CONTACT_NC,
            component_check.ACTION_EDIT_PINOUT, component_check.ACTION_SHOW_IN_KICAD,
            component_check.ACTION_OPEN_KICAD_TAB, component_check.ACTION_OPEN_SYMBOL_TAB,
            component_check_dialog.ACTION_SUGGEST_PIN_TYPES}
    assert set(component_check_dialog.ACTION_LABELS) == live
    assert "link_step_model" not in component_check_dialog.ACTION_LABELS
