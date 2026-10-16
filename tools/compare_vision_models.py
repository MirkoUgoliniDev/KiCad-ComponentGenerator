"""@brief Compare two vision models on the same set of YOLO proposals (paid API calls, cached).

Compares two vision models on the SAME set of YOLO proposals.

It answers one question only: which model recognises component type, sizes
and orientation better? To be able to say so, the only thing that changes
between the two runs must be the model: the photo, the calibration and the
proposals of the local detector are the same, detected once and reused.

Path exercised: `prepare_photo` -> `detect_yolo_components` ->
`review_global_inventory`, i.e. the production pipeline. There is no second
"trial" path that then behaves differently from the real one.

Examples (from the repository root, with OPENROUTER_API_KEY in the environment):

  python tools/compare_vision_models.py foto.png --length 25.5 --width 18 \\
      --model qwen/qwen3-vl-235b-a22b-instruct

  python tools/compare_vision_models.py foto.png --length 25.5 --width 18 \\
      --model qwen/qwen3-vl-235b-a22b-instruct \\
      --against anthropic/claude-opus-5 --output generated/confronto-modelli

MIND THE COST: every model named is **one** paid request. With `--against`
there are two. The local detection costs nothing. The answers are cached
under the output folder: re-running the same comparison replays the cache
instead of paying again.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: How much a dimension deviation counts before it is reported. It is not a
#: quality threshold: it only decides what is worth printing.
SIZE_REPORT_FRACTION = 0.15


def parse_arguments(argv=None):
    """@brief Read the command line of the comparison.

    @param argv Arguments, or None for those of the process.
    @return Namespace with photo, board sizes, models and output folder.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").replace("@brief ", "", 1),
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


def require_credentials():
    """@brief Load the .env and demand the API key BEFORE doing any work.

    @throws SystemExit When the key is missing.
    @note The application reads the .env at start-up; a command-line tool
        starts from a bare environment, and without this step the missing
        key was discovered only after minutes of local detection, in the
        shape of an empty answer that looked like an answer.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT/'.env')
    except ImportError:
        pass
    from kicadgen.services.openrouter import OpenRouterError, api_key
    try:
        api_key()
    except OpenRouterError as exc:
        raise SystemExit(str(exc))


def resolved_names(summary):
    """@brief The components the model actually named.

    @param summary Output of summarize.
    @return The set of names whose `resolved` flag is true.
    @note A proposal that comes back unchanged is not an answer of the
        model: it is the detector's proposal kept because nobody judged it.
        Counting it as an answer is what made two failed runs look as if
        they agreed.
    """
    return {name for name, row in summary.items() if row['resolved']}


def answered_or_stop(label, model, summary, components):
    """@brief Demand that the model has answered before going on.

    @param label Label of the run (A or B).
    @param model Model id, for the message.
    @param summary Output of summarize.
    @param components Raw hypotheses, from which the diagnostics are read.
    @return The reconciled names.
    @throws SystemExit When the model reconciled nothing.
    @note Without this stop the comparison went on, asked the second model
        too and declared «--- Differenze: 0» on two runs that had both failed:
        the worst possible verdict, because it looks like a result.
    """
    answered = resolved_names(summary)
    if answered:
        return answered
    errors = sorted({str(issue) for component in components
                     for issue in component.get('_global_inventory_errors', [])})
    raise SystemExit(f'{label} ({model}) non ha riconciliato nulla: '
                     + ('; '.join(errors) or 'nessuna diagnosi disponibile')
                     + '\nConfronto interrotto: nessun verdetto su una risposta assente.')


def inventory_with(model, photo, proposals, length, width, cache_dir, instructions, log):
    """@brief Run the global reconciliation with one model, and nothing else.

    @param model OpenRouter id of the model.
    @param photo Calibrated photograph (BGR).
    @param proposals Detector proposals, identical between the models.
    @param length Known board size along X, in mm.
    @param width Known board size along Y, in mm.
    @param cache_dir Cache folder: a second run does not pay again.
    @param instructions Extra instructions appended to the prompt, or an
        empty value for none.
    @param log Callable receiving each progress message.
    @return The component hypotheses, in the application's format.
    @note The call happens **once only** for each model, as in production:
        the cost of the comparison is predictable.
    """
    from kicadgen.services.openrouter import call_tool
    from kicadgen.services.photo_global_inventory import review_global_inventory
    from copy import deepcopy
    return review_global_inventory(
        photo, deepcopy(proposals), length, width, model, call_tool, log,
        cache_dir, extra_instructions=instructions or None)


def summarize(components):
    """@brief Reduce the hypotheses to what is to be compared.

    @param components Hypotheses returned by the global reconciliation.
    @return Dictionary by component_id with type, package, sizes and orientation.
    """
    rows = {}
    for component in components:
        # `component_id` is written by the reconciliation; a proposal kept because
        # unresolved keeps the detector's name instead.
        resolved = bool(component.get('component_id'))
        rows[str(component.get('component_id') or component.get('name'))] = dict(
            resolved=resolved,
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
    """@brief Where the two models do not say the same thing.

    @param left Summary of run A (output of summarize).
    @param right Summary of run B (output of summarize).
    @return Difference lines, one for each component and field; sizes are
        reported only beyond SIZE_REPORT_FRACTION of the larger value.
    @note Compares by `component_id`: a component that one model does not
        name at all is itself a difference, and has to be said.
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
    """@brief Run the comparison and write the report.

    @param argv Command-line arguments.
    @return Process exit code.
    @throws SystemExit When the key is missing, when the photograph cannot be
        calibrated, or when a model reconciles nothing.
    @note Every model named costs one request; the cache under the output
        folder avoids paying for the same comparison again.
    """
    arguments = parse_arguments(argv)
    # First of all: local detection takes minutes, and finding out only afterwards
    # that the key is missing means throwing them away.
    require_credentials()
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
    # A single detection for both models: it is the condition for the comparison
    # to say something about the model and not about the detector.
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
        answered = answered_or_stop(label, model, summaries[label], components)
        report['models'][label] = {'model': model, 'components': summaries[label],
                                   'resolved': len(answered), 'total': len(summaries[label])}
        log(f'{label}: {len(answered)} componenti riconciliati su {len(components)} '
            f'ipotesi, da {len(proposals)} proposte.')
        if len(answered) < len(summaries[label]):
            log(f'ATTENZIONE {label}: {len(summaries[label]) - len(answered)} proposte '
                'restano irrisolte e non entrano nel confronto.')
        for name, row in sorted(summaries[label].items()):
            log(f"  {name:<18} {row['klass']:<14} {row['package']:<18} "
                f"{row['length_mm']}x{row['width_mm']} mm  rot={row['rotation_deg']}  "
                f"n={row['terminal_count']}  rif={row['landmark']}")

    if len(summaries) == 2:
        report['differences'] = differences(
            {name: row for name, row in summaries['A'].items() if row['resolved']},
            {name: row for name, row in summaries['B'].items() if row['resolved']})
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
