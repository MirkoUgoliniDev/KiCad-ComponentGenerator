import math
import os
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QFont, QImage, QRegion, QTextDocument
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication, QLabel, QWidget
from kicadgen.gui.analysis_overlay import (TOOLS, AnalysisOverlay, AnalysisSpinner,
                                          ElidedLabel, _CARD_MIN_WIDTH,
                                          _CARD_PADDING, _OVERLAY_MARGIN,
                                          _STATUS_CHAR_LIMIT, _STATUS_LINES, _STATUS_WRAP_LINES,
                                          elide_paths, enumeration_lines,
                                          is_machine_line)
from kicadgen.services.datasheet_extractor import _files_sent_message


@pytest.fixture
def overlay(qapp):
    app = qapp
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
    # Forma effettiva dopo l'aggiunta del canale di rinuncia: la riga di uscita
    # porta anche `cancelled=`, e resta un record, non una frase. La coda fra
    # parentesi («… (interrotto su richiesta dell'utente)») non compare qui
    # perche' quella riga non passa mai per la modale: run_logged la scrive
    # soltanto sul file di log del CAD.
    "FreeCAD exit=-9, timeout=False, cancelled=True",
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


#: ── L'elenco sulla scheda: una voce per riga ─────────────────────────
#:
#: Richiesta dell'utente: «l'elenco nella modale deve essere fatto meglio, i
#: nomi dei file uno sotto l'altro, più chiaro». La riga che il programma
#: scrive resta UNA -- il log vive di un evento per riga -- e qui si verifica
#: che la scheda la rimpagini senza tagliarla e senza uscire dalla finestra.


def _document_line(count):
    """La riga vera di `datasheet_extractor`, non una sua imitazione: il patto
    fra chi scrive la riga e chi la impagina e' proprio la sua punteggiatura,
    quindi ricopiarla qui vorrebbe dire non provarlo."""
    names = [f'documento-{index}.png' for index in range(count)]
    roles = {name: ('top', 'pinout', 'hires')[index % 3]
             for index, name in enumerate(names)}
    return _files_sent_message([Path('progetto') / name for name in names], roles)


def _card_lines(overlay):
    """Le righe che l'utente LEGGE sulla scheda.

    CONTRATTO CAMBIATO: un elenco ora e' testo ricco (nome del file in
    grassetto), quindi `status.text()` restituisce il markup e non le parole.
    Si legge il testo del documento che la label disegna, che e' cio' che le
    asserzioni di questo file hanno sempre inteso."""
    return overlay.status_plain_text().split("\n")


def _assert_nothing_clipped(overlay):
    """Positivo di misura: il testo scritto sta TUTTO nello spazio riservato,
    in altezza e in larghezza. E' la differenza fra un elenco e un elenco
    tagliato a meta' dell'ultima riga.

    Si misura alla larghezza in cui la label disegna DAVVERO (bordo della
    scheda escluso), non a `larghezza scheda - 2 * _CARD_PADDING`: quella
    misura era 2 px piu' larga della label, ed e' proprio lo scarto che faceva
    andare a capo le righe accorciate (vedi tests/test_overlay_file_list.py)."""
    real = overlay._status_text_width()
    assert overlay.status.heightForWidth(real) <= overlay.status.minimumHeight()
    assert _required_height(overlay.status, real) <= overlay.status.minimumHeight()
    assert overlay.card.minimumHeight() >= overlay.status.minimumHeight()
    assert _widest_line(overlay.status) <= real, (
        f"riga oltre la label: {_card_lines(overlay)}")


def _required_height(label, width):
    """Altezza che il testo della label chiede a quella larghezza, SENZA il
    minimo imposto: `heightForWidth` della label stessa restituisce almeno il
    suo `minimumHeight`, quindi non puo' dire se il testo ci sta. Si chiede a
    una label gemella con lo stesso font, formato e testo."""
    twin = QLabel()
    twin.setFont(label.font())
    twin.setTextFormat(label.textFormat())
    twin.setWordWrap(label.wordWrap())
    twin.setAlignment(label.alignment())
    twin.setText(label.text())
    return twin.heightForWidth(width)


