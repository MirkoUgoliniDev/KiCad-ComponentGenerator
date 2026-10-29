"""@brief Geometry of KiCad's stroke-font texts, shared by the symbol and footprint writers.

KiCad draws properties and texts with its own stroke font (newstroke), not
with the editor's desktop font. Converting a text from KiCad's default
centred justification to ``(justify left bottom)`` without moving it IN
KICAD therefore needs KiCad's own advances and centring rules, collected
here from kicad-cli 10.0 SVG exports (read-only, 2026-10-16):

- ``sym export svg`` (schematic symbols, default pen) and ``fp export svg``
  (footprint fp_text/property texts, explicit thickness) give the SAME
  per-character advances, each an exact multiple of 1/21 of the size
  (250 characters re-checked on footprint texts at 1.0 mm / 0.15 mm, all
  within 0.0005 mm of the table).
- The width KiCad centres a one-line text on is
  ``size x sum(advances) / 21 + margin`` with ``margin = 25/19 x pen``,
  where ``pen`` is the stroke thickness clamped to 0.25 x size. Footprint
  measurements, pen -> margin (mm): 0.075 -> 0.0987 (size 0.5),
  0.1 -> 0.1316, 0.12 -> 0.1579 (size 0.8), 0.15 -> 0.1974 (size 1.0 and
  1.27), 0.225 -> 0.2961 (size 1.5), 0.3 -> 0.3947 (size 2.0), and 0.3 at
  size 1.0 -> 0.3289 = 25/19 x 0.25 (clamped); every value within
  0.0001 mm of the formula. Symbol properties use the schematic default pen
  0.1524 mm (6 mil): 25/19 x 0.1524 = 0.2005, measured 0.2002-0.2006 at
  0.635, 1.27 and 2.54 mm.
- The anchor of a bottom-justified text lies 0.585 x size below the anchor
  of the same text vertically centred (symbols and footprints, every size
  and thickness measured): towards -y in a symbol (y up), +y in a
  footprint (y down).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

#: Advances of KiCad's stroke font (newstroke), in 1/21 of the text size,
#: grouped by advance. Measured with kicad-cli 10.0 `sym export svg` on
#: «|c|» against «||» for every character listed and re-checked with
#: `fp export svg` (2026-10-16): every advance is an exact multiple of 1/21
#: within 0.003.
KICAD_ADVANCE_GROUPS = {
    0: '\xad',
    8: '`\xb4',
    10: "!',.:;Iij\xa1\xa6\xb8\xcc\xcd\xce\xcf\xec\xedΙ",
    11: 'lι',
    12: '^ft\xa8\xaf\xee\xef',
    13: 'r',
    14: '()[\\]{}\xba',
    15: '~\xaa',
    16: ' "*JT_vy\xb0\xb2\xb3\xb7\xb9\xfd\xffΤγλ',
    17: 'Lksxzεκνχ',
    18: '?AFVYce\xa2\xa7\xbf\xc0\xc1\xc2\xc3\xc4\xc5\xdd\xe7\xe8\xe9\xea\xebΑΓΔΛΥ'
        'ζξς',
    19: 'Eabdghnopqu\xa3\xc8\xc9\xca\xcb\xe0\xe1\xe2\xe3\xe4\xe5\xf0\xf1\xf2\xf3\xf4\xf5\xf6\xf8\xf9\xfa\xfb\xfc'
        '\xfeΕΞΣβδηθου',
    20: '$0123456789SXZ|\xab\xbbΖΧρστ',
    21: '#BCDGKPR\xa4\xc7\xd0\xdeΒΚΡα€',
    22: '/HNOQUw\xb5\xbc\xbd\xbe\xd1\xd2\xd3\xd4\xd5\xd6\xd8\xd9\xda\xdb\xdcΗΝΟΠΦΨ'
        'μφψω',
    23: '\xb6\xdfπ',
    24: '%MW\xa5ΘΜΩΩ',
    26: '&+-<=>\xac\xb1\xc6\xd7\xf7≤≥←→↑↓∞−•',
    27: '@',
    28: 'm\xe6',
    30: '…',
    36: '\xa9\xae',
}
#: {character: advance in 1/21 of the size} of `KICAD_ADVANCE_GROUPS`.
KICAD_ADVANCE_UNITS = {char: units for units, chars in KICAD_ADVANCE_GROUPS.items() for char in chars}
#: Advance assumed for a character outside the measured table, 1/21 of the
#: size. HEURISTIC: one full size, about a capital's advance; a CJK or rare
#: symbol may differ, which shifts only the converted corner of such a text.
#: Measuring the missing characters with kicad-cli would make it superfluous.
KICAD_ADVANCE_FALLBACK_UNITS = 21
#: Ratio of the constant part of the centring width to the stroke pen
#: (measured 25/19 on footprint texts at pens 0.075-0.25 mm, see the module
#: docstring; the symbol margin 0.2005 mm at the 0.1524 mm pen agrees).
KICAD_PEN_MARGIN_RATIO = 25 / 19
#: Largest pen KiCad draws a text with, as a fraction of its size (measured:
#: a 0.3 mm thickness at size 1.0 centres exactly like 0.25 mm).
KICAD_PEN_CLAMP_RATIO = 0.25
#: Pen of a schematic text written without a thickness, mm (KiCad's default
#: line width, 6 mil): the symbol properties `serialize_symbol` writes.
SCHEMATIC_DEFAULT_PEN_MM = 0.1524
#: Distance, as a fraction of the size, from the anchor of a vertically
#: centred KiCad text to the anchor of the same text drawn bottom-justified
#: (measured 0.585 x size for symbols at 0.635-2.54 mm and for footprints
#: at 0.5-2.0 mm with pens 0.075-0.3 mm, any text).
KICAD_CENTRE_TO_BOTTOM = 0.585


def kicad_text_margin(size: float, thickness: float = SCHEMATIC_DEFAULT_PEN_MM) -> float:
    """@brief Constant part of the width KiCad centres a text on, mm.

    @param size Font size, mm.
    @param thickness Stroke thickness written for the text, mm (the
        schematic default pen when the file writes none).
    @return 25/19 x the pen KiCad draws with (thickness clamped to 0.25 x size).
    """
    return KICAD_PEN_MARGIN_RATIO * min(float(thickness), KICAD_PEN_CLAMP_RATIO * float(size))


# ── Markup: overbar ~{...}, superscript ^{...}, subscript _{...} ─────────
# KiCad 10 parses every drawn text with the PEGTL grammar of
# include/markup_parser.h and draws the tree in KIFONT::FONT::drawMarkup
# (common/font/font.cpp). Rules reproduced here:
# - a markup is «~{», «^{» or «_{» followed by text and nested markups up to
#   its closing «}»; inside it «{identifier}» is kept as literal text and any
#   other character but «}» is literal. Without a closing «}» it is not a
#   markup and every character, «~{» included, is drawn literally; a lone
#   «~», «^», «_», «{» or «}» is literal too (KiCad 10 has no «~» toggle);
# - outside markups, «http://» or «https://» starts a URL that runs to the
#   next space and is drawn literally (no markup inside it);
# - empty markups («~{}», «^{}», «_{}») are discarded: no bar, no shift;
# - styles nest as flags: subscript wins over superscript at any depth, an
#   overbar inside a superscript keeps the superscript;
# - sub/superscript glyphs are 0.8 x size (advance included) and their
#   cursor moves 0.15 x 0.8 x size down / 0.35 x 0.8 x size up
#   (include/font/stroke_font.h: m_superSubSize 0.8,
#   m_subscriptBaselineOffset 0.15, m_superscriptBaselineOffset -0.35);
# - an overbar is a line drawn with the text pen from 0.1 x size after the
#   cursor where its span starts to 0.1 x size before the cursor where it
#   ends (font.cpp barTrim), 1.23 x size above the cursor line
#   (include/font/font_metrics.h m_OverbarHeight), whatever the style of
#   its content.
# Measured with kicad-cli 10.0 `sym export svg` and `fp export svg`
# (2026-10-17) on «H» around the markup at sizes 1.0 and 2.0, pens 0.05,
# 0.1524, 0.15 and 0.25 mm: the cursor line lies 1/21 x size above the
# baseline, so a superscript baseline rises 0.28952 x size, a subscript one
# drops 0.11048 x size and the bar lies 1.27762 x size above the baseline
# (measured 0.2896, 0.1104 and 1.2776 x size, within 0.0001 mm); the bar
# starts 0.1001 x size after the first cell of its span and ends
# 0.0999 x size before the last; the justification box is the summed
# advances (scripts at 0.8) plus the usual pen margin, and the vertical
# justification does not depend on the markup.

#: Size of superscript and subscript glyphs, as a fraction of the text size.
KICAD_SUPER_SUB_SIZE = 0.8
#: Cursor shift of a superscript, as a fraction of its own (0.8) glyph size,
#: towards the glyph tops (KiCad's -0.35 in a y-down frame).
KICAD_SUPERSCRIPT_CURSOR_SHIFT = 0.35
#: Cursor shift of a subscript, as a fraction of its own glyph size, towards
#: the glyph bottoms.
KICAD_SUBSCRIPT_CURSOR_SHIFT = 0.15
#: Height of KiCad's glyph cursor line above the baseline, as a fraction of
#: the glyph size (measured 1/21: see the markup notes above).
KICAD_CURSOR_ABOVE_BASELINE = 1 / 21
#: Height of the overbar above the cursor line, as a fraction of the size.
KICAD_OVERBAR_ABOVE_CURSOR = 1.23
#: Length cut from each end of an overbar, as a fraction of the size.
KICAD_OVERBAR_TRIM = 0.1

#: The replacements of KiCad's UnescapeString (common/string_utils.cpp).
KICAD_ESCAPES = {"dblquote": '"', "quote": "'", "lt": "<", "gt": ">", "backslash": "\\", "slash": "/",
                 "bar": "|", "comma": ",", "colon": ":", "space": " ", "dollar": "$", "tab": "\t",
                 "return": "\n", "brace": "{"}


def kicad_unescape(text: str) -> str:
    """@brief The text KiCad shows for a text or property written `text`: its «{token}» escapes replaced.

    @param text The string as written in the KiCad file.
    @return KiCad's UnescapeString of it (common/string_utils.cpp, 10.0):
        «{slash}», «{brace}» and the other `KICAD_ESCAPES` tokens replaced;
        a token right after «$», «~», «^» or «_» kept with its braces (it
        is a variable or a markup), unknown tokens kept, an unterminated
        «{» kept with the rest unescaped.
    @note Measured with kicad-cli 10.0: symbol texts and properties and
        footprint texts and properties are shown unescaped, pin names and
        numbers are NOT (a pin named «N{slash}A» shows the braces). The
        caller decides; `kicad_text_layout` and the width functions take
        the shown text.
    @note Iterative, with an explicit stack of brace levels: the former
        recursion per nested brace raised RecursionError at a few thousand
        nested «{» (review 2026-10-29, G6). Each level is the substring
        between a «{» and its closing brace (or the end of the enclosing
        level when unterminated), processed exactly as the recursive call
        on that substring was, so the output is unchanged; the closing
        braces are paired once for the whole text instead of rescanned at
        every level.
    """
    if len(text) <= 2:
        return text
    closing = _matching_braces(text)
    # A frame is one level: [index, stop, previous, out, suffix, unterminated]
    # where `suffix` is appended to the parent after this level's output
    # ("}" for a closed token, "" for an unterminated one, which also ends
    # the parent's scan).
    root: list = []
    stack = [[0, len(text), "", root, "", False]]
    while stack:
        frame = stack[-1]
        index, stop, previous, out = frame[0], frame[1], frame[2], frame[3]
        while index < stop:
            char = text[index]
            if char != "{":
                out.append(char)
                previous = char
                index += 1
                continue
            match = closing.get(index)
            unterminated = match is None or match >= stop
            end = stop if unterminated else match
            token_start = index + 1
            if not unterminated and previous not in ("$", "~", "^", "_") \
                    and text[token_start:end] in KICAD_ESCAPES:
                out.append(KICAD_ESCAPES[text[token_start:end]])
                # KiCad's `prev` is the last INPUT character: the closing brace.
                previous = "}"
                index = end + 1
                continue
            out.append("{")
            if end - token_start <= 2:
                # The early return of a short level: the token as written.
                out.append(text[token_start:end])
                if unterminated:
                    index = stop
                    break
                out.append("}")
                previous = "}"
                index = end + 1
                continue
            # Descend into the token; this level resumes after it.
            frame[0], frame[2] = (stop if unterminated else end + 1), "}"
            stack.append([token_start, end, "", [], "" if unterminated else "}", unterminated])
            break
        else:
            # Level finished: hand its text to the parent.
            stack.pop()
            if stack:
                stack[-1][3].append("".join(out) + frame[4])
            continue
        if stack[-1] is frame:
            # The scan stopped on an unterminated short token: level finished.
            stack.pop()
            if stack:
                stack[-1][3].append("".join(out) + frame[4])
    return "".join(root)


def _matching_braces(text: str) -> dict:
    """{index of a «{»: index of its closing «}»} for every brace closed in `text`.

    A «{» is closed by the first «}» after it at which the count of braces
    opened and closed since it returns to zero, the scan `kicad_unescape`
    performs; that position does not depend on the text before the «{», so
    one pass with a stack gives it for every level at once.
    """
    result = {}
    opened = []
    for index, char in enumerate(text):
        if char == "{":
            opened.append(index)
        elif char == "}" and opened:
            result[opened.pop()] = index
    return result


def _is_identifier_start(char: str) -> bool:
    """PEGTL identifier_first: ASCII letter or underscore."""
    return char == "_" or ("a" <= char <= "z") or ("A" <= char <= "Z")


def _is_identifier_other(char: str) -> bool:
    """PEGTL identifier_other: ASCII letter, digit or underscore."""
    return _is_identifier_start(char) or ("0" <= char <= "9")


def _escape_sequence_end(text: str, index: int) -> int:
    """End index of a «{identifier}» escape sequence starting at `index`, or -1."""
    if text[index:index + 1] != "{" or not _is_identifier_start(text[index + 1:index + 2]):
        return -1
    end = index + 2
    while end < len(text) and _is_identifier_other(text[end]):
        end += 1
    return end + 1 if text[end:end + 1] == "}" else -1


def _url_end(text: str, index: int) -> int:
    """End index of a URL (http:// or https://, then one or more non-spaces) starting at `index`, or -1."""
    for prefix in ("https://", "http://"):
        if text.startswith(prefix, index):
            end = index + len(prefix)
            while end < len(text) and text[end] != " ":
                end += 1
            return end if end > index + len(prefix) else -1
    return -1


def _opens_markup(text: str, index: int) -> bool:
    """True when a «~{», «^{» or «_{» starts at `index`."""
    return text[index:index + 1] in ("~", "^", "_") and text[index + 1:index + 2] == "{"


def _parse_markup(text: str, index: int, memo: dict):
    """(node, end) of the markup starting at `index`, or None: KiCad's `braces<ControlChar>` rule.

    A node is (kind, children) with kind «~», «^» or «_»; a child is a
    literal string or a nested node. Memoised per start index, since a
    failed markup is retried at every enclosing level.

    Iterative, with an explicit stack of open markups: the former recursion
    per nested markup raised RecursionError at a few thousand nested «~{»
    (review 2026-10-29, G6). A frame is [start, position, children,
    literal]. When a nested markup fails (it reaches the end of the text
    without its «}»), every enclosing one fails too and is memoised as
    such: the enclosing level would rescan the same characters, from the
    one after «~{», with the same steps and the same memo, and reach the
    end without a «}» as well (its own «~» and «{» are not one). The
    recursive version reached the same None after rescanning, so the
    result is unchanged and a deep failure costs one pass.
    """
    if index in memo:
        return memo[index]
    if not _opens_markup(text, index):
        memo[index] = None
        return None
    stack = [[index, index + 2, [], []]]
    while stack:
        frame = stack[-1]
        start, position, children, literal = frame
        while position < len(text):
            if text[position] == "}":
                break
            if position in memo:
                nested = memo[position]
            elif _opens_markup(text, position):
                break
            else:
                nested = memo[position] = None
            if nested is not None:
                if literal:
                    children.append("".join(literal))
                    literal = []
                    frame[3] = literal
                children.append(nested[0])
                position = nested[1]
                continue
            escape_end = _escape_sequence_end(text, position)
            if escape_end > 0:
                literal.append(text[position:escape_end])
                position = escape_end
                continue
            literal.append(text[position])
            position += 1
        frame[1] = position
        if position >= len(text):
            # Unclosed: this markup and every enclosing one fail (see above).
            for open_frame in stack:
                memo[open_frame[0]] = None
            return None
        if text[position] == "}":
            if literal:
                children.append("".join(literal))
            memo[start] = ((text[start], tuple(children)), position + 1)
            stack.pop()
            continue
        # A nested markup opens at `position`: descend, resume here after it.
        stack.append([position, position + 2, [], []])
    return memo[index]


def parse_kicad_markup(text: str) -> tuple:
    """@brief KiCad's markup tree of a shown text (include/markup_parser.h, 10.0).

    @param text The text as KiCad shows it (see `kicad_unescape`).
    @return Tuple of children: literal strings and (kind, children) nodes,
        kind «~» (overbar), «^» (superscript) or «_» (subscript). Unclosed
        markups and URLs are literal; empty markups are kept here and
        ignored by `kicad_text_layout`, as KiCad discards them.
    """
    memo: dict = {}
    children = []
    literal = []
    index = 0
    while index < len(text):
        markup = _parse_markup(text, index, memo)
        if markup is not None:
            if literal:
                children.append("".join(literal))
                literal = []
            children.append(markup[0])
            index = markup[1]
            continue
        url_end = _url_end(text, index)
        if url_end > 0:
            literal.append(text[index:url_end])
            index = url_end
            continue
        literal.append(text[index])
        index += 1
    if literal:
        children.append("".join(literal))
    return tuple(children)


@dataclass(frozen=True)
class KicadTextRun:
    """@brief A run of characters KiCad draws with one style.

    @note `start` and `advance` are in 1/21 of the text size along the
        reading direction, from the start of the first advance cell;
        `advance` already includes `scale`. `rise` is the baseline shift
        towards the glyph tops, as a fraction of the size.
    """
    text: str
    start: float
    advance: float
    scale: float = 1.0
    rise: float = 0.0


@dataclass(frozen=True)
class KicadOverbar:
    """@brief An overbar KiCad draws, as a line with the text pen.

    @note `start` and `end` in 1/21 of the size from the start of the first
        cell (already trimmed; `end` < `start` when the span is shorter
        than the trims, as KiCad then draws it reversed); `height` above
        the baseline as a fraction of the size.
    """
    start: float
    end: float
    height: float


@dataclass(frozen=True)
class KicadTextLayout:
    """@brief How KiCad lays out a one-line text: styled runs, overbars and total advance.

    @note `advance` in 1/21 of the size; an int for a text without
        sub/superscripts, so plain texts keep their exact sums.
    """
    runs: tuple
    overbars: tuple
    advance: float

    @property
    def visible_text(self) -> str:
        """@brief The characters KiCad draws, markup removed.

        @return The concatenated run texts.
        """
        return "".join(run.text for run in self.runs)

    @property
    def is_plain(self) -> bool:
        """@brief Whether the layout is one unstyled run without overbars.

        @return True when every character is drawn at full size on the baseline.
        """
        return not self.overbars and all(run.scale == 1.0 and run.rise == 0.0 for run in self.runs)


#: Baseline rise of a superscript, fraction of the size: its cursor shift
#: plus the cursor line scaled down with the glyph (0.28952).
KICAD_SUPERSCRIPT_RISE = (KICAD_SUPER_SUB_SIZE * KICAD_SUPERSCRIPT_CURSOR_SHIFT
                          + (1 - KICAD_SUPER_SUB_SIZE) * KICAD_CURSOR_ABOVE_BASELINE)
#: Baseline rise of a subscript, fraction of the size (-0.11048: it drops).
KICAD_SUBSCRIPT_RISE = (-KICAD_SUPER_SUB_SIZE * KICAD_SUBSCRIPT_CURSOR_SHIFT
                        + (1 - KICAD_SUPER_SUB_SIZE) * KICAD_CURSOR_ABOVE_BASELINE)
#: Height of an overbar above the baseline, fraction of the size (1.27762).
KICAD_OVERBAR_HEIGHT = KICAD_OVERBAR_ABOVE_CURSOR + KICAD_CURSOR_ABOVE_BASELINE


def _string_units(text: str) -> int:
    """Summed table advances of literal characters, 1/21 of the size."""
    return sum(KICAD_ADVANCE_UNITS.get(char, KICAD_ADVANCE_FALLBACK_UNITS) for char in text)


@lru_cache(maxsize=4096)
def kicad_text_layout(text: str) -> KicadTextLayout:
    """@brief KiCad's layout of a one-line shown text: styled runs, overbars and total advance.

    @param text The text as KiCad shows it (escapes already replaced where
        KiCad replaces them, see `kicad_unescape`).
    @return A `KicadTextLayout`; for a text without markup, one run equal
        to `text` and its plain advance sum.
    @note Walks `parse_kicad_markup` as KiCad's drawMarkup does: the cursor
        advances by each character's table advance (x 0.8 in a script);
        overbars span the cursor positions of their content.
    @note The walk is iterative (an explicit stack of open markups, each
        overbar appended when its markup closes, as the former recursive
        walk did): a deeply nested markup no longer raises RecursionError
        (review 2026-10-29, G6).
    """
    runs: list = []
    bars: list = []
    plain_units = 0
    script_units = 0

    def cursor() -> float:
        return plain_units + KICAD_SUPER_SUB_SIZE * script_units if script_units else plain_units

    # A frame is one open markup: [children, next child, script, kind, cursor at its start].
    stack = [[parse_kicad_markup(text), 0, None, None, 0.0]]
    while stack:
        frame = stack[-1]
        children, script = frame[0], frame[2]
        if frame[1] >= len(children):
            stack.pop()
            if frame[3] == "~":
                trim = KICAD_OVERBAR_TRIM * 21
                bars.append(KicadOverbar(frame[4] + trim, cursor() - trim, KICAD_OVERBAR_HEIGHT))
            continue
        child = children[frame[1]]
        frame[1] += 1
        if isinstance(child, str):
            units = _string_units(child)
            scale = KICAD_SUPER_SUB_SIZE if script else 1.0
            rise = {"^": KICAD_SUPERSCRIPT_RISE, "_": KICAD_SUBSCRIPT_RISE}.get(script, 0.0)
            start = cursor()
            if script:
                script_units += units
            else:
                plain_units += units
            previous = runs[-1] if runs else None
            if previous is not None and previous.scale == scale and previous.rise == rise:
                runs[-1] = KicadTextRun(previous.text + child, previous.start, cursor() - previous.start,
                                        scale, rise)
            else:
                runs.append(KicadTextRun(child, start, cursor() - start, scale, rise))
            continue
        kind, nested = child
        if not nested:
            # KiCad's parse tree discards empty markups.
            continue
        # Subscript wins over superscript whatever the nesting order.
        inner = "_" if "_" in (kind, script) else ("^" if "^" in (kind, script) else None)
        stack.append([nested, 0, inner, kind, cursor()])
    return KicadTextLayout(tuple(runs), tuple(bars), cursor())


def kicad_advance_units(text: str) -> float:
    """@brief Sum of the stroke-font advances of a one-line text, in 1/21 of the size.

    @param text The text as KiCad shows it.
    @return The summed advances as KiCad's cursor moves (`kicad_text_layout`):
        markup characters not counted, sub/superscript characters at 0.8; a
        character outside the table counts `KICAD_ADVANCE_FALLBACK_UNITS`
        (declared heuristic). An int for a text without sub/superscripts.
    """
    return kicad_text_layout(text).advance


def kicad_text_width(text: str, size: float, thickness: float = SCHEMATIC_DEFAULT_PEN_MM) -> float:
    """@brief Width KiCad centres a one-line text on, mm (stroke font).

    @param text The text as KiCad shows it.
    @param size Font size, mm.
    @param thickness Stroke thickness, mm; the default is the schematic pen.
    @return size x `kicad_advance_units` / 21 + `kicad_text_margin`.
    """
    return float(size) * kicad_advance_units(text) / 21 + kicad_text_margin(size, thickness)


def kicad_centre_to_corner(text: str, size: float, thickness: float = SCHEMATIC_DEFAULT_PEN_MM,
                           angle: int = 0, y_down: bool = False) -> tuple:
    """@brief Offset from a centred KiCad text's anchor to the bottom-left anchor drawing it in the same place.

    @param text The text as KiCad shows it.
    @param size Font size, mm.
    @param thickness Stroke thickness, mm.
    @param angle Text angle, degrees: 0 (reads +x) or 90 (reads up the
        page, glyph tops towards -x, as KiCad turns a text); other values
        are read modulo 180.
    @param y_down True for footprint coordinates (y grows down the page),
        False for symbol coordinates (y up).
    @return (dx, dy) mm, unrounded: half the width back along the reading
        direction and `KICAD_CENTRE_TO_BOTTOM` x size down in the glyph frame.
    """
    along = kicad_text_width(text, size, thickness) / 2
    down = KICAD_CENTRE_TO_BOTTOM * float(size)
    # "Down the page" is -y in a symbol and +y in a footprint.
    page_down = 1.0 if y_down else -1.0
    if int(angle) % 180 == 90:
        # Reading up the page: the glyph's "down" is +x and "back along the
        # reading direction" is down the page.
        return (down, page_down * along)
    return (-along, page_down * down)


def kicad_corner_from_centre(text: str, size: float, x: float, y: float, *,
                             thickness: float = SCHEMATIC_DEFAULT_PEN_MM, angle: int = 0,
                             y_down: bool = False) -> tuple:
    """@brief The ``(justify left bottom)`` anchor at which KiCad draws a text where it drew it centred at (x, y).

    @param text The text as KiCad shows it.
    @param size Font size, mm.
    @param x Centre x, mm.
    @param y Centre y, mm.
    @param thickness Stroke thickness, mm.
    @param angle Text angle, degrees (see `kicad_centre_to_corner`).
    @param y_down True for footprint coordinates, False for symbol ones.
    @return (x, y) of the anchor, mm, rounded to 0.1 um.
    """
    dx, dy = kicad_centre_to_corner(text, size, thickness, angle, y_down)
    return (round(float(x) + dx, 4) + 0.0, round(float(y) + dy, 4) + 0.0)


def kicad_centre_from_corner(text: str, size: float, x: float, y: float, *,
                             thickness: float = SCHEMATIC_DEFAULT_PEN_MM, angle: int = 0,
                             y_down: bool = False) -> tuple:
    """@brief Inverse of `kicad_corner_from_centre`: where KiCad would centre a bottom-left anchored text.

    @param text The text as KiCad shows it.
    @param size Font size, mm.
    @param x Anchor x, mm.
    @param y Anchor y, mm.
    @param thickness Stroke thickness, mm.
    @param angle Text angle, degrees.
    @param y_down True for footprint coordinates, False for symbol ones.
    @return (x, y) of the centred anchor, mm, unrounded.
    """
    dx, dy = kicad_centre_to_corner(text, size, thickness, angle, y_down)
    return (float(x) - dx, float(y) - dy)


#: Height of the baseline above the anchor of a bottom-justified KiCad text,
#: as a fraction of the size. Measured with kicad-cli 10.0 (2026-10-16) on
#: the stroke centreline of «H»: `sym export svg` 0.1304 mm at 1.0 mm and
#: 0.1635 mm at 1.27 mm (default pen), `fp export svg` 0.1250-0.1354 mm at
#: 1.0 mm with pens 0.05-0.25 mm, 0.2526-0.2604 mm at 2.0 mm, 0.0651 mm at
#: 0.5 mm. The pen-dependent residual above 0.125 x size is at most
#: 0.0104 mm in every measurement and is not modelled.
KICAD_BOTTOM_TO_BASELINE = 0.125


def kicad_advance_width(text: str, size: float) -> float:
    """@brief Summed advance cells of a one-line KiCad text, mm: the text width without the pen margin.

    @param text The text as KiCad shows it.
    @param size Font size, mm.
    @return size x `kicad_advance_units` / 21; `kicad_text_width` minus
        `kicad_text_margin`.
    """
    return float(size) * kicad_advance_units(text) / 21


def kicad_text_cells(text: str, size: float, thickness: float = SCHEMATIC_DEFAULT_PEN_MM,
                     justify: str = "left", vjustify: str = "bottom") -> tuple:
    """@brief Where KiCad lays the glyph cells of a text out, relative to its anchor.

    @param text The text as KiCad shows it.
    @param size Font size, mm.
    @param thickness Stroke thickness, mm (the schematic default pen when
        the file writes none).
    @param justify Horizontal justification: "left", "center" or "right"
        (anything else reads as "center", KiCad's default).
    @param vjustify Vertical justification: "bottom", "center" or "top"
        (anything else reads as "center").
    @return (left, baseline) mm in the text's own frame (x along the reading
        direction, y towards the glyph tops), from the anchor: where the
        first glyph's advance cell starts and the height of the baseline.
    @note Measured with kicad-cli 10.0 for every justification at 0 and 90
        degrees (2026-10-16): the box KiCad justifies on is
        `kicad_text_width` wide with half the pen margin before the first
        cell and half after the last (a left text's «H» stroke starts at
        margin/2 + 5/21 x size, its left bearing); the baseline is
        `KICAD_BOTTOM_TO_BASELINE` x size above a bottom anchor, and a
        centred (top) anchor lies `KICAD_CENTRE_TO_BOTTOM` x size (twice
        that) above the bottom anchor of the same text.
    """
    size = float(size)
    advance = kicad_advance_width(text, size)
    half_margin = kicad_text_margin(size, thickness) / 2
    if justify == "left":
        left = half_margin
    elif justify == "right":
        left = -half_margin - advance
    else:
        left = -advance / 2
    steps = {"bottom": 0, "top": 2}.get(vjustify, 1)
    baseline = (KICAD_BOTTOM_TO_BASELINE - steps * KICAD_CENTRE_TO_BOTTOM) * size
    return left, baseline


# ── Pin numbers ──────────────────────────────────────────────────────────
# KiCad 10 places a pin number in `PIN_LAYOUT_CACHE::getNumberBlockOffset`
# (eeschema/pin_layout_cache.cpp, branch 10.0): text offset
# MilsToIU(round(24 x m_TextOffsetRatio)) + PIN_TEXT_MARGIN (4 mil) + the
# number pen. Measured with kicad-cli 10.0 `sym export svg` (2026-10-16,
# library symbol, default settings): on the stroke centreline of «H» the
# number's baseline lies 0.4413 / 0.4860 / 0.5191 / 0.5472 / 0.6084 /
# 0.6745 mm above the pin line at sizes 0.635 / 1.0 / 1.27 / 1.5 / 2.0 /
# 2.54 mm, identical at pin angles 0, 90, 180 and 270 and with the names
# inside (pin_names offset 0.508) or outside (offset 0). A thickness written
# in the number's effects (0.05-0.5 mm) changes nothing: KiCad draws pin
# texts with its pin pen, 6 mil in a library export. The number is therefore
# a bottom-justified text anchored at the constant 14 mil below; its
# baseline is `KICAD_BOTTOM_TO_BASELINE` x size higher, like every other
# text (residual of the rule against the strokes: +0.0063 mm at 0.635,
# +0.0014 mm at 2.54, never more than 0.007 mm).

#: KiCad's pin text offset, mm: MilsToIU(round(24 x 0.15)) = 4 mil at the
#: default schematic text offset ratio 0.15 (KiCad's DEFAULT_TEXT_OFFSET_RATIO).
#: A schematic with another ratio in its settings would move the numbers.
KICAD_PIN_TEXT_OFFSET_MM = 0.1016
#: KiCad's PIN_TEXT_MARGIN, mm (4 mil).
KICAD_PIN_TEXT_MARGIN_MM = 0.1016
#: Pen KiCad draws pin lines and pin texts with in a library, mm (6 mil, the
#: schematic default line width; the file cannot change it per pin).
KICAD_PIN_PEN_MM = SCHEMATIC_DEFAULT_PEN_MM
#: Distance from a pin line to the bottom anchor of its number, mm:
#: offset + margin + pen = 14 mil = 0.3556 mm, independent of the size.
KICAD_PIN_NUMBER_ANCHOR_MM = KICAD_PIN_TEXT_OFFSET_MM + KICAD_PIN_TEXT_MARGIN_MM + KICAD_PIN_PEN_MM
#: Highest stroke centreline of a digit or capital above the baseline, as a
#: fraction of the size. Measured with kicad-cli 10.0 on «H0»-«H9» and
#: «HA»-«HZ» (2026-10-16): 1.0 (cap height) for every glyph except «4»,
#: whose apex reaches 22/21. Characters outside digits and capitals
#: (brackets, accents) were not measured and may reach higher.
KICAD_PIN_NUMBER_GLYPH_TOP = 22 / 21


def kicad_pin_number_baseline(size: float) -> float:
    """@brief Height of a pin number's baseline above its pin line, mm, as KiCad 10 draws it.

    @param size Pin number text size, mm.
    @return `KICAD_PIN_NUMBER_ANCHOR_MM` + `KICAD_BOTTOM_TO_BASELINE` x size
        (0.4350 mm at 0.635, 0.5144 mm at 1.27, 0.6731 mm at 2.54).
    @note Same for horizontal pins (number above the line) and vertical
        pins (number left of the line, reading up the page).
    """
    return KICAD_PIN_NUMBER_ANCHOR_MM + KICAD_BOTTOM_TO_BASELINE * float(size)


def kicad_pin_number_extent(size: float) -> float:
    """@brief Farthest point of a pin number's ink from its pin line, mm.

    @param size Pin number text size, mm.
    @return Baseline height + `KICAD_PIN_NUMBER_GLYPH_TOP` x size + half the
        pin pen: the top of the inked strokes of digits and capitals
        (1.9210 mm at 1.27, 3.3654 mm at 2.54).
    """
    size = float(size)
    return kicad_pin_number_baseline(size) + KICAD_PIN_NUMBER_GLYPH_TOP * size + KICAD_PIN_PEN_MM / 2
