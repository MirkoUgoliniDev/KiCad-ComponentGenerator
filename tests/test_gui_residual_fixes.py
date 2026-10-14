"""@brief Regression tests of the residual fixes after the four-cycle review (2026-10-14).

G1: the crash-report scrubber masks UNC paths after ``[ : , <``, user names
with an apostrophe, and spaced user names whose end the text proves (a
closing quote, or the same name proved elsewhere), without eating prose or
masking non-personal paths. G2: an extraction that fails before applying
anything keeps a pending footprint rewrite and retries it once; a run that
applied its result still hands the edit to the next generation.

Each rule has a positive and a negative case that differ structurally (a
token start or not, a per-user share or not, a quote that closes the path or
prose, a result applied or not), never by a tuned number.
"""
import pytest
from PyQt6.QtWidgets import QMessageBox

from kicadgen import crash_report as cr
from kicadgen.core.footprint import validate_footprint_drawings
from tests.test_gui_review_fixes import _turns, busy_window, record  # noqa: F401 - fixture

B = "\\"


# ── G1a: UNC paths after «[», «:», «,», «<» ─────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("[" + B * 2 + "srv" + B + "home" + B + "mario" + B + "x]",
     "[" + B * 2 + "srv" + B + "home" + B + "<user>" + B + "x]"),
    ("error:" + B * 2 + "srv" + B + "Users" + B + "anna" + B + "x",
     "error:" + B * 2 + "srv" + B + "Users" + B + "<user>" + B + "x"),
    ("a," + B * 2 + "srv" + B + "profiles" + B + "bob" + B + "x",
     "a," + B * 2 + "srv" + B + "profiles" + B + "<user>" + B + "x"),
    ("<" + B * 2 + "srv" + B + "homes" + B + "carl" + B + "x>",
     "<" + B * 2 + "srv" + B + "homes" + B + "<user>" + B + "x>"),
])
def test_unc_user_paths_after_brackets_colons_and_commas_are_masked(text, expected):
    assert cr.scrub(text) == expected


@pytest.mark.parametrize("text, expected", [
    # A one-letter word and a colon before a UNC path whose leading run is
    # twice its inner separator (raw 2/1, repr 4/2): a label, not a drive.
    ("a:" + B * 2 + "srv" + B + "home" + B + "bob" + B + "x",
     "a:" + B * 2 + "srv" + B + "home" + B + "<user>" + B + "x"),
    ("see x:" + B * 2 + "nas" + B + "Users" + B + "ann" + B + "y",
     "see x:" + B * 2 + "nas" + B + "Users" + B + "<user>" + B + "y"),
    ("'a:" + B * 4 + "srv" + B * 2 + "home" + B * 2 + "bob" + B * 2 + "x'",
     "'a:" + B * 4 + "srv" + B * 2 + "home" + B * 2 + "<user>" + B * 2 + "x'"),
])
def test_a_unc_path_after_a_one_letter_label_is_masked(text, expected):
    assert cr.scrub(text) == expected


@pytest.mark.parametrize("text", [
    # A drive path keeps one separator width throughout: raw or repr.
    "C:" + B + "srv" + B + "home" + B + "mario",
    "x=C:" + B * 2 + "srv" + B * 2 + "home" + B * 2 + "mario",
    "[D:" + B * 2 + "srv" + B * 2 + "home" + B * 2 + "mario]",
    "'a:" + B * 4 + "srv" + B * 4 + "home" + B * 4 + "bob'",
    # The same starts before a share that does not name a person.
    "[" + B * 2 + "srv" + B + "Projects" + B + "mario" + B + "x]",
    "error:" + B * 2 + "srv" + B + "KiCad" + B + "libs",
    # A doubled separator inside a word is not a token start.
    "ab" + B * 2 + "srv" + B + "home" + B + "mario",
])
def test_drive_paths_and_non_user_shares_stay_untouched(text):
    assert cr.scrub(text) == text


# ── G1b: user names with an apostrophe ──────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("C:" + B + "Users" + B + "O'Brien" + B + "app" + B + "x.py",
     "C:" + B + "Users" + B + "<user>" + B + "app" + B + "x.py"),
    ("at /home/d'angelo/src", "at /home/<user>/src"),
    ("C:" + B + "Users" + B + "Mary O'Neil" + B + "x",
     "C:" + B + "Users" + B + "<user>" + B + "x"),
    (B * 2 + "srv" + B + "home" + B + "O'Hara" + B + "x",
     B * 2 + "srv" + B + "home" + B + "<user>" + B + "x"),
])
def test_an_apostrophe_inside_a_user_name_is_masked_with_it(text, expected):
    scrubbed = cr.scrub(text)
    assert scrubbed == expected


@pytest.mark.parametrize("text, expected", [
    # A quote that closes the string is not part of the name, nor is what follows.
    ("['/home/bob', 'C:" + B * 2 + "x']", "['/home/<user>', 'C:" + B * 2 + "x']"),
    ("open('/Users/ann')", "open('/Users/<user>')"),
    ("'C:" + B * 2 + "Users" + B * 2 + "bob' not found",
     "'C:" + B * 2 + "Users" + B * 2 + "<user>' not found"),
])
def test_a_closing_quote_is_not_taken_as_an_apostrophe(text, expected):
    assert cr.scrub(text) == expected


# ── G1c: a spaced user name with nothing after it ───────────────────────

