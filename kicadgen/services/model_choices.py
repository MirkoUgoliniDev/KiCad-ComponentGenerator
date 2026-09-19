"""@brief Persist reviewed CAD choices without transferring them to a different photo/body."""
from copy import deepcopy
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import tempfile

FILENAME = 'kicad-model-choices.json'


def component_key(component):
    """@brief Identify unchanged component geometry and package independently of row order."""
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
            'components': [{'key': component_key(c), 'name': c.get('name', ''),
                            'model_selection': deepcopy(c.get('model_selection')),
                            'model_match': deepcopy(c.get('model_match'))} for c in components]}
    target = directory / FILENAME
    fd, temporary = tempfile.mkstemp(prefix='.model-choices-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, indent=2, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def restore_choices(directory, photo_png, components):
    """@brief Restore only explicit choices for identical photos and unchanged component geometry.

    @return Independent component list; missing state is normal, corrupt state raises ValueError.
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
        if isinstance(selection, dict) and selection.get('status') in ('confirmed', 'rejected'):
            component['model_selection'] = deepcopy(selection)
        if isinstance(record.get('model_match'), dict):
            component['model_match'] = deepcopy(record['model_match'])
    return result


def save_candidate_report(directory, components):
    """@brief Save the bounded candidate lists supplied to the CAD builder as inspectable JSON.

    @return Path of the report, preserving both uncertainty and explicit user selections.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'kicad-model-candidates.json'
    data = {'version': 1, 'components': [
        {'name': c.get('name'), 'source_row': c.get('source_row'), 'x': c.get('x'), 'y': c.get('y'),
         'length_mm': c.get('length_mm'), 'width_mm': c.get('width_mm'),
         'package': c.get('package'), 'model_match': c.get('model_match'),
         'model_selection': c.get('model_selection')} for c in components]}
    fd, temporary = tempfile.mkstemp(prefix='.model-candidates-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, indent=2, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target
