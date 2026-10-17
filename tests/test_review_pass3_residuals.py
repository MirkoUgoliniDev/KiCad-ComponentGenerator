"""@brief Review pass 3 residuals: name provenance, legacy body evidence, KLC cache key (2026-10-15).

L1: the provenance of a pin name is judged against the LAST ANALYSIS, so a
    name typed back to the analysis's clears it (an undo plus «Applica»
    used to record the analysis name as the user's).
L4: a project saved before `body_size_declared` with a documented
    10 x 10 x 5 mm body counted as the placeholder; the STEP record or the
    `.preview.json` built with that body is evidence it was analysed.
KLC: the file-check cache is keyed by the language CODE, never by a
    translated sentence two languages could share.
"""
import json

from kicadgen.gui import main_window as gui
from kicadgen.services import klc_checks, messages
from tests.ui_flow_helpers import header_extraction, prepare, run_extraction

NAME = "HDR"


def _type(window, row, name):
    window.tabs.setCurrentIndex(gui.TAB_SYMBOL)
    window.symbol_tab.table.item(row, 1).setText(name)
    window.symbol_tab.apply_button.click()
    window.tabs.setCurrentIndex(0)


# ── L1 ─────────────────────────────────────────────────────────────────────

def test_typing_the_analysis_name_back_clears_the_provenance(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, NAME)
    run_extraction(window, header_extraction(cols=2, pinout=[{"pin": "1", "name": "A1"}]))
    assert window._analysis_pin_names == {"1": "A1"}
    _type(window, 0, "VCC")
    assert window._user_pin_names == {"1": "VCC"}                 # negative: a user name
    _type(window, 0, "A1")                                        # back to the analysis's
    assert window._user_pin_names == {}
    asked = []
    monkeypatch.setattr(gui.MainWindow, "_ask_user_pin_names",
                        lambda self, *a, **k: asked.append(a) or self.USER_PIN_NAMES_KEEP)
    run_extraction(window, header_extraction(cols=2, pinout=[{"pin": "1", "name": "B1"}]))
    assert asked == [] and {str(r["pin"]): r["name"] for r in window.pinout}["1"] == "B1"


def test_the_analysis_names_survive_reopening(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, NAME)
    run_extraction(window, header_extraction(cols=2, pinout=[{"pin": "1", "name": "A1"}]))
    folder = window._project_dir()
    assert window._save_project_config(force=True)
    window._switch_project("Other", window._general_project_state)
    window._open_project_at(folder)
    assert window._analysis_pin_names == {"1": "A1"}


# ── L4 ─────────────────────────────────────────────────────────────────────

def _legacy_state(window, length=10.0, width=10.0, height=5.0):
    state = window._project_state()
    state.pop("body_size_declared", None)
    state.pop("step_source_body", None)
    state["widgets"] = dict(state.get("widgets") or {}, field_body_length=length,
                            field_body_width=width, field_body_height=height)
    return state


def test_a_documented_placeholder_sized_body_counts_as_declared_with_evidence(window, monkeypatch, tmp_path):  # noqa: F811
    prepare(window, monkeypatch, tmp_path, NAME)
    run_extraction(window, header_extraction(cols=2))
    folder = window._project_dir()
    shapes = folder / f"{NAME}.3dshapes"
    shapes.mkdir(exist_ok=True)
    mesh = shapes / f"{NAME}.preview.json"
    # Negative: no evidence, placeholder values -> undeclared.
    window._apply_project_state(_legacy_state(window))
    assert not window._body_size_declared
    # Positive: a 3D built with exactly that body (its preview records it).
    mesh.write_text(json.dumps({"body": {"length_mm": 10.0, "width_mm": 10.0, "height_mm": 5.0}}),
                    encoding="utf-8")
    window._apply_project_state(_legacy_state(window))
    assert window._body_size_declared
    # Negative: evidence of ANOTHER body is no evidence for these fields.
    mesh.write_text(json.dumps({"body": {"length_mm": 12.0, "width_mm": 10.0, "height_mm": 5.0}}),
                    encoding="utf-8")
    window._apply_project_state(_legacy_state(window))
    assert not window._body_size_declared
    # Positive: the STEP record of the project, when the file has none.
    mesh.unlink()
    state = _legacy_state(window)
    state["step_source_body"] = {"body": {"length_mm": 10.0, "width_mm": 10.0, "height_mm": 5.0}}
    window._apply_project_state(state)
    assert window._body_size_declared


# ── KLC cache key ──────────────────────────────────────────────────────────

class _Translator:
    """A translator like `I18n` for the test: a language code and a catalogue."""

    def __init__(self, language, entries=None):
        self.language = language
        self.entries = entries or {}

    def raw(self, key, default=None):
        return self.entries.get(key, default)


def test_the_klc_cache_key_is_the_language_code_even_without_entries(monkeypatch):
    previous = messages._translator
    try:
        keys = []
        for code in ("it", "en", "fr", "de"):
            messages.set_translator(_Translator(code).raw)      # no entry at all
            keys.append(klc_checks._language_token())
        assert keys == ["it", "en", "fr", "de"]
        # Two languages whose catalogues coincide still get distinct keys.
        same = {"ui:Attendi la fine del lavoro in corso.": "same text"}
        messages.set_translator(_Translator("fr", same).raw)
        key_fr = klc_checks._language_token()
        messages.set_translator(_Translator("de", same).raw)
        assert key_fr != klc_checks._language_token()
        messages.set_translator(None)
        assert klc_checks._language_token() == "it"               # the sources' language
    finally:
        messages.set_translator(previous)


def test_the_cached_findings_follow_the_language(monkeypatch):
    previous = messages._translator
    text = '(footprint "X" (version 20260206) (layer "F.Cu") (attr smd))\n'
    try:
        messages.set_translator(_Translator("it").raw)
        italian = [f.message for f in klc_checks.check_footprint_text(text)]
        assert italian and not any(m.startswith("EN ") for m in italian)

        class _English(_Translator):
            def raw(self, key, default=None):
                return "EN " + default      # every template translated

        messages.set_translator(_English("en").raw)
        assert all(m.startswith("EN ") for m in (f.message for f in klc_checks.check_footprint_text(text)))
    finally:
        messages.set_translator(previous)
