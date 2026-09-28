"""@brief The phase list survives a window resize and a language switch (HANDOFF point 138, C2 and C7).

C2: the rows of the completed phases are reserved from the height the window
has left over. That height was measured on the card WITH the list already in
it, so the rows were subtracted twice: any resize in the middle of a run
(`_refit`) shrank the reservation (4 rows -> 1) and the list was never drawn
again for the new room. The contract now:
- a resize that leaves the room unchanged keeps every row;
- a window enlarged after the start shows rows that did not fit at the start;
- a window shrunk still never lets the card overflow it;
- phase changes never make the card jump in height.
C7: phase names, «in corso» and catalogued outcomes are kept as Italian
sources and translated when drawn, so a language switch redraws the list.
"""
import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from kicadgen.gui.analysis_overlay import PHASE_HISTORY_ROWS, AnalysisOverlay
from kicadgen.services.i18n import I18n
from kicadgen.services.analysis_profiles import ProfileStore

_TOOLS = ("llm", "opencv", "yolo", "llm_inventory", "opencv", "yolo")


def _rows(card):
    return [line for line in card.phases_plain_text().splitlines() if line]


def _run_phases(card):
    for tool in _TOOLS:
        card.set_tool(tool)
        card.set_status(f"Fase {tool} conclusa.")
    QApplication.processEvents()


def _card_bottom(card):
    return card.card.mapTo(card, card.card.rect().bottomLeft()).y()


@pytest.fixture
def host(qapp):
    parent = QWidget()
    parent.resize(1100, 900)
    card = AnalysisOverlay(parent)
    parent.show()
    card.start()
    QApplication.processEvents()
    yield parent, card
    card.stop()
    parent.close()
    parent.deleteLater()


def _tightest_height_for_every_row(parent, card):
    """Lowest window height (10 px steps) whose run lists PHASE_HISTORY_ROWS rows."""
    for height in range(460, 1200, 10):
        parent.resize(1100, height)
        card.start()
        QApplication.processEvents()
        _run_phases(card)
        if len(_rows(card)) == PHASE_HISTORY_ROWS:
            return height
    pytest.fail("no window height lists every phase row")


def test_a_resize_that_changes_nothing_keeps_every_row(host):
    """Where the list only just fits, counting its rows twice would drop them."""
    parent, card = host
    height = _tightest_height_for_every_row(parent, card)
    for width in (1080, 1100):                     # same height: the room is the same
        parent.resize(width, height)
        QApplication.processEvents()
        assert len(_rows(card)) == PHASE_HISTORY_ROWS, (width, height)
    card.set_tool("llm")                           # the next phase redraws the list
    card.set_status("Fase successiva.")
    QApplication.processEvents()
    assert len(_rows(card)) == PHASE_HISTORY_ROWS, height
    assert _rows(card)[-1].startswith("● ")
    assert _card_bottom(card) <= parent.height()


def test_a_window_enlarged_mid_run_shows_rows_that_did_not_fit(host):
    parent, card = host
    parent.resize(1100, 480)                      # too low for the list at all
    card.start()
    QApplication.processEvents()
    _run_phases(card)
    few = len(_rows(card))
    assert few < PHASE_HISTORY_ROWS
    parent.resize(1100, 900)
    QApplication.processEvents()
    assert len(_rows(card)) == PHASE_HISTORY_ROWS
    assert _card_bottom(card) <= parent.height()


def test_a_window_shrunk_mid_run_keeps_what_a_fresh_start_would_show(host):
    """The rows after a shrink are those a run started at that size shows, and fit."""
    parent, card = host
    height = _tightest_height_for_every_row(parent, card)
    parent.resize(1100, 1000)
    card.start()
    QApplication.processEvents()
    _run_phases(card)
    for shrunk in (height, height - 40, height - 80):
        parent.resize(1100, shrunk)
        QApplication.processEvents()
        after_resize = (len(_rows(card)), _card_bottom(card))
        card.start()
        QApplication.processEvents()
        _run_phases(card)
        assert after_resize == (len(_rows(card)), _card_bottom(card)), shrunk
        assert after_resize[1] <= parent.height(), shrunk
        parent.resize(1100, 1000)                  # back up: the next shrink is mid-run again
        QApplication.processEvents()
    assert len(_rows(card)) == PHASE_HISTORY_ROWS


def test_after_a_resize_phase_changes_still_do_not_move_the_card(host):
    parent, card = host
    card.set_tool("llm")
    parent.resize(1050, 880)
    QApplication.processEvents()
    before = card.card.height()
    for tool in _TOOLS:
        card.set_tool(tool)
        card.set_status(f"Fase {tool} conclusa con un esito abbastanza lungo da occupare la riga.")
        QApplication.processEvents()
        assert card.card.height() == before, tool


@pytest.fixture
def translated(qapp, tmp_path):
    i18n = I18n(ProfileStore(tmp_path / "profiles.sqlite3"), "it")
    parent = QWidget()
    parent.resize(1100, 900)
    card = AnalysisOverlay(parent, i18n=i18n)
    parent.show()
    card.start()
    QApplication.processEvents()
    yield i18n, card
    card.stop()
    parent.close()
    parent.deleteLater()


def test_a_language_switch_redraws_the_phase_list(translated):
    i18n, card = translated
    card.set_tool("opencv")
    card.set_status("Pronto")                      # a catalogued outcome
    card.set_tool("yolo")
    before = card.phases_plain_text()
    assert "in corso" in before and "Pronto" in before
    i18n.set_language("en")
    card.refresh_command_texts()                   # what the window calls on a switch
    after = card.phases_plain_text()
    assert "in corso" not in after and "in progress" in after
    assert "YOLO (local detection)" in after
    assert "Pronto" not in after and "Ready" in after
    assert _rows(card)[0].startswith("✓ ")
    assert "in progress" not in card.phases.toolTip() and "Ready" in card.phases.toolTip()


def test_text_that_is_not_in_the_catalog_is_left_as_it_came(translated):
    """Negative: an outcome already formatted by its sender is not a catalogue key."""
    i18n, card = translated
    card.set_tool("opencv")
    card.set_status("Scheda trovata: 987 x 426 px.")
    card.set_tool("yolo")
    i18n.set_language("en")
    card.refresh_command_texts()
    # The name follows the new language; the sentence stays as its sender wrote it.
    assert "✓ OpenCV (local image analysis) — Scheda trovata: 987 x 426 px."         in card.phases_plain_text()
