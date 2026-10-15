"""@brief KLC checks (kicadgen.services.klc_checks): one passing and one failing text per rule.

Every rule is exercised with a text that must pass it and a text that must
fail it; the texts differ only in the quantity the rule looks at, so a check
that passes by coincidence (e.g. because it never fires) is caught.
"""
import pytest

from kicadgen.generators.pin_header import PinHeaderParams, build_pin_header
from kicadgen.generators.symbol import SymbolMetadata, SymbolPin, serialize_symbol
from kicadgen.services import klc_checks as k


GOOD_METADATA = {'reference': 'K', 'description': 'Relè 5 V', 'keywords': 'relay spdt',
                 'datasheet': 'https://example.org/relay.pdf', 'fp_filters': 'Relay_X*'}


def _codes(findings):
    return [f.code for f in findings]


def _symbol(**overrides):
    """A symbol text that passes every symbol rule; `overrides` patch single forms."""
    base = {
        'version': '20251024', 'pin_names': '(pin_names (offset 0.508))',
        'pin1': '(pin passive line (at -7.62 0 0) (length 2.54) (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))',
        'pin2': '(pin passive line (at 7.62 0 180) (length 2.54) (name "B" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))',
        'body': '(rectangle (start -5.08 5.08) (end 5.08 -5.08) (stroke (width 0.254) (type default)) (fill (type background)))',
    }
    base.update(overrides)
    return ('(kicad_symbol_lib (version {version}) (generator "t")\n  (symbol "S" {pin_names}\n'
            '    (symbol "S_0_1" {body})\n    (symbol "S_1_1" {pin1} {pin2})))').format(**base)


def _footprint(**overrides):
    base = {
        'version': '(version 20260206)', 'attr': '(attr smd)', 'descr': '(descr "a footprint")',
        'reference': '(property "Reference" "REF**" (at 0 0) (layer "F.SilkS"))',
        'value': '(property "Value" "FP" (at 0 0) (layer "F.Fab"))',
        'crtyd': '(fp_rect (start -1 -1) (end 1 1) (layer "F.CrtYd"))',
        'fab': '(fp_rect (start -1 -1) (end 1 1) (layer "F.Fab"))',
        'pad': '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask"))',
    }
    base.update(overrides)
    return ('(footprint "FP" {version} {attr} {descr} {reference} {value} {crtyd} {fab} {pad})'
            .format(**base))


# ── Finding ────────────────────────────────────────────────────────────────

def test_finding_accepts_the_two_severities_only():
    assert k.Finding('x', 'error', 'm').severity == 'error'
    assert k.Finding('x', 'warning', 'm').severity == 'warning'
    with pytest.raises(ValueError):
        k.Finding('x', 'info', 'm')


# ── check_metadata ─────────────────────────────────────────────────────────

def test_complete_metadata_and_a_clean_name_have_no_findings():
    assert k.check_metadata(GOOD_METADATA, 'Relay_X') == []


@pytest.mark.parametrize('reference,code', [
    ('', 'reference_empty'), ('   ', 'reference_empty'), ('U1', 'reference_letters'),
    ('U?', 'reference_letters'), ('R V', 'reference_letters')])
def test_reference_prefix_must_be_letters_only(reference, code):
    findings = k.check_metadata({**GOOD_METADATA, 'reference': reference}, 'FP')
    assert [f.severity for f in findings if f.code == code] == ['error']


@pytest.mark.parametrize('reference', ['U', 'RV', 'K', 'J'])
def test_letter_prefixes_pass(reference):
    assert k.check_metadata({**GOOD_METADATA, 'reference': reference}, 'FP') == []


def test_description_empty_and_multiline_are_errors():
    # CONTRACT CHANGED 2026-10-15: an empty description was a warning; the
    # user made it a REQUIRED field that blocks the installation, so it is
    # now an error. Blanks only count as empty.
    for blank in ('', '   '):
        empty = k.check_metadata({**GOOD_METADATA, 'description': blank}, 'FP')
        assert [(f.code, f.severity) for f in empty] == [('description_empty', 'error')]
    multi = k.check_metadata({**GOOD_METADATA, 'description': 'one\ntwo'}, 'FP')
    assert [(f.code, f.severity) for f in multi] == [('description_multiline', 'error')]


