import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication, QWidget
from kicadgen.gui.analysis_overlay import (TOOLS, AnalysisOverlay, AnalysisSpinner,
                                          _STATUS_CHAR_LIMIT, _STATUS_LINES,
                                          is_machine_line)


@pytest.fixture
def overlay():
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.resize(900, 650)
    widget = AnalysisOverlay(parent)
    parent.show()
    widget.start()
    app.processEvents()
    yield widget
    widget.stop()
    parent.close()


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_each_tool_is_named_and_drawn_differently(overlay, tool):
    """Il colore da solo non basta: ogni strumento ha anche una forma sua."""
    overlay.set_tool(tool, "dettaglio")
    name, color, glyph = TOOLS[tool]
    assert overlay.tool.isVisibleTo(overlay)
    assert overlay.tool.text() == f"{name} · dettaglio"
    assert (overlay.spinner.glyph, overlay.spinner.color) == (glyph, color)
    assert len({shape for _, _, shape in TOOLS.values()}) == len(TOOLS)


def test_an_unknown_tool_leaves_the_bare_ring_instead_of_failing(overlay):
    """Una fase nuova non deve poter far cadere un'analisi in corso."""
    overlay.set_tool("strumento-mai-visto")
    assert not overlay.tool.isVisibleTo(overlay)
    assert overlay.spinner.glyph == ""


def test_percentage_only_when_a_real_count_backs_it(overlay):
    overlay.set_progress(12, 45, "componenti")
    assert overlay.progress_row.isVisibleTo(overlay)
    assert overlay.progress.value() == 12 and overlay.progress.maximum() == 45
    assert overlay.progress_label.text() == "27% · 12/45 componenti"
    for done, total in ((None, None), (3, 0), (3, None), (7, 5), (-1, 5)):
        overlay.set_progress(done, total)
        assert not overlay.progress_row.isVisibleTo(overlay)


def test_service_markers_stay_in_the_log_and_off_the_card(overlay):
    """Sulla scheda occupavano il posto di cio' che stava accadendo."""
    overlay.set_status("Generazione del footprint KiCad…")
    for marker in ("AI_CALL_PROMPT_END id=1 model=anthropic/claude-opus-5",
                   "KICADGEN_IMPORT_OK: ESP32-S3-WROOM-1U.STEP; parts=231",
                   "AI_CALL_START id=2",
                   # Chi inoltra l'uscita di freecadcmd le antepone un prefisso,
                   # e con l'ancora iniziale passavano tutte.
                   "FreeCAD: KICADGEN_FCSTD_OK:C:\\Lavori\\TEST.FCStd",
                   "FreeCAD: KICADGEN_WARN:componente U3: package sconosciuto"):
        overlay.set_status(marker)
        assert overlay.status.text() == "Generazione del footprint KiCad…"
    # Una frase che comincia per maiuscolo non e' un marcatore: resta. E nemmeno
    # una parola sola tutta maiuscola, che nei log di questo progetto introduce
    # un avviso scritto per chi guarda.
    for human in ("Viste dall’alto disponibili: nessuna.",
                  "ATTENZIONE: geometria package ignorata",
                  "Candidate measurement budget: first 64 of 382 compatible names."):
        overlay.set_status(human)
        assert overlay.status.text() == human


#: Righe che gli strumenti esterni scrivono sul proprio stdout. Nessuna ha un
#: marcatore come quelli di questo programma: vanno riconosciute dalla forma.
MACHINE_SHAPED = (
    # Percorso assoluto con lettera di unita' + conteggio di entita'.
    "FreeCAD: Step File Name : C:/Lavori/Componenti/Test.step(134238 ents) Write Done",
    # Riga di sole decorazioni intorno a un campo "chiave = valore".
    "FreeCAD: ******        Transferring Shape, ShapeType = 0    ******",
    # Intestazione di sessione in forma "chiave : valore".
    "FreeCAD: ** WorkSession : Sending all data",
    # Percorso assoluto di un file temporaneo.
    "FreeCAD: File format not supported: C:/Users/mirko/AppData/tmpXXXX.kicadgen_params",
    # Le stesse forme senza alcun prefisso di prodotto: il riconoscimento non
    # dipende da chi ha scritto la riga.
    "Step File Name : /home/mirko/lavori/Test.step",
    "==================== inizio ====================",
    "Reading /var/tmp/kicadgen/params.json",
    "Shapes written (98211 ents)",
    "\\\\server\\lavori\\Test.step aggiornato",
)

