"""Test di kicadgen.services.photo_refine su immagini sintetiche.

Niente foto reali: si costruiscono a mano scheda e componente con
numpy/cv2, cosi' il risultato atteso e' noto in partenza e il test resta
deterministico. Il caso di riferimento e' proprio quello che la soglia sul
colore non sa gestire: un componente quasi nero su un PCB quasi nero, con
solo un piccolo salto di luminosita' e un po' di rumore realistico -- e'
li' che serve Canny invece di una soglia fissa.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="opencv-python non installato")

from kicadgen.services.photo_refine import (
    find_board, refine_component_edges, refine_components,
)

# Scala fissa per tutte le immagini sintetiche: 20 px/mm.
SCALE = 20.0
BODY_LENGTH_MM, BODY_WIDTH_MM = 40.0, 16.0
MARGIN_PX = 40


def _synthetic_photo(component_center_mm, component_size_mm, rotated: bool,
                      board_gray: int = 22, component_gray: int = 45, seed: int = 0):
    """Foto sintetica: sfondo bianco, scheda scura, un componente piu' chiaro
    di pochi livelli di grigio -- lo stesso scarto minimo di un case nero su
    soldermask nero, con un po' di rumore come una foto vera.
    """
    w_px = int(BODY_LENGTH_MM * SCALE) + 2 * MARGIN_PX
    h_px = int(BODY_WIDTH_MM * SCALE) + 2 * MARGIN_PX
    img = np.full((h_px, w_px, 3), 235, dtype="uint8")

    board_x0, board_y0 = MARGIN_PX, MARGIN_PX
    board_x1 = MARGIN_PX + int(BODY_LENGTH_MM * SCALE)
    board_y1 = MARGIN_PX + int(BODY_WIDTH_MM * SCALE)
    cv2.rectangle(img, (board_x0, board_y0), (board_x1, board_y1), (board_gray,) * 3, -1)

    cx_mm, cy_mm = component_center_mm
    length_mm, width_mm = component_size_mm
    cx_px = board_x0 + (BODY_LENGTH_MM / 2 + cx_mm) * SCALE
    cy_px = board_y0 + (BODY_WIDTH_MM / 2 + cy_mm) * SCALE
    span_x_mm = width_mm if rotated else length_mm
    span_y_mm = length_mm if rotated else width_mm
    half_x = span_x_mm * SCALE / 2
    half_y = span_y_mm * SCALE / 2
    p0 = (int(cx_px - half_x), int(cy_px - half_y))
    p1 = (int(cx_px + half_x), int(cy_px + half_y))
    cv2.rectangle(img, p0, p1, (component_gray,) * 3, -1)

    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 2.5, img.shape).astype("int16")
    img = np.clip(img.astype("int16") + noise, 0, 255).astype("uint8")
    return img


# ── find_board ───────────────────────────────────────────────────────

def test_find_board_matches_module_aspect_ratio():
    img = _synthetic_photo((0, 0), (2, 2), rotated=False)
    frame = find_board(img, BODY_LENGTH_MM, BODY_WIDTH_MM)
    assert frame is not None
    assert frame.width_px / frame.height_px == pytest.approx(BODY_LENGTH_MM / BODY_WIDTH_MM, rel=0.05)
    assert frame.scale == pytest.approx(SCALE, rel=0.05)


def test_find_board_rejects_wrong_aspect_ratio():
    img = _synthetic_photo((0, 0), (2, 2), rotated=False)
    # Un modulo dichiarato quasi quadrato non corrisponde al rettangolo
    # allungato nella foto: la vista non e' utilizzabile per QUESTO modulo.
    frame = find_board(img, body_length=BODY_WIDTH_MM, body_width=BODY_WIDTH_MM)
    assert frame is None


# ── refine_component_edges ───────────────────────────────────────────

def test_edges_detect_component_rotated_relative_to_the_guess():
    """Il caso reale: l'estrazione dice orizzontale, il componente in foto
    e' verticale -- i bordi (non le proporzioni dichiarate) decidono."""
    img = _synthetic_photo((5.0, -3.0), (3.0, 1.2), rotated=True)
    frame = find_board(img, BODY_LENGTH_MM, BODY_WIDTH_MM)
    assert frame is not None

    guess = {"x_mm": 5.0, "y_mm": -3.0, "length_mm": 3.0, "width_mm": 1.2, "rotation_deg": 0.0}
    measured = refine_component_edges(img, frame, guess)
    assert measured is not None
    assert measured["rotation_deg"] == 90.0
    assert measured["x_mm"] == pytest.approx(5.0, abs=0.5)
    assert measured["y_mm"] == pytest.approx(-3.0, abs=0.5)
    assert measured["length_mm"] == pytest.approx(3.0, abs=0.6)
    assert measured["width_mm"] == pytest.approx(1.2, abs=0.6)


