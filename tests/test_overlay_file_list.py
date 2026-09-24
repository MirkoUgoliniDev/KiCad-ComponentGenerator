"""@brief The document list on the analysis card: nothing clipped, names in bold.

Due note dell'utente sulla modale «Analisi AI in corso»:

1. «l'ultima riga dell'elenco si vede a meta'»: la label dello stato era piu'
   bassa del testo che disegnava. Causa: righe accorciate e misurate a una
   larghezza 2 px piu' larga della label vera (il bordo di 1 px della scheda),
   quindi una riga accorciata -- che `elidedText` riempie fino al limite --
   andava a capo nella label e l'elenco chiedeva una riga in piu' di quella
   riservata. Si prova sulla misura vera: l'altezza che il testo CHIEDE alla
   larghezza vera della label non supera l'altezza che la label HA.
2. «i nomi dei file non sono evidenziati, troppo attaccati al testo»: il nome
   va in grassetto e staccato dal ruolo. Si prova sul documento che la label
   disegna (pesi dei frammenti), non sul markup, e con i negativi: una riga che
   non dichiara un termine non riceve grassetto, e nessun testo del messaggio
   diventa markup.
"""
import itertools
import os
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytest.importorskip("PyQt6")
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QTextDocument
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from kicadgen.gui.analysis_overlay import (_CARD_PADDING, _OVERLAY_MARGIN,
                                          AnalysisOverlay, enumeration_lines,
                                          enumeration_term)
from kicadgen.services.datasheet_extractor import _files_sent_message


@pytest.fixture
def make_overlay():
    """@brief Build a shown overlay on a parent of the given size and font size.

    @return Callable(width, height, point_size=None) -> (overlay, parent, app).
    """
    app = QApplication.instance() or QApplication([])
    built = []

    def build(width=900, height=650, point_size=None):
        parent = QWidget()
        if point_size is not None:
            font = QFont(app.font())
            font.setPointSize(point_size)
            parent.setFont(font)
        parent.resize(width, height)
        overlay = AnalysisOverlay(parent)
        parent.show()
        overlay.start()
        app.processEvents()
        built.append((overlay, parent))
        return overlay, parent, app

    yield build
    for overlay, parent in built:
        overlay.stop()
        parent.close()


#: Nomi realistici: con spazi (Windows), senza estensione, UPPER_SNAKE, uno
#: lunghissimo che la sorgente accorcia nel mezzo e la scheda deve ancora
#: accorciare alla propria larghezza insieme al ruolo.
NAMES = ('Foto scheda dall alto.jpg', 'MOD_REV_B', 'pinout.png',
         'scheda-radio-vista-dall-alto-alta-risoluzione-rev-C-2026.png',
         'top.png', 'vista laterale con quote rev B.png', 'hires.jpg', 'x.pdf')


def _documents(count):
    """La riga VERA della sorgente per `count` documenti, ruoli a rotazione."""
    names = NAMES[:count]
    roles = {name: ('pinout', 'top', 'hires', None)[index % 4]
             for index, name in enumerate(names)}
    return _files_sent_message([Path('progetto') / name for name in names], roles)


def _lines(overlay):
    return overlay.status_plain_text().split("\n")


def _required_height(label, width):
    """Altezza che il testo chiede alla larghezza data, SENZA il minimo della
    label (`heightForWidth` della label lo include, quindi non basta a dire se
    il testo ci sta): la si chiede a una label gemella."""
    twin = QLabel()
    twin.setFont(label.font())
    twin.setTextFormat(label.textFormat())
    twin.setWordWrap(label.wordWrap())
    twin.setText(label.text())
    return twin.heightForWidth(width)


def _document(label):
    document = QTextDocument()
    document.setDefaultFont(label.font())
    document.setDocumentMargin(0)
    document.setHtml(label.text())
    return document


def _bold_runs(label):
    """Testi disegnati in grassetto, nell'ordine, letti dal documento della label."""
    if label.textFormat() != Qt.TextFormat.RichText:
        return []
    # Il documento va tenuto in vita: i blocchi sono viste su di esso.
    document = _document(label)
    runs, block = [], document.begin()
    while block.isValid():
        fragment = block.begin()
        while not fragment.atEnd():
            piece = fragment.fragment()
            if piece.charFormat().fontWeight() >= QFont.Weight.Bold.value:
                runs.append(piece.text())
            fragment += 1
        block = block.next()
    return runs


