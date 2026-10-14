"""@brief Contact errors written by the FreeCAD script carry a stable code the user reads translated."""
import ast
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d" / "freecad_build_body.py"


def _lift(name):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    namespace = {}
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    exec(compile(ast.Module(body=body, type_ignores=[]), "<lift>", "exec"), namespace)
    return namespace[name]


def test_pad_not_wider_than_hole_carries_its_code():
    through = _lift("_through_features")
    with pytest.raises(ValueError) as info:
        through(0.0, 0.0, 0.5, 1.0, 1.6, 0.06, 0.025, False)
    assert info.value.args[1] == "pad_not_wider_than_hole"


def test_a_wider_pad_is_built():
    through = _lift("_through_features")
    tubes, drills = through(0.0, 0.0, 0.5, 1.4, 1.6, 0.06, 0.025, False)
    assert len(tubes) == 3 and drills == [(0.0, 0.0, 0.56)]


def test_every_coded_reason_has_its_own_translated_warning():
    """The contact loop maps each code to a literal template, never to the English text."""
    source = SCRIPT.read_text(encoding="utf-8")
    from kicadgen.services.i18n import DEFAULT_TRANSLATIONS
    for code, template in (
            ("pad_not_wider_than_hole", "contatto {get} non generato: piazzola non piu' larga del foro"),
            ("stadium_too_short", "contatto {get} non generato: piazzola allungata piu' corta "
                                  "della sua estremita' tonda")):
        assert f'"{code}"' in source
        entry = DEFAULT_TRANSLATIONS["ui:" + template]
        assert set(entry) == {"it", "en", "fr", "de"}
        assert "must exceed" not in entry["en"] and "stadium shorter" not in entry["en"]
