"""@brief Shared photo-audit helpers: normalized boxes, standard JSON and retention.

Offline: no model call, no network. Audits are written under tmp_path only.
"""
import json
import os
import re

import pytest

from kicadgen.services import photo_audit, photo_yolo
from kicadgen.services.photo_audit import (AUDIT_RETENTION_PER_KIND, audit_safe, prune_audits,
                                           stage_audit_pattern, valid_fraction_box,
                                           write_stage_audit)


# ── valid_fraction_box (moved from the removed photo_yolo_review) ─────────────

def test_a_fraction_box_inside_the_image_is_accepted():
    assert valid_fraction_box((0, .1, .5, 1)) == [0, .1, .5, 1]


@pytest.mark.parametrize('box', [None, [0, 0, 1], [0, 0, 1.1, 1], [False, 0, .9, 1],
                                 [0, 0, float('nan'), 1], [0, 0, 10**400, 1],
                                 [.8, .1, .2, .9], [.1, .5, .2, .5]])
def test_a_missing_outside_or_empty_box_is_rejected(box):
    with pytest.raises(ValueError):
        valid_fraction_box(box)


def test_non_finite_numbers_become_explicit_in_standard_json():
    record = audit_safe({'a': [1.0, float('nan')], 'b': (float('inf'),), 'c': 'x'})
    assert record == {'a': [1.0, {'invalid_number': 'nan'}], 'b': [{'invalid_number': 'inf'}],
                      'c': 'x'}
    json.dumps(record, allow_nan=False)


# ── retention ──────────────────────────────────────────────────────────────

def _aged(path, age_s):
    """Give a file a distinct modification time, older by `age_s` seconds."""
    stamp = 1_700_000_000 - age_s
    os.utime(path, (stamp, stamp))


def test_writing_an_audit_keeps_only_the_newest_of_its_kind(tmp_path):
    """@brief Positivo: oltre il limite si cancellano le piu' vecchie dello stesso tipo."""
    folder = tmp_path/'yolo-audit'
    folder.mkdir()
    old = []
    for age in range(5):
        path = folder/f'global-inventory-{age:032x}.json'
        path.write_text('{}', encoding='utf-8')
        _aged(path, 100 + age)
        old.append(path)
    logs = []
    written = write_stage_audit(tmp_path, 'global-inventory', {'x': 1}, logs.append, keep=3)
    remaining = sorted(p.name for p in folder.glob('global-inventory-*.json'))
    # The new audit plus the two most recent old ones (ages 100 and 101).
    assert written.exists() and len(remaining) == 3
    assert old[0].exists() and old[1].exists()
    assert not any(p.exists() for p in old[2:])
    assert any(str(written) in str(line) for line in logs)


def test_pruning_never_touches_other_kinds_or_the_file_just_written(tmp_path):
    """@brief Negativo: altri tipi e il file appena scritto restano, anche se piu' vecchio."""
    folder = tmp_path/'yolo-audit'
    folder.mkdir()
    other = folder/'yolo-abc123.json'
    other.write_text('{}', encoding='utf-8')
    _aged(other, 10_000)
    unrelated = folder/'global-inventory-notes.json'   # not an audit file name
    unrelated.write_text('{}', encoding='utf-8')
    _aged(unrelated, 10_000)
    newer = []
    for age in range(3):
        path = folder/f'global-inventory-{age + 1:032x}.json'
        path.write_text('{}', encoding='utf-8')
        _aged(path, age)
        newer.append(path)
    just_written = folder/f'global-inventory-{0:032x}.json'
    just_written.write_text('{}', encoding='utf-8')
    _aged(just_written, 50_000)   # clock skew: it looks like the oldest
    removed = prune_audits(folder, stage_audit_pattern('global-inventory'), keep=2,
                           protect=just_written)
    assert just_written.exists() and other.exists() and unrelated.exists()
    assert newer[0].exists() and newer[1].exists() and not newer[2].exists()
    assert removed == 1


def test_the_local_detector_audit_is_pruned_the_same_way(tmp_path):
    """@brief Anche gli audit yolo-* del rilevatore locale hanno un limite."""
    for index in range(4):
        path = tmp_path/f'yolo-old{index}.json'
        path.write_text('{}', encoding='utf-8')
        _aged(path, 100 + index)
    keeper = tmp_path/'global-inventory-{}.json'.format('f' * 32)
    keeper.write_text('{}', encoding='utf-8')
    _aged(keeper, 99_999)
    written = photo_yolo._write_audit(tmp_path, {'x': 1}, keep=2)
    names = sorted(p.name for p in tmp_path.glob('yolo-*.json'))
    assert os.path.basename(written) in names and len(names) == 2
    assert (tmp_path/'yolo-old0.json').exists() and keeper.exists()


def test_the_retention_policy_is_declared_and_positive():
    assert isinstance(AUDIT_RETENTION_PER_KIND, int) and AUDIT_RETENTION_PER_KIND >= 1
    assert stage_audit_pattern('global-inventory').fullmatch('global-inventory-' + 'a' * 32 + '.json')
    assert not stage_audit_pattern('global-inventory').fullmatch('global-inventory-x.json')
    assert re.escape('a.b') in stage_audit_pattern('a.b').pattern


def test_an_unwritable_audit_is_logged_not_raised(tmp_path):
    target = tmp_path/'file'
    target.write_text('not a folder', encoding='utf-8')
    logs = []
    assert write_stage_audit(target, 'global-inventory', {}, logs.append) is None
    assert logs and photo_audit.tx("Log revisione YOLO non scrivibile: {exc}", exc='')[:10] \
        in str(logs[0])
