"""@brief Non-blocking progress overlay: which tool is working, real progress only."""
import html
import math
import re
from time import monotonic

from ..services.messages import WARNING, kind_of

from PyQt6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPalette,
                         QPen, QTextDocument)
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                             QVBoxLayout, QWidget)

#: Gli strumenti che possono lavorare durante un'analisi, ognuno col proprio
#: segno grafico. La distinzione che conta per chi guarda non e' estetica: il
#: rilevatore locale non costa nulla e dura poco, la chiamata al modello si
#: paga, il CAD tiene occupata la macchina per minuti. Il colore da solo non
#: basta a dirlo, quindi ogni strumento ha anche una forma diversa.
#:
#: La generazione ha tre passi e ognuno dice CHI lavora (utente, 2026-09-26:
#: «voglio vedere bene quando entra in azione FreeCAD o KiCad»): il footprint
#: lo scrive il codice di questo programma (KiCad non e' in esecuzione); la
#: scelta dei modelli legge i FILE della libreria 3D di KiCad e, per i
#: candidati non ancora misurati, avvia FreeCAD (`freecad_measure`, stesso
#: colore di FreeCAD, segno del calibro); il solido lo costruisce FreeCAD.
#:
#: OpenCV (utente, 2026-09-27: «hai evidenziato lo step OpenCV?») lavora in
#: locale sulla fotografia: trova e calibra la scheda, poi misura i bordi dei
#: corpi proposti. Prima questi passi non erano annunciati e la modale restava
#: sulla fase precedente mentre giravano.
TOOLS = {
    "llm": ("Modello AI", "#f9e2af", "cloud"),
    # Le due fasi a pagamento che seguono l'inventario globale, con un nome
    # proprio (utente, 2026-09-29, HANDOFF 156): «Modello AI — in corso» per
    # due minuti non diceva che cosa si stava pagando.
    "llm_crops": ("Lettura ritagli", "#f9e2af", "crop"),
    "llm_markings": ("Lettura sigle", "#f9e2af", "tag"),
    "yolo": ("YOLO", "#a6e3a1", "frame"),
    # La rete neurale che classifica i corpi tenuti (utente, 2026-10-02: «devi
    # anche darne contezza nella modale»). Era un alias di YOLO, quindi la
    # classificazione locale si presentava come un secondo rilevamento.
    "smd": ("Rete SMD locale", "#f5c2e7", "network"),
    "footprint": ("KiCadGen (footprint 2D, calcolo locale)", "#89b4fa", "pads"),
    "kicad": ("Libreria modelli 3D di KiCad", "#94e2d5", "library"),
    "consolidated": ("Consolidati — OpenCV", "#94e2d5", "lens"),
    "freecad_measure": ("FreeCAD (misura dei modelli)", "#cba6f7", "caliper"),
    "freecad": ("FreeCAD (modellazione locale)", "#cba6f7", "cube"),
    "opencv": ("OpenCV", "#fab387", "aperture"),
}

#: Glifi delle fasi che chiamano un modello remoto a pagamento: la nuvola e
#: le sue due specializzazioni (ritaglio, sigla). Sono le sole fasi che
#: spediscono dati (TRANSFER_SOURCES) e che si interrompono con la conferma
#: della spesa.
REMOTE_GLYPHS = frozenset({"cloud", "crop", "tag"})

#: Fasi che sono la *stessa* chiamata remota di una voce di TOOLS (stesso nome,
#: colore e segno) ma partono da dati diversi. Restano fuori da TOOLS perche'
#: la', per contratto, ogni strumento ha una forma sua: qui cambia solo cio'
#: che viene spedito, non chi lavora.
TOOL_ALIASES = {
    "llm_inventory": "llm",
}

#: Cosa parte dalla macchina verso il modello remoto, per le fasi che spediscono
#: dati: "documents" sono i documenti dell'estrazione, "board" la fotografia
#: della scheda con i corpi proposti dal rilevatore (inventario globale). Una
#: fase assente da qui non spedisce nulla e mostra solo l'anello.
TRANSFER_SOURCES = {
    "llm": "documents",
    "llm_inventory": "board",
    "llm_crops": "board",
    "llm_markings": "board",
}

#: Righe che il programma scrive per se' stesso: marcatori delle chiamate AI,
#: esiti di importazione, impronte sha256. Vanno nel log, dove servono a
#: ricostruire una corsa, non sulla scheda, dove occupano il posto di cio' che
#: sta accadendo senza dirlo a nessuno.
#:
#: Il marcatore si cerca in TESTA alla riga, dopo l'eventuale prefisso di
#: inoltro ("FreeCAD: ") con cui chi rilancia l'uscita di freecadcmd marca la
#: provenienza. Serve almeno un underscore fra maiuscole, cosi' una parola sola
#: come "ATTENZIONE:" resta una frase per chi guarda.
#:
#: L'ancora non e' un dettaglio: UPPER_SNAKE e' in questo dominio anche la
#: convenzione dei reference designator e dei nomi di libreria (U1_MODULE,
#: J_HEADER_BLK, RX_TX, SW_BOOT). Cercando il marcatore *ovunque* nella riga la
#: regola diventava di fatto una regola sui nomi dei componenti dell'utente e
#: scartava esiti veri («Componente J_HEADER_BLK non riconosciuto: geometria
#: generica.», «ATTENZIONE 3D: componente U1_MODULE (rf_module): geometria
#: approssimata»). In una frase il designator non e' mai la prima parola,
#: perche' la frase nomina prima la cosa; in una riga di servizio il marcatore
#: apre la riga perche' e' l'intestazione del record.
#:
#: Dopo il marcatore si accetta ":"/"=" (forma forte, priva di ambiguita'),
#: la fine della riga, oppure uno spazio. L'ultima alternativa e' un'euristica
#: DICHIARATA: serve per i dump che questo programma scrive di se'
#: ("AI_CALL_PROMPT_SYSTEM <testo del prompt>", "AI_CALL_START id=1 ...") e si
#: appoggia solo alla posizione, quindi scarterebbe anche un'ipotetica frase
#: che *inizi* con un designator. Diventerebbe superflua il giorno in cui
#: ai_diagnostics separasse marcatore e payload con ":" come fanno i marcatori
#: KICADGEN_*.
_MACHINE_MARKER = re.compile(
    r"^(?:\w+:\s+)?[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?:\s*[:=]|\s|$)")

#: Il marcatore sopra riconosce solo cio' che *questo* programma stampa per se'.
#: Sulla scheda arrivavano anche le righe che i programmi esterni scrivono sul
#: proprio stdout (esiti di scrittura file, intestazioni di sessione, conteggi
#: di entita'): non hanno un marcatore, quindi vanno riconosciute dalla *forma*.
#: Le regole qui sotto non nominano nessun prodotto e nessuna frase precisa --
#: agganciarle a "FreeCAD" o all'elenco delle frasi viste in una corsa vorrebbe
#: dire rifare il lavoro alla prossima versione dello stesso strumento.
#:
#: Ogni regola e' un'euristica tipografica, tarata su come scrive una macchina
#: e su come scrive una persona; sono dichiarate tali perche' nessuna e' una
#: verita' generale, e diventerebbero superflue se gli strumenti esterni
#: marcassero le proprie righe come fa questo programma.

#: Decorazione: tre o piu' ripetizioni dello stesso segno di interpunzione
#: ("******", "=====", "-----"). Sono esclusi il punto, il punto esclamativo e
#: il punto di domanda: i puntini di sospensione e la punteggiatura enfatica
#: ("Attenzione!!! il modello non ha risposto") sono normalissimi in una frase
#: scritta per chi guarda, e una decorazione da tracciato non e' mai fatta di
#: segni che in prosa portano intonazione.
_DECORATION_RUN = re.compile(r"([^\w\s.!?])\1{2,}")

#: Il punto escluso sopra va comunque distinto dalla sospensione: l'ellissi in
#: prosa e' esattamente "..." o "…", mentre "Recompute......" e' il riempimento
#: di una barra di avanzamento testuale. Quattro punti o piu' non sono
#: punteggiatura in nessuna delle due lingue usate qui.
_DOT_FILL_RUN = re.compile(r"\.{4,}")

#: Chiave e valore separati da spazio-due punti-spazio ("Step File Name : C:/…",
#: "WorkSession : Sending all data"). In prosa italiana o inglese i due punti
#: stanno attaccati alla parola che precede, quindi lo spazio davanti e' il
#: segno che la riga e' un campo di un tracciato, non una frase.
_SPACED_KEY_VALUE = re.compile(r"\S\s+:\s+\S")

#: Record "chiave=valore": un'etichetta breve e poi SOLO coppie, fino a fine
#: riga ("FreeCAD exit=0, timeout=False", "Inventario globale: prompt=…;
#: schema=…; sha256=…", "AI_CALL_END id=1 elapsed=65.664s tokens_in=16765").
#: L'uguale attaccato al nome del campo e' come una macchina serializza un
#: record, mentre in prosa l'uguale porta gli spazi ("drill_mm = 0 e
#: pad_diameter_mm = larghezza stimata"). Rende vera la promessa del docstring
#: di set_status: le righe con sha256 restano nel log.
#:
#: EURISTICA DICHIARATA sull'etichetta: al massimo due parole prima della prima
#: coppia. Un record e' il NOME di cio' che si registra piu' i suoi campi; se
#: fra il nome e i campi compare gia' un dato scritto per chi guarda la riga e'
#: una frase, non un record. E' il solo criterio che separa «Inventario
#: globale: prompt=…; schema=…» (record) da «Generazione TEST: 2x22,
#: pitch=2.54mm, row_spacing=22.86mm, pad=1.7mm, drill=1.0mm» (riepilogo di
#: cio' che si sta generando, da mostrare): le due forme sono per il resto
#: identiche, quindi la separazione e' fragile e va pinzata da test in
#: entrambi i versi. Diventerebbe superflua se i record portassero un
#: marcatore come quelli KICADGEN_*.
_KEY_VALUE_RECORD = re.compile(r"^(?:\S+\s+){0,2}\w+=\S+(?:[;,]?\s+\w+=\S+)+$")

#: Percentuale nuda, senza soggetto ("(47 %)", "FreeCAD: (100 %)"). Non dice a
#: cosa si riferisce ne' quanto manca, e resta appesa per tutta la fase del CAD
#: al posto delle righe che dicono quale componente si sta costruendo. E' lo
#: stesso motivo per cui set_progress rifiuta le stime sul tempo: un numero
#: senza referente sembra un'informazione e non lo e'. La barra vera, quando un
#: conteggio esiste, ha una riga propria.
_BARE_PERCENT = re.compile(r"^(?:\w+:\s+)?\(?\s*\d+(?:[.,]\d+)?\s*%\s*\)?\.?$")

#: Intestazioni di avvio di un programma esterno: nome piu' versione
#: semantica ("FreeCAD 1.1.1, Libs: 1.1.1R20260414") e riga di copyright
#: ("(C) 2001-2026 …"). Entrambe sono ancorate in testa, quindi non possono
#: agganciare una quota o un anno citati dentro una frase.
_VERSION_BANNER = re.compile(r"^(?:\w+:\s+)?\S+ \d+\.\d+\.\d+\b")
_COPYRIGHT_BANNER = re.compile(r"^(?:\w+:\s+)?(?:\(c\)|©)\s*\d{4}", re.IGNORECASE)

