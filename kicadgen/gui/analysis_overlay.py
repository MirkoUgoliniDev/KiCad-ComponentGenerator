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

#: The tools that can work during an analysis, each with its own graphic
#: mark. The distinction that matters to the viewer is not aesthetic: the
#: local detector costs nothing and is quick, the model call is paid for,
#: the CAD keeps the machine busy for minutes. Colour alone is not enough to
#: say it, so every tool also has a different shape.
#:
#: Generation has three steps and each one says WHO is working (user,
#: 2026-09-26: «voglio vedere bene quando entra in azione FreeCAD o KiCad»
#: (I want to see clearly when FreeCAD or KiCad steps in)): the footprint is
#: written by this program's own code (KiCad is not running); the model
#: selection reads the FILES of KiCad's 3D library and, for candidates not
#: yet measured, starts FreeCAD (`freecad_measure`, same colour as FreeCAD,
#: caliper mark); the solid is built by FreeCAD.
#:
#: OpenCV (user, 2026-09-27: «hai evidenziato lo step OpenCV?» (did you
#: highlight the OpenCV step?)) works locally on the photograph: it finds and
#: calibrates the board, then measures the edges of the proposed bodies.
#: Before, these steps were not announced and the modal stayed on the
#: previous phase while they ran.
TOOLS = {
    "llm": ("Modello AI", "#f9e2af", "cloud"),
    # The two paid phases that follow the global inventory, with a name of
    # their own (user, 2026-09-29, HANDOFF 156): «Modello AI — in corso» (AI
    # model — running) for two minutes did not say what was being paid for.
    "llm_crops": ("Lettura ritagli", "#f9e2af", "crop"),
    "llm_markings": ("Lettura sigle", "#f9e2af", "tag"),
    "yolo": ("YOLO", "#a6e3a1", "frame"),
    # The neural network that classifies the kept bodies (user, 2026-10-02:
    # «devi anche darne contezza nella modale» (you must also account for it
    # in the modal)). It was an alias of YOLO, so the local classification
    # presented itself as a second detection.
    "smd": ("Rete SMD locale", "#f5c2e7", "network"),
    "footprint": ("KiCadGen (footprint 2D, calcolo locale)", "#89b4fa", "pads"),
    "kicad": ("Libreria modelli 3D di KiCad", "#94e2d5", "library"),
    "consolidated": ("Consolidati — OpenCV", "#94e2d5", "lens"),
    "freecad_measure": ("FreeCAD (misura dei modelli)", "#cba6f7", "caliper"),
    "freecad": ("FreeCAD (modellazione locale)", "#cba6f7", "cube"),
    "opencv": ("OpenCV", "#fab387", "aperture"),
}

#: Glyphs of the phases that call a paid remote model: the cloud and its two
#: specialisations (crop, marking). They are the only phases that send data
#: (TRANSFER_SOURCES) and that are interrupted with the spending
#: confirmation.
REMOTE_GLYPHS = frozenset({"cloud", "crop", "tag"})

#: Phases that are the *same* remote call as a TOOLS entry (same name,
#: colour and mark) but start from different data. They stay out of TOOLS
#: because there, by contract, every tool has a shape of its own: here only
#: what is sent changes, not who is working.
TOOL_ALIASES = {
    "llm_inventory": "llm",
}

#: What leaves the machine for the remote model, for the phases that send
#: data: "documents" are the extraction documents, "board" the photograph of
#: the board with the bodies proposed by the detector (global inventory). A
#: phase absent from here sends nothing and shows only the ring.
TRANSFER_SOURCES = {
    "llm": "documents",
    "llm_inventory": "board",
    "llm_crops": "board",
    "llm_markings": "board",
}

#: Lines the program writes for itself: AI call markers, import outcomes,
#: sha256 fingerprints. They belong in the log, where they help reconstruct a
#: run, not on the card, where they take the place of what is happening
#: without telling anyone.
#:
#: The marker is searched at the START of the line, after the optional
#: forwarding prefix ("FreeCAD: ") with which whoever relays freecadcmd's
#: output marks its origin. At least one underscore between capitals is
#: required, so a single word such as "ATTENZIONE:" stays a sentence for the
#: viewer.
#:
#: The anchor is not a detail: in this domain UPPER_SNAKE is also the
#: convention of reference designators and library names (U1_MODULE,
#: J_HEADER_BLK, RX_TX, SW_BOOT). Searching for the marker *anywhere* in the
#: line turned the rule, in practice, into a rule on the names of the user's
#: components and discarded real outcomes («Componente J_HEADER_BLK non
#: riconosciuto: geometria generica.», «ATTENZIONE 3D: componente U1_MODULE
#: (rf_module): geometria approssimata»). In a sentence the designator is
#: never the first word, because the sentence names the thing first; in a
#: service line the marker opens the line because it is the record header.
#:
#: After the marker, ":"/"=" is accepted (strong form, free of ambiguity),
#: the end of the line, or a space. The last alternative is a DECLARED
#: heuristic: it serves the dumps this program writes about itself
#: ("AI_CALL_PROMPT_SYSTEM <prompt text>", "AI_CALL_START id=1 ...") and
#: relies only on position, so it would also discard a hypothetical sentence
#: that *starts* with a designator. It would become unnecessary the day
#: ai_diagnostics separated marker and payload with ":" as the KICADGEN_*
#: markers do.
_MACHINE_MARKER = re.compile(
    r"^(?:\w+:\s+)?[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?:\s*[:=]|\s|$)")

#: The marker above recognises only what *this* program prints for itself.
#: The card also received the lines external programs write on their own
#: stdout (file-write outcomes, session headers, entity counts): they have no
#: marker, so they must be recognised by their *shape*.
#: The rules below name no product and no exact sentence -- tying them to
#: "FreeCAD" or to the list of sentences seen in one run would mean redoing
#: the work at the next version of the same tool.
#:
#: Every rule is a typographic heuristic, calibrated on how a machine writes
#: and on how a person writes; they are declared as such because none is a
#: general truth, and they would become unnecessary if external tools marked
#: their own lines as this program does.

#: Decoration: three or more repetitions of the same punctuation sign
#: ("******", "=====", "-----"). The full stop, the exclamation mark and the
#: question mark are excluded: ellipses and emphatic punctuation
#: ("Attenzione!!! il modello non ha risposto") are perfectly normal in a
#: sentence written for the viewer, and a trace decoration is never made of
#: signs that carry intonation in prose.
_DECORATION_RUN = re.compile(r"([^\w\s.!?])\1{2,}")