def test_keywords_empty_and_control_characters_are_errors():
    # CONTRACT CHANGED 2026-10-15: empty keywords were a warning; required now.
    empty = k.check_metadata({**GOOD_METADATA, 'keywords': ' '}, 'FP')
    assert [(f.code, f.severity) for f in empty] == [('keywords_empty', 'error')]
    assert _codes(k.check_metadata({**GOOD_METADATA, 'keywords': 'a\tb'}, 'FP')) == ['keywords_multiline']


@pytest.mark.parametrize('datasheet,codes', [
    ('', ['datasheet_empty']), ('https://x.org/a.pdf', []), ('docs/a b.pdf', ['datasheet_blanks']),
    ('https://x.org/a\x00.pdf', ['datasheet_format']), ('C:/docs/a.pdf', [])])
def test_datasheet_is_empty_a_url_or_a_path_without_blanks(datasheet, codes):
    assert _codes(k.check_metadata({**GOOD_METADATA, 'datasheet': datasheet}, 'FP')) == codes


def test_an_empty_datasheet_is_an_error_and_blanks_inside_a_warning():
    """CONTRACT CHANGED 2026-10-15: the empty datasheet was a warning; it is a
    required field now. A path with blanks stays advice (a Windows path)."""
    (empty,) = k.check_metadata({**GOOD_METADATA, 'datasheet': '  '}, 'FP')
    assert (empty.code, empty.severity) == ('datasheet_empty', 'error')
    (blanks,) = k.check_metadata({**GOOD_METADATA, 'datasheet': 'docs/a b.pdf'}, 'FP')
    assert (blanks.code, blanks.severity) == ('datasheet_blanks', 'warning')


# ── Required fields (user, 2026-10-15) ────────────────────────────────────

@pytest.mark.parametrize('key,code', [
    ('reference', 'reference_empty'), ('description', 'description_empty'),
    ('keywords', 'keywords_empty'), ('datasheet', 'datasheet_empty')])
def test_each_empty_required_field_is_named_by_missing_required_fields(key, code):
    """Negative per field: emptying exactly one required field reports exactly that one."""
    # Blanks only (a no-break space included) count as empty; a TAB would be
    # a control character, which is a different error (`*_multiline`/format).
    findings = k.check_metadata({**GOOD_METADATA, key: '   '}, 'FP')
    assert [(f.code, f.severity) for f in findings] == [(code, 'error')]
    assert k.missing_required_fields(findings) == [key]


def test_complete_metadata_and_empty_filters_miss_no_required_field():
    """Positive: the four required fields filled; the optional filters may stay empty."""
    assert k.missing_required_fields(k.check_metadata(GOOD_METADATA, 'FP')) == []
    no_filters = k.check_metadata({**GOOD_METADATA, 'fp_filters': ''}, 'FP')
    assert [(f.code, f.severity) for f in no_filters] == [('fp_filters_empty', 'warning')]
    assert k.missing_required_fields(no_filters) == []


def test_a_malformed_but_filled_field_is_not_a_missing_one():
    """Negative: a multi-line description or a designator with digits are
    errors of another kind, not missing required fields."""
    findings = k.check_metadata({**GOOD_METADATA, 'description': 'a\nb', 'reference': 'U1'}, 'FP')
    assert {f.code for f in findings} == {'description_multiline', 'reference_letters'}
    assert k.missing_required_fields(findings) == []


def test_missing_required_fields_follow_the_form_order():
    findings = k.check_metadata({'fp_filters': 'X*'}, 'FP')
    assert k.missing_required_fields(reversed(findings)) == [
        'reference', 'description', 'keywords', 'datasheet']


@pytest.mark.parametrize('filters,codes', [
    ('', ['fp_filters_empty']), ('FP* Lib:FP_?', []), ('FP/*', ['fp_filters_format']),
    ('a\\b*', ['fp_filters_format']), ('A:B:C*', ['fp_filters_format'])])
