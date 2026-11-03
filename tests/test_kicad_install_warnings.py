"""@brief The KiCad tab acts on the install plan's warnings (review 2026-11-03).

`plan_component_install` reports a too old KiCad, a «KiCadGen» row of the
other scope that would shadow the install, and KiCad's lock files of an open
project. The window refuses the first and asks about the others; a plan with
none of them asks nothing.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QMessageBox  # noqa: E402


def _plan(**changes):
    values = dict(kicad_too_old=False, kicad_version="10.0", table_path=Path("fp-lib-table"),
                  shadowing=None, kicad_open=False, open_locks=())
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture
def boxes(window, monkeypatch):
    shown = {"warning": [], "question": []}
    answer = {"value": QMessageBox.StandardButton.Yes}
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown["warning"].append(a[2]))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: shown["question"].append(a[2]) or answer["value"])
    return window, shown, answer


def test_a_clean_plan_asks_nothing(boxes):
    window, shown, _answer = boxes
    assert window._confirm_install_plan_warnings(_plan(), project_library=True)
    assert shown == {"warning": [], "question": []}


def test_a_too_old_kicad_is_refused_before_any_question(boxes):
    window, shown, _answer = boxes
    assert not window._confirm_install_plan_warnings(_plan(kicad_too_old=True, kicad_version="9.0"), False)
    assert len(shown["warning"]) == 1 and "9.0" in shown["warning"][0]
    assert shown["question"] == []


def test_a_shadowing_row_and_an_open_project_are_asked_and_can_be_declined(boxes):
    window, shown, answer = boxes
    shadow = SimpleNamespace(table_path=Path("global/fp-lib-table"), library="KiCadGen", uri="C:/libs/KiCadGen.pretty")
    plan = _plan(shadowing=shadow, kicad_open=True, open_locks=(Path("~board.kicad_pcb.lck"),))
    assert window._confirm_install_plan_warnings(plan, project_library=True)
    assert len(shown["question"]) == 2
    assert "C:/libs/KiCadGen.pretty" in shown["question"][0] and "~board.kicad_pcb.lck" in shown["question"][1]
    shown["question"].clear()
    answer["value"] = QMessageBox.StandardButton.No
    assert not window._confirm_install_plan_warnings(plan, project_library=True)
    assert len(shown["question"]) == 1, "a refusal stops at the first question"


def test_only_a_project_install_looks_at_the_global_table(window, monkeypatch):
    import kicadgen.gui.main_window as gui
    monkeypatch.setattr(gui, "detect_external_applications", lambda: SimpleNamespace(kicad_cli="kicad-cli"))
    monkeypatch.setattr(gui, "resolve_global_fp_lib_table", lambda cli: SimpleNamespace(path=Path("g/fp-lib-table")))
    assert window._other_scope_table(True) == Path("g/fp-lib-table")
    assert window._other_scope_table(False) is None


def test_a_project_install_with_an_old_kicad_asks_and_an_unreadable_other_table_is_logged(boxes, monkeypatch):
    """D2/D6 (second review 2026-11-03): an old local KiCad is a question (not a refusal); an unreadable table a log line."""
    window, shown, _answer = boxes
    logged = []
    monkeypatch.setattr(window, "_append_log", logged.append)
    plan = _plan(detected_kicad_too_old=True, detected_kicad_version="9.0.7", shadowing_error="bad table")
    assert window._confirm_install_plan_warnings(plan, project_library=True)
    assert len(shown["question"]) == 1 and "9.0.7" in shown["question"][0]
    assert logged and "bad table" in logged[0]
    shown["question"].clear()
    assert window._confirm_install_plan_warnings(_plan(), project_library=True)
    assert shown["question"] == [], "negative: a current KiCad asks nothing"


def test_a_failed_install_names_the_rollback_folder_left(window, tmp_path):
    """D8: the error text carries the leftover rollback folder; without one it is the plain error."""
    from kicadgen.services.kicad_library import KiCadLibraryError
    left = tmp_path / ".kicadgen-rollback-x"
    with_left = KiCadLibraryError("install_write_refused", path=str(tmp_path), rollback_left=str(left))
    without = KiCadLibraryError("install_write_refused", path=str(tmp_path))
    assert str(left) in window._kicad_error_text_with_rollback(with_left)
    assert str(left) not in window._kicad_error_text_with_rollback(without)