#: The full stop excluded above must still be told apart from an ellipsis:
#: the ellipsis in prose is exactly "..." or "…", whereas "Recompute......"
#: is the filler of a textual progress bar. Four or more dots are not
#: punctuation in either of the two languages used here.
_DOT_FILL_RUN = re.compile(r"\.{4,}")

#: Key and value separated by space-colon-space ("Step File Name : C:/…",
#: "WorkSession : Sending all data"). In Italian or English prose the colon
#: sticks to the preceding word, so the space before it is the sign that the
#: line is a field of a trace, not a sentence.
_SPACED_KEY_VALUE = re.compile(r"\S\s+:\s+\S")

#: "key=value" record: a short label and then ONLY pairs, up to the end of
#: the line ("FreeCAD exit=0, timeout=False", "Inventario globale: prompt=…;
#: schema=…; sha256=…", "AI_CALL_END id=1 elapsed=65.664s tokens_in=16765").
#: An equals sign attached to the field name is how a machine serialises a
#: record, whereas in prose the equals sign carries spaces ("drill_mm = 0 e
#: pad_diameter_mm = larghezza stimata"). It makes the promise of
#: set_status's docstring true: lines with sha256 stay in the log.
#:
#: DECLARED HEURISTIC on the label: at most two words before the first pair.
#: A record is the NAME of what is being recorded plus its fields; if a datum
#: written for the viewer already appears between the name and the fields,
#: the line is a sentence, not a record. It is the only criterion that
#: separates «Inventario globale: prompt=…; schema=…» (record) from
#: «Generazione TEST: 2x22, pitch=2.54mm, row_spacing=22.86mm, pad=1.7mm,
#: drill=1.0mm» (summary of what is being generated, to be shown): the two
#: forms are otherwise identical, so the separation is fragile and must be
#: pinned by tests in both directions. It would become unnecessary if the
#: records carried a marker like the KICADGEN_* ones.
_KEY_VALUE_RECORD = re.compile(r"^(?:\S+\s+){0,2}\w+=\S+(?:[;,]?\s+\w+=\S+)+$")

#: Bare percentage, without a subject ("(47 %)", "FreeCAD: (100 %)"). It
#: says neither what it refers to nor how much is left, and it hangs there
#: for the whole CAD phase in place of the lines saying which component is
#: being built. It is the same reason set_progress rejects time-based
#: estimates: a number without a referent looks like information and is not.
#: The real bar, when a count exists, has a line of its own.
_BARE_PERCENT = re.compile(r"^(?:\w+:\s+)?\(?\s*\d+(?:[.,]\d+)?\s*%\s*\)?\.?$")

#: Start-up headers of an external program: name plus semantic version
#: ("FreeCAD 1.1.1, Libs: 1.1.1R20260414") and copyright line
#: ("(C) 2001-2026 …"). Both are anchored at the start, so they cannot catch
#: a dimension or a year quoted inside a sentence.
_VERSION_BANNER = re.compile(r"^(?:\w+:\s+)?\S+ \d+\.\d+\.\d+\b")
_COPYRIGHT_BANNER = re.compile(r"^(?:\w+:\s+)?(?:\(c\)|©)\s*\d{4}", re.IGNORECASE)

