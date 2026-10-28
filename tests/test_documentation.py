"""@brief Enforce the repository Doxygen-compatible documentation contract.

@note Scope of the contract (see .claude/CLAUDE.md, "Documentation standard"):
every Python file under the production package ``kicadgen/``, the launcher
``main.py``, the maintained command-line helpers in ``tools/`` and the
maintained ``examples/``. Tests are excluded. Inside that scope the module
docstring and the docstring of every *public* definition must start with
``@brief`` followed by a real description: the generated placeholder
"Provide the X operation for this workflow." is rejected. A definition is public when its name does not start with ``_``;
it is checked at module level, and inside a class only when the class itself
is public. Definitions nested inside functions are implementation details and
are not checked. Qt overrides such as ``paintEvent``, ``closeEvent`` or
``eventFilter`` are camelCase names without a leading underscore, so they are
public by the same rule and need no separate list. Definitions placed under
module- or class-level compound statements (``if``, ``for``, ``while``,
``match``, ``try``/``except*``, ``with``/``async with``) are checked as well,
by the ``@brief`` rule and by the tag rules alike. ``@brief`` must be followed
on the same line by text: an empty ``@brief`` or a longer word such as
``@briefing`` does not open a docstring.

Beyond ``@brief`` (HANDOFF, MEDIO 13 of the 2026-10-15 audit), a public
function must document every parameter with ``@param``, carry ``@return``
when it returns a value or is a generator that yields values (Doxygen has
no ``@yield`` command, so ``@return`` describes what is yielded), and
``@throws`` naming every exception class (or a parent of it) that escapes:
raised and caught by no enclosing ``try`` of the same function, or re-raised
by a bare ``raise`` inside an ``except`` handler; and every docstring,
private helpers and functions nested in functions included, must be written
in English. The language check is a stopword heuristic (see
``_ITALIAN_STOPWORDS`` and ``_ITALIAN_THRESHOLD``) applied after quoted text
is removed; it is deliberately crude and a proper language detector would
make it unnecessary.
"""
import ast
import io
import builtins
import re
import tokenize
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _documented_files():
    """Return the maintained source files covered by the documentation contract."""
    files = list((ROOT / 'kicadgen').rglob('*.py'))
    files += list((ROOT / 'examples').rglob('*.py'))
    files += list((ROOT / 'tools').glob('*.py'))
    files.append(ROOT / 'main.py')
    return sorted(path for path in files if '__pycache__' not in path.parts)


#: Generated placeholder briefs ("Provide the X operation for this workflow.")
#: say nothing about the definition, so they do not satisfy the contract.
_PLACEHOLDER_BRIEF = re.compile(r'for this workflow\.?\s*$')

#: ``@brief`` followed on the same line by text: rejects an empty ``@brief``
#: and a longer word that merely starts with it (``@briefing``).
_BRIEF_START = re.compile(r'@brief[ \t]+\S')


def _starts_with_brief(node):
    """Tell whether a docstring starts with a meaningful @brief line."""
    docstring = ast.get_docstring(node, clean=False)
    if not docstring or not _BRIEF_START.match(docstring.lstrip()):
        return False
    first_line = docstring.lstrip().splitlines()[0]
    return not _PLACEHOLDER_BRIEF.search(first_line)


_COMPOUND_TYPES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
                   ast.With, ast.AsyncWith) + tuple(
    getattr(ast, name) for name in ('TryStar',) if hasattr(ast, name))


def _compound_bodies(node):
    """Return the statements nested in a module- or class-level compound statement.

    Covers if/for/while with their ``else``, try and try/except* with handlers,
    ``else`` and ``finally``, with/async with and every ``case`` of a match;
    any other statement yields an empty list. Function and class bodies are
    walked by the callers, not here.
    """
    if hasattr(ast, 'Match') and isinstance(node, ast.Match):
        return [statement for case in node.cases for statement in case.body]
    if not isinstance(node, _COMPOUND_TYPES):
        return []
    nested = list(node.body) + list(getattr(node, 'orelse', []))
    nested += list(getattr(node, 'finalbody', []))
    for handler in getattr(node, 'handlers', []):
        nested += list(handler.body)
    return nested


def _public_definitions(body, prefix=''):
    """Yield (qualified name, node) for the public definitions in a block.

    Compound statements (see ``_compound_bodies``) are descended into because
    conditional definitions are still part of the public surface; function
    bodies are not.
    """
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith('_'):
                continue
            name = prefix + node.name
            yield name, node
            if isinstance(node, ast.ClassDef):
                yield from _public_definitions(node.body, name + '.')
        else:
            yield from _public_definitions(_compound_bodies(node), prefix)


#: Base classes whose ``run`` is the body of a worker thread (review
#: 2026-10-27, C4): the override is public API in all but name, so it needs
#: a ``@brief`` docstring even inside a private class.
_THREAD_BASES = frozenset({'QThread', 'QRunnable'})


def _mandatory_special_methods(body, prefix=''):
    """Yield (qualified name, node) for the methods documented regardless of privacy.

    Every ``__init__`` (its parameters are the class's constructor contract,
    public or not) and every ``run`` of a class deriving from QThread or
    QRunnable (see ``_THREAD_BASES``). Classes nested in classes and classes
    under compound statements are covered; function bodies are not.
    """
    for node in body:
        if isinstance(node, ast.ClassDef):
            name = prefix + node.name
            bases = {_terminal_name(base) for base in node.bases}
            for inner in node.body:
                if not isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if inner.name == '__init__' or (inner.name == 'run' and bases & _THREAD_BASES):
                    yield f'{name}.{inner.name}', inner
            yield from _mandatory_special_methods(node.body, name + '.')
        elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _mandatory_special_methods(_compound_bodies(node), prefix)


def undocumented_items(source, label):
    """@brief List the ``@brief`` violations of one source text.

    @param source Python source code to inspect.
    @param label File label used in the reported entries.
    @return Entries formatted as ``label:line:name``; ``<module>`` names the
            module docstring. An empty list means the source complies.
    @note Besides the public definitions, every ``__init__`` and every
          ``run`` of a QThread/QRunnable subclass must carry a ``@brief``
          (see ``_mandatory_special_methods``); private helpers other than
          these keep their exemption.
    """
    tree = ast.parse(source)
    failures = []
    if not _starts_with_brief(tree):
        failures.append(f'{label}:1:<module>')
    checked = set()
    for name, node in _public_definitions(tree.body):
        checked.add(id(node))
        if not _starts_with_brief(node):
            failures.append(f'{label}:{node.lineno}:{name}')
    for name, node in _mandatory_special_methods(tree.body):
        if id(node) not in checked and not _starts_with_brief(node):
            failures.append(f'{label}:{node.lineno}:{name}')
    return sorted(failures, key=lambda entry: int(entry.split(':')[1]))


