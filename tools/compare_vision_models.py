"""Confronta due modelli di visione sullo STESSO insieme di proposte YOLO.

Serve a rispondere a una domanda sola: quale modello riconosce meglio tipo,
quote e orientamento dei componenti? Per poterlo dire, l'unica cosa che cambia
fra le due esecuzioni deve essere il modello: la foto, la calibrazione e le
proposte del rilevatore locale sono le stesse, rilevate una volta e riusate.

Percorso esercitato: `prepare_photo` -> `detect_yolo_components` ->
`review_global_inventory`, cioe' la pipeline di produzione. Non c'e' una
seconda strada "di prova" che poi si comporta diversamente da quella vera.

Esempi (dalla radice del repository, con OPENROUTER_API_KEY nell'ambiente):

  python tools/compare_vision_models.py foto.png --length 25.5 --width 18 \\
      --model qwen/qwen3-vl-235b-a22b-instruct

  python tools/compare_vision_models.py foto.png --length 25.5 --width 18 \\
      --model qwen/qwen3-vl-235b-a22b-instruct \\
      --against anthropic/claude-opus-5 --output generated/confronto-modelli

ATTENZIONE AL COSTO: ogni modello indicato e' **una** richiesta a pagamento.
Con `--against` sono due. Il rilevamento locale non costa nulla. Le risposte
vengono messe in cache sotto la cartella di uscita: rieseguire lo stesso
confronto rigioca la cache invece di pagare di nuovo.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: Quanto conta uno scostamento di quota prima di essere segnalato. Non e' una
#: soglia di qualita': serve solo a decidere cosa vale la pena di stampare.
SIZE_REPORT_FRACTION = 0.15


def parse_arguments(argv=None):
    """@brief Legge la riga di comando del confronto.

    @param argv Argomenti, o None per quelli del processo.
    @return Namespace con foto, quote, modelli e cartella di uscita.
    """
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('photo', type=Path, help='Fotografia dall\'alto, non ritagliata.')
    parser.add_argument('--length', type=float, required=True,
                        help='Dimensione nota della scheda lungo X, in mm.')
    parser.add_argument('--width', type=float, required=True,
                        help='Dimensione nota della scheda lungo Y, in mm.')
    parser.add_argument('--model', required=True,
                        help='Id OpenRouter del modello da provare.')
    parser.add_argument('--against', default=None,
                        help='Secondo modello, sulle stesse proposte. Raddoppia il costo.')
    parser.add_argument('--yolo-model', type=Path, default=None,
                        help='Pesi ONNX del rilevatore locale; senza, quelli predefiniti.')
    parser.add_argument('--output', type=Path, default=ROOT/'generated'/'vision-model-comparison',
                        help='Cartella per rapporto, cache e audit.')
    parser.add_argument('--instructions', default='',
                        help='Note aggiuntive passate al modello, come nell\'applicazione.')
    return parser.parse_args(argv)


def inventory_with(model, photo, proposals, length, width, cache_dir, instructions, log):
    """@brief Esegue la riconciliazione globale con un modello, e nient'altro.

    @param model Id OpenRouter del modello.
    @param photo Fotografia calibrata (BGR).
    @param proposals Proposte del rilevatore, identiche fra i modelli.
    @param cache_dir Cartella di cache: una seconda esecuzione non ripaga.
    @return Le ipotesi di componente, nel formato dell'applicazione.
    @note La chiamata avviene **una volta sola** per modello, come in
        produzione: il costo del confronto e' prevedibile.
    """
    from kicadgen.services.openrouter import call_tool
    from kicadgen.services.photo_global_inventory import review_global_inventory
    from copy import deepcopy
    return review_global_inventory(
        photo, deepcopy(proposals), length, width, model, call_tool, log,
        cache_dir, extra_instructions=instructions or None)


def summarize(components):
    """@brief Riduce le ipotesi a cio' che si vuole confrontare.

    @param components Ipotesi restituite dalla riconciliazione globale.
    @return Dizionario per component_id con tipo, package, quote e orientamento.
    """
    rows = {}
    for component in components:
        rows[str(component.get('component_id') or component.get('name'))] = dict(
            klass=component.get('class') or component.get('kind'),
            package=component.get('package') or '',
            length_mm=round(float(component.get('length_mm') or 0), 3),
            width_mm=round(float(component.get('width_mm') or 0), 3),
            rotation_deg=component.get('_reported_rotation_deg',
                                       component.get('rotation_deg')),
            terminal_count=component.get('terminal_count'),
            landmark=component.get('asymmetry_landmark') or component.get('landmark_feature') or '',
            height_mm=component.get('height_mm'),
            confidence=component.get('confidence'),
        )
    return rows


def differences(left, right):
    """@brief Dove i due modelli non dicono la stessa cosa.

    @return Righe di differenza, una per componente e campo.
    @note Confronta per `component_id`: un componente che un modello non
        nomina affatto e' esso stesso una differenza, e va detto.
    """
    lines = []
    for key in sorted(set(left) | set(right)):
        a, b = left.get(key), right.get(key)
        if a is None or b is None:
            lines.append(f"{key}: presente solo in {'A' if b is None else 'B'}")
            continue
        for field in ('klass', 'package', 'rotation_deg', 'terminal_count', 'landmark'):
            if a[field] != b[field]:
                lines.append(f"{key}.{field}: A={a[field]!r} B={b[field]!r}")
        for field in ('length_mm', 'width_mm', 'height_mm'):
            first, second = a[field], b[field]
            if not (isinstance(first, (int, float)) and isinstance(second, (int, float))):
                continue
            reference = max(abs(first), abs(second))
            if reference and abs(first - second) / reference > SIZE_REPORT_FRACTION:
                lines.append(f"{key}.{field}: A={first} B={second}")
    return lines


def main(argv=None):
    """@brief Esegue il confronto e scrive il rapporto.

    @param argv Argomenti della riga di comando.
    @return Codice di uscita del processo.
    @throws SystemExit Se la fotografia non e' calibrabile.
    @note Ogni modello indicato costa una richiesta; la cache sotto la cartella
        di uscita evita di ripagare lo stesso confronto.
    """
    arguments = parse_arguments(argv)
    arguments.output.mkdir(parents=True, exist_ok=True)
    messages = []

    def log(message):
        messages.append(str(message))
        print(message, flush=True)

    from kicadgen.services.photo_inventory import prepare_photo
    from kicadgen.services.photo_yolo import detect_yolo_components

    calibration = {}
    photo = prepare_photo(arguments.photo, arguments.length, arguments.width, calibration)
    if photo is None:
        raise SystemExit('Foto non calibrabile: la scheda non e\' stata riconosciuta o e\' inclinata.')
    log(f'Calibrazione: {calibration}')
    # Un solo rilevamento per entrambi i modelli: e' la condizione perche' il
    # confronto dica qualcosa sul modello e non sul rilevatore.
    proposals = detect_yolo_components(photo, arguments.length, arguments.width,
                                       model_path=arguments.yolo_model, log=log,
                                       audit_dir=arguments.output/'yolo-audit',
                                       include_review_candidates=True)
    log(f'Proposte del rilevatore locale: {len(proposals)}.')

    report = {'photo': str(arguments.photo), 'board_mm': [arguments.length, arguments.width],
              'proposals': len(proposals), 'calibration': calibration, 'models': {}}
    summaries = {}
    for label, model in (('A', arguments.model), ('B', arguments.against)):
        if not model:
            continue
        log(f'--- {label}: {model}')
        components = inventory_with(model, photo, proposals, arguments.length, arguments.width,
                                    arguments.output/f'cache-{label}', arguments.instructions, log)
        summaries[label] = summarize(components)
        report['models'][label] = {'model': model, 'components': summaries[label]}
        log(f'{label}: {len(components)} ipotesi da {len(proposals)} proposte.')
        for name, row in sorted(summaries[label].items()):
            log(f"  {name:<18} {row['klass']:<14} {row['package']:<18} "
                f"{row['length_mm']}x{row['width_mm']} mm  rot={row['rotation_deg']}  "
                f"n={row['terminal_count']}  rif={row['landmark']}")

    if len(summaries) == 2:
        report['differences'] = differences(summaries['A'], summaries['B'])
        log(f"--- Differenze: {len(report['differences'])}")
        for line in report['differences']:
            log('  ' + line)

    report['log'] = messages
    destination = arguments.output/'comparison.json'
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'Rapporto: {destination}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
