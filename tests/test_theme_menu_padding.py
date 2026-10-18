"""@brief Context menu text must not run into the menu's right border (user, 2026-10-18).

Once `QMenu::item:selected` is styled, Qt's style sheet lays every item out
with its own box; without an explicit item padding the text of the longest
entry («Elimina disegno», delete drawing) touched the border and looked cut.
The measure runs on rendered pixels in a child process (`menu_padding_probe.py`):
positive with `APP_QSS`, negative with the same sheet stripped of the item
padding (the old theme), so the measure is shown to tell the two apart.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).with_name("menu_padding_probe.py")


@pytest.fixture(scope="module")
def gaps():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, str(PROBE)], capture_output=True, text=True,
                         env=env, timeout=120, check=True).stdout.strip()
    if out == "unavailable":
        pytest.skip("the native Windows 11 style is not available on this platform")
    match = re.fullmatch(r"new=(-?\d+) old=(-?\d+)", out.splitlines()[-1])
    assert match, out
    return int(match.group(1)), int(match.group(2))


def test_menu_text_keeps_a_right_margin(gaps):
    assert gaps[0] >= 12


def test_without_the_item_padding_the_text_reaches_the_border(gaps):
    assert gaps[1] < 6