#: Italian function words that do not occur as English words («per», «non»,
#: «con» and «crea» were dropped: they flagged English prose). The repository
#: writes accented words either with the accent or with an ASCII apostrophe
#: («e'», «piu'», «gia'»), so both spellings are listed; the lookarounds keep
#: an apostrophe form from matching inside a longer word or a quoted letter.
#: Most words match in any case, because the repository's Italian wrote
#: emphasis in capitals («UNA», «DEL»). Four short words collide with English
#: or code tokens and are matched case-sensitively instead: «il» only as
#: ``il``/``Il`` (not the abbreviation ``IL``), «uno» and «ecc» only in lower
#: case (not ``Arduino Uno``, not ``ECC`` memory), and «del» only as
#: ``del``/``Del`` and not as the Python statement, i.e. not followed by a
#: dotted or subscripted operand (``del self._cache``, ``del table[key]``).
#: 2026-10-27 (review of the GUI scope): the reviewers found Italian comments
#: made only of content words, which the function-word list could not see
#: («I testi lunghi vanno a capo», «Etichette leggibili», «Spenti su
#: richiesta: restano i fori», «Nessun dato: ripiego dichiarato», «valgono
#: 0»). A second alternation lists Italian verbs, adjectives and nouns that
#: are not English words (checked one by one against English vocabulary:
#: «vanno», «restano», «valgono», «espongono», «spenti», «leggibili»,
#: «ripiego», «richiesta», «nessun/nessuna/nessuno», «dichiarato/a»,
#: «ogni», «ancora», «sempre», «invece», «mentre», «altrimenti», «tutti»,
#: «tutte», «stesso/a/i/e», «valore/i», «misura/e», «letto/a», «scritto/a»,
#: «riga/righe», «corpo/i», «altezza», «larghezza», «lunghezza»,
#: «scheda», «testo/i», «lunghi», «capo», «fori», «dato/i», «errore/i»,
#: «messaggio/i», «servono», «usato/a/i/e», «fatto/a»; «foto» and «serve»
#: were measured and rejected: a sample file name «foto.png» in a usage
#: line and the English verb "serve");
#: and a third one matches an articulated preposition followed by an
#: article («su richiesta» is caught by the word, «a meta'», «per lato»,
#: «di un», «in una», «con le», «tra i» by the pair), a sequence that
#: English never produces. Measured on the working tree of 2026-10-27: the
#: additions flagged no English docstring or comment of the documented
#: files (see test_comment_language_rule_catches_content_word_italian for
#: the positive and negative samples).
_ITALIAN_STOPWORDS = re.compile(
    r"(?<![\w'])(?:(?i:della|delle|degli|dello|nella|nelle|negli|sulla|sulle"
    r"|viene|vengono|anche|senza|oppure|quando|perch[eé]|perche'|questo|questa"
    r"|questi|queste|restituisce|ritorna|calcola|sono|essere|gli|dei|una"
    r"|che|nel|nei|sul|sui|dal|dalla|dalle|alla|alle|agli"
    r"|e'|è|piu'|più|gia'|già|puo'|può|cosi'|così|cioe'|cioè|quindi|ovvero"
    r"|deve|devono|un|cui"
    r"|vanno|restano|valgono|espongono|spenti|spente|leggibil[ie]|ripiego"
    r"|richiest[ae]|nessun[aoi]?|dichiarat[aoie]|ogni|ancora|sempre|invece"
    r"|mentre|altrimenti|tutt[ie]|stess[aoie]|valor[ei]|misur[ae]|lett[aoie]"
    r"|scritt[aoie]|riga|righe|corp[oi]|altezz[ae]|larghezz[ae]|lunghezz[ae]"
    r"|sched[ae]|test[oi]|lunghi|capo|fori|dat[oi]|error[ei]|messagg[io]"
    r"|servono|usat[aoie]|fatt[aoie]"
    r"|(?:[adi]|da|in|su|per|con|tra|fra)\s+(?-i:il|lo|la|gli|le|un|uno|una)"
    r"|(?:a|da|di|in|su|per|con|tra|fra)\s+met[aà]'?)"
    r"|[iI]l|uno|ecc|[dD]el(?!\s+(?:key\b|\w+(?:\.\w|\[))))(?![\w'-])")

#: Italian stopwords needed to flag a docstring, counted after quoted text is
#: removed. Declared heuristic, measured on 3187 docstrings on 2026-10-16
#: (after the Italian-to-English pass, before its leftovers were translated):
#: at threshold 1 the list above flagged 9 docstrings and all 9 were Italian
#: (0 false positives); «il», «un», «cui» and «ecc» added no false positive.
#: «di», «le», «la» and «come» were measured and rejected: they matched
#: ``UTF-16-LE``, ``|La-Lb|``, the English verb "come" and an unquoted label.
#: Italian prose made only of words outside the list still slips through.
#: Re-measured on 2026-10-16 for the case-sensitive short words (see the
#: list): the working tree has 3196 docstrings, 0 flagged before and after,
#: and no unquoted «il», «uno», «ecc» or «del» in any case, so the four
#: collisions were latent. As a recall sample, the HEAD versions of the same
#: files (3174 docstrings, still partly Italian) flagged 427 docstrings
#: before the change and 427 after it, none lost. Their hits were 985 «il»
#: (none ``IL``), 65 «uno» (none ``Uno``), 1 «ecc» (not ``ECC``) and 405
#: «del» (2 written ``DEL``, no ``del x.y``); 6 docstrings were flagged by
#: «del» alone, which is why «del» is narrowed rather than dropped.
#: 2026-10-16, comments joined the rule: «del»/«Del» followed by «key» is not
#: counted, because English comments name the Del key (Italian «Del file» is
#: still flagged). A word followed by a hyphen is not counted either: English
#: prefixes such as «un-keep» or «un-applied» matched «un».
_ITALIAN_THRESHOLD = 1

