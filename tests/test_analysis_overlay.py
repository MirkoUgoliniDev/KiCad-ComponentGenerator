import math
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QFont, QImage, QRegion
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication, QLabel, QWidget
from kicadgen.gui.analysis_overlay import (TOOLS, AnalysisOverlay, AnalysisSpinner,
                                          ElidedLabel, _CARD_MIN_WIDTH,
                                          _CARD_PADDING, _OVERLAY_MARGIN,
                                          _STATUS_CHAR_LIMIT, _STATUS_LINES,
                                          elide_paths, is_machine_line)


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
                   "AI_CALL_PROMPT_SYSTEM Sei un assistente che legge datasheet.",
                   'AI_CALL_PROMPT_TOOL {"function": {"name": "report"}}',
                   # Chi inoltra l'uscita di freecadcmd le antepone un prefisso.
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


#: Il marcatore va cercato SOLO in testa alla riga. UPPER_SNAKE e' in questo
#: dominio anche la convenzione dei reference designator e dei nomi di libreria,
#: quindi cercarlo in mezzo alla riga rendeva la regola una regola sui nomi dei
#: componenti dell'utente -- vietata da CLAUDE.md -- e scartava esiti veri.
DESIGNATOR_BEARING = (
    "Global inventory FAILED/PARTIAL: component U1_MODULE: merged box excludes"
    " most of sources [44, 45]; unresolved proposals retained.",
    "ATTENZIONE 3D: componente U1_MODULE (rf_module): geometria approssimata",
    "Componente J_HEADER_BLK non riconosciuto: geometria generica.",
    "Il pin RX_TX non e' popolato.",
    "ATTENZIONE 3D: componenti sovrapposti: 'J_HEADER_BLK (connector)' e"
    " 'C8 (capacitor)' (2.25x2.47mm in comune) -- correggi posizione",
)


@pytest.mark.parametrize("line", DESIGNATOR_BEARING)
def test_a_designator_inside_a_sentence_is_not_a_service_marker(overlay, line):
    """Negativo strutturale: il marcatore di servizio APRE la riga, perche' e'
    l'intestazione del record; in una frase il designator non e' mai la prima
    parola, perche' la frase nomina prima la cosa ('Componente J_HEADER_BLK...',
    'Il pin RX_TX...'). Distinguere per posizione, non per nome."""
    assert not is_machine_line(line)
    overlay.set_status(line)
    assert overlay.status.text() == line


#: Righe che gli strumenti esterni scrivono sul proprio stdout. Nessuna ha un
#: marcatore come quelli di questo programma: vanno riconosciute dalla forma.
MACHINE_SHAPED = (
    # Percorso assoluto con lettera di unita' + conteggio di entita'.
    "FreeCAD: Step File Name : C:/Lavori/Componenti/Test.step(134238 ents) Write Done",
    # Riga di sole decorazioni intorno a un campo "chiave = valore".
    "FreeCAD: ******        Transferring Shape, ShapeType = 0    ******",
    # Intestazione di sessione in forma "chiave : valore".
    "FreeCAD: ** WorkSession : Sending all data",
    # Le stesse forme senza alcun prefisso di prodotto: il riconoscimento non
    # dipende da chi ha scritto la riga.
    "Step File Name : /home/mirko/lavori/Test.step",
    "==================== inizio ====================",
    "Shapes written (98211 ents)",
    # Percentuale nuda, senza soggetto: non dice a cosa si riferisce e resta
    # appesa per tutta la fase del CAD al posto delle righe che dicono cosa si
    # sta costruendo.
    "FreeCAD: (47 %)",
    "(100 %)",
    "47 %",
    # Riempimento di una barra testuale: quattro punti o piu' non sono
    # un'ellissi in nessuna delle due lingue usate qui.
    "Recompute......",
    "FreeCAD: Recompute......",
    # Record "chiave=valore": almeno due coppie di seguito. Fra queste ci sono
    # le impronte sha256, che il docstring di set_status promette di lasciare
    # nel log.
    "Global inventory prompt=global-yolo-inventory-6; schema=global-inventory-4;"
    " sha256=7cd600c8137b6c87af92bf248d88fefdbfab66ead9366ce094583f580426826b",
    "Inventario globale: prompt=g6; schema=gi4; sha256=7cd600c8137b6c87af92bf24",
    "FreeCAD exit=0, timeout=False",
    "AI_CALL_END id=1 model=x tool=y elapsed=65.664s result=object fields=21"
    " tokens_in=16765 tokens_out=6732 cost_usd=0.2521",
    # Intestazioni d'avvio di un programma esterno.
    "FreeCAD 1.1.1, Libs: 1.1.1R41202 (Git)",
    "FreeCAD: FreeCAD 1.1.1, Libs: 1.1.1R20260414 (Git shallow)",
    "(C) 2001-2026 FreeCAD contributors",
    "FreeCAD: (C) 2001-2026 FreeCAD contributors",
    # Riga che si auto-datifica: e' un record di sessione, e l'orologio della
    # scheda dice gia' da quanto si sta aspettando.
    "[2026-09-21T00:08:36+02:00] FreeCAD start",
    "[2026-09-21T00:09:01+02:00] FreeCAD exit=0, timeout=False",
    "2026-09-21 00:04:13 Analisi AI",
)

