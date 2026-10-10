"""@brief Read-only checks on disk artifacts and the current applied symbol."""
from dataclasses import dataclass
from pathlib import Path
from .messages import tx
import math
import os
import re
import subprocess
import tempfile

from .system_dependencies import find_kicad_cli
from .kicad_library import (ComponentArtifacts, create_preview_project,
                            step_file_is_complete, _direct_child_ranges, _node_arguments)
from ..generators.symbol import symbol_inputs, serialize_symbol


@dataclass(frozen=True)
class Check:
    """@brief Carry one verification outcome with status, title and diagnostic detail."""
    status: str
    title: str
    detail: str


def _children(text, name):
    return [text[a:b] for a, b in _direct_child_ranges(text, name)]


def _single_footprint(text):
    """Reject extra root expressions even when a usable pad precedes them."""
    text = text.strip()
    if not re.match(r'\(footprint\s', text):
        raise ValueError(tx('File footprint non valido.'))
    # The shared parser checks balance/quotes; additionally require one root.
    _direct_child_ranges(text, 'pad')
    depth, quoted, escaped = 0, False, False
    for index, character in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character == '(':
            depth += 1
        elif character == ')':
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                raise ValueError(tx('Contenuto estraneo dopo il footprint.'))


def _pad_geometry(pad):
    """Validate basic physical dimensions independently of CLI availability."""
    args = _node_arguments(pad)
    if len(args) < 3:
        raise ValueError(tx('Pad senza numero, tipo o forma.'))
    if args[1] not in ('smd', 'thru_hole', 'np_thru_hole', 'connect'):
        raise ValueError(tx('Pad {pin}: tipo non valido.', pin=args[0]))
    layers = _children(pad, 'layers')
    if len(layers) != 1 or not _node_arguments(layers[0]):
        raise ValueError(tx('Pad {pin}: layer mancanti.', pin=args[0]))
    for field in ('at', 'size'):
        nodes = _children(pad, field)
        if len(nodes) != 1:
            raise ValueError(tx('Pad {pin}: {field} mancante o duplicato.', pin=args[0], field=field))
        values = _node_arguments(nodes[0])
        if len(values) < 2:
            raise ValueError(tx('Pad {pin}: {field} incompleto.', pin=args[0], field=field))
        numbers = [float(v) for v in values[:3 if field == 'at' else 2]]
        if not all(math.isfinite(v) for v in numbers):
            raise ValueError(tx('Pad {pin}: coordinate/dimensioni non finite.', pin=args[0]))
        if field == 'size':
            if min(numbers) <= 0:
                raise ValueError(tx('Pad {pin}: dimensioni non positive.', pin=args[0]))
            size = numbers
    if args[1] in ('thru_hole', 'np_thru_hole'):
        drills = _children(pad, 'drill')
        if len(drills) != 1:
            raise ValueError(tx('Pad {pin}: foro mancante o duplicato.', pin=args[0]))
        drill = _node_arguments(drills[0])
        oval = drill[:1] == ['oval']
        dimensions = drill[1:3] if oval else drill[:1]
        if len(dimensions) != (2 if oval else 1):
            raise ValueError(tx('Pad {pin}: foro incompleto.', pin=args[0]))
        dimensions = [float(v) for v in dimensions]
        if not all(math.isfinite(v) and v > 0 for v in dimensions):
            raise ValueError(tx('Pad {pin}: diametro foro non valido.', pin=args[0]))
        if not oval:
            dimensions *= 2
        if any(d > s + 1e-9 for d, s in zip(dimensions, size)):
            raise ValueError(tx('Pad {pin}: foro maggiore del pad.', pin=args[0]))
    return args