@pytest.mark.parametrize("text, expected", [
    # The quote that opened the path closes right after the name.
    ("FileNotFoundError: [Errno 2] No such file: 'C:" + B * 2 + "Users" + B * 2 + "Mario Rossi'",
     "FileNotFoundError: [Errno 2] No such file: 'C:" + B * 2 + "Users" + B * 2 + "<user>'"),
    ('cwd="/home/jane doe"', 'cwd="/home/<user>"'),
    # The same name is proved elsewhere in the report (a traceback line).
    ('File "C:' + B + "Users" + B + "Mario Rossi" + B + 'app.py", line 3\n'
     "RuntimeError: cwd is C:" + B + "Users" + B + "Mario Rossi",
     'File "C:' + B + "Users" + B + "<user>" + B + 'app.py", line 3\n'
     "RuntimeError: cwd is C:" + B + "Users" + B + "<user>"),
    ("/home/Ann Lee/x and later /home/ann lee.", "/home/<user>/x and later /home/<user>."),
    # A possessive after a proved name does not leave the surname behind.
    ("C:" + B + "Users" + B + "Mario Rossi" + B + "x then C:" + B + "Users" + B
     + "Mario Rossi's notes",
     "C:" + B + "Users" + B + "<user>" + B + "x then C:" + B + "Users" + B + "<user>'s notes"),
])
def test_a_spaced_name_whose_end_is_proved_is_masked_whole(text, expected):
    assert cr.scrub(text) == expected


@pytest.mark.parametrize("text, expected", [
    # No proof: same shape as prose after a path, the prose stays (R6a).
    ("/Users/alice is missing; retry in 5 s", "/Users/<user> is missing; retry in 5 s"),
    ("open C:" + B + "Users" + B + "Mario Rossi", "open C:" + B + "Users" + B + "<user> Rossi"),
    # Mismatched quotes prove nothing.
    ("msg '/home/ann lee\" x", "msg '/home/<user> lee\" x"),
    # A proved name does not swallow a longer word that starts like it.
    ("/home/Mario Rossi/x then /home/Mario Rossini",
     "/home/<user>/x then /home/<user> Rossini"),
    # A quoted sentence that runs into another path is not a name.
    ("'/home/bob and then /tmp'", "'/home/<user> and then /tmp'"),
])
def test_an_unproved_spaced_name_keeps_the_text_after_it(text, expected):
    assert cr.scrub(text) == expected


# ── G2: a failed extraction keeps the pending footprint rewrite ─────────

@pytest.fixture
def pending_edit(busy_window, qapp, monkeypatch):
    """Window with a drawing edit saved while busy, the extraction about to finish."""
    window, busy, started = busy_window
    busy["on"] = True
    window._apply_footprint_drawings(validate_footprint_drawings([record()]))
    # CONTRACT CHANGED 2026-10-14 (footprint draft, user): the edit is saved
    # (and its rewrite armed) by «Applica», no longer by the edit itself.
    window.apply_footprint_draft()
    _turns(qapp)
    assert window._fp_rewrite_dirty and started == []
    busy["on"] = False
    monkeypatch.setattr(window, "_analysis_result_is_current", lambda: True)
    monkeypatch.setattr(window, "_show_ai_cost", lambda: None)
    monkeypatch.setattr(window, "_offer_inventory_review", lambda: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    window._applying_extraction = False
    window._automatic_generation_active = False
    window._auto_generation_params = None
    # As a new run's start does: `acquire` applied a result outside any run.
    window._extraction_applied_revision = None
    return window, busy, started


def test_a_failed_extraction_keeps_and_retries_the_rewrite_once(pending_edit, qapp):
    window, _busy, started = pending_edit
    window._on_extract_error("network down")
    window._on_extract_finished()
    _turns(qapp)
    assert len(started) == 1, "the edit is written: no field changed"
    assert not window._fp_rewrite_dirty
    # A later finished handler does not write it again.
    window._on_generation_finished()
    _turns(qapp)
    assert len(started) == 1


@pytest.mark.parametrize("apply_raises", [False, True])
def test_an_applied_extraction_hands_the_edit_to_the_next_generation(
        pending_edit, qapp, monkeypatch, apply_raises):
    """Negative: a result that reached the fields (even half way) owns the output."""
    window, _busy, started = pending_edit

    def apply(params):
        if apply_raises:
            raise ValueError("bad geometry")

    monkeypatch.setattr(window, "_apply_extracted_result", apply)
    window._on_extracted(object())
    window._on_extract_finished()
    _turns(qapp)
    assert started == [], "no footprint-only rewrite with unreviewed fields"
    assert not window._fp_rewrite_dirty


def test_a_failed_extraction_waits_while_the_pipeline_owns_the_output(pending_edit, qapp):
    window, _busy, started = pending_edit
    window._inventory_pause_active = True
    try:
        window._on_extract_error("network down")
        window._on_extract_finished()
        _turns(qapp)
        assert started == [] and window._fp_rewrite_dirty, "kept, not written"
    finally:
        window._inventory_pause_active = False
    window._on_generation_finished()
    _turns(qapp)
    assert len(started) == 1


def test_an_applied_but_abandoned_run_does_not_mark_a_later_failed_one(
        pending_edit, qapp, monkeypatch):
    """The «applied» mark of an abandoned run (revision bumped) does not leak."""
    window, _busy, started = pending_edit
    monkeypatch.setattr(window, "_apply_extracted_result", lambda params: None)
    window._on_extracted(object())          # run A applies...
    window._analysis_revision += 1          # ...and is abandoned before its finish
    window._on_extract_error("network down")  # run B fails
    window._on_extract_finished()
    _turns(qapp)
    assert len(started) == 1