def test_edges_correct_a_misplaced_estimate():
    """Il componente e' li' vicino ma non esattamente dove stimato: i bordi
    danno la posizione vera, non quella dichiarata."""
    img = _synthetic_photo((8.0, 2.0), (2.5, 1.5), rotated=False)
    frame = find_board(img, BODY_LENGTH_MM, BODY_WIDTH_MM)
    guess = {"x_mm": 8.8, "y_mm": 2.6, "length_mm": 2.5, "width_mm": 1.5, "rotation_deg": 0.0}
    measured = refine_component_edges(img, frame, guess)
    assert measured is not None
    assert measured["x_mm"] == pytest.approx(8.0, abs=0.5)
    assert measured["y_mm"] == pytest.approx(2.0, abs=0.5)


def test_edges_give_up_when_nothing_stands_out():
    """Componente esattamente dello stesso grigio del PCB: nessun bordo da
    trovare, e infatti non si inventa nulla."""
    img = _synthetic_photo((0, 0), (2, 2), rotated=False, board_gray=30, component_gray=30)
    frame = find_board(img, BODY_LENGTH_MM, BODY_WIDTH_MM)
    guess = {"x_mm": 0.0, "y_mm": 0.0, "length_mm": 2.0, "width_mm": 2.0, "rotation_deg": 0.0}
    measured = refine_component_edges(img, frame, guess)
    assert measured is None


def test_edges_reject_a_match_too_far_from_the_guess():
    """Un contorno reale ma lontano (un vicino, non questo componente) non
    deve sostituire una stima ragionevole."""
    img = _synthetic_photo((15.0, 5.0), (2.0, 2.0), rotated=False)
    frame = find_board(img, BODY_LENGTH_MM, BODY_WIDTH_MM)
    # Stima a 5mm di distanza dal vero componente: fuori dalla finestra di
    # ricerca (SEARCH_MARGIN_MM + meta' ingombro), quindi nessun contorno
    # nella finestra -> nessuna misura, non un'assegnazione sbagliata.
    guess = {"x_mm": 8.0, "y_mm": 5.0, "length_mm": 2.0, "width_mm": 2.0, "rotation_deg": 0.0}
    measured = refine_component_edges(img, frame, guess)
    assert measured is None


# ── refine_components (integrazione, con file su disco) ──────────────

def test_refine_components_end_to_end(tmp_path):
    img = _synthetic_photo((-6.0, 4.0), (2.5, 1.2), rotated=True)
    photo = tmp_path / "top.png"
    cv2.imwrite(str(photo), img)

    components = [
        {"name": "D1", "kind": "diode_melf", "x_mm": -6.0, "y_mm": 4.0,
         "length_mm": 2.5, "width_mm": 1.2, "height_mm": 1.0, "rotation_deg": 0.0},
    ]
    refined, report = refine_components([photo], BODY_LENGTH_MM, BODY_WIDTH_MM, components)
    assert report.photo == str(photo)
    assert report.measured == 1
    assert refined[0]["rotation_deg"] == 90.0
    # Il componente originale non viene mutato: refine_components lavora su copie.
    assert components[0]["rotation_deg"] == 0.0


def test_refine_components_skips_when_no_photo_matches(tmp_path):
    components = [
        {"name": "D1", "x_mm": 0.0, "y_mm": 0.0, "length_mm": 2.0, "width_mm": 1.0,
         "height_mm": 1.0, "rotation_deg": 0.0},
    ]
    refined, report = refine_components([], BODY_LENGTH_MM, BODY_WIDTH_MM, components)
    assert report.photo is None
    assert refined == components


def test_refine_components_preserves_name_kind_and_marking(tmp_path):
    """Solo la geometria si corregge: identita' e aspetto del componente restano."""
    img = _synthetic_photo((0.0, 0.0), (2.0, 2.0), rotated=False, board_gray=20, component_gray=60)
    photo = tmp_path / "top.png"
    cv2.imwrite(str(photo), img)
    components = [{
        "name": "SP3485E", "kind": "ic_soic", "marking": "SP3485E", "color_hex": "#1a1a1a",
        "x_mm": 0.0, "y_mm": 0.0, "length_mm": 2.0, "width_mm": 2.0,
        "height_mm": 1.2, "rotation_deg": 0.0,
    }]
    refined, _ = refine_components([photo], BODY_LENGTH_MM, BODY_WIDTH_MM, components)
    assert refined[0]["name"] == "SP3485E"
    assert refined[0]["kind"] == "ic_soic"
    assert refined[0]["marking"] == "SP3485E"
    assert refined[0]["color_hex"] == "#1a1a1a"
