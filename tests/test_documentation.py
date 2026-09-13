"""@brief Enforce the repository Doxygen-compatible documentation contract."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_modules_and_public_api_are_doxygen_documented():
    """@brief Ensure maintained production modules have Doxygen-compatible documentation."""
    failures = []
    files = list((ROOT / 'kicadgen').rglob('*.py')) + list((ROOT / 'examples').rglob('*.py'))
    for path in files:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        if not ast.get_docstring(tree):
            failures.append(f'{path}: module')
    assert not failures, 'Missing Doxygen-compatible documentation:\n' + '\n'.join(failures)
