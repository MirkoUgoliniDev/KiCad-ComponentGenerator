"""@brief Write-only S-expression tree and serializer that follows KiCad file formatting.

KiCad saves footprints (.kicad_mod), symbols and PCBs as S-expressions
(Lisp-style nested lists). This module provides a small node tree (SExpr)
and a serializer that replicates KiCad's formatting style: tab
indentation, one entry on each line for "structural" nodes, inline
rendering for lists made only of simple values (e.g. coordinates).

It is not a parser: it only serves the writing (generation) of files.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Union

class Symbol(str):
    """@brief Bare (unquoted) KiCad keyword token such as yes, no or thru_hole.

    KiCad distinguishes real strings (always between double quotes, e.g.
    layer names or texts) from symbols/keywords (never quoted, e.g. `yes`,
    `no`, `solid`, `thru_hole`). A plain `str` is always quoted by this
    serializer; use `Symbol("yes")` whenever a bare token is needed.
    """

    __slots__ = ()


YES = Symbol("yes")
NO = Symbol("no")


def bare(value: bool) -> Symbol:
    """@brief Convert a Python bool into the bare yes/no symbol expected by KiCad.

    @param value The flag to write.
    @return YES for True, NO for False.
    """
    return YES if value else NO


# A token can be: another SExpr, or an atomic value
# (str/int/float/bool/Symbol) rendered as a token or as a quoted string.
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
    # round to avoid noise such as 1.2699999999999
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
    # string: always quoted in double quotes, as KiCad does
    return _quote(str(value))


def _is_simple(token: Token) -> bool:
    """True when the token can stay inline (it is not an SExpr with its own children)."""
    return not isinstance(token, SExpr)


@dataclass
class SExpr:
    """@brief S-expression node holding a name and its atom or child arguments.

    Written as (name arg1 arg2 (child1 ...) (child2 ...) ...): atoms and
    child nodes keep the order in which they were added.
    """

    name: str
    args: list = field(default_factory=list)

    def add(self, *args: Token) -> "SExpr":
        """@brief Append arguments or children to the node and return self for chaining.

        @param args Atoms (str, int, float, bool, Symbol) or SExpr children,
            appended in the given order.
        @return This node.
        """
        self.args.extend(args)
        return self

    def child(self, name: str, *args: Token) -> "SExpr":
        """@brief Create a child node, append it and return it.

        @param name Name of the new child node.
        @param args Initial atoms or children of the new node.
        @return The new child SExpr(name, args), already appended to this node.
        """
        node = SExpr(name, list(args))
        self.args.append(node)
        return node

    def _inline(self) -> str:
        """Render the node on a single line (used for coordinates and simple atoms)."""
        parts = [self.name] + [
            arg._inline() if isinstance(arg, SExpr) else _format_atom(arg)
            for arg in self.args
        ]
        return "(" + " ".join(parts) + ")"

    def render(self, indent: int = 0) -> str:
        """@brief Render the node on multiple tab-indented lines in KiCad style.

        @param indent Nesting depth: number of leading tabs of the first line.
        @return The node text without a trailing newline: a single line when
            every argument is atomic, otherwise the head atoms on the first
            line, each child or later atom on its own indented line and the
            closing parenthesis on the last.
        """
        pad = "\t" * indent
        # if every argument is atomic, it stays on one line
        if all(_is_simple(a) for a in self.args):
            return pad + self._inline()

        # The atoms that precede the first child stay on the head line;
        # everything else follows in the original order (an atom after a child
        # goes on its own line, not moved to the head: the order matters).
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

    @param name Node name.
    @param args Atoms or child nodes, in order.
    @return The new SExpr(name, list(args)).
    """
    return SExpr(name, list(args))