#: Quoted spans removed before counting stopwords: «guillemets», "double
#: quotes" (never across a blank line), ``double`` and `single` backticks.
#: Quoted UI labels, user quotes and literal program output are legitimately
#: Italian and must not flag an English docstring.
_QUOTED = re.compile(
    r'«[^»]*»|“[^”]*”|"(?:[^"\n]|\n(?!\s*\n))*"|``.*?``|`[^`\n]*`', re.DOTALL)

_PARAM_TAG = re.compile(r'@param\s+(\w+(?:\s*,\s*\w+)*)')

#: ``@throws A`` or ``@throws A, b.B``: the class names a tag declares; a
#: dotted name is compared by its last component.
_THROWS_TAG = re.compile(r'@throws\s+([\w.]+(?:\s*,\s*[\w.]+)*)')


def _documented_parameters(doc):
    """Return every name a docstring documents, ``@param a, b`` listing both."""
    names = set()
    for group in _PARAM_TAG.findall(doc):
        names.update(part.strip() for part in group.split(','))
    return names


def _documented_exceptions(doc):
    """Return the exception class names declared after ``@throws`` tags (last dotted part)."""
    names = set()
    for group in _THROWS_TAG.findall(doc):
        names.update(part.strip().rsplit('.', 1)[-1] for part in group.split(','))
    return names


def _looks_italian(doc):
    """Tell whether a docstring, quoted text removed, reaches the Italian threshold."""
    return len(_ITALIAN_STOPWORDS.findall(_QUOTED.sub(' ', doc))) >= _ITALIAN_THRESHOLD


def _every_docstring(node, prefix=''):
    """Yield (line, qualified name, docstring) for every definition under a node.

    Unlike the tag rules, the language rule covers functions nested in
    functions and definitions under any compound statement (for, while,
    match, try/except*), so the walk descends into every child node.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = prefix + child.name
            yield child.lineno, name, ast.get_docstring(child) or ''
            yield from _every_docstring(child, name + '.')
        else:
            yield from _every_docstring(child, prefix)


def _terminal_name(expression):
    """Return the last dotted component of a (called) name expression, or None."""
    if isinstance(expression, ast.Call):
        expression = expression.func
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        return expression.attr
    return None


def _module_class_bases(tree):
    """Map each class defined anywhere in the module to the names of its bases."""
    return {node.name: [_terminal_name(b) for b in node.bases]
            for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def _is_subclass(name, parent, bases, seen=()):
    """Tell whether exception ``name`` derives from ``parent``, module classes included."""
    if parent == 'BaseException':
        return True
    if name is None:
        return False
    if name == parent:
        return True
    if name in bases and name not in seen:
        return any(_is_subclass(b, parent, bases, seen + (name,)) for b in bases[name] if b)
    known, wanted = getattr(builtins, name, None), getattr(builtins, parent, None)
    return (isinstance(known, type) and isinstance(wanted, type)
            and issubclass(known, wanted))


def _catches(handler, name, bases):
    """Tell whether one except clause catches the exception class ``name``."""
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_is_subclass(name, _terminal_name(t), bases) for t in types if _terminal_name(t))


def _handler_classes(handler):
    """Return the class names an except clause catches; [None] for a bare except."""
    if handler.type is None:
        return [None]
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return [_terminal_name(t) for t in types]


#: Handler classes so broad that re-raising them re-raises whatever the try
#: body raised: the escaping class is then unnamed (see ``_documented_class``).
_BROAD_HANDLER_CLASSES = frozenset({'Exception', 'BaseException'})


def _documented_class(name, bases, reraised):
    """Return the class name a ``@throws`` tag must cover, or None when it cannot be named.

    Declared heuristic. A raised name counts as a class when it is a class of
    the scanned modules, a builtin class or a CapWords name; a lower-case name
    is a variable or a factory function (``raise error``, ``raise
    _invalid(...)``) whose class only the author can name. A re-raise from a
    bare ``except:`` or from ``except Exception``/``BaseException`` (cleanup
    then ``raise``) propagates whatever the body raised, so it is unnamed as
    well. Measured on 2026-10-16: of the 680 ``raise`` statements of the
    documented files, 25 raise a name that is neither builtin nor defined in
    them; the one CapWords name is a class (``subprocess.TimeoutExpired``)
    and the 24 lower-case ones are variables or factory functions
    (``error``, ``invalid``, ``_windows_error``, ``_timeout_error``...).
    Resolving imports would make the convention unnecessary.
    """
    if name is None or (reraised and name in _BROAD_HANDLER_CLASSES):
        return None
    if name in bases or isinstance(getattr(builtins, name, None), type):
        return name
    return name if name.lstrip('_')[:1].isupper() else None


def _uncaught_raises(function, bases):
    """Return (raise node, escaping class names) for the raises no enclosing try catches.

    A ``raise X`` inside the body of a ``try`` whose handler catches X, or one
    of its parents, is handled locally and is not part of the function's
    contract. A bare ``raise`` inside an ``except A`` or ``except (A, B)``
    handler re-raises what that handler caught, so A (and B) escape unless an
    outer ``try`` catches them. A raised expression whose class cannot be
    named counts as caught only by a bare except or ``BaseException``; in the
    result None stands for a class that cannot be named (see
    ``_documented_class``), which any ``@throws`` tag documents. Raises in
    handlers, ``else`` and ``finally`` are outside that ``try``; a bare
    ``raise`` outside every handler is ignored.
    """
    uncaught = []
    try_types = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, 'TryStar') else ())

    def visit(node, active, handler):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        if isinstance(node, ast.Raise):
            reraised = node.exc is None
            if not reraised:
                names = [_terminal_name(node.exc)]
            else:
                names = _handler_classes(handler) if handler is not None else []
            names = [_documented_class(n, bases, reraised) for n in names
                     if not any(_catches(h, n, bases) for handlers in active for h in handlers)]
            if names:
                uncaught.append((node, names))
        if isinstance(node, try_types):
            for statement in node.body:
                visit(statement, active + [node.handlers], handler)
            for inner in node.handlers:
                for statement in inner.body:
                    visit(statement, active, inner)
            for statement in node.orelse + node.finalbody:
                visit(statement, active, handler)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, active, handler)

    for statement in function.body:
        visit(statement, [], None)
    return uncaught


def _undocumented_exceptions(raises, doc, bases):
    """Return the escaping class names that no ``@throws`` tag names, nor a parent of them.

    ``?`` stands for an unnamed class, documented by any ``@throws`` tag.
    """
    documented = _documented_exceptions(doc)
    missing = []
    for _node, names in raises:
        for name in names:
            if name is None:
                covered = bool(documented)
            else:
                covered = any(_is_subclass(name, d, bases) for d in documented)
            shown = name or '?'
            if not covered and shown not in missing:
                missing.append(shown)
    return missing


def _all_definitions(body, prefix=''):
    """Yield (qualified name, node, public) for every definition, nested classes included."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            public = not node.name.startswith('_')
            yield prefix + node.name, node, public
            if isinstance(node, ast.ClassDef):
                for name, inner, inner_public in _all_definitions(node.body, prefix + node.name + '.'):
                    yield name, inner, public and inner_public
        else:
            yield from _all_definitions(_compound_bodies(node), prefix)


