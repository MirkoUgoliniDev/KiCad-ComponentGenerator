"""@brief Persist reviewed CAD choices without transferring them to a different photo/body."""
from copy import deepcopy
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import tempfile

from ..fsutil import replace_file

FILENAME = 'kicad-model-choices.json'


def component_key(component):
    """@brief Identify unchanged component geometry and package independently of row order.

    @param component Component dict with x_mm, y_mm, length_mm, width_mm,
        rotation_deg (rounded to 1e-6), package, kind, _detected_kind and
        bbox_basis.
    @return SHA-256 hex digest of those values.
    """
    values = {key: component.get(key) for key in (
        'x_mm', 'y_mm', 'length_mm', 'width_mm', 'rotation_deg', 'package', 'kind',
        '_detected_kind', 'bbox_basis')}
    for key in ('x_mm', 'y_mm', 'length_mm', 'width_mm', 'rotation_deg'):
        values[key] = round(float(values[key] or 0), 6)
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def save_choices(directory, photo_png, components):
    """@brief Atomically save candidate JSON and explicit choices scoped to one calibrated photo.

    @param directory Component output directory, not a global library.
    @param photo_png Original calibrated photo bytes; never copied to the JSON.
    @param components Detached component dictionaries with optional model_match/selection.
    @return Path of the saved audit document.
    @throws OSError If persistence fails; callers must inform the user.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = {'version': 1, 'photo_sha256': hashlib.sha256(photo_png).hexdigest(),
            'components': [{
                'key': component_key(c),
                'name': c.get('name', ''),
                # Keep the complete local CAD state in the audit JSON.  Only
                # explicit selections are restored automatically below; the
                # other fields remain evidence for the next replay.
                'model_selection': deepcopy(c.get('model_selection')),
                'model_match': deepcopy(c.get('model_match')),
                'model_path': c.get('model_path'),
                'model_rotation_deg': c.get('model_rotation_deg'),
                'render_3d': c.get('render_3d', True),
                'rotation_deg': c.get('rotation_deg'),
                # The user's direct choices on the solid, distinct from
                # render_3d/model_rotation_deg which above are the computed
                # outcome: only these are reapplied on re-entry.
                'render_3d_override': c.get('render_3d_override'),
                'model_rotation_delta_deg': c.get('model_rotation_delta_deg') or 0,
            } for c in components]}
    target = directory / FILENAME
    fd, temporary = tempfile.mkstemp(prefix='.model-choices-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, indent=2, ensure_ascii=False, allow_nan=False)
        replace_file(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def restore_choices(directory, photo_png, components):
    """@brief Restore only explicit choices for identical photos and unchanged component geometry.

    @param directory Component output directory holding FILENAME.
    @param photo_png Calibrated photo bytes; choices are restored only when
        their SHA-256 matches the saved one.
    @param components Current component dictionaries (never modified).
    @return Independent component list; missing state is normal, corrupt state raises ValueError.
    @throws ValueError When the choice file has an unsupported version or
        its component records are not a list.
    @note Restored: an explicit confirmed/rejected model_selection (with the
        model_match report saved alongside it), the manual exclusion and the
        manual rotation delta. An automatic model_match without an explicit
        selection stays in the audit JSON only.
    @note AI responses never authorize arbitrary model paths; only this local choice store does.
    """
    result = deepcopy(components)
    target = Path(directory) / FILENAME
    if not target.is_file() or not photo_png:
        return result
    data = json.loads(target.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('Unsupported KiCad model choice document')
    if data.get('photo_sha256') != hashlib.sha256(photo_png).hexdigest():
        return result
    records = data.get('components', [])
    if not isinstance(records, list):
        raise ValueError('Invalid KiCad model choice records')
    valid = [r for r in records if isinstance(r, dict) and isinstance(r.get('key'), str)]
    recorded_counts = Counter(r['key'] for r in valid)
    current_counts = Counter(component_key(c) for c in result)
    # Duplicate geometry has no unique identity: do not propagate one row's choice.
    choices = {r['key']: r for r in valid
               if recorded_counts[r['key']] == 1 and current_counts[r['key']] == 1}
    for component in result:
        record = choices.get(component_key(component), {})
        selection = record.get('model_selection')
        explicit = isinstance(selection, dict) and selection.get('status') in ('confirmed', 'rejected')
        if explicit:
            component['model_selection'] = deepcopy(selection)
        # The match report travels only with the explicit decision it
        # documents: an automatic report is recomputed, never replayed, so a
        # stale automatic match cannot pose as a user choice.
        if explicit and isinstance(record.get('model_match'), dict):
            component['model_match'] = deepcopy(record['model_match'])
        # Manual exclusion and rotation: they are user choices like the
        # model selection, so they must survive regeneration exactly like
        # it. A missing value or one of the wrong type overwrites nothing:
        # the automatic behaviour comes back.
        if isinstance(record.get('render_3d_override'), bool):
            component['render_3d_override'] = record['render_3d_override']
        delta = record.get('model_rotation_delta_deg')
        if isinstance(delta, (int, float)) and not isinstance(delta, bool):
            component['model_rotation_delta_deg'] = float(delta)
    return result


def save_candidate_report(directory, components):
    """@brief Save the bounded candidate lists supplied to the CAD builder as inspectable JSON.

    @param directory Component output directory (created when missing).
    @param components Component dictionaries with their candidate lists.
    @return Path of the report, preserving both uncertainty and explicit user selections.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'kicad-model-candidates.json'
    data = {'version': 1, 'components': [
        {
            'name': c.get('name'),
            'source_row': c.get('source_row'),
            'x': c.get('x'),
            'y': c.get('y'),
            'length_mm': c.get('length_mm'),
            'width_mm': c.get('width_mm'),
            'rotation_deg': c.get('rotation_deg'),
            'package': c.get('package'),
            'model_match': c.get('model_match'),
            'model_selection': c.get('model_selection'),
            'model_path': c.get('model_path'),
            'model_rotation_deg': c.get('model_rotation_deg'),
            'render_3d': c.get('render_3d', True),
        } for c in components]}
    fd, temporary = tempfile.mkstemp(prefix='.model-candidates-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, indent=2, ensure_ascii=False, allow_nan=False)
        replace_file(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target
