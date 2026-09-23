"""@brief GUI contract of the per-side contact style of a castellated module.

L'utente vede sulla scheda che contatto ha ogni lato (mezzo foro tagliato dal
bordo, foro passante chiuso, oppure entrambi) e deve poterlo correggere e
rigenerare SENZA pagare un'altra analisi AI. Qui si verifica che la scelta:
compaia nella tabella lati con le tre voci e il suggerimento per
riconoscerle; arrivi al `CastellatedSide` consegnato alla generazione;
venga letta dall'estrazione quando c'e' e lasci il predefinito quando manca;
sopravviva ai punti in cui i lati vengono serializzati e riletti; sia
raggiungibile dal tab 3D in modalita' base; funzioni sia con il generatore
che conosce gia' il campo sia con quello che non lo conosce ancora.
"""
from dataclasses import dataclass, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Sequence

import pytest

from test_photo_inventory_gui import window, gui  # noqa: F401 - fixture condivisa
from kicadgen.cad3d import step_generator as step
from kicadgen.generators import castellated_module
from kicadgen.services import openrouter
from kicadgen.services.datasheet_extractor import ExtractedParams


STYLE_COLUMN = 6


class PaidCallAttempted(AssertionError):
    """Raised by the OpenRouter stubs: this path must stay free of AI calls."""


@pytest.fixture(autouse=True)
def no_ai_no_freecad(monkeypatch):
    """@brief Ogni chiamata AI o FreeCAD e' un fallimento di questo file.

    Le stub SOLLEVANO invece di registrare: un'eccezione non puo' essere
    ignorata da un `except` generico a monte senza lasciare traccia nel
    contatore, ed e' il contatore che i test controllano.
    """
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise PaidCallAttempted("chiamata a pagamento nel percorso gratuito")

    monkeypatch.setattr(openrouter, "call_tool", forbidden)
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(step, "_run_freecad", forbidden)
    return calls


@dataclass
class SideWithoutStyle:
    """The generator's side as it was before the contract landed."""

    edge: str
    count: int
    pitch: float
    first_pin: int = 1
    reverse: bool = False
    offset: float = 0.0
    pad_length: Optional[float] = None
    pad_width: Optional[float] = None
    pin_numbers: Optional[Sequence[str]] = None

    numbers = castellated_module.CastellatedSide.numbers
    # NON annotato, quindi non e' un campo e la firma non lo accetta: e' cio'
    # che conta per la GUI. L'attributo di classe serve solo al resto del
    # generatore installato (che puo' gia' leggerlo in `validate`): un
    # generatore che non conosce lo stile produce la geometria di sempre.
    contact_style = "half_and_through"


@dataclass
class SideWithStyle(SideWithoutStyle):
    """The generator's side once the contract has landed."""

    contact_style: str = "half_and_through"


@pytest.fixture(params=["generator_has_field", "generator_lacks_field"])
def generator_state(request, monkeypatch):
    """@brief Simula entrambi gli ordini di arrivo delle due modifiche.

    @return True quando il `CastellatedSide` installato accetta `contact_style`.
    """
    has_field = request.param == "generator_has_field"
    monkeypatch.setattr(gui, "CastellatedSide", SideWithStyle if has_field else SideWithoutStyle)
    if has_field:
        monkeypatch.setattr(castellated_module, "CONTACT_HALF_HOLE", "half_hole", raising=False)
        monkeypatch.setattr(castellated_module, "CONTACT_THROUGH_HOLE", "through_hole", raising=False)
        monkeypatch.setattr(castellated_module, "CONTACT_HALF_AND_THROUGH", "half_and_through",
                            raising=False)
        monkeypatch.setattr(castellated_module, "CONTACT_STYLES",
                            ("half_hole", "through_hole", "half_and_through"), raising=False)
    else:
        # Le costanti spariscono solo dalla vista della GUI: toglierle dal
        # modulo vero romperebbe il `validate` gia' installato, che le usa, e
        # simulerebbe un generatore a meta' che non esiste in nessun ordine.
        monkeypatch.setattr(gui, "_castellated_module", SimpleNamespace())
    return has_field


@pytest.fixture
def real_generator_with_field(monkeypatch):
    """@brief Il generatore vero se ha gia' il campo, altrimenti la sua simulazione."""
    if "contact_style" not in {f.name for f in fields(castellated_module.CastellatedSide)}:
        monkeypatch.setattr(gui, "CastellatedSide", SideWithStyle)


