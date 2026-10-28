"""Misura dei bordi delle proposte del rilevatore, con OpenCV.

Il rilevatore dice DOVE c'e' un corpo -- cosa che richiede addestramento --
ma il riquadro che ne da' e' approssimativo. I bordi si misurano sul
gradiente, che addestramento non ne richiede. Qui si verifica che la misura
corregga quando puo', che stia ferma quando non puo', e che le piazzole
dichiarate restino fuori dalla ricerca: erano loro a far fallire questa
misura quando fu tolta dal percorso standard.

Immagini sintetiche, niente foto reali: il risultato atteso e' noto in
partenza e il test resta deterministico.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="opencv-python non installato")

from kicadgen.services.contact_zones import contact_positions, contact_rects_mm
from kicadgen.services.photo_inventory import _normalized_box, measure_proposal_edges
from kicadgen.services.photo_refine import crop_frame

SCALE = 20.0
LENGTH_MM, WIDTH_MM = 40.0, 16.0


def _calibrated_board(board_gray=22):
    """Ritaglio gia' calibrato: la scheda occupa esattamente l'immagine."""
    return np.full((int(WIDTH_MM * SCALE), int(LENGTH_MM * SCALE), 3),
                   board_gray, dtype="uint8")


def _paint(image, centre_mm, size_mm, angle_deg=0.0, gray=70):
    cx = (centre_mm[0] / LENGTH_MM + 0.5) * image.shape[1]
    cy = (centre_mm[1] / WIDTH_MM + 0.5) * image.shape[0]
    box = cv2.boxPoints(((cx, cy), (size_mm[0] * SCALE, size_mm[1] * SCALE), angle_deg))
    cv2.fillPoly(image, [np.int32(np.round(box))], (gray,) * 3)
    return image


def _noisy(image, seed=0):
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 2.5, image.shape).astype("int16")
    return np.clip(image.astype("int16") + noise, 0, 255).astype("uint8")


def _proposal(centre_mm, size_mm, rotation=0.0):
    return {
        "name": "YOLO candidate 1", "kind": "generic",
        "x_mm": centre_mm[0], "y_mm": centre_mm[1],
        "length_mm": size_mm[0], "width_mm": size_mm[1], "rotation_deg": rotation,
        "_box": _normalized_box({"x_mm": centre_mm[0], "y_mm": centre_mm[1],
                                 "length_mm": size_mm[0], "width_mm": size_mm[1],
                                 "rotation_deg": rotation}, LENGTH_MM, WIDTH_MM),
    }


# ── crop_frame ───────────────────────────────────────────────────────

def test_crop_frame_reads_the_scale_from_the_declared_size():
    """Dentro il proprio ritaglio la scheda non si cerca: si sa gia' dov'e'."""
    frame = crop_frame(_calibrated_board(), LENGTH_MM, WIDTH_MM)
    assert frame is not None
    assert frame.scale == pytest.approx(SCALE)
    # Origine al centro, come la convenzione del footprint.
    assert frame.to_mm(*frame.to_px(3.0, -2.0)) == pytest.approx((3.0, -2.0))


def test_crop_frame_refuses_quotes_that_do_not_hold():
    assert crop_frame(_calibrated_board(), 0.0, WIDTH_MM) is None
    assert crop_frame(None, LENGTH_MM, WIDTH_MM) is None


# ── zone dei contatti ────────────────────────────────────────────────

DECLARED_EDGE = {"castellated_sides": [
    {"edge": "top", "count": 4, "pitch_mm": 2.54, "pad_length_mm": 1.6,
     "pad_width_mm": 0.9, "offset_mm": 0.0}]}
# Contratto aggiornato (verifica del 25/09, A2): i contatti di una matrice si
# collocano solo con gli offset dichiarati del corpo; senza, non si filtra nulla.
DECLARED_MATRIX = {"rows": 2, "cols": 5, "pitch_mm": 2.54, "row_spacing_mm": 2.54,
                   "body_offset_x_mm": -1.27, "body_offset_y_mm": -1.27}


def test_contact_rects_cover_the_declared_strip_along_its_edge():
    positions = contact_positions(DECLARED_EDGE, LENGTH_MM, WIDTH_MM)
    rects = contact_rects_mm(positions, LENGTH_MM, WIDTH_MM)
    assert len(rects) == len(positions) == 4
    for (x0, y0, x1, y1), contact in zip(rects, positions):
        # Il lato «top» sta a -WIDTH/2 e la striscia entra verso il centro.
        assert y0 == pytest.approx(-WIDTH_MM / 2)
        assert y1 > y0
        assert x0 < contact["x_mm"] < x1
        # Mezzo passo per lato, come dichiarato: non una fascia scelta a occhio.
        assert (x1 - x0) == pytest.approx(contact["pitch_mm"])


