"""Serializzatore generico di S-expression nel formato usato da KiCad.

KiCad salva footprint (.kicad_mod), simboli e PCB come S-expression
(liste annidate stile Lisp). Questo modulo fornisce un piccolo albero
di nodi (SExpr) e un serializzatore che replica lo stile di formattazione
di KiCad: indentazione a tab, una entry per riga per i nodi "strutturali",
inline per le liste di soli valori semplici (es. coordinate).

Non e' un parser: serve solo per la scrittura (generazione) dei file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Union

class Symbol(str):
    """Token bare (non quotato), es. yes/no/solid/thru_hole.

    KiCad distingue le stringhe vere (sempre tra virgolette, es. nomi layer
    o testi) dai simboli/keyword (mai quotati, es. `yes`, `no`, `solid`,
    `thru_hole`). Un normale `str` viene sempre quotato da questo
    serializzatore; usare `Symbol("yes")` quando serve un token bare.
    """

    __slots__ = ()


YES = Symbol("yes")
NO = Symbol("no")


def bare(value: bool) -> Symbol:
    """Converte un bool Python nel simbolo bare yes/no atteso da KiCad."""
    return YES if value else NO


# Un token puo' essere: un altro SExpr, oppure un valore atomico
# (str/int/float/bool/Symbol) che viene renderizzato come token o stringa quotata.
Atom = Union[str, int, float, bool, Symbol]
Token = Union["SExpr", Atom]


def _format_number(value: float) -> str:
    """Formatta un float come fa KiCad: niente zeri decimali inutili."""
    if isinstance(value, int):
        return str(value)
    # arrotonda per evitare rumore tipo 1.2699999999999
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


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
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _is_simple(token: Token) -> bool:
    """True se il token puo' stare inline (non e' un SExpr con figli complessi)."""
    return not isinstance(token, SExpr)


@dataclass
class SExpr:
    """Nodo S-expression: (name arg1 arg2 (child1 ...) (child2 ...) ...)."""

    name: str
    args: list = field(default_factory=list)

    def add(self, *args: Token) -> "SExpr":
        """Aggiunge argomenti/figli al nodo, ritorna self per chaining."""
        self.args.extend(args)
        return self

    def child(self, name: str, *args: Token) -> "SExpr":
        """Crea un figlio SExpr(name, args), lo appende e lo ritorna."""
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
        """Rende il nodo multi-riga con indentazione a tab, stile KiCad."""
        pad = "\t" * indent
        # se tutti gli argomenti sono atomici, resta su una riga
        if all(_is_simple(a) for a in self.args):
            return pad + self._inline()

        head_atoms = []
        children = []
        for arg in self.args:
            if isinstance(arg, SExpr):
                children.append(arg)
            else:
                head_atoms.append(arg)

        head = " ".join([self.name] + [_format_atom(a) for a in head_atoms])
        lines = [f"{pad}({head}"]
        for child in children:
            lines.append(child.render(indent + 1))
        lines.append(f"{pad})")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.render(0)


def sx(name: str, *args: Token) -> SExpr:
    """Scorciatoia per creare un SExpr."""
    return SExpr(name, list(args))