def castellated(window):
    """@brief Accende il modulo castellated: nascono i due lati predefiniti."""
    window.group_castellated.setChecked(True)
    assert window.table_sides.rowCount() == 2
    return window


def style_combo(window, row):
    return window.table_sides.cellWidget(row, STYLE_COLUMN)


def choose(combo, style):
    index = combo.findData(style)
    assert index >= 0, style
    combo.setCurrentIndex(index)


def extracted(sides):
    return ExtractedParams(
        name="", description="", tags="", confidence="medium", notes="",
        layout="castellated", castellated_sides=sides, body_length_mm=40, body_width_mm=20,
        rows=2, cols=4, pitch_mm=2.54, row_spacing_mm=2.54, pad_diameter_mm=1.7,
        drill_mm=1.0, photo_source="", components_3d=[])


def side(edge, first_pin, **extra):
    return dict(edge=edge, count=3, pitch_mm=2.54, offset_mm=0.0, first_pin=first_pin,
                reverse=False, **extra)


# ── La colonna ─────────────────────────────────────────────────────────


def test_column_offers_the_three_styles_with_the_telling_apart_tooltip(window):
    castellated(window)
    header = window.table_sides.horizontalHeaderItem(STYLE_COLUMN)
    assert header.text() == window.i18n.text("Tipo contatto")
    combo = style_combo(window, 0)
    assert [combo.itemData(i) for i in range(combo.count())] == [
        "half_hole", "through_hole", "half_and_through"]
    assert [combo.itemText(i) for i in range(combo.count())] == [
        window.i18n.text(gui.CONTACT_STYLE_LABELS[s])
        for s in ("half_hole", "through_hole", "half_and_through")]
    # Il suggerimento deve dire COME distinguerli, non solo elencarli: il
    # bordo che taglia il foro contro il materiale fra foro e bordo.
    for tip in (combo.toolTip(), header.toolTip()):
        assert "tagliato dal bordo" in tip
        assert "materiale della scheda" in tip
    # Negativo: il predefinito e' il comportamento di oggi, non un'altra voce.
    assert combo.currentData() == "half_and_through"


# ── Tabella -> generazione ─────────────────────────────────────────────


def test_choosing_through_hole_reaches_the_side_given_to_generation(
        window, generator_state, monkeypatch):
    castellated(window)
    logs = []
    monkeypatch.setattr(window, "_append_log", logs.append)
    combo = style_combo(window, 1)
    # Con o senza le costanti del generatore la combo offre gli stessi valori.
    assert [combo.itemData(i) for i in range(combo.count())] == [
        "half_hole", "through_hole", "half_and_through"]
    assert gui.contact_styles() == ("half_hole", "through_hole", "half_and_through")
    choose(combo, "through_hole")
    sides = window._sides_from_table()
    params = window._current_castellated_params()
    if generator_state:
        assert [s.contact_style for s in sides] == ["half_and_through", "through_hole"]
        assert [s.contact_style for s in params.sides] == ["half_and_through", "through_hole"]
        assert not any("non conosce ancora" in line for line in logs)
    else:
        # Senza il campo non si passa nulla (niente TypeError), il generatore
        # resta sulla geometria di sempre e la rinuncia e' detta nel log: la
        # combo mostra una scelta che non arrivera'.
        assert "contact_style" not in {f.name for f in fields(SideWithoutStyle)}
        assert [s.contact_style for s in sides] == ["half_and_through", "half_and_through"]
        assert any("non conosce ancora il tipo di contatto" in line for line in logs)
    # La scelta dell'utente resta comunque in tabella e nei dati serializzati.
    assert [d["contact_style"] for d in window._serialised_sides()] == [
        "half_and_through", "through_hole"]


def test_footprint_generation_receives_the_chosen_style(
        window, real_generator_with_field, monkeypatch):
    castellated(window)
    captured = []
    monkeypatch.setattr(gui.GenerateWorker, "start", lambda self: captured.append(self))
    window.group_3d.setChecked(False)
    choose(style_combo(window, 0), "through_hole")
    window._generate()
    assert len(captured) == 1
    assert [s.contact_style for s in captured[0].params.sides] == [
        "through_hole", "half_and_through"]


# ── Estrazione -> tabella ──────────────────────────────────────────────