def test_contact_rects_leave_pin_matrices_alone():
    """Su una matrice il connettore sta SOPRA i suoi pin: mascherarli
    impedirebbe di misurarlo, quindi la matrice non produce esclusioni."""
    positions = contact_positions(DECLARED_MATRIX, LENGTH_MM, WIDTH_MM)
    assert positions and all(p["edge"] == "matrix" for p in positions)
    assert contact_rects_mm(positions, LENGTH_MM, WIDTH_MM) == []


# ── misura delle proposte ────────────────────────────────────────────

def test_a_displaced_proposal_is_moved_onto_the_real_edges():
    image = _noisy(_paint(_calibrated_board(), (6.0, 2.0), (3.0, 1.6)))
    proposal = _proposal((6.8, 2.7), (3.0, 1.6))
    measured = measure_proposal_edges(image, [proposal], [], LENGTH_MM, WIDTH_MM, lambda _: None)
    assert measured[0]["_edges_measured"] is True
    assert measured[0]["x_mm"] == pytest.approx(6.0, abs=0.4)
    assert measured[0]["y_mm"] == pytest.approx(2.0, abs=0.4)
    # La proposta di partenza resta consultabile: la misura si puo' contestare.
    assert measured[0]["_detector_geometry_mm"]["x_mm"] == pytest.approx(6.8)
    # Il riquadro normalizzato segue la geometria misurata, non quella vecchia.
    x0, y0, x1, y1 = measured[0]["_box"]
    centre_x = (x0 + x1) / 2 - 0.5
    assert centre_x * LENGTH_MM == pytest.approx(6.0, abs=0.4)
    # L'originale non viene mutato.
    assert proposal["x_mm"] == pytest.approx(6.8)


def test_a_proposal_over_nothing_is_left_exactly_as_it_was():
    """Quando non misura, non peggiora: la proposta resta quella del rilevatore."""
    image = _noisy(_calibrated_board())
    proposal = _proposal((6.0, 2.0), (3.0, 1.6))
    measured = measure_proposal_edges(image, [proposal], [], LENGTH_MM, WIDTH_MM, lambda _: None)
    assert "_edges_measured" not in measured[0]
    assert measured[0]["x_mm"] == pytest.approx(6.0)
    assert measured[0]["length_mm"] == pytest.approx(3.0)


def test_the_declared_pads_are_invisible_to_the_measure():
    """Caso positivo e negativo sulla stessa immagine, stessa piazzola.

    Una piazzola dorata ha un contorno netto quanto quello di un componente:
    da sola viene misurata benissimo, ed e' proprio questo che faceva
    sostituire una stima buona con il bordo di un contatto. Con le zone
    dichiarate quella stessa piazzola smette di esistere per la misura. La
    differenza la fa un dato dichiarato -- lato, passo, lunghezza piazzola --
    non una soglia scelta guardando la foto.
    """
    board = _calibrated_board()
    declared = {"castellated_sides": [
        {"edge": "top", "count": 5, "pitch_mm": 2.54, "pad_length_mm": 1.6,
         "pad_width_mm": 0.9, "offset_mm": 0.0}]}
    positions = contact_positions(declared, LENGTH_MM, WIDTH_MM)
    assert positions
    # Piazzola tutta dentro la striscia dichiarata (che arriva a 2.6 mm dal
    # bordo) ma staccata dal bordo stesso, che ha una sua esclusione a parte.
    pad_centre_y = -WIDTH_MM / 2 + 1.3
    for contact in positions:
        _paint(board, (contact["x_mm"], pad_centre_y), (0.9, 1.6), gray=210)
    image = _noisy(board)

    # Una proposta esattamente su una piazzola: e' il caso che il filtro dei
    # contatti toglie a monte, ma qui serve a provare che la maschera copre
    # davvero la zona dichiarata e non una fascia qualsiasi.
    on_pad = _proposal((positions[2]["x_mm"], pad_centre_y), (1.6, 0.9), rotation=90.0)
    without_mask = measure_proposal_edges(image, [dict(on_pad)], [], LENGTH_MM, WIDTH_MM,
                                          lambda _: None)[0]
    with_mask = measure_proposal_edges(image, [dict(on_pad)], positions, LENGTH_MM, WIDTH_MM,
                                       lambda _: None)[0]

    assert without_mask.get("_edges_measured"), (
        "senza maschera la piazzola non veniva misurata: il test non prova nulla")
    assert "_edges_measured" not in with_mask
    assert with_mask["y_mm"] == pytest.approx(on_pad["y_mm"])


