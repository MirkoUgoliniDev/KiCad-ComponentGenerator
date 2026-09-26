"""@brief Minor audit findings (punto 82, BASSA) fixed by structure instead of names.

* sigle lette sul componente: forma di codice (lettere E cifre, almeno 5
  caratteri), non «almeno due trattini»;
* SOT-223: conteggio osservato di reofori e aletta contro le piazzole
  dichiarate dal footprint del modello, non il nome del file;
* bocca del connettore: lato osservato quando c'e', stima dichiarata altrimenti;
* SOIC senza terminali osservati: conteggio dichiarato o stima dalla lunghezza,
  niente tetto a 14 reofori.
"""
import math
from pathlib import Path

import pytest

from kicadgen.cad3d import kicad_models as km
from test_fixE_parts_orientation import parts  # noqa: F401 - fixture


# ── Sigle ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("marking,token", [
    ("ESP-12F", "ESP-12F"),
    ("AI-THINKER RFM95W", "RFM95W"),
    ("E73-2G4M08S1C", "E73-2G4M08S1C"),
    ("ESPRESSIF ESP32-S3-WROOM-1U", "ESP32-S3-WROOM-1U"),
])
def test_part_number_shaped_markings_become_tokens(marking, token):
    """Positivo: un solo trattino o nessuno non impedisce piu' di trovarle."""
    assert token in km._marking_model_tokens({"marking": marking})


@pytest.mark.parametrize("marking", ["WIFI MADE IN CHINA", "103", "2023", "R10", "A1B2", "U.FL"])
def test_words_values_and_short_codes_are_not_tokens(marking):
    """Negativo: parole senza cifre, valori senza lettere, codici troppo corti."""
    assert km._marking_model_tokens({"marking": marking}) == ()


def test_a_single_hyphen_module_marking_discovers_its_model(tmp_path):
    root = tmp_path / "3dmodels"
    model = root / "vendor.3dshapes" / "ESP-12F.step"
    model.parent.mkdir(parents=True)
    model.write_text("ISO-10303-21;\n", encoding="utf-8")
    (root / "vendor.3dshapes" / "ESP-01.step").write_text("x", encoding="utf-8")
    assert km._marked_model_paths({"marking": "ESP-12F"}, root) == [model]


# ── Tre reofori e aletta ────────────────────────────────────────────


def _footprint(folder, name, model, pads):
    """Scrive un footprint KiCad minimale collegato a `model`."""
    folder.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f'  (pad "{number}" smd rect (at {x} {y}) (size {w} {h}) (layers "F.Cu" "F.Mask"))\n'
        for number, x, y, w, h in pads)
    (folder / f"{name}.kicad_mod").write_text(
        f'(footprint "{name}"\n{body}  (model "{model.as_posix()}")\n)\n', encoding="utf-8")


def _kicad(tmp_path):
    share = tmp_path / "kicad"
    models = share / "3dmodels" / "Package_Custom.3dshapes"
    models.mkdir(parents=True)
    return share, models


THREE_LEADS_AND_TAB = [("1", -2.3, 3.15, 2.0, 1.5), ("2", 0.0, 3.15, 2.0, 1.5),
                       ("3", 2.3, 3.15, 2.0, 1.5), ("2", 0.0, -3.15, 3.8, 2.0)]


def test_the_land_pattern_is_read_from_the_footprint_not_the_name(tmp_path):
    share, models = _kicad(tmp_path)
    # Nome qualunque: la regola non guarda il nome del file.
    tab = models / "Anything_A.step"
    tab.write_text("x", encoding="utf-8")
    _footprint(share / "footprints" / "Package_Custom.pretty", "Anything_A", tab,
               THREE_LEADS_AND_TAB)
    even = models / "Anything_B.step"
    even.write_text("x", encoding="utf-8")
    _footprint(share / "footprints" / "Package_Custom.pretty", "Anything_B", even,
               [(str(n), n * 1.27, 0.0, 0.6, 1.5) for n in range(1, 5)])
    assert km._declared_land_pattern(tab) == (4, 1)
    assert km._declared_land_pattern(even) == (4, 0)
    assert km._declared_land_pattern(models / "Nessun_footprint.step") is None


