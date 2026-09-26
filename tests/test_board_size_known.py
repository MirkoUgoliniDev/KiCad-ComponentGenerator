"""@brief Board size is unknown until an analysis or the user declares it (2026-09-26).

Before, the fields' starting values (42.8 x 15.2 mm, an old test module)
always counted as the board size: the YOLO pretest calibrated the photo on
them and the document cards judged photos against them.
"""
from PyQt6.QtWidgets import QDialog, QDoubleSpinBox

from tests.test_photo_inventory_gui import window, gui, raw_params  # noqa: F401 - fixture condivise


def test_a_new_project_does_not_pretend_to_know_the_board_size(window):  # noqa: F811
    window._board_size_known = False
    assert window._body_size() is None


def test_the_pretest_asks_the_size_and_does_not_start_without_it(window, monkeypatch):  # noqa: F811
    window._board_size_known = False
    asked, started = [], []
    monkeypatch.setattr(window, "_ask_board_size", lambda: asked.append(True) or None)
    monkeypatch.setattr(window, "_pretest_top_views", lambda *args: started.append(True) or [])
    window._start_yolo_pretest([], None, None)
    assert asked == [True] and started == []


def test_the_declared_size_goes_into_the_fields_of_the_current_type(window, monkeypatch):  # noqa: F811
    window._board_size_known = False
    window.group_castellated.setChecked(True)

    def accept(dialog):
        spins = dialog.findChildren(QDoubleSpinBox)
        spins[0].setValue(63.3)
        spins[1].setValue(25.4)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", accept)
    assert window._ask_board_size() == (63.3, 25.4)
    assert window._board_size_known and window._body_size() == (63.3, 25.4)
    assert window.field_cast_body_length.value() == 63.3


def test_cancelling_leaves_the_size_unknown(window, monkeypatch):  # noqa: F811
    """@brief Negativo: annullare non inventa quote."""
    window._board_size_known = False
    monkeypatch.setattr(QDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected)
    assert window._ask_board_size() is None
    assert not window._board_size_known and window._body_size() is None


def test_an_analysis_makes_the_size_known(window, raw_params):  # noqa: F811
    window._board_size_known = False
    window._on_extracted(raw_params)
    assert window._board_size_known and window._body_size() is not None


def test_the_flag_is_saved_and_old_projects_follow_their_footprint(window):  # noqa: F811
    window._board_size_known = True
    state = window._project_state()
    assert state["board_size_known"] is True
    older = {key: value for key, value in state.items() if key != "board_size_known"}
    window._apply_project_state(older)
    assert window._board_size_known is False, "nessun footprint: quote non note"
    name = window._component_name()
    footprint = window._project_dir() / f"{name}.pretty" / f"{name}.kicad_mod"
    footprint.parent.mkdir(parents=True, exist_ok=True)
    footprint.write_text("(footprint)", encoding="utf-8")
    window._apply_project_state(older)
    assert window._board_size_known is True, "footprint esistente: un'analisi le ha scritte"
