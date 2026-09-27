"""@brief Photo detection must behave the same on a board that is not the test sample.

Each group pairs a positive and a negative case that differ for a structural
reason (measured or not, low confidence kept or not, tall or flat body...),
never for a number tuned on the sample board:

- oblique bodies keep the measured sides and angle up to the component dicts;
- the detail grid is sized in millimetres on BOTH axes;
- low-confidence detector proposals are visible but opt-in, never paid for
  by default;
- photo prompts carry no example taken from the sample board;
- heights are estimated from family and plan, and always marked as estimates.
"""
import json
import math
import os

import numpy as np
import pytest

from kicadgen.services import photo_global_inventory as inventory
from kicadgen.services import photo_grounding as grounding
from kicadgen.services import photo_yolo
from kicadgen.services.photo_heights import (CAN_MIN_DIAMETER_MM, PLAN_SOURCE,
                                              FAMILY_SOURCE, estimate_body_height)
from kicadgen.services.photo_inventory import _normalized_box

#: A board that is NOT the sample (63.3 x 25.4 mm): 40 x 20 mm.
LENGTH_MM, WIDTH_MM = 40.0, 20.0


def _measured(identity, x_mm, y_mm, length, width, rotation, **extra):
    """A detector proposal whose edges were measured, as measure_proposal_edges leaves it."""
    row = dict(_id=identity, name=f'YOLO candidate {identity}', kind='generic',
               x_mm=x_mm, y_mm=y_mm, length_mm=length, width_mm=width,
               rotation_deg=rotation, height_mm=1.0, color_hex='#303030',
               _source='YOLO local', _yolo_class='ic', _yolo_confidence=.8,
               _yolo_review_only=False, _edges_measured=True)
    row['_box'] = list(_normalized_box(row, LENGTH_MM, WIDTH_MM))
    row.update(extra)
    return row


def _row(identity, sources, bbox, **changes):
    """A complete global-inventory row (every required field present)."""
    row = dict(component_id=identity, grouping_basis='single_body',
               source_candidate_ids=sources, **{'class': 'ic'}, bbox=list(bbox),
               bbox_basis='body', rotation_deg=45, package='', marking='?',
               terminal_count=None, terminal_layout='unresolved', lead_count=None,
               lead_count_complete=False, lead_count_confidence='low', height_mm=None,
               height_source='unknown', confidence='medium', evidence='square leadless body',
               presence_confidence='high', class_confidence='medium', asymmetry_landmark=None)
    row.update(changes)
    return row


def _global(components, rows, photo=None):
    """Run the global inventory with one fake paid call; return (result, calls, logs)."""
    calls, logs = [], []
    decisions = [dict(candidate_id=s, status='keep', component_id=r['component_id'],
                      reason='same body') for r in rows for s in r['source_candidate_ids']]

    def fake(**request):
        calls.append(request)
        payload = json.loads(request['content_blocks'][4]['text'])
        sent = {c['id'] for c in payload['candidates']}
        return dict(components=[r for r in rows if set(r['source_candidate_ids']) <= sent],
                    candidate_decisions=[d for d in decisions if d['candidate_id'] in sent],
                    uncertainties=[])
    photo = np.zeros((200, 400, 3), np.uint8) if photo is None else photo
    result = inventory.review_global_inventory(photo, components, LENGTH_MM, WIDTH_MM,
                                               'offline/model', fake, logs.append)
    return result, calls, logs


# ── 6c: oblique angles survive up to the component dicts ──────────────────


def test_an_oblique_measured_body_keeps_its_true_size_and_angle():
    """@brief Positivo: un QFN 5x5 a 45 gradi resta 5x5 a 45 gradi, non 7.1x7.1."""
    qfn = _measured(1, 4.0, -2.0, 5.0, 5.0, 45.0)
    [body], calls, _ = _global([qfn], [_row('U1', [1], qfn['_box'])])
    assert body['length_mm'] == pytest.approx(5.0) and body['width_mm'] == pytest.approx(5.0)
    assert body['rotation_deg'] == pytest.approx(45.0)
    assert (body['x_mm'], body['y_mm']) == pytest.approx((4.0, -2.0))
    assert body['_edges_measured'] is True and body['_orientation_verified'] is False
    # The measurement travels to the model as evidence, with its angle.
    evidence = json.loads(calls[0]['content_blocks'][4]['text'])['candidates'][0]
    assert evidence['measured_body_mm']['rotation_deg'] == pytest.approx(45.0)
    assert evidence['measured_body_mm']['length'] == pytest.approx(5.0)