def _choice(path, error=0.05):
    return {"path": str(path), "package": Path(path).stem, "relative_xy_error": error,
            "dimensions_mm": [6.5, 7.0], "family": "semiconductor"}


def _three_leads_one_tab(**extra):
    return dict(kind="ic", lead_count=3, terminal_count=4, lead_count_complete=True,
                length_mm=7.0, width_mm=6.6, **extra)


def test_three_leads_and_a_tab_promote_a_model_that_declares_them(tmp_path):
    share, models = _kicad(tmp_path)
    tab = models / "Anything_A.step"
    tab.write_text("x", encoding="utf-8")
    _footprint(share / "footprints" / "Package_Custom.pretty", "Anything_A", tab,
               THREE_LEADS_AND_TAB)
    first = _choice(tab)
    assert km._leads_and_tab_agree(first, [first], _three_leads_one_tab()) is True


def test_a_model_with_other_lands_or_no_footprint_is_not_promoted(tmp_path):
    """Negativo: otto piazzole uguali (SOT-223-8 come struttura) o nessun footprint."""
    share, models = _kicad(tmp_path)
    eight = models / "Anything_C.step"
    eight.write_text("x", encoding="utf-8")
    _footprint(share / "footprints" / "Package_Custom.pretty", "Anything_C", eight,
               [(str(n), (n % 4) * 1.27, 3.0 if n < 5 else -3.0, 0.6, 1.5) for n in range(1, 9)])
    first = _choice(eight)
    assert km._leads_and_tab_agree(first, [first], _three_leads_one_tab()) is False
    bare = models / "SOT-223.step"   # anche con il nome «giusto»
    bare.write_text("x", encoding="utf-8")
    first = _choice(bare)
    assert km._leads_and_tab_agree(first, [first], _three_leads_one_tab()) is False


def test_a_second_family_in_the_tie_band_blocks_the_promotion(tmp_path):
    share, models = _kicad(tmp_path)
    tab = models / "Anything_A.step"
    tab.write_text("x", encoding="utf-8")
    _footprint(share / "footprints" / "Package_Custom.pretty", "Anything_A", tab,
               THREE_LEADS_AND_TAB)
    other = models / "Anything_D.step"
    other.write_text("x", encoding="utf-8")
    first, second = _choice(tab, 0.05), _choice(other, 0.07)
    assert km._leads_and_tab_agree(first, [first, second], _three_leads_one_tab()) is False


# ── Bocca del connettore ────────────────────────────────────────────


def _cavity_centre(parts_module, **kwargs):
    shell, *_ = parts_module._connector(0.0, 0.0, 0.0, 8.0, 6.0, 3.0, "#999",
                                        mating="edge", **kwargs)
    cavity = shell[0].cuts[0]
    x0, y0, x1, y1 = cavity.bounds()
    return (x0 + x1) / 2, (y0 + y1) / 2


@pytest.mark.parametrize("side,expected", [
    ("right", (1, 0)), ("left", (-1, 0)), ("top", (0, 1)), ("bottom", (0, -1))])
def test_the_observed_mouth_side_decides_axis_and_verse(parts, side, expected):
    """Positivo: il lato osservato (frame foto, Y in basso) vince sulla stima."""
    cx, cy = _cavity_centre(parts, mouth_side=side, board_centre=(-50.0, -50.0))
    assert (round(math.copysign(1, cx)) if abs(cx) > 1e-9 else 0,
            round(math.copysign(1, cy)) if abs(cy) > 1e-9 else 0) == expected


def test_the_observed_side_follows_the_component_rotation(parts):
    # Ruotato di 90 gradi (frame modello): la destra della foto e' il -Y locale.
    assert parts._mouth_axis("right", 90.0) == (1, -1.0)
    assert parts._mouth_axis("right", 45.0) is None      # diagonale: nessuna faccia
    assert parts._mouth_axis("nowhere", 0.0) is None


