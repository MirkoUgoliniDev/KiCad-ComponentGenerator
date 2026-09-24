"""@brief Frase «tipo SMD di proposito» nella descrizione del footprint.

Decisione dell'utente del 2026-09-24: un modulo che si salda a rifusione
resta `(attr smd)` anche quando le sue piazzole sono forate, perche' KiCad di
norma esporta nel file di posizionamento solo i footprint SMD. KiCad segnala
pero' la combinazione (controllo `footprint_type_mismatch`), quindi la ragione
va scritta nella descrizione.

Il criterio e' strutturale, la combinazione che KiCad controlla: tipo SMD e
almeno un pad forato. Non il nome del tipo di contatto, non il modulo. Ogni
caso positivo ha un negativo che differisce per una sola delle due condizioni.
"""
from kicadgen.generators.castellated_module import (
    CONTACT_HALF_AND_THROUGH, CONTACT_HALF_HOLE, CONTACT_THROUGH_HOLE,
    SMD_WITH_DRILLED_PADS_NOTE, CastellatedModuleParams, CastellatedSide,
    build_castellated_module,
)
from test_contact_style import rs485


def _mixed():
    """Un lato castellated e uno a foro passante: SMD con pad forati, senza stadi."""
    return CastellatedModuleParams(
        name="Mixed", body_length=30, body_width=20, thru_hole_offset=2.0,
        sides=[CastellatedSide(edge="left", count=3, pitch=2.54, first_pin=1,
                               contact_style=CONTACT_HALF_HOLE),
               CastellatedSide(edge="right", count=3, pitch=2.54, first_pin=4,
                               contact_style=CONTACT_THROUGH_HOLE)])


def _drilled(fp):
    return any(pad.pad_type == "thru_hole" for pad in fp.pads)


def test_positive_continuous_pads_are_drilled_smd_and_say_why():
    """@brief Le piazzole a stadio sono forate, il tipo resta SMD: la frase c'e', una volta."""
    fp = build_castellated_module(rs485(CONTACT_HALF_AND_THROUGH))
    assert fp.attr == "smd" and _drilled(fp)
    assert fp.description.count(SMD_WITH_DRILLED_PADS_NOTE) == 1
    assert fp.description.endswith(SMD_WITH_DRILLED_PADS_NOTE)


def test_positive_mixed_sides_get_the_sentence_without_any_continuous_pad():
    """@brief La regola non dipende dallo stadio: vale per ogni SMD con pad forati."""
    fp = build_castellated_module(_mixed())
    assert fp.attr == "smd" and _drilled(fp)
    assert all(pad.shape != "oval" for pad in fp.pads)
    assert SMD_WITH_DRILLED_PADS_NOTE in fp.description


def test_negative_smd_without_drilled_pads_says_nothing():
    """@brief Stesso tipo SMD, nessun pad forato: niente frase."""
    fp = build_castellated_module(rs485(CONTACT_HALF_HOLE))
    assert fp.attr == "smd" and not _drilled(fp)
    assert SMD_WITH_DRILLED_PADS_NOTE not in fp.description


def test_negative_half_and_through_without_declared_hole_says_nothing():
    """@brief Stesso tipo di contatto del caso positivo, ma senza foro: niente frase."""
    params = rs485(CONTACT_HALF_AND_THROUGH)
    params.thru_hole_drill = 0
    fp = build_castellated_module(params)
    assert fp.attr == "smd" and not _drilled(fp)
    assert SMD_WITH_DRILLED_PADS_NOTE not in fp.description


def test_negative_drilled_pads_on_a_through_hole_footprint_say_nothing():
    """@brief Pad forati ma tipo gia' coerente (through_hole): niente da spiegare."""
    params = rs485(CONTACT_THROUGH_HOLE)
    params.thru_hole_offset = 2.5
    fp = build_castellated_module(params)
    assert fp.attr == "through_hole" and _drilled(fp)
    assert SMD_WITH_DRILLED_PADS_NOTE not in fp.description


def test_a_declared_description_is_kept_and_the_sentence_follows_it():
    """@brief La descrizione estratta dall'AI resta intera, la frase viene dopo."""
    params = rs485(CONTACT_HALF_AND_THROUGH)
    params.description = "Scheda di sviluppo ESP32-S3, 2x22 contatti"
    fp = build_castellated_module(params)
    assert fp.description == f"{params.description} {SMD_WITH_DRILLED_PADS_NOTE}"


def test_the_sentence_reaches_the_kicad_file():
    """@brief La frase finisce in `descr`, cioe' dove la legge chi usa la libreria."""
    rendered = build_castellated_module(rs485(CONTACT_HALF_AND_THROUGH)).render()
    descr = next(line for line in rendered.splitlines() if line.strip().startswith("(descr"))
    assert SMD_WITH_DRILLED_PADS_NOTE in descr
