"""@brief I motivi dei candidati STEP nella lingua dell'interfaccia (utente, 2026-09-25).

I report salvati tengono i motivi in inglese, come li ha sempre scritti il
matching: la traduzione avviene quando si mostrano, cosi' valgono anche per i
report gia' su disco. Nessuna chiamata di rete, nessun file vero.
"""
import pytest

from kicadgen.cad3d import kicad_models as km
from kicadgen.services.messages import set_translator


@pytest.fixture
def english(tmp_path):
    from kicadgen.services.analysis_profiles import ProfileStore
    from kicadgen.services.i18n import I18n
    i18n = I18n(ProfileStore(tmp_path / "p.sqlite3"), "en")
    set_translator(i18n.raw)
    yield
    set_translator(None)


def test_every_fixed_reason_the_matching_writes_is_translated(english):
    """Ogni motivo fisso scritto dal matching (preso dal codice) ha la sua traduzione."""
    for stored in km._REASON_SOURCES:
        shown = km.localized_reason(stored)
        assert shown and shown != km._REASON_SOURCES[stored], stored
    assert km.localized_reason("Family and dimension discovery") == "Found by family and dimensions"


def test_the_italian_interface_shows_italian_reasons():
    assert km.localized_reason("Family and dimension discovery") == "Trovato per famiglia e misure"


@pytest.mark.parametrize("stored, shown", [
    ("Compatible body dimensions", "Misure del corpo compatibili"),
    ("Compatible total dimensions", "Ingombro totale compatibile"),
    (r"Compatible piazzole C:\KiCad\footprints\Package_TO_SOT_SMD.pretty\SOT-223-3_TabPin2.kicad_mod dimensions",
     "Misure compatibili con le piazzole di SOT-223-3_TabPin2.kicad_mod"),
])
def test_the_dimension_reason_names_its_basis_without_the_full_path(stored, shown):
    assert km.localized_reason(stored) == shown


@pytest.mark.parametrize("other", ["Gia' accettato da te in una scheda precedente", "", "qualcosa"])
def test_a_reason_the_matching_did_not_write_is_left_alone(other):
    """Negativo: i motivi gia' italiani (archivio) o sconosciuti restano come sono."""
    assert km.localized_reason(other) == other


def test_the_reasons_written_by_the_code_are_all_known():
    """Ogni motivo letterale in inglese nel matching e' nella tabella: un motivo
    nuovo aggiunto senza traduzione fa fallire questo test."""
    import ast
    from pathlib import Path
    source = Path(km.__file__).read_text(encoding="utf-8")
    known = set(km._REASON_SOURCES)
    strings = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "reasons" and isinstance(value, ast.List):
                    strings |= {e.value for e in ast.walk(value) if isinstance(e, ast.Constant)
                                and isinstance(e.value, str)}
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Subscript) and isinstance(t.value, ast.Subscript)
                and getattr(t.value.slice, "value", None) == "reasons" for t in node.targets):
            strings |= {e.value for e in ast.walk(node.value) if isinstance(e, ast.Constant)
                        and isinstance(e.value, str)}
    literal = {s for s in strings if s and not s.startswith("Compatible ") and s[0].isupper()}
    assert literal <= known, literal - known
