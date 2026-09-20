"""@brief Il confronto fra modelli deve mostrare le differenze che contano.

Lo strumento non chiama nessuna API in questi test: si verifica la parte che
decide *cosa* e' una differenza, perche' e' quella che potrebbe far sembrare
uguali due risposte diverse -- o diverse due risposte uguali.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('compare_vision_models',
                                               ROOT/'tools'/'compare_vision_models.py')
compare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compare)


def _component(identity='U1', **changes):
    row = dict(component_id=identity, **{'class': 'ic'}, package='SOT-223',
               length_mm=6.5, width_mm=3.7, height_mm=1.6, rotation_deg=0,
               _reported_rotation_deg=90, terminal_count=4,
               asymmetry_landmark='tab right', confidence='medium')
    row.update(changes)
    return row


def test_the_summary_keeps_the_reported_angle_not_the_axis_aligned_one():
    """@brief L'orientamento da confrontare e' quello che il modello ha detto.

    L'ingombro CAD e' sempre allineato agli assi: confrontare `rotation_deg`
    farebbe sembrare d'accordo due modelli che hanno visto il componente
    ruotato in modo diverso.
    """
    row = compare.summarize([_component()])['U1']
    assert row['rotation_deg'] == 90
    assert row['klass'] == 'ic' and row['package'] == 'SOT-223'
    assert row['landmark'] == 'tab right'


def test_a_component_only_one_model_named_is_itself_a_difference():
    left = compare.summarize([_component('U1'), _component('C3')])
    right = compare.summarize([_component('U1')])
    assert compare.differences(left, right) == ['C3: presente solo in A']


def test_type_package_orientation_and_terminals_are_reported_verbatim():
    left = compare.summarize([_component()])
    right = compare.summarize([_component(**{'class': 'regulator'}, package='SOT-23',
                                          _reported_rotation_deg=0, terminal_count=3,
                                          asymmetry_landmark='')])
    assert compare.differences(left, right) == [
        'U1.klass: A=\'ic\' B=\'regulator\'',
        'U1.package: A=\'SOT-223\' B=\'SOT-23\'',
        'U1.rotation_deg: A=90 B=0',
        'U1.terminal_count: A=4 B=3',
        "U1.landmark: A='tab right' B=''"]


@pytest.mark.parametrize('width,reported', [(3.75, False), (3.7 * 1.5, True)])
def test_only_a_real_size_disagreement_is_printed(width, reported):
    """@brief Coppia positivo/negativo: il rumore di misura non e' un disaccordo.

    Due modelli che leggono 3,70 e 3,75 mm sulla stessa fotografia non hanno
    visto due cose diverse; uno che legge il pacchetto e uno che legge la
    piazzola si'.
    """
    left = compare.summarize([_component()])
    right = compare.summarize([_component(width_mm=width)])
    assert bool(compare.differences(left, right)) is reported


def test_a_retained_proposal_is_not_an_answer():
    """@brief Distinguere chi ha risposto da chi e' tornato indietro com'era.

    Una proposta che la riconciliazione non giudica viene conservata con il
    nome del rilevatore e senza `component_id`: contarla come risposta e' cio'
    che faceva dichiarare «Differenze: 0» a due esecuzioni entrambe fallite.
    """
    summary = compare.summarize([
        _component('U1'),
        dict(name='YOLO candidate 7', kind='generic', length_mm=1.6, width_mm=.8,
             height_mm=1.0, rotation_deg=0, _uncertain=True,
             _global_inventory_errors=['candidate 7: missing/duplicate decision'])])
    assert compare.resolved_names(summary) == {'U1'}
    assert summary['YOLO candidate 7']['resolved'] is False


def test_a_run_without_a_single_answer_stops_the_comparison():
    """@brief Caso negativo del precedente, sul verdetto.

    Senza la fermata il confronto proseguiva, chiedeva anche il secondo
    modello e dichiarava «Differenze: 0» su due esecuzioni entrambe fallite:
    il verdetto piu' rassicurante proprio dove non c'e' stata risposta. La
    diagnosi della riconciliazione deve finire nel messaggio, altrimenti
    resta da cercare nei log.
    """
    raw = [dict(name='YOLO candidate 7', kind='generic', length_mm=1.6, width_mm=.8,
                height_mm=1.0, rotation_deg=0, _uncertain=True,
                _global_inventory_errors=['OPENROUTER_API_KEY non impostata'])]
    failed = compare.summarize(raw)
    with pytest.raises(SystemExit, match='OPENROUTER_API_KEY'):
        compare.answered_or_stop('A', 'qualche/modello', failed, raw)
    # Positivo: una sola riga riconciliata basta a proseguire, perche' il
    # confronto sa gia' segnalare quante ne restano irrisolte.
    mixed = compare.summarize(raw + [_component('U1')])
    assert compare.answered_or_stop('A', 'qualche/modello', mixed, raw) == {'U1'}


def test_the_key_is_demanded_before_the_local_detection_burns_minutes():
    """@brief La mancanza della chiave deve fermare in un secondo, non in dieci minuti.

    Senza questo controllo lo strumento rilevava 111 corpi, mandava la
    richiesta, riceveva l'errore della chiave sotto forma di risposta vuota e
    la trattava come una risposta.
    """
    from kicadgen.services import openrouter

    original = openrouter.api_key
    try:
        openrouter.api_key = lambda: (_ for _ in ()).throw(
            openrouter.OpenRouterError('Variabile d\'ambiente OPENROUTER_API_KEY non impostata.'))
        with pytest.raises(SystemExit, match='OPENROUTER_API_KEY'):
            compare.require_credentials()
        openrouter.api_key = lambda: 'chiave-di-prova'
        assert compare.require_credentials() is None
    finally:
        openrouter.api_key = original
