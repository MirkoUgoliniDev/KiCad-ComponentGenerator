"""@brief Righe AI contraddette dal rilevatore locale (HANDOFF 126, regole G1, G3, G6).

Sintomi reali (RS485, DeepSeek): un «assieme» di sei corpi disgiunti che
copriva per intero altri sette corpi rilevati; un diodo fuso con un LED in un
riquadro che copriva altri tre corpi; un LED aggiunto dal modello dentro il
riquadro del diodo; otto decisioni su ID inventati che marcavano «parziale»
ogni corpo. Ogni regola ha un caso che deve scattare e uno, strutturalmente
diverso, che non deve.
"""
from copy import deepcopy

from test_photo_global_inventory import body, candidate, run


def box(identity, bbox):
    item = candidate(identity)
    item['_box'] = list(bbox)
    return item


def decision(identity, parent, status='merge'):
    return dict(candidate_id=identity, status=status, component_id=parent,
                reason='Visible body')


def answer(rows, decisions):
    return dict(components=rows, candidate_decisions=decisions,
                uncertainties=['Height cannot be measured from top'])


# Tre corpi rilevati: due agli angoli di un riquadro, uno nel mezzo.
ITEMS = [box(1, [.1, .1, .2, .2]), box(2, [.5, .5, .6, .6]), box(3, [.3, .3, .4, .4])]
GROUP = [.05, .05, .65, .65]


# ── G1: esclusivita' dell'involucro di un gruppo ────────────────────────


def test_a_group_that_swallows_an_unclaimed_detected_body_is_not_applied(tmp_path):
    data = answer([body('group', [1, 2], bbox=GROUP), body('r3', [3], bbox=[.3, .3, .4, .4])],
                  [decision(1, 'group'), decision(2, 'group'), decision(3, 'r3', 'keep')])
    result, _, logs = run(data, deepcopy(ITEMS), cache=tmp_path)
    assert not any(r.get('component_id') == 'group' for r in result)
    alone = {r['_id']: r for r in result if 'component_id' not in r}
    assert set(alone) == {1, 2}
    assert all('[3]' in alone[i]['_review_reason'] for i in (1, 2))
    assert any(r.get('component_id') == 'r3' for r in result)
    assert any('contraddetto dal rilevatore' in line for line in logs)
    # Esito del controllo, non errore della risposta: la cache resta valida.
    assert list(tmp_path.glob('global-inventory-*.json'))


def test_a_group_that_accounts_for_everything_it_covers_is_kept():
    """Negativo: stesso riquadro, ma il gruppo dichiara anche il corpo centrale
    (un modulo che conta cio' che porta)."""
    data = answer([body('group', [1, 2, 3], bbox=GROUP)],
                  [decision(1, 'group'), decision(2, 'group'), decision(3, 'group')])
    result, _, _ = run(data, deepcopy(ITEMS))
    assert [r.get('component_id') for r in result] == ['group']


def test_a_neighbour_the_outline_only_grazes_does_not_break_the_group():
    """Negativo: il terzo corpo sfiora il riquadro (meno di meta' dentro)."""
    items = deepcopy(ITEMS)
    items[2]['_box'] = [.62, .3, .72, .4]
    data = answer([body('group', [1, 2], bbox=GROUP), body('r3', [3], bbox=[.62, .3, .72, .4])],
                  [decision(1, 'group'), decision(2, 'group'), decision(3, 'r3', 'keep')])
    result, _, _ = run(data, items)
    assert {r.get('component_id') for r in result} == {'group', 'r3'}


def test_a_body_the_model_rejected_is_not_counted_as_swallowed():
    """Negativo: una scritta di serigrafia rifiutata dentro un modulo non e' un corpo."""
    data = answer([body('group', [1, 2], bbox=GROUP)],
                  [decision(1, 'group'), decision(2, 'group'),
                   dict(candidate_id=3, status='reject', component_id=None,
                        reason='Printed logo', rejection_category='silkscreen_or_marking')])
    result, _, _ = run(data, deepcopy(ITEMS))
    assert [r.get('component_id') for r in result] == ['group']


# ── G2: niente doppioni fra riga e proposta ripristinata ────────────────


def test_a_conflicted_source_is_not_emitted_again_beside_the_row_that_claims_it():
    items = [box(1, [.2, .2, .4, .4]), box(2, [.35, .2, .6, .4])]
    data = answer([body('pair', [1, 2], bbox=[.2, .2, .6, .4])],
                  [decision(1, 'pair'),
                   dict(candidate_id=2, status='reject', component_id=None, reason='Label',
                        rejection_category='silkscreen_or_marking')])
    result, _, _ = run(data, items)
    assert [r.get('component_id') for r in result] == ['pair']
    assert result[0]['_global_inventory_status'] == 'partial'


