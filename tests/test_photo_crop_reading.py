"""@brief Per-body crop reading and provider-aware image limits (user decision 2026-09-28, HANDOFF 144).

Every test is offline: `call` is a scripted fake, never the network.
"""
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from kicadgen.services import photo_crop_reading as crop
from kicadgen.services import photo_grounding as grounding
from kicadgen.services import photo_markings as markings
from kicadgen.services.ai_diagnostics import CallCancelled
from kicadgen.services.openrouter import OpenRouterError, ToolArgumentsError

LENGTH, WIDTH = 42.8, 15.2
DEEPSEEK = "deepseek-api/deepseek-flash"
CLAUDE = "anthropic/claude-opus-5"


def _photo(width_px=987):
    height_px = round(width_px * WIDTH / LENGTH)
    return np.full((height_px, width_px, 3), 60, np.uint8)


def _row(**extra):
    """A global-inventory row that read an SOIC-8 as SOIC-16 (the observed instability)."""
    row = {'_id': 7, 'component_id': 'U1', 'name': 'U1 (ic)', 'class': 'ic', 'kind': 'ic',
           'x_mm': -5.0, 'y_mm': 1.0, 'length_mm': 6.0, 'width_mm': 5.0, 'rotation_deg': 0.0,
           'package': 'SOIC-16', 'package_confidence': 'high', '_package_verified': False,
           'lead_count': 16, 'lead_count_complete': True, 'lead_count_confidence': 'high',
           'pins_by_side': {'left': 8, 'right': 8}, 'pin_count': 16,
           'pin_count_confidence': 'high', 'pin_count_source': 'complete_lead_observation',
           'terminal_count': 16, 'marking': 'VPT 8?8B', 'marking_confidence': 'low',
           'class_confidence': 'medium', '_review_reason': 'global evidence'}
    row.update(extra)
    return row


def _reading(package='SOIC-8', left=4, right=4, top=0, bottom=0, cls='ic', tab=False,
             marking='VPT 858B'):
    return {'class': cls, 'package': package, 'tab': tab, 'marking': marking,
            'leads_by_side': {'left': left, 'right': right, 'top': top, 'bottom': bottom}}


