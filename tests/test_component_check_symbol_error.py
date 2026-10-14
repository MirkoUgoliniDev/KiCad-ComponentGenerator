"""@brief The component check shows a symbol generator error in the interface language."""
from kicadgen.generators.symbol import SymbolError, _label, _message


def _english(monkeypatch):
    """Make services.messages.tx answer from a small English catalogue."""
    from kicadgen.services import messages
    catalogue = {"{field}: non può essere vuoto.": "{field}: must not be empty (EN)",
                 "Nome del simbolo": "Symbol name (EN)"}
    real_tx = messages.tx

    def english_tx(template, **values):
        text = catalogue.get(template)
        if text is None:
            return real_tx(template, **values)
        return text.format(**values)

    monkeypatch.setattr(messages, "tx", english_tx)


def _run_check(tmp_path, monkeypatch, error):
    """Run verify_component with the symbol build failing with `error`."""
    from kicadgen.services import component_check
    def fail(_rows):
        raise error
    monkeypatch.setattr(component_check, "symbol_inputs", fail, raising=False)
    import kicadgen.generators.symbol as symbol
    monkeypatch.setattr(symbol, "symbol_inputs", fail)
    results = component_check.verify_component(tmp_path, "Board", [{"pin": "1", "name": "A"}])
    return [r.detail for r in results if r.status == "ERROR"]


def test_a_symbol_error_is_shown_in_the_active_language(tmp_path, monkeypatch):
    """Positive: with an English catalogue the check shows the English text,
    not the Italian template (cycle-3 review, 2026-10-14)."""
    _english(monkeypatch)
    error = SymbolError(_message("{field}: non può essere vuoto.", "{field}: must not be empty",
                                 field=_label("Nome del simbolo", "Symbol name")))
    details = _run_check(tmp_path, monkeypatch, error)
    assert "Symbol name (EN): must not be empty (EN)" in details, details
    assert not any("non può" in d for d in details)


def test_an_ordinary_exception_keeps_its_own_text(tmp_path, monkeypatch):
    """Negative: an exception without a translatable text is shown as str()."""
    _english(monkeypatch)
    details = _run_check(tmp_path, monkeypatch, ValueError("plain message"))
    assert "plain message" in details, details