def _own_statements(function):
    """Yield the statements of a function body without descending into nested functions."""
    stack = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _returns_value(statement):
    """Tell whether a return statement hands back something other than a bare None."""
    value = statement.value
    return value is not None and not (isinstance(value, ast.Constant) and value.value is None)


def _yields_value(node):
    """Tell whether an expression node is a ``yield from`` or a ``yield`` of something other than None."""
    if isinstance(node, ast.YieldFrom):
        return True
    return isinstance(node, ast.Yield) and _returns_value(node)


def _parameter_names(function):
    """Return the parameter names a docstring must describe (self/cls excluded)."""
    args = function.args
    names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
    if args.vararg:
        names.append(args.vararg.arg)
    if args.kwarg:
        names.append(args.kwarg.arg)
    return [n for n in names if n not in ('self', 'cls')]


def incomplete_items(source, label, known_bases=None):
    """@brief List the ``@param``/``@return``/``@throws`` and language violations of one source text.

    @param source Python source code to inspect.
    @param label File label used in the reported entries.
    @param known_bases Optional map class name -> base names from other
            modules, so that an imported exception documented through its
            parent is recognised; the module's own classes take precedence.
    @return Entries formatted as ``label:line:name:rule[:detail]`` where the rule
            is ``param`` (missing names in the detail), ``return``, ``yield``
            (a generator without ``@return``), ``throws`` (undocumented classes
            in the detail, ``?`` for an unnamed one) or ``italian``. An empty
            list means the source complies.
    @note Only public functions are held to the tag rules; the language rule
          applies to every docstring, the module's and nested functions'
          included. ``@throws`` is required only for exceptions that escape
          (see ``_uncaught_raises``) and must name each class or a parent.
    """
    tree = ast.parse(source)
    failures = []
    bases = dict(known_bases or {})
    bases.update(_module_class_bases(tree))
    for name, node, public in _all_definitions(tree.body):
        doc = ast.get_docstring(node) or ''
        if not public or not doc or not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        documented = _documented_parameters(doc)
        missing = [p for p in _parameter_names(node) if p not in documented]
        if missing:
            failures.append(f'{label}:{node.lineno}:{name}:param:{",".join(missing)}')
        statements = list(_own_statements(node))
        if any(isinstance(s, ast.Return) and _returns_value(s) for s in statements) and '@return' not in doc:
            failures.append(f'{label}:{node.lineno}:{name}:return')
        if any(_yields_value(s) for s in statements) and '@return' not in doc:
            failures.append(f'{label}:{node.lineno}:{name}:yield')
        undocumented = _undocumented_exceptions(_uncaught_raises(node, bases), doc, bases)
        if undocumented:
            failures.append(f'{label}:{node.lineno}:{name}:throws:{",".join(undocumented)}')
    docstrings = [(1, '<module>', ast.get_docstring(tree) or '')]
    docstrings += list(_every_docstring(tree))
    for lineno, name, doc in docstrings:
        if doc and _looks_italian(doc):
            failures.append(f'{label}:{lineno}:{name}:italian')
    return failures


def test_production_modules_and_public_api_are_doxygen_documented():
    """@brief Ensure maintained production code carries @brief docstrings."""
    failures = []
    for path in _documented_files():
        label = path.relative_to(ROOT).as_posix()
        failures += undocumented_items(path.read_text(encoding='utf-8'), label)
    assert not failures, (
        f'{len(failures)} definitions lack a docstring starting with a real @brief:\n'
        + '\n'.join(failures))


def test_documentation_checker_reports_only_undocumented_public_api():
    """@brief Prove the checker distinguishes compliant from non-compliant code."""
    source = '''"""@brief Synthetic module."""


def documented(value):
    """@brief Return the value unchanged."""
    return value


def undocumented(value):
    return value


def weak(value):
    """Docstring without the required tag."""
    return value


def placeholder(value):
    """@brief Provide the placeholder operation for this workflow."""
    return value


def _private_helper():
    return None


class Widget:
    """@brief Synthetic widget."""

    def paintEvent(self, event):
        pass

    def _hidden(self):
        pass


class _Private:
    def visible_name(self):
        pass


if True:
    def conditional():
        pass
'''
    assert undocumented_items(source, 'sample.py') == [
        'sample.py:9:undocumented',
        'sample.py:13:weak',
        'sample.py:18:placeholder',
        'sample.py:30:Widget.paintEvent',
        'sample.py:43:conditional',
    ]
    assert undocumented_items('x = 1\n', 'bare.py') == ['bare.py:1:<module>']


def test_public_api_documents_parameters_returns_exceptions_in_english():
    """@brief Enforce @param/@return/@throws on the public API and English everywhere."""
    sources = {path.relative_to(ROOT).as_posix(): path.read_text(encoding='utf-8')
               for path in _documented_files()}
    known_bases = {}
    for source in sources.values():
        known_bases.update(_module_class_bases(ast.parse(source)))
    failures = []
    for label, source in sources.items():
        failures += incomplete_items(source, label, known_bases)
    assert not failures, (
        f'{len(failures)} documentation-contract violations (rule after the name):\n'
        + '\n'.join(failures))


