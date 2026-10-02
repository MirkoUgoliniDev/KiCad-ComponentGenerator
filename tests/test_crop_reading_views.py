"""Offline request/cache checks for the two views sent to the crop model."""
import base64
import json

import cv2
import numpy as np

from kicadgen.services import photo_crop_reading as crop
from test_photo_crop_reading import _read, _row, _obs, _photo, Script


def test_two_views_same_axes_and_native_metadata_in_each_request(tmp_path):
    row = _row(lead_count=8, bbox_basis='body')
    script = Script(_obs(), _obs())
    summary, _ = _read([row], script, cache_dir=tmp_path)
    assert summary.calls == 2
    for request in script.requests:
        blocks = request['content_blocks']
        images = [block for block in blocks if block['type'] == 'image_url']
        assert len(images) == 2
        decoded = [cv2.imdecode(np.frombuffer(base64.b64decode(
            block['image_url']['url'].split(',', 1)[1]), np.uint8), cv2.IMREAD_COLOR)
                   for block in images]
        assert all(im is not None for im in decoded)
        assert np.any(np.all(decoded[0] == [255, 0, 255], axis=2))
        assert not np.any(np.all(decoded[1] == [255, 0, 255], axis=2))
        assert 'SAME target' in blocks[0]['text']
        assert 'Native target extent' in blocks[0]['text']
        assert 'body estimate; leads may extend beyond it' in blocks[0]['text']
    evidence = row['_crop_image_evidence']
    assert max(evidence['native_target_size_px']) < 1024
    json.dumps(evidence)  # no ndarray/bytes in persisted metadata
    repeat = Script()
    summary, _ = _read([_row(lead_count=8, bbox_basis='body')], repeat, cache_dir=tmp_path)
    assert summary.readings_cached == 2 and not repeat.requests


def test_cache_includes_detail_pixels_even_when_context_stays_same(tmp_path, monkeypatch):
    from kicadgen.services import crop_observation_images as views
    original = views.prepare_observation_views
    _read([_row()], Script(_obs(), _obs()), cache_dir=tmp_path)

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        result['detail_bgr'][10:30, 10:30] = (255, 255, 255)
        return result

    monkeypatch.setattr(views, 'prepare_observation_views', changed)
    script = Script(_obs(), _obs())
    summary, _ = _read([_row()], script, cache_dir=tmp_path)
    assert summary.calls == 2 and summary.readings_cached == 0


def test_disputed_terminal_count_cannot_reenter_cad_through_legacy_fields():
    from kicadgen.cad3d.kicad_models import observed_terminal_count
    row = _row(presence_confidence='high', terminal_layout='two rows',
               terminal_count_basis='observed')
    answer = _obs(contacts_complete=False)
    _read([row], Script(answer, answer))
    assert row['_crop_terminal_review_required'] is True
    assert observed_terminal_count(row) is None
    manual = dict(row, pin_count=6, pin_count_confidence='high', pin_count_source='manual')
    assert observed_terminal_count(manual) == 6
    _read([row], Script(_obs(contacts_complete=True), _obs(contacts_complete=True)))
    assert '_crop_terminal_review_required' not in row
    assert observed_terminal_count(row) == 8
