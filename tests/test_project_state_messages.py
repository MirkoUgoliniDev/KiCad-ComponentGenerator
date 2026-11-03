"""@brief The errors of the project file are translated like every other message.

Review finding 2026-11-03: «{name} illeggibile: {error}» was built with an
f-string, so it stayed Italian in every interface language.
"""
import pytest

from kicadgen.gui import project_state
from kicadgen.services import messages


@pytest.fixture
def english(monkeypatch):
    """@brief A translator that knows the project-file message used here."""
    catalog = {"ui:{name} illeggibile: {error}": "{name} unreadable: {error}"}
    monkeypatch.setattr(messages, "_translator",
                        lambda key, default="": catalog.get(key, default))


def _config(tmp_path, text):
    path = project_state.project_config_path(tmp_path)
    path.write_text(text, encoding="utf-8")
    return path


def test_an_unreadable_project_file_is_reported_in_the_interface_language(tmp_path, english):
    """@brief Positive: the broken-JSON message goes through the catalog."""
    path = _config(tmp_path, "{not json")
    with pytest.raises(project_state.ProjectConfigError) as caught:
        project_state.read_project_config(tmp_path)
    assert str(caught.value).startswith(f"{path.name} unreadable: ")
    assert "illeggibile" not in str(caught.value)


def test_without_a_translator_the_message_stays_the_italian_source(tmp_path, monkeypatch):
    """@brief Negative: no catalog registered, the Italian template is filled as written."""
    monkeypatch.setattr(messages, "_translator", None)
    path = _config(tmp_path, "{not json")
    with pytest.raises(project_state.ProjectConfigError) as caught:
        project_state.read_project_config(tmp_path)
    assert str(caught.value).startswith(f"{path.name} illeggibile: ")
