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
    aspect_deformation, find_board, refine_component_edges, refine_components,
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


def test_find_board_calibrates_other_proportions_axis_by_axis():
    """Contratto cambiato il 27/09 (utente: «devi accettare la foto anche in
    questo formato»). Prima un modulo dichiarato quadrato rifiutava la scheda
    allungata della foto. Ora, se e' l'unico oggetto separato dallo sfondo,
    la scala si misura per asse e la deformazione viene dichiarata: o la foto
    e' deformata o le quote sono sbagliate, e la sola foto non lo dice."""
    img = _synthetic_photo((0, 0), (2, 2), rotated=False)
    frame = find_board(img, body_length=BODY_WIDTH_MM, body_width=BODY_WIDTH_MM)
    assert frame is not None
    assert frame.width_px == pytest.approx(BODY_LENGTH_MM * SCALE, abs=2)
    assert frame.height_px == pytest.approx(BODY_WIDTH_MM * SCALE, abs=2)
    assert aspect_deformation(frame, BODY_WIDTH_MM, BODY_WIDTH_MM) == pytest.approx(
        BODY_LENGTH_MM / BODY_WIDTH_MM - 1, abs=0.02)


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


def _tilted_board(scale: float, tilt_deg: float = 0.4):
    """Scheda inclinata di una frazione di grado, alla scala richiesta.

    E' la geometria che rompeva il rilevamento: bordi lunghi resi irregolari
    da pad castellati e angoli raccordati fanno leggere a `minAreaRect`
    qualche decimo di grado anche su una scheda posata dritta. La deriva che
    ne risulta sul lato lungo cresce con la risoluzione, mentre
    l'inclinazione -- cioe' l'errore geometrico vero -- resta la stessa.
    """
    margin = int(round(8 * scale))
    board_w, board_h = int(BODY_LENGTH_MM * scale), int(BODY_WIDTH_MM * scale)
    img = np.full((board_h + 2 * margin, board_w + 2 * margin, 3), 235, dtype="uint8")
    cv2.rectangle(img, (margin, margin), (margin + board_w, margin + board_h), (22,) * 3, -1)
    center = (img.shape[1] / 2, img.shape[0] / 2)
    matrix = cv2.getRotationMatrix2D(center, tilt_deg, 1.0)
    return cv2.warpAffine(img, matrix, (img.shape[1], img.shape[0]),
                          flags=cv2.INTER_LINEAR, borderValue=(235, 235, 235))


@pytest.mark.parametrize("scale", [6.0, 12.0, 20.0, 34.0])
def test_find_board_verdict_does_not_depend_on_resolution(scale):
    """Stessa scheda, stessa inclinazione, risoluzioni diverse: stesso esito.

    Con una soglia di deriva in pixel assoluta la scheda passava a bassa
    risoluzione e veniva scartata ad alta: il filtro misurava la dimensione
    dell'immagine invece della geometria.
    """
    frame = find_board(_tilted_board(scale), BODY_LENGTH_MM, BODY_WIDTH_MM)
    assert frame is not None, f"scheda non trovata a {scale} px/mm"
    assert frame.scale == pytest.approx(scale, rel=0.06)


def test_find_board_rejects_a_small_lookalike_instead_of_calibrating_on_it():
    """Un dettaglio scuro con il rapporto d'aspetto giusto non e' la scheda.

    Senza il filtro sulla risoluzione minima veniva accettato in silenzio e
    l'inventario finiva calibrato su un ritaglio di pochi pixel: un errore
    muto, peggiore di un rifiuto esplicito.
    """
    img = np.full((1536, 2708, 3), 200, dtype="uint8")
    # Rapporto d'aspetto identico a quello del modulo, ma minuscolo.
    w = 119
    h = int(round(w / (BODY_LENGTH_MM / BODY_WIDTH_MM)))
    cv2.rectangle(img, (1078, 893), (1078 + w, 893 + h), (20,) * 3, -1)
    assert find_board(img, BODY_LENGTH_MM, BODY_WIDTH_MM) is None


# ── orientamento misurato, non arrotondato ───────────────────────────

def _photo_with_oblique_body(center_mm, size_mm, angle_deg, scale=SCALE,
                             board_gray=22, component_gray=45, seed=0):
    """Foto sintetica con un corpo posato a un angolo qualsiasi.

    Serve a distinguere due cose che l'arrotondamento a 0/90 confondeva: un
    pezzo davvero obliquo e un pezzo dritto misurato male.
    """
    margin = int(round(2.0 * scale))
    board_w, board_h = int(BODY_LENGTH_MM * scale), int(BODY_WIDTH_MM * scale)
    img = np.full((board_h + 2 * margin, board_w + 2 * margin, 3), 235, dtype="uint8")
    cv2.rectangle(img, (margin, margin), (margin + board_w, margin + board_h),
                  (board_gray,) * 3, -1)
    cx_px = margin + (BODY_LENGTH_MM / 2 + center_mm[0]) * scale
    cy_px = margin + (BODY_WIDTH_MM / 2 + center_mm[1]) * scale
    box = cv2.boxPoints(((cx_px, cy_px),
                         (size_mm[0] * scale, size_mm[1] * scale), angle_deg))
    cv2.fillPoly(img, [np.int32(np.round(box))], (component_gray,) * 3)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 2.5, img.shape).astype("int16")
    return np.clip(img.astype("int16") + noise, 0, 255).astype("uint8")


