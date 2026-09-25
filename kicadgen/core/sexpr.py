"""@brief Write-only S-expression tree and serializer that follows KiCad file formatting.

Serializzatore generico di S-expression nel formato usato da KiCad.

KiCad salva footprint (.kicad_mod), simboli e PCB come S-expression
(liste annidate stile Lisp). Questo modulo fornisce un piccolo albero
di nodi (SExpr) e un serializzatore che replica lo stile di formattazione
di KiCad: indentazione a tab, una entry per riga per i nodi "strutturali",
inline per le liste di soli valori semplici (es. coordinate).

Non e' un parser: serve solo per la scrittura (generazione) dei file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Union

class Symbol(str):
    """@brief Bare (unquoted) KiCad keyword token such as yes, no or thru_hole.

    Token bare (non quotato), es. yes/no/solid/thru_hole.

    KiCad distingue le stringhe vere (sempre tra virgolette, es. nomi layer
    o testi) dai simboli/keyword (mai quotati, es. `yes`, `no`, `solid`,
    `thru_hole`). Un normale `str` viene sempre quotato da questo
    serializzatore; usare `Symbol("yes")` quando serve un token bare.
    """

    __slots__ = ()


YES = Symbol("yes")
NO = Symbol("no")


def bare(value: bool) -> Symbol:
    """@brief Convert a Python bool into the bare yes/no symbol expected by KiCad.

    Converte un bool Python nel simbolo bare yes/no atteso da KiCad.
    """
    return YES if value else NO


# Un token puo' essere: un altro SExpr, oppure un valore atomico
# (str/int/float/bool/Symbol) che viene renderizzato come token o stringa quotata.
Atom = Union[str, int, float, bool, Symbol]
Token = Union["SExpr", Atom]


def _format_number(value: float) -> str:
    """@brief Format a number the way KiCad does: no useless trailing zeros.

    @param value Finite int or float, in the file's unit (mm, degrees...).
    @return Text with at most 6 decimals; negative zero (including values
        that round to it, e.g. -1e-7) is written as "0", never "-0".
    @throws ValueError When the value is NaN or infinite: KiCad cannot read
        "nan"/"inf" and would refuse the whole file, so the error is raised
        where the bad number is produced instead.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not math.isfinite(value):
        raise ValueError(f"cannot write a non-finite number to a KiCad file: {value!r}")
    # arrotonda per evitare rumore tipo 1.2699999999999
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    if text in ("", "-0", "-"):
        return "0"
    return text


#: Control characters KiCad escapes inside quoted strings (its OUTPUTFORMATTER
#: writes them as backslash sequences and its lexer reads them back). A raw
#: newline inside a quoted string makes KiCad refuse the file.
_STRING_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _quote(text: str) -> str:
    """Quote a string for a KiCad S-expression, escaping what KiCad escapes."""
    return '"' + "".join(_STRING_ESCAPES.get(ch, ch) for ch in text) + '"'


def _format_atom(value: Atom) -> str:
    if isinstance(value, Symbol):
        return str(value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return _format_number(value)
    if isinstance(value, int):
        return str(value)
    # stringa: quotata sempre tra virgolette doppie, come fa KiCad
    return _quote(str(value))


def _is_simple(token: Token) -> bool:
    """True se il token puo' stare inline (non e' un SExpr con figli complessi)."""
    return not isinstance(token, SExpr)


@dataclass
class SExpr:
    """@brief S-expression node holding a name and its atom or child arguments.

    Nodo S-expression: (name arg1 arg2 (child1 ...) (child2 ...) ...).
    """

    name: str
    args: list = field(default_factory=list)

    def add(self, *args: Token) -> "SExpr":
        """@brief Append arguments or children to the node and return self for chaining.

        Aggiunge argomenti/figli al nodo, ritorna self per chaining.
        """
        self.args.extend(args)
        return self

    def child(self, name: str, *args: Token) -> "SExpr":
        """@brief Create a child node, append it and return it.

        Crea un figlio SExpr(name, args), lo appende e lo ritorna.
        """
        node = SExpr(name, list(args))
        self.args.append(node)
        return node

    def _inline(self) -> str:
        """Rende il nodo su una sola riga (usato per coordinate/atomi semplici)."""
        parts = [self.name] + [
            arg._inline() if isinstance(arg, SExpr) else _format_atom(arg)
            for arg in self.args
        ]
        return "(" + " ".join(parts) + ")"

    def render(self, indent: int = 0) -> str:
        """@brief Render the node on multiple tab-indented lines in KiCad style.

        Rende il nodo multi-riga con indentazione a tab, stile KiCad.
        """
        pad = "\t" * indent
        # se tutti gli argomenti sono atomici, resta su una riga
        if all(_is_simple(a) for a in self.args):
            return pad + self._inline()

        # Gli atomi che precedono il primo figlio restano sulla riga di testa;
        # tutto il resto segue nell'ordine originale (un atomo dopo un figlio
        # va su una riga propria, non spostato in testa: l'ordine conta).
        first_child = next(i for i, a in enumerate(self.args) if isinstance(a, SExpr))
        head = " ".join([self.name] + [_format_atom(a) for a in self.args[:first_child]])
        lines = [f"{pad}({head}"]
        for arg in self.args[first_child:]:
            if isinstance(arg, SExpr):
                lines.append(arg.render(indent + 1))
            else:
                lines.append(pad + "\t" + _format_atom(arg))
        lines.append(f"{pad})")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.render(0)


def sx(name: str, *args: Token) -> SExpr:
    """@brief Shortcut that creates an SExpr from a name and arguments.

    Scorciatoia per creare un SExpr.
    """
    return SExpr(name, list(args))
