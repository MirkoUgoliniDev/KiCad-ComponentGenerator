"""@brief The window side of the redesigned «Verifica componente» (2026-11-14).

@details The window records each KiCad installation (scope and library
folder) with the project, so the check compares the installed footprint with
the current one; it routes the dialog's hand-offs (Symbol tab with or without
the AI suggestion, KiCad tab); «Mostra in KiCad» opens the check's preview
without going through `_refuse_kicad_command` (the open dialog counts as
work) but refuses a footprint on disk behind the applied state; the editor's
issues reach the check with their KIND. Positive and negative cases.
"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicadgen.gui import main_window as gui
from kicadgen.services.kicad_library import PreviewLaunch, PreviewProject


def test_an_installation_is_recorded_saved_and_given_to_the_check(window, tmp_path):
    """@brief Positive: one entry per scope, the newest wins; a project library is stored RELATIVE
    to the project (review H) and given to the check as an absolute folder; saved and restored.
    Negative: a project library outside the project, no footprint path, a malformed entry,
    a remote (UNC) folder are never recorded or restored."""
    project = window._project_dir()
    window._record_kicad_install("project", project / "a" / "KiCadGen.pretty" / "X.kicad_mod")
    window._record_kicad_install("global", tmp_path / "g" / "KiCadGen.pretty" / "X.kicad_mod")
    window._record_kicad_install("project", project / "b" / "KiCadGen.pretty" / "X.kicad_mod")
    window._record_kicad_install("project", None)
    assert window._kicad_installs == [
        {"scope": "global", "pretty_dir": str(tmp_path / "g" / "KiCadGen.pretty")},
        {"scope": "project", "pretty_dir": str(Path("b") / "KiCadGen.pretty")}]
    assert window._component_check_args()["installs"] == [
        {"scope": "global", "pretty_dir": str(tmp_path / "g" / "KiCadGen.pretty")},
        {"scope": "project", "pretty_dir": str(project / "b" / "KiCadGen.pretty")}]
    # CONTRACT CHANGED 2026-11-14 (review M1): a KiCad project OUTSIDE the
    # component folder is recorded with its absolute local path.
    elsewhere = (tmp_path / "elsewhere" / "KiCadGen.pretty").resolve()
    window._record_kicad_install("project", elsewhere / "X.kicad_mod")
    assert window._kicad_installs[-1] == {"scope": "project", "pretty_dir": str(elsewhere)}
    assert window._resolved_kicad_installs()[-1]["pretty_dir"] == str(elsewhere)
    state = window._project_state()
    window._kicad_installs = []
    window._apply_project_state(deepcopy(state))
    assert len(window._kicad_installs) == 2
    for bad in ([{"scope": "moon", "pretty_dir": "x"}, "bad"],
                [{"scope": "global", "pretty_dir": "\\\\server\\share\\KiCadGen.pretty"}],
                [{"scope": "global", "pretty_dir": "\\\\?\\C:\\lib\\KiCadGen.pretty"}],
                [{"scope": "project", "pretty_dir": "..\\up\\KiCadGen.pretty"}]):
        window._apply_project_state(dict(state, kicad_installs=bad))
        assert window._kicad_installs == [], bad


@pytest.mark.parametrize("handoff, tab, suggested", [
    ("suggest_pin_types", gui.TAB_SYMBOL, True),
    ("open_symbol_tab", gui.TAB_SYMBOL, False),
    ("open_kicad_tab", gui.TAB_KICAD, False),
])
def test_the_handoffs_are_routed(window, monkeypatch, handoff, tab, suggested):
    """@brief Positive: each hand-off shows its tab; only the AI one asks for the types."""
    asked = []
    monkeypatch.setattr(window, "_suggest_pin_types", lambda: asked.append(1))
    # A project with a component keeps those tabs on; the empty test window would
    # switch them off again on every tab change.
    monkeypatch.setattr(window, "_refresh_output_actions", lambda: None)
    window.tabs.setCurrentIndex(gui.TAB_PROJECT)
    for index in (gui.TAB_SYMBOL, gui.TAB_KICAD):
        window.tabs.setTabEnabled(index, True)
    window._route_check_handoff(handoff)
    assert window.tabs.currentIndex() == tab and bool(asked) is suggested


def test_no_handoff_changes_nothing(window, monkeypatch):
    """@brief Negative: None -> no tab change, no AI call."""
    monkeypatch.setattr(window, "_suggest_pin_types", lambda: pytest.fail("no AI call"))
    window.tabs.setCurrentIndex(gui.TAB_PROJECT)
    window._route_check_handoff(None)
    assert window.tabs.currentIndex() == gui.TAB_PROJECT


def _preview_ready(window, monkeypatch, tmp_path, opened):
    """The window with a footprint on disk and KiCad faked; records what is opened."""
    monkeypatch.setattr(window, "_kicad_output_complete", lambda: True)
    monkeypatch.setattr(window, "_footprint_pending_block", lambda: None)
    monkeypatch.setattr(window, "_footprint_alignment_block", lambda: None)
    monkeypatch.setattr(window, "_current_kicad_artifacts", lambda: SimpleNamespace(has_step=False))
    monkeypatch.setattr(window, "_refuse_kicad_command",
                        lambda: pytest.fail("the open check dialog must not refuse its own view"))
    monkeypatch.setattr(gui, "detect_external_applications", lambda: SimpleNamespace(pcbnew="pcbnew"))
    preview = PreviewProject(tmp_path, tmp_path / "X-preview.kicad_pcb", tmp_path / "X-preview.kicad_pro")
    monkeypatch.setattr(gui, "create_preview_project", lambda artifacts: preview)
    monkeypatch.setattr(gui, "launch_preview_editors",
                        lambda preview, pcbnew: opened.append(preview) or PreviewLaunch(()))


def test_show_in_kicad_opens_the_preview_without_the_busy_gate(window, monkeypatch, tmp_path):
    """@brief Positive: the preview opens and the outcome names the board."""
    opened = []
    _preview_ready(window, monkeypatch, tmp_path, opened)
    outcome = window._show_check_in_kicad()
    assert len(opened) == 1 and "X-preview.kicad_pcb" in outcome


def test_show_in_kicad_refuses_a_footprint_behind_the_applied_state(window, monkeypatch, tmp_path):
    """@brief Negative: a pending footprint rewrite -> RuntimeError with the reason, nothing opened."""
    opened = []
    _preview_ready(window, monkeypatch, tmp_path, opened)
    monkeypatch.setattr(window, "_footprint_pending_block", lambda: ("footprint_pending_write", "Applica ai file"))
    with pytest.raises(RuntimeError, match="Applica ai file"):
        window._show_check_in_kicad()
    assert opened == []


def test_editor_issues_carry_their_kind(window):
    """@brief Positive: every issue has a kind; the texts are the ones `review_issues` returns."""
    window.symbol_tab.set_data("Demo", [{"pin": "1", "name": ""}])
    items = window.symbol_tab.review_issue_items()
    assert {kind for kind, _text, _pins in items} >= {"missing_name", "electrical_type"}
    assert [text for _kind, text, _pins in items] == window.symbol_tab.review_issues()
    assert all(pins == ("1",) for kind, _text, pins in items if kind != "geometry")


# ── Review of 2026-11-14 ──────────────────────────────────────────────────

def test_show_in_kicad_uses_the_applied_symbol_and_the_gate_minus_busy(window, monkeypatch, tmp_path):
    """@brief Positive (F): the preview is built from the APPLIED symbol staged in the preview
    folder; the open dialog («busy») does not refuse. Negative: pads kept blocked -> refused."""
    from kicadgen.services.kicad_library import ComponentArtifacts
    opened, made = [], []
    _preview_ready(window, monkeypatch, tmp_path, opened)
    artifacts = ComponentArtifacts("X", tmp_path, tmp_path / "X.kicad_mod", tmp_path / "X.step")
    monkeypatch.setattr(window, "_current_kicad_artifacts", lambda: artifacts)
    monkeypatch.setattr(window, "_applied_symbol_full_text", lambda: "(kicad_symbol_lib APPLIED)")
    monkeypatch.setattr(window, "_kicad_work_busy", lambda: True)
    preview = PreviewProject(tmp_path, tmp_path / "X-preview.kicad_pcb", tmp_path / "X-preview.kicad_pro")
    monkeypatch.setattr(gui, "create_preview_project",
                        lambda artifacts, output_dir=None: made.append((artifacts, output_dir)) or preview)
    window._show_check_in_kicad()
    (staged, output), = made
    assert (staged.component_dir / "X.kicad_sym").read_text(encoding="utf-8") == "(kicad_symbol_lib APPLIED)"
    assert output == window._project_dir() / ".kicad-preview" and len(opened) == 1
    window._pads_kept_blocked = True
    try:
        with pytest.raises(RuntimeError, match="bloccat"):
            window._show_check_in_kicad()
    finally:
        window._pads_kept_blocked = False
    assert len(opened) == 1


def test_a_value_error_never_escapes_the_kicad_preview_slot(window, monkeypatch, tmp_path):
    """@brief M: «Prova in KiCad» reports a ValueError of the preview instead of raising it."""
    warned = []
    monkeypatch.setattr(window, "_kicad_output_complete", lambda: True)
    monkeypatch.setattr(window, "_refuse_kicad_command", lambda: False)
    monkeypatch.setattr(window, "_current_kicad_artifacts", lambda: SimpleNamespace(has_step=True))
    monkeypatch.setattr(gui, "detect_external_applications", lambda: SimpleNamespace(pcbnew="pcbnew"))

    def broken(artifacts):
        raise ValueError("invalid literal for int(): '²'")
    monkeypatch.setattr(gui, "create_preview_project", broken)
    monkeypatch.setattr(gui.QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
    window._test_in_kicad()
    assert warned and "²" in warned[0]


def test_the_default_model_must_read_the_images_the_run_sends(window, monkeypatch):
    """@brief Q1: a message from `default_model_image_problem` stops the run with a warning;
    negative: none -> the run may start."""
    from kicadgen.services import prompt_models
    warned = []
    monkeypatch.setattr(window, "_ensure_keys_for", lambda models: True)
    monkeypatch.setattr(gui.QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
    seen = []
    monkeypatch.setattr(prompt_models, "default_model_image_problem",
                        lambda model, steps, fields: seen.append((model, tuple(fields))) or "Non legge immagini")
    assert window._ensure_api_key("analysis") is False and warned == ["Non legge immagini"]
    assert seen[0][0] == window._effective_model() and seen[0][1]
    monkeypatch.setattr(prompt_models, "default_model_image_problem", lambda model, steps, fields: None)
    assert window._ensure_api_key("analysis") is True
