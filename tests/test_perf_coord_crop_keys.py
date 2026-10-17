"""@brief Exactness of the memo keys added by the 2026-10-17 typing-speed work.

Two memos answer from a key instead of recomputing; each is only correct if
its key covers every input:

- the window's per-body crop descriptor is keyed on
  `component_image_match.CROP_GEOMETRY_KEYS`: the list must be exactly the
  row keys `extract_component_crop` reads;
- `workers.accepts_keyword` is memoised on the function: the memo must give
  the answer `inspect.signature` gives, bound methods included.
"""
import inspect

import numpy as np
import pytest

from kicadgen.services import component_image_match as cim


class _Recording(dict):
    """A row that records which keys are read."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)


@pytest.mark.parametrize("rotation", [None, 0.0, 30.0])
def test_the_crop_reads_exactly_the_declared_keys(rotation):
    image = np.full((200, 300, 3), 90, dtype=np.uint8)
    row = _Recording(x_mm=1.0, y_mm=-2.0, length_mm=4.0, width_mm=3.0, height_mm=1.2,
                     label="U1", category="ic")
    if rotation is not None:
        row["rotation_deg"] = rotation
    cim.extract_component_crop(image, row, 30.0, 20.0)
    assert row.read == set(cim.CROP_GEOMETRY_KEYS)


def test_a_key_outside_the_list_does_not_change_the_crop():
    """@brief Negative: what the memo ignores (height, label) truly does not enter the crop."""
    image = np.random.default_rng(1).integers(0, 255, (200, 300, 3), dtype=np.uint8)
    base = dict(x_mm=1.0, y_mm=-2.0, length_mm=4.0, width_mm=3.0, rotation_deg=15.0)
    first = cim.extract_component_crop(image, dict(base, height_mm=1.0, label="A"), 30.0, 20.0)
    second = cim.extract_component_crop(image, dict(base, height_mm=9.0, label="B"), 30.0, 20.0)
    moved = cim.extract_component_crop(image, dict(base, x_mm=2.0), 30.0, 20.0)
    assert np.array_equal(first, second)
    assert not np.array_equal(first, moved)


def test_the_window_keys_its_memo_on_the_service_list():
    pytest.importorskip("PyQt6")
    from kicadgen.gui.main_window import MainWindow
    assert MainWindow.CROP_GEOMETRY_KEYS is cim.CROP_GEOMETRY_KEYS


# ── accepts_keyword memo ─────────────────────────────────────────────

def _reference(function, name):
    """The answer before the memo (2026-10-16), straight from inspect.signature."""
    try:
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        return False
    named = [p for p in parameters if p.kind not in (
        inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
    for parameter in parameters:
        if parameter.kind is inspect.Parameter.VAR_KEYWORD and named:
            return True
        if parameter.name == name and parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            return True
    return False


class _Owner:
    def plain(self, a, cancel=None):
        return a

    def only_self_and_kwargs(self, **kwargs):
        return kwargs

    def self_named_cancel(cancel, x):  # noqa: N805 - first parameter IS the instance
        return x

    @classmethod
    def klass(cls, **kwargs):
        return kwargs

    @staticmethod
    def static(cancel):
        return cancel


def _callables():
    owner = _Owner()
    return [
        lambda: None, lambda cancel=None: cancel, lambda *a, **kw: None,
        lambda a, **kw: None, lambda **kw: None, lambda *, cancel: cancel,
        owner.plain, owner.only_self_and_kwargs, owner.self_named_cancel, owner.klass,
        owner.static, _Owner.plain, len, print, np.zeros,
    ]


@pytest.mark.parametrize("name", ["cancel", "a", "x"])
def test_the_memo_gives_the_signature_answer(name):
    from kicadgen.gui.workers import accepts_keyword
    for function in _callables():
        for _ in range(2):  # second call served by the memo
            assert accepts_keyword(function, name) == _reference(function, name), (function, name)


def test_a_replaced_function_is_answered_for_itself():
    """@brief Negative: a monkeypatched method (a new function) is never answered from the old one."""
    from kicadgen.gui.workers import accepts_keyword
    owner = _Owner()
    assert accepts_keyword(owner.plain, "cancel")
    owner.plain = lambda a: a
    assert not accepts_keyword(owner.plain, "cancel")


def test_the_memo_keeps_no_instance_alive():
    import gc
    import weakref
    from kicadgen.gui.workers import accepts_keyword
    owner = _Owner()
    ref = weakref.ref(owner)
    accepts_keyword(owner.plain, "cancel")
    del owner
    gc.collect()
    assert ref() is None