#: Marca temporale in testa alla riga, con o senza parentesi quadre
#: ("[2026-09-21T00:08:36+02:00] FreeCAD start", "2026-09-21 00:04:13 Analisi
#: AI"). Una riga che si auto-datifica e' un record di sessione: l'orologio
#: della scheda dice gia' da quanto si aspetta.
_TIMESTAMP_HEAD = re.compile(r"^\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

#: Conteggio di entita' fra parentesi: numero grande seguito da un'abbreviazione
#: corta e NON dentro una frase conclusa ("…/Test.step(134238 ents) Write Done",
#: "Shapes written (98211 ents)").
#:
#: SEPARAZIONE DICHIARATA FRAGILE. Dal solo conteggio non si distingue
#: "(134238 ents)" da "(12345 pezzi)": "pezzi", "test", "corpi", "righe",
#: "fori" stanno tutti nelle sei lettere dell'abbreviazione, quindi il numero di
#: cifre sarebbe l'unico criterio, e un criterio solo su due non basta (vedi
#: CLAUDE.md). Il secondo criterio e' tipografico: la prosa chiude con un segno
#: di fine frase, il record no. Cosi' "Importati (12345 pezzi) dal modello
#: STEP." e "Suite locale: (1061 test) superati." restano sulla scheda. Anche
#: questo resta un ripiego: una riga di prosa senza punto finale e con un
#: conteggio a quattro cifre verrebbe scartata. Diventerebbe superflua se
#: freecadcmd marcasse le proprie righe come fa questo programma.
_ENTITY_COUNT = re.compile(r"\(\s*\d{4,}\s+\w{1,6}\.?\s*\)(?![^.!?…]*[.!?…]\s*$)")

_MACHINE_LINE_RULES = (_MACHINE_MARKER, _DECORATION_RUN, _DOT_FILL_RUN,
                       _SPACED_KEY_VALUE, _KEY_VALUE_RECORD, _BARE_PERCENT,
                       _VERSION_BANNER, _COPYRIGHT_BANNER, _TIMESTAMP_HEAD,
                       _ENTITY_COUNT)

#: Percorso assoluto del filesystem: lettera di unita' ("C:/…", "D:\\…"),
#: percorso UNC, o percorso POSIX con almeno due livelli ("/home/x/…").
#:
#: Il percorso NON e' piu' un motivo per scartare la riga. Questo e' uno
#: strumento il cui prodotto SONO file: «Inventario salvato: …», «Proposte del
#: rilevatore salvate: …» e soprattutto «Inventario non salvato su disco:
#: [Errno 28] No space left on device: '…'» sono esiti che l'utente deve
#: leggere, e la vecchia regola li nascondeva tutti, compreso un errore di
#: scrittura, perche' il testo dell'OSError porta un percorso. Fra le due
#: soluzioni proposte si sceglie di ACCORCIARE il percorso e tenere la riga: il
#: criterio "quasi solo percorso" avrebbe scartato proprio «Inventario salvato:
#: <tre percorsi>», che e' la riga da mostrare. Del percorso resta il nome del
#: file, che e' l'unica parte che l'utente riconosce; il testo integrale e' nel
#: log, a un clic di distanza.
#:
#: Il lookbehind su "/" evita di accorciare l'interno di una URL, e il secondo
#: livello obbligatorio lascia intatti gli slug dei modelli
#: ("anthropic/claude-opus-5") e i nomi di file senza separatori.
_DRIVE_PATH = re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s;,'\")]*")
_UNC_PATH = re.compile(r"\\\\[\w.$-]+\\[^\s;,'\")]*")
_POSIX_PATH = re.compile(
    r"(?:^|(?<=[\s(\"'=]))(?<!/)~?/[\w.+-]+/[^\s;,'\")]*")

_PATH_RULES = (_DRIVE_PATH, _UNC_PATH, _POSIX_PATH)

#: Lo stato viene troncato: oltre questa lunghezza la riga non si legge a colpo
#: d'occhio e il testo integrale e' comunque nel log.
_STATUS_CHAR_LIMIT = 220

#: Separatore con cui questo programma elenca piu' VOCI dentro una sola riga di
#: avanzamento: i documenti che partono nella chiamata a pagamento
#: (`datasheet_extractor._files_sent_message`), i motivi di un inventario
#: parziale (`photo_global_inventory._DETAIL_SEPARATOR`).
#:
#: La riga resta una: il log vive di un evento per riga, con una marca
#: temporale sola, e spezzarla li' renderebbe una corsa illeggibile a ritroso.
#: La scheda invece ha bisogno dell'elenco -- richiesta dell'utente: «i nomi
#: dei file uno sotto l'altro» -- quindi e' QUI che la riga si rimpagina, senza
#: che la sorgente sappia nulla della scheda.
#:
#: Il punto mediano non e' un vezzo: in italiano e in inglese non e' un segno
#: di punteggiatura, quindi in una frase scritta per chi guarda non compare, e
#: le due sorgenti sopra lo usavano gia' per elencare senza ricorrere al punto
#: e virgola, che i filtri della modale contano. Riconoscere l'elenco da questo
#: separatore e' quindi una regola sulla FORMA, valida per qualunque riga che
#: elenchi, non una regola sul messaggio dei documenti.
_LIST_SEPARATOR = " · "

#: Separatore fra il TERMINE di una voce e la sua descrizione: «pinout.png —
#: Pinout / disposizione contatti». E' la convenzione tipografica della voce di
#: un elenco di definizioni (termine, lineetta, spiegazione), e la scheda la
#: rende come tale: il termine in grassetto, la lineetta spaziata che lo stacca
#: dal resto. Richiesta dell'utente: «i nomi dei file non sono evidenziati,
#: troppo attaccati al testo».
#:
#: Regola sulla FORMA, come le due precedenti: non guarda se il termine
#: "sembra" un nome di file (un nome puo' avere spazi, nessuna estensione,
#: maiuscole e underscore), guarda solo se la voce DICHIARA un termine con
#: questo segno. La sorgente dei documenti lo scrive apposta
#: (`datasheet_extractor._files_sent_message`); una voce che non lo porta -- i
#: motivi di un inventario parziale, «component J2: …» -- resta testo semplice.
#:
#: SEPARAZIONE DICHIARATA FRAGILE: il segno non e' riservato. Un nome di file
#: che contenga esso stesso " — " verrebbe diviso li' (grassetto solo sulla
#: prima parte; il testo resta tutto, cambia solo il peso), e un testo altrui
#: interpolato in una voce (l'errore di un'API) che lo contenga avrebbe la
#: prima parte in grassetto. Si divide sulla PRIMA occorrenza perche' il
#: termine sta in testa per definizione. Diventerebbe superflua se lo stato
#: arrivasse alla scheda come dato strutturato invece che come stringa.
_TERM_SEPARATOR = " — "

#: Righe di testo riservate allo stato. La scheda e' dimensionata su questo
#: numero, non sulla frase che passa in quel momento: una scheda che cambia
#: altezza a ogni messaggio e' piu' fastidiosa di una scheda un po' piu' alta.
_STATUS_LINES = 3
#: Righe su cui lo stato troncato viene DISTRIBUITO per ricavare la larghezza
#: della scheda: meno righe = scheda piu' larga. Erano 2,5 (tre righe con
#: mezza riga di margine); l'utente (2026-09-30, HANDOFF 173) la voleva piu'
#: larga, cosi' le righe delle fasi si troncano piu' tardi: due righe, quindi
#: circa un quarto in piu', sempre entro la larghezza che la finestra concede.
#: Le tre righe riservate restano: una intera di margine per le lettere larghe.
_STATUS_WRAP_LINES = 2.0
#: Completed phases listed on the card (HANDOFF point 135): the most recent
#: ones, one row each. Bounded so the card cannot outgrow a small window.
PHASE_HISTORY_ROWS = 4

#: Margine interno orizzontale della scheda (per lato), usato sia dal layout sia
#: dal calcolo della larghezza: vanno d'accordo per costruzione.
_CARD_PADDING = 28

#: Margine fra il bordo della finestra e la scheda (per lato). E' lo stesso
#: valore dei contentsMargins dell'overlay, e serve anche a dedurre quanta
#: larghezza la scheda puo' davvero occupare.
_OVERLAY_MARGIN = 24

#: Larghezza minima desiderata: evita una scheda smilza con caratteri molto
#: stretti. NON esiste piu' un massimo costante: il tetto si deduce dalla
#: finestra che ospita l'overlay (vedi _available_card_width). Un tetto fisso
#: (760 px) annullava la derivazione dal font e TAGLIAVA il testo dai 14 pt in
#: su, cioe' proprio nel caso -- font grande, ipovisione -- in cui la scheda
#: serve di piu'.
_CARD_MIN_WIDTH = 480

#: Larghezza da usare quando la finestra non ha ancora una dimensione (primo
#: layout): e' un ripiego temporaneo, il primo resize la sostituisce.
_CARD_FALLBACK_WIDTH = 960

#: Gemella della precedente per l'altezza: serve a decidere quante righe di
#: elenco la finestra concede quando la finestra (e lo schermo) non hanno
#: ancora una dimensione. Stesso ripiego temporaneo, stessa vita breve.
_CARD_FALLBACK_HEIGHT = 720

#: Frase peggiore possibile della lunghezza massima consentita, fatta di parole
#: di lettere larghe: serve a contare le righe che lo stato occupa DAVVERO alla
#: larghezza scelta, invece di fidarsi della larghezza media dei caratteri.
_WORST_CASE_STATUS = " ".join(["M" * 8] * 30)[:_STATUS_CHAR_LIMIT]


def is_machine_line(text):
    """@brief Tell a line written for the log from a line written for a person.

    @param text One status line, already collapsed to single spaces.
    @return True when the line is machine bookkeeping and belongs in the log.
    @note Recognition is structural (a record header in first position,
        decoration or dot-fill runs, spaced key/value fields, key=value records,
        referent-less percentages, start-up banners, self-dated lines and entity
        counters); no rule names a product or a specific sentence, so a new
        external tool needs no new rule. A human sentence carrying quotes,
        parentheses, units, emphatic punctuation or a double dash keeps none of
        those shapes and still reaches the card.
    @note Naming a file is NOT one of those shapes: the outcome of this program
        is files, so a path is shortened for display (see elide_paths) instead
        of costing the line its place on the card.
    """
    line = " ".join(str(text or "").split())
    return any(rule.search(line) for rule in _MACHINE_LINE_RULES)


def enumeration_lines(text):
    """@brief Split a one-line enumeration into the lines a card can show.

    @param text One status line, as it is written to the log.
    @return The lines to show -- the lead-in first, then ONE item per line --
        or None when the line is not an enumeration and must stay on one line.
    @note Recognized shape, structural and not tied to any sender: a lead-in
        ending with a colon, then items separated by `_LIST_SEPARATOR`. Both
        signals are required. The separator alone would turn any sentence that
        happens to carry a middle dot into a list; the colon alone is ordinary
        punctuation. Two items are enough to be a list: with a single document
        there is nothing to line up, and the line stays as it is.
    @note EURISTICA DICHIARATA sul taglio della testa: si usa il PRIMO ": "
        della riga, non l'ultimo. L'elenco comincia dove il testo lo annuncia,
        mentre i due punti che compaiono dopo appartengono alla voce che li
        contiene: gli esiti di questo programma scrivono voci come «component
        U1: merged box excludes…», e tagliando sull'ultimo ": " la prima voce
        perderebbe proprio il soggetto. Sarebbe superflua il giorno in cui le
        righe portassero l'elenco in forma strutturata invece che punteggiata;
        finche' il canale e' una stringa sola, la punteggiatura e' il canale.
    @note Separators in a row count as one: an item can carry a text that is
        not ours (the reason an API gave for failing) and that came back empty,
        and the alternative is a blank row, or a stray middle dot opening the
        next line. Fewer than two items left means there is nothing to line up,
        and the line stays one line.
    @note The log is never reflowed: this function is presentation, the caller
        keeps the full line for `_append_log` and for the log file, where one
        event is one line with one timestamp.
    """
    line = " ".join(str(text or "").split())
    if _LIST_SEPARATOR not in line:
        return None
    head, colon, first_item = line[:line.index(_LIST_SEPARATOR)].partition(": ")
    if not colon or not head.strip():
        return None
    tail = first_item + line[line.index(_LIST_SEPARATOR):]
    items = [item.strip().strip("·").strip() for item in tail.split(_LIST_SEPARATOR)]
    items = [item for item in items if item]
    return [f"{head}:"] + items if len(items) > 1 else None


def enumeration_term(item):
    """@brief Split one enumeration item into its declared term and description.

    @param item One item line, as `enumeration_lines` returns it (lead-in
        excluded: the lead-in announces the list, it is not a term).
    @return (term, description) when the item declares a term with
        `_TERM_SEPARATOR` and both sides carry text; None otherwise, and the
        item is then shown as plain text.
    @note The rule is on the separator only, never on what the term looks
        like: a file name may have spaces, no extension or UPPER_SNAKE words,
        and a shape test would highlight some documents of a list and not the
        others. See `_TERM_SEPARATOR` for the declared fragility.
    """
    term, separator, description = str(item or "").partition(_TERM_SEPARATOR)
    term, description = term.strip(), description.strip()
    if not separator or not term or not description:
        return None
    return term, description


def _elide_row(line, width, regular, bold, is_item):
    """Accorcia UNA riga dell'elenco alla larghezza vera, sul testo semplice.

    Restituisce (termine, testo): il termine e' None per le righe senza
    termine dichiarato. Si accorcia PRIMA di costruire il markup, cosi' il taglio
    non puo' cadere dentro un tag o dentro un'entita' HTML, e si misura il
    termine col font in grassetto con cui verra' disegnato: misurato col peso
    normale, il grassetto sborderebbe di qualche pixel.

    Quando la riga non sta, si accorcia prima il TERMINE nel mezzo (restano
    l'inizio e la fine, cioe' l'estensione di un nome di file) e la
    descrizione resta intera, purche' la descrizione col separatore occupi al
    massimo meta' della riga. Il termine e' un dato lungo quanto vuole chi lo
    ha scritto; la descrizione, nelle righe che la portano (il ruolo di un
    documento), e' un'etichetta breve che dice la cosa da sapere. Prima si
    tagliava la descrizione: con i caratteri di Windows un nome gia'
    accorciato a 40 caratteri dal mittente lasciava «Foto ad alta risoluzio…».

    La meta' e' una scelta dichiarata, non una misura: nessuna delle due parti
    toglie all'altra piu' dello spazio che lascia. Non e' tarata su nessun
    caso; sarebbe superflua se le righe dell'elenco potessero andare a capo.
    Oltre quella soglia vale la regola di `ElideRight`: si tiene la TESTA, si
    accorcia la descrizione; se non resta posto neppure per l'ellissi dopo il
    termine, si accorcia il termine e la descrizione sparisce.
    """
    parts = enumeration_term(line) if is_item else None
    if parts is None:
        return None, regular.elidedText(line, Qt.TextElideMode.ElideRight, width)
    term, description = parts
    lead = bold.horizontalAdvance(term) + regular.horizontalAdvance(_TERM_SEPARATOR)
    if lead + regular.horizontalAdvance(description) <= width:
        return term, description
    tail = regular.horizontalAdvance(_TERM_SEPARATOR + description)
    if tail <= width / 2:
        return bold.elidedText(term, Qt.TextElideMode.ElideMiddle, width - tail), description
    if lead + regular.horizontalAdvance("…") <= width:
        return term, regular.elidedText(description, Qt.TextElideMode.ElideRight,
                                        width - lead)
    return bold.elidedText(term, Qt.TextElideMode.ElideRight, width), ""


def _row_width(row, font):
    """Larghezza di UNA riga come la disegna il documento della label."""
    document = QTextDocument()
    document.setDefaultFont(font)
    document.setDocumentMargin(0)
    document.setHtml(_enumeration_markup([row]))
    return document.idealWidth()


def _fit_row(line, width, font, is_item):
    """Accorcia una riga finche' il documento che la disegna sta in `width`.

    Le metriche del font sommate pezzo per pezzo (termine in grassetto, poi il
    resto) possono differire di una frazione di pixel dal layout del documento,
    che posiziona i glifi a modo suo. Invece di un margine scelto a occhio si
    MISURA la riga come verra' disegnata e, se sborda, si riaccorcia togliendo
    esattamente l'eccesso. Il ciclo e' corto per costruzione: ogni giro riduce
    lo spazio di almeno un pixel, e l'eccesso e' una frazione di glifo.
    """
    regular = QFontMetricsF(font)
    bold_font = QFont(font)
    bold_font.setBold(True)
    bold = QFontMetricsF(bold_font)
    budget = float(width)
    row = _elide_row(line, budget, regular, bold, is_item)
    for _ in range(4):
        excess = _row_width(row, font) - width
        if excess <= 0:
            break
        budget -= math.ceil(excess)
        row = _elide_row(line, budget, regular, bold, is_item)
    return row


def _enumeration_markup(rows):
    """Markup dell'elenco: una riga per voce, termine in grassetto, nulla a capo.

    Tutto cio' che arriva dalle righe e' ESCAPATO: nomi di file e testi altrui
    sono dati, e un nome come «<b>x</b>.png» deve leggersi com'e', non
    cambiare il peso del resto. `white-space: pre` vieta l'a capo: ogni riga e'
    gia' stata accorciata alla larghezza vera, quindi il numero di righe
    disegnate e' ESATTAMENTE il numero di voci, ed e' su quel numero che la
    scheda si dimensiona.
    """
    shown = []
    for term, text in rows:
        if term is None:
            shown.append(html.escape(text, quote=False))
            continue
        row = f"<b>{html.escape(term, quote=False)}</b>"
        if text:
            row += html.escape(_TERM_SEPARATOR + text, quote=False)
        shown.append(row)
    return '<div style="white-space:pre">' + "<br>".join(shown) + "</div>"


def elide_paths(text):
    """@brief Shorten absolute paths to "…<separator><file name>".

    @param text One status line.
    @return The same line with every absolute path reduced to its file name,
        preceded by an ellipsis so it is visible that the path was cut.
    @note This is what replaces dropping the line: on a card two hundred
        characters wide a full Windows path is three quarters of the room, but
        the file name is exactly the part the user recognises, and the outcome
        that names it ("Inventario salvato: …", "Inventario non salvato su
        disco: [Errno 28] …") is one of the few lines worth the space.
    @note A model slug ("anthropic/claude-opus-5"), a bare file name and the
        path inside a URL have no second separator preceded by a word boundary
        and are left untouched.
    """
    line = str(text or "")
    for rule in _PATH_RULES:
        line = rule.sub(_shorten_path, line)
    return line


def _shorten_path(match):
    """Keep the file name of one matched path, with the cut made visible."""
    path = match.group(0)
    separator = "\\" if "\\" in path else "/"
    name = path.rstrip(separator).rpartition(separator)[2]
    return f"…{separator}{name}" if name else path


class ElidedLabel(QLabel):
    """@brief One-line label that shortens its own text instead of clipping it.

    @note Qt centres a non-wrapping QLabel and then cuts it on BOTH sides when
        it does not fit: the header lost the first word ("Modello") and the last,
        which is unreadable rather than merely short. Painting the elided text
        keeps the beginning -- the tool and the model, the two things the header
        exists to say -- and makes the cut visible as an ellipsis.
    @note `text()` keeps returning the full string: main_window parses this
        label to know which detail the header already shows, so shortening the
        stored text would silently change that decision.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        #: Colore dell'inchiostro. Il foglio di stile di questa scheda cambia il
        #: colore per strumento, e qui si dipinge a mano: si tiene il valore
        #: invece di dedurlo dalla palette, che con un foglio di stile puo' non
        #: essere aggiornata.
        self.ink = None

    def paintEvent(self, event):
        """@brief Paint the text elided to the available width."""
        painter = QPainter(self)
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(
            self.text(), Qt.TextElideMode.ElideRight, rect.width())
        color = (QColor(self.ink) if self.ink
                 else self.palette().color(QPalette.ColorRole.WindowText))
        painter.setPen(color)
        painter.drawText(rect, int(self.alignment()), text)


class AnalysisSpinner(QWidget):
    """@brief Animated ring that also names the tool at work by its shape."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(56, 56)
        self.angle = 0
        self.glyph = ""
        self.color = "#89b4fa"

    def set_tool(self, glyph, color):
        """@brief Draw this tool's mark inside the ring.

        @param glyph One of the shapes in TOOLS, or "" for the bare ring.
        @param color Ring and mark color.
        """
        self.glyph, self.color = glyph, color
        self.update()

    def paintEvent(self, event):
        """@brief Paint the ring, its moving arc and the tool mark."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(7, 7, 42, 42)
        painter.setPen(QPen(QColor("#45475a"), 3))
        painter.drawEllipse(rect)
        pen = QPen(QColor(self.color), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, -self.angle * 16, 100 * 16)
        if self.glyph:
            self._paint_glyph(painter)

    def _paint_glyph(self, painter):
        """@brief Draw the tool mark centred in the ring.

        @note The marks span about 23x23 px inside the 42 px ring; none of them
            reaches the ring itself, so the arc stays readable while it turns.
        """
        pen = QPen(QColor(self.color), 1.6)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.glyph == "cloud":
            # Nuvola: la chiamata esce dalla macchina e si paga. I tre cerchi
            # vanno *uniti*, non sovrapposti: gli archi interni si leggevano
            # come tre bolle, cioe' come nessuna forma riconoscibile.
            path = QPainterPath()
            for box in (QRectF(18, 27, 12, 11), QRectF(25, 22, 15, 15),
                        QRectF(22, 30, 15, 8)):
                blob = QPainterPath()
                blob.addEllipse(box)
                path = path.united(blob)
            painter.drawPath(path.simplified())
        elif self.glyph == "crop":
            # Ritaglio: il riquadro ritagliato dalla foto, con dentro il corpo
            # pieno che il modello guarda da vicino. Stesso colore della
            # nuvola (si paga), forma diversa (test: due strumenti non si
            # disegnano mai uguali).
            painter.drawRect(QRectF(17, 17, 22, 22))
            painter.setBrush(QColor(self.color))
            painter.drawRoundedRect(QRectF(24, 24, 8, 8), 1.5, 1.5)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for x in (20.0, 36.0):
                painter.drawLine(QPointF(x, 28), QPointF(x + 2, 28))
            for y in (20.0, 36.0):
                painter.drawLine(QPointF(28, y), QPointF(28, y + 2))
        elif self.glyph == "tag":
            # Sigla: un'etichetta con due righe di testo, cio' che e' stampato
            # sul corpo e che il modello rilegge dal ritaglio ingrandito.
            painter.drawRoundedRect(QRectF(16, 20, 24, 16), 2, 2)
            painter.drawLine(QPointF(20, 26), QPointF(36, 26))
            painter.drawLine(QPointF(20, 31), QPointF(30, 31))
        elif self.glyph == "frame":
            # Mirino: quattro squadrette d'angolo piu' il bersaglio al centro.
            # Un riquadro chiuso si confondeva col footprint e col cubo alla
            # dimensione a cui viene disegnato; le squadrette staccate sono il
            # segno con cui un rilevatore marca un corpo, e il puntino pieno
            # dice che il riquadro e' *misurato*, non disegnato a mano.
            arm = 7.0
            for x, y, dx, dy in ((17, 17, 1, 1), (39, 17, -1, 1),
                                 (17, 39, 1, -1), (39, 39, -1, -1)):
                painter.drawLine(QPointF(x, y), QPointF(x + arm * dx, y))
                painter.drawLine(QPointF(x, y), QPointF(x, y + arm * dy))
            painter.drawLine(QPointF(28, 21), QPointF(28, 24))
            painter.drawLine(QPointF(28, 32), QPointF(28, 35))
            painter.drawLine(QPointF(21, 28), QPointF(24, 28))
            painter.drawLine(QPointF(32, 28), QPointF(35, 28))
            painter.setBrush(QColor(self.color))
            painter.drawEllipse(QRectF(26, 26, 4, 4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        elif self.glyph == "cube":
            # Cubo isometrico: si sta costruendo un solido.
            top, left, right = QPointF(28, 17), QPointF(18, 23), QPointF(38, 23)
            low_l, low_r, bottom = QPointF(18, 34), QPointF(38, 34), QPointF(28, 40)
            centre = QPointF(28, 28)
            for start, end in ((top, left), (top, right), (left, low_l),
                               (right, low_r), (low_l, bottom), (low_r, bottom),
                               (left, centre), (right, centre), (top, centre)):
                painter.drawLine(start, end)
        elif self.glyph == "library":
            # Libreria: tre dorsi di libro su uno scaffale, uno inclinato. Si
            # sceglie un file gia' esistente, non si costruisce nulla.
            for x, top in ((17.0, 19.0), (22.0, 17.0), (27.0, 20.0)):
                painter.drawRoundedRect(QRectF(x, top, 4, 38 - top), 1, 1)
            painter.drawLine(QPointF(33, 21), QPointF(38, 37))
            painter.drawLine(QPointF(15, 38), QPointF(41, 38))
        elif self.glyph == "lens":
            # Lente d'ingrandimento con una griglia di pixel dentro: OpenCV
            # guarda la fotografia e la misura, non rileva ne' costruisce.
            painter.drawEllipse(QRectF(17, 17, 16, 16))
            painter.drawLine(QPointF(31, 31), QPointF(39, 39))
            painter.drawLine(QPointF(25, 20), QPointF(25, 30))
            painter.drawLine(QPointF(20, 25), QPointF(30, 25))
        elif self.glyph == "aperture":
            # OpenCV: a camera iris, distinct from the KiCad library's pixel
            # magnifier. This phase measures pixels locally; it does not
            # search a library or send the photo away.
            points = [QPointF(28, 16), QPointF(38, 22), QPointF(38, 34),
                      QPointF(28, 40), QPointF(18, 34), QPointF(18, 22)]
            path = QPainterPath(points[0])
            for point in points[1:]:
                path.lineTo(point)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawEllipse(QRectF(24, 24, 8, 8))
            for end in (QPointF(28, 20), QPointF(34, 28), QPointF(28, 36),
                        QPointF(22, 28)):
                painter.drawLine(QPointF(28, 28), end)
        elif self.glyph == "caliper":
            # Calibro: asta con due becchi, uno fisso e uno scorrevole. FreeCAD
            # qui MISURA dei modelli esistenti, non costruisce il solido.
            painter.drawLine(QPointF(16, 22), QPointF(40, 22))
            painter.drawLine(QPointF(18, 22), QPointF(18, 36))
            painter.drawLine(QPointF(18, 36), QPointF(21, 33))
            painter.drawRect(QRectF(28, 19, 5, 6))
            painter.drawLine(QPointF(30.5, 25), QPointF(30.5, 36))
            painter.drawLine(QPointF(30.5, 36), QPointF(27.5, 33))
        elif self.glyph == "network":
            # Rete neurale: tre strati di nodi pieni collegati. Classifica in
            # locale cio' che il rilevatore ha trovato, senza spedire nulla.
            layers = ((19.0, (22.0, 34.0)), (28.0, (19.0, 28.0, 37.0)), (37.0, (24.0, 32.0)))
            for (x0, ys0), (x1, ys1) in zip(layers, layers[1:]):
                for y0 in ys0:
                    for y1 in ys1:
                        painter.drawLine(QPointF(x0, y0), QPointF(x1, y1))
            painter.setBrush(QColor(self.color))
            for x, ys in layers:
                for y in ys:
                    painter.drawEllipse(QRectF(x - 2.2, y - 2.2, 4.4, 4.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        elif self.glyph == "pads":
            # Footprint: due file di piazzole *piene* attorno al contorno del
            # corpo. Le piazzole vuote si leggevano come un altro riquadro; il
            # pieno del rame contro il filo di contorno distingue subito questo
            # segno dal solido del CAD, che e' tutto wireframe.
            # Tre piazzole per fila, larghe 4 px e distanti 4: il tratto del
            # contorno (1.6 px) ne mangia 1.6 per ogni vuoto, quindi con lo
            # stacco precedente di 1 px le tre si saldavano in una barra unica e
            # il segno si leggeva come due barre invece di sei piazzole.
            painter.setBrush(QColor(self.color))
            for row in (17.0, 35.0):
                for column in (18.0, 26.0, 34.0):
                    painter.drawRoundedRect(QRectF(column, row, 4, 4), 1.2, 1.2)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # Contorno del corpo fra le due file, staccato dalle piazzole, col
            # segno del pin 1 a sinistra: e' come si legge un footprint.
            painter.drawRect(QRectF(20, 24, 16, 8))
            painter.drawLine(QPointF(20, 27), QPointF(24, 27))


class TransferAnimation(QWidget):
    """@brief Local data icon with packets travelling right, towards the spinner.

    Sits at the left of the spinner while a remote model call is running: the
    spinner on the right carries the cloud, this widget carries what left the
    machine. Together they read «local data -> AI cloud».

    @note The packets say only that a remote request is in flight. They are
        NOT an upload or processing percentage: the HTTP call reports no
        progress, so a phase-by-phase animation would present a guess as a
        fact (same rule as `AnalysisOverlay.set_progress`).
    @note The board pictogram is a fixed drawing, not the detector's count:
        the number of real proposals is in the status line.
    """

    #: Travelling packets on the path at the same time.
    PACKETS = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(150, 56)
        self.source = "documents"
        self.color = "#f9e2af"
        #: Position of the first packet along the path, 0..1.
        self.offset = 0.0

    def set_source(self, source, color):
        """@brief Choose what is drawn as leaving the machine.

        @param source "documents" or "board" (see TRANSFER_SOURCES); anything
            else falls back to the documents icon rather than raising.
        @param color Packet color, the same as the tool at work.
        """
        self.source = source if source in ("documents", "board") else "documents"
        self.color = color
        self.update()

    def advance(self, step=0.012):
        """@brief Move the packets one animation step towards the cloud.

        @param step Fraction of the path covered per call (timer runs at 25 Hz).
        """
        self.offset = (self.offset + step) % 1.0
        self.update()

    def paintEvent(self, event):
        """@brief Paint the source icon and the packets moving to the right."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#cdd6f4"), 1.6)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self.source == "board":
            self._paint_board(painter)
        else:
            self._paint_documents(painter)
        # Binario tratteggiato fra i dati e la nuvola, poi i pacchetti.
        start, end, y = 56.0, float(self.width() - 8), 28.0
        rail = QPen(QColor("#45475a"), 1.4, Qt.PenStyle.DashLine)
        painter.setPen(rail)
        painter.drawLine(QPointF(start, y), QPointF(end, y))
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(self.PACKETS):
            t = (self.offset + index / self.PACKETS) % 1.0
            color = QColor(self.color)
            # Compaiono e svaniscono agli estremi invece di saltare.
            color.setAlphaF(max(0.0, min(1.0, 4 * t, 4 * (1 - t))))
            painter.setBrush(color)
            x = start + t * (end - start)
            painter.drawRoundedRect(QRectF(x - 3, y - 3, 6, 6), 1.5, 1.5)

    @staticmethod
    def _paint_documents(painter):
        """Two stacked sheets with text lines: the documents being sent."""
        painter.setBrush(QColor("#1e1e2e"))
        painter.drawRoundedRect(QRectF(16, 10, 22, 28), 2.5, 2.5)
        painter.drawRoundedRect(QRectF(10, 16, 22, 28), 2.5, 2.5)
        for row in (23.0, 28.0, 33.0, 38.0):
            painter.drawLine(QPointF(14, row), QPointF(28 if row < 38 else 22, row))

    @staticmethod
    def _paint_board(painter):
        """A board outline with a few bodies on it: the photographed module."""
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(6, 13, 40, 30), 3, 3)
        painter.setBrush(QColor("#cdd6f4"))
        for body in (QRectF(11, 18, 12, 9), QRectF(27, 18, 6, 4), QRectF(36, 19, 5, 7),
                     QRectF(27, 31, 14, 7), QRectF(12, 32, 5, 4)):
            painter.drawRect(body)