def test_angular_resolution_shrinks_as_the_body_grows():
    """Quanto fine puo' essere l'angolo dipende dalla taglia del contorno."""
    from kicadgen.services.photo_refine import angular_resolution_deg
    assert angular_resolution_deg(16) > angular_resolution_deg(40) > angular_resolution_deg(400)
    # Un corpo lungo un pixel non puo' dichiarare nessun angolo utile.
    assert angular_resolution_deg(1) == pytest.approx(45.0)


def test_edges_report_the_angle_of_a_clearly_oblique_body():
    """Un corpo posato a 30 gradi va dichiarato a 30, non raddrizzato a 0.

    La catena CAD applica rotazioni qualsiasi: schiacciare la misura sulla
    cardinale piu' vicina buttava via l'unica informazione che OpenCV da'
    gratis, senza addestramento.
    """
    img = _photo_with_oblique_body((5.0, 0.0), (6.0, 2.0), 30.0)
    frame = find_board(img, BODY_LENGTH_MM, BODY_WIDTH_MM)
    assert frame is not None
    guess = {"x_mm": 5.0, "y_mm": 0.0, "length_mm": 6.0, "width_mm": 2.0,
             "rotation_deg": 0.0}
    measured = refine_component_edges(img, frame, guess)
    assert measured is not None
    assert measured["rotation_deg"] == pytest.approx(30.0, abs=3.0)
    assert measured["length_mm"] == pytest.approx(6.0, abs=0.7)
    assert measured["width_mm"] == pytest.approx(2.0, abs=0.7)


def test_the_same_tilt_is_a_measure_on_a_large_body_and_noise_on_a_small_one():
    """Caso positivo e caso negativo con LO STESSO angolo fisico.

    Due gradi su un corpo lungo 6 mm stanno sopra la risoluzione angolare del
    suo contorno e vanno riportati; gli stessi due gradi su un passivo
    minuscolo stanno sotto, e dichiararli sarebbe inventare precisione. La
    regola distingue i due casi per un motivo strutturale -- la taglia del
    contorno -- non per coincidenza numerica.
    """
    from kicadgen.services.photo_refine import angular_resolution_deg
    tilt = 2.0
    large = _photo_with_oblique_body((5.0, 0.0), (6.0, 2.0), tilt, component_gray=70)
    small = _photo_with_oblique_body((-8.0, 0.0), (1.2, 0.6), tilt, component_gray=70)
    frame_large = find_board(large, BODY_LENGTH_MM, BODY_WIDTH_MM)
    frame_small = find_board(small, BODY_LENGTH_MM, BODY_WIDTH_MM)

    # Le due taglie stanno davvero ai due lati della soglia, altrimenti il
    # test passerebbe per caso invece che per la regola. I lati sono quelli
    # del contorno, gia' gonfiati dalla chiusura morfologica.
    assert angular_resolution_deg(2.0 * SCALE + 4) < tilt < angular_resolution_deg(0.6 * SCALE + 4)

    big = refine_component_edges(large, frame_large, {
        "x_mm": 5.0, "y_mm": 0.0, "length_mm": 6.0, "width_mm": 2.0, "rotation_deg": 0.0})
    tiny = refine_component_edges(small, frame_small, {
        "x_mm": -8.0, "y_mm": 0.0, "length_mm": 1.2, "width_mm": 0.6, "rotation_deg": 0.0})
    assert big is not None and tiny is not None
    assert big["rotation_deg"] == pytest.approx(tilt, abs=0.5)
    assert tiny["rotation_deg"] == 0.0


@pytest.mark.parametrize("size", [(1.0, 0.5), (1.2, 0.6), (1.6, 0.8), (2.0, 1.0),
                                  (3.2, 1.6), (6.0, 2.0), (10.0, 4.0)])
def test_a_straight_body_never_gets_an_invented_angle_at_any_size(size):
    """La prova su cui poggiano CONTOUR_NOISE_PX e MORPH_GROWTH_PX.

    Un corpo posato dritto, rasterizzato su una griglia di pixel, puo' far
    leggere a minAreaRect qualche grado di inclinazione, e tanti piu' quanto
    piu' e' piccolo (su un blob da 14 px si sono misurati fino a 4.8 gradi
    falsi). Se la risoluzione dichiarata non limitasse quell'errore, il
    rilevamento inizierebbe a ruotare componenti dritti: e' il modo in cui
    questa misura puo' peggiorare il risultato invece di migliorarlo.
    """
    for seed in range(4):
        img = _photo_with_oblique_body((5.0, 0.0), size, 0.0, component_gray=70, seed=seed)
        frame = find_board(img, BODY_LENGTH_MM, BODY_WIDTH_MM)
        measured = refine_component_edges(img, frame, {
            "x_mm": 5.0, "y_mm": 0.0, "length_mm": size[0], "width_mm": size[1],
            "rotation_deg": 0.0})
        if measured is None:
            continue
        assert measured["rotation_deg"] in (0.0, 90.0), (
            f"corpo dritto {size} dichiarato obliquo (seed {seed})")


def test_a_body_laid_straight_keeps_a_cardinal_angle():
    """Nessun tremolio: un pezzo dritto resta a 0, non a 0.4 gradi."""
    img = _photo_with_oblique_body((0.0, 0.0), (6.0, 2.0), 0.0)
    frame = find_board(img, BODY_LENGTH_MM, BODY_WIDTH_MM)
    measured = refine_component_edges(img, frame, {
        "x_mm": 0.0, "y_mm": 0.0, "length_mm": 6.0, "width_mm": 2.0, "rotation_deg": 0.0})
    assert measured is not None
    assert measured["rotation_deg"] == 0.0