def test_completeness_checker_separates_compliant_from_incomplete_code():
    """@brief Prove the tag and language rules flag only what they must."""
    source = '''"""@brief Synthetic module."""


def complete(value, *extra, flag=False, **options):
    """@brief Return the value.

    @param value Input value.
    @param extra Additional values.
    @param flag Switch.
    @param options Keyword options.
    @return The value.
    @throws ValueError When the value is negative.
    """
    if value < 0:
        raise ValueError(value)
    return value


def missing_param(value, other):
    """@brief Return the value.

    @param value Input value.
    @return The value.
    """
    return value


def missing_return(value):
    """@brief Compute something.

    @param value Input value.
    """
    return value * 2


def missing_throws(value):
    """@brief Validate.

    @param value Input value.
    """
    if value is None:
        raise ValueError("none")


def nested_return_is_not_own(value):
    """@brief Register a callback.

    @param value Input value.
    """
    def inner():
        return value
    inner()


def italian(value):
    """@brief Calcola il valore della somma.

    @param value Il valore che viene sommato.
    @return La somma.
    """
    return value


def _private_italian():
    """Restituisce il valore senza controlli."""
    return 1


def quoted_label(value):
    """@brief Set the «Non tenuta» caption.

    @param value Caption text.
    """
    return None


class Widget:
    """@brief Synthetic widget."""

    def method(self, event):
        """@brief Handle the event.

        @param event Qt event.
        """
        return None


class _Hidden:
    def visible(self, value):
        """@brief Not held to the tag rules because the class is private."""
        return value
'''
    assert incomplete_items(source, 'sample.py') == [
        'sample.py:19:missing_param:param:other',
        'sample.py:28:missing_return:return',
        'sample.py:36:missing_throws:throws:ValueError',
        'sample.py:55:italian:italian',
        'sample.py:64:_private_italian:italian',
    ]


def test_language_rule_reaches_every_definition_and_ignores_quotes():
    """@brief Prove the language rule covers nested definitions and skips quoted Italian.

    Positive cases: Italian prose in a function nested in a function, in
    definitions under for/while/match/try-except* blocks, and in the
    apostrophe spellings («e'», «gia'», «piu'») the repository uses. Negative
    cases: English prose that quotes an Italian label in «», "" or backticks,
    and an apostrophe after a letter or around a quoted letter.
    """
    source = '''"""@brief Synthetic module."""


def outer():
    """@brief Register a callback."""
    def inner():
        """Il valore viene letto dal file."""
    def inner_english():
        """Read the value from the file."""


for _ in range(1):
    def under_for():
        """Valore gia' letto."""

while False:
    def under_while():
        """Calcolo piu' rapido."""

match 1:
    case 1:
        def under_match():
            """Il valore e' pronto."""

try:
    pass
except* ValueError:
    def under_trystar():
        """Il risultato e' vuoto."""


def quoted_labels():
    """@brief Press «Aggiorna il file del simbolo», "Diverso per lato" or `Solo YOLO locale`."""


def quoted_user_words():
    """@brief Follow the user («devo poter tornare sui miei passi senza fare intervenire AI»)."""


def apostrophes():
    """@brief The code's letter 'e' and the type's name stay English."""


def unquoted_label():
    """@brief Press Aggiorna il file del simbolo in the KiCad tab."""
'''
    assert incomplete_items(source, 'sample.py') == [
        'sample.py:6:outer.inner:italian',
        'sample.py:13:under_for:italian',
        'sample.py:17:under_while:italian',
        'sample.py:22:under_match:italian',
        'sample.py:28:under_trystar:italian',
        'sample.py:44:unquoted_label:italian',
    ]


def test_throws_rule_exempts_locally_caught_raises():
    """@brief Prove ``@throws`` is required only for raises no enclosing try catches.

    Exempt: the same class or a parent caught (builtin hierarchy and classes
    of the module), a bare except. Still required: a class the handler does
    not catch, a raise inside the handler itself, a raise of an unnamed
    expression under a specific handler, and (since the 2026-10-16 review) a
    bare ``raise`` that re-raises what its handler caught.
    """
    source = '''"""@brief Synthetic module."""


class LocalError(ValueError):
    """@brief Module error derived from ValueError."""


def same_class():
    """@brief Caught exactly."""
    try:
        raise ValueError("x")
    except ValueError:
        pass


def builtin_parent():
    """@brief Caught through a builtin parent."""
    try:
        raise KeyError("x")
    except (TypeError, LookupError):
        pass


def module_parent():
    """@brief Caught through a parent of a module class."""
    try:
        raise LocalError("x")
    except ValueError:
        pass


def bare_except():
    """@brief Caught by a bare except."""
    try:
        raise RuntimeError("x")
    except:
        pass


def reraise():
    """@brief Not exempt: the bare raise lets ValueError escape."""
    try:
        pass
    except ValueError:
        raise


def other_class():
    """@brief Not caught: wrong class."""
    try:
        raise TypeError("x")
    except ValueError:
        pass


def raise_in_handler():
    """@brief Not caught: the raise is in the handler, outside the try."""
    try:
        pass
    except ValueError:
        raise RuntimeError("x")


def unnamed_expression(error):
    """@brief Not caught: the class of the raised expression is unknown.

    @param error Exception instance chosen by the caller.
    """
    try:
        raise error
    except ValueError:
        pass


def unnamed_expression_base(error):
    """@brief Caught: BaseException catches whatever is raised.

    @param error Exception instance chosen by the caller.
    """
    try:
        raise error
    except BaseException:
        pass
'''
    assert incomplete_items(source, 'sample.py') == [
        'sample.py:40:reraise:throws:ValueError',
        'sample.py:48:other_class:throws:TypeError',
        'sample.py:56:raise_in_handler:throws:RuntimeError',
        'sample.py:64:unnamed_expression:throws:?',
    ]


def test_param_rule_accepts_comma_separated_names():
    """@brief Prove ``@param a, b`` documents both names and nothing else."""
    source = '''"""@brief Synthetic module."""


def both(x, y):
    """@brief Point.

    @param x, y Coordinates in mm.
    """


def spaced(x, y, z):
    """@brief Point.

    @param x , y Coordinates in mm.
    @param z Height in mm.
    """


def one_missing(x, y, z):
    """@brief Point.

    @param x, y Coordinates in mm, and z is mentioned only in prose.
    """
'''
    assert incomplete_items(source, 'sample.py') == [
        'sample.py:19:one_missing:param:z',
    ]