def test_an_unmeasured_body_keeps_the_axis_aligned_envelope():
    """@brief Negativo: senza misura dei bordi resta l'ingombro allineato del modello."""
    qfn = _measured(1, 4.0, -2.0, 5.0, 5.0, 45.0)
    qfn.pop('_edges_measured')
    [body], calls, _ = _global([qfn], [_row('U1', [1], qfn['_box'])])
    diagonal = 5.0 * math.sqrt(2)
    assert body['length_mm'] == pytest.approx(diagonal, rel=1e-6)
    assert body['rotation_deg'] in (0, 90) and body['_reported_rotation_deg'] == 45
    assert 'measured_body_mm' not in json.loads(calls[0]['content_blocks'][4]['text'])['candidates'][0]


def test_a_box_the_model_redrew_is_not_overridden_by_the_measurement():
    """@brief Negativo: se il modello ridisegna l'ingombro (altro corpo), vale il modello."""
    qfn = _measured(1, 4.0, -2.0, 5.0, 5.0, 45.0)
    x0, y0, x1, y1 = qfn['_box']
    grown = [max(0, x0 - .2), max(0, y0 - .3), min(1, x1 + .2), min(1, y1 + .3)]
    [body], _, _ = _global([qfn], [_row('U1', [1], grown)])
    assert body['rotation_deg'] in (0, 90) and '_edges_measured' not in body
    assert body['_box'] == grown


def test_an_assembly_of_several_proposals_keeps_the_model_envelope():
    """@brief Negativo: una fusione di piu' proposte non e' il corpo misurato."""
    first = _measured(1, -5.0, 0.0, 4.0, 4.0, 30.0)
    second = _measured(2, -1.0, 0.0, 4.0, 4.0, 30.0)
    bbox = [min(first['_box'][0], second['_box'][0]), min(first['_box'][1], second['_box'][1]),
            max(first['_box'][2], second['_box'][2]), max(first['_box'][3], second['_box'][3])]
    [body], _, _ = _global([first, second],
                           [_row('M1', [1, 2], bbox, grouping_basis='assembly', **{'class': 'module'})])
    assert body['rotation_deg'] in (0, 90) and '_edges_measured' not in body


# ── 7: detail grid on both axes, sized in millimetres ────────────────────


@pytest.mark.parametrize('length,width,count,grid', [
    (63.3, 25.4, 3, (3, 1)),   # same count as the old three fixed strips
    (20.0, 50.0, 2, (1, 2)),   # tall board: split along Y, not in vertical strips
    (100.0, 80.0, 18, (6, 3)),
    (30.0, 30.0, 2, (1, 2)),
])
def test_the_detail_grid_follows_the_board_size_on_both_axes(length, width, count, grid):
    """@brief Numero di dettagli (= chiamate di localizzazione) per scheda, dichiarato."""
    tiles = grounding.detail_grid(length, width)
    assert len(tiles) == count
    columns = {round(cell[0], 6) for _, cell in tiles}
    rows = {round(cell[1], 6) for _, cell in tiles}
    assert (len(columns), len(rows)) == grid
    target = grounding.DETAIL_TARGET_PX / grounding.SMALLEST_PACKAGE_SHORT_SIDE_MM
    for region, cell in tiles:
        span = ((region[2] - region[0]) * length, (region[3] - region[1]) * width)
        assert grounding.model_px_per_mm(*span) >= target
        assert region[0] <= cell[0] < cell[2] <= region[2]
        assert region[1] <= cell[1] < cell[3] <= region[3]
    # Ownership cells tile the board exactly once.
    area = sum((c[2] - c[0]) * (c[3] - c[1]) for _, c in tiles)
    assert area == pytest.approx(1.0)


