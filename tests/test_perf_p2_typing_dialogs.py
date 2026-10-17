"""@brief Typing in dialog fields does no work out of proportion to the keystroke.

Two fields validated or filtered at every key:

- the model picker's search rebuilt its whole table (five cells per model,
  several hundred models) at every key, also when the visible models were
  the same; now it rebuilds only when what the rows show changes;
- the project name dialog listed the library root and tested every entry
  for a folder at every key; now it reads the root again only when the
  root folder changed, and always on OK.

Contracts by call counts, each with its negative twin (a real change IS
shown), never by wall-clock time.
"""

import os

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtTest import QTest

from kicadgen.gui import model_dialog, project_dialogs
from kicadgen.gui.model_dialog import ModelPickerDialog
from kicadgen.gui.project_dialogs import ProjectNameDialog
from kicadgen.services.openrouter import ModelInfo


def _models(count=30):
    """Synthetic catalogue: vendors a/b, every model able to read images and files."""
    return [ModelInfo(f"{'ab'[i % 2]}vendor/model-{i}", f"Model {i}", 0.1 * i, 0.3 * i, 128000,
                      ("text", "image", "file"), True) for i in range(count)]


@pytest.fixture
def picker(qapp, monkeypatch):
    """A model picker fed offline with the synthetic catalogue."""
    monkeypatch.setattr(ModelPickerDialog, "_load_models", lambda self, force_refresh=False: None)
    monkeypatch.setattr(model_dialog, "list_models", lambda *args, **kwargs: pytest.fail(
        "a unit test must not call OpenRouter"))
    dialog = ModelPickerDialog(None, None)
    dialog._on_models_loaded(_models())
    yield dialog
    dialog.close()


def _count_cells(monkeypatch):
    """Count the price cells built (two per shown model at every rebuild)."""
    calls = []
    original = model_dialog._price_text

    def counting(value, i18n=None):
        calls.append(value)
        return original(value, i18n)
    monkeypatch.setattr(model_dialog, "_price_text", counting)
    return calls


def _shown_ids(dialog):
    return sorted(dialog.table.item(row, model_dialog._COL_ID).text()
                  for row in range(dialog.table.rowCount()))


def test_a_key_that_keeps_the_same_models_rebuilds_no_row(picker, monkeypatch):
    """@brief Positive: «v», «ve», «ven»… match every model: the rows are not rebuilt."""
    built = _count_cells(monkeypatch)
    for text in ("v", "ve", "ven", "vend", "vendo", "vendor"):
        picker.field_search.setText(text)
    assert built == []
    assert len(_shown_ids(picker)) == 30


def test_a_key_that_changes_the_models_rebuilds_them(picker, monkeypatch):
    """@brief Negative: «a» narrows the list: the rows are rebuilt and show exactly the matches."""
    built = _count_cells(monkeypatch)
    picker.field_search.setText("avendor")
    expected = sorted(m.id for m in _models() if m.id.startswith("avendor"))
    assert _shown_ids(picker) == expected
    assert len(built) == 2 * len(expected)
    picker.field_search.setText("")
    assert len(_shown_ids(picker)) == 30, "clearing the filter shows every model again"


def test_a_skipped_rebuild_still_sorts_by_input_price(picker):
    """@brief Same output as before: every filter pass leaves the input-price order."""
    picker.table.sortItems(model_dialog._COL_ID, model_dialog.Qt.SortOrder.DescendingOrder)
    picker.field_search.setText("v")
    prices = [picker.table.item(row, model_dialog._COL_IN)._value
              for row in range(picker.table.rowCount())]
    assert prices == sorted(prices)


def test_a_new_catalogue_is_always_shown(picker, monkeypatch):
    """@brief Negative: new models with the same ids (e.g. new prices) are rebuilt, not skipped."""
    repriced = [ModelInfo(m.id, m.name, m.prompt_usd_per_mtok + 1, m.completion_usd_per_mtok,
                          m.context_length, m.input_modalities, m.supports_tools) for m in _models()]
    built = _count_cells(monkeypatch)
    picker._on_models_loaded(repriced)
    assert len(built) == 60


# ── Project name dialog ───────────────────────────────────────────────────────

def _count_listings(monkeypatch):
    calls = []
    original = project_dialogs.taken_project_names

    def counting(root):
        calls.append(root)
        return original(root)
    monkeypatch.setattr(project_dialogs, "taken_project_names", counting)
    return calls


def test_typing_a_project_name_lists_the_library_once(qapp, tmp_path, monkeypatch):
    """@brief Positive: ten keys, one listing of the library root (at opening)."""
    for index in range(5):
        (tmp_path / f"Project{index}").mkdir()
    listings = _count_listings(monkeypatch)
    dialog = ProjectNameDialog(tmp_path, "Nuovo progetto")
    try:
        for char in "NewProject":
            QTest.keyClick(dialog.field, char)
        assert dialog.field.text() == "NewProject"
        assert len(listings) == 1
        assert dialog.problem.text() == ""
    finally:
        dialog.close()


def test_a_folder_created_while_typing_is_seen_at_the_next_key(qapp, tmp_path, monkeypatch):
    """@brief Negative: a change of the root folder is read again, and the name is refused."""
    dialog = ProjectNameDialog(tmp_path, "Nuovo progetto")
    try:
        QTest.keyClicks(dialog.field, "Tak")
        assert dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()
        (tmp_path / "Taken").mkdir()
        # The stamp of the root moves on creating a folder; set it apart
        # explicitly so a coarse file-system clock cannot hide the change.
        stamp = os.stat(tmp_path).st_mtime_ns
        os.utime(tmp_path, ns=(stamp + 10_000_000_000, stamp + 10_000_000_000))
        QTest.keyClicks(dialog.field, "en")
        assert dialog.field.text() == "Taken"
        assert not dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()
        assert "Esiste" in dialog.problem.text()
    finally:
        dialog.close()


def test_ok_reads_the_library_again_even_with_an_unchanged_stamp(qapp, tmp_path):
    """@brief Negative: the final check of OK never trusts the snapshot."""
    dialog = ProjectNameDialog(tmp_path, "Nuovo progetto")
    try:
        QTest.keyClicks(dialog.field, "Taken")
        stamp = os.stat(tmp_path).st_mtime_ns
        (tmp_path / "Taken").mkdir()
        os.utime(tmp_path, ns=(stamp, stamp))
        dialog.accept()
        assert dialog.result() == 0, "an existing project's name must not be accepted"
        assert "Esiste" in dialog.problem.text()
    finally:
        dialog.close()