def _assert_fully_visible(overlay, app):
    """Ogni riga mostrata e' TUTTA visibile: in altezza e in larghezza, alla
    geometria che il layout ha dato davvero alla label."""
    app.processEvents()
    label = overlay.status
    real = label.contentsRect().width()
    assert overlay._status_text_width() == real, (
        "misura e disegno devono usare la stessa larghezza")
    needed = _required_height(label, real)
    assert needed <= label.height(), (
        f"la label e' alta {label.height()} px ma il testo ne chiede {needed}:"
        " l'ultima riga si vedrebbe tagliata")
    assert needed <= label.minimumHeight(), "l'altezza riservata non basta al testo"
    if label.textFormat() == Qt.TextFormat.RichText:
        assert _document(label).idealWidth() <= real, "una riga sborda di lato"
    card = overlay.card.geometry()
    assert card.bottom() < overlay.height(), "la scheda esce dalla finestra"


# ── 1. Nessuna riga tagliata ──────────────────────────────────────────


@pytest.mark.parametrize("point_size", (9, 14))
@pytest.mark.parametrize("count", (3, 5, 8))
def test_every_listed_line_is_fully_visible(make_overlay, count, point_size):
    """@brief Positivo: 3, 5 e 8 documenti, due corpi di font, tutti visibili.

    E' la prova che avrebbe preso la riga tagliata: si confronta l'altezza che
    il testo chiede alla larghezza VERA della label con l'altezza che la label
    ha dopo il layout, non con una stima fatta di righe per interlinea.
    """
    overlay, _, app = make_overlay(900, 650, point_size)
    overlay.set_status(_documents(count))
    lines = _lines(overlay)
    assert len(lines) == count + 1 or lines[-1] == "…"
    assert lines[0].startswith(f"Invio a AI {count} documenti")
    _assert_fully_visible(overlay, app)


def test_a_line_between_the_old_and_the_true_width_does_not_wrap(make_overlay):
    """@brief Regressione della causa: la riga larga fra la larghezza vera
    della label e quella che il codice credeva (2 px in piu', il bordo della
    scheda).

    Con la misura vecchia quella riga NON veniva accorciata, la label la
    mandava a capo, e l'elenco chiedeva una riga piu' di quelle riservate. Qui
    la riga viene accorciata alla larghezza vera: resta una riga, e l'altezza
    chiesta sta in quella riservata. L'elenco e' lungo apposta (piu' righe di
    quelle riservate d'ufficio), cosi' la riga in piu' non si nasconde nel
    margine delle righe di base.
    """
    overlay, _, app = make_overlay(900, 650)
    real = overlay._status_text_width()
    believed = overlay.card.minimumWidth() - 2 * _CARD_PADDING
    assert believed > real, "la prova presuppone il bordo della scheda"
    metrics = overlay.status.fontMetrics()
    head = "Riga di prova con parole brevi che si allunga fino al bordo"
    wide = next((text for tail in itertools.product("iltfjrI.,:;!-", repeat=3)
                 for fill in range(0, 80)
                 for text in [f"{head} {'m' * fill}{''.join(tail)}"]
                 if real < metrics.horizontalAdvance(text) <= believed), None)
    if wide is None:
        pytest.skip("nessuna combinazione di glifi cade nei 2 px con questo font")
    items = [wide] + [f"motivo {index}" for index in range(7)]
    overlay.set_status("Esito: " + " · ".join(items))
    lines = _lines(overlay)
    assert len(lines) == len(items) + 1
    assert lines[1] != wide and lines[1].endswith("…"), (
        "la riga va accorciata alla larghezza vera, non lasciata andare a capo")
    _assert_fully_visible(overlay, app)


def test_a_short_window_still_prunes_and_declares_the_cut(make_overlay):
    """@brief Negativo dell'altezza: quando l'elenco davvero non ci sta, resta
    il comportamento dichiarato -- voci tolte in fondo e un'ellissi su una riga
    propria -- e cio' che resta e' tutto visibile."""
    overlay, _, app = make_overlay(900, 480)
    overlay.set_status(_documents(8))
    lines = _lines(overlay)
    assert 2 < len(lines) < 9 and lines[-1] == "…"
    assert " — " in lines[1]
    _assert_fully_visible(overlay, app)