def test_footprint_filters_use_name_characters_and_wildcards_only(filters, codes):
    assert _codes(k.check_metadata({**GOOD_METADATA, 'fp_filters': filters}, 'FP')) == codes


def test_the_empty_filter_warning_suggests_the_footprint_name():
    (finding,) = k.check_metadata({**GOOD_METADATA, 'fp_filters': ''}, 'Relay_X')
    assert 'Relay_X*' in finding.message


@pytest.mark.parametrize('name,codes', [
    ('Relay_X', []), ('Lib:Name', ['footprint_name_illegal']), ('a/b', ['footprint_name_illegal']),
    ('a\\b', ['footprint_name_illegal']), ('a\nb', ['footprint_name_illegal', 'footprint_name_spaces']),
    ('Relay X', ['footprint_name_spaces']), ('', ['footprint_name_empty'])])
def test_footprint_name_characters(name, codes):
    findings = k.check_metadata(GOOD_METADATA, name)
    assert _codes(findings) == codes
    assert all(f.severity == ('warning' if f.code.endswith('spaces') else 'error') for f in findings)


def test_missing_keys_are_treated_as_empty_and_non_dict_rejected():
    assert set(_codes(k.check_metadata({}, 'FP'))) == {
        'reference_empty', 'description_empty', 'keywords_empty', 'datasheet_empty', 'fp_filters_empty'}
    assert _codes(k.check_metadata('nope', 'FP')) == ['metadata_type']


# ── check_symbol_text ──────────────────────────────────────────────────────

def test_a_compliant_symbol_text_has_no_findings():
    assert k.check_symbol_text(_symbol()) == []


def test_the_generator_output_passes_every_symbol_rule():
    text = serialize_symbol('Mod', [SymbolPin('1', 'A'), SymbolPin('2', 'B', side='right'),
                                    SymbolPin('3', 'C', side='top')],
                            footprint='L:F', metadata=SymbolMetadata(**GOOD_METADATA))
    assert k.check_symbol_text(text) == []


@pytest.mark.parametrize('text,code', [
    ('', 'symbol_unreadable'), ('(kicad_symbol_lib (version 1', 'symbol_unreadable'),
    ('(footprint "x")', 'symbol_format'),
    ('(kicad_symbol_lib (generator "t") (symbol "S"))', 'symbol_version'),
    (_symbol(version='20200101'), 'symbol_version')])
def test_unreadable_or_old_symbol_files_are_errors(text, code):
    findings = k.check_symbol_text(text)
    assert code in _codes(findings)
    assert all(f.severity == 'error' for f in findings if f.code == code)


def test_version_at_or_above_the_minimum_passes():
    assert 'symbol_version' not in _codes(k.check_symbol_text(_symbol(version='20211014')))


def test_pin_off_the_grid_is_an_error_and_on_the_grid_is_not():
    off = _symbol(pin1='(pin passive line (at -7.5 0 0) (length 2.54) (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))')
    findings = k.check_symbol_text(off)
    assert [(f.code, f.severity) for f in findings] == [('pin_grid', 'error')]
    on = _symbol(pin1='(pin passive line (at -12.7 5.08 0) (length 2.54) (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))')
    assert k.check_symbol_text(on) == []


def test_pin_length_below_two_fifty_four_or_off_the_half_grid_is_a_warning():
    """KLC S4.1: 2.54 mm, longer in 1.27 mm steps is fine; shorter or odd lengths are advice."""
    short = _symbol(pin1='(pin passive line (at -7.62 0 0) (length 1.27) (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))')
    assert [(f.code, f.severity) for f in k.check_symbol_text(short)] == [('pin_length', 'warning')]
    odd = _symbol(pin1='(pin passive line (at -7.62 0 0) (length 3.0) (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))')
    assert [(f.code, f.severity) for f in k.check_symbol_text(odd)] == [('pin_length', 'warning')]
    longer = _symbol(pin1='(pin passive line (at -7.62 0 0) (length 5.08) (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))')
    assert k.check_symbol_text(longer) == []


