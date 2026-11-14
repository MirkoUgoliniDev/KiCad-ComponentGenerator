"""@brief One glyph size for every 3D pin label, each one fitting (user, 2026-11-14).

«uniformiamo la dimensione delle scritte, badando che ci stiano»: on the ESP32
module each label was shrunk alone, so the long names (alternatives in
brackets) drew at 0.13 mm next to 0.65 mm short ones. Positive and negative
cases differ by the room the geometry gives, never by a name.
"""
import numpy as np
import pytest

pytest.importorskip("pyvista")

from kicadgen.gui.preview_3d_vtk import (  # noqa: E402
    _LABEL_MAX_LINES, _ascii_label, _balanced_lines, _label_tokens, _nearest_anchor_gap,
    _pin_label_geometry, _pin_label_layout, _pin_text_mesh, _text_width)

LONG = "40 (GPIO12 (ADC2_1/FSPICLK/FSPIIO6/RTC/SUBSPICLK/TOUCH12))"


def _two_rows(count, pitch, across):
    """Anchors of a two-row module: `count` pins per row, rows `across` mm apart.

    The rows must be longer than the distance between them, or the label frame
    reads the other axis as «across» (review 2026-11-14, 1): asserted here.
    """
    xs = np.arange(count) * pitch
    top = np.column_stack([xs, np.full(count, across / 2), np.zeros(count)])
    bottom = np.column_stack([xs, np.full(count, -across / 2), np.zeros(count)])
    points = np.vstack([top, bottom])
    assert _pin_label_geometry(points)[0], "rows along X: the test frame is the module's"
    return points


def _drawn_width(text, scale):
    return float(np.ptp(text.points[:, 0])) * scale


def _drawn_height(text, scale):
    return float(np.ptp(text.points[:, 1])) * scale


def test_a_long_name_shrinks_every_label_together_and_every_label_fits():
    points = _two_rows(22, 2.54, 17.5)
    labels = ["1 (GND)", LONG] + [f"{n} (GPIO{n})" for n in range(3, 45)]
    layout = _pin_label_layout(points, labels)
    # One scale for all: no label is drawn larger or smaller than another.
    assert layout["fraction"] < 1.0
    for text in layout["texts"]:
        assert _drawn_width(text, layout["scale"]) <= layout["max_width"] + 1e-6
        assert _drawn_height(text, layout["scale"]) <= layout["room"] + 1e-6
    # The long name wraps: a single line would need a much smaller scale.
    assert 1 < len(layout["blocks"][1]) <= _LABEL_MAX_LINES
    assert "".join(layout["blocks"][1]).replace(" ", "") == LONG.replace(" ", "")
    # Short names stay on one line.
    assert layout["blocks"][0] == ["1 (GND)"] or layout["blocks"][0] == ("1 (GND)",)


def test_short_names_keep_the_nominal_size():
    """@brief Negative: nothing too long -> no shrink, no wrap (the size before the change)."""
    points = _two_rows(8, 5.08, 20.0)
    layout = _pin_label_layout(points, ["1 (+V0)", "2 (-V0)", "3 (AC)", "4 (AC)"]
                               + [f"{n} (NC)" for n in range(5, 17)])
    assert layout["fraction"] == pytest.approx(1.0)
    assert all(len(block) == 1 for block in layout["blocks"])


def test_a_tight_pitch_forbids_the_wrap_and_shrinks_instead():
    """@brief The room between neighbouring pins decides the lines, not the name.

    Same long name, same room across the board: with a wide pitch it wraps onto
    more lines and keeps a larger size; with a tight pitch each extra line costs
    height, so it uses fewer lines and every label is smaller.
    """
    wide_points, tight_points = _two_rows(10, 6.0, 17.5), _two_rows(30, 0.9, 17.5)
    assert _pin_label_geometry(wide_points)[5] == pytest.approx(_pin_label_geometry(tight_points)[5])
    wide = _pin_label_layout(wide_points, [LONG] + [str(n) for n in range(2, 21)])
    tight = _pin_label_layout(tight_points, [LONG] + [str(n) for n in range(2, 61)])
    assert len(wide["blocks"][0]) > 1
    for layout in (wide, tight):
        for text in layout["texts"]:
            assert _drawn_width(text, layout["scale"]) <= layout["max_width"] + 1e-6
            assert _drawn_height(text, layout["scale"]) <= layout["room"] + 1e-6
    assert len(tight["blocks"][0]) < len(wide["blocks"][0])
    assert tight["fraction"] < wide["fraction"]


