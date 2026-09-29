"""@brief HANDOFF 156: the crop and marking readings are phases of their own on the card.

User, 2026-09-29: «la fase di lettura dei ritagli non la vedo censita qui».
The list showed «Modello AI (remoto, a consumo) — in corso» for the longest
paid stage. Now: «Lettura ritagli (remoto, a consumo)» with a per-body count
on its row, on the bar and in the central status, «Lettura sigle (remoto, a
consumo)» for the marking reading, and the local colour sampling announced
as OpenCV instead of a second «Modello AI» row. The count never becomes the
outcome of the phase: that stays the first status the stage reported.
"""

import numpy as np
import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from kicadgen.gui.analysis_overlay import TOOL_ALIASES, TOOLS, AnalysisOverlay
from kicadgen.services import photo_inventory as inventory
from test_photo_crop_reading import DEEPSEEK, _inventory_response, _obs, _reconcile


@pytest.fixture
def overlay(qapp):
    parent = QWidget()
    parent.resize(1100, 900)
    card = AnalysisOverlay(parent)
    parent.show()
    card.start()
    QApplication.processEvents()
    yield card
    card.stop()
    parent.close()
    parent.deleteLater()


def test_the_two_readings_have_their_own_names():
    assert TOOLS["llm_crops"][0] == "Lettura ritagli (remoto, a consumo)"
    assert TOOLS["llm_markings"][0] == "Lettura sigle (remoto, a consumo)"
    assert "llm_markings" not in TOOL_ALIASES


def test_the_running_row_and_the_status_show_the_count_without_touching_the_outcome(overlay):
    overlay.set_tool("llm_inventory", DEEPSEEK)
    overlay.set_status("Inventario globale: 41 ipotesi; completezza non certificata.")
    overlay.set_tool("llm_crops", DEEPSEEK)
    overlay.set_progress(17, 42, "componenti")
    lines = [line for line in overlay.phases_plain_text().splitlines() if line]
    assert lines[0].startswith("✓ Modello AI (remoto, a consumo) — Inventario globale: 41 ipotesi")
    assert lines[-1] == "● Lettura ritagli (remoto, a consumo) — 17 di 42 componenti"
    assert overlay.status_plain_text() == "Lettura ritagli (remoto, a consumo) — 17 di 42 componenti"
    # The first status the stage reports is its outcome, not the count.
    overlay.set_status("Lettura dei singoli componenti: 42 corpi letti due volte; "
                       "28 con letture concordi, 13 discordi, 1 in minoranza.")
    overlay.set_progress(42, 42, "componenti")
    overlay.set_tool("opencv", "campionamento dei colori dalla foto")
    lines = [line for line in overlay.phases_plain_text().splitlines() if line]
    assert any(line.startswith("✓ Lettura ritagli (remoto, a consumo) — Lettura dei singoli "
                               "componenti: 42 corpi") for line in lines)
    assert lines[-1] == "● OpenCV (analisi immagine locale) — in corso"
    # A new phase starts without the old count.
    overlay.set_tool("llm_markings", DEEPSEEK)
    assert overlay.phases_plain_text().splitlines()[-1] == \
        "● Lettura sigle (remoto, a consumo) — in corso"


def test_a_countless_progress_clears_the_count_of_the_row(overlay):
    overlay.set_tool("llm_crops", DEEPSEEK)
    overlay.set_progress(3, 10, "componenti")
    assert "3 di 10" in overlay.phases_plain_text()
    overlay.set_progress(0, 0)
    assert overlay.phases_plain_text().splitlines()[-1].endswith("— in corso")


def test_the_announcer_forwards_counts_only_to_callbacks_that_take_them():
    two, four = [], []
    announce = inventory._phase_announcer(lambda tool, detail: two.append((tool, detail)), print)
    announce("llm_crops", "m", 3, 10)
    assert two == [("llm_crops", "m")]
    announce = inventory._phase_announcer(
        lambda tool, detail, done, total: four.append((tool, detail, done, total)), print)
    announce("llm_crops", "m", 3, 10)
    announce("opencv", "colori")
    # A callback that takes counts always receives them, (0, 0) when there are none.
    assert four == [("llm_crops", "m", 3, 10), ("opencv", "colori", 0, 0)]
    assert inventory._accepts_counts(None) is False
    assert inventory._accepts_counts(lambda *args: None) is True


def test_the_relaunch_counts_the_bodies_and_the_paid_calls_body_by_body():
    """@brief End to end on the service: one body, two paid readings, then colours."""
    def call(**kwargs):
        name = kwargs['tool']['function']['name']
        return _inventory_response() if name == 'report_global_inventory' else _obs(marking='')
    phases = []

    def phase(tool, detail, done=0, total=0):
        phases.append((tool, detail, done, total))
    from kicadgen.services import photo_inventory as inv
    photo = np.full((100, 200, 3), 50, np.uint8)
    proposals = [dict(_id=1, _box=[.2, .2, .4, .6], name='target', kind='generic', x_mm=-6,
                      y_mm=-2, length_mm=8, width_mm=8, height_mm=1, rotation_deg=0,
                      color_hex='#303030', _source='YOLO local')]
    logs = []
    inv.reconcile_saved_proposals(photo, proposals, 40, 20, DEEPSEEK, call, logs.append,
                                  phase=phase)
    crops = [entry for entry in phases if entry[0] == 'llm_crops']
    assert crops[0] == ('llm_crops', DEEPSEEK, 0, 0)
    assert crops[-1] == ('llm_crops', f'{DEEPSEEK} · 2 richieste', 1, 1)
    tools = [entry[0] for entry in phases]
    assert tools[tools.index('llm_crops') + len(crops)] == 'opencv'
    assert phases[tools.index('opencv')][1] == 'campionamento dei colori dalla foto'
    assert 'llm_inventory' not in tools[tools.index('llm_crops'):]
