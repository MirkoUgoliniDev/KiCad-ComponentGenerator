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


def test_a_project_file_nested_too_deep_is_refused_not_a_crash(tmp_path):
    """@brief Positive: a nesting deeper than the parser's stack is a ProjectConfigError."""
    depth = 100_000
    _config(tmp_path, '{"format": "kicadgen-project", "version": 1, "state": {"a": '
            + "[" * depth + "]" * depth + "}}")
    with pytest.raises(project_state.ProjectConfigError):
        project_state.read_project_config(tmp_path)


def test_a_state_too_deep_for_the_non_finite_walk_is_refused(tmp_path, monkeypatch):
    """@brief Positive: the RecursionError of the NaN walk becomes a ProjectConfigError."""
    _config(tmp_path, '{"format": "kicadgen-project", "version": 1, "state": {"a": [[1]]}}')

    def too_deep(*_args):
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr(project_state, "_drop_non_finite", too_deep)
    with pytest.raises(project_state.ProjectConfigError):
        project_state.read_project_config(tmp_path)


def test_a_moderately_nested_state_is_still_read(tmp_path):
    """@brief Negative: ordinary nesting is read as before."""
    _config(tmp_path, '{"format": "kicadgen-project", "version": 1, "state": {"a": '
            + "[" * 50 + "]" * 50 + "}}")
    state = project_state.read_project_config(tmp_path)
    assert isinstance(state["a"], list)
