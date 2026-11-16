"""@brief Pad lists in extraction messages speak the interface language, both tails (review 2026-11-15, round 2).

`_bounded_pad_list` reuses the footprint note's compression and bound, but
its two tails («e altri N», «N senza numero») are the interface's: the
footprint note's fixed file language must never leak into a message.
Positive: in English both tails are English. Negative: the file note's own
wording («more», «unnumbered» as written in the .kicad_mod) never appears in
an Italian message.
"""
from kicadgen.generators.package import ESTIMATED_PADS_MORE, ESTIMATED_PADS_UNNAMED, estimated_pad_list
from kicadgen.services import datasheet_extractor as extractor
from kicadgen.services import messages
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS

#: Enough numbered pads to overflow the listed bound, plus two unnumbered.
NUMBERS = ['1', '2', '3', '', ''] + [str(n) for n in range(10, 80, 2)]


def _english(key, default=None):
    """A catalog lookup in English, as `I18n.raw` would answer."""
    entry = DEFAULT_TRANSLATIONS.get(key)
    return entry["en"] if entry else default


def test_italian_message_uses_the_interface_tails_not_the_file_note(monkeypatch):
    """@brief Negative: no file-language tail in an Italian message."""
    monkeypatch.setattr(messages, "_translator", None)
    text = extractor._bounded_pad_list(NUMBERS)
    assert "e altri" in text and "2 senza numero" in text
    file_note = estimated_pad_list(NUMBERS)
    tails = (ESTIMATED_PADS_MORE.split("{count}")[-1].strip(), ESTIMATED_PADS_UNNAMED.split("{count}")[-1].strip())
    for tail in tails:
        if tail and tail not in ("e altri", "senza numero"):
            assert tail in file_note and tail not in text


def test_english_message_has_english_tails(monkeypatch):
    """@brief Positive: both tails follow the interface language."""
    monkeypatch.setattr(messages, "_translator", _english)
    text = extractor._bounded_pad_list(NUMBERS)
    assert "more" in text and "2 without a number" in text
    assert "e altri" not in text and "senza numero" not in text
