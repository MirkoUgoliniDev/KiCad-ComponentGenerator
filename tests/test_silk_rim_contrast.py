"""@brief No silkscreen outline around the components of the 3D model.

User, 2026-09-27: «non mettere la serigrafia ingombro attorno ai componenti».
Until then a white rectangle was drawn around every dark approximated body so
that it would stand out from a dark board; it came from no document and no
photograph and read as real silkscreen. The board frame went the same way on
2026-09-26. The module's own pin labels (`silk_texts`) are footprint
silkscreen, requested by an explicit option, and stay.
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "kicadgen" / "cad3d" / "freecad_build_body.py"


def _tree():
    return ast.parse(SRC.read_text(encoding="utf-8"))


def test_the_outline_builders_are_gone():
    names = {n.name for n in _tree().body if isinstance(n, ast.FunctionDef)}
    assert not names & {"_rim", "_needs_component_rim", "_needs_silk_rim"}


def test_no_part_named_after_a_component_outline_is_added():
    """No `add(f"{base}_silk", ...)`: the only silkscreen objects are the pin labels."""
    source = SRC.read_text(encoding="utf-8")
    assert '_silk"' not in source and "_silk'" not in source
    assert 'f"Silk_{i}"' in source            # le sigle dei contatti restano