def test_brief_needs_text_on_its_own_line():
    """@brief Prove ``@brief`` counts only when real text follows it on the same line.

    Positive (flagged): an empty ``@brief``, ``@brief`` alone with the text on
    the next line, a longer word such as ``@briefing``. Negative (accepted):
    ``@brief`` followed by text, also after leading blank space.
    """
    source = """'''@brief Synthetic module.'''


def empty():
    '''@brief
    '''


def next_line():
    '''@brief
    Text on the following line.
    '''


def longer_word():
    '''@briefing the reader.'''


def real():
    '''@brief Return the value.'''


def indented():
    '''
    @brief Text after a leading newline.
    '''
"""
    assert undocumented_items(source, 'sample.py') == [
        'sample.py:4:empty',
        'sample.py:9:next_line',
        'sample.py:15:longer_word',
    ]


def test_generators_document_what_they_yield():
    """@brief Prove a public generator that yields values needs ``@return``.

    Doxygen has no ``@yield`` command, so the contract keeps ``@return`` for
    what a generator yields. Positive: ``yield value`` and ``yield from``
    without ``@return``. Negative: the same with ``@return``, a bare ``yield``
    or ``yield None`` (a context manager hands back nothing), a yield that
    belongs to a nested function, and a private generator.
    """
    source = """'''@brief Synthetic module.'''


def values():
    '''@brief Walk the items.'''
    yield 1


def delegated(items):
    '''@brief Walk the items.

    @param items Source iterable.
    '''
    yield from items


def documented():
    '''@brief Walk the items.

    @return One integer per step.
    '''
    yield 1


def context():
    '''@brief Hold a lock while the caller works.'''
    yield


def context_none():
    '''@brief Hold a lock while the caller works.'''
    yield None


def nested_generator():
    '''@brief Build a generator for the caller.'''
    def inner():
        yield 1
    inner()


def _private():
    yield 1
"""
    assert incomplete_items(source, 'sample.py') == [
        'sample.py:4:values:yield',
        'sample.py:9:delegated:yield',
    ]


def test_reraise_requires_throws_naming_the_caught_class():
    """@brief Prove a bare ``raise`` in a handler needs ``@throws`` naming what it re-raises.

    Positive: the caught class undocumented, a ``@throws`` naming an unrelated
    class, a tuple handler with only one class named, a bare ``except:``
    re-raised without any tag. Negative: the class named, a parent named, an
    outer ``try`` that catches the re-raised class, a broad handler
    (``Exception``) re-raised with whatever class the author names.
    """
    source = """'''@brief Synthetic module.'''


def plain():
    '''@brief Clean up and re-raise.'''
    try:
        pass
    except KeyError:
        raise


def wrong_class():
    '''@brief Clean up and re-raise.

    @throws ValueError Never, in fact.
    '''
    try:
        pass
    except KeyError:
        raise


def tuple_half():
    '''@brief Clean up and re-raise.

    @throws KeyError When the key is missing.
    '''
    try:
        pass
    except (KeyError, OSError):
        raise


def bare_untagged():
    '''@brief Clean up and re-raise.'''
    try:
        pass
    except:
        raise


def named():
    '''@brief Clean up and re-raise.

    @throws KeyError When the key is missing.
    '''
    try:
        pass
    except KeyError:
        raise


def parent_named():
    '''@brief Clean up and re-raise.

    @throws LookupError When the key is missing.
    '''
    try:
        pass
    except KeyError:
        raise


def caught_outside():
    '''@brief The outer try swallows the re-raise.'''
    try:
        try:
            pass
        except KeyError:
            raise
    except LookupError:
        pass


def broad_cleanup():
    '''@brief Remove the temporary file and re-raise.

    @throws OSError When the write fails, after the cleanup.
    '''
    try:
        pass
    except Exception:
        raise
"""
    assert incomplete_items(source, 'sample.py') == [
        'sample.py:4:plain:throws:KeyError',
        'sample.py:12:wrong_class:throws:KeyError',
        'sample.py:23:tuple_half:throws:OSError',
        'sample.py:34:bare_untagged:throws:?',
    ]


def test_throws_must_name_each_escaping_class():
    """@brief Prove the ``@throws`` tag must name the class, not merely be present.

    Positive: a tag followed by prose instead of a class, a class named only
    in prose, one of two raised classes left out, an imported class
    documented by its parent when its bases are unknown. Negative: dotted and
    comma-separated names, a parent of a module class, the same imported
    class once its bases are known, a lower-case factory call documented by
    any tag.
    """
    source = """'''@brief Synthetic module.'''


class LocalError(ValueError):
    '''@brief Module error derived from ValueError.'''


def prose_tag(value):
    '''@brief Validate.

    @param value Input value.
    @throws When the value is negative.
    '''
    if value < 0:
        raise ValueError(value)


def prose_only(value):
    '''@brief Validate; raises ValueError on a negative value.

    @param value Input value.
    '''
    if value < 0:
        raise ValueError(value)


def one_of_two(value):
    '''@brief Validate.

    @param value Input value.
    @throws ValueError When the value is negative.
    '''
    if value < 0:
        raise ValueError(value)
    raise TypeError(value)


def imported_parent():
    '''@brief Install.

    @throws RuntimeError When the library cannot be written.
    '''
    raise LibraryError("x")


def dotted_and_list(value):
    '''@brief Validate.

    @param value Input value.
    @throws builtins.ValueError, TypeError On a bad value.
    '''
    if value < 0:
        raise ValueError(value)
    raise TypeError(value)


def module_parent():
    '''@brief Validate.

    @throws ValueError On a bad value.
    '''
    raise LocalError("x")


def factory():
    '''@brief Validate.

    @throws ValueError Built by the module's factory.
    '''
    raise _invalid("x")
"""
    assert incomplete_items(source, 'sample.py') == [
        'sample.py:8:prose_tag:throws:ValueError',
        'sample.py:18:prose_only:throws:ValueError',
        'sample.py:27:one_of_two:throws:TypeError',
        'sample.py:38:imported_parent:throws:LibraryError',
    ]
    assert incomplete_items(source, 'sample.py', {'LibraryError': ['RuntimeError']}) == [
        'sample.py:8:prose_tag:throws:ValueError',
        'sample.py:18:prose_only:throws:ValueError',
        'sample.py:27:one_of_two:throws:TypeError',
    ]