#: Frasi scritte per chi guarda. Contengono apici, parentesi, unita' di misura,
#: cifre, punteggiatura enfatica, un doppio trattino, una URL e uno slug di
#: modello: nessuno di questi segni, da solo, e' un indizio di riga macchina, ed
#: e' proprio il caso che l'utente vuole vedere in modale.
HUMAN_SHAPED = (
    "ATTENZIONE 3D: componenti sovrapposti: 'YOLO candidate 19' e 'YOLO candidate 22'"
    " (0.24x1.00mm in comune) -- correggi posizione o rotazione",
    "Rilancio dell'inventario su 12 proposte da proposals.json: una richiesta a"
    " pagamento al modello anthropic/claude-opus-5.",
    "Generazione del footprint KiCad…",
    "Trovati 3 corpi (3 componenti) sulla vista dall'alto.",
    "Preparazione dei documenti…",
    "Viste dall’alto disponibili: nessuna.",
    # Punteggiatura enfatica: tre segni ripetuti in una frase sono enfasi, non
    # una decorazione da tracciato.
    "Attenzione!!! il modello non ha risposto entro il tempo previsto.",
    "Il modello ha risposto??? nessun campo utile nel risultato.",
    # Conteggi scritti per chi guarda: il numero grande da solo non basta a
    # farne un conteggio di entita', la frase e' conclusa da un punto.
    "Importati (12345 pezzi) dal modello STEP.",
    "Suite locale: (1061 test) superati.",
    # Analisi principale: un solo "chiave=valore" non fa un record.
    "Analisi principale completata: layout=castellated, componenti iniziali=47.",
    # SEPARAZIONE FRAGILE, pinzata qui: questo riepilogo ha la stessa forma di
    # un record ("etichetta: coppie"), e si distingue solo perche' fra
    # l'etichetta e le coppie c'e' gia' un dato scritto per chi guarda ("2x22").
    # E' la riga che dice quale footprint si sta generando: non deve sparire.
    "Generazione TEST: 2x22, pitch=2.54mm, row_spacing=22.86mm, pad=1.7mm,"
    " drill=1.0mm",
    "Generazione TEST: modulo castellated 63.3x25.4mm, superiore 22x2.54mm,"
    " inferiore 22x2.54mm",
    # Una URL resta una URL: non e' un percorso su disco.
    "Scarica da https://example.org/kicad/3dmodels/index.json il modello mancante.",
    # Italiano: i due punti stanno attaccati alla parola che precede.
    "chiave: Valore -- dettaglio... fine",
)