def test_a_photo_coarser_than_the_target_gets_no_useless_zoom():
    """@brief Negativo: un ritaglio non aggiunge pixel che la foto non ha."""
    assert grounding.detail_grid(100.0, 80.0, (1000, 800)) == []
    assert len(grounding.detail_grid(100.0, 80.0, (4000, 3200))) == 18


def test_a_small_board_needs_no_detail_at_all():
    """@brief Negativo: se la vista completa raggiunge l'obiettivo, niente chiamate in piu'."""
    assert grounding.detail_grid(15.0, 10.0) == []


def test_grounding_regions_cut_the_photo_on_both_axes():
    """@brief I ritagli corrispondono ai pixel dichiarati anche lungo Y."""
    # 45 px/mm: finer than the detail target, so the grid is not capped.
    height, width = 2250, 900
    rows, cols = np.indices((height, width))
    photo = np.stack([rows % 251, cols % 241, (rows + cols) % 239], axis=2).astype(np.uint8)
    regions = list(grounding.grounding_regions(photo, 20.0, 50.0))
    assert len(regions) == 3 and regions[0][2] == (0., 0., 1., 1.)
    tops = set()
    for _, region, ownership, crop in regions[1:]:
        left, top = round(region[0] * width), round(region[1] * height)
        right, bottom = round(region[2] * width), round(region[3] * height)
        assert np.array_equal(crop, photo[top:bottom, left:right])
        tops.add(ownership[1])
    assert len(tops) == 2, 'a tall board is split along Y'


@pytest.mark.parametrize('box,region,kept', [
    ([0, 100, 400, 300], (0., .4, 1., 1.), False),    # cut at the TOP of an inner detail
    ([600, 100, 1000, 300], (0., 0., 1., .6), False),  # cut at the BOTTOM of an inner detail
    ([0, 100, 400, 300], (0., 0., 1., .6), True),      # top of the detail is the board edge
    ([200, 100, 400, 300], (0., .4, 1., 1.), True),    # well inside the detail
])
def test_a_body_cut_by_a_detail_edge_is_rejected_on_y_too(box, region, kept):
    """@brief Il taglio al bordo di un dettaglio vale su Y come su X; il bordo scheda no."""
    data = {'components': [{'box_2d': box, 'label': 'body', 'kind': 'generic'}]}
    decoded = grounding.decode_boxes(data, region, LENGTH_MM, WIDTH_MM, ('generic',),
                                     'test', lambda _m: None)
    assert bool(decoded) is kept


def test_the_global_request_carries_the_mm_detail_tiles():
    """@brief La richiesta globale porta la stessa griglia (immagini, non chiamate)."""
    photo = np.zeros((1300, 2600, 3), np.uint8)   # ~41 px/mm on a 63.3 x 25.4 board
    calls = []

    def fake(**request):
        calls.append(request)
        return dict(components=[], candidate_decisions=[], uncertainties=['none'])
    with pytest.raises(ValueError):
        inventory.review_global_inventory(photo, [], 63.3, 25.4, 'offline/model', fake,
                                          lambda _m: None)
    texts = [b['text'] for b in calls[0]['content_blocks'] if b['type'] == 'text']
    tiles = [t for t in texts if t.startswith('Context tile ')]
    assert len(tiles) == 3 == len(grounding.detail_grid(63.3, 25.4, (2600, 1300)))
    assert sum(b['type'] == 'image_url' for b in calls[0]['content_blocks']) == 1 + 3


# ── 5b: low-confidence proposals are visible but opt-in ──────────────────


def _low(identity, box, **extra):
    x0, y0, x1, y1 = box
    row = dict(_id=identity, _box=list(box), name=f'YOLO candidate {identity}', kind='generic',
               x_mm=((x0 + x1) / 2 - .5) * LENGTH_MM, y_mm=((y0 + y1) / 2 - .5) * WIDTH_MM,
               length_mm=(x1 - x0) * LENGTH_MM, width_mm=(y1 - y0) * WIDTH_MM,
               rotation_deg=0, height_mm=1.0, _source='YOLO local', _yolo_class='capacitor',
               _yolo_confidence=.12, _yolo_review_only=True,
               _yolo_origin='low_confidence_review', _yolo_low_confidence=True)
    row.update(extra)
    return row