def test_pin_text_size_other_than_one_twenty_seven_is_a_warning():
    big = _symbol(pin1='(pin passive line (at -7.62 0 0) (length 2.54) (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.5 1.5)))))')
    assert [(f.code, f.severity) for f in k.check_symbol_text(big)] == [('pin_text_size', 'warning')]


def test_duplicate_pin_numbers_and_overlapping_pins_are_errors():
    same_number = _symbol(pin2='(pin passive line (at 7.62 0 180) (length 2.54) (name "B" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))')
    assert [(f.code, f.severity) for f in k.check_symbol_text(same_number)] == [('pin_duplicate', 'error')]
    same_place = _symbol(pin2='(pin passive line (at -7.62 0 0) (length 2.54) (name "B" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))')
    assert [(f.code, f.severity) for f in k.check_symbol_text(same_place)] == [('pin_overlap', 'error')]


def test_body_rectangle_stroke_and_fill_are_warnings():
    thin = _symbol(body='(rectangle (start -5.08 5.08) (end 5.08 -5.08) (stroke (width 0.1) (type default)) (fill (type background)))')
    assert [(f.code, f.severity) for f in k.check_symbol_text(thin)] == [('body_stroke', 'warning')]
    hollow = _symbol(body='(rectangle (start -5.08 5.08) (end 5.08 -5.08) (stroke (width 0.254) (type default)) (fill (type none)))')
    assert [(f.code, f.severity) for f in k.check_symbol_text(hollow)] == [('body_fill', 'warning')]


def test_the_body_is_the_largest_rectangle_not_the_first():
    """A small decoration drawn before the body (coil box) must not be mistaken for it."""
    text = _symbol(body='(rectangle (start -1 1) (end 1 -1) (stroke (width 0) (type default)) (fill (type none)))'
                        ' (rectangle (start -5.08 5.08) (end 5.08 -5.08) (stroke (width 0.254) (type default)) (fill (type background)))')
    assert k.check_symbol_text(text) == []


def test_pin_names_offset_absent_is_the_default_and_a_different_one_is_a_warning():
    """KiCad omits (pin_names (offset ...)) at its default 0.508 mm, the KLC value."""
    assert k.check_symbol_text(_symbol(pin_names='')) == []
    other = _symbol(pin_names='(pin_names (offset 1.016))')
    assert [(f.code, f.severity) for f in k.check_symbol_text(other)] == [('pin_names_offset', 'warning')]


def test_pins_of_different_units_and_legal_stacks_do_not_overlap():
    """KLC S4.3: units are separate drawings; hidden same-name pins may stack."""
    pin = '(pin passive line (at -7.62 0 0) (length 2.54) (name "{n}" (effects (font (size 1.27 1.27)))) (number "{k}" (effects (font (size 1.27 1.27)))){h})'
    two_units = _symbol(pin2='')
    two_units = two_units.replace('(symbol "S_1_1"', '(symbol "S_2_1" ' + pin.format(n='A', k='9', h='') + ') (symbol "S_1_1"')
    assert k.check_symbol_text(two_units) == []
    stack = _symbol(pin2=pin.format(n='A', k='2', h=' (hide yes)'))
    assert k.check_symbol_text(stack) == []
    old_stack = _symbol(pin2=pin.format(n='A', k='2', h=' hide'))
    assert k.check_symbol_text(old_stack) == []
    two_visible = _symbol(pin2=pin.format(n='A', k='2', h=''))
    assert [f.code for f in k.check_symbol_text(two_visible)] == ['pin_overlap']
    other_name = _symbol(pin2=pin.format(n='B', k='2', h=' (hide yes)'))
    assert [f.code for f in k.check_symbol_text(other_name)] == ['pin_overlap']


# ── check_footprint_text ───────────────────────────────────────────────────

def test_a_compliant_footprint_text_has_no_findings():
    assert k.check_footprint_text(_footprint()) == []


def test_the_pin_header_generator_passes_every_footprint_rule():
    fp = build_pin_header(PinHeaderParams(name='HDR', rows=1, cols=2, body_length=5.08, body_width=2.54,
                                          body_offset_x=-1.27, body_offset_y=-1.27))
    assert k.check_footprint_text(fp.render()) == []