def test_the_width_model_matches_the_drawn_text():
    import pyvista as pv
    for line in ["1 (GND)", "GPIO12 (ADC2_1/", "FSPICLK/FSPIIO6/RTC/SUBSPICLK/TOUCH12))", "+V0", "A",
                 "5V0 (é)"]:
        drawn = float(np.ptp(pv.Text3D(line, depth=0).points[:, 0]))
        assert _text_width(line) == pytest.approx(drawn, rel=1e-6, abs=1e-9), line


def test_balanced_lines_are_the_narrowest_split():
    """@brief Against every possible 3-line split: none has a narrower widest line."""
    from itertools import combinations
    pieces = _label_tokens(LONG)
    widest, lines = _balanced_lines(pieces, 3, _text_width)
    assert len(lines) == 3 and widest == pytest.approx(max(_text_width(line) for line in lines))
    for a, b in combinations(range(1, len(pieces)), 2):
        split = ["".join(pieces[:a]).strip(), "".join(pieces[a:b]).strip(), "".join(pieces[b:]).strip()]
        assert widest <= max(_text_width(line) for line in split) + 1e-9


def test_tokens_break_after_slashes_and_before_spaces():
    assert _label_tokens("3 (GPIO19 (A/B))") == ["3", " (GPIO19", " (A/", "B))"]
    assert _label_tokens("GND") == ["GND"]


def test_coincident_anchors_do_not_erase_every_label():
    """@brief Review 3: the same pin read twice at one spot gave room 0 and scale 0."""
    points = np.array([[0, 0, 0], [0, 0, 0], [5, 5, 0]], float)
    assert _nearest_anchor_gap(points) == pytest.approx(np.hypot(5, 5))
    layout = _pin_label_layout(points, ["1", "1", "2"])
    assert layout["fraction"] > 0


def test_a_line_break_inside_a_name_is_a_space_and_the_block_fits():
    """@brief Review 4: a «\\n» drew lines the height model did not count."""
    points = _two_rows(20, 1.27, 17.5)
    labels = ["1 (A\nB\nC)"] + [str(n) for n in range(2, 41)]
    layout = _pin_label_layout(points, labels)
    assert layout["blocks"][0] in (["1 (A B C)"], ("1 (A B C)",))
    for text in layout["texts"]:
        assert _drawn_height(text, layout["scale"]) <= layout["room"] + 1e-6


def test_symbols_the_font_cannot_draw_are_spelled_and_never_crash():
    """@brief Review 7: «IN−» lost its minus, a name of only such symbols crashed the view."""
    assert _ascii_label("IN−") == "IN-"
    assert _ascii_label("R 10Ω") == "R 10Ohm"
    assert _ascii_label("Vé") == "Ve"
    points = _two_rows(6, 2.54, 10.0)
    mesh = _pin_text_mesh(points, ["中", "IN−"] + [str(n) for n in range(3, 13)])
    assert mesh.n_points > 0


def test_a_block_is_flush_on_the_side_of_its_own_strip():
    """@brief Review 6: the lines of a wrapped name line up on the side of their pin, on both rows."""
    from kicadgen.gui.preview_3d_vtk import _block_mesh, _line_advance
    # Capitals only: no glyph of one line reaches into the band of the other
    # («/» and brackets of a second line rise above half the line advance).
    lines = ("ABCDEFGH", "ABC")
    for right in (False, True):
        block = _block_mesh(lines, right=right).points
        second = block[block[:, 1] < -_line_advance() / 2]
        first = block[block[:, 1] >= -_line_advance() / 2]
        if right:
            assert second[:, 0].max() == pytest.approx(block[:, 0].max())
            assert second[:, 0].min() > first[:, 0].min()
        else:
            assert second[:, 0].min() == pytest.approx(block[:, 0].min())
            assert second[:, 0].max() < first[:, 0].max()
