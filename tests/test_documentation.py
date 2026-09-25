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
module- or class-level ``if``/``try``/``with`` blocks are checked as well.
"""
import ast
import re
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


def _starts_with_brief(node):
    """Tell whether a docstring starts with a meaningful @brief line."""
    docstring = ast.get_docstring(node, clean=False)
    if not docstring or not docstring.lstrip().startswith('@brief'):
        return False
    first_line = docstring.lstrip().splitlines()[0]
    return not _PLACEHOLDER_BRIEF.search(first_line)


def _public_definitions(body, prefix=''):
    """Yield (qualified name, node) for the public definitions in a block.

    Compound statements (if/try/with) are descended into because conditional
    definitions are still part of the public surface; function bodies are not.
    """
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith('_'):
                continue
            name = prefix + node.name
            yield name, node
            if isinstance(node, ast.ClassDef):
                yield from _public_definitions(node.body, name + '.')
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            nested = list(node.body) + list(getattr(node, 'orelse', []))
            nested += list(getattr(node, 'finalbody', []))
            for handler in getattr(node, 'handlers', []):
                nested += list(handler.body)
            yield from _public_definitions(nested, prefix)


def undocumented_items(source, label):
    """@brief List the documentation-contract violations of one source text.

    @param source Python source code to inspect.
    @param label File label used in the reported entries.
    @return Entries formatted as ``label:line:name``; ``<module>`` names the
            module docstring. An empty list means the source complies.
    """
    tree = ast.parse(source)
    failures = []
    if not _starts_with_brief(tree):
        failures.append(f'{label}:1:<module>')
    for name, node in _public_definitions(tree.body):
        if not _starts_with_brief(node):
            failures.append(f'{label}:{node.lineno}:{name}')
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