#: Frasi scritte per chi guarda. Contengono apici, parentesi, unita' di misura,
#: cifre e un doppio trattino: nessuno di questi segni, da solo, e' un indizio
#: di riga macchina, ed e' proprio il caso che l'utente vuole vedere in modale.
HUMAN_SHAPED = (
    "ATTENZIONE 3D: componenti sovrapposti: 'YOLO candidate 19' e 'YOLO candidate 22'"
    " (0.24x1.00mm in comune) -- correggi posizione o rotazione",
    "Rilancio dell'inventario su 12 proposte da proposals.json: una richiesta a"
    " pagamento al modello anthropic/claude-opus-5.",
    "Generazione del footprint KiCad…",
    "Trovati 3 corpi (3 componenti) sulla vista dall'alto.",
    "Preparazione dei documenti…",
    "Viste dall’alto disponibili: nessuna.",
)


@pytest.mark.parametrize("line", MACHINE_SHAPED)
def test_machine_shaped_lines_are_recognised_by_their_form(overlay, line):
    """Positivo: percorsi, campi «chiave : valore», decorazioni e conteggi di
    entita' sono forme da tracciato, non frasi. Restano nel log: in modale
    coprivano l'unica riga che diceva cosa stava accadendo. La regola non
    nomina nessun prodotto, cosi' non va riscritta a ogni nuova versione
    dello strumento che le stampa."""
    assert is_machine_line(line)
    overlay.set_status("Generazione del footprint KiCad…")
    overlay.set_status(line)
    assert overlay.status.text() == "Generazione del footprint KiCad…", (
        "scartando una riga l'ultimo stato leggibile deve restare scritto: una"
        " label vuota fa sembrare l'operazione bloccata")


@pytest.mark.parametrize("line", HUMAN_SHAPED)
def test_human_sentences_still_reach_the_card(overlay, line):
    """Negativo: la stessa regola non deve mangiare un avviso scritto per chi
    guarda. Le due classi si distinguono per struttura -- il colon attaccato
    alla parola, l'assenza di separatori di percorso, i numeri dentro una
    frase -- non perche' si sia elencata la frase giusta."""
    assert not is_machine_line(line)
    overlay.set_status(line)
    assert overlay.status.text() == line