@pytest.mark.parametrize("line", MACHINE_SHAPED)
def test_machine_shaped_lines_are_recognised_by_their_form(overlay, line):
    """Positivo: campi «chiave : valore», record «chiave=valore», decorazioni,
    riempimenti di punti, conteggi di entita', percentuali senza soggetto,
    intestazioni di versione/copyright e righe auto-datate sono forme da
    tracciato, non frasi. Restano nel log: in modale coprivano l'unica riga che
    diceva cosa stava accadendo. Nessuna regola nomina un prodotto, cosi' non va
    riscritta a ogni nuova versione dello strumento che le stampa."""
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
    alla parola, la posizione del marcatore, l'assenza di coppie chiave=valore
    in fila, il punto che chiude la frase -- non perche' si sia elencata la
    frase giusta."""
    assert not is_machine_line(line)
    overlay.set_status(line)
    assert overlay.status.text() == line


#: Righe che NOMINANO UN FILE. Il prodotto di questo strumento sono file: la
#: vecchia regola le scartava tutte perche' contenevano un percorso, e con esse
#: l'esito piu' importante di tutti, il fallimento di una scrittura su disco.
#: CONTRATTO CAMBIATO: il percorso non costa piu' la riga, viene accorciato al
#: nome del file. Fra le due soluzioni possibili (scartare quando la riga e'
#: quasi solo percorso, oppure accorciare) la prima avrebbe scartato proprio
#: «Inventario salvato: <tre percorsi>», che e' la riga da mostrare.
PATH_BEARING = (
    ("Inventario salvato: C:\\Lavori\\gen\\inv-photo.png;"
     " C:\\Lavori\\gen\\inv-overlay.png; C:\\Lavori\\gen\\inv.json",
     "Inventario salvato: …\\inv-photo.png; …\\inv-overlay.png; …\\inv.json"),
    ("Proposte del rilevatore salvate: C:\\Lavori\\gen\\det-overlay.png;"
     " C:\\Lavori\\gen\\det.json",
     "Proposte del rilevatore salvate: …\\det-overlay.png; …\\det.json"),
    ("Inventario non salvato su disco: [Errno 28] No space left on device:"
     " 'C:\\Lavori\\inv.json'",
     "Inventario non salvato su disco: [Errno 28] No space left on device:"
     " '…\\inv.json'"),
    ("OK (3D) -> C:\\Lavori\\gen\\TEST\\TEST.3dshapes\\TEST.step",
     "OK (3D) -> …\\TEST.step"),
    ("Reading /var/tmp/kicadgen/params.json", "Reading …/params.json"),
    ("\\\\server\\lavori\\Test.step aggiornato", "…\\Test.step aggiornato"),
)


@pytest.mark.parametrize("line,shown", PATH_BEARING)
def test_naming_a_file_does_not_cost_the_line_its_place(overlay, line, shown):
    """Positivo: l'esito resta leggibile e il percorso si accorcia al nome del
    file, che e' la parte che l'utente riconosce. Un messaggio di scrittura
    FALLITA veniva scartato solo perche' il testo dell'OSError porta un
    percorso: e' l'errore piu' importante che la scheda possa mostrare."""
    assert not is_machine_line(line)
    overlay.set_status(line)
    assert overlay.status.text() == shown


def test_what_is_not_a_path_is_left_alone():
    """Negativo: lo slug di un modello, un nome di file senza separatori e il
    percorso dentro una URL non hanno la forma di un percorso assoluto e non
    vanno toccati -- accorciarli perderebbe l'unica informazione della riga."""
    for text in ("modello anthropic/claude-opus-5 scelto da impostazioni",
                 "Importato ESP32-S3-WROOM-1U.step senza colori nativi.",
                 "Scarica da https://example.org/kicad/3dmodels/index.json il file.",
                 "<b>Crop 2: analisi</b>"):
        assert elide_paths(text) == text


def test_a_path_only_line_keeps_only_its_file_name():
    """Positivo/negativo sulla forma dell'accorciamento: sparisce l'albero delle
    cartelle, resta il nome, e il taglio e' visibile come ellissi."""
    assert elide_paths("C:\\Lavori\\Python\\gen\\TEST\\TEST.step") == "…\\TEST.step"
    assert elide_paths("/home/mirko/lavori/TEST.step") == "…/TEST.step"
    assert elide_paths("\\\\server\\lavori\\TEST.step") == "…\\TEST.step"


