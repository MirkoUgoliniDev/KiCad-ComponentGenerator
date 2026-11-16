"""@brief Board edge names in castellated-module messages follow the interface language (2026-11-15).

EDGE_LABELS holds Italian SOURCE words; `edge_label` translates them when a
message is composed. Positive: an English interface gets «left». Negative:
with no translator the Italian source stays, and an unknown key is returned
as given (never invented).
"""
from kicadgen.generators import castellated_module as cm
from kicadgen.services import messages
from kicadgen.services.i18n import DEFAULT_TRANSLATIONS


def _english(key, default=None):
    """A catalog lookup in English, as `I18n.raw` would answer."""
    entry = DEFAULT_TRANSLATIONS.get(key)
    return entry["en"] if entry else default


def test_every_edge_label_is_translated_in_english(monkeypatch):
    """@brief Positive: each edge reads in English, none in Italian."""
    monkeypatch.setattr(messages, "_translator", _english)
    assert [cm.edge_label(edge) for edge in ("left", "right", "top", "bottom")] == \
        ["left", "right", "top", "bottom"]


def test_italian_source_and_unknown_keys(monkeypatch):
    """@brief Negative: no translator keeps the source; an unknown key is never invented."""
    monkeypatch.setattr(messages, "_translator", None)
    assert cm.edge_label("left") == "sinistro"
    assert cm.edge_label("diagonal") == "diagonal" and cm.edge_label("diagonal", "") == ""


def test_every_edge_label_has_a_catalog_entry():
    """@brief Each Italian source word is a catalog key in four languages."""
    for source in cm.EDGE_LABELS.values():
        entry = DEFAULT_TRANSLATIONS["ui:" + source]
        assert all(entry.get(lang) for lang in ("it", "en", "fr", "de"))