def _widest_line(label):
    """Larghezza della riga piu' larga COME VIENE DISEGNATA: col grassetto,
    per il testo ricco; con le metriche del font, per il testo semplice."""
    if label.textFormat() == Qt.TextFormat.RichText:
        document = QTextDocument()
        document.setDefaultFont(label.font())
        document.setDocumentMargin(0)
        document.setHtml(label.text())
        return document.idealWidth()
    metrics = label.fontMetrics()
    return max(metrics.horizontalAdvance(line) for line in label.text().split("\n"))


def test_the_documents_are_listed_one_per_line_with_name_and_role(overlay):
    """Positivo: tre documenti, tre righe, e su ognuna il nome e il ruolo con
    cui il file parte. Prima era una riga sola con i documenti separati dal
    punto mediano, che la scheda mandava a capo dove capitava, spezzando i nomi
    a meta'."""
    message = _files_sent_message(
        [Path('a/pinout.png'), Path('a/top.png'), Path('a/hires.jpg')],
        {'pinout.png': 'pinout', 'top.png': 'top', 'hires.jpg': 'hires'})
    overlay.set_status(message)
    assert _card_lines(overlay) == [
        'Invio a AI 3 documenti per layout, quote e pinout:',
        'pinout.png — Pinout / disposizione contatti',
        'top.png — Immagine con quote',
        'hires.jpg — Foto ad alta risoluzione.']
    _assert_nothing_clipped(overlay)
    # Incolonnati a sinistra: centrati, i nomi partirebbero da una colonna
    # diversa per riga, che e' il difetto che l'elenco deve togliere.
    assert overlay.status.alignment() & Qt.AlignmentFlag.AlignLeft


def test_the_card_makes_room_for_eight_documents_without_clipping(qapp):
    """Positivo al limite: otto documenti sono nove righe, cioe' molte piu'
    delle tre riservate d'ufficio. La scheda deve SEGUIRE il contenuto (e
    restare dentro la finestra), non tagliarlo: il numero di documenti lo
    sceglie l'utente, non la scheda."""
    app = qapp
    parent = QWidget()
    parent.resize(900, 650)
    overlay = AnalysisOverlay(parent)
    parent.show()
    try:
        overlay.start()
        app.processEvents()
        before = overlay.card.minimumHeight()
        overlay.set_status(_document_line(8))
        app.processEvents()
        lines = _card_lines(overlay)
        assert len(lines) == 9 and lines[-1].endswith('.'), (
            "l'elenco deve arrivare in fondo, senza troncature")
        assert all(' — ' in line for line in lines[1:])
        _assert_nothing_clipped(overlay)
        assert overlay.card.minimumHeight() > before, (
            "la scheda deve crescere per contenere l'elenco")
        assert overlay.card.minimumHeight() + 2 * _OVERLAY_MARGIN <= 650, (
            "una scheda piu' alta della finestra perde i pulsanti in fondo")
    finally:
        overlay.stop()
        parent.close()


def test_a_window_too_short_cuts_the_list_instead_of_overflowing(qapp):
    """Negativo dell'altezza: quando le righe non ci stanno, l'elenco si
    accorcia e il taglio si DICHIARA con un'ellissi su una riga propria -- lo
    stesso segno con cui questa scheda dichiara ogni altro accorciamento.
    Lasciarlo debordare manderebbe i pulsanti fuori dallo schermo, e tagliarlo
    in silenzio farebbe credere che i documenti fossero meno."""
    app = qapp
    parent = QWidget()
    parent.resize(900, 480)
    overlay = AnalysisOverlay(parent)
    parent.show()
    try:
        overlay.start()
        app.processEvents()
        overlay.set_status(_document_line(8))
        app.processEvents()
        lines = _card_lines(overlay)
        assert 2 < len(lines) < 9 and lines[-1] == '…'
        assert lines[0].endswith(':') and ' — ' in lines[1]
        _assert_nothing_clipped(overlay)
        assert overlay.card.minimumHeight() + 2 * _OVERLAY_MARGIN <= 480
    finally:
        overlay.stop()
        parent.close()