def test_tag_rules_reach_definitions_under_every_compound_statement():
    """@brief Prove ``@brief`` and the tag rules cover public functions under for/while/match/try-except*.

    Positive: a missing ``@param`` under for, while, match and try/except*,
    and a missing ``@brief`` under for. Negative: complete definitions in the
    same places, and private ones. ``async for``/``async with`` reach module
    level only under top-level await, so they are proved on a source parsed
    with that flag.
    """
    source = """'''@brief Synthetic module.'''


for _ in range(1):
    def under_for(value):
        '''@brief Loop body.'''
    def under_for_complete(value):
        '''@brief Loop body.

        @param value Input.
        '''
    def under_for_nobrief():
        '''Loop body.'''
else:
    def _private_else(value):
        '''@brief Private.'''

while False:
    def under_while(value):
        '''@brief Loop body.'''

match 1:
    case 1:
        def under_match(value):
            '''@brief Case body.'''

try:
    pass
except* ValueError:
    def under_trystar(value):
        '''@brief Handler body.'''


class Holder:
    '''@brief Class with a conditional method.'''

    for _ in range(1):
        def method(self, value):
            '''@brief Loop body.'''
"""
    assert incomplete_items(source, 'sample.py') == [
        'sample.py:5:under_for:param:value',
        'sample.py:19:under_while:param:value',
        'sample.py:24:under_match:param:value',
        'sample.py:30:under_trystar:param:value',
        'sample.py:38:Holder.method:param:value',
    ]
    assert undocumented_items(source, 'sample.py') == ['sample.py:12:under_for_nobrief']
    tree = compile(
        "async for _ in x:\n    def f():\n        pass\n"
        "async with x:\n    def g():\n        pass\n",
        'sample.py', 'exec', flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    assert [name for name, _node in _public_definitions(tree.body)] == ['f', 'g']


def test_short_stopwords_skip_english_and_code_tokens():
    """@brief Prove the four case-sensitive stopwords keep Italian and skip the colliding tokens.

    Positive: «il», «Il», «uno», «ecc», «del» and «Del» in Italian use.
    Negative: the abbreviation ``IL``, the board name ``Arduino Uno``, ``ECC``
    memory and the Python ``del`` statement on a dotted or subscripted
    operand.
    """
    source = """'''@brief Synthetic module.'''


def lower_il():
    '''@brief Read il file.'''


def upper_il():
    '''@brief Il file is read.'''


def lower_uno():
    '''@brief Read uno file.'''


def lower_ecc():
    '''@brief Pins, pads ecc.'''


def lower_del():
    '''@brief The colour del LED.'''


def upper_del():
    '''@brief Del file.'''


def abbreviation():
    '''@brief Return the IL value.'''


def board():
    '''@brief Pinout of the Arduino Uno board.'''


def memory():
    '''@brief ECC memory size.'''


def keyword_attribute():
    '''@brief Free it as del self._cache would.'''


def keyword_subscript():
    '''@brief Same as del table[key] in Python.'''
"""
    assert incomplete_items(source, 'sample.py') == [
        'sample.py:4:lower_il:italian',
        'sample.py:8:upper_il:italian',
        'sample.py:12:lower_uno:italian',
        'sample.py:16:lower_ecc:italian',
        'sample.py:20:lower_del:italian',
        'sample.py:24:upper_del:italian',
    ]

# ── comments: English like the docstrings (user decision, 2026-10-16) ──────────
#
# The user asked that the language rule also hold for every ``#`` and ``#:``
# comment of the covered code, so that an Italian comment fails the suite the
# next time one is written. The same stopword heuristic and quoted-span removal
# as the docstrings apply, on whole comment BLOCKS: consecutive comment lines
# are judged together, because an Italian block often continues on lines that
# contain no stopword of their own.


#: A multi-line string literal counts as an embedded script when one of its
#: lines is an import, a definition or an assignment: such strings are
#: Python handed to another interpreter (kicad_models._MEASURE_SCRIPT runs
#: in FreeCAD) and their ``#`` lines are comments like any other. Prose
#: strings with Markdown headings («# Titolo») are left to the docstring
#: and UI-text rules: a heading is not a comment.
_SCRIPT_LINE = re.compile(
    r'^\s*(?:import\s+\w|from\s+[\w.]+\s+import\s|def\s+\w+\s*\(|class\s+\w+|\w[\w.]*\s*=[^=])',
    re.MULTILINE)

#: A ``#`` comment line of an embedded script (the whole line, so that a
#: ``#`` inside a string of the script is not taken for one).
_SCRIPT_COMMENT = re.compile(r'^\s*#(.*)$')


def _looks_like_script(text):
    """Tell whether the body of a triple-quoted string is an embedded Python script."""
    return bool(_SCRIPT_LINE.search(text))


def _embedded_script_comments(token):
    """Yield (line, text) for the ``#`` lines of a triple-quoted STRING token holding a script."""
    text = token.string
    quote = text.lstrip('rRbBuUfF')[:3]
    if quote not in ('"""', "'''") or not _looks_like_script(text):
        return
    body = text[len(text) - len(text.lstrip('rRbBuUfF')) + 3:-3]
    for offset, line in enumerate(body.split('\n')):
        match = _SCRIPT_COMMENT.match(line)
        if match:
            yield token.start[0] + offset, match.group(1).lstrip(':').strip()


def _comment_blocks(source):
    """Yield (first line, joined text) for each run of comments on consecutive lines.

    Since 2026-10-27 the ``#`` lines of an embedded script (see
    ``_looks_like_script``) are comments too: they are judged in blocks of
    their own, by their line in the host file.
    """
    block_start, block_end, parts = None, None, []
    comments = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comments.append((token.start[0], token.string.lstrip('#').lstrip(':').strip()))
        elif token.type == tokenize.STRING:
            comments.extend(_embedded_script_comments(token))
    for line, text in sorted(comments):
        if block_end is not None and line == block_end + 1:
            parts.append(text)
        else:
            if parts:
                yield block_start, ' '.join(parts)
            block_start, parts = line, [text]
        block_end = line
    if parts:
        yield block_start, ' '.join(parts)


def italian_comments(source, label):
    """@brief List the comment blocks of one source text that read as Italian.

    @param source Python source code to inspect.
    @param label File label used in the reported entries.
    @return Entries formatted as ``label:line:italian-comment`` (line of the
            block's first comment). An empty list means every comment is English.
    @note Quoted spans («…», "…", `…`) are removed first: a quoted UI label or
          user quote is legitimately Italian.
    """
    return [f'{label}:{line}:italian-comment'
            for line, text in _comment_blocks(source) if _looks_italian(text)]


def test_comments_are_written_in_english():
    """@brief Enforce English ``#`` and ``#:`` comments in the covered code."""
    failures = []
    for path in _documented_files():
        label = path.relative_to(ROOT).as_posix()
        failures += italian_comments(path.read_text(encoding='utf-8'), label)
    assert not failures, (
        f'{len(failures)} comment blocks are written in Italian:\n' + '\n'.join(failures))


def test_comment_language_rule_judges_blocks_and_ignores_quotes_and_strings():
    """@brief Prove the comment rule flags Italian blocks only, not quotes or code strings."""
    source = '\n'.join([
        '# English comment about the body outline.',     # 1 negative
        'x = 1  # trailing English note',                 # 2 negative
        '',
        '# Il contorno viene letto dalla foto',            # 4 positive
        '# e poi convertito in millimetri.',               # 5 same block
        '',
        '#: Valore letto per lato.',                       # 7 no stopword alone...
        '#: Il valore resta stimato.',                     # 8 ...but same block: positive at 7
        '',
        '# The button «Rigenera il modello» is enabled.',  # 10 negative: quoted label
        'label = "Il valore che viene letto"',             # 11 negative: a string, not a comment
        'del cache  # Del key also clears the view',       # 12 negative: English words
        '# An un-applied draft is never installed.',         # 13 negative: hyphenated prefix
    ]) + '\n'
    assert italian_comments(source, 's.py') == ['s.py:4:italian-comment', 's.py:7:italian-comment']


def test_init_and_thread_run_need_a_brief_even_in_private_classes():
    """@brief Prove every ``__init__`` and every QThread/QRunnable ``run`` need ``@brief`` (review 2026-10-27, C4).

    Positive: an undocumented ``__init__`` of a public class, of a private
    class and of a class nested under ``if``; an undocumented ``run`` of a
    private QThread subclass and of a QRunnable subclass. Negative: the same
    methods documented, a ``run`` of a class that is no thread, and the
    other private helpers, which keep their exemption.
    """
    source = '''"""@brief Synthetic module."""


class Public:
    """@brief Public class."""

    def __init__(self, value):
        self.value = value

    def _helper(self):
        pass


class _Private:
    def __init__(self, value):
        self.value = value


class _Worker(QThread):
    def __init__(self):
        """@brief Documented constructor."""

    def run(self):
        pass


class _Task(QtCore.QRunnable):
    def run(self):
        pass


class _Plain:
    def run(self):
        pass


class _Documented(QThread):
    def __init__(self):
        """@brief Documented constructor."""

    def run(self):
        """@brief Documented thread body."""


if True:
    class Conditional:
        """@brief Nested under a compound statement."""

        def __init__(self):
            pass
'''
    assert undocumented_items(source, 'sample.py') == [
        'sample.py:7:Public.__init__',
        'sample.py:15:_Private.__init__',
        'sample.py:23:_Worker.run',
        'sample.py:28:_Task.run',
        'sample.py:49:Conditional.__init__',
    ]


def test_comment_language_rule_catches_content_word_italian():
    """@brief Prove the comments the 2026-10-27 reviewers found are flagged, and English ones with Italian quotes are not.

    Positive: the five comments quoted in the review, which carry no
    function word of the first list, an articulated preposition pair («a
    meta'», «di un») and a ``noqa`` tail in Italian. Negative: English
    comments quoting Italian labels in «…», the English verb "serve", a
    sample file name «foto.png» written unquoted, a proper name after
    "in", and the translated form of the first positive.
    """
    lines = [
        '# I testi lunghi vanno a capo (`_breakable`): nessuno scorrimento in orizzontale.',  # 1 +
        '',
        '#: Etichette leggibili (GUI, messaggi d\'errore).',                       # 3 +
        '',
        '# I modelli "auto" espongono prezzi sentinella negativi: valgono 0.',   # 5 +
        '',
        '# Spenti su richiesta (Body3DParams.show_pins_3d): restano i fori.',    # 7 +
        '',
        '#: Nessun dato: ripiego dichiarato (ASSUMED_SCALE_PX_PER_MM).',         # 9 +
        '',
        '# Sezione presa a meta\' altezza.',                                      # 11 +
        '',
        '# Letto da un file di un altro progetto.',                               # 13 +
        '',
        'x = 1  # noqa: BLE001 - errori rete/API riportati in UI',                # 15 +
        '',
        '# Long texts wrap (`_breakable`) instead of scrolling horizontally.',    # 17 -
        '# The «Modello rete SMD» row and the «Nessun dato» hint stay in sync.', # 18 -
        '# These rows serve as the fallback when the detector is silent.',       # 19 -
        '# Usage: compare_vision_models.py foto.png --length 25.5',              # 20 -
        '# Measured in La Jolla on a cloudy day.',                                # 21 -
        '# The "ripiego" field (declared fallback) is read first.',              # 22 -
    ]
    source = '\n'.join(lines) + '\n'
    assert italian_comments(source, 's.py') == [
        f's.py:{line}:italian-comment' for line in (1, 3, 5, 7, 9, 11, 13, 15)]


def test_comment_rule_scans_the_hash_lines_of_embedded_scripts():
    """@brief Prove ``#`` lines inside a triple-quoted string that is a script are judged, by their host line.

    Positive: an Italian comment block in a script stored in a constant
    (like kicad_models._MEASURE_SCRIPT), reported at its line in the host
    file. Negative: an English comment in the same script, a Markdown
    heading in a prose string that is no script, and a ``#`` inside a
    string of the script.
    """
    source = '\n'.join([
        '"""@brief Module."""',                                   # 1
        'SCRIPT = """',                                           # 2
        'import FreeCAD',                                         # 3
        '# Ogni misura finita viene scritta SUBITO, riga per riga.',  # 4 positive
        '# Cio\' che era gia\' stato misurato sopravvive.',       # 5 same block
        'shape = FreeCAD.open(path)',                             # 6
        '# Body extent only: the section is taken high up.',      # 7 negative
        'print("# not a comment: il valore")',                    # 8 negative: inside a string
        '"""',                                                    # 9
        'PROMPT = """',                                           # 10
        '# Istruzioni per il modello',                            # 11 negative: a heading, no script
        'Elenca i corpi della scheda.',                           # 12
        '"""',                                                    # 13
    ]) + '\n'
    assert italian_comments(source, 's.py') == ['s.py:4:italian-comment']