def test_a_body_outside_the_declared_strip_is_still_measured():
    """La maschera non deve spegnere la misura su tutta la zona dei contatti.

    Il corpo montato appena oltre la striscia dichiarata resta misurabile:
    altrimenti l'esclusione, invece di togliere le piazzole, toglierebbe i
    componenti che ci stanno vicino.
    """
    board = _calibrated_board()
    declared = {"castellated_sides": [
        {"edge": "top", "count": 5, "pitch_mm": 2.54, "pad_length_mm": 1.6,
         "pad_width_mm": 0.9, "offset_mm": 0.0}]}
    positions = contact_positions(declared, LENGTH_MM, WIDTH_MM)
    for contact in positions:
        _paint(board, (contact["x_mm"], -WIDTH_MM / 2 + 0.8), (0.9, 1.6), gray=210)
    # La striscia dichiarata arriva a 2.6 mm dal bordo: questo corpo e' oltre.
    body_centre = (positions[2]["x_mm"], -WIDTH_MM / 2 + 3.4)
    _paint(board, body_centre, (2.0, 1.2), gray=60)
    image = _noisy(board)

    proposal = _proposal((body_centre[0] + 0.4, body_centre[1] + 0.3), (2.0, 1.2))
    measured = measure_proposal_edges(image, [proposal], positions, LENGTH_MM, WIDTH_MM,
                                      lambda _: None)[0]
    assert measured.get("_edges_measured")
    assert measured["x_mm"] == pytest.approx(body_centre[0], abs=0.4)
    assert measured["y_mm"] == pytest.approx(body_centre[1], abs=0.4)


# ── la finestra di ricerca si ferma dove comincia il vicino ──────────

def test_search_window_shrinks_on_a_crowded_board_and_opens_on_a_sparse_one():
    """La densità della scheda decide la finestra, non un numero scelto a mano."""
    from kicadgen.services.photo_refine import (MORPH_GROWTH_PX, SEARCH_MARGIN_MM,
                                                search_margin_mm)
    scale = 55.0
    # Corpo isolato: nessun vicino a chiudere la zona, vale il tetto.
    assert search_margin_mm(20.0, scale) == pytest.approx(SEARCH_MARGIN_MM)
    # Vicino a 1 mm: ci si ferma a metà strada.
    assert search_margin_mm(1.0, scale) == pytest.approx(0.5)
    # Riquadri che si toccano: resta solo quanto la chiusura morfologica
    # gonfia il contorno, altrimenti si taglierebbe il proprio stesso bordo.
    assert search_margin_mm(0.0, scale) == pytest.approx(MORPH_GROWTH_PX / scale)


def test_a_neighbour_stops_the_measure_from_grabbing_it():
    """Caso positivo e negativo: stessa foto, finestra stretta o larga.

    È il difetto misurato sul modulo vero: con una finestra fissa di 2 mm, su
    una foto fitta il contorno del vicino finiva nella stessa finestra e la
    misura lo agganciava, spostando il corpo di oltre un millimetro. Qui il
    corpo cercato è quasi invisibile (nessun contorno utile) e accanto c'è un
    vicino netto della stessa taglia: con la finestra larga la misura diventa
    il vicino, con la finestra che si ferma a metà strada non misura affatto e
    la proposta resta quella del rilevatore -- che è il comportamento giusto,
    perché non misurare non peggiora mai.
    """
    from kicadgen.services.photo_refine import (SEARCH_MARGIN_MM, crop_frame,
                                                refine_component_edges)
    board = _calibrated_board()
    _paint(board, (0.0, 0.0), (1.4, 1.0), gray=24)      # corpo, quasi invisibile
    neighbour = (1.8, 0.0)
    _paint(board, neighbour, (1.4, 1.0), gray=120)      # vicino, netto
    image = _noisy(board)
    proposal = _proposal((0.0, 0.0), (1.4, 1.0))

    narrow = measure_proposal_edges(image, [dict(proposal)], [], LENGTH_MM, WIDTH_MM,
                                    lambda _: None)[0]
    frame = crop_frame(image, LENGTH_MM, WIDTH_MM)
    wide = refine_component_edges(image, frame, dict(proposal), None, SEARCH_MARGIN_MM)

    # CONTRACT CHANGED 2026-10-27 (review F2, photo_refine.centre_on_hypothesis):
    # this precondition used to assert that the WIDE window grabbed the
    # neighbour, to prove that only the narrow window protected the body. The
    # measured centre must now lie on the proposed body itself, so the class of
    # defect is closed at its root and the wide window is safe too: the stricter
    # contract is that NEITHER window turns the neighbour into this body.
    assert wide is None or wide["x_mm"] != pytest.approx(neighbour[0], abs=0.3), (
        "the wide window moved the body onto its neighbour")
    assert "_edges_measured" not in narrow
    assert narrow["x_mm"] == pytest.approx(0.0)