class Script:
    """Fake paid call: returns (or raises) the scripted answers in order, records every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, **kwargs):
        self.requests.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _read(components, call, cache_dir=None, model=DEEPSEEK, photo=None, **kwargs):
    logs = []
    summary = crop.read_bodies(photo if photo is not None else _photo(), components,
                               LENGTH, WIDTH, model, call, logs.append, cache_dir, **kwargs)
    return summary, logs


# ── agreement ─────────────────────────────────────────────────────────────

def test_two_agreeing_readings_are_adopted_through_the_paid_call():
    component = _row()
    call = Script(_reading(), _reading(package='SOP-8'))      # same family after normalisation
    summary, logs = _read([component], call)
    assert (summary.agreed, summary.disagreed, summary.calls) == (1, 0, 2)
    assert component['_crop_reading'] == 'agreed'
    assert component['package'] == 'SOIC-8' and component['_package_inventory'] == 'SOIC-16'
    assert component['package_confidence'] == 'medium'
    assert component['lead_count'] == 8 and component['lead_count_complete'] is True
    assert component['pins_by_side'] == {'left': 4, 'right': 4}
    assert component['pin_count'] == 8 and component['terminal_count'] == 8
    assert component['_package_verified'] is False           # two readings are not a verification
    assert component['marking'] == 'VPT 858B' and component['_marking_inventory'] == 'VPT 8?8B'
    # Every request is the short per-body question, sent through `call`.
    for request in call.requests:
        assert request['model'] == DEEPSEEK and request['max_tokens'] == crop.MAX_TOKENS
        assert request['tool']['function']['name'] == 'report_body'
        assert [b['type'] for b in request['content_blocks']] == ['text', 'image_url']
    assert any('crop_reading=agreed' in line for line in logs)


def test_disagreeing_readings_keep_the_global_row_and_mark_it_for_review():
    """Negative of the test above: a different lead count (SOIC-8 vs SOIC-16) is not adopted."""
    component = _row()
    call = Script(_reading(), _reading(package='SOIC-16', left=8, right=8))
    summary, _ = _read([component], call)
    assert (summary.agreed, summary.disagreed) == (0, 1)
    assert component['_crop_reading'] == 'disagreed'
    assert component['package'] == 'SOIC-16' and '_package_inventory' not in component
    assert component['lead_count'] == 16 and component['pins_by_side'] == {'left': 8, 'right': 8}
    assert component['package_confidence'] == 'low'
    assert component['lead_count_confidence'] == 'low'
    assert 'pin_count' not in component                      # promotion needs medium/high
    assert component['_uncertain'] is True
    reason = component['_review_reason']
    assert 'package=SOIC-8' in reason and 'package=SOIC-16' in reason
    assert reason.endswith('global evidence')


@pytest.mark.parametrize('first,second,agree', [
    ('SOIC-8', 'sop8', True), ('SOT23', 'SOT-23 (3 leads)', True),
    ('SOIC-8', 'SOIC-16', False), ('SOIC-8', 'SSOP-8', False), ('SOT-23', 'SOT-23-6', False)])
def test_package_family_normalises_spelling_but_keeps_family_and_count(first, second, agree):
    assert (crop.package_family(first) == crop.package_family(second)) is agree


def _inventory_values(component):
    """The row without the keys the crop reading records about itself."""
    return {k: v for k, v in component.items() if not k.startswith('_crop_reading')}


def test_a_failed_reading_keeps_the_inventory_row_after_one_retry_per_body():
    """Contract changed by the user (HANDOFF 146 V7): a FAILED reading is not a disagreement.

    Before, a body without two readings was flagged like a disagreement:
    confidences demoted, pin_count removed, review reason added. No reading
    exists to disagree with, so now the row keeps every inventory value.
    """
    component = _row()
    before = _inventory_values(component)
    call = Script(OpenRouterError('truncated'), OpenRouterError('truncated'), _reading())
    summary, logs = _read([component], call)
    assert summary.calls == 3                                 # 2 readings + ONE retry, not more
    assert (summary.failed, summary.disagreed, summary.bodies_read) == (1, 0, 0)
    assert summary.interrupted is False                       # 2 errors in a row, not 3
    assert component['_crop_reading'] == 'failed'
    assert _inventory_values(component) == before             # nothing demoted or removed
    assert summary.warning and any(summary.warning in str(line) for line in logs)


def test_a_retry_that_succeeds_still_allows_agreement_and_the_budget_is_per_body():
    first, second = _row(), _row(_id=8, component_id='U2', x_mm=10.0)
    call = Script(ToolArgumentsError('not json'), _reading(), _reading(),   # body 1: retried
                  OpenRouterError('down'), _reading(), _reading())          # body 2: its own retry
    summary, _ = _read([first, second], call)
    assert summary.calls == 6 and summary.agreed == 2
    # A second failure on the same body is not retried again.
    third = _row()
    call = Script(OpenRouterError('a'), _reading(), OpenRouterError('b'))
    summary, _ = _read([third], call)
    assert summary.calls == 3 and third['_crop_reading'] == 'failed'


def test_an_answer_outside_the_schema_counts_as_a_failed_reading():
    """An answer that does not parse is no reading: the row is kept, not flagged."""
    component = _row()
    before = _inventory_values(component)
    call = Script(_reading(cls='gizmo'), {'class': 'ic'}, _reading())
    summary, _ = _read([component], call)
    assert component['_crop_reading'] == 'failed' and summary.failed == 1
    assert _inventory_values(component) == before
    assert summary.provider_errors == 0 and not summary.interrupted


# ── provider errors: stop and warn (user decision, HANDOFF 146 V7) ───────

def test_repeated_provider_errors_stop_the_pass_and_leave_every_row_untouched():
    from kicadgen.services.messages import WARNING, kind_of
    rows = [_row(_id=i, component_id=f'U{i}', x_mm=-12.0 + 6 * i) for i in range(4)]
    before = [_inventory_values(row) for row in rows]
    call = Script(*[OpenRouterError('HTTP 402: no credit')] * 20)
    summary, logs = _read(rows, call)
    # The run stops after PROVIDER_ERRORS_BEFORE_STOP requests: no further paid call.
    assert summary.calls == len(call.requests) == crop.PROVIDER_ERRORS_BEFORE_STOP
    assert summary.interrupted is True and summary.provider_errors == summary.calls
    assert (summary.failed, summary.not_read, summary.bodies_read) == (1, 3, 0)
    assert rows[0]['_crop_reading'] == 'failed'
    for row in rows[1:]:
        assert not any(key.startswith('_crop_reading') for key in row)
    assert [_inventory_values(row) for row in rows] == before
    # One visible warning, returned for the caller's warnings and logged as such.
    assert 'HTTP 402: no credit' in summary.warning
    assert [kind_of(line) for line in logs if str(line) == summary.warning] == [WARNING]


def test_a_missing_key_or_a_network_error_stops_the_pass_too():
    """Every provider/transport failure counts, whatever its class."""
    from kicadgen.services.openrouter import MissingApiKeyError
    for error in (MissingApiKeyError('no key'), OSError('network down'), ValueError('bad body')):
        rows = [_row(), _row(_id=9, component_id='U9', x_mm=10.0)]
        summary, _ = _read(rows, Script(*[error] * 10))
        assert summary.interrupted and summary.calls == crop.PROVIDER_ERRORS_BEFORE_STOP
        assert '_crop_reading' not in rows[1]


def test_broken_answers_between_provider_errors_do_not_stop_the_pass():
    """Negative of the rule: a broken answer IS an answer, so the provider is up."""
    rows = [_row(), _row(_id=9, component_id='U9', x_mm=10.0)]
    call = Script(OpenRouterError('a'), ToolArgumentsError('not json'), OpenRouterError('b'),
                  OpenRouterError('c'), _reading(), _reading())
    summary, _ = _read(rows, call)
    assert not summary.interrupted and summary.calls == 6
    assert rows[0]['_crop_reading'] == 'failed' and rows[1]['_crop_reading'] == 'agreed'


def test_one_transient_error_then_success_reads_the_body_normally():
    component = _row()
    summary, _ = _read([component], Script(OpenRouterError('timeout'), _reading(), _reading()))
    assert component['_crop_reading'] == 'agreed' and component['package'] == 'SOIC-8'
    assert summary.failed == 0 and summary.warning is None and not summary.interrupted


def test_two_valid_different_readings_are_still_a_disagreement_not_a_failure():
    """Negative of the failure rule: disagreement needs two VALID readings, and still flags."""
    component = _row()
    summary, _ = _read([component], Script(_reading(), _reading(package='SOIC-16', left=8,
                                                                right=8)))
    assert component['_crop_reading'] == 'disagreed' and summary.failed == 0
    assert component['package_confidence'] == 'low' and 'pin_count' not in component
    assert summary.warning is None


def test_cancellation_propagates_and_is_never_retried():
    class Cancelled(CallCancelled):
        pass
    component = _row()
    call = Script(Cancelled('stop'))
    with pytest.raises(Cancelled):
        _read([component], call)
    assert len(call.requests) == 1 and '_crop_reading' not in component


def test_bodies_excluded_by_default_are_not_read():
    excluded = _row(_ai_only_excluded=True)
    kept_by_user = _row(_id=9, _ai_only_excluded=False)
    call = Script(_reading(), _reading())
    summary, _ = _read([excluded, kept_by_user], call)
    assert summary.bodies_read == 1 and summary.skipped == 1 and summary.calls == 2
    assert '_crop_reading' not in excluded and excluded['package'] == 'SOIC-16'
    assert kept_by_user['_crop_reading'] == 'agreed'


def test_an_uninformative_agreement_does_not_erase_the_global_row():
    component = _row()
    blank = _reading(cls='unknown', package='', left=None, right=None, top=None, bottom=None,
                     marking='')
    _read([component], Script(blank, dict(blank)))
    assert component['_crop_reading'] == 'agreed'
    assert component['class'] == 'ic' and component['package'] == 'SOIC-16'
    assert component['lead_count'] == 16 and component['marking'] == 'VPT 8?8B'


def test_disagreeing_markings_keep_the_one_with_fewer_unreadable_characters():
    component = _row(marking='V?T 8?8B')
    _read([component], Script(_reading(marking='VPT 858B'), _reading(marking='VPT 85?B')))
    assert component['marking'] == 'VPT 858B' and component['marking_confidence'] == 'low'
    # Negative: a reading no better than the global one leaves it.
    other = _row(marking='VPT 858B')
    _read([other], Script(_reading(marking='V?T 858B'), _reading(marking='VPT 8??B')))
    assert other['marking'] == 'VPT 858B'


# ── cache ─────────────────────────────────────────────────────────────────

def test_a_rerun_on_the_same_crop_pays_nothing(tmp_path):
    first = _row()
    _read([first], Script(_reading(), _reading()), tmp_path)
    again = _row()

    def forbidden(**kwargs):
        pytest.fail('cached reading paid again')
    summary, _ = _read([again], forbidden, tmp_path)
    assert summary.calls == 0 and summary.readings_cached == 2
    assert again['package'] == 'SOIC-8' and again['_crop_reading'] == 'agreed'


def test_the_two_readings_are_cached_apart_and_failures_are_not_cached(tmp_path):
    """A shared key would replay reading 1 as reading 2 and turn a disagreement into agreement."""
    component = _row()
    _read([component], Script(_reading(), _reading(package='SOIC-16', left=8, right=8)), tmp_path)
    replay = _row()
    _read([replay], lambda **kw: pytest.fail('paid'), tmp_path)
    assert replay['_crop_reading'] == 'disagreed'
    # A failed reading leaves no cache entry: the next run pays for it again.
    failed_dir = tmp_path / 'failed'
    _read([_row()], Script(OpenRouterError('x'), OpenRouterError('y'), _reading()), failed_dir)
    call = Script(_reading())
    summary, _ = _read([_row()], call, failed_dir)
    assert summary.calls == 1 and summary.readings_cached == 1


# ── the crop ──────────────────────────────────────────────────────────────

def test_the_crop_frames_the_body_and_is_enlarged():
    photo = _photo()
    scale = photo.shape[1] / LENGTH
    body = _row(x_mm=-10.0, y_mm=2.0, length_mm=5.0, width_mm=4.0)
    # A white body at the row's place, in pixels of the calibrated photo.
    cx, cy = (body['x_mm'] + LENGTH / 2) * scale, (body['y_mm'] + WIDTH / 2) * scale
    cv2.rectangle(photo, (round(cx - 2.5 * scale), round(cy - 2 * scale)),
                  (round(cx + 2.5 * scale), round(cy + 2 * scale)), (255, 255, 255), -1)
    image, text = crop.body_crop(photo, body, LENGTH, WIDTH)
    assert max(image.shape[:2]) == crop.CROP_LONG_SIDE_PX
    assert image.shape[1] > 5 * scale                           # enlarged, not native
    h, w = image.shape[:2]
    assert (image[h // 2, w // 2] == 255).all()                 # the body is in the middle
    magenta = np.all(image == crop.FRAME_BGR, axis=2)
    assert magenta.any() and not magenta[h // 2, w // 2]        # framed, frame on its outline
    assert '5.00 x 4.00 mm' in text
    # Negative: a body elsewhere does not bring this one to the middle.
    other, _ = crop.body_crop(photo, _row(x_mm=12.0, y_mm=-3.0), LENGTH, WIDTH)
    oh, ow = other.shape[:2]
    assert not (other[oh // 2, ow // 2] == 255).all()


def test_the_crop_sent_to_a_small_model_respects_its_long_side():
    component = _row()
    call = Script(_reading(), _reading())
    _read([component], call, model='m')                         # unknown: 768 px assumed
    url = call.requests[0]['content_blocks'][1]['image_url']['url']
    import base64
    png = base64.b64decode(url.split(',', 1)[1])
    image = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    assert max(image.shape[:2]) == grounding.UNKNOWN_IMAGE_LIMITS.long_side_px


# ── provider-aware image limits ──────────────────────────────────────────

def test_image_limits_come_from_the_provider_of_the_identifier():
    assert grounding.model_image_limits(DEEPSEEK) is grounding.DEEPSEEK_IMAGE_LIMITS
    assert grounding.model_image_limits(CLAUDE) is grounding.REFERENCE_IMAGE_LIMITS
    assert grounding.model_image_limits('qwen/qwen3-vl') is grounding.REFERENCE_IMAGE_LIMITS
    assert grounding.model_image_limits('deepseek/deepseek-chat') is grounding.REFERENCE_IMAGE_LIMITS
    for unknown in ('m', '', None):
        assert grounding.model_image_limits(unknown) is grounding.UNKNOWN_IMAGE_LIMITS
    # The reference limits are today's constants: nothing changes for Claude.
    assert grounding.model_px_per_mm(40, 20, grounding.REFERENCE_IMAGE_LIMITS) \
        == grounding.model_px_per_mm(40, 20)


def test_deepseek_gets_detail_tiles_where_claude_does_not():
    size = _photo().shape[1::-1]                                # the RS485 photo: 987 px
    assert grounding.detail_grid(LENGTH, WIDTH, size,
                                 grounding.model_image_limits(CLAUDE)) == []
    assert grounding.detail_grid(LENGTH, WIDTH, size,
                                 grounding.model_image_limits(DEEPSEEK)) != []
    assert grounding.detail_grid(LENGTH, WIDTH, size,
                                 grounding.model_image_limits('m')) != []


def test_detail_tiles_are_enlarged_only_for_models_that_gain_from_it():
    tile = np.zeros((300, 400, 3), np.uint8)
    assert grounding.fit_to_model(tile, grounding.REFERENCE_IMAGE_LIMITS) is tile
    enlarged = grounding.fit_to_model(tile, grounding.DEEPSEEK_IMAGE_LIMITS)
    assert enlarged.shape[1] > 400
    assert enlarged.shape[0] * enlarged.shape[1] <= grounding.DEEPSEEK_IMAGE_LIMITS.max_pixels
    labels = [label for label, *_ in grounding.grounding_regions(_photo(), LENGTH, WIDTH,
                                                                  DEEPSEEK)]
    assert len(labels) > 1


def test_the_marking_reading_is_skipped_for_claude_but_runs_for_deepseek():
    def components():
        return [dict(name='U1', x_mm=-5.0, y_mm=0.0, length_mm=5.0, width_mm=4.0,
                     rotation_deg=0.0, marking='SP3?52', marking_confidence='medium')]
    answer = {'components': [dict(id=1, marking='SP3452', marking_confidence='high')]}
    claude, deepseek = Script(), Script(answer)
    markings.read_markings(_photo(1024), components(), LENGTH, WIDTH, CLAUDE, claude,
                           lambda _l: None)
    assert claude.requests == []
    rows = components()
    markings.read_markings(_photo(1024), rows, LENGTH, WIDTH, DEEPSEEK, deepseek,
                           lambda _l: None)
    assert len(deepseek.requests) == 1 and rows[0]['marking'] == 'SP3452'


# ── integration in extract_inventory ─────────────────────────────────────

def _inventory_response():
    return dict(components=[dict(component_id='U1', source_candidate_ids=[1],
        **{'class': 'ic'}, bbox=[.2, .2, .4, .6], bbox_basis='body_and_leads', rotation_deg=0,
        package='SOIC-16', marking='', terminal_count=16, terminal_layout='8 left, 8 right',
        height_mm=None, height_source='unknown', confidence='high', evidence='ic body',
        presence_confidence='high', class_confidence='high', lead_count=16,
        lead_count_complete=True, lead_count_confidence='high', package_confidence='high',
        leads_by_side={'left': 8, 'right': 8, 'top': 0, 'bottom': 0},
        grouping_basis='single_body', asymmetry_landmark=None)],
        candidate_decisions=[dict(candidate_id=1, status='keep', component_id='U1',
                                  reason='body')], uncertainties=[])


def _extract(monkeypatch, tmp_path, call, phases):
    from kicadgen.services import photo_inventory as inv, photo_yolo
    photo = np.full((100, 200, 3), 50, np.uint8)
    monkeypatch.setattr(inv, 'prepare_photo', lambda *args: photo)
    monkeypatch.setattr(photo_yolo, 'detect_yolo_components', lambda *a, **k: [dict(
        _id=1, _box=[.2, .2, .4, .6], name='target', kind='generic', x_mm=-6, y_mm=-2,
        length_mm=8, width_mm=8, height_mm=1, rotation_deg=0, color_hex='#303030',
        _source='YOLO local')])
    return inv.extract_inventory([tmp_path / 'image.png'], 'photo/model', 40, 20,
                                 ['generic', 'ic'], call, detector='yolo',
                                 classifier_model=DEEPSEEK, top_photo_index=0,
                                 phase=lambda tool, detail: phases.append((tool, detail)))


def test_extract_inventory_reads_every_retained_body_after_the_global_inventory(
        monkeypatch, tmp_path):
    def call(**kwargs):
        name = kwargs['tool']['function']['name']
        call.names.append(name)
        return _inventory_response() if name == 'report_global_inventory' else _reading(marking='')
    call.names = []
    phases = []
    report = _extract(monkeypatch, tmp_path, call, phases)
    assert call.names[:3] == ['report_global_inventory', 'report_body', 'report_body']
    [row] = report.components
    assert row['package'] == 'SOIC-8' and row['_crop_reading'] == 'agreed'
    tools = [tool for tool, _ in phases]
    start = tools.index('llm')
    assert tools[start - 1] == 'llm_inventory' and tools[start + 1] == 'llm_inventory'
    assert phases[start][1] == 'lettura dei singoli componenti'


def test_extract_inventory_lets_a_cancellation_through(monkeypatch, tmp_path):
    class Cancelled(CallCancelled):
        pass

    def call(**kwargs):
        if kwargs['tool']['function']['name'] == 'report_global_inventory':
            return _inventory_response()
        raise Cancelled('user stop')
    with pytest.raises(Cancelled):
        _extract(monkeypatch, tmp_path, call, [])


# ── differences of form only (user decision 2026-09-28) ─────────────────

def _chip_row(length_mm=2.1, width_mm=1.3, **extra):
    row = _row(**{'class': 'resistor', 'kind': 'resistor'}, package='1206',
               length_mm=length_mm, width_mm=width_mm, lead_count=0,
               lead_count_complete=False, terminal_count=2, marking='')
    for key in ('pins_by_side', 'pin_count', 'pin_count_confidence', 'pin_count_source'):
        row.pop(key)
    row.update(extra)
    return row


def _chip(package, sides=(0, 0, 0, 0), cls='resistor'):
    return _reading(package=package, cls=cls, marking='', left=sides[0], right=sides[1],
                    top=sides[2], bottom=sides[3])


def test_zero_and_null_on_a_side_are_the_same_observation():
    first = crop.parse_reading(_reading(top=0, bottom=None))
    second = crop.parse_reading(_reading(top=None, bottom=0))
    assert crop.disagreements(first, second) == []
    # Negative: a counted lead is not the same as none.
    third = crop.parse_reading(_reading(top=1, bottom=None))
    assert crop.disagreements(first, third) == ['leads']


def test_a_leadless_chip_read_as_zeros_or_nulls_agrees_and_keeps_its_terminals():
    component = _chip_row()
    _read([component], Script(_chip('0805'), _chip('0805', (None, None, None, None))))
    assert component['_crop_reading'] == 'agreed'
    assert component['terminal_count'] == 2 and 'pins_by_side' not in component
    assert component['package'] == '0805'


def test_a_generic_chip_word_agrees_with_a_size_code_that_fits_the_photo():
    component = _chip_row(length_mm=2.1, width_mm=1.3)       # an 0805 body on the photo
    _read([component], Script(_chip('CHIP'), _chip('chip resistor 0805 (2012 metric)')))
    assert component['_crop_reading'] == 'agreed'
    assert component['package'] == '0805'                     # the size code is adopted
    assert component['_package_inventory'] == '1206'


def test_a_size_code_that_does_not_fit_the_photo_still_disagrees():
    """Negative: the same answers on a body measured as a 1206 do not agree."""
    component = _chip_row(length_mm=3.3, width_mm=1.65)
    _read([component], Script(_chip('CHIP'), _chip('0805')))
    assert component['_crop_reading'] == 'disagreed'
    assert component['package'] == '1206'


def test_the_tolerance_comes_from_the_photo_scale():
    """A width 0.2 mm off 0805 fits on a coarse photo, not on a sharp one."""
    first, second = crop.parse_reading(_chip('SMD chip')), crop.parse_reading(_chip('0805'))
    coarse = crop._envelope(_chip_row(width_mm=1.45), _photo(300), LENGTH, WIDTH)
    sharp = crop._envelope(_chip_row(width_mm=1.45), _photo(4000), LENGTH, WIDTH)
    assert coarse[2] > sharp[2]
    assert crop.disagreements(first, second, coarse) == []
    assert crop.disagreements(first, second, sharp) == ['package']


@pytest.mark.parametrize('a,b,aspect', [
    (_chip('CHIP', cls='resistor'), _chip('0805', cls='capacitor'), 'class'),
    (_chip('0805'), _chip('0402'), 'package'),
    (_chip('SOD-123', cls='diode'), _chip('SMA', cls='diode'), 'package'),
    (_chip(''), _chip('0805'), 'package')])
def test_real_differences_still_disagree(a, b, aspect):
    envelope = (2.1, 1.3, 0.2)
    reasons = crop.disagreements(crop.parse_reading(a), crop.parse_reading(b), envelope)
    assert aspect in reasons


def test_two_generic_chip_words_agree_without_changing_the_package():
    component = _chip_row()
    _read([component], Script(_chip('CHIP'), _chip('SMD chip resistor')))
    assert component['_crop_reading'] == 'agreed' and component['package'] == '1206'


def test_size_codes_are_read_in_any_spelling():
    assert crop.chip_size_code('1206 (3216 metric)') == '1206'
    assert crop.chip_size_code('3216 Metric') == '1206'
    assert crop.chip_size_code('CHIP-RESISTOR-0805') == '0805'
    assert crop.chip_size_code('0603') == '0603'              # bare code: imperial
    assert crop.chip_size_code('0805 or 1206') is None
    assert crop.chip_size_code('SOT-23') is None and crop.chip_size_code('SOIC-8') is None


# ── the photo measurement outranks agreeing readings (defect fix, 2026-09-28) ──

def test_two_readings_agreeing_on_a_size_the_photo_contradicts_are_not_adopted():
    """The real-run case: an 0805-sized body read twice as 0402."""
    component = _chip_row(length_mm=2.48, width_mm=1.45, package='0805')
    summary, _ = _read([component], Script(_chip('0402'), _chip('0402')))
    assert summary.disagreed == 1 and summary.agreed == 0
    assert component['_crop_reading'] == 'disagreed'
    assert component['package'] == '0805' and '_package_inventory' not in component
    assert component['package_confidence'] == 'low'
    reason = component['_review_reason']
    assert '0402' in reason and '2.48 x 1.45 mm' in reason and 'mm)' in reason


def test_two_readings_agreeing_on_a_size_the_photo_confirms_are_adopted():
    """Positive twin of the test above: same answers, a body that is an 0402."""
    component = _chip_row(length_mm=1.05, width_mm=0.52, package='0805')
    _read([component], Script(_chip('0402'), _chip('0402 (1005 metric)')))
    assert component['_crop_reading'] == 'agreed' and component['package'] == '0402'


def test_packages_without_a_standard_chip_size_are_not_checked_against_the_envelope():
    """Negative: the size check applies to chip codes only, not to SOIC and the like."""
    component = _row(length_mm=20.0, width_mm=20.0)
    _read([component], Script(_reading(), _reading()))
    assert component['_crop_reading'] == 'agreed' and component['package'] == 'SOIC-8'


def test_a_chip_size_without_a_measured_envelope_is_not_adopted():
    first = crop.parse_reading(_chip('0805'))
    assert crop.size_contradiction(first, None) == '0805'
    assert crop.size_contradiction(first, (2.1, 1.3, 0.2)) is None


# ── prompt contract (HANDOFF 145) ─────────────────────────────────────────

def test_chip_passives_read_with_an_empty_package_keep_the_inventory_package():
    """The prompt asks no size code for chip passives: the inventory's package stays.

    Negative twin: a leaded body read twice with a family does get that family.
    """
    component = _chip_row()
    confidence = component.get('package_confidence')
    _read([component], Script(_chip(''), _chip('')))
    assert component['_crop_reading'] == 'agreed'
    assert component['package'] == '1206' and component.get('package_confidence') == confidence
    leaded = _row(package='SOIC-16')
    _read([leaded], Script(_reading(), _reading()))
    assert leaded['package'] == 'SOIC-8' and leaded['package_confidence'] == 'medium'


def test_prompt_is_generic_and_versioned():
    """Criteria by appearance of families, nothing of the reference board, new cache version."""
    prompt = crop.SYSTEM_PROMPT
    assert crop.PROMPT_VERSION != 'crop-reading-1'
    assert 'leave package EMPTY' in prompt and 'size code is not read from this crop' in prompt
    for criterion in ('METAL', 'chip resistor', 'ceramic capacitor', 'led:', 'diode:', 'SOT-23'):
        assert criterion in prompt
    # Markings and names of the board the prompt was measured on must not leak into it.
    for token in ('A15HB', '2A', '712', 'VPT', '858B', 'RS485', 'CBE', '3457'):
        assert token not in prompt


# ── editable prompt (profile field 'crop_prompt', HANDOFF 146) ───────────

CUSTOM_PROMPT = 'Custom crop instructions edited in the profile.'


def test_the_profile_prompt_reaches_the_paid_call():
    call = Script(_reading(), _reading())
    _read([_row()], call, system_prompt=CUSTOM_PROMPT)
    assert [request['system_prompt'] for request in call.requests] == [CUSTOM_PROMPT] * 2


@pytest.mark.parametrize('given', [None, '', '   \n'])
def test_a_missing_or_blank_prompt_sends_the_factory_text(given):
    """Negative of the test above: no text is never an empty system prompt."""
    call = Script(_reading(), _reading())
    _read([_row()], call, system_prompt=given)
    assert [request['system_prompt'] for request in call.requests] == [crop.SYSTEM_PROMPT] * 2


def test_a_reading_cached_with_other_instructions_is_not_reused(tmp_path):
    _read([_row()], Script(_reading(), _reading()), tmp_path, system_prompt=CUSTOM_PROMPT)
    # Same crop, same model, other text: both readings are paid again.
    call = Script(_reading(), _reading())
    summary, _ = _read([_row()], call, tmp_path)
    assert (summary.calls, summary.readings_cached) == (2, 0)
    # Same text as the first run: served from the cache, nothing paid.
    summary, _ = _read([_row()], lambda **kw: pytest.fail('paid'), tmp_path,
                       system_prompt=CUSTOM_PROMPT)
    assert (summary.calls, summary.readings_cached) == (0, 2)


#: Cache key of one fixed reading with the factory text, computed once and
#: frozen here (HANDOFF 146 V6). It pins the key formula, PROMPT_VERSION,
#: reading_tool and SYSTEM_PROMPT together: any change to them invalidates the
#: readings already cached on users' disks, and must be deliberate.
FROZEN_FACTORY_KEY = 'c09e86213b6bfc64874f3feae24ebc0c9d373e57f46ca83fda16a65289f63a30'


def test_the_factory_text_keeps_a_frozen_cache_key():
    """The key of a fixed reading with the factory text is a known literal.

    If SYSTEM_PROMPT is changed on purpose: update this literal AND bump
    PROMPT_VERSION AND add the digest of the old text to
    SUPERSEDED_CROP_PROMPT_DIGESTS (so existing profile databases receive the
    new text). A change of the key formula alone also lands here.
    """
    args = (b'\x89PNG frozen crop', DEEPSEEK, 'frozen request text', 1)
    assert crop._cache_key(*args, crop.effective_prompt(None)) == FROZEN_FACTORY_KEY
    # Negative: another text, another reading number or another model is another key.
    assert crop._cache_key(*args, CUSTOM_PROMPT) != FROZEN_FACTORY_KEY
    assert crop._cache_key(*args[:3], 2, crop.SYSTEM_PROMPT) != FROZEN_FACTORY_KEY
    assert crop._cache_key(args[0], CLAUDE, *args[2:], crop.SYSTEM_PROMPT) != FROZEN_FACTORY_KEY


# ── provenance of the text on the row (HANDOFF 146 V5) ───────────────────

def test_rows_record_the_digest_of_the_text_that_produced_them():
    from kicadgen.services.component_types import text_digest
    factory, edited = _row(), _row()
    _read([factory], Script(_reading(), _reading()))
    _read([edited], Script(_reading(), _reading()), system_prompt=CUSTOM_PROMPT)
    assert factory['_crop_reading_prompt_digest'] == text_digest(crop.SYSTEM_PROMPT)
    assert edited['_crop_reading_prompt_digest'] == text_digest(CUSTOM_PROMPT)
    assert factory['_crop_reading_prompt_digest'] != edited['_crop_reading_prompt_digest']
    # The version alone could not tell them apart.
    assert factory['_crop_reading_version'] == edited['_crop_reading_version']


def test_the_stage_audit_records_the_text_actually_sent(tmp_path):
    import json
    _read([_row()], Script(_reading(), _reading()), tmp_path, system_prompt=CUSTOM_PROMPT)
    [audit] = (tmp_path / 'yolo-audit').glob('crop-reading-*.json')
    assert json.loads(audit.read_text(encoding='utf-8'))['prompt'] == CUSTOM_PROMPT


def test_extract_inventory_forwards_the_profile_prompt_to_the_crop_reading(
        monkeypatch, tmp_path):
    from kicadgen.services import photo_inventory as inv, photo_yolo
    photo = np.full((100, 200, 3), 50, np.uint8)
    monkeypatch.setattr(inv, 'prepare_photo', lambda *args: photo)
    monkeypatch.setattr(photo_yolo, 'detect_yolo_components', lambda *a, **k: [dict(
        _id=1, _box=[.2, .2, .4, .6], name='target', kind='generic', x_mm=-6, y_mm=-2,
        length_mm=8, width_mm=8, height_mm=1, rotation_deg=0, color_hex='#303030',
        _source='YOLO local')])
    sent = []

    def call(**kwargs):
        if kwargs['tool']['function']['name'] == 'report_global_inventory':
            return _inventory_response()
        sent.append(kwargs['system_prompt'])
        return _reading(marking='')
    inv.extract_inventory([tmp_path / 'image.png'], 'photo/model', 40, 20, ['generic', 'ic'],
                          call, detector='yolo', classifier_model=DEEPSEEK, top_photo_index=0,
                          crop_prompt=CUSTOM_PROMPT)
    assert sent[:2] == [CUSTOM_PROMPT] * 2


# ── «Rilancio inventario» runs the same tail (HANDOFF 146 V2) ────────────

def _reconcile(call, phases=None, **kwargs):
    from kicadgen.services import photo_inventory as inv
    photo = np.full((100, 200, 3), 50, np.uint8)
    proposals = [dict(_id=1, _box=[.2, .2, .4, .6], name='target', kind='generic', x_mm=-6,
                      y_mm=-2, length_mm=8, width_mm=8, height_mm=1, rotation_deg=0,
                      color_hex='#303030', _source='YOLO local')]
    logs = []
    report = inv.reconcile_saved_proposals(
        photo, proposals, 40, 20, DEEPSEEK, call, logs.append,
        phase=None if phases is None else (lambda tool, detail: phases.append((tool, detail))),
        **kwargs)
    return report, logs


def test_the_relaunch_reads_every_retained_body_with_the_profile_prompt():
    sent = []

    def call(**kwargs):
        name = kwargs['tool']['function']['name']
        sent.append((name, kwargs['system_prompt']))
        return _inventory_response() if name == 'report_global_inventory' else _reading(marking='')
    phases = []
    report, _ = _reconcile(call, phases, crop_prompt=CUSTOM_PROMPT)
    # Same order as extract_inventory: global inventory, then two crop readings.
    assert [name for name, _ in sent[:3]] == ['report_global_inventory', 'report_body',
                                              'report_body']
    assert [prompt for name, prompt in sent if name == 'report_body'] == [CUSTOM_PROMPT] * 2
    [row] = report.components
    assert row['package'] == 'SOIC-8' and row['_crop_reading'] == 'agreed'
    tools = [tool for tool, _ in phases]
    assert tools[tools.index('llm') + 1] == 'llm_inventory'


def test_the_relaunch_shares_the_tail_of_extract_inventory(monkeypatch, tmp_path):
    """Fails if either route stops calling the shared helper (they drifted once)."""
    from kicadgen.services import photo_inventory as inv
    seen = []
    real = inv.read_retained_bodies

    def spy(photo, report, *args):
        seen.append(args[-2])                 # crop_prompt
        return real(photo, report, *args)
    monkeypatch.setattr(inv, 'read_retained_bodies', spy)

    def call(**kwargs):
        name = kwargs['tool']['function']['name']
        return _inventory_response() if name == 'report_global_inventory' else _reading(marking='')
    _reconcile(call, crop_prompt=CUSTOM_PROMPT)
    _extract(monkeypatch, tmp_path, call, [])      # extract_inventory, no profile prompt
    assert seen == [CUSTOM_PROMPT, None]


def test_the_relaunch_keeps_rows_and_warns_when_the_provider_fails():
    def call(**kwargs):
        if kwargs['tool']['function']['name'] == 'report_global_inventory':
            return _inventory_response()
        raise OpenRouterError('HTTP 402: no credit')
    report, _ = _reconcile(call)
    [row] = report.components
    assert row['package'] == 'SOIC-16' and row['_crop_reading'] == 'failed'
    assert row['package_confidence'] == 'high'
    assert any('HTTP 402: no credit' in warning for warning in report.warnings)
