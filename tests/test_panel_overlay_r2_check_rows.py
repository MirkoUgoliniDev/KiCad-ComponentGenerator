"""@brief Round-2 fixes of the «Verifica componente» rows: pad ranges and French colons (2026-11-15).

T6: the «Pad stimati» row (code «estimated_pads») read «(pin 1–5)» although
its numbers are PADS of the footprint. The range is now worded by the kind of
number the row's checks carry (`PAD_NUMBER_CODES`); pin-numbered rows keep
«pin». The overlap row («footprint_pads_overlap») carries pad numbers too.

T7: «À faire dans KiCad: …» was composed as f'{level}: …', so French lost
the space its typography puts before the colon. The manual row, the
«title: detail» rows and the tooltips are composed through catalogue
templates whose French text carries « : ».
"""
from kicadgen.gui import component_check_dialog as ccd
from kicadgen.services.component_check import Check
from tests.test_component_check_document import _dialog, _release_dialogs  # noqa: F401

_ESTIMATED = Check('WARN', 'Pad stimati', 'Pad 1-5: dimensioni stimate dal programma.', 'final', 2,
                   'Confronta questi pad con il disegno quotato.', None, 'estimated_pads',
                   ('1', '2', '3', '4', '5'))
_OVERLAP = Check('ERROR', 'Geometria del footprint', 'Pad 1 e pad 2: il rame si sovrappone.', 'technical', 1,
                 'Sposta uno dei due pad.', None, 'footprint_pads_overlap', ('1', '2'))
#: A row whose numbers are PIN numbers of the symbol (document contradiction).
_PIN_ROW = Check('WARN', 'Pinout del documento', 'Etichette su altri contatti.', 'final', 2,
                 'Rivedi i nomi dei pin.', None, 'document_label_placement', ('3', '4'))
_MANUAL = Check('WARN', 'Verifiche manuali', 'ERC del tuo schema e DRC della tua scheda in KiCad.', 'manual',
                3, '', None, 'manual_checks')
_NO_REMEDY = Check('WARN', 'Solidi 3D', 'Presenza di solidi non confermata.')

#: The French catalogue texts these rows use (the real entries are in i18n.py).
_FRENCH = {
    '{text} (pad {pads})': '{text} (pastille {pads})',
    '{text} (pin {pins})': '{text} (broche {pins})',
    '{level}: {text}': '{level} : {text}',
    '{title}: {detail}': '{title} : {detail}',
    'Da fare in KiCad': 'À faire dans KiCad',
}


def _french(text):
    """A French `tx` for the templates above; every other text stays as written."""
    return _FRENCH.get(text, text)


def _row_texts(dialog):
    """The sentences of the rows as shown (the labels marked `i18n_skip`, icons excluded)."""
    return [label.text() for label in dialog.items_box.findChildren(ccd.QLabel)
            if label.property('i18n_skip')]


def test_pad_numbered_rows_say_pad_and_pin_numbered_rows_say_pin(qapp, monkeypatch):
    """@brief Positive: estimated pads and overlapping pads read «(pad …)»; negative: a pin row reads «(pin …)»."""
    dialog = _dialog(qapp, monkeypatch, [_ESTIMATED, _OVERLAP, _PIN_ROW])
    texts = {item.results[0].code: item.text for item in dialog.items}
    assert texts['estimated_pads'] == 'Confronta questi pad con il disegno quotato (pad 1–5)'
    assert texts['footprint_pads_overlap'] == 'Sposta uno dei due pad (pad 1–2)'
    assert texts['document_label_placement'] == 'Rivedi i nomi dei pin (pin 3–4)'
    assert all('(pin ' not in texts[code] for code in ccd.PAD_NUMBER_CODES)
    dialog.close()


def test_pad_ranges_are_translated_through_their_own_template(qapp, monkeypatch):
    """@brief French: «(pastille 1–5)» for pads, «(broche 3–4)» for pins -- two keys, two words."""
    dialog = _dialog(qapp, monkeypatch, [_ESTIMATED, _PIN_ROW], tx=_french)
    texts = {item.results[0].code: item.text for item in dialog.items}
    assert texts['estimated_pads'].endswith('(pastille 1–5)')
    assert texts['document_label_placement'].endswith('(broche 3–4)')
    dialog.close()


def test_the_manual_row_has_the_french_space_before_its_colon(qapp, monkeypatch):
    """@brief Positive: French «À faire dans KiCad : …»; negative: Italian keeps «Da fare in KiCad: …»."""
    dialog = _dialog(qapp, monkeypatch, [_MANUAL], tx=_french)
    # The fake French `tx` translates the templates only: the detail stays Italian.
    assert any(text.startswith('À faire dans KiCad : ERC del tuo schema') for text in _row_texts(dialog))
    dialog.close()
    dialog = _dialog(qapp, monkeypatch, [_MANUAL])
    assert any(text.startswith('Da fare in KiCad: ERC del tuo schema') for text in _row_texts(dialog))
    assert not any(' : ' in text for text in _row_texts(dialog))
    dialog.close()


def test_title_and_detail_rows_and_tooltips_use_the_catalogue_colon(qapp, monkeypatch):
    """@brief A row without remedy («title: detail») and every tooltip follow the language's colon."""
    dialog = _dialog(qapp, monkeypatch, [_NO_REMEDY], tx=_french)
    assert dialog.items[0].text == 'Solidi 3D : Presenza di solidi non confermata.'
    tooltips = [label.toolTip() for label in dialog.items_box.findChildren(ccd.QLabel)
                if label.property('i18n_skip')]
    assert 'Solidi 3D : Presenza di solidi non confermata.' in tooltips
    dialog.close()
    # Negative: in Italian the colon sticks to the title.
    dialog = _dialog(qapp, monkeypatch, [_NO_REMEDY])
    assert dialog.items[0].text == 'Solidi 3D: Presenza di solidi non confermata.'
    dialog.close()