def test_a_low_confidence_proposal_is_not_sent_to_the_paid_model_by_default():
    """@brief Positivo: la debole non tenuta resta fuori dalla richiesta e dall'inventario."""
    strong = _measured(1, -10.0, 0.0, 2.0, 1.0, 0.0)
    strong.pop('_edges_measured')
    weak = _low(2, (.7, .7, .75, .75))
    result, calls, logs = _global([strong, weak], [_row('C1', [1], strong['_box'], rotation_deg=0)])
    sent = [c['id'] for c in json.loads(calls[0]['content_blocks'][4]['text'])['candidates']]
    assert sent == [1]
    assert all(2 not in (c.get('source_candidate_ids') or []) and c.get('_id') != 2 for c in result)
    assert any('bassa confidenza' in line for line in logs)


def test_a_low_confidence_proposal_the_user_kept_is_sent():
    """@brief Negativo: tenuta dall'utente, la stessa proposta va al modello."""
    strong = _measured(1, -10.0, 0.0, 2.0, 1.0, 0.0)
    strong.pop('_edges_measured')
    weak = _low(2, (.7, .7, .75, .75), _yolo_user_kept=True, _yolo_review_only=False)
    _, calls, _ = _global([strong, weak], [_row('C1', [1], strong['_box'], rotation_deg=0)])
    sent = [c['id'] for c in json.loads(calls[0]['content_blocks'][4]['text'])['candidates']]
    assert sent == [1, 2]


def test_a_seam_rebuilt_weak_body_is_not_opt_in():
    """@brief Negativo: una ricomposta dalle cuciture non e' marcata e va al modello."""
    rebuilt = _low(3, (.1, .1, .3, .3), _yolo_low_confidence=False, _yolo_clipped_recovery=True)
    assert not photo_yolo.awaiting_user_keep(rebuilt)
    assert photo_yolo.split_ai_candidates([rebuilt]) == ([rebuilt], [])


# ── 1: no sample-specific examples in the photo prompts ──────────────────

#: Tokens that came from the sample board (ESP32-S3 module with RF shield,
#: coaxial antenna socket, USB-C, RS485 transceiver, SOT-223 regulator) or
#: from its numbers. None of them may appear in a photo prompt or schema.
SAMPLE_TOKENS = ('U.FL', 'IPEX', 'USB_C', 'USB-C', 'ESP32', 'WROOM', 'antenna',
                 'coax socket', 'RF module', 'SP3485', 'VPT', 'AMS1117', 'SOT-223',
                 '63.3', '25.4', '30-50')


def _photo_prompt_texts():
    """Every prompt and tool schema the photo detection modules send."""
    kinds = ('generic', 'resistor', 'capacitor', 'ic', 'connector')
    captured = []

    def capture(**request):
        captured.append(request)
        return {'components': [{'id': 1, 'present': True, 'confidence': 'low', 'package': ''}]}
    component = dict(_id=1, _box=(.2, .2, .4, .4), kind='generic', name='x', x_mm=0, y_mm=0,
                     length_mm=2, width_mm=1, rotation_deg=0)
    grounding.mechanical_envelopes(np.zeros((50, 100, 3), np.uint8), [component], 'm', capture,
                                   lambda _m: None, None)
    return [
        grounding.GROUNDING_PROMPT, grounding.MECHANICAL_EVIDENCE_PROMPT,
        grounding.VERIFICATION_PROMPT, json.dumps(grounding.grounding_tool(kinds)),
        json.dumps(grounding.classification_tool(kinds)), captured[0]['system_prompt'],
        json.dumps(captured[0]['tool']), inventory.PROMPT, inventory.PROMPT_ENUMERATE,
        inventory.PROMPT_ENUMERATE_AFTER_EMPTY_DETECTOR, json.dumps(inventory.inventory_tool()),
    ]


def test_photo_prompts_carry_no_example_taken_from_the_sample_board():
    """@brief Positivo: nessun prompt o schema fotografico nomina il campione."""
    for text in _photo_prompt_texts():
        found = [token for token in SAMPLE_TOKENS if token.casefold() in text.casefold()]
        assert not found, f'sample-specific tokens {found} in: {text[:120]}...'


