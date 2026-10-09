"""Read-only checks on disk artifacts and the current applied symbol."""
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import tempfile

from .system_dependencies import find_kicad_cli
from .kicad_library import (ComponentArtifacts, create_preview_project,
                            step_file_is_complete, _direct_child_ranges, _node_arguments)
from ..generators.symbol import symbol_inputs, serialize_symbol


@dataclass(frozen=True)
class Check:
    status: str
    title: str
    detail: str


def verify_component(directory, name, rows, *, pending=False, issues=(), emit=lambda result: None):
    """Use scratch copies only; failed checks do not suppress independent checks."""
    results = []
    def add(status, title, detail):
        result = Check(status, title, str(detail))
        results.append(result)
        emit(result)

    root = Path(directory)
    fp = root / f'{name}.pretty' / f'{name}.kicad_mod'
    step = root / f'{name}.3dshapes' / f'{name}.step'
    cli = find_kicad_cli()
    if not cli:
        add('SKIP', 'KiCad', 'KiCad CLI non disponibile: apertura reale non verificata.')
    else:
        add('OK', 'KiCad CLI', cli)
    if pending:
        add('WARN', 'Modifiche non applicate', 'Verificato il simbolo applicato; la bozza non è inclusa.')
    for issue in issues:
        add('WARN', 'Revisione simbolo', issue)

    with tempfile.TemporaryDirectory(prefix='kicadgen-check-') as scratch:
        scratch = Path(scratch)
        def run(title, args, output, pattern=None):
            if not cli:
                add('SKIP', title, 'Richiede KiCad CLI.')
                return False
            try:
                if pattern:
                    output.mkdir(parents=True, exist_ok=True)
                proc = subprocess.run([cli, *map(str, args)], capture_output=True,
                    text=True, errors='replace', timeout=60,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                log = (proc.stdout + '\n' + proc.stderr).strip()
                files = list(output.rglob(pattern)) if pattern else [output]
                valid = any(p.is_file() and p.stat().st_size for p in files)
                if proc.returncode or not valid:
                    add('ERROR', title, log or 'Nessun risultato prodotto da KiCad.')
                    return False
                # A successful CLI exit can still contain model import warnings.
                warning = bool(re.search(r'\b(error|warning|failed|unable|cannot)\b', log, re.I))
                add('WARN' if warning else 'OK', title, log or 'File letto da KiCad.')
                return True
            except (OSError, subprocess.TimeoutExpired) as exc:
                add('ERROR', title, exc)
                return False

        pad_ids = None
        try:
            text = fp.read_text(encoding='utf-8')
            if not re.match(r'\s*\(footprint\s', text):
                raise ValueError('File footprint non valido.')
            pads = [_node_arguments(text[a:b]) for a, b in _direct_child_ranges(text, 'pad')]
            pad_ids = {p[0] for p in pads if len(p) > 2 and p[1] != 'np_thru_hole' and p[0]}
            if not pad_ids:
                raise ValueError('Nessun pad elettrico trovato.')
            add('OK', 'Footprint su disco', str(fp))
            run('Lettura footprint in KiCad', ['fp', 'export', 'svg', '--footprint', name, '-o', scratch / 'fp', fp.parent], scratch / 'fp', '*.svg')
        except (OSError, ValueError, RuntimeError) as exc:
            add('ERROR', 'Footprint su disco', exc)

        try:
            pins, body = symbol_inputs(rows)
            symbol = serialize_symbol(name, pins, footprint=f'KiCadGen:{name}', body=body)
            ids = {str(p.number) for p in pins}
            if pad_ids is None:
                add('SKIP', 'Corrispondenza pin-pad', 'Footprint non leggibile.')
            elif ids != pad_ids:
                add('ERROR', 'Corrispondenza pin-pad',
                    f'Solo simbolo: {sorted(ids - pad_ids)}; solo footprint: {sorted(pad_ids - ids)}')
            else:
                add('OK', 'Corrispondenza pin-pad', f'{len(ids)} numeri elettrici corrispondenti.')
            candidate = scratch / f'{name}.kicad_sym'
            candidate.write_text(symbol, encoding='utf-8')
            run('Simbolo applicato: lettura in KiCad', ['sym', 'export', 'svg', '-o', scratch / 'sym', candidate], scratch / 'sym', '*.svg')
            saved = root / f'{name}.kicad_sym'
            if saved.is_file():
                if saved.read_text(encoding='utf-8') != symbol:
                    add('WARN', 'Simbolo esportato', 'Il file nella cartella componente differisce dal simbolo applicato: riesportarlo.')
                run('Simbolo su disco: lettura in KiCad', ['sym', 'export', 'svg', '-o', scratch / 'saved', saved], scratch / 'saved', '*.svg')
            else:
                add('WARN', 'Simbolo esportato', 'Nessun .kicad_sym nella cartella componente; verificata la versione applicata in memoria.')
        except (OSError, ValueError, TypeError) as exc:
            add('ERROR', 'Simbolo', exc)

        try:
            if not step_file_is_complete(step):
                raise ValueError('STEP incompleto o troncato.')
            add('OK', 'Integrità STEP', str(step))
            if fp.is_file():
                artifacts = ComponentArtifacts(name, root, fp, step)
                preview = create_preview_project(artifacts, scratch / 'project')
                output = scratch / 'assembly.step'
                if run('Importazione 3D in KiCad', ['pcb', 'export', 'step', '--no-board-body', '-o', output, preview.board_path], output):
                    solid = re.search(r'\b(MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS)\s*\(', output.read_text(errors='replace'))
                    if not solid:
                        add('WARN', 'Solidi 3D', 'Esportazione riuscita, ma presenza di solidi non confermata.')
            else:
                add('SKIP', 'Importazione 3D in KiCad', 'Footprint mancante.')
        except (OSError, ValueError, RuntimeError) as exc:
            add('ERROR', 'Modello 3D', exc)
    add('WARN', 'Verifiche manuali',
        'Confrontare pinout e misure con la documentazione; verificare visivamente posizione, scala e orientamento 3D. '
        'Il test 3D usa il collegamento normalizzato della libreria generata. '
        'Registrazione nelle librerie installate, ERC dello schema e DRC della scheda finale non verificati.')
    return results
