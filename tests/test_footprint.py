"""Test di base per il generatore di footprint (kicadgen.core.footprint)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from kicadgen.core.footprint import Footprint, Pad
from kicadgen.core.sexpr import Symbol, bare


def test_pad_requires_drill_for_thru_hole():
    with pytest.raises(ValueError):
        Pad(number="1", pad_type="thru_hole", shape="circle", at=(0, 0), size=(1, 1), layers=["*.Cu"])


def test_pad_rejects_invalid_type():
    with pytest.raises(ValueError):
        Pad(number="1", pad_type="bogus", shape="circle", at=(0, 0), size=(1, 1), layers=["*.Cu"], drill=1.0)


def test_footprint_render_contains_bare_tokens_not_quoted():
    fp = Footprint(name="TEST_FP", attr="smd")
    fp.add_pad(Pad(
        number="1", pad_type="smd", shape="rect",
        at=(0, 0), size=(1.0, 1.0), layers=["F.Cu", "F.Paste", "F.Mask"],
    ))
    text = fp.render()

    assert '(attr smd)' in text
    assert '"smd"' not in text
    assert '(pad "1" smd rect' in text
    assert '(unlocked yes)' in text
    assert "'yes'" not in text


def test_footprint_quotes_real_strings():
    fp = Footprint(name="TEST_FP", description="una descrizione", tags="tag1 tag2")
    text = fp.render()
    assert '(descr "una descrizione")' in text
    assert '(tags "tag1 tag2")' in text
    assert '(footprint "TEST_FP"' in text


def test_bare_helper():
    assert bare(True) == Symbol("yes")
    assert bare(False) == Symbol("no")


def test_save_creates_directories(tmp_path):
    fp = Footprint(name="TEST_FP")
    out = tmp_path / "TEST_FP.pretty" / "TEST_FP.kicad_mod"
    fp.save(str(out))
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert content.startswith('(footprint "TEST_FP"')