def test_the_token_scan_would_catch_a_sample_example():
    """@brief Negativo: lo stesso controllo trova un esempio del campione se c'e'."""
    text = 'Sigla standard (SOT-223, U.FL, ESP32-S3-WROOM-1)'
    assert [t for t in SAMPLE_TOKENS if t.casefold() in text.casefold()] == \
        ['U.FL', 'ESP32', 'WROOM', 'SOT-223']


# ── 13: heights estimated from family and plan, always marked ────────────


def test_a_can_sized_capacitor_is_not_a_flat_disc():
    """@brief Positivo: un condensatore a barattolo da 10 mm e' alto ~10 mm, non 0.8."""
    assert estimate_body_height('capacitor', 10.0, 10.0) == (10.0, PLAN_SOURCE)
    assert estimate_body_height('capacitor_electrolytic', 6.3, 6.6) == (6.3, PLAN_SOURCE)


def test_a_chip_capacitor_keeps_the_flat_family_height():
    """@brief Negativo: un chip 1206 (rettangolare, lato corto < 4 mm) resta piatto."""
    assert estimate_body_height('capacitor', 3.2, 1.6) == (.8, FAMILY_SOURCE)
    # A square plan narrower than the smallest can is still a chip.
    small = CAN_MIN_DIAMETER_MM - .5
    assert estimate_body_height('capacitor', small, small)[1] == FAMILY_SOURCE
    assert estimate_body_height('generic', 10, 10) is None


def test_the_mechanical_path_follows_the_voted_family_and_plan():
    """@brief Il voto «capacitor» su una pianta a barattolo da' un'altezza da barattolo."""
    component = dict(_id=1, _box=(.1, .1, .35, .6), kind='generic', name='x', x_mm=0, y_mm=0,
                     length_mm=10.0, width_mm=10.0, rotation_deg=0)

    def vote(**_request):
        return {'components': [{'id': 1, 'present': True, 'confidence': 'high',
                                'package': '', 'kind': 'capacitor'}]}
    [can] = grounding.mechanical_envelopes(np.zeros((50, 100, 3), np.uint8), [component], 'm',
                                           vote, lambda _m: None, None)
    assert can['height_mm'] == 10.0 and can['_height_estimated'] is True
    assert can['_height_source'].endswith('_not_measured')
    chip = dict(component, length_mm=1.6, width_mm=.8)
    [flat] = grounding.mechanical_envelopes(np.zeros((50, 100, 3), np.uint8), [chip], 'm',
                                            vote, lambda _m: None, None)
    assert flat['height_mm'] == .8 and flat['_height_estimated'] is True


def test_the_global_inventory_estimates_a_missing_height_from_the_plan():
    """@brief Senza altezza dal modello: stima di famiglia e pianta, marcata stimata."""
    can = _measured(1, 0.0, 0.0, 8.0, 8.0, 0.0, _yolo_class='capacitor')
    can.pop('_edges_measured')
    [body], _, _ = _global([can], [_row('C1', [1], can['_box'], rotation_deg=0,
                                        **{'class': 'capacitor'})])
    assert body['height_mm'] == pytest.approx(8.0)
    assert body['_height_estimated'] is True and body['_height_source'].endswith('_not_measured')
    [given], _, _ = _global([can], [_row('C1', [1], can['_box'], rotation_deg=0,
                                         height_mm=10.2, height_source='visual_estimate',
                                         **{'class': 'capacitor'})])
    assert given['height_mm'] == 10.2 and given['_height_estimated'] is True


# ── minor: hand-drawn boxes have a floor in millimetres, not a photo fraction ─


def test_a_0402_short_side_can_be_drawn_on_a_large_board(qapp):
    """@brief Positivo e negativo: il minimo e' in mm sulla scheda, qualunque sia la scheda."""
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PyQt6')
    from kicadgen.gui.yolo_review_dialog import MIN_NEW_BOX_MM, YoloReviewDialog
    dialog = YoloReviewDialog(b'', [], 300.0, 200.0, photo_yolo.detector_classes())
    try:
        view = dialog.view
        # 0.5 mm on a 300 mm board is 0.0017 of the photo: the old fixed
        # fraction (0.004) refused it.
        assert view.large_enough((.5, .5, .5 + .5 / 300, .5 + 1.0 / 200))
        assert not view.large_enough((.5, .5, .5 + (MIN_NEW_BOX_MM / 2) / 300, .6))
    finally:
        dialog.close()


