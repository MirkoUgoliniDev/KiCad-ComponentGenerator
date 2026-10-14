"""@brief Source of the lead count a leaded family is derived from (user decision D3, 2026-10-14).

When both crop readings agree on a COMPLETE per-side count, that count (already
written into the row by the terminal adoption) is the one the package family
comes from; a partial or unadopted count, and an outvoted reading, keep the
inventory count. Every test is offline: `call` is a scripted fake.
"""
import pytest

pytest.importorskip("cv2")

from kicadgen.services import photo_crop_reading as crop
from kicadgen.services.messages import tx

from test_photo_crop_reading import Script, _obs, _read, _recorded_two_terminal, _sot_row

#: The envelope _read measures for _sot_row (3.09 x 2.29 mm, photo tolerance).
SOT_ENVELOPE = (3.09016, 2.2898)


def _agreed(row, answer):
    """Two identical crop answers on `row`: an agreed reading."""
    _read([row], Script(answer, dict(answer)))
    assert row['_crop_reading'] == 'agreed'
    return row


def test_family_lead_count_comes_from_the_standard_tables_only():
    assert crop._family_lead_count('SOT-23') == 3
    assert crop._family_lead_count('sot143') == 4
    assert crop._family_lead_count('SOIC-8 (narrow)') == 8
    assert crop._family_lead_count('SOT-223') == 3
    # No count is assumed for names outside the tables: they never contradict.
    for name in ('SMA', 'QFN-16', 'SC-70', '', None):
        assert crop._family_lead_count(name) is None


def test_an_agreed_complete_count_gives_the_family_when_the_measure_rejects_the_name():
    """Positive: inventory 3 leads, both crops count 2 + 2 on a SOT-23 sized body."""
    row = _agreed(_sot_row(), _obs(2, 2, 0, 0, marking='A15HB'))
    assert row['lead_count'] == 4 and row['lead_count_complete'] is True
    assert row['_lead_count_inventory'] == 3
    assert row['package'] == 'SOT-143' and row['_package_inventory'] == 'SOT-223'
    assert row['package_source'] == crop.PACKAGE_SOURCE_COUNT_AND_MEASURE
    assert row['package_confidence'] == 'medium'
    assert row['_review_reason'].startswith(tx(
        "Package «{declared}» incompatibile con la misura {long:.2f} x {short:.2f} mm: "
        "famiglia «{family}» derivata da {n} piedini contati da entrambe le letture del "
        "ritaglio e misura; da verificare.", declared='SOT-223', long=SOT_ENVELOPE[0],
        short=SOT_ENVELOPE[1], family='SOT-143', n=4))


def test_an_agreed_complete_count_rejects_a_name_with_another_standard_count():
    """The measure fits «SOT-23», but the two readings saw four leads: the name decays."""
    row = _agreed(_sot_row(package='SOT-23'), _obs(2, 2, 0, 0, marking='A15HB'))
    assert row['package'] == 'SOT-143' and row['_package_inventory'] == 'SOT-23'
    assert row['package_source'] == crop.PACKAGE_SOURCE_COUNT_AND_MEASURE
    assert row['_review_reason'].startswith(tx(
        "Package «{declared}» incompatibile con i {n} piedini contati da entrambe le "
        "letture del ritaglio: famiglia «{family}» derivata da quel conteggio e dalla "
        "misura; da verificare.", declared='SOT-23', family='SOT-143', n=4))
    # Negative: a name whose standard count equals the agreed one stays untouched.
    kept = _agreed(_sot_row(package='SOT-143'), _obs(2, 2, 0, 0, marking='A15HB'))
    assert kept['package'] == 'SOT-143' and '_package_inventory' not in kept
    assert 'package_source' not in kept
    assert 'incompatibile' not in kept.get('_review_reason', '')


def test_a_complete_count_that_fits_no_family_leaves_the_package_empty_for_review():
    """Seven leads (4 + 3) on a SOT-23 sized body: no registered family, no guess."""
    row = _agreed(_sot_row(), _obs(4, 3, 0, 0, marking='A15HB'))
    assert row['lead_count'] == 7 and row['lead_count_complete'] is True
    assert row['package'] == '' and row['_package_inventory'] == 'SOT-223'
    assert row['package_confidence'] == 'low'
    assert row['package_source'] == crop.PACKAGE_SOURCE_NAME_REJECTED
    assert row['_uncertain'] is True
    assert row['_review_reason'].startswith(tx(
        "Package «{declared}» incompatibile con la misura {long:.2f} x {short:.2f} mm: "
        "nessuna famiglia univoca da {n} piedini contati da entrambe le letture del "
        "ritaglio e misura ({families}); package lasciato vuoto, da verificare.",
        declared='SOT-223', long=SOT_ENVELOPE[0], short=SOT_ENVELOPE[1], n=7, families='-'))


