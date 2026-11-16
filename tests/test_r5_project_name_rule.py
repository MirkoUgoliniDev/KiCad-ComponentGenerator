"""@brief The project-name dialog applies the generated files' own name rule (review 2026-11-15, round 5, item 2).

@details `project_dialogs` kept its own copy of the forbidden characters
(`<>:"/\\|?*`) and accepted «50%», which the generation then refused
(`output_names.KICAD_UNSAFE_NAME_CHARACTERS`: kicad-cli 10 reads «%» in a
footprint or symbol name as a printf format and crashes). The project name
becomes the component name, so the dialog now decides with
`output_names.validate_output_name` and shows its message naming the
character. Positive and negative twins.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QDialogButtonBox  # noqa: E402

from kicadgen.core.output_names import is_valid_output_name, validate_output_name  # noqa: E402
from kicadgen.gui import project_dialogs  # noqa: E402
from kicadgen.gui.project_dialogs import ProjectNameDialog, name_problem  # noqa: E402


def _kicad_message(name):
    """The shared rule's own refusal of `name`."""
    with pytest.raises(ValueError) as caught:
        validate_output_name(name)
    return str(caught.value)


def test_a_percent_name_is_refused_with_the_shared_message(tmp_path):
    """@brief Positive: «50%» is refused with the generation's message naming «%»; negative
    twin: «50pct», the same name without the character, is accepted."""
    problem = name_problem(tmp_path, "50%")
    assert problem == _kicad_message("50%") and "%" in problem
    assert name_problem(tmp_path, "50pct") == ""


@pytest.mark.parametrize("name, fragment", [
    ("a<b", "< > :"), ("a%<b", "< > :"), ("fine.", "finire"), ("fine%.", "finire"), ("...", "finire")])
def test_other_refusals_keep_their_specific_message(tmp_path, name, fragment):
    """@brief Negative: a name that is wrong for another reason as well gets the dialog's own
    message for it, never the «%» one (which would hide the other fault)."""
    assert fragment in name_problem(tmp_path, name)


@pytest.mark.parametrize("name", ["50%", "x%NOPE%y", "50pct", "a<b", "a:b", "fine.", "ok.name",
                                  "Relay_SPDT", "a\x01b", "..", "Modulo ESP32"])
def test_the_dialog_and_the_generation_agree_on_every_name(tmp_path, name):
    """@brief One source of truth: a name the dialog accepts is one the generation accepts, and
    a name the generation refuses is refused by the dialog (Windows-reserved device names are
    the dialog's only extra refusal; none is in this table)."""
    assert (name_problem(tmp_path, name) == "") == is_valid_output_name(name.strip())
    assert not hasattr(project_dialogs, "_FORBIDDEN")


def test_the_dialog_shows_the_percent_refusal_and_keeps_ok_off(qapp, tmp_path):
    """@brief Positive: typing «50%» disables OK and shows the message naming «%»; negative twin:
    «50pct» enables OK with no message."""
    dialog = ProjectNameDialog(tmp_path, "Nuovo progetto")
    try:
        ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        dialog.field.setText("50%")
        assert not ok.isEnabled()
        assert dialog.problem.text() == _kicad_message("50%")
        dialog.field.setText("50pct")
        assert ok.isEnabled() and dialog.problem.text() == ""
    finally:
        dialog.deleteLater()


def test_the_already_translated_refusal_passes_a_catalog_lookup_unchanged(qapp, tmp_path):
    """@brief The «%» refusal is returned already translated (it names the character, so it is no
    catalog key): a translator that falls back to its input, as `I18n.text` does, shows it as
    is. Negative twin: a catalogued source («Scrivi il nome del progetto.») is translated."""
    catalog = {"Scrivi il nome del progetto.": "Write the project name."}

    def text(source):
        """`I18n.text`-like lookup: the catalog entry, or the source itself."""
        return catalog.get(source, source)

    assert text(name_problem(tmp_path, "50%")) == _kicad_message("50%")
    assert text(name_problem(tmp_path, "")) == "Write the project name."
