"""@brief The «Sigla del componente» field (user, 2026-10-31: «usiamo il nome del progetto come sigla»).

The part number is saved with the project, falls back to the project name
when left empty, and is never inherited by a project that does not declare it.
"""


def test_an_empty_part_number_searches_with_the_project_name_without_storing_it(window):
    window.field_part_number.clear()
    name = window._component_name()
    assert name, "the window fixture opens a named project"
    assert window._part_number_for_analysis() == name
    assert window.field_part_number.text() == "", "a fallback is not saved as typed data"


def test_a_typed_part_number_wins_over_the_project_name(window):
    window.field_part_number.setText("  G5V-2-H1  ")
    assert window._part_number_for_analysis() == "G5V-2-H1"
    assert window.field_part_number.text() == "  G5V-2-H1  "


def test_the_part_number_is_saved_and_never_inherited(window):
    window.field_part_number.setText("G5V-2-H1")
    state = window._project_state()
    assert state["widgets"]["field_part_number"] == "G5V-2-H1"
    window.field_part_number.setText("other")
    window._apply_project_state(state)
    assert window.field_part_number.text() == "G5V-2-H1"
    # A project saved before the field existed does not keep the previous one.
    older = dict(state, widgets={k: v for k, v in state["widgets"].items()
                                 if k != "field_part_number"})
    window._apply_project_state(older)
    assert window.field_part_number.text() == ""
