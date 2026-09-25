"""@brief `tx` and `I18n.tr` never raise on a value that rejects its format spec (D10)."""
from kicadgen.services import messages
from kicadgen.services.analysis_profiles import ProfileStore
from kicadgen.services.i18n import I18n


def test_tx_falls_back_when_a_value_rejects_the_format_spec(monkeypatch):
    monkeypatch.setattr(messages, "_translator", None)
    # None does not accept «.2f»: TypeError, formerly propagated to the caller.
    assert messages.tx("costo {a:.2f}", a=None) == "costo {a:.2f}"


def test_tx_still_formats_a_valid_value(monkeypatch):
    """Negative case: a value that accepts its spec is formatted, not left raw."""
    monkeypatch.setattr(messages, "_translator", None)
    assert messages.tx("costo {a:.2f}", a=1.5) == "costo 1.50"


def test_tx_uses_the_source_when_only_the_translation_is_broken(monkeypatch):
    """A translation whose spec rejects the value falls back to the formatted source."""
    monkeypatch.setattr(messages, "_translator",
                        lambda key, default=None: "cost {a:.2f}")
    assert messages.tx("costo {a}", a=None) == "costo None"


def test_i18n_tr_returns_the_raw_text_on_a_type_error(tmp_path):
    i18n = I18n(ProfileStore(tmp_path / "profiles.sqlite3"), "it")
    assert i18n.tr("fixD.missing", default="valore {a:.2f}", a=None) == "valore {a:.2f}"
    assert i18n.tr("fixD.missing", default="valore {a:.2f}", a=2) == "valore 2.00"