def _render(spinner):
    """Disegna lo spinner in un'immagine confrontabile, ad angolo fermo."""
    spinner.angle = 0
    image = QImage(spinner.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    spinner.render(image)
    return image


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_every_tool_draws_a_glyph_inside_the_ring(overlay, tool):
    """Positivo: il segno dello strumento esiste davvero sullo schermo, non solo
    nella tabella TOOLS: si confronta col solo anello."""
    spinner = AnalysisSpinner()
    bare = _render(spinner)
    _, color, glyph = TOOLS[tool]
    spinner.set_tool(glyph, color)
    assert _render(spinner) != bare, f"{tool}: nessun pixel disegnato oltre l'anello"


def test_two_tools_never_render_the_same_picture(overlay):
    """Positivo: a colpo d'occhio la fase si riconosce dalla forma, quindi le
    quattro immagini rese devono differire a coppie. Il colore non basta come
    prova: si confrontano i pixel, dove un cambio di forma si vede."""
    spinner = AnalysisSpinner()
    rendered = {}
    for tool, (_, color, glyph) in TOOLS.items():
        spinner.set_tool(glyph, color)
        rendered[tool] = _render(spinner)
    keys = sorted(rendered)
    for first in range(len(keys)):
        for second in range(first + 1, len(keys)):
            assert rendered[keys[first]] != rendered[keys[second]], (
                f"{keys[first]} e {keys[second]} si disegnano uguali")


def test_an_unknown_glyph_draws_the_bare_ring_without_crashing(overlay):
    """Negativo: una fase nuova, o un refuso nella chiave, deve dare l'anello
    nudo -- identico a quello senza strumento -- e non un'eccezione dentro
    paintEvent, che in Qt porta giu' l'animazione."""
    spinner = AnalysisSpinner()
    bare = _render(spinner)
    spinner.set_tool("segno-mai-visto", "#89b4fa")
    assert _render(spinner) == bare


def test_the_card_is_sized_on_its_content_and_does_not_jump(overlay):
    """La scheda era bassa e strozzata. La larghezza deve bastare a distribuire
    il testo troncato su _STATUS_LINES righe e allo stato vanno riservate
    _STATUS_LINES righe piene, misurate col font vero; e le righe che compaiono
    a operazione avviata non devono cambiare l'ingombro della scheda, altrimenti
    la modale cresce e rimpicciolisce sotto gli occhi."""
    metrics = overlay.status.fontMetrics()
    needed = metrics.averageCharWidth() * (_STATUS_CHAR_LIMIT // _STATUS_LINES)
    assert overlay.card.minimumWidth() >= needed
    assert overlay.status.minimumHeight() >= _STATUS_LINES * metrics.lineSpacing()
    assert overlay.card.minimumHeight() >= overlay.status.minimumHeight()
    assert overlay.card.minimumWidth() == overlay.card.maximumWidth(), (
        "la larghezza e' fissa: una scheda che si allarga sulla frase piu' lunga"
        " sposta tutto il contenuto a ogni messaggio")
    before = overlay.card.sizeHint().height()
    overlay.set_tool("llm", "anthropic/claude-opus-5")
    overlay.set_progress(3, 9, "componenti")
    overlay.set_cost("Costo analisi: 0.0417 USD")
    overlay.set_status("Ricostruzione dei corpi rilevati sulla vista dall'alto e"
                       " confronto con le quote dichiarate, poi conversione in"
                       " millimetri delle posizioni misurate in pixel.")
    QApplication.instance().processEvents()
    assert overlay.card.sizeHint().height() == before, (
        "la scheda non deve cambiare altezza quando compaiono le righe"
        " facoltative o quando lo stato diventa piu' lungo")


def test_overlay_animation_resize_literal_status_and_cleanup():
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.resize(900, 650)
    overlay = AnalysisOverlay(parent)
    parent.show()
    try:
        overlay.start()
        app.processEvents()
        assert overlay.isVisible() and overlay.timer.isActive()
        assert overlay.geometry() == parent.rect()
        angle = overlay.spinner.angle
        overlay._tick()
        assert overlay.spinner.angle != angle
        overlay.set_status("<b>Crop 2: analisi</b>")
        assert overlay.status.text() == "<b>Crop 2: analisi</b>"
        overlay.set_status("x" * 1000)
        assert len(overlay.status.text()) <= 220
        parent.resize(650, 500)
        app.processEvents()
        assert overlay.geometry() == parent.rect()
        spy = QSignalSpy(overlay.log_requested)
        overlay.log_button.click()
        assert len(spy) == 1
        overlay.stop()
        assert not overlay.isVisible() and not overlay.timer.isActive()
        overlay.start()
        assert overlay.status.text() == "Preparazione dei documenti…"
        assert overlay.elapsed.text().endswith("00:00")
        overlay.start('Generazione footprint e modello 3D', 'Generazione…')
        assert overlay.title.text() == 'Generazione footprint e modello 3D'
        assert overlay.status.text() == 'Generazione…'
    finally:
        overlay.stop()
        parent.close()