def test_a_list_is_refitted_when_the_window_narrows(make_overlay):
    """@brief Le righe erano accorciate per la larghezza di prima: su una scheda
    piu' stretta sbordavano o andavano a capo. Al ridimensionamento l'elenco si
    riaccorcia e si rimisura; la riga semplice che segue resta centrata."""
    overlay, parent, app = make_overlay(1400, 800, 14)
    overlay.set_status(_documents(5))
    app.processEvents()
    wide = overlay._status_text_width()
    parent.resize(560, 800)
    app.processEvents()
    assert overlay._status_text_width() < wide
    assert _bold_runs(overlay.status), "l'elenco deve restare un elenco"
    _assert_fully_visible(overlay, app)
    overlay.set_status("Generazione del footprint KiCad…")
    assert overlay.status.textFormat() == Qt.TextFormat.PlainText
    assert overlay.status.alignment() == Qt.AlignmentFlag.AlignCenter


# ── 2. Nomi dei file in grassetto, staccati dal ruolo ─────────────────


def test_file_names_are_bold_and_set_apart_from_their_role(make_overlay):
    """@brief Positivo: ogni nome e' in grassetto, e SOLO il nome; il ruolo
    segue la lineetta spaziata. Nomi con spazi, senza estensione o UPPER_SNAKE
    sono nomi come gli altri: nessuna regola sulla loro forma."""
    overlay, _, app = make_overlay()
    names = ('Foto scheda dall alto.jpg', 'MOD_REV_B', 'pinout.png')
    overlay.set_status(_files_sent_message(
        [Path('d') / name for name in names],
        {'Foto scheda dall alto.jpg': 'top', 'MOD_REV_B': 'pinout'}))
    assert _bold_runs(overlay.status) == list(names)
    assert _lines(overlay)[1:] == ['Foto scheda dall alto.jpg — Immagine con quote',
                                   'MOD_REV_B — Pinout / disposizione contatti',
                                   'pinout.png — ruolo non dichiarato.']
    assert overlay.status.alignment() & Qt.AlignmentFlag.AlignLeft
    _assert_fully_visible(overlay, app)


def test_a_list_without_declared_terms_gets_no_bold(make_overlay):
    """@brief Negativo: i motivi di un inventario parziale sono un elenco, ma
    nessuna voce dichiara un termine. I due punti dentro la voce («component
    J2: …») non sono il separatore del termine: il grassetto non compare, e le
    righe restano quelle di prima."""
    overlay, _, app = make_overlay()
    message = ('Inventario globale parziale: candidate 91: invalid status'
               ' · component J2: merged box excludes most of sources'
               ' · proposte irrisolte conservate.')
    overlay.set_status(message)
    assert _bold_runs(overlay.status) == []
    assert _lines(overlay) == enumeration_lines(message)
    _assert_fully_visible(overlay, app)


@pytest.mark.parametrize("item,expected", (
    ("pinout.png — Pinout / disposizione contatti",
     ("pinout.png", "Pinout / disposizione contatti")),
    ("vista laterale.png — Immagine con quote", ("vista laterale.png", "Immagine con quote")),
    ("MOD_REV_B — ruolo non dichiarato", ("MOD_REV_B", "ruolo non dichiarato")),
    ("component J2: merged box excludes most of sources", None),
    ("pinout.png, Pinout / disposizione contatti", None),
    ("2.54mm, passo dichiarato", None),
    ("— descrizione senza termine", None),
    ("termine senza descrizione —", None),
))
def test_the_term_rule_reads_the_separator_not_the_name(item, expected):
    """@brief La regola e' sul SEPARATORE dichiarato. Positivi: nomi con spazi,
    senza estensione, UPPER_SNAKE. Negativi: la virgola (dentro un nome e'
    legittima, quindi non puo' separare), i due punti, un lato vuoto."""
    assert enumeration_term(item) == expected


def test_one_document_is_not_a_list_and_stays_plain(make_overlay):
    """@brief Negativo: un solo documento non e' un elenco, quindi resta una
    riga di testo semplice e centrata, senza grassetto, anche se la voce ha la
    lineetta."""
    overlay, _, _ = make_overlay()
    message = _files_sent_message([Path('d/top.png')], {'top.png': 'top'})
    overlay.set_status(message)
    assert overlay.status.textFormat() == Qt.TextFormat.PlainText
    assert overlay.status.text() == message
    assert _bold_runs(overlay.status) == []