def test_without_an_observation_the_declared_heuristic_is_kept(parts):
    """Negativo: senza lato osservato vale la stima (asse corto, verso l'esterno)."""
    # 8 x 6: la stima sceglie l'asse corto (Y locale) e il verso lontano dal
    # centro della scheda, qui sotto il componente.
    cx, cy = _cavity_centre(parts, board_centre=(0.0, -50.0))
    assert cy > 0 and cx == 0
    assert parts._observed_mouth_side({"landmark_feature": "connector_opening",
                                       "landmark_side": "left",
                                       "landmark_confidence": "low"}) is None
    assert parts._observed_mouth_side({"landmark_feature": "pin1_marker",
                                       "landmark_side": "left",
                                       "landmark_confidence": "high"}) is None
    assert parts._observed_mouth_side({"landmark_feature": "cable_exit",
                                       "landmark_side": "left",
                                       "landmark_confidence": "medium"}) == "left"


# ── SOIC senza terminali osservati ──────────────────────────────────


def test_a_declared_lead_count_sets_count_and_pitch(parts):
    """TSSOP-20: 10 per lato su 6.5 mm, passo 0.65, non 1.27."""
    per_side, pitch, estimated = parts._soic_leads(6.5, 20)
    assert (per_side, estimated) == (10, False) and pitch == pytest.approx(0.65)


def test_without_a_count_the_estimate_follows_the_body_length_uncapped(parts):
    assert parts._soic_leads(4.9) == (4, parts.LEAD_PITCH_MM, True)      # SOIC-8
    assert parts._soic_leads(9.9) == (8, parts.LEAD_PITCH_MM, True)      # SOIC-16
    assert parts._soic_leads(25.0)[0] > 14                                # niente tetto
    # Conteggi non utilizzabili: dispari o troppo piccoli -> stima.
    assert parts._soic_leads(6.5, 7)[2] is True and parts._soic_leads(6.5, 2)[2] is True


def test_the_soic_builder_draws_the_declared_leads(parts):
    shapes = parts._ic_soic(0.0, 0.0, 0.0, 6.5, 6.4, 1.2, "#111", lead_count=20)
    assert len(shapes) - 1 == 20
    plain = parts._ic_soic(0.0, 0.0, 0.0, 4.9, 3.9, 1.5, "#111")
    assert len(plain) - 1 == 8


def test_build_component_uses_only_a_complete_lead_count(parts):
    base = dict(name="U1", kind="ic_soic", x=0.0, y=0.0, length_mm=6.5, width_mm=6.4,
                height_mm=1.2, color_hex="#111")
    complete = parts.build_component(dict(base, lead_count=20, lead_count_complete=True), 0.0)
    partial = parts.build_component(dict(base, lead_count=20, lead_count_complete=False), 0.0)
    assert len(complete) - 1 == 20
    assert len(partial) - 1 == 2 * int(round(6.5 / parts.LEAD_PITCH_MM))


# ── Vocabolario dei tipi ampliato (punto 84 medio 13) ───────────────


def test_every_new_kind_has_a_builder_and_a_declared_family(parts):
    from kicadgen.services.datasheet_extractor import COMPONENT_KINDS
    assert set(COMPONENT_KINDS) <= set(parts.BUILDERS)
    assert km.KIND_FAMILY_ALIASES["capacitor_electrolytic"] == "capacitor"
    assert km.KIND_FAMILY_ALIASES["pin_header"] == "connector"
    assert km.KIND_FAMILY_ALIASES["ic_sot"] == "semiconductor"


def test_the_electrolytic_can_stays_inside_the_measured_envelope(parts):
    shapes = parts._electrolytic(0.0, 0.0, 0.0, 6.6, 6.6, 5.4, "#777")
    assert len(shapes) == 2
    for shape, _ in shapes:
        x0, y0, x1, y1 = shape.bounds()
        assert -3.3 - 1e-9 <= x0 and x1 <= 3.3 + 1e-9 and -3.3 - 1e-9 <= y0 and y1 <= 3.3 + 1e-9
        assert shape.BoundBox.ZMax <= 5.4 + 1e-9
    # Troppo piatto per base e lattina: resta il blocco dell'ingombro.
    assert len(parts._electrolytic(0.0, 0.0, 0.0, 6.6, 6.6, 0.2, "#777")) == 1