def _render(spinner):
    """Disegna lo spinner in un'immagine confrontabile, ad angolo fermo."""
    spinner.angle = 0
    image = QImage(spinner.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    spinner.render(image)
    return image


def _draw(widget):
    """Disegna il widget SENZA lo sfondo della finestra, che altrimenti riempie
    tutta l'immagine e rende impossibile vedere dove finisce il disegno."""
    image = QImage(widget.size(), QImage.Format.Format_ARGB32)
    image.fill(0)
    widget.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    return image


def _runs_added_by_the_glyph(spinner, glyph, color, y):
    """Numero di tratti separati che il segno aggiunge al solo anello, sulla
    riga y: l'anello e' identico nelle due immagini, quindi le differenze sono
    esattamente i pixel del segno."""
    spinner.set_tool("", color)
    bare = _draw(spinner)
    spinner.set_tool(glyph, color)
    marked = _draw(spinner)
    runs, inside = 0, False
    for x in range(marked.width()):
        different = marked.pixel(x, y) != bare.pixel(x, y)
        if different and not inside:
            runs += 1
        inside = different
    return runs


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_every_tool_draws_a_glyph_inside_the_ring(overlay, tool):
    """Positivo: il segno dello strumento esiste davvero sullo schermo, non solo
    nella tabella TOOLS: si confronta col solo anello."""
    spinner = AnalysisSpinner()
    bare = _render(spinner)
    _, color, glyph = TOOLS[tool]
    spinner.set_tool(glyph, color)
    assert _render(spinner) != bare, f"{tool}: nessun pixel disegnato oltre l'anello"


def test_the_footprint_glyph_reads_as_separate_pads(overlay):
    """Positivo e negativo sulla stessa riga di pixel: le tre piazzole di una
    fila devono restare tre tratti distinti (il vuoto dev'essere piu' largo del
    tratto del contorno, che se lo mangia), mentre la riga che attraversa il
    contorno del corpo ne mostra due, i suoi due lati. Con lo stacco precedente
    le piazzole si saldavano e il segno si leggeva come due barre."""
    spinner = AnalysisSpinner()
    assert _runs_added_by_the_glyph(spinner, "pads", "#89b4fa", 19) == 3
    assert _runs_added_by_the_glyph(spinner, "pads", "#89b4fa", 37) == 3
    assert _runs_added_by_the_glyph(spinner, "pads", "#89b4fa", 29) == 2


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


#: Frase peggiore ammessa: esattamente la lunghezza a cui lo stato viene
#: troncato. La vecchia prova usava 155 caratteri, cioe' non provava il caso che
#: il dimensionamento dichiara di coprire.
_SENTENCE = ("Ricostruzione dei corpi rilevati sulla vista dall'alto e"
             " confronto con le quote dichiarate, poi conversione in"
             " millimetri delle posizioni misurate in pixel. ")
WORST_CASE_STATUS = (_SENTENCE * 3)[:_STATUS_CHAR_LIMIT]
assert len(WORST_CASE_STATUS) == _STATUS_CHAR_LIMIT, len(WORST_CASE_STATUS)


@pytest.mark.parametrize("parent_width,parent_height", ((900, 650), (640, 480)))
@pytest.mark.parametrize("point_size", (7, 16, 24))
def test_the_card_is_sized_on_its_content_and_does_not_jump(parent_width,
                                                            parent_height,
                                                            point_size):
    """La larghezza deve essere quella derivata dal font, con un tetto dedotto
    dalla finestra e non costante: un tetto fisso (760 px) annullava la
    derivazione e tagliava il testo dai 14 pt in su, e come minimo non entrava
    in una finestra da 640 px. Quando il tetto morde, le righe riservate allo
    stato vanno RICONTATE alla larghezza sopravvissuta, altrimenti la frase
    peggiore viene tagliata a meta' di una riga. E le righe che compaiono a
    operazione avviata non devono cambiare l'ingombro della scheda, altrimenti
    la modale cresce e rimpicciolisce sotto gli occhi."""
    app = QApplication.instance() or QApplication([])
    font = QFont(app.font())
    font.setPointSize(point_size)
    parent = QWidget()
    parent.setFont(font)
    parent.resize(parent_width, parent_height)
    overlay = AnalysisOverlay(parent)
    parent.show()
    try:
        overlay.start()
        app.processEvents()
        metrics = overlay.status.fontMetrics()
        chars_per_line = math.ceil(_STATUS_CHAR_LIMIT / (_STATUS_LINES - 0.5))
        wanted = 2 * _CARD_PADDING + metrics.averageCharWidth() * chars_per_line
        cap = parent_width - 2 * _OVERLAY_MARGIN
        expected = min(cap, max(_CARD_MIN_WIDTH, wanted))
        assert overlay.card.minimumWidth() == expected, (
            "la larghezza e' la derivazione dal font, limitata dalla finestra")
        assert overlay.card.minimumWidth() == overlay.card.maximumWidth(), (
            "la larghezza e' fissa: una scheda che si allarga sulla frase piu'"
            " lunga sposta tutto il contenuto a ogni messaggio")
        assert overlay.card.minimumWidth() + 2 * _OVERLAY_MARGIN <= parent_width, (
            "una scheda piu' larga della finestra viene tagliata di lato")
        assert overlay.status.minimumHeight() >= _STATUS_LINES * metrics.lineSpacing()
        assert overlay.card.minimumHeight() >= overlay.status.minimumHeight()
        # Il testo peggiore possibile deve STARE nell'altezza riservata: e' la
        # differenza fra una riga che va a capo e una riga tagliata a meta'.
        usable = overlay.card.minimumWidth() - 2 * _CARD_PADDING
        overlay.set_status(WORST_CASE_STATUS)
        assert overlay.status.heightForWidth(usable) <= overlay.status.minimumHeight(), (
            "alla larghezza sopravvissuta lo stato occupa piu' righe di quelle"
            " riservate: il testo verrebbe tagliato")
        before = overlay.card.height()
        overlay.set_tool("llm", "anthropic/claude-opus-5 (da impostazioni,"
                                " verifica dimensioni)")
        overlay.set_progress(3, 9, "componenti")
        overlay.set_cost("Costo elaborazione AI: 0.8762 USD su 2 chiamate;"
                         " 53673 token in ingresso, 24314 in uscita.")
        overlay.set_status(WORST_CASE_STATUS)
        app.processEvents()
        assert overlay.card.height() == before, (
            "la scheda non deve cambiare altezza quando compaiono le righe"
            " facoltative o quando lo stato diventa piu' lungo")
        assert overlay.card.layout().minimumSize().height() <= overlay.card.minimumHeight(), (
            "il contenuto deve stare nello spazio riservato all'avvio,"
            " altrimenti la scheda cresce appena le righe compaiono")
    finally:
        overlay.stop()
        parent.close()


def test_the_card_follows_a_font_change_after_startup():
    """Il calcolo stava solo in __init__: un cambio di tema o del font di
    accessibilita' a programma avviato lasciava la scheda sulle metriche
    vecchie, cioe' col testo tagliato. Il font nuovo arriva SULLA label dello
    stato -- il foglio di stile della scheda fissa il font dei figli, quindi non
    passa dall'overlay -- ed e' li' che va intercettato. Positivo: la larghezza
    derivata cresce col font; negativo: resta dentro la finestra."""
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.resize(1600, 900)
    overlay = AnalysisOverlay(parent)
    parent.show()
    try:
        small = overlay.card.minimumWidth()
        font = QFont(overlay.status.font())
        font.setPointSize(font.pointSize() * 2 if font.pointSize() > 0 else 20)
        overlay.status.setFont(font)
        app.processEvents()
        assert overlay.card.minimumWidth() > small, (
            "con un font piu' grande la scheda deve riallargarsi")
        assert overlay.card.minimumWidth() + 2 * _OVERLAY_MARGIN <= 1600
        assert overlay.status.minimumHeight() >= (
            _STATUS_LINES * overlay.status.fontMetrics().lineSpacing()), (
            "anche l'altezza riservata va rifatta sulle metriche nuove")
    finally:
        overlay.stop()
        parent.close()


def _painted_extent(label):
    """Prima e ultima colonna dipinta dalla label, resa in un'immagine."""
    image = _draw(label)
    columns = [x for x in range(image.width())
               for y in range(image.height())
               if (image.pixel(x, y) >> 24) > 40]
    return (min(columns), max(columns)) if columns else (None, None)


HEADER_TEXT = ("Modello AI (remoto, a consumo) · anthropic/claude-opus-5"
               " (da impostazioni, verifica dimensioni)")


def test_the_tool_header_shortens_itself_instead_of_being_clipped():
    """Positivo e negativo sulla stessa frase: una QLabel normale, centrata e
    senza a capo, viene tagliata sui DUE lati (si perde la prima parola,
    'Modello', e l'ultima), quindi l'intestazione diventa illeggibile proprio
    mentre altre righe vengono scartate 'perche' l'intestazione lo dice gia''.
    La label che si accorcia da sola resta tutta dentro il proprio rettangolo."""
    app = QApplication.instance() or QApplication([])
    assert app is not None
    elided, plain = ElidedLabel(), QLabel()
    for label in (elided, plain):
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setText(HEADER_TEXT)
        label.resize(260, label.fontMetrics().lineSpacing() + 4)
    first, last = _painted_extent(elided)
    assert first is not None and first > 0 and last < elided.width() - 1, (
        "il testo accorciato deve stare dentro il rettangolo della label")
    plain_first, plain_last = _painted_extent(plain)
    assert plain_first == 0 or plain_last == plain.width() - 1, (
        "la prova non e' valida se la label normale non viene tagliata")


def test_the_header_keeps_its_full_text_for_main_window(overlay):
    """main_window legge tool.text() per sapere quale dettaglio l'intestazione
    mostra gia': accorciare il testo MEMORIZZATO cambierebbe in silenzio quella
    decisione. Si accorcia solo cio' che si dipinge."""
    detail = "anthropic/claude-opus-5 (da impostazioni, verifica dimensioni)"
    overlay.set_tool("llm", detail)
    assert overlay.tool.text() == f"{TOOLS['llm'][0]} · {detail}"
    assert overlay.tool.text().endswith(detail)


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
        assert overlay.card.minimumWidth() + 2 * _OVERLAY_MARGIN <= 650, (
            "rimpicciolendo la finestra il tetto della larghezza va rifatto")
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