def test_every_component_kind_has_a_declared_height_rule_except_generic():
    """@brief Ogni tipo del vocabolario ha una stima di altezza; solo generic resta segnaposto."""
    from kicadgen.services.datasheet_extractor import COMPONENT_KINDS
    missing = [kind for kind in COMPONENT_KINDS
               if kind != 'generic' and estimate_body_height(kind, 6.0, 5.0) is None]
    assert not missing, f'kinds without a height rule: {missing}'
    assert estimate_body_height('generic', 6.0, 5.0) is None


def test_a_battery_height_follows_the_plan_shape_not_its_size():
    """@brief Positivo e negativo: cilindro sdraiato alto quanto largo, bottone basso."""
    assert estimate_body_height('battery', 77.0, 21.0) == (21.0, PLAN_SOURCE)
    coin, _ = estimate_body_height('battery', 22.0, 21.0)
    assert coin == pytest.approx(21.0 * .3)


# ── 5b in the review window: shown distinctly, not kept until the user keeps it ─


@pytest.fixture
def low_dialog(qapp):
    """A review window with one strong proposal and one low-confidence proposal."""
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    pytest.importorskip('PyQt6')
    from kicadgen.gui.yolo_review_dialog import YoloReviewDialog
    app = qapp
    strong = _low(1, (.1, .1, .2, .2), _yolo_confidence=.8, _yolo_review_only=False,
                  _yolo_origin='operating_threshold', _yolo_low_confidence=False)
    weak = _low(2, (.6, .6, .7, .7))
    dialog = YoloReviewDialog(b'', [strong, weak], LENGTH_MM, WIDTH_MM,
                              photo_yolo.detector_classes())
    yield app, dialog
    dialog.close()


def test_a_low_confidence_proposal_is_shown_as_such_and_not_kept(low_dialog):
    """@brief Positivo: la debole si vede, puntinata ed etichettata, ma non e' tenuta."""
    from PyQt6.QtCore import Qt
    from kicadgen.gui.yolo_review_dialog import LOW_CONFIDENCE_COLOR, box_pen
    _app, dialog = low_dialog
    assert dialog.list.count() == 2
    assert 'bassa confidenza' in dialog.list.item(1).text()
    assert 'non tenuta' in dialog.list.item(1).text()
    pen = box_pen(dialog.proposals[1])
    assert pen.style() == Qt.PenStyle.DotLine
    assert pen.color().name() == LOW_CONFIDENCE_COLOR
    assert [p['_id'] for p in dialog.kept()] == [1]


def test_a_low_confidence_proposal_the_user_keeps_is_returned(low_dialog):
    """@brief Negativo: tenuta (T) torna nella revisione, non piu' da rivedere; lasciata sparisce."""
    _app, dialog = low_dialog
    dialog._select_row(1)
    assert dialog.btn_keep.isEnabled()
    assert dialog.toggle_keep() is True
    kept = dialog.kept()
    assert [p['_id'] for p in kept] == [1, 2]
    assert kept[1]['_yolo_user_kept'] is True and kept[1]['_yolo_review_only'] is False
    assert 'tenuta' in dialog.list.item(1).text() and 'non tenuta' not in dialog.list.item(1).text()
    assert dialog.toggle_keep() is False
    assert [p['_id'] for p in dialog.kept()] == [1]
    dialog._select_row(0)
    assert not dialog.btn_keep.isEnabled() and dialog.toggle_keep() is None


def test_moving_a_low_confidence_proposal_keeps_it(low_dialog):
    """@brief Ritoccarla e' guardarla: la mano di chi rivede e' la decisione di presenza."""
    from kicadgen.gui.yolo_review_dialog import YoloReviewDialog
    _app, dialog = low_dialog
    YoloReviewDialog.mark_manual(dialog.proposals[1])
    assert [p['_id'] for p in dialog.kept()] == [1, 2]