def verify_component(directory, name, rows, *, pending=False, issues=(), emit=lambda result: None):
    """@brief Use scratch copies only; failed checks do not suppress independent checks."""
    results = []
    def add(status, title, detail):
        result = Check(status, title, str(detail))
        results.append(result)
        emit(result)

    root = Path(directory)
    if not isinstance(name, str) or not name.strip() or name in ('.', '..') or any(
            c in name for c in '/\\:\x00'):
        add('ERROR', tx('Componente'), tx('Nome componente non valido.'))
        return results
    fp = root / f'{name}.pretty' / f'{name}.kicad_mod'
    step = root / f'{name}.3dshapes' / f'{name}.step'
    cli = find_kicad_cli()
    if not cli:
        add('SKIP', tx('KiCad'), tx('KiCad CLI non disponibile: apertura reale non verificata.'))
    else:
        add('OK', tx('KiCad CLI'), cli)
    if pending:
        add('WARN', tx('Modifiche non applicate'), tx('Verificato il simbolo applicato; la bozza non è inclusa.'))
    for issue in issues:
        add('WARN', tx('Revisione simbolo'), issue)
    # Inspect the applied snapshot even when the editor has an unrelated draft.
    # UI review messages describe the draft and must not be its only evidence.
    for row in rows:
        number = str(row.get('pin', ''))
        if not str(row.get('name') or '').strip():
            message = tx('Nome mancante: {number}', number=number)
            if message not in issues:
                add('WARN', tx('Revisione simbolo'), message)
        if row.get('electrical_type_reviewed') is False or (
                row.get('electrical_type', 'passive') in ('passive', 'unspecified')
                and not row.get('electrical_type_reviewed')):
            message = tx('Tipo elettrico da verificare: {number}', number=number)
            if message not in issues:
                add('WARN', tx('Revisione simbolo'), message)

    with tempfile.TemporaryDirectory(prefix='kicadgen-check-') as scratch:
        scratch = Path(scratch)
        def run(title, args, output, pattern=None):
            if not cli:
                add('SKIP', title, tx('Richiede KiCad CLI.'))
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
                    add('ERROR', title, log or tx('Nessun risultato prodotto da KiCad.'))
                    return False
                # A successful CLI exit can still contain model import warnings.
                if re.search(r'^[\s*]*(?:error|errore|fehler|erreur|fallita|fallito|impossibile|échec|echec)\b|\b(?:failed|unable)\s+to\b|\bcannot\s+(?:read|open|load|import)\b', log, re.I | re.M):
                    add('ERROR', title, log)
                    return False
                warning = bool(re.search(r'^[\s*]*(?:warning|avviso|attenzione|warnung|avertissement)\b', log, re.I | re.M))
                add('WARN' if warning else 'OK', title, log or tx('File letto da KiCad.'))
                return True
            except (OSError, subprocess.TimeoutExpired) as exc:
                add('ERROR', title, exc)
                return False

        pad_ids = None
        try:
            text = fp.read_text(encoding='utf-8')
            _single_footprint(text)
            pads = [_pad_geometry(pad) for pad in _children(text, 'pad')]
            pad_ids = {p[0] for p in pads if len(p) > 2 and p[1] != 'np_thru_hole' and p[0]}
            if not pad_ids:
                raise ValueError(tx('Nessun pad elettrico trovato.'))
            add('OK', tx('Footprint su disco'), str(fp))
            if any(not p[0] and p[1] != 'np_thru_hole' for p in pads):
                add('WARN', tx('Pad senza numero'), tx('Pad di rame senza numero esclusi dalla corrispondenza elettrica.'))
            models = _children(text, 'model')
            if not models:
                add('WARN', tx('Collegamento 3D originale'), tx('Il footprint su disco non contiene un riferimento 3D; il test di importazione lo aggiunge soltanto nella copia temporanea.'))
            for model in models:
                # The isolated import normalizes transforms; inspect the original
                # values separately so an invalid scale is not hidden by that copy.
                changed_transform = False
                for field, default in (('offset', (0., 0., 0.)), ('rotate', (0., 0., 0.)), ('scale', (1., 1., 1.))):
                    nodes = _children(model, field)
                    if not nodes:
                        continue
                    xyz = _children(nodes[0], 'xyz')
                    try:
                        values = [float(v) for v in _node_arguments(xyz[0])] if len(xyz) == 1 else []
                        valid = len(nodes) == 1 and len(values) == 3 and all(math.isfinite(v) for v in values)
                        if field == 'scale':
                            valid = valid and all(v != 0 for v in values)
                    except ValueError:
                        valid = False
                    if not valid:
                        add('ERROR', tx('Collegamento 3D originale'), tx('Trasformazione {field} non valida.', field=field))
                    elif tuple(values) != default:
                        changed_transform = True
                if changed_transform:
                    add('WARN', tx('Collegamento 3D originale'), tx('Trasformazione 3D originale non verificata: la prova temporanea usa scala e orientamento normalizzati.'))
                args = _node_arguments(model)
                reference = args[0] if args else ''
                expanded = os.path.expandvars(reference)
                if not reference:
                    add('ERROR', tx('Collegamento 3D originale'), tx('Riferimento modello vuoto.'))
                elif '${' in expanded or re.search(r'%[^%]+%', expanded):
                    add('SKIP', tx('Collegamento 3D originale'), tx('Variabile KiCad da risolvere nel progetto di destinazione: {reference}', reference=reference))
                elif not Path(expanded).is_absolute():
                    add('SKIP', tx('Collegamento 3D originale'), tx('Percorso relativo da verificare nel progetto di destinazione: {reference}', reference=reference))
                elif not Path(expanded).is_file():
                    add('ERROR', tx('Collegamento 3D originale'), tx('Modello non trovato: {reference}', reference=reference))
                else:
                    add('OK', tx('Collegamento 3D originale'), reference)
            run(tx('Lettura footprint in KiCad'), ['fp', 'export', 'svg', '--footprint', name, '-o', scratch / 'fp', fp.parent], scratch / 'fp', '*.svg')
        except (OSError, ValueError, RuntimeError) as exc:
            add('ERROR', tx('Footprint su disco'), exc)

        try:
            pins, body = symbol_inputs(rows)
            symbol = serialize_symbol(name, pins, footprint=f'KiCadGen:{name}', body=body)
            ids = {str(p.number) for p in pins}
            if pad_ids is None:
                add('SKIP', tx('Corrispondenza pin-pad'), tx('Footprint non leggibile.'))
            elif ids != pad_ids:
                add('ERROR', tx('Corrispondenza pin-pad'),
                    tx('Solo simbolo: {symbol_only}; solo footprint: {footprint_only}', symbol_only=sorted(ids - pad_ids), footprint_only=sorted(pad_ids - ids)))
            else:
                add('OK', tx('Corrispondenza pin-pad'), tx('{count} numeri elettrici corrispondenti.', count=len(ids)))
            candidate = scratch / f'{name}.kicad_sym'
            candidate.write_text(symbol, encoding='utf-8')
            run(tx('Simbolo applicato: lettura in KiCad'), ['sym', 'export', 'svg', '-o', scratch / 'sym', candidate], scratch / 'sym', '*.svg')
            saved = root / f'{name}.kicad_sym'
            if saved.is_file():
                if saved.read_text(encoding='utf-8') != symbol:
                    add('WARN', tx('Simbolo esportato'), tx('Il file nella cartella componente differisce dal simbolo applicato: riesportarlo.'))
                run(tx('Simbolo su disco: lettura in KiCad'), ['sym', 'export', 'svg', '-o', scratch / 'saved', saved], scratch / 'saved', '*.svg')
            else:
                add('WARN', tx('Simbolo esportato'), tx('Nessun .kicad_sym nella cartella componente; verificata la versione applicata in memoria.'))
        except (OSError, ValueError, TypeError) as exc:
            add('ERROR', tx('Simbolo'), exc)

        try:
            if not step_file_is_complete(step):
                raise ValueError(tx('STEP incompleto o troncato.'))
            add('OK', tx('Integrità STEP'), str(step))
            if fp.is_file():
                artifacts = ComponentArtifacts(name, root, fp, step)
                preview = create_preview_project(artifacts, scratch / 'project')
                output = scratch / 'assembly.step'
                if run(tx('Importazione 3D in KiCad'), ['pcb', 'export', 'step', '--no-board-body', '-o', output, preview.board_path], output):
                    solid = re.search(r'\b(MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS)\s*\(', output.read_text(errors='replace'))
                    if not solid:
                        add('WARN', tx('Solidi 3D'), tx('Esportazione riuscita, ma presenza di solidi non confermata.'))
            else:
                add('SKIP', tx('Importazione 3D in KiCad'), tx('Footprint mancante.'))
        except (OSError, ValueError, RuntimeError) as exc:
            add('ERROR', tx('Modello 3D'), exc)
    add('WARN', tx('Verifiche manuali'),
        tx('Confrontare pinout e misure con la documentazione; verificare visivamente posizione, scala e orientamento 3D. Il test 3D usa il collegamento normalizzato della libreria generata. Registrazione nelle librerie installate, ERC dello schema e DRC della scheda finale non verificati.'))
    return results