@pytest.mark.parametrize('text,code', [
    ('', 'footprint_unreadable'), ('(footprint "x"', 'footprint_unreadable'),
    ('(kicad_symbol_lib (version 20251024))', 'footprint_format'),
    (_footprint(version=''), 'footprint_version')])
def test_unreadable_footprint_or_missing_version_are_errors(text, code):
    findings = k.check_footprint_text(text)
    assert [f.severity for f in findings if f.code == code] == ['error']


def test_a_footprint_without_pads_is_a_warning():
    """Legal in KiCad (logos); for a component it is advice, not a refusal."""
    assert [(f.code, f.severity) for f in k.check_footprint_text(_footprint(pad=''))] == [('footprint_pads', 'warning')]


@pytest.mark.parametrize('override,code', [
    ({'attr': ''}, 'footprint_attr'), ({'attr': '(attr exclude_from_bom)'}, 'footprint_attr'),
    ({'descr': ''}, 'footprint_descr'), ({'descr': '(descr "  ")'}, 'footprint_descr'),
    ({'reference': '(property "Reference" "U1" (at 0 0) (layer "F.SilkS"))'}, 'footprint_reference'),
    ({'reference': '(property "Reference" "REF**" (at 0 0) (layer "F.Fab"))'}, 'footprint_reference'),
    ({'value': '(property "Value" "FP" (at 0 0) (layer "F.SilkS"))'}, 'footprint_value'),
    ({'value': ''}, 'footprint_value'),
    ({'crtyd': ''}, 'footprint_courtyard'), ({'fab': ''}, 'footprint_fab')])
def test_each_footprint_convention_is_a_single_warning(override, code):
    findings = k.check_footprint_text(_footprint(**override))
    assert [(f.code, f.severity) for f in findings] == [(code, 'warning')]


def test_a_fab_text_alone_is_not_a_fabrication_outline():
    """Every generator writes ${REFERENCE} on F.Fab (2026-10-14): a text is not an outline."""
    text_only = '(fp_text user "${REFERENCE}" (at 0 0) (layer "F.Fab"))'
    findings = k.check_footprint_text(_footprint(fab=text_only))
    assert [(f.code, f.severity) for f in findings] == [('footprint_fab', 'warning')]
    # With a drawn outline next to the text nothing is reported.
    both = '(fp_rect (start -1 -1) (end 1 1) (layer "F.Fab")) ' + text_only
    assert k.check_footprint_text(_footprint(fab=both)) == []


def test_through_hole_attribute_passes():
    assert k.check_footprint_text(_footprint(attr='(attr through_hole)')) == []


# ── check_all ──────────────────────────────────────────────────────────────

def test_check_all_merges_errors_first_and_skips_absent_texts():
    # CONTRACT CHANGED 2026-10-15: the warning used here was the empty
    # description, now an error (required field); the empty footprint filter
    # is still a warning and keeps the «errors first» ordering under test.
    findings = k.check_all({**GOOD_METADATA, 'fp_filters': ''}, 'FP', symbol_text=_symbol(version='1'))
    assert _codes(findings) == ['symbol_version', 'fp_filters_empty']
    assert k.check_all(GOOD_METADATA, 'FP') == []
    both = k.check_all(GOOD_METADATA, 'FP', symbol_text=_symbol(), footprint_text=_footprint(pad=''))
    assert _codes(both) == ['footprint_pads']


def test_check_all_drops_the_same_finding_reported_twice():
    findings = k.check_all(GOOD_METADATA, 'Lib:FP', footprint_text='(footprint "Lib:FP" (version 1) (attr smd) (descr "d")'
                           ' (property "Reference" "REF**" (layer "F.SilkS")) (property "Value" "FP" (layer "F.Fab"))'
                           ' (fp_rect (layer "F.CrtYd")) (fp_rect (layer "F.Fab")) (pad "1" smd rect (layers "F.Cu")))')
    assert _codes(findings) == ['footprint_name_illegal']
