"""@brief A row of lands on a straight edge is the presence of contacts the outline cannot see.

ESP32 devkit, 2026-10-08: shallow half-holes whose plated barrel hides the
background showed no notch, the edge was «clean» and 22 contacts per edge
were dropped all day. The copper along the edge is a second measurement:
identical lands, periodic at the declared pitch, in a consecutive chain.
Positive and negative cases differ in STRUCTURE (a row against scattered
features of unequal width; a chain long enough against one too short).
"""
import cv2
import numpy as np
import pytest

from kicadgen.services.edge_lands import MIN_ROW_LANDS, count_edge_copper

SCALE = 30                      # px per mm, isotropic
LENGTH, WIDTH = 40.0, 16.0      # board mm
BOARD, GOLD, HOLE, LABEL = (28, 28, 28), (60, 170, 215), (90, 90, 90), (235, 235, 235)


def _px(mm):
    return int(round(mm * SCALE))


def board(lands=(), features=(), pitch=2.54, land_w=1.8, land_in=2.6, hole_r=0.5):
    """A straight-edged black board; `lands` = indices of top-edge contacts at `pitch`,
    `features` = (x_mm, w_mm, h_mm, colour) things touching the top edge."""
    image = np.full((_px(WIDTH), _px(LENGTH), 3), BOARD, np.uint8)
    for k in lands:
        cx = _px(5.0 + k * pitch)
        cv2.rectangle(image, (cx - _px(land_w / 2), 0), (cx + _px(land_w / 2), _px(land_in)), GOLD, -1)
        cv2.circle(image, (cx, _px(1.6)), _px(hole_r), HOLE, -1)
    for x, w, h, colour in features:
        cv2.rectangle(image, (_px(x), 0), (_px(x + w), _px(h)), colour, -1)
    return image


def test_a_row_of_identical_lands_on_the_edge_is_counted_at_the_pitch():
    """@brief Positivo: 22 piazzole uguali al passo, bordo dritto -> fila trovata."""
    row = count_edge_copper(board(lands=range(12)), "top", 2.54, LENGTH, WIDTH)
    assert row.found == 12 and len(row.centres_mm) == 12
    spacing = np.diff(row.centres_mm)
    assert spacing == pytest.approx([2.54] * 11, abs=0.15)


def test_scattered_features_of_unequal_width_on_the_edge_are_not_a_row():
    """@brief Negativo: etichette e corpi di larghezze diverse che toccano il bordo (RS485)."""
    features = [(3.0, 1.2, 0.6, LABEL), (7.1, 0.5, 1.5, LABEL), (12.0, 2.3, 1.0, LABEL),
                (16.3, 0.9, 0.4, LABEL), (22.0, 1.6, 2.0, HOLE), (27.7, 0.7, 1.1, LABEL),
                (31.0, 2.9, 0.8, LABEL)]
    row = count_edge_copper(board(features=features), "top", 2.54, LENGTH, WIDTH)
    assert row.found == 0


def test_a_chain_shorter_than_the_minimum_is_unverifiable_not_absent():
    """@brief Contratto cambiato (revisione 2026-11-03): una fila corta non e' assenza.

    Prima il test fissava `found == 0`, e l'estrattore cancellava un lato
    dichiarato di 4 contatti. Meno di MIN_ROW_LANDS piazzole non CONFERMANO la
    fila, ma neppure la escludono: il risultato e' None con un motivo.
    """
    row = count_edge_copper(board(lands=range(MIN_ROW_LANDS - 1)), "top", 2.54, LENGTH, WIDTH)
    assert row.found is None and row.reason
    assert len(row.centres_mm) == MIN_ROW_LANDS - 1


def test_a_short_declared_row_on_a_bare_edge_is_unverifiable_but_a_long_one_is_absent():
    """@brief Bordo nudo: assenza provabile solo se la fila dichiarata poteva essere confermata.

    Positivo e negativo differiscono per struttura: una fila dichiarata di
    MIN_ROW_LANDS contatti avrebbe formato una catena confermabile, quindi il
    bordo nudo la esclude (0); una di MIN_ROW_LANDS - 1 non avrebbe mai potuto
    farlo, quindi il bordo nudo non dice nulla (None).
    """
    image = board()
    assert count_edge_copper(image, "top", 2.54, LENGTH, WIDTH,
                             declared_count=MIN_ROW_LANDS).found == 0
    short = count_edge_copper(image, "top", 2.54, LENGTH, WIDTH, declared_count=MIN_ROW_LANDS - 1)
    assert short.found is None and short.reason


def test_a_row_is_found_whichever_colour_the_mask_is():
    """@brief Stessa fila su maschera verde: nessun colore e' assunto."""
    image = board(lands=range(10))
    image[np.all(image == BOARD, axis=2)] = (60, 110, 40)
    assert count_edge_copper(image, "top", 2.54, LENGTH, WIDTH).found == 10


def test_an_edge_without_a_pitch_or_without_a_photo_says_so():
    assert count_edge_copper(board(), "top", None, LENGTH, WIDTH).found is None
    assert count_edge_copper(None, "top", 2.54, LENGTH, WIDTH).found is None


def test_junk_of_one_other_width_outnumbering_the_lands_does_not_hide_the_row():
    """@brief Positivo (revisione G): 13 segni di 0.6 mm accanto a 8 piazzole -> la fila resta."""
    marks = [(1.0 + k * 1.05, 0.6, 0.8, LABEL) for k in range(13)]
    # The marks start past the last land (5 + 7 x 2.54 + 0.9 = 23.7 mm): two families, apart.
    image = board(lands=range(8), features=[(x + 24.5, w, h, c) for x, w, h, c in marks])
    row = count_edge_copper(image, "top", 2.54, LENGTH, WIDTH)
    assert row.found == 8


def test_identical_marks_at_another_pitch_are_not_a_row_at_the_declared_one():
    """@brief Negativo (revisione G): 7 segni uguali a 3.0 mm, passo dichiarato 2.8 -> nessuna fila."""
    marks = [(3.0 + k * 3.0, 0.8, 1.0, LABEL) for k in range(7)]
    # CONTRACT CHANGED 2026-11-03 (generality audit): three of the marks fall
    # on the 2.8 mm lattice within its tolerance, a short regular chain; such
    # a chain is UNVERIFIABLE whatever the declared length (a declared row of
    # 6 with two lands under a label looks the same), never a confirmed row
    # and never a proved absence. The point of this negative stands: marks at
    # another pitch are not a row of 7 at the declared one.
    row = count_edge_copper(board(features=marks), "top", 2.8, LENGTH, WIDTH, declared_count=7)
    assert row.found is None and len(row.centres_mm) < 7
    assert count_edge_copper(board(features=marks), "top", 3.0, LENGTH, WIDTH).found == 7


def test_the_row_survives_a_scale_error_of_the_calibration():
    """@brief Positivo: passo reale 2.49 su dichiarato 2.54 (2 % come sulla foto campione) -> fila."""
    assert count_edge_copper(board(lands=range(12), pitch=2.49), "top", 2.54, LENGTH, WIDTH).found == 12


# ── Declared positions (second review 2026-11-03) ──────────────────────

def _centres(indices, pitch=2.54):
    """Declared centres (mm from the left end) of contacts at `indices` of the board() lattice."""
    return [5.0 + k * pitch for k in indices]


def test_a_short_declared_row_is_found_at_its_declared_positions():
    """Positive: 2 real lands at the declared positions -> present, though shorter than MIN_ROW_LANDS."""
    row = count_edge_copper(board(lands=(3, 4)), "top", 2.54, LENGTH, WIDTH,
                            declared_count=2, declared_centres_mm=_centres((3, 4)))
    assert row.found == 2


def test_a_phantom_short_row_on_a_bare_edge_is_a_measured_absence():
    """Negative twin: the same 2 declared contacts on a bare edge -> 0 (no lands where they are said to be)."""
    row = count_edge_copper(board(lands=(), features=[(1.0, 0.8, 1.0, LABEL)]), "top", 2.54, LENGTH, WIDTH,
                            declared_count=2, declared_centres_mm=_centres((3, 4)))
    assert row.found == 0


def test_chance_copper_at_one_position_on_a_busy_edge_is_unverifiable():
    """A busy edge whose features fall on 1 of 2 declared positions: neither a row nor a proved absence.

    CONTRACT CHANGED (generality audit 2026-11-03, GA1): this used to be 0
    (copper «as frequent as elsewhere») and the side was excluded; failing to
    reject chance is no evidence of absence, so the side stays flagged.
    """
    features = [(x, 0.8, 1.0, LABEL) for x in (1.0, 9.3, 14.0, 20.0, 25.0, 31.0, 36.0)]
    row = count_edge_copper(board(lands=(), features=features), "top", 2.54, LENGTH, WIDTH,
                            declared_count=2, declared_centres_mm=_centres((1, 2)))
    assert row.found is None


def test_a_real_short_row_with_one_land_covered_on_a_busy_edge_is_never_absent():
    """Positive twin of the chance case (GA1): 3 declared lands, the middle one under a dark mounted body -> None, not 0."""
    features = [(x, 0.8, 1.0, LABEL) for x in (1.0, 22.0, 27.0, 33.0)]
    features.append((5.0 + 2 * 2.54 - 1.2, 2.4, 3.0, BOARD))      # dark body over land 2
    row = count_edge_copper(board(lands=(1, 3), features=features), "top", 2.54, LENGTH, WIDTH,
                            declared_count=3, declared_centres_mm=_centres((1, 2, 3)))
    assert row.found is None


def test_an_absence_needs_every_declared_position_read():
    """GA2: positions off the strip are unread; nothing at the read ones is not an absence (None).

    Negative twin: the same bare positions, all on the board, are a measured absence (0).
    """
    bare = board(lands=(), features=[(1.0, 0.8, 1.0, LABEL)])
    partly_off = count_edge_copper(bare, "top", 2.54, LENGTH, WIDTH, declared_count=3,
                                   declared_centres_mm=[LENGTH - 2.54, LENGTH + 0.5, LENGTH + 3.04])
    assert partly_off.found is None
    on_board = count_edge_copper(bare, "top", 2.54, LENGTH, WIDTH, declared_count=3,
                                 declared_centres_mm=_centres((4, 5, 6)))
    assert on_board.found == 0


def test_bare_positions_of_an_assumed_centred_row_are_not_an_absence():
    """GA3: with the offset omitted the positions are the contract's guess: nothing there -> None.

    Negative twin: the same positions DECLARED (offset given) -> 0.
    """
    bare = board(lands=(), features=[(1.0, 0.8, 1.0, LABEL)])
    centres = _centres((6, 7))
    assumed = count_edge_copper(bare, "top", 2.54, LENGTH, WIDTH, declared_count=2,
                                declared_centres_mm=centres, centres_assumed=True)
    declared = count_edge_copper(bare, "top", 2.54, LENGTH, WIDTH, declared_count=2,
                                 declared_centres_mm=centres, centres_assumed=False)
    assert (assumed.found, declared.found) == (None, 0)


def test_a_full_match_that_chance_explains_on_a_busy_edge_is_not_a_row():
    """GA4: features falling on both declared positions of an edge crowded with the same features -> None.

    Positive twin: the same two declared lands on a quiet edge -> confirmed (2).
    """
    xs = [0.6, 2.2, 3.9, 9.6 - 0.4, 12.1 - 0.4, 15.3, 17.0, 19.4, 21.6, 23.1, 25.7, 27.4, 29.9,
          31.3, 33.8, 35.2, 37.6]
    busy = board(lands=(), features=[(x, 0.8, 1.0, LABEL) for x in xs])
    chance = count_edge_copper(busy, "top", 2.54, LENGTH, WIDTH, declared_count=2,
                               declared_centres_mm=[9.6, 12.1])
    assert chance.found is None
    quiet = count_edge_copper(board(lands=(3, 4)), "top", 2.54, LENGTH, WIDTH, declared_count=2,
                              declared_centres_mm=_centres((3, 4)))
    assert quiet.found == 2


def test_a_pitch_finer_than_the_reading_window_is_unreadable():
    """GA7: at 0.4 mm on a 30 px/mm photo land and gap windows overlap -> None, never 0 or a count."""
    bare = board(lands=(), features=[(1.0, 0.8, 1.0, LABEL)])
    row = count_edge_copper(bare, "top", 0.4, LENGTH, WIDTH, declared_count=3,
                            declared_centres_mm=[10.0, 10.4, 10.8])
    assert row.found is None


def test_a_long_row_partly_hidden_with_no_background_left_is_unverifiable_not_absent():
    """A row filling the edge with some lands missing: too little edge outside it to call it chance."""
    lands = [k for k in range(13) if k not in (4, 5)]
    row = count_edge_copper(board(lands=lands), "top", 2.54, LENGTH, WIDTH,
                            declared_count=13, declared_centres_mm=_centres(range(13)))
    assert row.found != 0


def test_only_a_number_given_for_the_offset_makes_the_positions_declared():
    """GA3 at the caller: an omitted, null or non-numeric offset leaves the positions assumed."""
    from kicadgen.services.photo_inventory import _offset_declared
    sides = [{"edge": "Top", "count": 2, "offset_mm": 0}, {"edge": "left", "count": 3},
             {"edge": "right", "offset_mm": None}, {"edge": "bottom", "offset": "abc"}]
    assert _offset_declared(sides, "top") is True
    assert [_offset_declared(sides, edge) for edge in ("left", "right", "bottom")] == [False] * 3
    assert _offset_declared(None, "top") is False