@pytest.mark.parametrize('count', (1, 0))
def test_without_a_list_the_line_stays_one_centred_line(overlay, count):
    """Negativo doppio: un documento solo non e' un elenco (due righe per dire
    cio' che ne occupa una), e zero documenti non elencano affatto -- quella
    riga dice che NESSUNA richiesta parte, e un elenco vuoto sembrerebbe un
    invio a vuoto, cioe' pagato."""
    message = _document_line(count)
    overlay.set_status(message)
    assert _card_lines(overlay) == [message]
    assert enumeration_lines(message) is None
    assert overlay.status.alignment() == Qt.AlignmentFlag.AlignCenter


@pytest.mark.parametrize('name', ('MOD_REV_B.pdf', 'MOD_REV_B'))
def test_a_document_named_like_a_service_marker_still_reaches_the_card(overlay, name):
    """Negativo contro i filtri VERI della modale: UPPER_SNAKE e' la forma con
    cui questo programma marca le proprie righe di servizio ed e' anche un nome
    di file legittimo. Il marcatore si cerca in testa alla riga, e il nome di
    un documento non ci arriva mai, perche' la riga comincia con l'annuncio:
    l'unica riga che dice che cosa e' partito non puo' sparire per come
    l'utente ha chiamato il suo file. Si prova anche senza estensione."""
    window = pytest.importorskip('kicadgen.gui.main_window')
    message = _files_sent_message([Path('d') / name, Path('d/top.png')],
                                  {name: 'pinout', 'top.png': 'top'})
    assert not is_machine_line(message) and not window.per_item_detail(message)
    overlay.set_status(message)
    lines = _card_lines(overlay)
    assert lines[1] == f'{name} — Pinout / disposizione contatti'
    assert len(lines) == 3


def test_a_very_long_name_is_shortened_by_the_sender_and_shown_whole_line(overlay):
    """Il nome lunghissimo viene accorciato da chi scrive la riga (nel mezzo,
    cosi' l'estensione resta) e sulla scheda occupa la sua riga come gli altri.
    Che il modello riceva comunque il nome INTERO e' provato in
    tests/test_progress_messages.py, dove si guardano i blocchi inviati."""
    name = 'scheda-radio-vista-dall-alto-alta-risoluzione-rev-C-2026.png'
    message = _files_sent_message([Path('d') / name, Path('d/top.png')],
                                  {name: 'hires', 'top.png': 'top'})
    overlay.set_status(message)
    lines = _card_lines(overlay)
    assert len(lines) == 3
    shown, _, role = lines[1].partition(' — ')
    assert role == 'Foto ad alta risoluzione' and '…' in shown
    assert shown.startswith('scheda-radio-vista') and shown.endswith('.png')
    assert name not in overlay.status_plain_text()
    _assert_nothing_clipped(overlay)


def test_any_other_sender_gets_the_same_layout(overlay):
    """Positivo di generalita': la regola e' sulla FORMA della riga (annuncio
    coi due punti, voci separate dal punto mediano), non sul messaggio dei
    documenti. L'inventario globale elenca allo stesso modo i motivi di un
    esito parziale, e anche quelli si leggono meglio incolonnati. Una regola
    valida per una riga sola sarebbe una regola tarata sul caso in mano."""
    message = ('Inventario globale FAILED/PARTIAL: component U1: merged box'
               ' excludes most of sources · 2 problemi · proposte irrisolte'
               ' conservate.')
    overlay.set_status(message)
    assert _card_lines(overlay) == [
        'Inventario globale FAILED/PARTIAL:',
        'component U1: merged box excludes most of sources',
        '2 problemi',
        'proposte irrisolte conservate.']
    _assert_nothing_clipped(overlay)