#: Icon size of the progress window commands, pixels (toolbars use 22).
OVERLAY_ICON_PX = 30


class AnalysisOverlay(QWidget):
    """@brief Present analysis progress and expose the AI log and abort actions.

    @note La scheda copre tutta la finestra e le schede sotto sono spente
        mentre si lavora: senza un comando che esca da qui l'unica via d'uscita
        era chiudere il programma. Il pulsante sta accanto a «Apri log AI»
        perche' quella riga e' l'unica parte della scheda su cui si puo'
        agire; cio' che l'interruzione *fa* dipende dal lavoro in corso e lo
        decide la finestra, non questa scheda (vedi `abort_requested`).
    """
    log_requested = pyqtSignal()
    #: Emesso al clic su «Interrompi». La scheda non sa che cosa stia
    #: lavorando -- un processo FreeCAD si uccide, una richiesta HTTP gia'
    #: partita no -- quindi non decide nulla: chiede, e chi ha avviato il
    #: lavoro sceglie. E' anche il motivo per cui questo modulo non importa
    #: main_window: la dipendenza va in un verso solo.
    abort_requested = pyqtSignal()
    #: Emesso quando l'utente chiude una scheda rimasta aperta a lavoro finito
    #: (`show_completed`): chi l'ha lasciata aperta riprende da li'.
    closed = pyqtSignal()
    #: Emesso all'inizio di un'operazione con il titolo gia' tradotto, e alla
    #: fine: la barra di stato della finestra dice «Pronto» o cosa e' in corso.
    operation_started = pyqtSignal(str)
    operation_finished = pyqtSignal()

    def __init__(self, parent, *, i18n=None):
        super().__init__(parent)
        self.i18n = i18n
        self.setObjectName("analysisOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("""
            QWidget#analysisOverlay { background: rgba(10, 12, 22, 205); }
            QFrame#analysisCard { background: #1e1e2e; border: 1px solid #585b70;
                                 border-radius: 16px; }
            QLabel { background: transparent; border: none; color: #cdd6f4; }
            QProgressBar { background: #313244; border: none; border-radius: 5px;
                           height: 10px; text-align: center; color: transparent; }
            QProgressBar::chunk { background: #89b4fa; border-radius: 5px; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*(4 * (_OVERLAY_MARGIN,)))
        outer.addStretch()
        self.card = QFrame()
        self.card.setObjectName("analysisCard")
        content = QVBoxLayout(self.card)
        content.setContentsMargins(_CARD_PADDING, 24, _CARD_PADDING, 24)
        content.setSpacing(16)
        self.spinner = AnalysisSpinner()
        # Per le chiamate remote i dati locali stanno a sinistra dell'anello con
        # la nuvola, e i pacchetti vanno verso di essa (utente, 2026-09-26). La
        # riga ha l'altezza dell'anello: comparire o sparire non fa sobbalzare
        # la scheda.
        self.transfer = TransferAnimation()
        self.transfer.hide()
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(0)
        header.addStretch()
        header.addWidget(self.transfer)
        header.addWidget(self.spinner)
        header.addStretch()
        self.header_row = QWidget()
        self.header_row.setLayout(header)
        content.addWidget(self.header_row)
        self.title = title = QLabel("Analisi AI in corso")
        title.setStyleSheet("font-size: 18pt; font-weight: 600;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(title)
        # Chi sta lavorando, in chiaro e accanto al suo segno. Senza questa
        # riga l'unico indizio erano i messaggi di servizio che scorrevano al
        # posto dello stato, che e' esattamente cio' che non si doveva leggere.
        # Una QLabel normale, centrata e senza a capo, veniva tagliata sui due
        # lati gia' al font predefinito (526 px disponibili contro 638
        # necessari): si perdeva la parola iniziale, quindi l'intestazione
        # diventava illeggibile proprio mentre altre righe vengono scartate
        # "perche' l'intestazione lo dice gia'".
        self.tool = ElidedLabel()
        self.tool.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tool.setProperty("i18n_skip", True)
        self.tool.setStyleSheet("font-weight: 600;")
        self.tool.hide()
        content.addWidget(self.tool)
        # I passi di un lavoro in piu' fasi (generazione): quello in corso in
        # evidenza, quelli fatti spuntati, quelli non disponibili barrati con
        # il motivo. Nascosta per le operazioni di una fase sola.
        self.steps = QLabel()
        self.steps.setObjectName("analysisSteps")
        self.steps.setTextFormat(Qt.TextFormat.RichText)
        self.steps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.steps.setWordWrap(True)
        self.steps.setProperty("i18n_skip", True)
        self.steps.hide()
        content.addWidget(self.steps)
        # Le fasi gia' concluse di questa operazione, con il loro esito
        # (utente, 2026-09-28): una fase locale come OpenCV dura meno di un
        # secondo e senza questo elenco non si vedeva nemmeno passare.
        self.phases = QLabel()
        self.phases.setObjectName("analysisPhases")
        self.phases.setTextFormat(Qt.TextFormat.RichText)
        self.phases.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.phases.setProperty("i18n_skip", True)
        self.phases.hide()
        content.addWidget(self.phases)
        #: Fasi concluse: (nome dello strumento, esito, e' un avviso), come
        #: SORGENTI italiane. Si traducono quando si disegnano, cosi' un cambio
        #: di lingua a meta' corsa ridisegna l'elenco nella lingua nuova
        #: (HANDOFF 138, C7; vedi `refresh_command_texts`).
        self._phase_history: list[tuple[str, str, bool]] = []
        #: Vero dall'avvio di un'operazione finche' non dichiara dei passi:
        #: le righe dell'elenco sono riservate da subito (`_size_the_card`),
        #: come le altre righe facoltative, cosi' la scheda non cresce quando
        #: la prima fase finisce.
        self._phases_reserved = False
        #: Fase in corso: chiave, nome sorgente, primo stato mostrato (il suo
        #: esito) e ultimo avviso, che lo sostituisce; testi sorgente anche qui.
        self._phase_key = ""
        self._phase_name = ""
        #: Tick counter of the pulse of the running phase's «●» (before any render).
        self._pulse_tick = 0
        self._phase_last_status = ""
        self._phase_warning = ""
        #: Conteggio della fase in corso (fatti, totale, unita' tradotta), o
        #: None: lo mostra la riga «●» dell'elenco e lo stato centrale.
        self._phase_progress = None
        #: Testi (gia' tradotti) dei passi, passo corrente, motivi di chi non c'e'.
        self._step_labels: list[str] = []
        self._step_current = -1
        self._step_unavailable: dict[int, str] = {}
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(self.status)
        # La barra compare solo quando esiste un conteggio vero da mostrare.
        # Una percentuale stimata sul tempo si ferma al 90% e resta li': dice
        # meno del nulla, perche' sembra un'informazione.
        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(10)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        self.progress_label = QLabel()
        self.progress_label.setProperty("i18n_skip", True)
        self.progress_label.setStyleSheet("color: #a6adc8;")
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.progress_label)
        self.progress_row = QWidget()
        self.progress_row.setLayout(progress_row)
        self.progress_row.hide()
        content.addWidget(self.progress_row)
        self.elapsed = QLabel()
        self.elapsed.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.elapsed.setStyleSheet("color: #a6adc8;")
        content.addWidget(self.elapsed)
        # Il consuntivo dell'analisi AI resta qui finche' la modale e' aperta.
        # Sta in una riga propria, non nello stato, perche' i messaggi di
        # avanzamento della generazione lo sovrascriverebbero subito: e' il
        # motivo per cui il costo non si riusciva a leggere.
        # Una riga sola che si accorcia da sola, come l'intestazione: con l'a
        # capo la riga del costo passava da una riga a due nel momento in cui il
        # consuntivo arriva, e la scheda cambiava altezza sotto gli occhi --
        # esattamente cio' che il dimensionamento deve evitare. Il numero sta in
        # testa alla frase, quindi resta leggibile anche accorciata, e il testo
        # integrale e' nel suggerimento e nel Log AI.
        self.cost = ElidedLabel()
        self.cost.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cost.setTextFormat(Qt.TextFormat.PlainText)
        self.cost.setStyleSheet("color: #a6e3a1;")
        self.cost.setProperty('i18n_skip', True)
        self.cost.hide()
        # Il costo resta vicino allo stato dell'inventario, dove c'e' spazio,
        # invece di finire sotto il tempo trascorso.
        content.insertWidget(content.indexOf(self.elapsed), self.cost)
        # I due comandi stanno sulla stessa riga, nell'ordine in cui l'utente
        # li ha chiesti: il log a sinistra, l'interruzione subito alla sua
        # destra. La riga resta centrata come lo era il solo pulsante del log.
        # Icone con nome e spiegazione nel tooltip (utente, 2026-09-24), come
        # le barre delle altre finestre. I testi li compone
        # `refresh_command_texts`, anche al cambio di lingua: i pulsanti sono
        # `i18n_skip` perche' il tooltip composto non e' una chiave.
        from .review_tools import tool_button
        self.log_button = tool_button("ai-log.svg", "Apri log AI")
        self.log_button.setObjectName("analysisLogButton")
        self.log_button.clicked.connect(self.log_requested.emit)
        self.abort_button = tool_button("stop.svg", "Interrompi")
        self.abort_button.setObjectName("analysisAbortButton")
        self.abort_button.clicked.connect(self.abort_requested.emit)
        # Piu' grandi delle icone delle barre (utente, 2026-09-24): nella card
        # larga, con il titolo a 18pt, la misura delle barre sembrava piccola.
        # Solo qui: `tool_button` resta uguale per tutte le altre finestre.
        for button in (self.log_button, self.abort_button):
            button.setIconSize(QSize(OVERLAY_ICON_PX, OVERLAY_ICON_PX))
        self.refresh_command_texts()
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(12)
        buttons.addStretch()
        buttons.addWidget(self.log_button)
        buttons.addWidget(self.abort_button)
        # A lavoro finito la scheda resta aperta con tutti i passi spuntati e
        # si chiude solo da qui (utente, 2026-09-26: il passo FreeCAD non si
        # vedeva mai concluso, la scheda spariva appena finiva).
        self.close_button = QPushButton("Chiudi")
        self.close_button.setObjectName("analysisCloseButton")
        self.close_button.clicked.connect(self._close_completed)
        self.close_button.hide()
        buttons.addWidget(self.close_button)
        buttons.addStretch()
        self.button_row = QWidget()
        self.button_row.setLayout(buttons)
        content.addWidget(self.button_row)
        #: Vero solo mentre esiste un lavoro che ha senso interrompere. Nasce
        #: falso: una scheda ferma non deve offrire di fermare nulla.
        self._abortable = False
        self.set_abortable(False)
        #: Altezza (px) che lo stato occupa con le righe riservate d'ufficio, e
        #: supplemento concesso a un elenco piu' alto di quelle. Il supplemento
        #: e' MONOTONO dentro una corsa e si azzera solo in `start()`: un elenco
        #: di otto documenti compare una volta sola, e far rimpicciolire la
        #: scheda al messaggio successivo sarebbe lo stesso sobbalzo che il
        #: dimensionamento esiste per evitare.
        self._status_base_height = 0
        self._status_extra = 0
        #: Stato da mostrare, gia' tradotto e coi percorsi accorciati ma NON
        #: ancora impaginato. Si conserva perche' l'impaginazione dipende dalla
        #: geometria: se la finestra o il font cambiano mentre un elenco e' sulla
        #: scheda, l'elenco va riaccorciato e rimisurato sulla larghezza nuova,
        #: altrimenti le righe accorciate per la larghezza vecchia sbordano.
        self._status_source = ""
        # La scheda entra nell'overlay PRIMA di essere misurata: il bordo di 1 px
        # che il foglio di stile dell'overlay le da' restringe lo spazio del
        # testo, e una scheda non ancora figlia dell'overlay non lo conosce.
        outer.addWidget(self.card, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._size_the_card()
        self.status.installEventFilter(self)
        outer.addStretch()
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._tick)
        self.started_at = 0.0
        #: Istante in cui l'overlay e' stato nascosto a operazione in corso
        #: (revisione dei corpi): al ritorno l'orologio salta quel tempo.
        self._hidden_at = None
        parent.installEventFilter(self)
        if self.i18n is not None:
            self.i18n.retranslate_widgets(self)
        self.hide()

    def refresh_command_texts(self):
        """@brief Put name and explanation of the two commands in their tooltips.

        @note Called at construction and by the window on a language change.
            It also redraws the list of phases: the list is `i18n_skip` (it is
            composed, not a catalogue key) and keeps Italian sources, so a
            language switch in the middle of a run reaches it only through here.
        """
        from .review_tools import set_tool_texts
        tr = self.i18n.text if self.i18n is not None else None
        set_tool_texts(self.log_button, "Apri log AI", "",
                       "Apri il log completo dell'analisi AI, aggiornato in tempo reale", tr)
        set_tool_texts(self.abort_button, "Interrompi", "",
                       "Interrompi l'operazione in corso: verrà chiesta conferma.", tr)
        if getattr(self, "phases", None) is not None:
            self._render_phases()

    def _available_card_width(self):
        """@brief Widest card the hosting window can show without clipping.

        @return Maximum card width in pixels, margins already subtracted.
        @note Il tetto va dedotto, non scelto: una costante (760 px) era insieme
            troppo bassa -- annullava la derivazione dal font e tagliava il testo
            dai 14 pt in su -- e troppo alta, perche' 760 px di minimo piu' i
            margini non stanno in una finestra da 640 px e la scheda finiva
            tagliata di lato.
        """
        parent = self.parentWidget()
        room = parent.width() if parent is not None else 0
        if room <= 0:
            screen = self.screen()
            room = screen.availableGeometry().width() if screen is not None else 0
        if room <= 0:
            room = _CARD_FALLBACK_WIDTH
        return max(1, room - 2 * _OVERLAY_MARGIN)

    def _available_card_height(self):
        """@brief Tallest card the hosting window can show without clipping.

        @return Maximum card height in pixels, margins already subtracted.
        @note Gemella di `_available_card_width`, e per lo stesso motivo: un
            elenco lungo deve poter allungare la scheda, ma non oltre la
            finestra che la ospita, altrimenti la parte in fondo -- pulsanti
            compresi -- finirebbe fuori dallo schermo.
        """
        parent = self.parentWidget()
        room = parent.height() if parent is not None else 0
        if room <= 0:
            screen = self.screen()
            room = screen.availableGeometry().height() if screen is not None else 0
        if room <= 0:
            room = _CARD_FALLBACK_HEIGHT
        return max(1, room - 2 * _OVERLAY_MARGIN)

    def _status_height_room(self):
        """@brief Tallest status block that still fits in the hosting window.

        @return Height in pixels available to the status label, its currently
            reserved height included.
        @note Si misura sulla scheda VERA (`sizeHint`), non su una stima delle
            righe: la scheda porta anche titolo, anello, barra, costo e
            pulsanti, e quanto spazio resta allo stato dipende da tutti loro.
        """
        spare = self._available_card_height() - self.card.sizeHint().height()
        return self.status.minimumHeight() + max(0, spare)

    def _status_text_width(self):
        """@brief Width in pixels the status label really lays its text out in.

        @return Card width minus the card's own contents margins (they include
            the 1 px style-sheet border of the QFrame), the layout padding, and
            the label's contents margins and margin.
        @note CAUSA del difetto «ultima riga tagliata a meta'»: prima si usava
            `larghezza della scheda - 2 * _CARD_PADDING`, cioe' 2 px in piu'
            della label vera (il bordo della scheda). Le righe dell'elenco
            venivano accorciate a QUELLA larghezza, e `elidedText` riempie la
            riga fino al limite: una riga accorciata cadeva facilmente negli
            ultimi 2 px, la label vera la mandava a capo, e l'elenco occupava una
            riga in piu' di quella misurata e riservata. Dove la finestra non
            concedeva altro spazio, la label restava piu' bassa del suo testo e
            l'ultima riga si vedeva a meta'. La misura ora e' quella dello spazio
            in cui il testo viene davvero disegnato.
        """
        # Il foglio di stile applica bordo e font solo alla lucidatura: misurare
        # prima darebbe la larghezza di una scheda senza bordo.
        self.card.ensurePolished()
        card = self.card.contentsMargins()
        padding = self.card.layout().contentsMargins()
        label = self.status.contentsMargins()
        return max(1, self.card.minimumWidth()
                   - card.left() - card.right()
                   - padding.left() - padding.right()
                   - label.left() - label.right() - 2 * self.status.margin())

    def _status_height(self):
        """Altezza che il testo attuale dello stato occupa alla larghezza vera della label."""
        return self.status.heightForWidth(self._status_text_width())

    def _size_the_card(self):
        """@brief Size the card on its content, not on the texts of one run.

        La scheda era troppo bassa e strozzata: lo stato andava a capo male e
        le righe che compaiono a meta' operazione (strumento, barra, costo) la
        facevano crescere e rimpicciolire sotto gli occhi.

        Larghezza: lo stato e' troncato a `_STATUS_CHAR_LIMIT` caratteri, quindi
        la larghezza che serve e' quella che li distribuisce su `_STATUS_LINES`
        righe, misurata con le metriche del font effettivo (`averageCharWidth`),
        non con un numero di pixel scelto a occhio su una frase. Il minimo
        `_CARD_MIN_WIDTH` tiene leggibile un font molto stretto; il massimo NON
        e' una costante ma la larghezza che la finestra concede davvero.

        Altezza: quando il tetto della finestra morde, la larghezza voluta non
        c'e' e le righe dello stato non sono piu' tre. Si CONTANO alla larghezza
        sopravvissuta, misurando l'ingombro della frase peggiore possibile
        (`_WORST_CASE_STATUS`, lettere larghe) con le metriche vere: e' la
        differenza fra un testo che va a capo e un testo tagliato a meta' di una
        riga. `_STATUS_LINES` resta il minimo, cosi' la scheda non si abbassa
        quando il font e' minuscolo.

        Le tre righe facoltative mantengono il proprio spazio anche da nascoste,
        cosi' l'altezza complessiva e' quella di una scheda piena dall'inizio.

        @note L'intestazione dello strumento si accorcia da sola (ElidedLabel),
            quindi una riga le basta a qualunque font e non entra nel conteggio.
        """
        # Prima di leggere le metriche: la lucidatura puo' cambiare il font della
        # label (foglio di stile dell'applicazione), e misurare col font vecchio
        # darebbe righe e larghezze di un altro testo.
        self.card.ensurePolished()
        metrics = self.status.fontMetrics()
        # Una riga di margine: `averageCharWidth` e' una media, e con una frase
        # di lettere larghe il testo troncato sborderebbe sulla riga dopo
        # facendo crescere la scheda. Dimensionando la larghezza su due righe
        # (_STATUS_WRAP_LINES), le tre riservate bastano anche nel caso peggiore.
        chars_per_line = math.ceil(_STATUS_CHAR_LIMIT / _STATUS_WRAP_LINES)
        wanted = 2 * _CARD_PADDING + metrics.averageCharWidth() * chars_per_line
        width = min(self._available_card_width(), max(_CARD_MIN_WIDTH, wanted))
        self.card.setMinimumWidth(width)
        self.card.setMaximumWidth(width)
        # Le righe si contano alla larghezza in cui la label disegna davvero,
        # bordo della scheda escluso (vedi `_status_text_width`).
        usable = self._status_text_width()
        wrapped = metrics.boundingRect(QRect(0, 0, usable, 0),
                                       int(Qt.TextFlag.TextWordWrap),
                                       _WORST_CASE_STATUS)
        lines = max(_STATUS_LINES,
                    math.ceil(wrapped.height() / max(1, metrics.lineSpacing())))
        self._status_base_height = lines * metrics.lineSpacing() + metrics.descent()
        # Il supplemento dell'elenco si somma alle righe riservate: le righe
        # d'ufficio restano il pavimento (una scheda che si abbassa quando
        # l'elenco finisce e' lo stesso sobbalzo di una che si alza).
        self.status.setMinimumHeight(self._status_base_height + self._status_extra)
        self.tool.setMinimumHeight(self.tool.fontMetrics().lineSpacing())
        # Una riga per passo (uno sotto l'altro, utente 2026-09-26), ma solo
        # quando ci sono: le operazioni di una fase sola non pagano quello
        # spazio (una finestra bassa non lo ha).
        self.steps.setMinimumHeight(
            (len(self._step_labels) + len(getattr(self, '_step_results', {}))) * self.steps.fontMetrics().lineSpacing()
            + self.steps.fontMetrics().lineSpacing() // 2 if self._step_labels else 0)
        self.cost.setMinimumHeight(self.cost.fontMetrics().lineSpacing())
        self.phases.setMinimumHeight(0)
        # Anche il tempo trascorso: nasce vuoto e riceve il primo testo in
        # `start`, DOPO questo calcolo. Una label vuota e una con testo non
        # chiedono la stessa altezza (14 e 12 px su una piattaforma di prova),
        # quindi la prima operazione aveva una scheda di 2 px diversa dalle
        # successive. Una riga intera riservata la rende indipendente dal testo.
        self.elapsed.setMinimumHeight(self.elapsed.fontMetrics().lineSpacing())
        # Spazio riservato anche quando la riga e' nascosta: evita che la scheda
        # salti quando lo strumento, la barra o il costo compaiono a operazione
        # avviata. La visibilita' logica non cambia (isVisibleTo resta falso).
        for row in (self.tool, self.progress_row, self.cost, self.phases):
            policy = row.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            row.setSizePolicy(policy)
        # Le righe delle fasi concluse prendono solo lo spazio che la finestra
        # avanza DOPO tutto il resto, fino a PHASE_HISTORY_ROWS: una finestra
        # bassa ne mostra meno (o nessuna) invece di far uscire la scheda.
        # Il resto si misura SENZA l'elenco: a corsa avviata la label contiene
        # gia' le sue righe e `card.sizeHint` le conta, quindi sottrarle di
        # nuovo le toglieva due volte e ogni ridimensionamento a meta' corsa
        # riduceva l'elenco (HANDOFF 138, C2: 4 righe -> 1). La sua altezza
        # minima e' appena tornata a zero, quindi il suo contributo e' il suo
        # `sizeHint`, che si toglie.
        spare = (self._available_card_height() - self.card.sizeHint().height()
                 + self.phases.sizeHint().height())
        rows = 0
        if getattr(self, "_phases_reserved", False):
            # Altezza misurata sul testo vero (righe della tabella comprese),
            # non stimata: una stima corta faceva crescere la scheda di qualche
            # pixel a ogni fase.
            while rows < PHASE_HISTORY_ROWS and self._phase_rows_height(rows + 1) <= spare:
                rows += 1
        self._phase_rows = rows
        self.phases.setMinimumHeight(self._phase_rows_height(rows) if rows else 0)
        # Le righe riservate possono essere cambiate (finestra ridimensionata,
        # elenco dello stato piu' alto): l'elenco si ridisegna PRIMA di fissare
        # l'altezza della scheda, cosi' mostra quante righe ci stanno adesso e
        # un testo rimasto dalla misura vecchia non entra nel `sizeHint`.
        self._render_phases()
        self.card.setMinimumHeight(self.card.sizeHint().height())

    def changeEvent(self, event):
        """@brief Re-derive the card size when the font or the style changes.

        @note Il calcolo stava solo in `__init__`: un cambio di tema o del font
            di accessibilita' a programma avviato lasciava la scheda dimensionata
            sulle metriche vecchie, cioe' col testo tagliato -- proprio il
            difetto che il calcolo doveva evitare.
        """
        super().changeEvent(event)
        if getattr(self, "card", None) is None:
            return
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange,
                            QEvent.Type.ApplicationFontChange):
            self._refit()

    def _refit(self):
        """@brief Re-derive the card AND re-lay the current status on new geometry.

        @note Un elenco sulla scheda e' accorciato e misurato per una larghezza e
            un font precisi. Ridimensionare solo la scheda lasciava le righe
            accorciate per la geometria vecchia: su una scheda piu' stretta
            andavano a capo (o sbordavano) e l'ultima riga finiva fuori
            dall'altezza riservata.
        @note Il supplemento d'altezza, monotono fra un messaggio e l'altro,
            qui si RIDERIVA da zero: e' stato misurato su una geometria che non
            esiste piu', e tenerlo farebbe restare la scheda piu' alta della
            finestra appena rimpicciolita. Il sobbalzo che la monotonia evita e'
            quello fra due messaggi; un ridimensionamento cambia gia' la scheda.
        @note Anche le righe delle fasi si riderivano qui (`_size_the_card` le
            ridisegna): una finestra ingrandita a meta' corsa mostra righe che
            all'avvio non ci stavano, una rimpicciolita ne toglie.
        """
        self._status_extra = 0
        self._size_the_card()
        if getattr(self, "_status_source", ""):
            self._render_status()

    def eventFilter(self, watched, event):
        """@brief Keep the overlay covering its parent and sized on its metrics.

        @note Il tetto della larghezza dipende dalla finestra, quindi si
            ricalcola al resize: rimpicciolendo la finestra una scheda
            dimensionata per la vecchia larghezza resterebbe tagliata di lato.
        @note Il cambio di font si intercetta SULLA label dello stato, non
            sull'overlay: il foglio di stile di questa scheda fissa il font dei
            propri figli, quindi un nuovo font di sistema arriva alla label
            (quando il tema viene riapplicato) senza passare da qui. Sizing e
            metriche misurate devono restare la stessa cosa.
        """
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.rect())
            self._refit()
        elif watched is self.status and event.type() == QEvent.Type.FontChange:
            self._refit()
        return super().eventFilter(watched, event)

    def set_abortable(self, abortable):
        """@brief Offer the abort action only while something can actually be stopped.

        @param abortable True while an operation that the window can abort runs.
        @note Spento E nascosto insieme, secondo la stessa disciplina degli
            altri comandi di questo programma: un pulsante acceso su nessun
            lavoro e' una promessa che non si puo' mantenere, e uno visibile ma
            spento occupa il posto senza dire perche'.
        @note Chi avvia il lavoro deve dichiararlo (`start(..., abortable=True)`):
            il valore predefinito e' falso, cosi' un percorso nuovo che non ha
            pensato all'interruzione non offre un pulsante che non farebbe nulla.
        """
        self._abortable = bool(abortable)
        self.abort_button.setEnabled(self._abortable)
        self.abort_button.setVisible(self._abortable)

    def is_abortable(self):
        """@brief Tell whether the card is currently offering the abort action."""
        return self._abortable

    def start(self, title='Analisi AI in corso', status='Preparazione dei documenti…',
              abortable=False):
        """@brief Show the overlay for a new operation, with no tool yet claimed.

        @param title Heading of the card.
        @param status First status line.
        @param abortable True when the caller knows how to stop this operation.
        """
        self.set_abortable(abortable)
        self.title.setText(self.i18n.text(title) if self.i18n is not None else title)
        self.started_at = monotonic()
        self._hidden_at = None
        self.spinner.angle = 0
        self.clear_phases()
        self._phases_reserved = True
        self.set_tool(None)
        self.clear_steps()
        self.close_button.hide()
        # Nuova operazione, nuovo ingombro: il supplemento concesso all'elenco
        # della corsa precedente si azzera QUI e in nessun altro punto. Si
        # svuota prima la label perche' il suo testo vecchio, se era un elenco,
        # terrebbe alto il `sizeHint` su cui la scheda si ridimensiona.
        self._status_extra = 0
        self._status_source = ""
        self.status.clear()
        self._size_the_card()
        self.set_status(status)
        self.setGeometry(self.parentWidget().rect())
        self._tick()
        self.show()
        self.raise_()
        self.log_button.setFocus()
        self.timer.start()
        self.operation_started.emit(self.title.text())

    def set_tool(self, tool, detail=""):
        """@brief Name the tool now at work, with its own mark and color.

        @param tool Key in TOOLS or TOOL_ALIASES, or None/unknown to show the
            bare ring. Keys in TRANSFER_SOURCES also show the local data being
            sent, at the left of the ring.
        @param detail Free text appended in parentheses, e.g. the model name.
        @note An unknown key is treated as no tool rather than raising: a new
            phase must never be able to take down a running analysis.
        @note The label keeps the full text and shortens it only while painting:
            main_window reads `tool.text()` to know which detail the header
            already carries.
        """
        key = str(tool or "")
        source_name, color, glyph = TOOLS.get(TOOL_ALIASES.get(key, key), ("", "#89b4fa", ""))
        # La label e' `i18n_skip` (porta un dettaglio che non e' testo nostro):
        # il nome dello strumento si traduce qui, prima di comporla. L'elenco
        # delle fasi conserva invece la sorgente (vedi `_phase_history`).
        name = self._translate(source_name) if source_name else ""
        if key != self._phase_key:
            # Un altro strumento: la fase di prima e' conclusa (vedi _end_phase).
            self._end_phase()
            self._phase_key, self._phase_name = (key, source_name) if source_name else ("", "")
            self._phase_last_status = self._phase_warning = ""
            self._phase_progress = None
            self._render_phases()
        self.spinner.set_tool(glyph, color)
        source = TRANSFER_SOURCES.get(key)
        if source is not None:
            self.transfer.set_source(source, color)
        self.transfer.setVisible(source is not None)
        detail = " ".join(str(detail or "").split())
        self.tool.setText(f"{name} · {detail}" if name and detail else name)
        self.tool.setToolTip(self.tool.text())
        self.tool.ink = color
        self.tool.setStyleSheet(f"font-weight: 600; color: {color};")
        self.tool.setVisible(bool(name))
        self.progress.setStyleSheet(
            f"QProgressBar::chunk {{ background: {color}; border-radius: 5px; }}")

    def set_steps(self, labels, unavailable=None):
        """@brief Show the steps of a multi-phase job, none of them started yet.

        @param labels Italian source texts of the steps, in order.
        @param unavailable Optional {index: reason}: steps that will not run
            (e.g. FreeCAD missing), drawn struck through with the reason.
        """
        translate = self.i18n.text if self.i18n is not None else (lambda text: text)
        # Un lavoro a passi spunta i suoi passi: l'elenco delle fasi non serve
        # e il suo spazio torna allo stato (il ridimensionamento e' qui sotto).
        self._phases_reserved = False
        self.clear_phases()
        self._step_labels = [translate(str(label)) for label in labels]
        self._step_results = {}
        self._step_unavailable = {int(index): translate(str(reason))
                                  for index, reason in (unavailable or {}).items()}
        self._step_current = -1
        self._render_steps()
        # Chiamata subito dopo `start`, prima del primo disegno: la scheda
        # prende adesso l'altezza che terra' per tutta la generazione.
        self._size_the_card()

    def set_step_result(self, index, result):
        """@brief Keep a phase result visible while subsequent steps run."""
        if not self._step_labels or not 0 <= int(index) < len(self._step_labels):
            return
        self._step_results[int(index)] = str(result)
        self._render_steps()
        self._size_the_card()

    def set_current_step(self, index):
        """@brief Mark step `index` as running and every earlier step as done.

        @param index Position in the list given to `set_steps`; an index out of
            range, or no steps shown, is ignored rather than raising.
        """
        if not self._step_labels or not 0 <= int(index) < len(self._step_labels):
            return
        self._step_current = int(index)
        self._render_steps()

    def show_completed(self, message):
        """@brief Keep the card open on a finished job: every step ticked, «Chiudi» to leave.

        @param message Final outcome, already translated, shown as the status.
        @note The clock and the animation stop (nothing is running any more)
            and the abort command goes away; the card closes only through
            «Chiudi», which emits `closed`.
        """
        self.timer.stop()
        self._end_phase()
        if self._step_labels:
            self._step_current = len(self._step_labels)
            self._render_steps()
        self.set_progress(None, None)
        self.set_abortable(False)
        self.set_status(message)
        self.close_button.show()
        self.close_button.setFocus()
        self.operation_finished.emit()

    def is_holding_completed(self):
        """@brief Tell whether the card is open on a finished job, waiting for «Chiudi»."""
        return not self.isHidden() and not self.close_button.isHidden()

    def _close_completed(self):
        self.close_button.hide()
        self.stop()
        self.closed.emit()

    def clear_phases(self):
        """@brief Forget the completed phases (a new operation starts)."""
        self._phase_history = []
        self._phase_key = self._phase_name = self._phase_last_status = self._phase_warning = ""
        self._phase_progress = None
        self.phases.clear()
        self.phases.setToolTip("")
        self.phases.hide()

    def phases_plain_text(self):
        """@brief The phase list exactly as the reader sees it, as plain text.

        @return One line per shown row, marks («✓», «⚠», «●») included, in the
            language active when the list was last drawn.
        @note Used by the tests and kept for diagnostics: `phases.text()` is
            the table markup, not the words on screen. The full history, rows
            beyond the reserved room included, is the label's tooltip.
        """
        document = QTextDocument()
        document.setHtml(self.phases.text())
        return document.toPlainText()

    def _end_phase(self):
        """Move the running phase, if any, into the completed list with its outcome.

        The outcome is the FIRST status the phase showed -- what the tool says
        about its own work right after it is announced; the last one is often
        the preparation of the next phase -- or, when the phase issued a
        warning (messages.WARNING), that warning. No phase-specific wording,
        so a new phase is listed without code here. A job with steps
        (`set_steps`) already ticks its steps: nothing listed.
        """
        if not self._phase_name or self._step_labels:
            self._phase_key = self._phase_name = self._phase_last_status = ""
            self._phase_warning = ""
            return
        warned = bool(self._phase_warning)
        self._phase_history.append((self._phase_name,
                                    self._phase_warning or self._phase_last_status, warned))
        self._phase_key = self._phase_name = self._phase_last_status = self._phase_warning = ""
        self._render_phases()

    #: Colours of the phase rows: done, done with a warning, running.
    _PHASE_COLORS = {"done": "#a6e3a1", "warning": "#f9e2af", "running": "#f5e0dc"}
    #: The «●» of the running phase pulses between these two (user, 2026-09-30):
    #: a still dot read as one more finished item. Dim = the colour of the
    #: steps not yet reached, bright = the running row's own colour.
    _PULSE_DIM = (0x6c, 0x70, 0x86)
    _PULSE_BRIGHT = (0xf5, 0xe0, 0xdc)
    #: One full pulse every this many timer ticks (40 ms each): ~1.3 s, slow
    #: enough not to distract, fast enough to read as «alive».
    _PULSE_TICKS = 32

    @classmethod
    def _phase_rows_html(cls, lines, pulse_color=None):
        """Rich text of the phase list: one table row per (text, state) line.

        @param pulse_color Colour of the «●» of a running row, or None to
            leave the mark in the row's colour.
        """
        rows = []
        for text, state in lines:
            body = html.escape(text)
            if state == "running" and pulse_color and text.startswith("● "):
                body = f"<span style='color:{pulse_color}'>●</span>" + html.escape(text[1:])
            rows.append(f"<tr><td style='padding:1px 0; color:{cls._PHASE_COLORS[state]}'>"
                        f"{body}</td></tr>")
        return f"<table align='center' cellspacing='0'>{''.join(rows)}</table>" if rows else ""

    def _pulse_color(self):
        """Colour of the running mark at this instant of the pulse (cosine between dim and bright)."""
        import math
        phase = (self._pulse_tick % self._PULSE_TICKS) / self._PULSE_TICKS
        mix = (1 - math.cos(2 * math.pi * phase)) / 2
        return "#%02x%02x%02x" % tuple(
            round(dim + (bright - dim) * mix)
            for dim, bright in zip(self._PULSE_DIM, self._PULSE_BRIGHT))

    def _phase_rows_height(self, count):
        """Height in px that `count` rows of the list really take in the label."""
        probe = QLabel()
        probe.setTextFormat(Qt.TextFormat.RichText)
        probe.setFont(self.phases.font())
        probe.setText(self._phase_rows_html([("✓ M", "done")] * count))
        return probe.sizeHint().height()

    def _translate(self, source):
        """Catalogue text of an Italian source, or the source itself without i18n."""
        return self.i18n.text(source) if self.i18n is not None else source

    def _phase_outcome_text(self, source):
        """Outcome of a phase as shown: translated now, paths shortened, one line.

        A source that is not a catalogue key (a sentence its sender already
        formatted through `messages.tx`, in the language of that moment) comes
        back unchanged: `I18n.text` falls back to the source. Only catalogued
        outcomes follow a later language switch.
        """
        return " ".join(elide_paths(self._translate(source)).split())

    def _render_phases(self):
        """Draw the phase list in the current language, within the reserved rows."""
        rows_room = getattr(self, "_phase_rows", PHASE_HISTORY_ROWS)
        metrics = self.phases.fontMetrics()
        width = self._status_text_width()
        running = self._phase_name and not self._step_labels
        # The running phase is the last row, marked «●» (user, 2026-09-28:
        # with ticks only, every phase looked finished); the completed ones
        # fill the rows above it, the most recent first to stay.
        room = max(0, rows_room - (1 if running else 0))
        shown = self._phase_history[-room:] if room else []
        def entry(name, outcome, warned):
            outcome = self._phase_outcome_text(outcome) if outcome else ""
            name = self._translate(name)
            # Phase labels already identify the tool. Keep the complete source
            # in the log, but avoid e.g. "YOLO — YOLO locale: ..." in the card.
            if name.startswith("YOLO"):
                outcome = re.sub(r"^(?:(?:Local|Lokales)\s+)?YOLO(?:\s+(?:locale|local))?\s*:\s*", "", outcome, flags=re.I)
            elif name.startswith("OpenCV"):
                outcome = re.sub(r"^(?:Bordi con OpenCV|Edges with OpenCV|Bords avec OpenCV|OpenCV-Kanten)\s*:\s*", "", outcome)
            return f"{'⚠' if warned else '✓'} " + (f"{name} — {outcome}" if outcome else name)

        lines = []
        for name, outcome, warned in shown:
            lines.append((metrics.elidedText(entry(name, outcome, warned),
                                             Qt.TextElideMode.ElideRight, width),
                          "warning" if warned else "done"))
        if running and rows_room:
            # Con un conteggio vero la riga dice a che punto e' (HANDOFF 156):
            # «17 di 42 componenti» invece di un «in corso» che dura minuti.
            text = "● " + self._translate(self._phase_name) + " — " + (
                self._progress_text() if self._phase_progress else self._translate("in corso"))
            lines.append((metrics.elidedText(text, Qt.TextElideMode.ElideRight, width),
                          "running"))
        self.phases.setText(self._phase_rows_html(lines, self._pulse_color()))
        self.phases.setToolTip("\n".join(entry(*phase) for phase in self._phase_history))
        self.phases.setVisible(bool(lines))

    def clear_steps(self):
        """@brief Hide the step row (single-phase operations have none)."""
        self._step_labels, self._step_unavailable, self._step_current = [], {}, -1
        self.steps.clear()
        self.steps.hide()

    def steps_plain_text(self):
        """@brief The step row as plain text, marks included (for logs and tests)."""
        document = QTextDocument()
        document.setHtml(self.steps.text())
        return document.toPlainText()

    def _render_steps(self):
        parts = []
        for index, label in enumerate(self._step_labels):
            text = html.escape(f"{index + 1} · {label}")
            result = getattr(self, '_step_results', {}).get(index)
            if result:
                text += '<br/>' + html.escape(result)
            if index in self._step_unavailable:
                reason = html.escape(self._step_unavailable[index])
                parts.append(f'<span style="color:#6c7086"><s>{text}</s> ({reason})</span>')
            elif index < self._step_current:
                parts.append(f'<span style="color:#a6e3a1">&#10003; {text}</span>')
            elif index == self._step_current:
                # The running step's mark pulses like the running phase's «●»
                # (user, 2026-09-30): only the colour of the mark moves.
                parts.append(f'<span style="color:#f5e0dc; font-weight:600">'
                             f'<span style="color:{self._pulse_color()}">&#9679;</span> {text}</span>')
            else:
                parts.append(f'<span style="color:#a6adc8">&#9675; {text}</span>')
        # Uno sotto l'altro (utente, 2026-09-26): in fila andavano a capo a
        # meta' di un passo. Una colonna centrata come blocco, righe allineate
        # a sinistra, cosi' i segni ✓ ● ○ stanno in colonna.
        rows = "".join(f"<tr><td style='padding:1px 0'>{part}</td></tr>" for part in parts)
        self.steps.setText(f"<table align='center' cellspacing='0'>{rows}</table>"
                           if parts else "")
        self.steps.setVisible(bool(parts))

    def set_progress(self, done=None, total=None, unit=""):
        """@brief Show a percentage only when a real count backs it.

        @param done Items completed; None hides the bar.
        @param total Items expected; a non-positive total hides the bar too.
        @param unit Word for the items, e.g. "componenti".
        @note Deliberately no time-based estimate: a bar that stops at 90%
            because a call is slower than usual reports a guess as a fact.
        @note While a listed phase runs (no steps), the count also goes to its
            «●» row and to the central status (HANDOFF 156), WITHOUT becoming
            the phase's outcome: the outcome stays the first status the phase
            itself reported (see `set_status`).
        """
        countable = (type(done) is int and type(total) is int
                     and total > 0 and 0 <= done <= total)
        self.progress_row.setVisible(countable)
        if not countable:
            if self._phase_progress is not None:
                self._phase_progress = None
                self._render_phases()
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        percent = round(done * 100 / total)
        if unit and self.i18n is not None:
            unit = self.i18n.text(unit)
        self.progress_label.setText(
            f"{percent}% · {done}/{total} {unit}".rstrip() if unit
            else f"{percent}% · {done}/{total}")
        if self._phase_name and not self._step_labels:
            self._phase_progress = (done, total, unit)
            self._render_phases()
            self._status_source = elide_paths(
                self._translate(self._phase_name) + " — " + self._progress_text())
            self._render_status()

    def _progress_text(self):
        """«17 di 42 componenti» for the running phase, in the current language."""
        done, total, unit = self._phase_progress
        template = self._translate("{done} di {total} {unit}")
        try:
            return template.format(done=done, total=total, unit=unit).rstrip()
        except (KeyError, IndexError, ValueError):
            return f"{done} di {total} {unit}".rstrip()

    def clear_cost(self):
        """@brief Clear and hide the AI cost summary at the start of a new AI analysis.

        Azzera il consuntivo all'inizio di una NUOVA analisi AI.

        Non in start(): la modale riparte anche per la generazione FreeCAD,
        che non fa chiamate al modello. Azzerare li' faceva sparire il costo
        un attimo dopo averlo mostrato.
        """
        self.cost.clear()
        self.cost.hide()

    def set_cost(self, text):
        """@brief Show the AI analysis cost summary inside the overlay; an empty text hides it.

        Mostra il consuntivo dell'analisi AI dentro la modale.

        Il costo dell'ultima analisi non ha piu' una label sotto il pulsante
        (utente, 2026-09-24): resta qui, nella modale che si sta guardando, e
        nel Log AI.
        """
        self.cost.setText(str(text or ""))
        self.cost.setToolTip(self.cost.text())
        self.cost.setVisible(bool(text))

    def set_status(self, message):
        """@brief Show one status written for a person, never a service line.

        @param message Status line; machine bookkeeping is dropped, not shown.
            One line in, one line shown -- unless the line enumerates, and then
            it is laid out as a list (see the last note).
        @note Le righe che il programma scrive per se' (AI_CALL_... in testa
            alla riga, record "chiave=valore" con le impronte sha256) e quelle
            che gli strumenti esterni scrivono sul proprio stdout (campi
            "chiave : valore", conteggi di entita', decorazioni, intestazioni di
            versione e copyright, righe auto-datate, percentuali senza soggetto)
            restano nel log e non arrivano qui: sulla scheda occupavano il posto
            di cio' che stava accadendo senza dirlo. Il pulsante del log resta a
            un clic di distanza.
        @note Un percorso NON fa scartare la riga: viene accorciato al nome del
            file (vedi elide_paths). Il prodotto di questo strumento sono file,
            quindi «Inventario salvato: …» e «Inventario non salvato su disco:
            [Errno 28] …» sono esiti, non contabilita' interna.
        @note Quando una riga viene scartata l'ultimo stato leggibile *resta*
            scritto: azzerare la label farebbe sembrare l'operazione bloccata,
            che e' il difetto che questo filtro deve evitare, non causare.
        @note Una riga che ELENCA (lead-in coi due punti e voci separate dal
            punto mediano: i documenti inviati al modello, i motivi di un
            inventario parziale) viene rimpaginata a una voce per riga, e la
            scheda si allunga per contenerle. La riga che va nel log resta una
            sola, con una marca temporale sola: la divisione fra il log e la
            scheda avviene qui, non alla sorgente (vedi `enumeration_lines`).
        @note In un elenco, la voce che dichiara un termine («nome — ruolo»,
            vedi `enumeration_term`) mostra il termine in grassetto, staccato
            dalla descrizione. E' l'unico caso in cui la label passa al testo
            ricco, e tutto il testo che arriva dal messaggio viene escapato: un
            nome di file non puo' diventare markup. Ogni altra riga resta
            `PlainText`, letterale com'era.
        """
        # Full messages stay in the log; keep the card stable for long replies.
        warned = kind_of(message) == WARNING
        source = " ".join(str(message).split())
        if is_machine_line(source):
            return
        text = self.i18n.text(source) if self.i18n is not None else source
        self._status_source = elide_paths(text)
        if self._phase_name:
            # Esito della fase: il PRIMO stato dopo il suo annuncio; un avviso
            # (l'ultimo) lo sostituisce, perche' e' cio' che va guardato. Si
            # conserva la SORGENTE: la traduzione si fa quando l'elenco si
            # disegna (`_phase_outcome_text`), anche dopo un cambio di lingua.
            transient_crop_progress = re.match(
                r"^(?:Analisi componente|Componente|Analysing component|Component|"
                r"Analyse du composant|Composant|Bauteil) \d+ (?:di|of|sur|von) \d+", source)
            if not self._phase_last_status and not transient_crop_progress:
                self._phase_last_status = source
            if warned:
                self._phase_warning = source
        self._render_status()

    def status_plain_text(self):
        """@brief Return the status exactly as the reader sees it, without markup.

        @return The shown text, one "\\n" per shown line. For a plain status it
            is `status.text()`; for a list it is the text of the rich document
            the label draws, so bold terms, escapes and separators come back as
            the characters on screen.
        @note `status.text()` returns the MARKUP when a list is shown: reading it
            to know what the user sees would compare tags, not words.
        """
        if self.status.textFormat() != Qt.TextFormat.RichText:
            return self.status.text()
        document = QTextDocument()
        document.setHtml(self.status.text())
        return document.toPlainText()

    def _render_status(self):
        """Impagina lo stato conservato: elenco se elenca e ci sta, riga sola altrimenti."""
        text = self._status_source
        lines = enumeration_lines(text)
        if lines is not None and self._show_enumeration(lines):
            return
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setText(text if len(text) <= _STATUS_CHAR_LIMIT
                            else text[:_STATUS_CHAR_LIMIT - 3] + "…")

    def _show_enumeration(self, lines):
        """@brief Lay an enumeration out one item per line, sized to fit.

        @param lines Lead-in and items, as `enumeration_lines` returns them.
        @return True when the list is on the card; False when there is not even
            room for two lines, and the caller must fall back to the one-line
            form -- a list that does not fit is not an improvement.
        @note Ogni riga viene accorciata alla larghezza in cui la label disegna
            DAVVERO (`_status_text_width`, bordo della scheda escluso) invece di
            essere mandata a capo, e il markup vieta l'a capo: cosi' una voce
            occupa una riga e una sola, il conteggio delle righe e' esatto e il
            nome del file resta allineato sotto il precedente, che e' cio' che
            l'utente ha chiesto. Il testo integrale e' nel log.
        @note L'altezza si misura sul documento che la label disegna
            (`heightForWidth` del testo ricco), non su `lineSpacing` moltiplicato
            per le righe: interlinea, grassetto e font di ripiego sono gia'
            dentro la misura, perche' misura e disegno sono lo stesso layout.
        @note Se le voci non ci stanno in altezza si TAGLIA l'elenco, non lo si
            lascia debordare: si tolgono le ultime voci e il taglio si dichiara
            con un'ellissi su una riga propria -- lo stesso segno con cui
            questa scheda dichiara ogni altro accorciamento. Meglio un elenco
            dichiaratamente parziale che una scheda piu' alta della finestra,
            con i pulsanti fuori dallo schermo.
        @note L'elenco e' allineato a sinistra: centrato, i nomi dei file
            partirebbero da colonne diverse a ogni riga, cioe' esattamente il
            difetto («più chiaro») che l'elenco deve togliere. Le righe non
            elencate restano centrate come prima.
        """
        metrics = self.status.fontMetrics()
        width = self._status_text_width()
        shown = [_fit_row(line, width, self.status.font(), index > 0)
                 for index, line in enumerate(lines)]
        room = self._status_height_room()
        if room < 2 * metrics.lineSpacing() + metrics.descent():
            return False
        ellipsis = (None, "…")
        self.status.setTextFormat(Qt.TextFormat.RichText)
        self.status.setAlignment(Qt.AlignmentFlag.AlignLeft
                                 | Qt.AlignmentFlag.AlignVCenter)
        self.status.setText(_enumeration_markup(shown))
        while len(shown) > 2 and self._status_height() > room:
            # La prima potatura sostituisce l'ultima voce con l'ellissi (stesso
            # numero di righe, una voce in meno); dalla seconda in poi si toglie
            # anche l'ellissi, quindi ogni giro perde una riga e il ciclo
            # termina sempre, al piu' sulle due righe del limite.
            del shown[-2 if shown[-1] == ellipsis else -1:]
            shown.append(ellipsis)
            self.status.setText(_enumeration_markup(shown))
        self._reserve_status_height(self._status_height())
        return True

    def _reserve_status_height(self, needed):
        """@brief Grow the reserved status height so a list is never clipped.

        @param needed Height in pixels the status text occupies right now.
        @note Solo in aumento e solo dentro una corsa: `start()` riporta il
            supplemento a zero. Una scheda che si rialza e si riabbassa a ogni
            messaggio e' piu' fastidiosa di una scheda un po' piu' alta, che e'
            la stessa ragione per cui l'altezza si riserva d'ufficio.
        """
        extra = max(0, needed - self._status_base_height)
        if extra > self._status_extra:
            self._status_extra = extra
            self._size_the_card()

    def _tick(self):
        seconds = max(0, int(monotonic() - self.started_at))
        elapsed = (
            self.i18n.tr("analysis.elapsed", time=f"{seconds // 60:02d}:{seconds % 60:02d}")
            if self.i18n is not None else f"Tempo trascorso  {seconds // 60:02d}:{seconds % 60:02d}"
        )
        self.elapsed.setText(elapsed)
        self.spinner.angle = (self.spinner.angle + 9) % 360
        self.spinner.update()
        if self.transfer.isVisible():
            self.transfer.advance()
        # The running phase's «●» pulses with the same clock; only the mark's
        # colour changes, the rows and their text stay as they are.
        self._pulse_tick += 1
        if self._step_labels and 0 <= self._step_current < len(self._step_labels):
            self._render_steps()
        elif self._phase_name and self.phases.isVisible():
            self._render_phases()

    def stop(self):
        """@brief Hide the overlay and stop the animation.

        @note Toglie anche l'interruzione: il lavoro e' finito (da solo o
            perche' e' stato fermato), quindi da qui in poi non c'e' piu'
            niente da fermare. `hide()` da solo non basta -- la scheda viene
            nascosta anche a operazione in corso, durante la revisione dei
            corpi, e li' il comando deve restare valido.
        """
        self.timer.stop()
        self.set_progress(None, None)
        self.set_abortable(False)
        self.hide()
        self.operation_finished.emit()

    def hideEvent(self, event):
        """@brief Stop the animation whenever the overlay goes away.

        @note Records when it went away: the overlay is hidden mid-operation
            while the reviewer edits the detected bodies, and the clock must
            not count that time as waiting for the model.
        """
        self.timer.stop()
        self._hidden_at = monotonic()
        super().hideEvent(event)

    def showEvent(self, event):
        """@brief Resume the animation when the overlay comes back mid-operation.

        @note `hideEvent` stops the timer; a plain `show()` after the review
            used to leave the spinner and the clock frozen at the value they
            had when hidden (seen by the user as a crash). The time spent
            hidden is subtracted, so the clock measures the operation, not
            the reviewer.
        """
        super().showEvent(event)
        if self._hidden_at is not None:
            self.started_at += max(0.0, monotonic() - self._hidden_at)
            self._hidden_at = None
        if not self.timer.isActive():
            self._tick()
            self.timer.start()
