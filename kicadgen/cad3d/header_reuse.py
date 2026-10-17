"""@brief Reuse the last successful model choices when only optional headers change."""

from dataclasses import asdict, is_dataclass
from copy import deepcopy
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile

from ..fsutil import replace_file

REUSE_PROGRESS = 'KICADGEN_HEADER_REUSE'


def _default(value):
    if isinstance(value, (bytes, bytearray)):
        # OpenCV crop descriptors are transient binary matching evidence in
        # ComponentBox, even though the CAD payload already encodes them.
        return {'__binary_base64__': base64.b64encode(value).decode('ascii')}
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(type(value).__name__)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                     default=_default).encode('utf-8')).hexdigest()


def _stamp(path):
    info = Path(path).stat()
    return [info.st_size, info.st_mtime_ns]


class HeaderModelReuse:
    """@brief A project-local snapshot, valid only for a headers-only change.

    Normal regeneration still searches the libraries. A changed body, contact,
    annotation, model file or generated output invalidates the snapshot.
    Missing/old snapshots and cache failures always use the normal pipeline.
    """

    def __init__(self, directory, params, body, context):
        self.path = Path(directory) / '.header-model-reuse.json'
        self.show = bool(body.show_pins_3d)
        self.key = None
        #: Model-resolution messages of the run that saved the snapshot, set
        #: by a successful `load` (empty for a snapshot written without them).
        self.notes = []
        try:
            physical = asdict(body)
            physical.pop('show_pins_3d', None)
            self.inputs = json.loads(json.dumps([type(params).__name__, params, physical, context],
                                               default=_default, allow_nan=False))
            self.key = _digest(self.inputs)
        except (ValueError, TypeError, OverflowError):
            pass

    def load(self):
        """@brief Return copied resolved components, or None when reuse is not proven.

        @return The list of component dictionaries saved for the same inputs,
            or None when the file is missing, malformed, built for another
            key or `show` flag, or any referenced file changed since.
        """
        if self.key is None:
            return None
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if (saved['version'] != 1 or self.key not in saved['keys']
                    or not isinstance(saved['show'], bool) or saved['show'] == self.show):
                return None
            components = saved['components']
            if not isinstance(components, list) or not all(isinstance(c, dict) for c in components):
                return None
            if saved['digest'] != _digest(components) or not saved['files']:
                return None
            if any(_stamp(path) != stamp for path, stamp in saved['files'].items()):
                return None
            notes = saved.get('notes', [])
            self.notes = ([str(note) for note in notes if isinstance(note, str)]
                          if isinstance(notes, list) else [])
            return components
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return None

    def _feedback_key(self, components):
        """Accept the exact metadata copied back by MainWindow._show_generated_mesh.

        The result report updates model_match and rotations in the editor.
        These are proven output values, not an unrelated user edit; all other
        data, including explicit model selections and rotation deltas, stay keyed.
        """
        inputs = deepcopy(self.inputs)
        for owner in inputs[1:3]:
            current = owner.get('components') or []
            for fallback, record in enumerate(components):
                index = record.get('source_row', fallback)
                if type(index) is not int or not 0 <= index < len(current):
                    continue
                component = current[index]
                if any(record.get(a) != component.get(b) for a, b in (
                        ('name', 'name'), ('package', 'package'),
                        ('length_mm', 'length'), ('width_mm', 'width'))):
                    continue
                component['model_match'] = record.get('model_match')
                for source, target in (('model_rotation_deg', 'model_rotation_deg'),
                                       ('rotation_deg', 'rotation')):
                    if source in record:
                        component[target] = (float(record[source]) if target == 'rotation'
                                             else record[source])
        return _digest(inputs)

    def save(self, components, result, notes=None):
        """@brief Publish a snapshot only after a successful STEP exists; best effort.

        @param components Resolved component payloads to reuse.
        @param result Generator result; `step_path` must name the built STEP.
        @param notes Model-resolution messages of this run, replayed by the
            caller when the snapshot is reused (see `self.notes`); optional, a
            snapshot without them reloads with no messages.
        """
        if self.key is None or not result.get('step_path'):
            return
        temporary = None
        try:
            step = Path(result['step_path']).resolve()
            files = {str(step): _stamp(step)}
            for key in ('mesh_path', 'wrl_path', 'fcstd_path'):
                if result.get(key):
                    path = Path(result[key]).resolve()
                    files[str(path)] = _stamp(path)
            for component in components:
                if component.get('model_path'):
                    path = Path(component['model_path']).resolve()
                    files[str(path)] = _stamp(path)
            saved = dict(version=1, keys=[self.key, self._feedback_key(components)],
                         show=self.show, components=components,
                         digest=_digest(components), files=files,
                         notes=[str(note) for note in (notes or [])])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.header-reuse-', dir=self.path.parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(saved, stream, allow_nan=False, default=_default)
            replace_file(temporary, self.path)
        except (OSError, ValueError, TypeError, OverflowError):
            pass
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass
