"""@brief One declared bound on how many pad positions a generated footprint may describe.

A pin grid or a contact count reaches the generators from the user's spin
boxes, from project files and from model answers. Unbounded, a 1000 x 1000
grid took 6.5 s to validate and a 10000 x 10000 one more than ten minutes
(review 2026-11-16, R11-3), and a float count escaped as a raw TypeError.
This module is the single bound every path applies: the pin-header
generator, the castellated module and the readers of grids proposed by a
model (`services.datasheet_extractor`) refuse the same counts.
"""

from __future__ import annotations

import numbers

from ..services.messages import tx

#: Largest number of pad POSITIONS a generated footprint may describe: the
#: rows x columns of a grid, EMPTY positions included (a partial pinout still
#: enumerates its whole grid), or the contacts of a castellated module.
#: DECLARED LIMIT, not tuned on a part. The largest real package counted
#: this way is a full land grid array: the LGA 7529 socket of Intel Xeon 6
#: has 7529 contacts, about 1.33x below this bound, and its rows x columns
#: box holds at least as many positions as contacts (how many more depends
#: on its pattern, not checked here); the largest FPGA BGAs have about 3000
#: balls. 10000 keeps a
#: validation of the whole grid well under a second; a footprint with more
#: positions is a typing or reading error. The margin is thin for the
#: largest sockets: a bigger array, or a staggered one whose rows x columns
#: box counts many empty positions, would need a grid representation that
#: does not count empty positions (only the populated pads), not a higher
#: number here.
MAX_PAD_POSITIONS = 10000


def is_integer_count(value) -> bool:
    """@brief Tell whether `value` is a whole-number count (rows, columns, contacts).

    @param value Candidate count.
    @return True for any integral type (int, numpy integer), bool excluded;
        False for a float (even 2.0: a count read as a measure is a reading
        error), a string or anything else.
    """
    return not isinstance(value, bool) and isinstance(value, numbers.Integral)


def validate_grid_counts(rows, cols) -> tuple:
    """@brief Check a rows x columns pin grid: whole numbers, at least one each, bounded positions.

    @param rows Number of rows of the grid.
    @param cols Number of columns of the grid.
    @return (rows, cols) as ints.
    @throws ValueError With a localized message when either is not a whole
        number (`is_integer_count`), is below 1, or rows x cols exceeds
        `MAX_PAD_POSITIONS`.
    @note The product is checked, not each axis: a single row of 10000
        positions is as bounded as a 100 x 100 grid. Nothing is enumerated
        here, so a huge grid is refused at once.
    """
    if not (is_integer_count(rows) and is_integer_count(cols)):
        raise ValueError(tx("Righe e colonne devono essere numeri interi (righe {rows!r}, colonne {cols!r}).",
                            rows=rows, cols=cols))
    rows, cols = int(rows), int(cols)
    if rows < 1 or cols < 1:
        raise ValueError(tx("Righe e colonne devono essere >= 1."))
    if rows * cols > MAX_PAD_POSITIONS:
        raise ValueError(tx("La matrice {rows}x{cols} ha {count} posizioni di pad, oltre il limite di "
                            "{limit}: nessun componente reale ne ha tante. Controlla righe e colonne.",
                            rows=rows, cols=cols, count=rows * cols, limit=MAX_PAD_POSITIONS))
    return rows, cols