def test_extraction_declaring_the_style_sets_the_combo(window):
    window._apply_extracted_castellated(extracted([
        side("left", 1, contact_style="through_hole"),
        side("right", 4, contact_style="half_hole")]))
    assert window._table_contact_styles() == ["through_hole", "half_hole"]


def test_extraction_without_the_key_keeps_the_default(window, monkeypatch):
    logs = []
    monkeypatch.setattr(window, "_append_log", logs.append)
    window._apply_extracted_castellated(extracted([side("left", 1), side("right", 4)]))
    assert window._table_contact_styles() == ["half_and_through", "half_and_through"]
    # Il log dice che e' un predefinito, non una lettura della scheda.
    assert any("predefinito" in line for line in logs)
    assert not any("sconosciuto" in line for line in logs)


def test_unknown_declared_style_is_not_guessed_into_another_one(window, monkeypatch):
    logs = []
    monkeypatch.setattr(window, "_append_log", logs.append)
    window._apply_extracted_castellated(extracted([
        side("left", 1, contact_style="castellated_via"), side("right", 4)]))
    assert window._table_contact_styles() == ["half_and_through", "half_and_through"]
    assert any("sconosciuto" in line and "castellated_via" in line for line in logs)


# ── Serializzazione e ripristino ───────────────────────────────────────


def test_relaunch_base_round_trips_the_style_through_the_table(window):
    castellated(window)
    choose(style_combo(window, 0), "half_hole")
    choose(style_combo(window, 1), "through_hole")
    window._last_extracted = None
    bundle = SimpleNamespace(body_length_mm=40.0, body_width_mm=20.0, photo_path=None)
    base = window._relaunch_base_params(bundle)
    assert [s["contact_style"] for s in base.castellated_sides] == ["half_hole", "through_hole"]
    window.table_sides.setRowCount(0)
    window._apply_extracted_castellated(base)
    assert window._table_contact_styles() == ["half_hole", "through_hole"]


def test_old_saved_sides_without_the_key_load_with_the_default(window):
    castellated(window)
    choose(style_combo(window, 0), "through_hole")
    old_record = [side("left", 1), side("right", 4)]
    assert all("contact_style" not in s for s in old_record)
    window._apply_extracted_castellated(extracted(old_record))
    # Il record vecchio sostituisce la tabella: nessun residuo della scelta
    # precedente, nessuna deduzione, il comportamento di sempre.
    assert window._table_contact_styles() == ["half_and_through", "half_and_through"]


def test_inventory_relaunch_keeps_the_style_corrected_after_the_extraction(window):
    window._apply_extracted_castellated(extracted([side("left", 1), side("right", 4)]))
    choose(style_combo(window, 1), "through_hole")
    earlier = [side("left", 1), side("right", 4)]
    carried = window._sides_with_table_styles(earlier)
    assert [s["contact_style"] for s in carried] == ["half_and_through", "through_hole"]
    assert all("contact_style" not in s for s in earlier), "l'estrazione non va modificata"


def test_inventory_relaunch_does_not_pair_ambiguous_sides(window):
    """@brief Negativo: due righe sullo stesso bordo non si abbinano a indovinare."""
    window._apply_extracted_castellated(extracted([side("left", 1), side("left", 4)]))
    choose(style_combo(window, 1), "through_hole")
    earlier = [side("left", 1), side("left", 4)]
    assert window._sides_with_table_styles(earlier) == earlier


# ── Modalita' base: dal tab 3D ─────────────────────────────────────────


def test_basic_mode_reaches_the_style_from_the_3d_tab(window):
    assert not window._advanced_user()
    assert not window.tabs.isTabVisible(1), "la tabella lati e' nascosta in modalita' base"
    assert window.contact_style_bar_3d.isHidden(), "senza modulo castellated non serve"
    castellated(window)
    assert not window.contact_style_bar_3d.isHidden()
    assert window.model_3d_action_bar.isAncestorOf(window.combo_contact_style_3d)
    assert "materiale della scheda" in window.combo_contact_style_3d.toolTip()
    window._last_mesh_path = Path("previous.preview.json")
    revision = window._model_revision
    choose(window.combo_contact_style_3d, "through_hole")
    assert window._table_contact_styles() == ["through_hole", "through_hole"]
    assert window._model_revision > revision and window._last_mesh_path is None
    window.group_castellated.setChecked(False)
    assert window.contact_style_bar_3d.isHidden()