def test_without_an_adopted_complete_count_the_inventory_count_is_used_as_before():
    """Negatives: hidden leads (nothing adopted) and a partial count keep the inventory's."""
    inventory_text = tx(
        "Package «{declared}» incompatibile con la misura {long:.2f} x {short:.2f} mm: "
        "famiglia «{family}» derivata da {n} piedini e misura; da verificare.",
        declared='SOT-223', long=SOT_ENVELOPE[0], short=SOT_ENVELOPE[1], family='SOT-23', n=3)
    hidden = _obs(None, None, None, None, metal_contacts_total=0, marking='A15HB')
    row = _agreed(_sot_row(), hidden)
    assert row['package'] == 'SOT-23' and row['lead_count'] == 3
    assert row['_review_reason'].startswith(inventory_text)
    # A partial count adopted by an older payload: the row carries four leads
    # but not complete, the inventory said three. No crop count is passed, so
    # the inventory count decides -- the row's lead_count is NOT trusted.
    partial = _sot_row(lead_count=4, lead_count_complete=False, _lead_count_inventory=3)
    envelope = SOT_ENVELOPE + (0.17,)
    assert crop._reconcile_declared_package(partial, envelope, None) is True
    assert partial['package'] == 'SOT-23'
    assert partial['_review_reason'].startswith(inventory_text)
    # The same row's complete-looking flag alone is not a crop count either:
    # the inventory sets lead_count_complete too.
    flagged = _sot_row(lead_count=4, lead_count_complete=True, _lead_count_inventory=3)
    crop._reconcile_declared_package(flagged, envelope, None)
    assert flagged['package'] == 'SOT-23'
    # Without crop count a measure-compatible name is never touched for its count.
    fine = _sot_row(package='SOT-23', lead_count=4)
    assert crop._reconcile_declared_package(fine, envelope, None) is False
    assert fine['package'] == 'SOT-23'


def test_an_outvoted_reading_keeps_the_inventory_count():
    """The outvoted branch adopts nothing: the family still comes from the inventory's 3 leads."""
    row = _sot_row()
    _read([row], Script(_recorded_two_terminal(), _recorded_two_terminal()))
    assert row['_crop_reading'] == 'outvoted'
    assert row['package'] == 'SOT-23' and row['lead_count'] == 3
    assert row['_review_reason'].startswith(tx(
        "Package «{declared}» incompatibile con la misura {long:.2f} x {short:.2f} mm: "
        "famiglia «{family}» derivata da {n} piedini e misura; da verificare.",
        declared='SOT-223', long=SOT_ENVELOPE[0], short=SOT_ENVELOPE[1], family='SOT-23', n=3))


@pytest.mark.parametrize('declared', ['SOT-223', 'SOT-23', 'SOT-143', 'SOIC-8', 'SOT-23-6'])
@pytest.mark.parametrize('leads', [(2, 1, 0, 0), (2, 2, 0, 0), (3, 3, 0, 0), (4, 3, 0, 0),
                                   (4, 4, 0, 0), (1, 1, 1, 1), (0, 3, 0, 0)])
@pytest.mark.parametrize('size', [(3.09016, 2.2898), (6.0, 4.9), (6.5, 7.0)])
def test_after_reconciliation_a_family_never_contradicts_a_complete_lead_count(declared, leads, size):
    """Invariant of D3 over names, counts and body sizes: package family and lead_count agree."""
    row = _sot_row(package=declared, length_mm=size[0], width_mm=size[1])
    contacts = sum(leads)
    _read([row], Script(_obs(*leads, metal_contacts_total=contacts, marking='A15HB'),
                        _obs(*leads, metal_contacts_total=contacts, marking='A15HB')))
    # Every case reaches the adoption: the invariant is checked, never skipped.
    assert row['_crop_reading'] == 'agreed' and row['lead_count_complete'] is True
    assert row['lead_count'] == contacts
    standard = crop._family_lead_count(row.get('package'))
    if row.get('package') and standard is not None:
        assert standard == row['lead_count'], (row['package'], row['lead_count'])