@pytest.mark.parametrize('line', (
    # Punto mediano senza annuncio: non e' un elenco, e' una frase.
    'Modello AI (remoto, a consumo) · anthropic/claude-opus-5',
    '27% · 12/45 componenti',
    # Annuncio senza punto mediano: elenca col punto e virgola, che questa
    # regola non tocca (i due punti da soli sono punteggiatura normale).
    'Inventario salvato: inv-photo.png; inv-overlay.png; inv.json',
))
def test_one_signal_alone_does_not_make_a_list(overlay, line):
    """Negativo della regola: servono ENTRAMBI i segni. Il solo punto mediano
    spezzerebbe frasi che non elencano nulla; i soli due punti sono
    punteggiatura di tutti i giorni e trasformerebbero mezza modale in elenchi
    di una voce."""
    assert enumeration_lines(line) is None
    overlay.set_status(line)
    assert _card_lines(overlay) == [line]


def test_an_empty_item_does_not_become_a_blank_row(overlay):
    """Negativo di robustezza: i motivi che questo programma elenca possono
    venire da testi altrui (un errore dell'API che torna vuoto), e due
    separatori di fila darebbero una riga bianca in mezzo all'elenco -- che non
    dice nulla e fa contare male le voci. Se dopo lo scarto resta una voce
    sola, non c'e' niente da incolonnare e la riga resta una riga."""
    assert enumeration_lines('Esito: primo motivo ·  · secondo motivo.') == [
        'Esito:', 'primo motivo', 'secondo motivo.']
    assert enumeration_lines('Esito: unico motivo ·  · ') is None
    overlay.set_status('Esito: unico motivo ·  · ')
    assert _card_lines(overlay) == ['Esito: unico motivo · ·']


def test_the_card_does_not_shrink_back_when_the_list_is_gone(overlay):
    """La scheda cresce una volta per l'elenco e resta cosi' fino alla fine
    dell'operazione: rialzarsi e riabbassarsi a ogni messaggio e' lo stesso
    sobbalzo che il dimensionamento esiste per evitare. Una NUOVA operazione
    invece riparte dall'ingombro di prima."""
    small = overlay.card.minimumHeight()
    overlay.set_status(_document_line(5))
    grown = overlay.card.minimumHeight()
    assert grown > small
    overlay.set_status('Generazione del footprint KiCad…')
    assert overlay.card.minimumHeight() == grown
    assert overlay.status.alignment() == Qt.AlignmentFlag.AlignCenter
    overlay.start()
    assert overlay.card.minimumHeight() == small