def test_3d_combo_shows_mixed_sides_without_choosing_for_the_user(window):
    castellated(window)
    choose(style_combo(window, 0), "half_hole")
    combo = window.combo_contact_style_3d
    assert combo.currentData() is None
    assert not combo.model().item(combo.currentIndex()).isEnabled()
    # Negativo: selezionare la voce informativa non tocca i lati.
    window._on_contact_style_all_changed(combo.currentIndex())
    assert window._table_contact_styles() == ["half_hole", "half_and_through"]
    choose(style_combo(window, 0), "half_and_through")
    assert combo.currentData() == "half_and_through"


# ── Il percorso gratuito ───────────────────────────────────────────────


def test_style_change_and_3d_regeneration_make_no_ai_call(
        window, real_generator_with_field, no_ai_no_freecad, monkeypatch, tmp_path):
    castellated(window)
    window.group_3d.setChecked(True)
    started = []
    monkeypatch.setattr(gui.Cad3DWorker, "start", lambda self: started.append(self))
    received = []

    def generator(params, body, project_dir, **kwargs):
        received.append(params)
        return {"step_path": tmp_path / "x.step"}

    monkeypatch.setattr(gui, "generate_castellated_3d_model", generator)
    window._last_mesh_path = Path("previous.preview.json")
    choose(window.combo_contact_style_3d, "through_hole")
    assert window._last_mesh_path is None, "la vecchia anteprima non va lasciata a video"
    window._regenerate_3d()
    assert len(started) == 1, window.label_3d_status.text()
    worker = started[0]
    # Stessa chiamata che fa Cad3DWorker.run, eseguita qui senza thread.
    gui._generate_3d(worker.params, worker.body, tmp_path, worker.fillets, worker.face_texts)
    assert [s.contact_style for s in received[0].sides] == ["through_hole", "through_hole"]
    assert no_ai_no_freecad == [], "nessuna chiamata AI o FreeCAD nel percorso gratuito"
    window._on_regen_3d_finished()


# ── La distanza fra le file arriva al generatore ──────────────────────────

def _through_hole_board(spacing):
    """@brief La scheda dell'utente in piccolo: due file opposte a foro passante."""
    params = extracted([side("top", 1, contact_style="through_hole"),
                        side("bottom", 4, contact_style="through_hole")])
    params.body_length_mm, params.body_width_mm = 40.0, 25.4
    params.row_spacing_mm = spacing
    return params


def test_declared_row_spacing_places_the_through_holes_as_on_the_board(window):
    """@brief Positivo: file a 22,86 mm su un corpo di 25,4 mm -> fori a 1,27 mm dal bordo.

    E' la quota della scheda dell'utente. Senza il collegamento la GUI non
    passava la distanza fra le file e il generatore ripiegava sulla stima per
    difetto (raggio della corona + parete, circa 1,05 mm): i fori finivano
    piu' vicini al bordo che nella foto.
    """
    window._on_extracted(_through_hole_board(22.86))
    module = window._current_castellated_params()
    assert module.row_spacing == pytest.approx(22.86)
    inset = castellated_module.through_hole_inset_from_rows(module.body_width, module.row_spacing)
    assert inset == pytest.approx(1.27)


@pytest.mark.parametrize("spacing", [None, 0, -3.0, float("nan"), True])
def test_an_unusable_row_spacing_is_not_passed_on(window, spacing):
    """@brief Negativo: una quota assente o assurda non arriva al generatore.

    Al suo posto resta la stima dichiarata del generatore, che lo scrive nel
    log; un numero inventato qui sarebbe peggio di una stima che si dichiara.
    """
    window._on_extracted(_through_hole_board(spacing))
    assert window._current_castellated_params().row_spacing is None


def test_a_new_extraction_forgets_the_previous_board_spacing(window):
    """@brief Negativo: la quota di una scheda precedente non passa a quella nuova."""
    window._on_extracted(_through_hole_board(22.86))
    assert window._current_castellated_params().row_spacing == pytest.approx(22.86)
    window._on_extracted(_through_hole_board(None))
    assert window._current_castellated_params().row_spacing is None


def test_the_inventory_relaunch_keeps_the_declared_spacing(window):
    """@brief Il rilancio rientra dall'estrazione: senza la quota i fori tornerebbero alla stima."""
    window._on_extracted(_through_hole_board(22.86))
    rebuilt = window._relaunch_base_params(
        SimpleNamespace(body_length_mm=40.0, body_width_mm=25.4, photo_path=None))
    assert rebuilt.row_spacing_mm == pytest.approx(22.86)
