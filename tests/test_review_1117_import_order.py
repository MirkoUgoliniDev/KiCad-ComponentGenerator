"""@brief Library parts are read in the document's creation order, whatever order the importer returns.

FreeCAD's `Import.insert` returns its (object, colours) pairs in a different
order on every call; the part index names the solids, so two builds of the
same payload wrote the same geometry in a different order (investigation of
2026-11-17 on a real project: STEP, WRL and preview differed between runs).
"""

import itertools
from types import SimpleNamespace

from test_freecad_heartbeat import parts  # noqa: F401 - fixture: freecad_parts with FreeCAD stubbed


def _objects(names):
    """Fake document objects with the given names."""
    return [SimpleNamespace(Name=name) for name in names]


def test_every_return_order_of_the_importer_gives_the_document_order(parts):  # noqa: F811
    """@brief Positive: the pairs come out in `doc.Objects` order for every permutation the
    importer may return, each object keeping its own colours."""
    objects = _objects(["Part", "Part001", "Part002", "Part003"])
    doc = SimpleNamespace(Objects=objects)
    pairs = [(obj, ["colour of " + obj.Name]) for obj in objects]
    for shuffled in itertools.permutations(pairs):
        ordered = parts._in_document_order(list(shuffled), doc)
        assert [obj.Name for obj, _colours in ordered] == ["Part", "Part001", "Part002", "Part003"]
        assert all(colours == ["colour of " + obj.Name] for obj, colours in ordered)


def test_objects_the_document_does_not_list_keep_their_place_after_the_listed_ones(parts):  # noqa: F811
    """@brief Negative: an object missing from the document is not dropped nor reordered among
    its like; a document without `Objects` (the importer's order is all there is) changes nothing."""
    listed = _objects(["A", "B"])
    stray = _objects(["X", "Y"])
    doc = SimpleNamespace(Objects=listed)
    pairs = [(stray[0], []), (listed[1], []), (stray[1], []), (listed[0], [])]
    assert [obj.Name for obj, _c in parts._in_document_order(pairs, doc)] == ["A", "B", "X", "Y"]
    bare = SimpleNamespace(Name="doc")
    assert parts._in_document_order(pairs, bare) == pairs