def test_names_and_roles_are_never_interpreted_as_markup(make_overlay):
    """@brief Sicurezza del testo ricco: un nome di file con «<b>», «&» o
    «&lt;» si legge com'e'. Il grassetto copre esattamente il nome, e nessun
    tag del nome cambia il peso del resto della riga."""
    overlay, _, app = make_overlay()
    # Niente «/» nei nomi: e' il separatore delle cartelle, quindi non puo'
    # stare in un nome di file. Il tag aperto e non chiuso e' il caso peggiore:
    # interpretato, metterebbe in grassetto tutto il resto dell'elenco.
    names = ('<b>tutto & più.png', 'a&lt;b.pdf', '<i>x')
    overlay.set_status(_files_sent_message([Path('d') / name for name in names],
                                           {name: 'top' for name in names}))
    assert _bold_runs(overlay.status) == list(names)
    assert _lines(overlay)[1:] == [f'{name} — Immagine con quote' for name in names[:2]] + [
        f'{names[2]} — Immagine con quote.']
    assert "&lt;b&gt;tutto" in overlay.status.text()
    assert "&amp;lt;b.pdf" in overlay.status.text()
    _assert_fully_visible(overlay, app)


def test_the_log_line_stays_one_plain_line():
    """@brief Il log non cambia natura: una riga, nessun markup, nessun a capo.
    Il grassetto nasce sulla scheda, non nel messaggio."""
    message = _documents(5)
    assert "\n" not in message and "<" not in message
    assert message.count(" · ") == 4 and message.count(" — ") == 5


def test_a_very_long_term_is_elided_in_bold_without_breaking_the_markup(make_overlay):
    """@brief Accorciamento col testo ricco: si accorcia il testo semplice e
    POI si costruisce il markup, quindi il taglio non cade mai dentro un tag o
    un'entita'. Un termine piu' largo della scheda con una descrizione breve
    si accorcia NEL MEZZO (col font in grassetto con cui e' disegnato: restano
    inizio ed estensione) e la descrizione resta intera; un termine corto con
    una descrizione lunghissima tiene il termine intero e accorcia la
    descrizione.

    CAMBIATO il 2026-09-24: prima il termine lunghissimo si accorciava in coda
    e la descrizione spariva. Su Windows lo stesso ordine tagliava il ruolo di
    un documento («Foto ad alta risoluzio…») anche con un nome gia' accorciato
    dal mittente, in contrasto con
    test_a_very_long_name_is_shortened_by_the_sender_and_shown_whole_line; i
    due contratti convivevano solo perche' coi caratteri di Linux quella riga
    stava nella scheda. La regola e' in `_elide_row`."""
    overlay, _, app = make_overlay()
    long_name = "&<" + "nome-lunghissimo-" * 30 + ".png"
    long_role = "descrizione " * 40
    overlay.set_status(f"Documenti: {long_name} — ruolo · corto.png — {long_role}")
    lines = _lines(overlay)
    assert len(lines) == 3
    shown, separator, role = lines[1].partition(" — ")
    assert separator and role == "ruolo"
    assert shown.startswith("&<nome-lunghissimo-") and shown.endswith(".png") and "…" in shown
    assert lines[2].startswith("corto.png — descrizione") and lines[2].endswith("…")
    runs = _bold_runs(overlay.status)
    assert runs == [shown, "corto.png"]
    _assert_fully_visible(overlay, app)


def test_the_plain_reader_of_the_status_sees_what_is_shown(make_overlay):
    """@brief `status_plain_text` restituisce le parole, non il markup: per lo
    stato semplice e' il testo della label, per l'elenco il testo del documento."""
    overlay, _, _ = make_overlay()
    overlay.set_status("<b>Crop 2: analisi</b>")
    assert overlay.status_plain_text() == "<b>Crop 2: analisi</b>"
    overlay.set_status(_documents(2))
    assert "<b>" in overlay.status.text()
    assert "<" not in overlay.status_plain_text()
    assert overlay.card.minimumHeight() + 2 * _OVERLAY_MARGIN <= 650