#: Timestamp at the start of the line, with or without square brackets
#: ("[2026-09-21T00:08:36+02:00] FreeCAD start", "2026-09-21 00:04:13 Analisi
#: AI"). A line that dates itself is a session record: the card's clock
#: already says how long the wait has been.
_TIMESTAMP_HEAD = re.compile(r"^\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

#: Entity count in parentheses: a large number followed by a short
#: abbreviation and NOT inside a finished sentence ("…/Test.step(134238 ents)
#: Write Done", "Shapes written (98211 ents)").
#:
#: SEPARATION DECLARED FRAGILE. From the count alone "(134238 ents)" cannot
#: be told from "(12345 pezzi)": "pezzi", "test", "corpi", "righe", "fori"
#: all fit within the six letters of the abbreviation, so the number of
#: digits would be the only criterion, and one criterion out of two is not
#: enough (see CLAUDE.md). The second criterion is typographic: prose ends
#: with a sentence-ending sign, the record does not. So
#: "Importati (12345 pezzi) dal modello STEP." and
#: "Suite locale: (1061 test) superati." stay on the card. This too remains
#: a fallback: a prose line without a final
#: full stop and with a four-digit count would be discarded. It would become
#: unnecessary if freecadcmd marked its own lines as this program does.
_ENTITY_COUNT = re.compile(r"\(\s*\d{4,}\s+\w{1,6}\.?\s*\)(?![^.!?…]*[.!?…]\s*$)")

_MACHINE_LINE_RULES = (_MACHINE_MARKER, _DECORATION_RUN, _DOT_FILL_RUN,
                       _SPACED_KEY_VALUE, _KEY_VALUE_RECORD, _BARE_PERCENT,
                       _VERSION_BANNER, _COPYRIGHT_BANNER, _TIMESTAMP_HEAD,
                       _ENTITY_COUNT)

#: Absolute filesystem path: drive letter ("C:/…", "D:\\…"), UNC path, or
#: POSIX path with at least two levels ("/home/x/…").
#:
#: The path is NO LONGER a reason to discard the line. This is a tool whose
#: product IS files: «Inventario salvato: …» (inventory saved),
#: «Proposte del rilevatore salvate: …» (detector proposals saved) and above all
#: «Inventario non salvato su disco: [Errno 28] No space left on device: '…'»
#: (inventory not saved to disk) are outcomes the user must read, and the old
#: rule hid them all, including a write error, because the OSError text
#: carries a path. Of the two proposed solutions, SHORTENING the path and
#: keeping the line is chosen: the "almost only a path" criterion would have
#: discarded precisely «Inventario salvato: <three paths>», which is the line
#: to show. What remains of the path is the file name, which is the only part
#: the user recognises; the full text is in the log, one click away.
#:
#: The lookbehind on "/" avoids shortening the inside of a URL, and the
#: mandatory second level leaves model slugs ("anthropic/claude-opus-5") and
#: file names without separators untouched.
_DRIVE_PATH = re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s;,'\")]*")
_UNC_PATH = re.compile(r"\\\\[\w.$-]+\\[^\s;,'\")]*")
_POSIX_PATH = re.compile(
    r"(?:^|(?<=[\s(\"'=]))(?<!/)~?/[\w.+-]+/[^\s;,'\")]*")

_PATH_RULES = (_DRIVE_PATH, _UNC_PATH, _POSIX_PATH)

#: The status is truncated: beyond this length the line cannot be read at a
#: glance, and the full text is in the log anyway.
_STATUS_CHAR_LIMIT = 220

#: Separator with which this program lists several ITEMS inside a single
#: progress line: the documents leaving in the paid call
#: (`datasheet_extractor._files_sent_message`), the reasons for a partial
#: inventory (`photo_global_inventory._DETAIL_SEPARATOR`).
#:
#: The line stays one: the log lives on one event per line, with a single
#: timestamp, and splitting it there would make a run unreadable backwards.
#: The card instead needs the list -- user request:
#: «i nomi dei file uno sotto l'altro» (the file names one below the other)
#: -- so it is HERE that
#: the line is re-laid out, without the source knowing anything about the
#: card.
#:
#: The middle dot is not a whim: in Italian and in English it is not a
#: punctuation sign, so it does not appear in a sentence written for the
#: viewer, and the two sources above already used it to list without
#: resorting to the semicolon, which the modal's filters count. Recognising
#: the list from this separator is therefore a rule on the SHAPE, valid for
#: any line that lists, not a rule on the documents message.
_LIST_SEPARATOR = " · "

#: Separator between the TERM of an item and its description: «pinout.png —
#: Pinout / disposizione contatti». It is the typographic convention of a
#: definition-list item (term, dash, explanation), and the card renders it as
#: such: the term in bold, the spaced dash setting it apart from the rest.
#: User request:
#: «i nomi dei file non sono evidenziati, troppo attaccati al testo»
#: (the file names are not highlighted, too close to the text).
#:
#: A rule on the SHAPE, like the two previous ones: it does not look at
#: whether the term "looks like" a file name (a name can have spaces, no
#: extension, capitals and underscores), it only looks at whether the item
#: DECLARES a term with this sign. The documents source writes it on purpose
#: (`datasheet_extractor._files_sent_message`); an item that does not carry
#: it -- the reasons for a partial inventory, «component J2: …» -- stays
#: plain text.
#:
#: SEPARATION DECLARED FRAGILE: the sign is not reserved. A file name that
#: itself contains " — " would be split there (bold only on the first part;
#: all the text stays, only the weight changes), and third-party text
#: interpolated into an item (an API error) containing it would have its
#: first part in bold. The split is on the FIRST occurrence because the term
#: is at the start by definition. It would become unnecessary if the status
#: reached the card as structured data instead of as a string.
_TERM_SEPARATOR = " — "

#: Text lines reserved for the status. The card is sized on this number, not
#: on the sentence passing at that moment: a card that changes height at
#: every message is more annoying than a card that is a little taller.
_STATUS_LINES = 3
#: Lines over which the truncated status is SPREAD to derive the card width:
#: fewer lines = wider card. It was 2.5 (three lines with half a line of
#: margin); the user (2026-09-30, HANDOFF 173) wanted it wider, so the phase
#: lines get truncated later: two lines, hence about a quarter more, always
#: within the width the window allows.
#: The three reserved lines stay: one whole line of margin for wide letters.
_STATUS_WRAP_LINES = 2.0
#: Completed phases listed on the card (HANDOFF point 135): the most recent
#: ones, one row each. Bounded so the card cannot outgrow a small window.
PHASE_HISTORY_ROWS = 4

#: Horizontal inner margin of the card (per side), used both by the layout
#: and by the width computation: they agree by construction.
_CARD_PADDING = 28

#: Margin between the window edge and the card (per side). It is the same
#: value as the overlay's contentsMargins, and it is also used to deduce how
#: much width the card can really take.
_OVERLAY_MARGIN = 24

#: Desired minimum width: avoids a skinny card with very narrow characters.
#: There is NO LONGER a constant maximum: the ceiling is deduced from the
#: window hosting the overlay (see _available_card_width). A fixed ceiling
#: (760 px) cancelled the derivation from the font and CUT the text from
#: 14 pt upwards, that is precisely in the case -- large font, low vision --
#: where the card is needed most.
_CARD_MIN_WIDTH = 480

#: Width to use when the window does not have a size yet (first layout): it
#: is a temporary fallback, the first resize replaces it.
_CARD_FALLBACK_WIDTH = 960

#: Twin of the previous one for the height: it is used to decide how many
#: list lines the window allows when the window (and the screen) do not have
#: a size yet. Same temporary fallback, same short life.
_CARD_FALLBACK_HEIGHT = 720

#: Worst possible sentence of the maximum allowed length, made of words of
#: wide letters: it is used to count the lines the status REALLY takes at the
#: chosen width, instead of trusting the average character width.
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
    @note DECLARED HEURISTIC on the cut of the lead-in: the FIRST ": " of the
        line is used, not the last. The list begins where the text announces
        it, while the colons that appear later belong to the item containing
        them: the outcomes of this program write items such as «component
        U1: merged box excludes…», and cutting on the last ": " the first item
        would lose precisely its subject. It would become superfluous the day
        the lines carried the list in structured form instead of punctuated;
        as long as the channel is a single string, the punctuation is the
        channel.
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
    """Shorten ONE list row to the real width, on the plain text.

    Returns (term, text): the term is None for rows without a declared term.
    The row is shortened BEFORE the markup is built, so the cut cannot fall
    inside a tag or an HTML entity, and the term is measured with the bold
    font it will be drawn with: measured with the regular weight, the bold
    would overflow by a few pixels.

    When the row does not fit, the TERM is shortened first, in the middle
    (the beginning and the end remain, that is the extension of a file name)
    and the description stays whole, provided the description with its
    separator takes at most half the row. The term is a datum as long as its
    author wanted; the description, in the rows that carry one (the role of a
    document), is a short label saying the thing to know. The description
    used to be cut first: with Windows fonts a name already shortened to 40
    characters by the sender left «Foto ad alta risoluzio…».

    The half is a declared choice, not a measurement: neither part takes
    from the other more than the room it leaves. It is not tuned on any
    case; it would be superfluous if list rows could wrap. Beyond that
    threshold the `ElideRight` rule applies: the HEAD is kept, the
    description is shortened; if there is no room even for the ellipsis after
    the term, the term is shortened and the description disappears.
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
    """Width in px of ONE row as the label's document draws it."""
    document = QTextDocument()
    document.setDefaultFont(font)
    document.setDocumentMargin(0)
    document.setHtml(_enumeration_markup([row]))
    return document.idealWidth()


def _fit_row(line, width, font, is_item):
    """Shorten a row until the document that draws it fits in `width` px.

    The font metrics summed piece by piece (bold term, then the rest) can
    differ by a fraction of a pixel from the document layout, which places
    the glyphs its own way. Instead of a margin chosen by eye the row is
    MEASURED as it will be drawn and, if it overflows, shortened again by
    exactly the excess. The loop is short by construction: every turn reduces
    the room by at least one pixel, and the excess is a fraction of a glyph.
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
    """List markup: one row per item, bold term, no wrapping.

    Everything coming from the rows is ESCAPED: file names and other people's
    texts are data, and a name such as «<b>x</b>.png» must read as it is, not
    change the weight of the rest. `white-space: pre` forbids wrapping: every
    row has already been shortened to the real width, so the number of drawn
    rows is EXACTLY the number of items, and the card is sized on that
    number.
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
        #: Ink colour. This card's style sheet changes the colour per tool,
        #: and here the painting is done by hand: the value is kept instead
        #: of deducing it from the palette, which with a style sheet may not
        #: be up to date.
        self.ink = None

    def paintEvent(self, event):
        """@brief Paint the text elided to the available width.

        @param event Qt paint event.
        """
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
        """@brief Paint the ring, its moving arc and the tool mark.

        @param event Qt paint event.
        """
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
            # Cloud: the call leaves the machine and is paid for. The three
            # circles must be *united*, not overlapped: the inner arcs read
            # as three bubbles, that is as no recognisable shape.
            path = QPainterPath()
            for box in (QRectF(18, 27, 12, 11), QRectF(25, 22, 15, 15),
                        QRectF(22, 30, 15, 8)):
                blob = QPainterPath()
                blob.addEllipse(box)
                path = path.united(blob)
            painter.drawPath(path.simplified())
        elif self.glyph == "crop":
            # Crop: the frame cropped from the photo, with inside it the
            # solid body the model looks at closely. Same colour as the
            # cloud (it is paid for), different shape (test: two tools are
            # never drawn the same).
            painter.drawRect(QRectF(17, 17, 22, 22))
            painter.setBrush(QColor(self.color))
            painter.drawRoundedRect(QRectF(24, 24, 8, 8), 1.5, 1.5)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for x in (20.0, 36.0):
                painter.drawLine(QPointF(x, 28), QPointF(x + 2, 28))
            for y in (20.0, 36.0):
                painter.drawLine(QPointF(28, y), QPointF(28, y + 2))
        elif self.glyph == "tag":
            # Marking: a label with two lines of text, what is printed on the
            # body and what the model reads again from the enlarged crop.
            painter.drawRoundedRect(QRectF(16, 20, 24, 16), 2, 2)
            painter.drawLine(QPointF(20, 26), QPointF(36, 26))
            painter.drawLine(QPointF(20, 31), QPointF(30, 31))
        elif self.glyph == "frame":
            # Viewfinder: four corner brackets plus the target at the centre.
            # A closed frame was confused with the footprint and the cube at
            # the size it is drawn at; the detached brackets are the mark
            # with which a detector marks a body, and the solid dot says the
            # frame is *measured*, not drawn by hand.
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
            # Isometric cube: a solid is being built.
            top, left, right = QPointF(28, 17), QPointF(18, 23), QPointF(38, 23)
            low_l, low_r, bottom = QPointF(18, 34), QPointF(38, 34), QPointF(28, 40)
            centre = QPointF(28, 28)
            for start, end in ((top, left), (top, right), (left, low_l),
                               (right, low_r), (low_l, bottom), (low_r, bottom),
                               (left, centre), (right, centre), (top, centre)):
                painter.drawLine(start, end)
        elif self.glyph == "library":
            # Library: three book spines on a shelf, one tilted. An existing
            # file is chosen, nothing is built.
            for x, top in ((17.0, 19.0), (22.0, 17.0), (27.0, 20.0)):
                painter.drawRoundedRect(QRectF(x, top, 4, 38 - top), 1, 1)
            painter.drawLine(QPointF(33, 21), QPointF(38, 37))
            painter.drawLine(QPointF(15, 38), QPointF(41, 38))
        elif self.glyph == "lens":
            # Magnifying glass with a grid of pixels inside: OpenCV looks at
            # the photograph and measures it, it neither detects nor builds.
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
            # Caliper: a beam with two jaws, one fixed and one sliding. Here
            # FreeCAD MEASURES existing models, it does not build the solid.
            painter.drawLine(QPointF(16, 22), QPointF(40, 22))
            painter.drawLine(QPointF(18, 22), QPointF(18, 36))
            painter.drawLine(QPointF(18, 36), QPointF(21, 33))
            painter.drawRect(QRectF(28, 19, 5, 6))
            painter.drawLine(QPointF(30.5, 25), QPointF(30.5, 36))
            painter.drawLine(QPointF(30.5, 36), QPointF(27.5, 33))
        elif self.glyph == "network":
            # Neural network: three layers of connected solid nodes. It
            # classifies locally what the detector found, sending nothing.
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
            # Footprint: two rows of *solid* pads around the body outline.
            # Hollow pads read as another frame; the solid copper against the
            # outline stroke immediately tells this mark apart from the CAD
            # solid, which is all wireframe.
            # Three pads per row, 4 px wide and 4 apart: the outline stroke
            # (1.6 px) eats 1.6 of every gap, so with the previous 1 px gap
            # the three fused into a single bar and the mark read as two
            # bars instead of six pads.
            painter.setBrush(QColor(self.color))
            for row in (17.0, 35.0):
                for column in (18.0, 26.0, 34.0):
                    painter.drawRoundedRect(QRectF(column, row, 4, 4), 1.2, 1.2)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # Body outline between the two rows, detached from the pads, with
            # the pin 1 mark on the left: it is how a footprint is read.
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
        """@brief Paint the source icon and the packets moving to the right.

        @param event Qt paint event.
        """
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
        # Dashed track between the data and the cloud, then the packets.
        start, end, y = 56.0, float(self.width() - 8), 28.0
        rail = QPen(QColor("#45475a"), 1.4, Qt.PenStyle.DashLine)
        painter.setPen(rail)
        painter.drawLine(QPointF(start, y), QPointF(end, y))
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(self.PACKETS):
            t = (self.offset + index / self.PACKETS) % 1.0
            color = QColor(self.color)
            # They appear and fade at the ends instead of jumping.
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

    @note The card covers the whole window and the tabs beneath are disabled
        while work runs: without a command leaving from here the only way out
        was closing the program. The button sits next to «Apri log AI»
        because that row is the only part of the card one can act on; what
        the abort *does* depends on the job in progress and is decided by the
        window, not by this card (see `abort_requested`).
    """
    log_requested = pyqtSignal()
    #: Emitted on a click on «Interrompi» (Stop). The card does not know what
    #: is working -- a FreeCAD process can be killed, an HTTP request already
    #: sent cannot -- so it decides nothing: it asks, and whoever started the
    #: work chooses. It is also why this module does not import main_window:
    #: the dependency goes one way only.
    abort_requested = pyqtSignal()
    #: Emitted when the user closes a card left open after the work finished
    #: (`show_completed`): whoever left it open resumes from there.
    closed = pyqtSignal()
    #: Emitted at the start of an operation with the title already translated,
    #: and at the end: the window status bar says «Pronto» (Ready) or what is
    #: in progress.
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
        # For remote calls the local data sit to the left of the ring with
        # the cloud, and the packets travel towards it (user, 2026-09-26).
        # The row has the ring's height: appearing or disappearing does not
        # make the card jump.
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
        # Who is working, in plain words and next to its mark. Without this
        # line the only clue was the service messages scrolling in place of
        # the status, which is exactly what was not supposed to be read.
        # A normal QLabel, centred and without wrapping, was cut on both
        # sides already at the default font (526 px available against 638
        # needed): the initial word was lost, so the header became
        # unreadable precisely while other lines are discarded "because the
        # header already says it".
        self.tool = ElidedLabel()
        self.tool.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tool.setProperty("i18n_skip", True)
        self.tool.setStyleSheet("font-weight: 600;")
        self.tool.hide()
        content.addWidget(self.tool)
        # The steps of a multi-phase job (generation): the running one
        # highlighted, the done ones ticked, the unavailable ones struck
        # through with the reason. Hidden for single-phase operations.
        self.steps = QLabel()
        self.steps.setObjectName("analysisSteps")
        self.steps.setTextFormat(Qt.TextFormat.RichText)
        self.steps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.steps.setWordWrap(True)
        self.steps.setProperty("i18n_skip", True)
        self.steps.hide()
        content.addWidget(self.steps)
        # The phases of this operation already finished, with their outcome
        # (user, 2026-09-28): a local phase such as OpenCV lasts less than a
        # second and without this list it was not even seen passing.
        self.phases = QLabel()
        self.phases.setObjectName("analysisPhases")
        self.phases.setTextFormat(Qt.TextFormat.RichText)
        self.phases.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.phases.setProperty("i18n_skip", True)
        self.phases.hide()
        content.addWidget(self.phases)
        #: Finished phases: (tool name, outcome, is a warning), as Italian
        #: SOURCES. They are translated when drawn, so a language change in
        #: mid-run redraws the list in the new language (HANDOFF 138, C7; see
        #: `refresh_command_texts`).
        self._phase_history: list[tuple[str, str, bool]] = []
        #: True from the start of an operation until it declares steps: the
        #: list lines are reserved right away (`_size_the_card`), like the
        #: other optional lines, so the card does not grow when the first
        #: phase ends.
        self._phases_reserved = False
        #: Running phase: key, source name, first status shown (its outcome)
        #: and last warning, which replaces it; source texts here too.
        self._phase_key = ""
        self._phase_name = ""
        #: Tick counter of the pulse of the running phase's «●» (before any render).
        self._pulse_tick = 0
        self._phase_last_status = ""
        self._phase_warning = ""
        #: Count of the running phase (done, total, translated unit), or
        #: None: shown by the «●» row of the list and by the central status.
        self._phase_progress = None
        #: Texts (already translated) of the steps, current step, reasons of the absent ones.
        self._step_labels: list[str] = []
        self._step_current = -1
        self._step_unavailable: dict[int, str] = {}
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(self.status)
        # The bar appears only when a real count exists to show. A
        # percentage estimated on time stops at 90% and stays there: it says
        # less than nothing, because it looks like information.
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
        # The AI analysis summary stays here while the modal is open. It is
        # on a line of its own, not in the status, because the generation
        # progress messages would overwrite it at once: that is why the cost
        # could not be read.
        # A single line that shortens itself, like the header: with wrapping
        # the cost line went from one line to two the moment the summary
        # arrived, and the card changed height before one's eyes -- exactly
        # what the sizing must avoid. The number is at the start of the
        # sentence, so it stays readable even when shortened, and the full
        # text is in the tooltip and in the AI Log.
        self.cost = ElidedLabel()
        self.cost.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cost.setTextFormat(Qt.TextFormat.PlainText)
        self.cost.setStyleSheet("color: #a6e3a1;")
        self.cost.setProperty('i18n_skip', True)
        self.cost.hide()
        # The cost stays close to the inventory status, where there is room,
        # instead of ending up below the elapsed time.
        content.insertWidget(content.indexOf(self.elapsed), self.cost)
        # The two commands are on the same row, in the order the user asked
        # for them: the log on the left, the stop right to its right. The
        # row stays centred as the log button alone was.
        # Icons with name and explanation in the tooltip (user, 2026-09-24),
        # like the toolbars of the other windows. The texts are composed by
        # `refresh_command_texts`, also on a language change: the buttons are
        # `i18n_skip` because the composed tooltip is not a key.
        from .review_tools import tool_button
        self.log_button = tool_button("ai-log.svg", "Apri log AI")
        self.log_button.setObjectName("analysisLogButton")
        self.log_button.clicked.connect(self.log_requested.emit)
        self.abort_button = tool_button("stop.svg", "Interrompi")
        self.abort_button.setObjectName("analysisAbortButton")
        self.abort_button.clicked.connect(self.abort_requested.emit)
        # Larger than the toolbar icons (user, 2026-09-24): in the wide card,
        # with the title at 18pt, the toolbar size looked small.
        # Only here: `tool_button` stays the same for all the other windows.
        for button in (self.log_button, self.abort_button):
            button.setIconSize(QSize(OVERLAY_ICON_PX, OVERLAY_ICON_PX))
        self.refresh_command_texts()
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(12)
        buttons.addStretch()
        buttons.addWidget(self.log_button)
        buttons.addWidget(self.abort_button)
        # When the work is finished the card stays open with all the steps
        # ticked and closes only from here (user, 2026-09-26: the FreeCAD step
        # was never seen finished, the card vanished as soon as it ended).
        self.close_button = QPushButton("Chiudi")
        self.close_button.setObjectName("analysisCloseButton")
        self.close_button.clicked.connect(self._close_completed)
        self.close_button.hide()
        buttons.addWidget(self.close_button)
        buttons.addStretch()
        self.button_row = QWidget()
        self.button_row.setLayout(buttons)
        content.addWidget(self.button_row)
        #: True only while there is a job that makes sense to stop. It starts
        #: false: an idle card must not offer to stop anything.
        self._abortable = False
        self.set_abortable(False)
        #: Height (px) the status takes with the lines reserved by default,
        #: and the extra allowed to a list taller than those. The extra is
        #: MONOTONIC within a run and is reset only in `start()`: a list of
        #: eight documents appears only once, and shrinking the card at the
        #: next message would be the same jump the sizing exists to avoid.
        self._status_base_height = 0
        self._status_extra = 0
        #: Status to show, already translated and with shortened paths but
        #: NOT yet laid out. It is kept because the layout depends on the
        #: geometry: if the window or the font change while a list is on the
        #: card, the list must be shortened and measured again on the new
        #: width, otherwise the lines shortened for the old width overflow.
        self._status_source = ""
        # The card enters the overlay BEFORE being measured: the 1 px border
        # the overlay's style sheet gives it narrows the text space, and a
        # card that is not yet a child of the overlay does not know it.
        outer.addWidget(self.card, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._size_the_card()
        self.status.installEventFilter(self)
        outer.addStretch()
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._tick)
        self.started_at = 0.0
        #: Instant at which the overlay was hidden with an operation running
        #: (body review): on return the clock skips that time.
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
        @note The ceiling must be derived, not chosen: a constant (760 px) was
            at once too low -- it cancelled the derivation from the font and
            clipped the text from 14 pt upwards -- and too high, because a
            760 px minimum plus the margins does not fit in a 640 px window
            and the card ended up clipped at the side.
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
        @note Twin of `_available_card_width`, and for the same reason: a long
            list must be able to lengthen the card, but not beyond the window
            hosting it, otherwise the bottom part -- buttons included -- would
            end up off screen.
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
        @note Measured on the REAL card (`sizeHint`), not on an estimate of the
            rows: the card also carries title, ring, bar, cost and buttons, and
            how much room is left to the status depends on all of them.
        """
        spare = self._available_card_height() - self.card.sizeHint().height()
        return self.status.minimumHeight() + max(0, spare)

    def _status_text_width(self):
        """@brief Width in pixels the status label really lays its text out in.

        @return Card width minus the card's own contents margins (they include
            the 1 px style-sheet border of the QFrame), the layout padding, and
            the label's contents margins and margin.
        @note CAUSE of the «last row cut in half» defect: before, `card width
            - 2 * _CARD_PADDING` was used, that is 2 px more than the real
            label (the card border). The list rows were shortened to THAT
            width, and `elidedText` fills the row up to the limit: a shortened
            row easily fell in the last 2 px, the real label wrapped it, and
            the list took one row more than the one measured and reserved.
            Where the window granted no further room, the label stayed lower
            than its text and the last row showed in half. The measure is now
            that of the space in which the text is really drawn.
        """
        # The style sheet applies border and font only on polishing: measuring
        # earlier would give the width of a card without border.
        self.card.ensurePolished()
        card = self.card.contentsMargins()
        padding = self.card.layout().contentsMargins()
        label = self.status.contentsMargins()
        return max(1, self.card.minimumWidth()
                   - card.left() - card.right()
                   - padding.left() - padding.right()
                   - label.left() - label.right() - 2 * self.status.margin())

    def _status_height(self):
        """Height in px the current status text takes at the label's real width."""
        return self.status.heightForWidth(self._status_text_width())

    def _size_the_card(self):
        """@brief Size the card on its content, not on the texts of one run.

        The card was too low and cramped: the status wrapped badly and the
        rows that appear mid-operation (tool, bar, cost) made it grow and
        shrink before one's eyes.

        Width: the status is truncated to `_STATUS_CHAR_LIMIT` characters, so
        the width needed is the one that spreads them over `_STATUS_LINES`
        rows, measured with the metrics of the effective font
        (`averageCharWidth`), not with a number of pixels chosen by eye on one
        sentence. The minimum `_CARD_MIN_WIDTH` keeps a very narrow font
        readable; the maximum is NOT a constant but the width the window
        really grants.

        Height: when the window ceiling bites, the wanted width is not there
        and the status rows are no longer three. They are COUNTED at the
        surviving width, measuring the extent of the worst possible sentence
        (`_WORST_CASE_STATUS`, wide letters) with the real metrics: it is the
        difference between a text that wraps and a text cut in the middle of a
        row. `_STATUS_LINES` remains the minimum, so the card does not lower
        when the font is tiny.

        The three optional rows keep their own room even when hidden, so the
        overall height is that of a card full from the start.

        @note The tool header shortens itself (ElidedLabel), so one row is
            enough for it at any font and it does not enter the count.
        """
        # Before reading the metrics: polishing can change the label font
        # (application style sheet), and measuring with the old font would
        # give lines and widths of another text.
        self.card.ensurePolished()
        metrics = self.status.fontMetrics()
        # One line of margin: `averageCharWidth` is an average, and with a
        # sentence of wide letters the truncated text would overflow onto the
        # next line, making the card grow. Sizing the width on two lines
        # (_STATUS_WRAP_LINES), the three reserved ones suffice even in the
        # worst case.
        chars_per_line = math.ceil(_STATUS_CHAR_LIMIT / _STATUS_WRAP_LINES)
        wanted = 2 * _CARD_PADDING + metrics.averageCharWidth() * chars_per_line
        width = min(self._available_card_width(), max(_CARD_MIN_WIDTH, wanted))
        self.card.setMinimumWidth(width)
        self.card.setMaximumWidth(width)
        # The lines are counted at the width at which the label really draws,
        # card border excluded (see `_status_text_width`).
        usable = self._status_text_width()
        wrapped = metrics.boundingRect(QRect(0, 0, usable, 0),
                                       int(Qt.TextFlag.TextWordWrap),
                                       _WORST_CASE_STATUS)
        lines = max(_STATUS_LINES,
                    math.ceil(wrapped.height() / max(1, metrics.lineSpacing())))
        self._status_base_height = lines * metrics.lineSpacing() + metrics.descent()
        # The list extra is added to the reserved lines: the default lines
        # stay the floor (a card that shrinks when the list ends is the same
        # jump as one that grows).
        self.status.setMinimumHeight(self._status_base_height + self._status_extra)
        self.tool.setMinimumHeight(self.tool.fontMetrics().lineSpacing())
        # One line per step (one below the other, user 2026-09-26), but only
        # when there are steps: single-phase operations do not pay for that
        # space (a low window does not have it).
        self.steps.setMinimumHeight(
            (len(self._step_labels) + len(getattr(self, '_step_results', {}))) * self.steps.fontMetrics().lineSpacing()
            + self.steps.fontMetrics().lineSpacing() // 2 if self._step_labels else 0)
        self.cost.setMinimumHeight(self.cost.fontMetrics().lineSpacing())
        self.phases.setMinimumHeight(0)
        # The elapsed time too: it starts empty and receives its first text
        # in `start`, AFTER this computation. An empty label and one with
        # text do not ask for the same height (14 and 12 px on a test
        # platform), so the first operation had a card 2 px different from
        # the following ones. A whole reserved line makes it text-independent.
        self.elapsed.setMinimumHeight(self.elapsed.fontMetrics().lineSpacing())
        # Space reserved even when the line is hidden: it keeps the card from
        # jumping when the tool, the bar or the cost appear with the operation
        # running. The logical visibility does not change (isVisibleTo stays
        # false).
        for row in (self.tool, self.progress_row, self.cost, self.phases):
            policy = row.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            row.setSizePolicy(policy)
        # The finished-phase lines take only the space the window has left
        # AFTER everything else, up to PHASE_HISTORY_ROWS: a low window shows
        # fewer (or none) instead of pushing the card out.
        # The rest is measured WITHOUT the list: with a run under way the
        # label already holds its lines and `card.sizeHint` counts them, so
        # subtracting them again removed them twice and every mid-run resize
        # shrank the list (HANDOFF 138, C2: 4 lines -> 1). Its minimum height
        # has just gone back to zero, so its contribution is its `sizeHint`,
        # which is subtracted.
        spare = (self._available_card_height() - self.card.sizeHint().height()
                 + self.phases.sizeHint().height())
        rows = 0
        if getattr(self, "_phases_reserved", False):
            # Height measured on the real text (table rows included), not
            # estimated: a short estimate made the card grow by a few pixels
            # at every phase.
            while rows < PHASE_HISTORY_ROWS and self._phase_rows_height(rows + 1) <= spare:
                rows += 1
        self._phase_rows = rows
        self.phases.setMinimumHeight(self._phase_rows_height(rows) if rows else 0)
        # The reserved lines may have changed (window resized, taller status
        # list): the list is redrawn BEFORE fixing the card height, so it
        # shows how many lines fit now and a text left from the old
        # measurement does not enter the `sizeHint`.
        self._render_phases()
        self.card.setMinimumHeight(self.card.sizeHint().height())

    def changeEvent(self, event):
        """@brief Re-derive the card size when the font or the style changes.

        @param event Qt change event; only font, style and application font
            changes trigger the refit.
        @note The computation lived only in `__init__`: a change of theme or
            of the accessibility font with the program running left the card
            sized on the old metrics, that is with clipped text -- precisely
            the defect the computation was meant to avoid.
        """
        super().changeEvent(event)
        if getattr(self, "card", None) is None:
            return
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange,
                            QEvent.Type.ApplicationFontChange):
            self._refit()

    def _refit(self):
        """@brief Re-derive the card AND re-lay the current status on new geometry.

        @note A list on the card is shortened and measured for a precise width
            and font. Resizing only the card left the rows shortened for the
            old geometry: on a narrower card they wrapped (or overflowed) and
            the last row ended up outside the reserved height.
        @note The height supplement, monotonic between one message and the
            next, is RE-DERIVED from zero here: it was measured on a geometry
            that no longer exists, and keeping it would leave the card taller
            than the window just shrunk. The jump the monotonicity avoids is
            the one between two messages; a resize already changes the card.
        @note The phase rows are re-derived here too (`_size_the_card` redraws
            them): a window enlarged mid-run shows rows that did not fit at
            start, a shrunk one removes some.
        """
        self._status_extra = 0
        self._size_the_card()
        if getattr(self, "_status_source", ""):
            self._render_status()

    def eventFilter(self, watched, event):
        """@brief Keep the overlay covering its parent and sized on its metrics.

        @param watched Object the event is for: the parent widget (resize) or
            the status label (font change).
        @param event Qt event being filtered.
        @return The base class result: the event is never consumed here.
        @note The width ceiling depends on the window, so it is recomputed on
            resize: shrinking the window, a card sized for the old width would
            stay clipped at the side.
        @note The font change is intercepted ON the status label, not on the
            overlay: the style sheet of this card fixes the font of its own
            children, so a new system font reaches the label (when the theme
            is reapplied) without passing through here. Sizing and measured
            metrics must remain the same thing.
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
        @note Disabled AND hidden together, by the same discipline as the
            other commands of this program: an enabled button on no job is a
            promise that cannot be kept, and a visible but disabled one takes
            the place without saying why.
        @note Whoever starts the job must declare it (`start(..., abortable=True)`):
            the default is False, so a new path that has not thought about
            interruption does not offer a button that would do nothing.
        """
        self._abortable = bool(abortable)
        self.abort_button.setEnabled(self._abortable)
        self.abort_button.setVisible(self._abortable)

    def is_abortable(self):
        """@brief Tell whether the card is currently offering the abort action.

        @return True while `set_abortable(True)` is in force.
        """
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
        # New operation, new footprint: the extra allowed to the previous
        # run's list is reset HERE and nowhere else. The label is emptied
        # first because its old text, if it was a list, would keep high the
        # `sizeHint` the card resizes on.
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
        # The label is `i18n_skip` (it carries a detail that is not our own
        # text): the tool name is translated here, before composing it. The
        # phase list instead keeps the source (see `_phase_history`).
        name = self._translate(source_name) if source_name else ""
        if key != self._phase_key:
            # Another tool: the previous phase is finished (see _end_phase).
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
        # A stepped job ticks its steps: the phase list is not needed and
        # its space goes back to the status (the resize is just below).
        self._phases_reserved = False
        self.clear_phases()
        self._step_labels = [translate(str(label)) for label in labels]
        self._step_results = {}
        self._step_unavailable = {int(index): translate(str(reason))
                                  for index, reason in (unavailable or {}).items()}
        self._step_current = -1
        self._render_steps()
        # Called right after `start`, before the first paint: the card takes
        # now the height it will keep for the whole generation.
        self._size_the_card()

    def set_step_result(self, index, result):
        """@brief Keep a phase result visible while subsequent steps run.

        @param index Position in the list given to `set_steps`; out of range,
            or no steps shown, is ignored rather than raising.
        @param result Outcome text shown under that step's label.
        """
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
        """@brief Tell whether the card is open on a finished job, waiting for «Chiudi».

        @return True while the overlay is shown with the close button visible.
        """
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
            # With a real count the row says how far along it is (HANDOFF
            # 156): «17 di 42 componenti» (17 of 42 components) instead of an
            # «in corso» (running) that lasts minutes.
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
        """@brief The step row as plain text, marks included (for logs and tests).

        @return The text of the rich document the label draws, one line per
            step; "" when no steps are shown.
        """
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
        # One below the other (user, 2026-09-26): in a row they wrapped in
        # the middle of a step. A column centred as a block, lines aligned
        # left, so the marks ✓ ● ○ stand in a column.
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
        """«17 di 42 componenti» for the running phase, in the current language.

        @return The formatted count; the untranslated template when the
            translated one cannot be formatted.
        """
        done, total, unit = self._phase_progress
        template = self._translate("{done} di {total} {unit}")
        try:
            return template.format(done=done, total=total, unit=unit).rstrip()
        except (KeyError, IndexError, ValueError):
            return f"{done} di {total} {unit}".rstrip()

    def clear_cost(self):
        """@brief Clear and hide the AI cost summary at the start of a new AI analysis.

        Resets the summary at the start of a NEW AI analysis.

        Not in start(): the modal restarts for the FreeCAD generation too,
        which makes no calls to the model. Resetting there made the cost
        disappear a moment after showing it.
        """
        self.cost.clear()
        self.cost.hide()

    def set_cost(self, text):
        """@brief Show the AI analysis cost summary inside the overlay; an empty text hides it.

        Shows the AI analysis cost summary inside the modal.

        The cost of the last analysis no longer has a label under the button
        (user, 2026-09-24): it stays here, in the modal being looked at, and
        in the AI log.

        @param text Cost summary, already formatted; "" or None hides the row.
        """
        self.cost.setText(str(text or ""))
        self.cost.setToolTip(self.cost.text())
        self.cost.setVisible(bool(text))

    def set_status(self, message):
        """@brief Show one status written for a person, never a service line.

        @param message Status line; machine bookkeeping is dropped, not shown.
            One line in, one line shown -- unless the line enumerates, and then
            it is laid out as a list (see the last note).
        @note The lines the program writes for itself (AI_CALL_... at the head
            of the line, "key=value" records with sha256 fingerprints) and
            those external tools write on their own stdout ("key : value"
            fields, entity counts, decorations, version and copyright
            headers, self-dated lines, percentages without a subject) stay in
            the log and do not arrive here: on the card they took the place of
            what was happening without saying it. The log button stays one
            click away.
        @note A path does NOT make the line dropped: it is shortened to the
            file name (see elide_paths). The product of this tool is files, so
            «Inventario salvato: …» and «Inventario non salvato su disco:
            [Errno 28] …» are outcomes, not internal bookkeeping.
        @note When a line is dropped the last readable status *stays* written:
            clearing the label would make the operation look stuck, which is
            the defect this filter must avoid, not cause.
        @note A line that ENUMERATES (lead-in with a colon and items separated
            by the middle dot: the documents sent to the model, the reasons of
            a partial inventory) is laid out one item per row, and the card
            lengthens to hold them. The line going to the log stays one, with
            one timestamp: the split between the log and the card happens
            here, not at the source (see `enumeration_lines`).
        @note In a list, the item declaring a term («name — role», see
            `enumeration_term`) shows the term in bold, detached from the
            description. It is the only case in which the label switches to
            rich text, and all the text coming from the message is escaped: a
            file name cannot become markup. Every other line stays
            `PlainText`, literal as it was.
        """
        # Full messages stay in the log; keep the card stable for long replies.
        warned = kind_of(message) == WARNING
        source = " ".join(str(message).split())
        if is_machine_line(source):
            return
        text = self.i18n.text(source) if self.i18n is not None else source
        self._status_source = elide_paths(text)
        if self._phase_name:
            # Phase outcome: the FIRST status after its announcement; a
            # warning (the last one) replaces it, because it is what must be
            # looked at. The SOURCE is kept: translation happens when the
            # list is drawn (`_phase_outcome_text`), even after a language
            # change.
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
        """Lay out the kept status: a list when it enumerates and fits, one line otherwise."""
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
        @note Every row is shortened to the width in which the label REALLY
            draws (`_status_text_width`, card border excluded) instead of
            being wrapped, and the markup forbids wrapping: so an item takes
            one row and one only, the row count is exact and the file name
            stays aligned under the previous one, which is what the user
            asked for. The full text is in the log.
        @note The height is measured on the document the label draws
            (`heightForWidth` of the rich text), not on `lineSpacing` times
            the rows: line spacing, bold and fallback fonts are already inside
            the measure, because measure and drawing are the same layout.
        @note If the items do not fit in height the list is CUT, not left to
            overflow: the last items are removed and the cut is declared with
            an ellipsis on a row of its own -- the same sign with which this
            card declares every other shortening. Better a declaredly partial
            list than a card taller than the window, with the buttons off
            screen.
        @note The list is left-aligned: centred, the file names would start
            from different columns on every row, that is exactly the defect
            («più chiaro») the list must remove. Non-list lines stay centred
            as before.
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
            # The first pruning replaces the last item with the ellipsis (same
            # number of lines, one item fewer); from the second on the
            # ellipsis is removed too, so every round loses a line and the
            # loop always ends, at worst on the two lines of the limit.
            del shown[-2 if shown[-1] == ellipsis else -1:]
            shown.append(ellipsis)
            self.status.setText(_enumeration_markup(shown))
        self._reserve_status_height(self._status_height())
        return True

    def _reserve_status_height(self, needed):
        """@brief Grow the reserved status height so a list is never clipped.

        @param needed Height in pixels the status text occupies right now.
        @note Only upwards and only within a run: `start()` brings the
            supplement back to zero. A card that rises and lowers at every
            message is more annoying than a slightly taller card, which is
            the same reason the height is reserved by default.
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

        @note It also removes the abort action: the job is over (on its own or
            because it was stopped), so from here on there is nothing left to
            stop. `hide()` alone is not enough -- the card is hidden also with
            the operation in progress, during the review of the bodies, and
            there the command must stay valid.
        """
        self.timer.stop()
        self.set_progress(None, None)
        self.set_abortable(False)
        self.hide()
        self.operation_finished.emit()

    def hideEvent(self, event):
        """@brief Stop the animation whenever the overlay goes away.

        @param event Qt hide event.
        @note Records when it went away: the overlay is hidden mid-operation
            while the reviewer edits the detected bodies, and the clock must
            not count that time as waiting for the model.
        """
        self.timer.stop()
        self._hidden_at = monotonic()
        super().hideEvent(event)

    def showEvent(self, event):
        """@brief Resume the animation when the overlay comes back mid-operation.

        @param event Qt show event.
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