def test_a_conflicted_proposal_nobody_claims_is_still_restored():
    """Negativo: nessuna riga la reclama -> torna da sola, come prima."""
    items = [box(1, [.2, .2, .4, .4]), box(2, [.6, .6, .7, .7])]
    data = answer([body('one', [1], bbox=[.2, .2, .4, .4])],
                  [decision(1, 'one', 'keep'), decision(2, 'absent')])
    result, _, _ = run(data, items)
    assert any(r['_id'] == 2 and 'component_id' not in r for r in result)


# ── G3: corpo solo-AI annidato in un corpo rilevato ─────────────────────


def test_an_ai_only_body_inside_a_detected_body_becomes_a_doubt():
    """Probabile doppione (il LED dentro il diodo della corsa DeepSeek), ma puo'
    anche essere un corpo mancato dal rilevatore sopra un modulo: resta, come
    dubbio, e non conta come corpo sicuro."""
    items = [box(1, [.6, .1, .9, .4])]
    data = answer([body('D1', [1], bbox=[.6, .1, .9, .4]),
                   body('LD1', [], bbox=[.7, .15, .75, .35], **{'class': 'led'})],
                  [decision(1, 'D1', 'keep')])
    result, _, logs = run(data, items)
    rows = {r['component_id']: r for r in result}
    assert set(rows) == {'D1', 'LD1'}
    assert rows['LD1']['presence_confidence'] == 'low' and rows['LD1']['_uncertain']
    assert 'D1' in rows['LD1']['_review_reason']
    assert rows['D1']['presence_confidence'] != 'low'
    assert any('LD1' in line for line in logs)


def test_an_ai_only_body_that_contains_a_detected_body_is_kept():
    """Negativo: un dissipatore (o schermo) sopra un corpo rilevato: impilamento,
    non doppione; il verso opposto non si tocca."""
    items = [box(1, [.4, .4, .5, .5])]
    data = answer([body('U1', [1], bbox=[.4, .4, .5, .5]),
                   body('HS1', [], bbox=[.3, .3, .6, .6], **{'class': 'other'})],
                  [decision(1, 'U1', 'keep')])
    result, _, _ = run(data, items)
    assert {r.get('component_id') for r in result} == {'U1', 'HS1'}


def test_an_ai_only_body_beside_detected_bodies_is_kept():
    """Negativo: un corpo che il rilevatore ha mancato, in una zona libera."""
    items = [box(1, [.1, .1, .2, .2])]
    data = answer([body('U1', [1], bbox=[.1, .1, .2, .2]),
                   body('missed', [], bbox=[.6, .6, .8, .8])],
                  [decision(1, 'U1', 'keep')])
    result, _, _ = run(data, items)
    assert {r.get('component_id') for r in result} == {'U1', 'missed'}


def test_nested_rows_are_kept_when_the_model_enumerated_the_bodies_itself():
    """Negativo: senza rilevatore non c'e' nessun corpo rilevato su cui
    appoggiare il giudizio; due righe del modello non si annullano fra loro."""
    data = answer([body('M1', [], bbox=[.2, .2, .8, .8]),
                   body('C1', [], bbox=[.3, .3, .4, .4], **{'class': 'capacitor'})], [])
    result, _, _ = run(data, [])
    assert {r.get('component_id') for r in result} == {'M1', 'C1'}


# ── G6: errori su componenti inesistenti ────────────────────────────────


def test_a_decision_on_an_invented_id_and_parent_does_not_mark_every_body_partial():
    data = answer([body('one', [1], bbox=[.2, .2, .4, .4])],
                  [decision(1, 'one', 'keep'), decision(99, 'ghost', 'keep')])
    result, _, logs = run(data, [box(1, [.2, .2, .4, .4])])
    assert result[0]['_global_inventory_status'] == 'validated_hypotheses'
    assert result[0]['_global_inventory_errors'] == []
    assert result[0]['_global_inventory_response_issues']
    assert any('FAILED/PARTIAL' in line for line in logs)


def test_a_decision_on_an_invented_id_naming_a_real_row_still_marks_that_row():
    """Negativo: la decisione nomina una riga che esiste -> l'errore e' suo."""
    data = answer([body('one', [1], bbox=[.2, .2, .4, .4])],
                  [decision(1, 'one', 'keep'), decision(99, 'one', 'keep')])
    result, _, _ = run(data, [box(1, [.2, .2, .4, .4])])
    assert result[0]['_global_inventory_status'] == 'partial'


# ── G4: corpo solo-AI lungo una fila di contatti di bordo ───────────────

from kicadgen.services.contact_zones import contact_positions, rows_over_edge_contacts  # noqa: E402
from kicadgen.services.photo_inventory import flag_ai_bodies_on_edge_contacts  # noqa: E402
import numpy as np  # noqa: E402

RS485 = (42.8, 15.2)
SIDES = dict(layout='castellated', castellated_sides=[
    dict(edge='left', count=3, pitch_mm=5.08, contact_style='half_hole', half_hole_diameter_mm=2.2),
    dict(edge='right', count=4, pitch_mm=2.54, contact_style='half_hole', half_hole_diameter_mm=1.2)])


