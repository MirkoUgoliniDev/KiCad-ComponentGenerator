"""@brief Contact label anchors follow the name FreeCAD gives the object (2026-11-03).

FreeCAD rewrites a requested object name that is not a valid identifier
("PinA-1" -> "PinA_1") or already taken ("Pin1" -> "Pin001"); the anchors
were stored under the requested string and read back by `obj.Name`, so every
such contact lost its label in the preview. Proved with freecadcmd 1.1 (see
the review report); here the contract is checked on the script source,
since the runner has no FreeCAD.
"""
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d"
          / "freecad_build_body.py").read_text(encoding="utf-8")


def test_anchors_are_keyed_on_the_object_freecad_returned():
    """@brief Positive: the key is the returned object's Name, the reader uses obj.Name too."""
    assert "label_anchors[pin_obj.Name] = " in SCRIPT
    assert "label_anchors.get(obj.Name)" in SCRIPT


def test_no_anchor_is_keyed_on_the_requested_name():
    """@brief Negative: no key built from the requested "Pin<number>" string remains."""
    assert 'label_anchors[f"Pin' not in SCRIPT