def test_the_elapsed_time_row_does_not_change_the_card_height(overlay):
    """La riga del tempo trascorso nasce vuota e riceve il primo testo in
    `start`, dopo il dimensionamento. Una label vuota e una con testo non
    chiedono sempre la stessa altezza (14 e 12 px sulla piattaforma offscreen),
    e la prima operazione aveva una scheda di 2 px diversa dalle successive:
    la riga ha quindi un'intera riga di testo riservata, come strumento e
    costo, e l'altezza della scheda non dipende da cosa c'e' scritto."""
    assert overlay.elapsed.minimumHeight() >= overlay.elapsed.fontMetrics().lineSpacing()
    heights = []
    for text in ("", "12 s", "1 h 05 min"):
        overlay.elapsed.setText(text)
        overlay._size_the_card()
        heights.append(overlay.card.minimumHeight())
    assert len(set(heights)) == 1


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
def test_the_card_is_sized_on_its_content_and_does_not_jump(qapp, parent_width,
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
    app = qapp
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
        chars_per_line = math.ceil(_STATUS_CHAR_LIMIT / _STATUS_WRAP_LINES)
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
        # Misurato alla larghezza VERA della label (bordo della scheda escluso):
        # la vecchia `larghezza - 2 * _CARD_PADDING` era 2 px piu' larga, cioe'
        # contava meno righe di quelle che la label disegna.
        usable = overlay._status_text_width()
        overlay.set_status(WORST_CASE_STATUS)
        app.processEvents()
        assert usable == overlay.status.contentsRect().width()
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


def test_the_card_follows_a_font_change_after_startup(qapp):
    """Il calcolo stava solo in __init__: un cambio di tema o del font di
    accessibilita' a programma avviato lasciava la scheda sulle metriche
    vecchie, cioe' col testo tagliato. Il font nuovo arriva SULLA label dello
    stato -- il foglio di stile della scheda fissa il font dei figli, quindi non
    passa dall'overlay -- ed e' li' che va intercettato. Positivo: la larghezza
    derivata cresce col font; negativo: resta dentro la finestra."""
    app = qapp
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


def test_the_tool_header_shortens_itself_instead_of_being_clipped(qapp):
    """Positivo e negativo sulla stessa frase: una QLabel normale, centrata e
    senza a capo, viene tagliata sui DUE lati (si perde la prima parola,
    'Modello', e l'ultima), quindi l'intestazione diventa illeggibile proprio
    mentre altre righe vengono scartate 'perche' l'intestazione lo dice gia''.
    La label che si accorcia da sola resta tutta dentro il proprio rettangolo."""
    app = qapp
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


def test_the_abort_action_exists_only_while_something_can_be_stopped(qapp):
    """Positivo: dichiarata l'interruzione, il comando c'e' ed emette il segnale.

    Negativo doppio: una scheda ferma, e una scheda avviata da un percorso che
    non sa fermare il proprio lavoro, non offrono il pulsante. La scheda non
    decide nulla da sola -- chiede e basta -- quindi il contratto che conta e'
    esattamente questo: quando si vede, e che cosa emette.
    """
    app = qapp
    parent = QWidget()
    parent.resize(900, 650)
    overlay = AnalysisOverlay(parent)
    parent.show()
    try:
        assert not overlay.is_abortable()
        assert not overlay.abort_button.isEnabled()
        overlay.start()
        app.processEvents()
        assert not overlay.is_abortable(), (
            "chi non dichiara di saper fermare il lavoro non offre di fermarlo")
        overlay.start('Generazione modello 3D', 'Avvio di FreeCAD…', abortable=True)
        app.processEvents()
        assert overlay.is_abortable()
        assert overlay.abort_button.isEnabled()
        assert overlay.abort_button.isVisibleTo(overlay.button_row)
        spy = QSignalSpy(overlay.abort_requested)
        overlay.abort_button.click()
        assert len(spy) == 1
        overlay.stop()
        assert not overlay.is_abortable()
        assert not overlay.abort_button.isEnabled()
        # Nascondere la scheda a operazione in corso (revisione dei corpi) NON
        # e' la fine del lavoro: li' il comando deve sopravvivere.
        overlay.start('Analisi AI in corso', 'Preparazione…', abortable=True)
        overlay.hide()
        overlay.show()
        assert overlay.is_abortable()
    finally:
        overlay.stop()
        parent.close()


def test_overlay_animation_resize_literal_status_and_cleanup(qapp):
    app = qapp
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


# --- Dati locali -> nuvola AI (utente, 2026-09-26) ---------------------------

from kicadgen.gui.analysis_overlay import TOOL_ALIASES, TRANSFER_SOURCES, TransferAnimation


def test_a_remote_call_shows_the_local_data_leaving_for_the_cloud(overlay):
    """Positivo: la chiamata remota sui documenti mostra i documenti a sinistra
    della nuvola; quella sulla foto mostra la scheda. Stesso nome e stesso
    anello: cambia solo cio' che parte."""
    overlay.set_tool("llm", "vendor/model")
    assert overlay.transfer.isVisibleTo(overlay)
    assert overlay.transfer.source == "documents"
    overlay.set_tool("llm_inventory", "vendor/model")
    assert overlay.transfer.isVisibleTo(overlay)
    assert overlay.transfer.source == "board"
    assert overlay.tool.text() == f"{TOOLS['llm'][0]} · vendor/model"
    assert overlay.spinner.glyph == TOOLS["llm"][2]


@pytest.mark.parametrize("tool", ["yolo", "freecad", "kicad", None, "strumento-mai-visto"])
def test_local_tools_send_nothing_and_show_only_the_ring(overlay, tool):
    """Negativo: il lavoro locale non spedisce dati, quindi niente pacchetti
    verso una nuvola che non c'e'."""
    overlay.set_tool("llm")
    overlay.set_tool(tool)
    assert not overlay.transfer.isVisibleTo(overlay)


def test_every_transfer_phase_is_a_known_remote_tool():
    """Contratto: una sorgente di trasferimento esiste solo per chiamate remote."""
    from kicadgen.gui.analysis_overlay import REMOTE_GLYPHS
    for key in TRANSFER_SOURCES:
        assert TOOLS[TOOL_ALIASES.get(key, key)][2] in REMOTE_GLYPHS
    # And the other way round: every remote glyph belongs to a paid phase.
    assert {glyph for _, _, glyph in TOOLS.values() if glyph in REMOTE_GLYPHS} == set(REMOTE_GLYPHS)


def test_the_two_sources_draw_different_pictures(overlay):
    """Positivo: documenti e scheda si distinguono dai pixel, non solo dal nome;
    i pacchetti si muovono a ogni passo dell'animazione."""
    widget = TransferAnimation()

    def picture():
        image = QImage(widget.size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        widget.render(image)
        return image

    widget.set_source("documents", "#f9e2af")
    documents = picture()
    widget.set_source("board", "#f9e2af")
    board = picture()
    assert documents != board
    widget.advance(0.1)
    assert picture() != board


def test_the_header_keeps_the_ring_height(overlay):
    """Negativo: mostrare i dati in partenza non deve far sobbalzare la scheda."""
    overlay.set_tool("yolo")
    QApplication.processEvents()
    before = overlay.card.height()
    overlay.set_tool("llm")
    QApplication.processEvents()
    assert overlay.card.height() == before


def test_opencv_is_a_local_tool_of_its_own(overlay):
    """User, 27/09: the OpenCV steps are shown as such, local and free, with their own mark."""
    from kicadgen.gui.analysis_overlay import TOOLS, TRANSFER_SOURCES
    name, color, glyph = TOOLS["opencv"]
    assert name == "OpenCV"
    assert glyph == "aperture"
    assert glyph != TOOLS["consolidated"][2]
    assert "opencv" not in TRANSFER_SOURCES                  # nulla lascia la macchina
    others = [spec for key, spec in TOOLS.items() if key != "opencv"]
    assert glyph not in {g for _, _, g in others}
    assert color not in {c for _, c, _ in others if c != "#cba6f7"}
    overlay.set_tool("opencv", "ricerca e calibrazione della scheda")
    assert overlay.tool.text().startswith("OpenCV")


def test_the_smd_network_has_its_own_phase_not_the_detector_one(overlay):
    """@brief La rete SMD si vede nella modale come tale (utente, 2026-10-02).

    @note Era un alias di YOLO: la classificazione locale si presentava come un
        secondo rilevamento. Caso negativo: YOLO resta il suo segno.
    """
    from kicadgen.gui.analysis_overlay import REMOTE_GLYPHS, TOOL_ALIASES
    assert "smd" not in TOOL_ALIASES
    name, color, glyph = TOOLS["smd"]
    assert name == "Rete SMD locale" and glyph == "network"
    assert glyph not in REMOTE_GLYPHS
    assert TOOLS["yolo"][2] == "frame" != glyph
    overlay.set_tool("smd", "smd_classifier.pt (v3)")
    assert (overlay.spinner.glyph, overlay.spinner.color) == (glyph, color)