def ai_body(name, bbox):
    return dict(component_id=name, name=name, source_candidate_ids=[], bbox=list(bbox),
                presence_confidence='high', class_confidence='high')


def test_ai_bodies_along_a_contact_row_become_doubts_at_any_depth():
    """Le due «strisce connettore» della corsa DeepSeek: 4.5 e 3 mm di profondita',
    oltre il rame dei contatti; non si cancellano, diventano dubbi."""
    items = [ai_body('J01', [0.0, 0.0, 0.105, 1.0]), ai_body('J02', [0.93, 0.0, 1.0, 1.0])]
    photo = np.zeros((304, 856, 3), np.uint8)
    assert flag_ai_bodies_on_edge_contacts(photo, items, SIDES, *RS485) == 2
    assert all(i['presence_confidence'] == 'low' and i['class_confidence'] == 'low'
               and i['_uncertain'] for i in items)


def test_ai_bodies_near_the_edge_but_not_on_it_are_left_alone():
    """Negativo, dalla corsa Claude: due resistori veri aggiunti dal modello a
    3 mm dal bordo destro e un corpo a 5 mm dal sinistro non toccano il bordo."""
    items = [ai_body('R9', [0.896, 0.331, 0.923, 0.472]),
             ai_body('R11', [0.897, 0.544, 0.923, 0.678]),
             ai_body('X1', [0.114, 0.387, 0.162, 0.711])]
    photo = np.zeros((304, 856, 3), np.uint8)
    assert flag_ai_bodies_on_edge_contacts(photo, items, SIDES, *RS485) == 0
    assert all(i['presence_confidence'] == 'high' for i in items)


def test_a_detected_body_on_the_contacts_is_not_judged_here():
    """Negativo: un corpo con una proposta del rilevatore dietro (un connettore
    visto da YOLO) non e' un'aggiunta del modello."""
    item = ai_body('J1', [0.0, 0.0, 0.105, 1.0]) | dict(source_candidate_ids=[4])
    photo = np.zeros((304, 856, 3), np.uint8)
    assert flag_ai_bodies_on_edge_contacts(photo, [item], SIDES, *RS485) == 0


def test_one_contact_is_not_a_row_and_through_holes_are_exempt():
    positions = contact_positions(SIDES, *RS485)
    # Tocca il bordo sinistro su un solo contatto (quello centrale): non e' una fila.
    assert rows_over_edge_contacts([[0.0, 0.4, 0.05, 0.6]], positions, *RS485) == set()
    through = dict(layout='castellated', castellated_sides=[
        dict(edge='left', count=3, pitch_mm=5.08, contact_style='through_hole',
             through_hole_drill_mm=1.0, through_hole_inset_mm=1.5)])
    assert rows_over_edge_contacts([[0.0, 0.0, 0.105, 1.0]],
                                   contact_positions(through, *RS485), *RS485) == set()


# ── Correzioni della revisione avversariale del codice (126) ────────────


def test_a_non_string_component_id_does_not_crash_the_inventory():
    """B1: un component_id oggetto rendeva la risposta intera inutilizzabile."""
    bad = body('x', [2], bbox=[.6, .6, .7, .7])
    bad['component_id'] = {'a': 1}
    data = answer([body('one', [1], bbox=[.2, .2, .4, .4]), bad],
                  [decision(1, 'one', 'keep'), decision(2, 'x', 'keep')])
    result, _, _ = run(data, [box(1, [.2, .2, .4, .4]), box(2, [.6, .6, .7, .7])])
    assert any(r.get('component_id') == 'one' for r in result)
    assert any(r['_id'] == 2 and 'component_id' not in r for r in result)


def test_one_invalid_source_id_does_not_let_a_group_hide_its_members():
    """Una riga con un ID inventato fra le sorgenti diventa un involucro
    generico; i corpi veri che reclamava devono restare corpi a se': un solo
    campo malformato non puo' far assorbire corpi disgiunti a un riquadro
    mai controllato (seconda revisione avversariale, 126)."""
    items = [box(1, [.1, .1, .2, .2]), box(2, [.5, .5, .6, .6]), box(3, [.3, .3, .4, .4])]
    data = answer([body('group', [1, 2, 99], bbox=GROUP), body('r3', [3], bbox=[.3, .3, .4, .4])],
                  [decision(1, 'group'), decision(2, 'group'), decision(3, 'r3', 'keep')])
    result, _, _ = run(data, items)
    alone = {r['_id'] for r in result if 'component_id' not in r}
    assert {1, 2} <= alone


def test_edge_bodies_are_not_doubts_when_the_model_enumerated_everything():
    """R7: senza proposte del rilevatore ogni corpo e' «senza proposte»; un
    connettore di bordo vero non deve diventare un dubbio per questo."""
    items = [ai_body('J1', [0.0, 0.0, 0.105, 1.0])]
    photo = np.zeros((304, 856, 3), np.uint8)
    assert flag_ai_bodies_on_edge_contacts(photo, items, SIDES, *RS485, enumerated=True) == 0
    assert items[0]['presence_confidence'] == 'high'
