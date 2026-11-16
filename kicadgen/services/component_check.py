"""@brief Read-only checks on disk artifacts and the current applied symbol."""
from dataclasses import dataclass, field, replace as _replace_fields
import functools
from functools import reduce
from pathlib import Path
from . import messages as _messages
import json
import operator
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace

from .system_dependencies import check_kicad_version, find_kicad_cli
from .kicad_library import (ComponentArtifacts, KiCadLibraryError, PREVIEW_FOOTPRINT_ORIGIN,
                            _atomic_write_text,
                            UTF8_BOM, create_preview_project, neutral_working_dir,
                            detach_3d_model, name_footprint, step_file_is_complete,
                            _decode_utf8, _direct_child_ranges, _node_arguments)
from ..core.footprint import KICAD_VERSION
from ..core.output_names import kicad_unsafe_characters
from ..generators.castellated_module import EDGE_LABELS
from ..generators.symbol import (SYMBOL_FORMAT_VERSION, primary_pin_names_enabled, symbol_inputs,
                                 serialize_symbol, symbol_property_positions)


#: Categories of a check: the document coherence (pinout against pads and
#: names), the technical file checks (KiCad reads, STEP, alignment), the
#: final reviews (electrical types, names left to the user) and the manual
#: checks the program cannot run (ERC, DRC, visual 3D).
CATEGORIES = ('document', 'technical', 'final', 'manual')

#: Importance of a check: 1 blocks the use of the component, 2 must be fixed
#: before production (a user decision or a difference left on purpose),
#: 3 belongs to the final review. The dialog's verdict is derived from
#: status, category and importance, never from a translated title.
IMPORTANCE_BLOCKS_USE = 1
IMPORTANCE_BEFORE_PRODUCTION = 2
IMPORTANCE_FINAL_REVIEW = 3

#: Stable ids of the automatic fixes a check can offer (`Check.action`). The
#: creator of the dialog maps them to callables; the check only names them.
ACTION_REEXPORT_SYMBOL = 'reexport_symbol'
#: Ask again the open «contact_nc» question (which switch terminal is
#: normally closed) and recompose the symbol from the answer.
ACTION_ANSWER_CONTACT_NC = 'answer_contact_nc'
#: Open the Pinout table to correct the NC/NO functions by hand.
ACTION_EDIT_PINOUT = 'edit_pinout'
#: Open the preview board and schematic of the check in KiCad's editors, to
#: see where a DRC/ERC violation is. A view: nothing changes, no re-check.
ACTION_SHOW_IN_KICAD = 'show_in_kicad'
#: Hand-offs: the dialog closes and the window shows the KiCad tab (an
#: installed library to refresh) or the Symbol tab (electrical types).
ACTION_OPEN_KICAD_TAB = 'open_kicad_tab'
ACTION_OPEN_SYMBOL_TAB = 'open_symbol_tab'

#: Codes of the checks that need kicad-cli: without it they are SKIP and the
#: dialog folds them into one «KiCad non trovato» row (`kicad_cli` is the
#: code of the row that says kicad-cli is missing).
CLI_CHECK_CODES = frozenset({'kicad_footprint_read', 'kicad_symbol_read', 'kicad_symbol_file_read',
                             'kicad_3d_import', 'drc', 'erc'})

#: Exit codes kicad-cli DECLARES (KiCad's common/cli/exit_codes.h): 0
#: success, 1 wrong arguments, 2 «unknown» failure, 3 invalid input file,
#: 4 output conflict, 5 DRC/ERC violations found (only with
#: --exit-code-violations, which the check does not pass).
CLI_EXIT_SUCCESS = 0
CLI_ERR_ARGS = 1
CLI_ERR_UNKNOWN = 2
CLI_ERR_INVALID_INPUT_FILE = 3
CLI_ERR_INVALID_OUTPUT_CONFLICT = 4
CLI_ERR_RC_VIOLATIONS = 5

#: The codes by which the three reads of the check (`fp export svg`, `sym
#: export svg`, `pcb export step --no-board-body`) MAY say that KiCad
#: refused their input. Observed with kicad-cli 10.0 on 2026-11-15, scratch
#: files: 3 for a .pretty / .kicad_sym / board that does not exist or cannot
#: be opened; 2 for a file that exists but does not load -- a footprint or a
#: symbol with an unknown token or a leading UTF-8 BOM («Unable to load
#: library»), and a STEP model KiCad cannot read («No valid PCB assembly»,
#: exit 2 even when the board body is exported and a file is written).
#: The SAME codes come from failures of the tool itself (round 2, measured
#: with kicad-cli 10.0 on valid inputs): 2 from `pcb export step` onto a
#: read-only output file or a folder in the output's place («Cannot rename
#: temporary file '$tempfile$.step'»), 2 from `fp/sym export svg` with the
#: input held exclusively by another process (the same «Unable to load
#: library» as a corrupt file), 2 from `sym export svg -o <folder that
#: cannot be created>`, 3 from `pcb export step` on a board held
#: exclusively by another process. Other codes seen: 1 a wrong argument, 4
#: from `fp export svg -o <folder that cannot be created>`; exit 0 with no
#: output from `fp export svg` into a folder that does not exist or for a
#: footprint name not in the library, and from `sym export svg` on a
#: library holding no symbol.
#: So a code in this set is only a CANDIDATE refusal: the check attributes
#: it to the component when it REPEATS on a fresh copy of the input into a
#: fresh output folder (`verify_component`, inner `run`); a tool failure that
#: does not repeat is not a verdict on the file. A refusal that repeats is
#: then tested against a DIFFERENTIAL CONTROL (round 3, `CONTROL_READS`):
#: the same kind of command on the program's own known-good file -- refused
#: too, the environment refuses fresh files (SKIP «environment»); read, the
#: defect is the component's (ERROR). An unbalanced or garbage
#: .kicad_mod made kicad-cli 10.0 HANG (no code at all): the check's own
#: parser (`_single_footprint`) refuses such a file before kicad-cli is run.
CLI_INPUT_REFUSED = frozenset({CLI_ERR_UNKNOWN, CLI_ERR_INVALID_INPUT_FILE})


def cli_read_verdict(returncode, produced):
    """@brief What one kicad-cli read/export says about the component's file.

    @param returncode Exit code of kicad-cli.
    @param produced True when the expected output exists and is not empty.
    @return ``'unreadable'`` when KiCad declared its input refused
        (`CLI_INPUT_REFUSED`): a candidate defect of the component, an
        ERROR only when a second run on fresh copies refuses again AND the
        differential control of the same kind is read (the same codes come
        from failures of the tool and of the environment, see
        `CLI_INPUT_REFUSED`).
        ``'read'`` for exit 0 with an output. ``'not_run'`` otherwise: any
        other declared code, a crash code (e.g. 0xC0000005), or exit 0
        without an output -- a failure of the checking tool, never a verdict
        on the file (review 2026-11-15, A2).
    @note Classified by the DECLARED code only, never by the log text, which
        is in KiCad's interface language. Observed with kicad-cli 10.0 on
        2026-11-15: `fp export svg` exits 0 without writing anything when it
        cannot write its output or does not find the footprint name in the
        library («no output» is then not a verdict on the file's content).
    """
    if returncode in CLI_INPUT_REFUSED:
        return 'unreadable'
    if returncode == CLI_EXIT_SUCCESS and produced:
        return 'read'
    return 'not_run'


def _exit_code_text(code):
    """An exit code as the user can look it up: a Windows status (crash) in hex, a small code in decimal."""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return str(code)
    return f'0x{value & 0xFFFFFFFF:08X}' if value < 0 or value > 255 else str(value)


#: Seconds kicad-cli gets for the first attempt of a read of the component.
CLI_READ_TIMEOUT_S = 60
#: Least seconds the repeat of a refused read gets (round 3, item 4): the
#: repeat gets only what the first attempt left of `CLI_READ_TIMEOUT_S`, so
#: a refusal after 59 s and a timeout do not add up to two minutes, but
#: never less than this, so a repeat after a slow refusal can still start
#: KiCad (a cold start of kicad-cli 10 takes a few seconds).
CLI_RETRY_MIN_TIMEOUT_S = 15
#: Seconds the differential control (`CONTROL_READS`) gets: its input is a
#: few lines, so a kicad-cli that needs longer is not answering.
CLI_CONTROL_TIMEOUT_S = 30
#: Seconds of a check run after which a read that TIMED OUT gets no
#: differential control (round 4, item 1): a budget bound, not a
#: classification threshold. A timeout already cost `CLI_READ_TIMEOUT_S`;
#: before round 4 four reads that all time out cost 4 x 60 s, and a control
#: per kind would add up to 3 x 30 s more. Past this point the timeout stays
#: a SKIP «timeout» without a verdict, exactly as before round 4; a control
#: of the same kind already made in the run is reused whatever the time
#: (it costs nothing). Twice the read budget leaves room for the first
#: timeout's control after the version check and the footprint read.
CLI_TIMEOUT_CONTROL_LIMIT_S = 2 * CLI_READ_TIMEOUT_S + CLI_CONTROL_TIMEOUT_S
#: How many times `CLI_READ_TIMEOUT_S` the EXTENDED read gets (round 5,
#: item 1): a read that timed out while the differential control of its kind
#: was read is run once more with this longer budget before the timeout is
#: blamed on the component. The control proved that kicad-cli starts and
#: reads a file of this kind under the same conditions, so what is left to
#: tell apart is a file that is merely LARGE (a STEP model of tens of MB
#: imported on a slow PC) from one that keeps KiCad busy for good. A bound of
#: patience, not a calibration: four times the normal budget (4 minutes) is
#: how long a user can be asked to wait for one file of a check; a sound file
#: that needs longer still is declared slower than the check accepts (an
#: ERROR the remedy explains), and a hanging file never answers anyway.
CLI_SLOW_READ_FACTOR = 4
#: Seconds of the extended read (round 5, item 1).
CLI_SLOW_READ_TIMEOUT_S = CLI_SLOW_READ_FACTOR * CLI_READ_TIMEOUT_S
#: Extended reads one check run may make (round 5, item 1): the declared cap
#: of the whole run. One is enough to judge a slow file and keeps the worst
#: case of a run bounded at `CLI_SLOW_READ_TIMEOUT_S` more than before round
#: 5, however many reads time out; a further timeout whose control reads
#: gets no extended read and stays a SKIP «timeout» without a verdict (the
#: tool's, as before round 4), never an ERROR on the 60 s criterion alone.
CLI_SLOW_READS_PER_RUN = 1

#: Clock of the read budgets; a module attribute so tests can fake it.
_clock = time.monotonic

#: Name of the symbol, footprint and board of the differential control.
CONTROL_NAME = 'KiCadGenControl'

#: Known-good minimal inputs of the DIFFERENTIAL CONTROL (round 3, item 1),
#: one per kind of kicad-cli read: {kind: (relative input path, text, args
#: builder (input, output) -> arguments, output pattern, output file name)}.
#: When the component's file is refused twice, the same kind of command is
#: run on this input under the same scratch conditions (`verify_component`):
#: refused too -> the environment refuses fresh files (an antivirus holding
#: new files in %TEMP%, a broken KiCad configuration), never the component.
#: Verified with kicad-cli 10.0 on 2026-11-15 (scratch files): each reads
#: with exit 0 and writes its output -- `fp export svg` one SVG, `sym export
#: svg` one SVG, `pcb export step --no-board-body` a STEP holding the
#: control model's solid.
#: Literals on purpose, not the program's serializers: a regression of a
#: generator must not make a sound control fail and turn a defective
#: component into «environment». The FORMAT VERSION is the one exception
#: (round 4, item 5): each control carries the version header the program's
#: files of that kind carry (`KICAD_VERSION` for footprints and the preview
#: board, `SYMBOL_FORMAT_VERSION` for symbols), so a KiCad too old for the
#: format refuses the control too -- the environment, not the component.
#: CONTRACT CHANGED 2026-11-15 (round 4, item 4): the STEP control used to
#: export the BOARD BODY of a board without a model, which proved the board
#: read and the STEP write but not a model import; it now holds one
#: footprint whose model is a tiny known-good STEP (`CONTROL_STEP_TEXT`,
#: written beside the board, `CONTROL_EXTRA_FILES`) and runs with the same
#: ``--no-board-body`` as the component's 3D import, so an environment that
#: breaks model loading fails the control too. Measured with kicad-cli 10.0
#: on 2026-11-15: that board exports with exit 0 and a MANIFOLD_SOLID_BREP;
#: the same board with an empty STEP model exits 2 («No valid PCB
#: assembly»), like a component whose model KiCad cannot load.
CONTROL_READS = {
    'fp': (Path(f'{CONTROL_NAME}.pretty') / f'{CONTROL_NAME}.kicad_mod',
           f'(footprint "{CONTROL_NAME}"\n  (version {KICAD_VERSION})\n  (generator "kicadgen")\n'
           '  (layer "F.Cu")\n'
           '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))\n)\n',
           lambda source, output: ['fp', 'export', 'svg', '--footprint', CONTROL_NAME, '-o', output,
                                   source.parent],
           '*.svg', None),
    'sym': (Path(f'{CONTROL_NAME}.kicad_sym'),
            f'(kicad_symbol_lib\n  (version {SYMBOL_FORMAT_VERSION})\n  (generator "kicadgen")\n'
            f'  (symbol "{CONTROL_NAME}"\n    (in_bom yes)\n    (on_board yes)\n'
            '    (property "Reference" "U" (at 0 3.81 0) (effects (font (size 1.27 1.27))))\n'
            f'    (property "Value" "{CONTROL_NAME}" (at 0 -3.81 0) (effects (font (size 1.27 1.27))))\n'
            f'    (symbol "{CONTROL_NAME}_0_1"\n'
            '      (rectangle (start -2.54 2.54) (end 2.54 -2.54) (stroke (width 0.254) (type default))'
            ' (fill (type none)))\n    )\n  )\n)\n',
            lambda source, output: ['sym', 'export', 'svg', '-o', output, source],
            '*.svg', None),
    'step': (Path(CONTROL_NAME) / f'{CONTROL_NAME}.kicad_pcb',
             f'(kicad_pcb\n  (version {KICAD_VERSION})\n  (generator "kicadgen")\n'
             '  (general (thickness 1.6))\n'
             '  (paper "A4")\n  (layers\n    (0 "F.Cu" signal)\n    (2 "B.Cu" signal)\n'
             '    (25 "Edge.Cuts" user)\n  )\n'
             f'  (footprint "{CONTROL_NAME}" (layer "F.Cu") (at 5 5)\n'
             '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))\n'
             f'    (model "${{KIPRJMOD}}/{CONTROL_NAME}.step" (offset (xyz 0 0 0))'
             ' (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))\n  )\n'
             '  (gr_rect (start 0 0) (end 10 10) (stroke (width 0.1) (type default)) (fill no)'
             ' (layer "Edge.Cuts"))\n)\n',
             lambda source, output: ['pcb', 'export', 'step', '--no-board-body', '-o', output, source],
             None, f'{CONTROL_NAME}.step'),
}


def _box_step_text(size_mm):
    """The ISO 10303-21 (AP214) text of one axis-aligned box solid, corner at the origin, mm.

    Built from its 8 corners: 12 straight edges, 6 planar faces whose loops
    run counter-clockwise seen from outside, one closed shell, one manifold
    solid, millimetre units -- the smallest solid a STEP reader must load.
    Private to the differential control: not the program's 3D generator
    (see `CONTROL_READS`, «literals on purpose»).
    """
    entities = []

    def entity(text):
        entities.append(text)
        return f'#{len(entities)}'

    def number(value):
        # STEP reals always carry a decimal point («1.», «0.5»).
        text = repr(float(value))
        return text[:-1] if text.endswith('.0') else text

    def point(xyz):
        return entity(f"CARTESIAN_POINT('',({','.join(number(c) for c in xyz)}))")

    def direction(xyz):
        return entity(f"DIRECTION('',({','.join(number(c) for c in xyz)}))")

    def unit(start, end):
        delta = [e - s for s, e in zip(start, end)]
        length = math.sqrt(sum(c * c for c in delta))
        return [c / length for c in delta], length

    dx, dy, dz = size_mm
    corners = [((i & 1) * dx, ((i >> 1) & 1) * dy, ((i >> 2) & 1) * dz) for i in range(8)]
    vertices = [entity(f"VERTEX_POINT('',{point(corner)})") for corner in corners]
    # (corner indices counter-clockwise seen from outside, outward normal).
    faces = (((0, 2, 3, 1), (0, 0, -1)), ((4, 5, 7, 6), (0, 0, 1)), ((0, 1, 5, 4), (0, -1, 0)),
             ((2, 6, 7, 3), (0, 1, 0)), ((0, 4, 6, 2), (-1, 0, 0)), ((1, 3, 7, 5), (1, 0, 0)))
    edges = {}

    def oriented_edge(a, b):
        key = (min(a, b), max(a, b))
        if key not in edges:
            start, end = corners[key[0]], corners[key[1]]
            along, length = unit(start, end)
            vector = entity(f"VECTOR('',{direction(along)},{number(length)})")
            line = entity(f"LINE('',{point(start)},{vector})")
            edges[key] = entity(f"EDGE_CURVE('',{vertices[key[0]]},{vertices[key[1]]},{line},.T.)")
        return entity(f"ORIENTED_EDGE('',*,*,{edges[key]},{'.T.' if a < b else '.F.'})")

    face_ids = []
    for loop, normal in faces:
        sides = [oriented_edge(loop[k], loop[(k + 1) % 4]) for k in range(4)]
        edge_loop = entity("EDGE_LOOP('',(" + ','.join(sides) + '))')
        bound = entity(f"FACE_OUTER_BOUND('',{edge_loop},.T.)")
        reference, _length = unit(corners[loop[0]], corners[loop[1]])
        placement = entity(f"AXIS2_PLACEMENT_3D('',{point(corners[loop[0]])},{direction(normal)},"
                        f"{direction(reference)})")
        plane = entity(f"PLANE('',{placement})")
        face_ids.append(entity(f"ADVANCED_FACE('',({bound}),{plane},.T.)"))
    shell = entity("CLOSED_SHELL('',(" + ','.join(face_ids) + '))')
    solid = entity(f"MANIFOLD_SOLID_BREP('{CONTROL_NAME}',{shell})")
    origin = entity(f"AXIS2_PLACEMENT_3D('',{point((0, 0, 0))},{direction((0, 0, 1))},{direction((1, 0, 0))})")
    length_unit = entity('( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) )')
    angle_unit = entity('( NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.) )')
    solid_angle_unit = entity('( NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT() )')
    uncertainty = entity(f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),{length_unit},"
                      "'distance_accuracy_value','confusion accuracy')")
    context = entity(f'( GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT(({uncertainty})) '
                  f'GLOBAL_UNIT_ASSIGNED_CONTEXT(({length_unit},{angle_unit},{solid_angle_unit})) '
                  "REPRESENTATION_CONTEXT('','') )")
    representation = entity(f"ADVANCED_BREP_SHAPE_REPRESENTATION('',({origin},{solid}),{context})")
    application = entity("APPLICATION_CONTEXT('core data for automotive mechanical design processes')")
    entity(f"APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,{application})")
    product_context = entity(f"PRODUCT_CONTEXT('',{application},'mechanical')")
    product = entity(f"PRODUCT('{CONTROL_NAME}','{CONTROL_NAME}','',({product_context}))")
    formation = entity(f"PRODUCT_DEFINITION_FORMATION('','',{product})")
    definition_context = entity(f"PRODUCT_DEFINITION_CONTEXT('part definition',{application},'design')")
    definition = entity(f"PRODUCT_DEFINITION('design','',{formation},{definition_context})")
    shape = entity(f"PRODUCT_DEFINITION_SHAPE('','',{definition})")
    entity(f"SHAPE_DEFINITION_REPRESENTATION({shape},{representation})")
    data = ''.join(f'#{index} = {text};\n' for index, text in enumerate(entities, 1))
    return ('ISO-10303-21;\nHEADER;\n'
            f"FILE_DESCRIPTION(('{CONTROL_NAME}'),'2;1');\n"
            f"FILE_NAME('{CONTROL_NAME}.step','2026-11-15T00:00:00',(''),(''),'','','');\n"
            "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\nENDSEC;\nDATA;\n"
            + data + 'ENDSEC;\nEND-ISO-10303-21;\n')


#: The known-good STEP model of the 3D-import control: a 1 x 1 x 1 mm box.
CONTROL_STEP_TEXT = _box_step_text((1.0, 1.0, 1.0))

#: Files a control needs BESIDE its main input (`CONTROL_READS`), keyed by
#: kind: {relative path inside the attempt's input folder: text}. The STEP
#: control's board names its model through ${KIPRJMOD}, the board's folder,
#: exactly as the component's preview board does.
CONTROL_EXTRA_FILES = {
    'step': {Path(CONTROL_NAME) / f'{CONTROL_NAME}.step': CONTROL_STEP_TEXT},
}


def _output_produced(output, pattern):
    """True when a read's expected output exists and is not empty.

    `output` is a folder searched with `pattern`, or the output file when
    `pattern` is None. An OSError (an antivirus quarantining the file) goes
    to the caller: a failure on the checking side, never a verdict.
    """
    files = list(Path(output).rglob(pattern)) if pattern else [Path(output)]
    return any(path.is_file() and path.stat().st_size for path in files)


#: Conditions under which a blocking-capable check applies to one run
#: (`BlockingCheck.applies`; every condition listed must hold). Each one is
#: a fact the caller of `required_for_install` declares:
#: «cli» a kicad-cli was available (`find_kicad_cli`), «step» the component
#: folder holds its STEP, «symbol_file» it holds its saved ``<name>.kicad_sym``,
#: «document» a document pinout was given (`document_check_applies`),
#: «contacts» the applied rows declare switch functions (`declares_contacts`),
#: «pad_leads» declared terminals were given (`pad_leads` non-empty).
CONDITIONS = ('cli', 'step', 'symbol_file', 'document', 'contacts', 'pad_leads')


@dataclass(frozen=True)
class BlockingCheck:
    """@brief One check that can emit an ERROR, declared with when it applies and what concludes it.

    @param applies The `CONDITIONS` that must all hold for the check to run
        and be able to block (empty: every run).
    @param concluded_by Codes of the rows whose emission proves the check
        reached its outcome: its own single conclusion row (an OK, or the
        ERROR rows it publishes FIRST), a row emitted in its place when it
        could not be made (e.g. `footprint_missing`), and the rows that are
        always published AFTER the whole check in `verify_component` (the
        next stage's first row, the run's last row `manual_checks`). A
        check whose rows interleave statuses (a per-model loop, a list of
        DRC violations) is concluded only by a later row. Exact codes, or a
        family written ``prefix*`` (the ERC violation rows are
        ``erc_<violation type>``, round 4, item 3).
    @param errors The ERROR codes the check emits: exact codes, or a family
        written ``prefix*`` (the DRC rows are ``drc_<violation type>``).
    @param own The codes (or families) among `concluded_by` that are the
        check's OWN outcome rows, as opposed to rows published after it. An
        own row concludes the check only when it IS an outcome: an OK, WARN
        or ERROR, or a SKIP when `skip_concludes`; a row declared
        `Check.file_busy` never does (round 4, item 7: the check was not
        made now). A later row concludes it whatever its status.
    @param skip_concludes True when a SKIP of an own row is a legitimate
        outcome of the check (KiCad's reads: a tool failure is judged by
        the window's read rules, `required_reads`); False when a SKIP means
        the check was not made (the footprint file held by another program,
        pin-pad without a readable footprint).
    @param needs Keys of `BLOCKING_CHECKS` that must be concluded for this
        check to be: the checks of a stage that runs only after its
        footprint was read (`footprint_file`) are not concluded by later
        rows when that read itself was not made (round 4, item 7).
    """
    applies: frozenset
    concluded_by: frozenset
    errors: frozenset
    own: frozenset = frozenset()
    skip_concludes: bool = True
    needs: frozenset = frozenset()


#: The statuses of a row that is an outcome of the check it belongs to.
_OUTCOME_STATUSES = frozenset({'OK', 'WARN', 'ERROR'})


def _code_matches(code, pattern):
    """True when a row code is `pattern`, or belongs to the family ``prefix*`` it names."""
    return code.startswith(pattern[:-1]) if pattern.endswith('*') else code == pattern


#: The last row of every `verify_component` run: once it is emitted, every
#: check of the run has reached its outcome.
_RUN_END = frozenset({'manual_checks'})
#: First rows of the footprint stage: published after the KiCad version check.
_FOOTPRINT_STAGE_START = frozenset({'footprint_bom', 'footprint_file', 'footprint_missing'})
#: Rows that close the footprint stage: KiCad's read of the footprint is its
#: last row (a SKIP without kicad-cli too), or the footprint does not exist.
_FOOTPRINT_STAGE_END = frozenset({'kicad_footprint_read', 'footprint_missing'})
#: Rows that close the symbol stage: the read of the saved symbol file, the
#: row that says it is missing, or the stage's own failure row.
_SYMBOL_STAGE_END = frozenset({'kicad_symbol_file_read', 'symbol_export_missing', 'symbol_error'})
#: First rows of the STEP stage, always published after the document and
#: contact checks: no STEP, the STEP's integrity, or its failure.
_STEP_STAGE_START = frozenset({'step_missing', 'step_integrity', 'step_model'})


def _blocking(applies, concluded_by, errors, *, own=(), skip_concludes=True, needs=(), run_end=True):
    """A `BlockingCheck` from plain iterables.

    The run's last row concludes it unless `run_end` is False: a check whose
    own row is always published when the check is made, and whose SKIP or
    absence means it was NOT made (round 4, item 7), must not be concluded
    merely because the run ended. The own codes are added to `concluded_by`.
    """
    concluded = frozenset(concluded_by) | frozenset(own) | (_RUN_END if run_end else frozenset())
    return BlockingCheck(frozenset(applies), concluded, frozenset(errors), frozenset(own),
                         skip_concludes, frozenset(needs))


#: Every check of `verify_component` that can emit an ERROR, which forbids
#: the installation (review 2026-11-15, round 3, item 2). Keyed by the
#: check's code. `required_for_install` returns the keys that apply to a
#: run, `concluded_required` those a (possibly interrupted) run concluded.
#: CONTRACT CHANGED 2026-11-15 (round 3): the set used to be hand-picked
#: (five codes) and missed checks that block -- the document pinout, the
#: NC/NO contact roles, the 3D import, the applied symbol's read; a run
#: interrupted before them could be judged «crashed» (installation allowed)
#: with an ERROR never reached. Now every ERROR site of this module is here
#: or in `ERROR_CODES_NOT_REQUIRED`, enforced by an AST scan in
#: tests/test_component_check_r3_required_table.py.
#: CONTRACT CHANGED 2026-11-15 (round 4, item 7): a footprint held by
#: another program gave a SKIP «file in uso» that concluded `footprint_file`,
#: and the run's last row concluded everything else, so the footprint stage
#: (pads, terminals, model links, KiCad's read) and pin-pad counted as
#: concluded although never made. Now `footprint_file` and `pin_pad` are
#: concluded only by a real outcome of their own row (never a SKIP, never the
#: run's end), the saved symbol's read never by a «file in uso» SKIP, and
#: the checks that need the footprint read (`needs`) stay unconcluded with it.
BLOCKING_CHECKS = {
    # Run start: a kicad-cli older than the minimum.
    'kicad_version': _blocking({'cli'}, {'kicad_version'} | _FOOTPRINT_STAGE_START,
                               {'kicad_version'}),
    # Footprint stage.
    # Reading the footprint file: parsed (OK), refused (ERROR), absent
    # (`footprint_missing`, emitted in its place). Its SKIP «file in uso»
    # means the file was not read now: not an outcome, and the run's end
    # does not stand in for it.
    'footprint_file': _blocking((), {'footprint_missing'}, {'footprint_file', 'footprint_missing'},
                                own={'footprint_file'}, skip_concludes=False, run_end=False),
    'footprint_pads_overlap': _blocking((), {'footprint_pads_overlap', 'footprint_geometry'}
                                        | _FOOTPRINT_STAGE_END, {'footprint_pads_overlap'},
                                        needs={'footprint_file'}),
    'lead_hole': _blocking({'pad_leads'}, {'lead_hole', 'lead_hole_contradicted'} | _FOOTPRINT_STAGE_END,
                           {'lead_hole'}, needs={'footprint_file'}),
    # One row per model node and per transform, statuses interleaved: only
    # the end of the stage concludes it.
    'model_link': _blocking((), _FOOTPRINT_STAGE_END, {'model_link'}, needs={'footprint_file'}),
    # A SKIP of KiCad's read (a tool failure, kicad-cli missing) is its
    # outcome, judged by the window's read rules (`required_reads`).
    'kicad_footprint_read': _blocking({'cli'}, {'footprint_missing'}, {'kicad_footprint_read'},
                                      own={'kicad_footprint_read'}, needs={'footprint_file'}),
    # Symbol stage.
    # Pin-pad: its SKIP «footprint not readable» compared nothing; a
    # `symbol_error` (the applied symbol cannot be built) or a
    # `footprint_missing` (no footprint to compare with, an ERROR that
    # blocks on its own) stands in for it.
    'pin_pad': _blocking((), {'symbol_error', 'footprint_missing'}, {'pin_pad'}, own={'pin_pad'},
                         skip_concludes=False, run_end=False),
    'kicad_symbol_read': _blocking({'cli'}, _SYMBOL_STAGE_END, {'kicad_symbol_read'},
                                   own={'kicad_symbol_read'}),
    # The saved symbol's read: a SKIP «file in uso» (`Check.file_busy`) is
    # not its outcome; the file gone meanwhile (`symbol_export_missing`) is.
    'kicad_symbol_file_read': _blocking({'cli', 'symbol_file'},
                                        {'symbol_error', 'symbol_export_missing'},
                                        {'kicad_symbol_file_read'},
                                        own={'kicad_symbol_file_read'}, run_end=False),
    'symbol_error': _blocking((), _SYMBOL_STAGE_END, {'symbol_error'}),
    # Document and contact checks, published before the STEP stage.
    'document_dropped': _blocking({'document'}, _STEP_STAGE_START, {'document_dropped'}),
    'document_unnamed': _blocking({'document'}, _STEP_STAGE_START, {'document_unnamed'}),
    # The sides' contact count was unusable, the pinout not compared
    # (pinout_coherence.FINDING_COUNT_UNUSABLE, review 2026-11-16).
    'document_count_unusable': _blocking({'document'}, _STEP_STAGE_START,
                                         {'document_count_unusable'}),
    'pin_role_contradiction': _blocking({'contacts'}, {'pin_role_contradiction'} | _STEP_STAGE_START,
                                        {'pin_role_contradiction'}),
    # STEP stage.
    # The STEP's integrity: OK, or `step_model` (incomplete, unreadable) in its place.
    'step_integrity': _blocking({'step'}, {'step_integrity', 'step_model'}, {'step_model'}),
    # The preview board holds the footprint read above: with that read not
    # made (`footprint_file` unconcluded) the import was not made either.
    'kicad_3d_import': _blocking({'cli', 'step'}, {'step_model', 'footprint_missing'},
                                 {'kicad_3d_import'}, own={'kicad_3d_import'},
                                 needs={'footprint_file'}),
    # DRC of the preview: a real contact between two pads is an ERROR,
    # published among the other violations, so only the ERC rows after it (or
    # the end) conclude it. CONTRACT CHANGED 2026-11-15 (round 4, item 3):
    # the ERC publishes ONE row `erc` (OK or SKIP) or one row per violation
    # group coded `erc_<violation type>`; only the former concluded the DRC,
    # so a run with ERC violations that stopped before its last row left the
    # DRC unconcluded. The family `erc_*` concludes it too.
    'drc': _blocking({'cli'}, {'drc', 'erc', 'erc_*', 'kicad_library'}, {'drc_*'},
                     needs={'footprint_file'}),
}

#: ERROR codes of this module that are NOT a requirement, each with the
#: structural reason (round 3, item 2). The AST scan of the tests accepts an
#: ERROR code only when it is in a `BlockingCheck.errors` or here.
ERROR_CODES_NOT_REQUIRED = {
    # An invalid component name is the run's ONLY row: the run returns at
    # once, so no interruption can fall between the check and its ERROR.
    'component_name': 'emitted alone, the run ends with it',
    # `erc_checks` passes no contact rule to `_violation_groups`, so no ERC
    # group is ever of the «zero» class: `_violation_checks` gives ERC rows
    # WARN only (statically the shared code could say ERROR).
    'erc_*': 'erc_checks never classifies a violation as a contact',
}

#: The codes of every blocking-capable check (all conditions true).
REQUIRED_FOR_INSTALL = frozenset(BLOCKING_CHECKS)

#: KiCad's reads of the files an installation COPIES (the footprint and the
#: saved symbol file): what the window requires KiCad to have READ (OK or
#: WARN), as opposed to merely concluded. `required_reads` applies them.
INSTALLED_FILE_READS = frozenset({'kicad_footprint_read', 'kicad_symbol_file_read'})


def document_check_applies(document):
    """@brief True when `document_checks` can judge this document (the «document» condition).

    @param document The `document` argument of `verify_component`.
    @return True for a side-numbered layout with at least one pinout entry:
        exactly the case in which `document_checks` emits a row (and may
        emit «document_dropped» / «document_unnamed»).
    """
    if not isinstance(document, dict) or document.get('layout', 'sides') != 'sides':
        return False
    return any(str((entry or {}).get('pin', '')).strip() for entry in document.get('pinout') or ()
               if isinstance(entry, dict) or entry is None)


def declares_contacts(rows):
    """@brief True when the applied rows declare a switch function (the «contacts» condition).

    @param rows Applied symbol rows.
    @return True when a row's function is NC, NO or COM as the relay symbol
        reads it (`symbol_relay.pin_function`): exactly the rows
        `pin_role_checks` judges.
    """
    from ..generators.symbol_relay import pin_function
    return any(pin_function(row) in ('nc', 'no', 'common') for row in rows or () if isinstance(row, dict))


def required_for_install(*, cli_available, has_step, has_symbol_file, has_document=False,
                         has_contacts=False, has_pad_leads=False):
    """@brief The blocking-capable checks (`BLOCKING_CHECKS`) that apply to one check run.

    @param cli_available True when the check had a kicad-cli to run (the
        window's `find_kicad_cli` result): without it the KiCad reads, the
        version check and the DRC are never made, and are not required.
    @param has_step True when the component folder holds its STEP model.
    @param has_symbol_file True when the component folder holds its saved
        ``<name>.kicad_sym``.
    @param has_document True when a document pinout was given
        (`document_check_applies` of the `document` argument); False by
        default, so a caller that passes no document requires no document check.
    @param has_contacts True when the applied rows declare switch functions
        (`declares_contacts` of the rows); False by default.
    @param has_pad_leads True when declared terminals were given (the
        `pad_leads` argument non-empty); False by default.
    @return A frozenset of check codes (keys of `BLOCKING_CHECKS`).
    @note CONTRACT CHANGED 2026-11-15 (round 3, item 2): every check that can
        block is returned when it applies, not five hand-picked ones; the
        new keyword-only flags default to False, so existing callers still
        get a correct (if smaller) set.
    """
    facts = {'cli': cli_available, 'step': has_step, 'symbol_file': has_symbol_file,
             'document': has_document, 'contacts': has_contacts, 'pad_leads': has_pad_leads}
    return frozenset(code for code, entry in BLOCKING_CHECKS.items()
                     if all(facts[condition] for condition in entry.applies))


def required_reads(*, cli_available, has_symbol_file):
    """@brief KiCad's reads of the installed files that must have READ them (OK or WARN).

    @param cli_available See `required_for_install`.
    @param has_symbol_file See `required_for_install`.
    @return The codes of `INSTALLED_FILE_READS` that apply: none without
        kicad-cli, the footprint read, plus the saved symbol file's read when
        that file exists.
    @note Distinct from `required_for_install` (round 3): the applied
        symbol's read (`kicad_symbol_read`) can block and must be concluded,
        but the installation does not copy that text, so it is not a read
        the window waits for.
    """
    if not cli_available:
        return frozenset()
    return frozenset({'kicad_footprint_read'} | ({'kicad_symbol_file_read'} if has_symbol_file else set()))


def concluded_required(results, *, cli_available, has_step, has_symbol_file, has_document=False,
                       has_contacts=False, has_pad_leads=False):
    """@brief Which of the checks an installation needs a (possibly interrupted) run concluded.

    @param results The `Check` rows the run emitted, in any order.
    @param cli_available See `required_for_install`.
    @param has_step See `required_for_install`.
    @param has_symbol_file See `required_for_install`.
    @param has_document See `required_for_install`.
    @param has_contacts See `required_for_install`.
    @param has_pad_leads See `required_for_install`.
    @return The set of required codes (`required_for_install`) for which a
        concluding row (`BlockingCheck.concluded_by`) was emitted: a later
        row whatever its status, an own row as an outcome -- an OK, WARN or
        ERROR, and a SKIP «not run» of KiCad's reads too, a declared outcome
        the window judges by its usual rules (an ERROR blocks, a run where
        KiCad read nothing is «unread»), exactly as in a run that ended
        normally.
    @note For the window's «crashed» state (review 2026-11-15, round 2, R1):
        a run that stopped on an internal failure may be treated as
        «crashed» (installation allowed, outcome incomplete) only when this
        set equals `required_for_install(...)`; otherwise something the
        installation needs was not verified and the state is «failed». Read
        on the declared `Check.code` only, never on a translated text.
    @note A required check that never ran because an earlier one failed
        (no KiCad read of a footprint that did not parse) stays missing here;
        that run holds an ERROR, which blocks the installation on its own.
    @note CONTRACT CHANGED 2026-11-15 (round 4, items 3 and 7): a row
        concludes a check through a code or a declared family
        (``prefix*``); a check's OWN row concludes it only as an outcome
        (`BlockingCheck.own`, `skip_concludes`; never a `Check.file_busy`
        row), and a check is concluded only when the checks it `needs` are.
        So a footprint held by another program leaves `footprint_file`, its
        stage and `pin_pad` unconcluded even in a run that ended.
    """
    rows = [result for result in results or () if isinstance(getattr(result, 'code', None), str)]

    def concludes(entry, row):
        """True when `row` concludes the check declared by `entry`."""
        if not any(_code_matches(row.code, pattern) for pattern in entry.concluded_by):
            return False
        if not any(_code_matches(row.code, pattern) for pattern in entry.own):
            return True
        if getattr(row, 'file_busy', False):
            return False
        status = getattr(row, 'status', '')
        return status in _OUTCOME_STATUSES or (status == 'SKIP' and entry.skip_concludes)

    concluded = {code for code, entry in BLOCKING_CHECKS.items()
                 if any(concludes(entry, row) for row in rows)}
    # A check whose prerequisite was not concluded is not either; repeated
    # until stable (the `needs` graph is acyclic and shallow).
    while True:
        dropped = {code for code in concluded if not BLOCKING_CHECKS[code].needs <= concluded}
        if not dropped:
            break
        concluded -= dropped
    required = required_for_install(cli_available=cli_available, has_step=has_step,
                                    has_symbol_file=has_symbol_file, has_document=has_document,
                                    has_contacts=has_contacts, has_pad_leads=has_pad_leads)
    return required & concluded


def missing_required(results, *, cli_available, has_step, has_symbol_file, has_document=False,
                     has_contacts=False, has_pad_leads=False):
    """@brief The required checks a run did NOT conclude (`required_for_install` minus `concluded_required`).

    @param results The `Check` rows the run emitted.
    @param cli_available See `required_for_install`.
    @param has_step See `required_for_install`.
    @param has_symbol_file See `required_for_install`.
    @param has_document See `required_for_install`.
    @param has_contacts See `required_for_install`.
    @param has_pad_leads See `required_for_install`.
    @return A frozenset of check codes; empty when the run concluded every one.
    """
    flags = dict(cli_available=cli_available, has_step=has_step, has_symbol_file=has_symbol_file,
                 has_document=has_document, has_contacts=has_contacts, has_pad_leads=has_pad_leads)
    return frozenset(required_for_install(**flags) - concluded_required(results, **flags))


#: DRC violation types that come from the PREVIEW board, not from the
#: footprint: no net (unconnected items) and the preview's own footprint
#: library (lib_footprint_*). Never shown. The other entries CANNOT OCCUR on
#: the preview -- one footprint (courtyards_overlap, duplicate_footprints),
#: no schematic parity run (schematic_parity, extra_footprint,
#: missing_footprint, footprint_symbol_mismatch), no copper zone
#: (isolated_copper, starved_thermal) -- and are listed so that a future
#: kicad-cli default never turns them into rows about the preview.
DRC_PREVIEW_ARTEFACTS = frozenset({
    'unconnected_items', 'lib_footprint_issues', 'lib_footprint_mismatch',
    'schematic_parity', 'courtyards_overlap', 'duplicate_footprints', 'extra_footprint',
    'missing_footprint', 'footprint_symbol_mismatch', 'isolated_copper', 'starved_thermal'})
#: Findings about libraries and the drawing grid: no manufacturing rule, so
#: their rows carry no «KiCad's default rules, not your manufacturer's» note.
LIBRARY_TYPES = frozenset({'lib_symbol_issues', 'lib_symbol_mismatch', 'footprint_link_issues',
                           'endpoint_off_grid', 'lib_footprint_issues', 'lib_footprint_mismatch'})
#: Edge types: an artefact only when the violation names the preview's OWN
#: outline (its uuid); an Edge.Cuts drawing of the footprint stays visible.
DRC_EDGE_TYPES = frozenset({'copper_edge_clearance', 'silk_edge_clearance', 'invalid_outline'})
#: DRC types between two pads that `footprint_pads_overlap` already reports
#: when it named the same pad pair: shown once, by the program's own rule.
DRC_OVERLAP_TYPES = frozenset({'clearance', 'shorting_items', 'solder_mask_bridge'})
#: ERC types of a symbol placed alone on the preview sheet: its pins are not
#: connected (counted in the OK row) and its power inputs have no driver
#: (said in the manual row: the user's schematic must drive them).
ERC_PREVIEW_ARTEFACTS = frozenset({'pin_not_connected', 'power_pin_not_driven'})

#: Italian sentence of the violation types the user meets on a footprint or a
#: symbol. Any other type is shown with KiCad's own description, which is in
#: KiCad's interface language.
VIOLATION_TEXTS = {
    'clearance': 'Rame troppo vicino ad altro rame',
    'shorting_items': 'Elementi di rame in cortocircuito',
    'solder_mask_bridge': 'Ponte di solder mask fra pad',
    'annular_width': 'Anello di rame del foro troppo sottile',
    'drill_out_of_range': 'Foro fuori dai limiti di KiCad',
    'hole_to_hole': 'Fori troppo vicini fra loro',
    'hole_clearance': 'Foro troppo vicino al rame',
    'malformed_courtyard': 'Courtyard non chiuso o malformato',
    'missing_courtyard': 'Courtyard mancante',
    'text_height': 'Testo troppo basso',
    'text_thickness': 'Tratto del testo troppo sottile',
    'silk_over_copper': 'Serigrafia sopra il rame',
    'silk_overlap': 'Serigrafie sovrapposte',
    'padstack': 'Pad non valido',
    'pin_to_pin': 'Tipi elettrici dei pin in conflitto',
    'duplicate_pins': 'Numeri di pin duplicati',
    'endpoint_off_grid': 'Pin fuori dalla griglia',
    'lib_symbol_issues': 'Simbolo diverso da quello della libreria',
    'lib_symbol_mismatch': 'Simbolo diverso da quello della libreria',
    'footprint_link_issues': 'Footprint del simbolo non trovato',
}


class LocalizedStr(str):
    """@brief A text rendered in the current language that remembers how to render itself again.

    @param text The rendered text (what `str` methods see).
    @param spec How to render it again, or None: ``('tx', template, ((key,
        value), ...))`` for a `tx` message (values may be LocalizedStr
        themselves), ``('concat', (part, ...))`` for texts joined with ``+``,
        ``('join', separator, (item, ...))`` for `_join`, ``('call', obj)``
        for an object with its own ``localized()`` (a `SymbolError`).
    @note A `str` on purpose: every consumer that only shows, compares or
        hashes the text is unaffected; `localized` reads `spec` to recompose
        the text after a language switch (review 2026-11-15, M3). ``+`` with
        another str keeps the recipe (``__radd__`` of a str subclass runs
        before ``str.__add__``); other str methods return plain text, which
        then simply stays in the language it was written in.
    """

    spec = None

    def __new__(cls, text, spec=None):
        """@brief A rendered text with its recipe.

        @param text The rendered text.
        @param spec The recipe (see the class), None for none; a default so
            that pickle and deepcopy, which call ``cls(text)``, work.
        @return The new LocalizedStr.
        """
        obj = super().__new__(cls, text)
        obj.spec = spec
        return obj

    def __add__(self, other):
        """@brief Concatenation that keeps both recipes.

        @param other Another str.
        @return A LocalizedStr whose recipe is the concatenation.
        """
        if not isinstance(other, str):
            return NotImplemented
        return LocalizedStr(str.__add__(self, other), ('concat', (self, other)))

    def __radd__(self, other):
        """@brief ``plain + localized``: keeps the recipe of the right operand.

        @param other A str on the left.
        @return A LocalizedStr whose recipe is the concatenation.
        """
        if not isinstance(other, str):
            return NotImplemented
        return LocalizedStr(str.__add__(other, self), ('concat', (other, self)))


def tx(source, /, **values):
    """@brief `messages.tx` whose result also remembers its template and values.

    @param source Italian template (the catalog key, as for `messages.tx`).
    @param values Placeholder values; LocalizedStr values are rendered again
        with the message.
    @return A `LocalizedStr` equal to `messages.tx(source, **values)`.
    @note Every message of this module goes through it, so `localized` can
        recompose a Check in the language active when it is SHOWN, not the one
        active when it was produced.
    """
    return LocalizedStr(_messages.tx(source, **values), ('tx', source, tuple(values.items())))


def _join(separator, items):
    """``separator.join(items)`` that keeps the recipes of translated items.

    A plain str when no item carries a recipe (pin numbers, file names).
    """
    items = tuple(items)
    text = separator.join(items)
    if any(isinstance(item, LocalizedStr) and item.spec is not None for item in items):
        return LocalizedStr(text, ('join', separator, items))
    return text


def _render(value):
    """A value rendered again in the current language: a recipe is re-run, any other value is kept."""
    spec = value.spec if isinstance(value, LocalizedStr) else None
    if spec is None:
        return value
    try:
        kind = spec[0]
        if kind == 'tx':
            return tx(spec[1], **{key: _render(item) for key, item in spec[2]})
        if kind == 'concat':
            return reduce(operator.add, [_render(part) for part in spec[1]])
        if kind == 'join':
            return _join(spec[1], [_render(item) for item in spec[2]])
        if kind == 'call':
            return LocalizedStr(str(spec[1].localized()), spec)
    except Exception:  # noqa: BLE001 - a broken recipe or catalog keeps the text as written
        return value
    return value


#: The Check fields that `localized` recomposes.
_LOCALIZED_FIELDS = ('title', 'detail', 'remedy')


@dataclass(frozen=True)
class Check:
    """@brief Carry one verification outcome with status, title and diagnostic detail.

    @param status `OK`, `ERROR`, `WARN` or `SKIP`.
    @param title Translated title.
    @param detail Translated detail.
    @param category One of `CATEGORIES`; `technical` by default.
    @param importance 1, 2 or 3 (see `IMPORTANCE_*`); 3 by default. An
        ERROR always blocks the use whatever this value says.
    @param remedy Translated remedy (what to do and where), '' when the
        title and detail already say it.
    @param action Stable id of an automatic fix (`ACTION_*`), or None.
    @param code Stable id of the kind of check (e.g. `electrical_type`),
        so that the dialog groups and recognises checks without comparing
        translated texts; '' for an unnamed kind.
    @param pins Pin or pad numbers the check concerns, as text; the dialog
        compresses them into ranges.
    @param needs_cli True for a check skipped only because kicad-cli is missing.
    @param cli_failure Why kicad-cli delivered no reading on a SKIP row:
        'start', 'timeout', 'not_run', 'environment', 'scratch'; on an
        ERROR row of a read, 'component' (see the field); '' otherwise.
    @param file_busy True on a SKIP made because a component file is held
        by another program (see the field).
    @param cli_exit_code The non-refusal exit code of the kicad-cli process
        that made a read row SKIP, None otherwise (see the field).
    @note `texts` is derived, never passed: for each of title, detail and
        remedy produced by `tx` (a `LocalizedStr`), its recipe -- the Italian
        template and its values, ``('tx', template, ((key, value), ...))``,
        or a concatenation/join of such recipes. A text that did not come
        from `tx` (a kicad-cli log, a path) has no entry. It is left out of
        equality and hash, so two checks with the same texts stay equal;
        `localized` uses it to recompose the texts in the current language.
    """
    status: str
    title: str
    detail: str
    category: str = 'technical'
    importance: int = IMPORTANCE_FINAL_REVIEW
    remedy: str = ''
    action: str | None = None
    code: str = ''
    pins: tuple = ()
    #: True for a SKIP only because kicad-cli is missing: the dialog folds
    #: these into one «KiCad non trovato» row (structural, never the text).
    needs_cli: bool = False
    #: Why kicad-cli did not deliver a reading on a SKIP row: 'start' (the
    #: program could not be launched, OSError), 'timeout' (no answer in
    #: time), 'not_run' (any other tool-side failure), 'environment' (KiCad
    #: refused the component's file twice AND refused the program's own
    #: known-good control file of the same kind: the computer refuses fresh
    #: files, round 3 item 1), 'scratch' (round 4, item 6: the check's OWN
    #: scratch side failed -- its temporary output folder not writable, the
    #: input copy not openable, or kicad-cli's output there not readable: a
    #: temporary folder or an antivirus, never KiCad and never the
    #: component); '' otherwise. On an ERROR row of a read, 'component'
    #: (round 4, item 1): kicad-cli failed on the component's file without
    #: declaring it refused -- a crash, exit 0 without output, another exit
    #: code, no answer in time -- and read the program's own known-good
    #: control file of the same kind under the same conditions, so the
    #: failure is the file's. Declared so the window tells a persistent cause
    #: (a kicad-cli that never starts) from a transient one, and the tool's
    #: failure from the component's, without reading translated text (review
    #: 2026-11-15, R5).
    cli_failure: str = ''
    #: The exit code of the kicad-cli process whose failure made a read row
    #: SKIP, when that code is NOT an input refusal (`CLI_INPUT_REFUSED`):
    #: another declared code, a crash code (e.g. 0xC0000005 as 3221225477),
    #: or 0 for an exit 0 that wrote nothing; None on every other row (a
    #: timeout, a start failure, a refusal, a read). Declared for the
    #: window's persistent-failure signature «the same non-input exit code on
    #: every read in two consecutive runs» (round 3, item 5).
    cli_exit_code: int | None = None
    #: True on a SKIP made because a file of the component exists but
    #: another program holds it (`_ComponentFileBusy`: the footprint, the
    #: saved symbol): the check was not made NOW, which a repeat may change.
    #: Such a row never concludes the check it belongs to
    #: (`concluded_required`, round 4, item 7). False on every other row.
    file_busy: bool = False
    #: {field name: recipe} of the translated texts (see the class note).
    texts: dict | None = field(default=None, init=False, compare=False, repr=False)

    def __post_init__(self):
        """@brief Record the recipes of the translated texts.

        @note Derived from the fields at every construction, `dataclasses.replace`
            included: a field replaced by plain text loses its recipe, one
            replaced by a `LocalizedStr` brings its own.
        """
        texts = {name: getattr(self, name).spec for name in _LOCALIZED_FIELDS
                 if isinstance(getattr(self, name), LocalizedStr)
                 and getattr(self, name).spec is not None}
        object.__setattr__(self, 'texts', texts or None)


def localized(check):
    """@brief A check with its translated texts recomposed in the CURRENT interface language.

    @param check A `Check` (any object without `texts` is returned as it is).
    @return A new Check whose title, detail and remedy that came from `tx`
        are rendered again through `messages.tx` with the same values; texts
        that did not come from `tx` (raw kicad-cli logs, paths) stay as they
        are. The same check when it has no translated text.
    @note For the panel, which shows checks produced before a language
        switch (review 2026-11-15, M3). Never raises: a recipe that cannot
        be rendered keeps its text.
    """
    texts = getattr(check, 'texts', None)
    if not texts:
        return check
    return _replace_fields(check, **{name: _render(getattr(check, name)) for name in texts})


def pin_ranges(values):
    """@brief Pin numbers as compact ranges («1–4, 7, A1»).

    @param values Pin numbers as text.
    @return The canonical ASCII decimals joined into ranges, then every
        other value as written, sorted, comma separated.
    @note Only a canonical ASCII decimal joins a range: «01» is another pin
        than «1» (KiCad compares pad numbers as text) and must not collapse
        into it, and a Unicode digit such as «²» passes `str.isdigit` but
        not `int`.
    """
    values = [str(value) for value in values]

    def canonical(value):
        return value.isascii() and value.isdigit() and str(int(value)) == value
    numeric = sorted({int(value) for value in values if canonical(value)})
    other = sorted({value for value in values if not canonical(value)})
    parts = []
    if numeric:
        start = previous = numeric[0]
        for value in numeric[1:]:
            if value == previous + 1:
                previous = value
                continue
            parts.append(str(start) if start == previous else f'{start}–{previous}')
            start = previous = value
        parts.append(str(start) if start == previous else f'{start}–{previous}')
    return ', '.join(parts + other)


def _label_relation():
    """The shared side-label rule, `pinout_coherence.label_relation`.

    Imported lazily: the check module stays importable on its own, and the
    rule lives in ONE place (docs/pinout-coherence-plan.md, rule 2).
    """
    from .pinout_coherence import label_relation
    return label_relation


def document_checks(document, rows, pad_ids):
    """@brief Checks of the symbol and footprint against the document pinout.

    @param document Dict with `pinout` (list of {pin, name}), `side_labels`
        (list of (pad, label) read on the board's sides), `user_names` (set
        of pin numbers whose name the user typed), `decision`
        (`renumber`, `keep_sides` or None: the answer to the side numbering
        question), `open_question` (True while that question is open),
        `layout` ("sides" for a module whose pads are numbered along its
        sides, anything else for another layout; missing reads as "sides")
        `off_side_pins` (pinout entries DECLARED on no side, e.g. a numbered
        exposed pad; may be missing), `unexplained_pins` (entries without a
        pad that nobody declared off the sides while the numbering is not
        shifted; may be missing), `shifted` (True when the side numbering
        is shifted against the pinout with positive evidence:
        `pinout_coherence.side_pinout_report` «contradicted») and
        `numbers_printed` (False when the documents print no pin numbers;
        missing reads as printed) and `label_conflicts` ([{"edge", "pins"}]:
        sides whose labels strongly put the pinout names on other contacts,
        `pinout_coherence.FINDING_LABEL_PLACEMENT`; may be missing) and
        `count_unusable` (`side_pinout_report(...)["count_unusable"]`:
        None, or {"total", "limit"} when the sides' contact count is not
        usable -- one blocking «document_count_unusable» row, total None
        shown «?»; may be missing).
    @param rows Applied symbol rows ({pin, name, ...}).
    @param pad_ids Electrical pad numbers of the footprint on disk, or None
        when it was not readable (the symbol's pin numbers stand in: the
        symbol pins are the footprint pads).
    @return A list of `Check`, category `document`.
    @note Only STRUCTURAL findings block use (ERROR importance 1): a
        pinout entry without a pad (the numbering is shifted) and a symbol
        pin left unnamed where the document names it; after the user chose
        `keep_sides` they are WARN importance 2 naming the choice. A side
        label `different` from the pinout name is a HEURISTIC
        (`pinout_coherence.label_relation`, common abbreviations): at most a
        WARN importance 2 asking to verify, never a block (generality audit
        2026-11-13, H3). A name the user typed that differs from the
        document is a WARN of importance 2. `unknown` relations never yield
        a finding.
    @note Without printed pin numbers (`numbers_printed` False) the
        extractor applied a fixed conventional numbering: the caller passes
        the CONVENTION rows (same frame as the sides), the normal checks run,
        a final-review note says the numbering is conventional, and no claim
        about the manufacturer's numbers is made (no shifted numbering, no
        dropped entry: an entry without a pad is unexplained, with a remedy
        that does not mention the side-numbering question).
    @note Side-based only (audit M1): the rules and remedies speak of the
        sides' numbering, so another layout gets no document check here. A
        pinout entry on no side (`off_side_pins`, e.g. an exposed pad) is not
        a dropped pin: without a pad in the footprint it is to be fixed
        before production (WARN importance 2, a missing electrical pad, L6);
        present, a final-review note (importance 3); never an error.
    """
    if not document or document.get('layout', 'sides') != 'sides':
        return []
    # Without printed pin numbers the extractor numbered the contacts by a
    # fixed convention (user decision 2026-11-13): the document's rows and the
    # sides are both in that frame (audit HIGH 1), so the normal checks run;
    # only claims about the MANUFACTURER's numbers are suppressed (no shifted
    # numbering, no dropped entry), and a note says the numbering is
    # conventional.
    convention = document.get('numbers_printed') is False
    relation = _label_relation()
    results = []
    if convention:
        results.append(Check(
            'WARN', tx('Numerazione dei pin'),
            tx("Il documento non stampa i numeri dei pin: numerazione convenzionale (primo "
               "contatto incontrato girando in senso antiorario dall'angolo in alto a sinistra), "
               "non quella del costruttore."),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Prima della produzione confronta la posizione del pin 1 e il verso della '
               'numerazione con la scheda reale o con lo schema del produttore.'),
            ACTION_EDIT_PINOUT, 'document_numbering_convention'))
    pinout = {}
    for entry in document.get('pinout') or ():
        pin = str((entry or {}).get('pin', '')).strip()
        if pin:
            pinout.setdefault(pin, str(entry.get('name') or '').strip())
    names = {str(row.get('pin', '')): str(row.get('name') or '').strip() for row in rows}
    pads = {str(p) for p in pad_ids} if pad_ids is not None else set(names)
    user_names = {str(pin) for pin in (document.get('user_names') or ())}
    kept = document.get('decision') == 'keep_sides'
    choice = tx('Scelta dell’utente: numerazione dei lati mantenuta.') + ' ' if kept else ''

    def contradiction(title, detail, remedy, code, pins):
        if kept:
            results.append(Check('WARN', title, choice + detail, 'document',
                                 IMPORTANCE_BEFORE_PRODUCTION,
                                 tx('Hai scelto di mantenere la numerazione dei lati: confronta il '
                                    'componente con il documento prima della produzione.'),
                                 None, code, tuple(pins)))
        else:
            results.append(Check('ERROR', title, detail, 'document', IMPORTANCE_BLOCKS_USE,
                                 remedy, None, code, tuple(pins)))

    renumber = tx('Rispondi alla domanda sulla numerazione dei lati, oppure correggi i numeri dei pin '
                  'nella scheda «Simbolo elettrico» confrontandoli con il pinout del documento.')
    # pinout_coherence.FINDING_COUNT_UNUSABLE (review 2026-11-16): the sides
    # declare more contacts than `count_unusable` accepts (or an infinite
    # count), so the side/pinout comparison was NOT made. Blocking like the
    # other structural document errors: an absent comparison must never read
    # as a passed one.
    unusable = document.get('count_unusable')
    if unusable:
        total = unusable.get('total') if isinstance(unusable, dict) else None
        limit = unusable.get('limit') if isinstance(unusable, dict) else None
        contradiction(tx('Pinout del documento'),
                      tx('I lati dichiarano {total} contatti, oltre il limite di {limit}: il '
                         'conteggio non è utilizzabile, il pinout non è stato confrontato. '
                         'Controlla il numero di contatti dei lati.',
                         total='?' if total is None else total,
                         limit='?' if limit is None else limit),
                      tx('Correggi il numero di contatti dei lati del modulo (o ripeti l’analisi), '
                         'poi ripeti la verifica.'),
                      'document_count_unusable', ())
    off_side = {str(pin) for pin in (document.get('off_side_pins') or ())}
    # A shifted numbering (positive evidence) drops its entries: structural, it
    # blocks. Without that evidence an entry without a pad is unexplained; by
    # convention numbers nothing is ever «shifted» (no manufacturer numbers).
    if convention:
        unexplained = {pin for pin in pinout if pin not in pads and pin not in off_side}
    else:
        unexplained = (set() if document.get('shifted') else
                       {str(pin) for pin in (document.get('unexplained_pins') or ())})
    # Only a SHIFTED numbering drops entries (structural, blocks use); entries
    # nobody explained are a review item of their own below.
    dropped = [pin for pin in pinout
               if pin not in pads and pin not in off_side and pin not in unexplained]
    if dropped:
        contradiction(tx('Pinout del documento'),
                      tx('Voci del pinout senza pad nel footprint: {pins}.', pins=pin_ranges(dropped)),
                      renumber, 'document_dropped', dropped)
    # FINDING_LABEL_PLACEMENT: a side's labels strongly place the names on
    # other contacts -- to review before use, listed once with its pins; the
    # plain label differences below skip the pins it already names.
    placed = set()
    for conflict in document.get('label_conflicts') or ():
        pins = [str(pin) for pin in conflict.get('pins') or ()]
        if not pins:
            continue
        placed.update(pins)
        edge = str(conflict.get('edge') or '')
        results.append(Check(
            'WARN', tx('Etichette dei lati'),
            choice + tx("Etichette del lato {label} che mettono i nomi del pinout su altri contatti "
                        "(pin {pins}): nomi lasciati come li ha dati l'analisi, da rivedere prima "
                        "dell'uso.", label=tx(EDGE_LABELS.get(edge, edge)), pins=pin_ranges(pins)),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Verifica sul documento i pad indicati: etichetta del lato e nome del pinout sono '
               'confrontati con una regola approssimata, che può sbagliare sulle abbreviazioni.'),
            None, 'document_label_placement', tuple(pins)))
    differing = []
    for pad, label in document.get('side_labels') or ():
        pad = str(pad)
        if pad in pinout and pad not in placed and relation(label, pinout[pad]) == 'different':
            differing.append((pad, str(label), pinout[pad]))
    if differing:
        # A heuristic comparison (H3): never a block, always «verify».
        pairs = '; '.join(f'{pad} «{label}» / «{name}»' for pad, label, name in differing)
        results.append(Check(
            'WARN', tx('Etichette dei lati'),
            choice + tx('Etichetta sul lato diversa dal nome del pinout per lo stesso pad '
                        '(pad «lato» / «pinout»): {pairs}.', pairs=pairs),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Verifica sul documento i pad indicati: etichetta del lato e nome del pinout sono '
               'confrontati con una regola approssimata, che può sbagliare sulle abbreviazioni.'),
            None, 'document_side_label', tuple(pad for pad, _l, _n in differing)))
    lost = [pin for pin in pinout if pin in unexplained and pin not in pads]
    if lost:
        # FINDING_UNEXPLAINED_ENTRY: no pad and no declaration -- maybe a pad
        # off the sides, maybe a missing pad; not a proof, so not a block.
        results.append(Check(
            'WARN', tx('Pinout del documento'),
            choice + tx('Voci del pinout senza pad e non dichiarate fuori dai lati: {pins}.',
                        pins=pin_ranges(lost)),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            # In convention mode no side-numbering question exists (audit MEDIUM 2).
            tx("Verifica sul documento se è un pad (per esempio esposto) e aggiungilo, oppure "
               "togli la voce.") if convention else
            tx('Verifica sul documento se queste voci sono pad fuori dai lati (per esempio un pad '
               'esposto) o pad mancanti nel footprint, poi rispondi alla domanda sulla numerazione '
               'dei lati.'),
            None, 'document_unexplained', tuple(lost)))
    missing_off_side = [pin for pin in pinout if pin in off_side and pin not in pads]
    if missing_off_side:
        # L6: a numbered pad declared off the sides that the footprint lacks is
        # a missing electrical pad: to fix before production.
        results.append(Check(
            'WARN', tx('Pinout del documento'),
            tx('Voci del pinout fuori dai lati (per esempio un pad esposto) senza pad nel '
               'footprint: {pins}.', pins=pin_ranges(missing_off_side)),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Verifica sul documento se queste voci sono pad del componente da aggiungere al '
               'footprint.'),
            None, 'document_off_side', tuple(missing_off_side)))
    present_off_side = [pin for pin in pinout if pin in off_side and pin in pads]
    if present_off_side:
        # The same entries present as pads: only a note for the final review.
        results.append(Check(
            'WARN', tx('Pinout del documento'),
            tx('Voci del pinout fuori dai lati presenti come pad nel footprint: {pins}.',
               pins=pin_ranges(present_off_side)),
            'document', IMPORTANCE_FINAL_REVIEW,
            tx('Verifica sul documento la posizione di questi pad fuori dai lati.'),
            None, 'document_off_side_present', tuple(present_off_side)))
    unnamed = [pin for pin, name in names.items() if not name and pinout.get(pin)]
    if unnamed:
        contradiction(tx('Nomi dei pin'),
                      tx('Pin del simbolo senza nome che il documento nomina: {pins}.',
                         pins=pin_ranges(unnamed)),
                      tx('Nella scheda «Simbolo elettrico» dai ai pin indicati il nome del pinout del '
                         'documento, poi Applica.'),
                      'document_unnamed', unnamed)
    # A name that reads as a remark of the model instead of the printed
    # signal (pinout_coherence.FINDING_NAME_REMARK, a declared heuristic):
    # left as it is and flagged before production (user, 2026-11-13). Read on
    # the APPLIED names, so editing them in the symbol tab clears it.
    from .pinout_coherence import name_remarks
    # Names the user typed are theirs (audit L2): never flagged as remarks.
    remarked = [(pin, name) for pin, name in names.items()
                if pin not in user_names and name_remarks(name)]
    if remarked:
        pairs = '; '.join(f'{pin} «{name}»' for pin, name in remarked)
        results.append(Check(
            'WARN', tx('Nomi dei pin'),
            tx('Nomi dei pin che sembrano commenti e non il solo nome del segnale stampato: '
               '{pairs}.', pairs=pairs),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Nella scheda «Simbolo elettrico» lascia nel nome solo il segnale come è stampato '
               'sul documento (le note vanno altrove), poi Applica. La regola che li riconosce è '
               'approssimata.'),
            None, 'document_name_remark', tuple(pin for pin, _n in remarked)))
    typed = [(pin, name, pinout[pin]) for pin, name in names.items()
             if pin in user_names and name and pinout.get(pin)
             and relation(name, pinout[pin]) == 'different']
    if typed:
        pairs = '; '.join(f'{pin} «{name}» / «{doc}»' for pin, name, doc in typed)
        results.append(Check(
            'WARN', tx('Nomi dei pin'),
            tx('Nomi scritti a mano diversi dal documento (pin «simbolo» / «documento»): {pairs}.',
               pairs=pairs),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Confronta i nomi indicati con il pinout del documento nella scheda «Simbolo '
               'elettrico»: li hai scritti a mano e restano come li hai scritti.'),
            None, 'document_user_name', tuple(pin for pin, _n, _d in typed)))
    if document.get('open_question'):
        results.append(Check(
            'WARN', tx('Domanda aperta'),
            tx('La domanda sulla numerazione dei lati non ha ancora una risposta.'),
            'document', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Rispondi alla domanda sulla numerazione dei lati e ripeti la verifica.'),
            None, 'document_open_question'))
    if (not results or (convention and len(results) == 1)) and pinout:
        results.append(Check('OK', tx('Pinout del documento'),
                             tx('{count} voci del pinout coerenti con pad e nomi.', count=len(pinout)),
                             'document', code='document_coherent'))
    return results


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


def _file_copper_pads(text):
    """The pads of a .kicad_mod text as the objects the copper rules read (`footprint_checks`).

    @param text The footprint file, already validated pad by pad (`_pad_geometry`).
    @return One namespace per pad with the attributes `footprint_checks.pad_copper`
        and `check_pads_overlap` read: number, pad_type, shape, at, rotation,
        size, layers, rect_delta, roundrect_rratio and drill_offset (the
        `(drill ... (offset dx dy))` that moves a castellated pad's copper),
        plus `hole`: (side along the pad length, side across) mm, None
        without a drill.
    @note A «custom» pad is checked through its anchor only (`pad_copper`
        reads any unknown shape as a rectangle of its size): its primitives
        are not read, a declared limit -- an overlap of a primitive is missed,
        never invented.
    """
    pads = []
    for node in _children(text, 'pad'):
        args = _node_arguments(node)
        at = [float(v) for v in _node_arguments(_children(node, 'at')[0])]
        size = [float(v) for v in _node_arguments(_children(node, 'size')[0])[:2]]
        layers = _node_arguments(_children(node, 'layers')[0])
        delta = _children(node, 'rect_delta')
        delta = tuple(float(v) for v in _node_arguments(delta[0])[:2]) if delta else None
        offset, hole = (0.0, 0.0), None
        for drill in _children(node, 'drill'):
            offsets = _children(drill, 'offset')
            if offsets:
                offset = tuple(float(v) for v in _node_arguments(offsets[0])[:2])
            values = [v for v in _node_arguments(drill) if v != 'oval']
            try:
                sides = [float(v) for v in values[:2]]
            except ValueError:
                sides = []
            if sides:
                # (side along the pad length, side across): round holes repeat it.
                hole = (sides[0], sides[1] if len(sides) > 1 else sides[0])
        pads.append(SimpleNamespace(number=args[0], pad_type=args[1], shape=args[2],
                                    at=(at[0], at[1]), rotation=at[2] if len(at) > 2 else 0.0,
                                    size=tuple(size), layers=layers, rect_delta=delta,
                                    roundrect_rratio=None, drill_offset=offset, hole=hole))
    return pads


def pin_role_checks(rows):
    """@brief Pins named after one switch role whose declared function is another.

    @param rows Applied symbol rows ({"pin", "name", "function", ...}).
    @return One ERROR (importance 1) per contradicting pin, with the action
        that opens the Pinout table; [] when no pin contradicts.
    @note Real night run 2026-11-14: a pin named «NC» declared normally
        open drew the contact the wrong way in the relay symbol. The role
        words are the ONE vocabulary of `core.contact_roles` (NC/NO/COM,
        NA/NF, the long forms, a contact number), shared with the
        extraction. Only rows whose function is a switch role are judged
        (`role_conflicts`), so an IC pin named «NC» (not connected) never is.
    """
    from ..core.contact_roles import role_conflicts
    from ..generators.symbol_relay import pin_function
    # The declared function normalised as the relay symbol reads it.
    entries = [dict(row, function=pin_function(row)) for row in rows or () if isinstance(row, dict)]
    results = []
    for conflict in role_conflicts(entries):
        pin, function = conflict['pin'], conflict['function']
        results.append(Check('ERROR', tx('Funzioni NC/NO'), tx(
            'Nome del pin {pin} ({name}) in contrasto con la funzione ({function}).',
            pin=pin, name=str(conflict['name']),
            function={'nc': 'NC', 'no': 'NO', 'common': 'COM'}[function]),
            'technical', IMPORTANCE_BLOCKS_USE,
            tx('Correggi il nome (o la funzione) del pin nella tabella Pinout.'),
            ACTION_EDIT_PINOUT, 'pin_role_contradiction', (pin,)))
    return results


def _file_holes(text):
    """{pad number: (side along the pad length, side along its width)} of the holed pads of a .kicad_mod."""
    holes = {}
    for node in _children(text, 'pad'):
        args = _node_arguments(node)
        drills = _children(node, 'drill')
        if len(args) < 2 or not drills:
            continue
        values = _node_arguments(drills[0])
        try:
            sides = ((float(values[1]), float(values[2])) if values[:1] == ['oval']
                     else (float(values[0]), float(values[0])))
        except (IndexError, ValueError):
            continue
        holes.setdefault(str(args[0]), sides)
    return holes


def _lead_entry(entry):
    """(lead, fit, source) of one `pad_leads` value: a lead mapping, or {"lead", "fit", "source"}."""
    if isinstance(entry, dict) and "lead" in entry:
        return entry.get("lead"), entry.get("fit"), entry.get("source")
    return entry, None, None


def lead_hole_checks(text, pad_leads):
    """@brief Pads whose hole IN THE FILE does not let the declared terminal pass.

    @param text The .kicad_mod text on disk.
    @param pad_leads {pad number: entry} of the applied package, where an
        entry is {"lead": lead mapping, "fit": `PinSpec.lead_fit`, "source":
        the pad's provenance of the lead} (a bare lead mapping is read as
        {"lead": it}); {} or None when no terminal is declared.
    @return [] when no declared terminal meets a hole in the file;
        otherwise, in this order: one ERROR (importance 1) naming the pads
        whose terminal does not pass; one WARN (importance 2) naming the pads
        whose terminal does not pass but whose lead reading the user's chosen
        hole contradicts (provenance «contradicted»: the terminal is the
        doubtful value, not the hole); one OK naming how many pads were
        checked when none fails -- so the absence of an error is visible.
    @note The rule is `tht_estimates.lead_passes` (a round hole larger than
        the lead's circumscribed diameter, a slot the lead fits in):
        structural, no threshold. A press-fit pad (`lead_fit` «press_fit»)
        is meant to be tighter than its terminal and is not judged.
    @note Real run 2026-11-14 (SRD relay): every declared terminal fitted,
        and the check said nothing at all, so the user could not tell it had
        run; the OK row fixes that.
    """
    from ..core.tht_estimates import lead_passes
    if not pad_leads:
        return []
    holes = _file_holes(text)
    blocked, contradicted, checked = [], [], 0
    for number, entry in pad_leads.items():
        lead, fit, source = _lead_entry(entry)
        hole = holes.get(str(number))
        if not lead or hole is None or fit == "press_fit":
            continue
        try:
            passes = lead_passes(lead, hole[0], hole[1])
        except ValueError:
            continue
        checked += 1
        if passes:
            continue
        (contradicted if source == "contradicted" else blocked).append(str(number))
    results = []
    if blocked:
        results.append(Check('ERROR', tx('Geometria del footprint'), tx(
            'I terminali dei pad {pads} non entrano nei fori.', pads=', '.join(blocked)),
            'technical', IMPORTANCE_BLOCKS_USE,
            tx('Correggi il foro dei pad in «Piazzole del componente THT» (o nel dialog del pad), '
               'poi applica e ripeti la verifica.'), None, 'lead_hole', tuple(blocked)))
    if contradicted:
        results.append(Check('WARN', tx('Geometria del footprint'), tx(
            'Pad {pads}: terminale letto contraddetto dal foro scelto dall\'utente.',
            pads=', '.join(contradicted)),
            'technical', IMPORTANCE_BEFORE_PRODUCTION,
            tx('Controlla sul disegno la sezione del terminale: il foro e\' quello scelto; se il '
               'terminale letto e\' giusto, correggi il foro in «Piazzole del componente THT».'),
            None, 'lead_hole_contradicted', tuple(contradicted)))
    if checked and not results:
        results.append(Check('OK', tx('Geometria del footprint'), tx(
            'Terminali dichiarati nei fori: {count} pad controllati, tutti passano.', count=checked),
            'technical', code='lead_hole'))
    return results


def footprint_geometry_checks(text):
    """@brief The geometric rules of the footprint tab run on a footprint FILE: copper overlaps.

    @param text The .kicad_mod text (validated by `_pad_geometry`).
    @return One ERROR `Check` (importance 1, «NON USARE») per pair of pads
        with different numbers whose copper overlaps or touches, with the
        rule's remedy; one OK `Check` when no pair does.
    @note Real run 2026-11-14 (an RS485 module): 16 mm pads with copper
        overlapping between two pads got «UTILIZZABILE CON RISERVE» because
        the check ran no footprint geometry. The rule is the footprint tab's
        own (`footprint_checks.check_pads_overlap`: pads sharing one number
        are one pin and may overlap), so the preview and the check agree.
    """
    from .footprint_checks import check_pads_overlap
    pads = _file_copper_pads(text)
    results = []
    for finding in check_pads_overlap(SimpleNamespace(pads=pads)):
        results.append(Check('ERROR', tx('Geometria del footprint'), finding.message, 'technical',
                             IMPORTANCE_BLOCKS_USE, finding.hint, None, 'footprint_pads_overlap',
                             tuple(str(pads[index].number) for index in finding.pads)))
    if not results:
        results.append(Check('OK', tx('Geometria del footprint'),
                             tx('Nessun pad con il rame sovrapposto a quello di un pad di un altro '
                                'numero.'), 'technical', code='footprint_geometry'))
    return results


def contact_function_checks(witness, rows=()):
    """@brief The check of the NC/NO functions of a switch contact, from the extraction's witness.

    @param witness `ExtractedParams.contact_function_witness` as saved with
        the project: «two_models», «user», «same_model», «single», «manual»,
        «asked», or None.
    @param rows Applied symbol rows: with no witness, pins declaring a switch
        function (nc, no, common) are read as «single» -- a project analysed
        before the witness existed has NC/NO nobody verified (practical test
        2026-11-14); without such pins no row is made.
    @return [] for None or an unknown value; otherwise one `Check`:
        «two_models» and «user» OK; «same_model» a final review (importance
        3: two readings of one model are not independent); «single»,
        «manual» and «asked» a WARN to fix before production (importance 2):
        «manual» (the user answered «Nessuna delle due») with the action
        that opens the Pinout table, «asked» with the action that asks the
        open question again.
    @note A wrong NC/NO swaps the contact in the circuit with no error from
        KiCad: only the drawing of the contact settles it, so a reading the
        program could not confirm is never reported as OK.
    """
    title = tx('Funzioni NC/NO')
    verify = tx('Confronta NC e NO dei pin del contatto con il disegno del contatto nel '
                'datasheet; se sono scambiati correggi i nomi nella scheda Simbolo.')
    if witness is None:
        from ..generators.symbol_relay import pin_function
        if any(pin_function(row) in ('nc', 'no', 'common') for row in rows or ()):
            witness = 'single'
    # The rows that ask to compare with the drawing open the Pinout table
    # (names and functions); the dialog adds «Rivedi nella scheda Simbolo».
    if witness == 'two_models':
        return [Check('OK', title, tx('Funzioni NC/NO concordi in due letture di modelli diversi.'),
                      code='contact_functions')]
    if witness == 'user':
        return [Check('OK', title, tx('Funzioni NC/NO scelte dall\'utente.'),
                      code='contact_functions')]
    if witness == 'same_model':
        return [Check('WARN', title, tx('Funzioni NC/NO uguali in due letture dello stesso modello: '
                                        'non sono letture indipendenti, da controllare sul disegno.'),
                      'final', IMPORTANCE_FINAL_REVIEW, verify, ACTION_EDIT_PINOUT, 'contact_functions')]
    if witness == 'single':
        return [Check('WARN', title, tx('Funzioni NC/NO lette una sola volta, non verificate: '
                                        'controllarle sul disegno del contatto.'),
                      'technical', IMPORTANCE_BEFORE_PRODUCTION, verify, ACTION_EDIT_PINOUT,
                      'contact_functions')]
    if witness == 'manual':
        return [Check('WARN', title, tx('Funzioni NC/NO lasciate alla correzione a mano nella '
                                        'tabella Pinout.'),
                      'technical', IMPORTANCE_BEFORE_PRODUCTION,
                      tx('Correggi NC e NO a mano nella tabella Pinout (pulsante «Apri la tabella '
                         'Pinout» qui sotto) confrontandoli con il disegno del contatto, poi ripeti '
                         'la verifica.'), ACTION_EDIT_PINOUT, 'contact_functions')]
    if witness == 'asked':
        return [Check('WARN', title, tx('Funzioni NC/NO: le letture non concordano e la domanda su '
                                        'quale contatto e\' normalmente chiuso e\' ancora aperta; il '
                                        'simbolo usa intanto le funzioni della risposta principale.'),
                      'technical', IMPORTANCE_BEFORE_PRODUCTION,
                      tx('Rispondi alla domanda con «Rispondi alla domanda» qui sotto: il simbolo '
                         'viene ricomposto dalla risposta.'), ACTION_ANSWER_CONTACT_NC,
                      'contact_functions')]
    return []


#: Distance, mm, within which a report position names a pad centre: KiCad
#: writes positions with 4 decimals, the pads are far coarser.
_POSITION_TOLERANCE_MM = 1e-3
#: Distance, mm, at or below which two shapes count as touching: float noise only.
_TOUCH_EPS_MM = 1e-6


def _rotated(x, y, angle_deg):
    """(x, y) turned by `angle_deg` the KiCad way (counter-clockwise on a y-down board)."""
    angle = math.radians(angle_deg or 0.0)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return x * cos_a + y * sin_a, -x * sin_a + y * cos_a


def preview_board_pads(board_text):
    """@brief The pads of the footprint on a preview board, with what the DRC rows need.

    @param board_text The .kicad_pcb text of `create_preview_project`.
    @return (pads, outline_uuids): `pads` are the namespaces of
        `_file_copper_pads` (footprint coordinates) plus `index` (order in
        the footprint, the same as in the .kicad_mod), `uuid` (None when the
        file gives the pad none) and `board_xy` (pad centre on the board, mm);
        `outline_uuids` are the uuids of the board's own Edge.Cuts drawings
        (the preview outline), never the footprint's.
    """
    pads, outline = [], set()
    for footprint in _children(board_text, 'footprint'):
        at = _children(footprint, 'at')
        values = [float(v) for v in _node_arguments(at[0])] if at else list(PREVIEW_FOOTPRINT_ORIGIN)
        fx, fy, angle = values[0], values[1], values[2] if len(values) > 2 else 0.0
        nodes = _children(footprint, 'pad')
        for index, (node, pad) in enumerate(zip(nodes, _file_copper_pads(footprint))):
            uuids = _children(node, 'uuid')
            values = _node_arguments(uuids[0]) if uuids else []
            pad.index = index
            pad.uuid = str(values[0]) if values else None
            dx, dy = _rotated(float(pad.at[0]), float(pad.at[1]), angle)
            pad.board_xy = (fx + dx, fy + dy)
            pads.append(pad)
    for kind in ('gr_rect', 'gr_line', 'gr_poly', 'gr_arc', 'gr_circle'):
        for node in _children(board_text, kind):
            layers = [_node_arguments(layer) for layer in _children(node, 'layer')]
            uuids = _children(node, 'uuid')
            if uuids and any(args and args[0] == 'Edge.Cuts' for args in layers):
                values = _node_arguments(uuids[0])
                if values:
                    outline.add(str(values[0]))
    return pads, outline


def _item_key(item):
    """The identity of one report item: its uuid, else its position."""
    uuid = str(item.get('uuid') or '')
    if uuid:
        return uuid
    pos = item.get('pos') if isinstance(item.get('pos'), dict) else {}
    try:
        return ('pos', round(float(pos.get('x')), 4), round(float(pos.get('y')), 4))
    except (TypeError, ValueError):
        return ('item', str(item.get('description') or ''))


def _item_pad(item, pads):
    """The pad a report item is, or None.

    By uuid first. A position is matched ONLY with a pad written without a
    uuid (KiCad gives it one at load, unknown to the program): an item with
    an unknown uuid that is not such a pad -- the footprint itself, a text,
    a drawing -- names no pad, even when it sits on a pad centre (an exposed
    pad at the footprint origin, review 2026-11-14).
    """
    uuid = str(item.get('uuid') or '')
    by_uuid = next((pad for pad in pads if pad.uuid and pad.uuid == uuid), None)
    if by_uuid is not None:
        return by_uuid
    pos = item.get('pos') if isinstance(item.get('pos'), dict) else {}
    try:
        x, y = float(pos.get('x')), float(pos.get('y'))
    except (TypeError, ValueError):
        return None
    return next((pad for pad in pads if pad.uuid is None
                 and abs(pad.board_xy[0] - x) <= _POSITION_TOLERANCE_MM
                 and abs(pad.board_xy[1] - y) <= _POSITION_TOLERANCE_MM), None)


_SCHEMATIC_PIN = re.compile(r'\(pin\s+"((?:[^"\\]|\\.)*)"\s+\(uuid\s+"([^"]+)"\s*\)')


def _schematic_pin_uuids(schematic_text):
    """{uuid: pin number} of the pins of the symbol instances on a schematic text."""
    return {uuid: number for number, uuid in _SCHEMATIC_PIN.findall(schematic_text)}


#: Italian source of each reason the preview schematic was not written
#: (`kicad_library.PREVIEW_SCHEMATIC_SKIP_REASONS`), for the ERC SKIP row.
SCHEMATIC_SKIP_TEXTS = {
    "symbol_missing": "Schema non creato: manca la libreria simboli del componente.",
    "symbol_unreadable": "Schema non creato: la libreria simboli non è leggibile.",
    "symbol_not_found": "Schema non creato: la libreria non contiene il simbolo del componente.",
    "symbol_derived": "Schema non creato: il simbolo deriva da un altro simbolo.",
    "sym_lib_table_foreign": ("Schema non creato: la cartella contiene già un'altra tabella "
                              "dei simboli, lasciata intatta."),
    "fp_lib_table_foreign": ("Schema non creato: la cartella contiene già un'altra tabella "
                             "delle impronte, lasciata intatta."),
    "symbol_copy_foreign": ("Schema non creato: la cartella contiene già un'altra libreria "
                            "simboli con lo stesso nome, lasciata intatta."),
    "footprint_copy_foreign": ("Schema non creato: la cartella contiene già un'altra libreria "
                               "di impronte KiCadGen_Preview, lasciata intatta."),
}


def courtyard_checks(text):
    """@brief KLC F5.3 on the footprint FILE: the courtyard encloses pads and fabrication drawings.

    @param text The .kicad_mod text.
    @return One WARN (importance 2) per finding of
        `footprint_checks.check_courtyard_enclosure`, with its remedy; [] when
        the file draws no courtyard or encloses everything.
    @note The preview's edge rules are filtered (`DRC_EDGE_TYPES`), so the
        program's own enclosure rule runs on the file: a body drawn outside
        its courtyard is a real defect that no DRC row would show.
    """
    from ..core.footprint import FpArc, FpCircle, FpLine, FpPoly, FpRect
    from .footprint_checks import check_courtyard_enclosure

    def point(node, key):
        nodes = _children(node, key)
        return tuple(float(v) for v in _node_arguments(nodes[0])[:2]) if nodes else None

    def layer(node):
        nodes = _children(node, 'layer')
        args = _node_arguments(nodes[0]) if nodes else []
        return args[0] if args else ''

    graphics = []
    for node in _children(text, 'fp_line'):
        start, end = point(node, 'start'), point(node, 'end')
        if start and end:
            graphics.append(FpLine(start, end, layer(node)))
    for node in _children(text, 'fp_rect'):
        start, end = point(node, 'start'), point(node, 'end')
        if start and end:
            graphics.append(FpRect(start, end, layer(node)))
    for node in _children(text, 'fp_circle'):
        centre, end = point(node, 'center'), point(node, 'end')
        if centre and end:
            graphics.append(FpCircle(centre, math.dist(centre, end), layer(node)))
    for node in _children(text, 'fp_arc'):
        start, mid, end = point(node, 'start'), point(node, 'mid'), point(node, 'end')
        if start and mid and end:
            graphics.append(FpArc(start, mid, end, layer(node)))
    for node in _children(text, 'fp_poly'):
        pts = _children(node, 'pts')
        xys = [tuple(float(v) for v in _node_arguments(xy)[:2]) for xy in _children(pts[0], 'xy')] if pts else []
        if len(xys) >= 3:
            graphics.append(FpPoly(xys, layer(node)))
    footprint = SimpleNamespace(pads=_file_copper_pads(text), graphics=graphics)
    try:
        findings = check_courtyard_enclosure(footprint)
    except (TypeError, ValueError, AttributeError):
        return []
    return [Check('WARN', tx('Geometria del footprint'), finding.message, 'technical',
                  IMPORTANCE_BEFORE_PRODUCTION, finding.hint, None, 'footprint_courtyard')
            for finding in findings]


def _hole_capsule(pad):
    """(segment start, segment end, radius), mm, footprint frame, of a pad's hole; None without one.

    A round hole is a point with its radius; a slot a segment along its
    longer side with half the shorter side as radius, turned with the pad.
    `at` is the hole centre (a drill offset moves the copper, not the hole).
    """
    sides = getattr(pad, 'hole', None)
    if not sides:
        return None
    along, across = float(sides[0]), float(sides[1])
    cx, cy = float(pad.at[0]), float(pad.at[1])
    half = abs(along - across) / 2
    ux, uy = (1.0, 0.0) if along >= across else (0.0, 1.0)
    dx, dy = _rotated(ux * half, uy * half, getattr(pad, 'rotation', 0.0))
    return (cx - dx, cy - dy), (cx + dx, cy + dy), min(along, across) / 2


def _zero_distance(kind, pads):
    """True for a two-pad violation whose shapes really meet: the part cannot be built.

    @note Computed from the board's own geometry, never from KiCad's
        description (it is in KiCad's interface language): a short is always
        a contact; a clearance at 0 mm between two coppers; two drills that
        intersect; a drill that cuts another pad's copper. Two pads with the
        same non-empty number are one pin and never a contact. Any other
        shortfall is a distance below KiCad's default rule.
    """
    from ..core.package_geometry import overlapping_pad_pairs
    from .footprint_checks import _segment_segment_distance, pad_copper, segment_copper_distance
    if len(pads) == 2 and str(pads[0].number) and str(pads[0].number) == str(pads[1].number):
        # One electrical pin in two pieces (a split pad, a thermal pad with
        # its vias): touching is allowed, KiCad's severity ranks the row --
        # unless the two are COPIES of one pad read twice (GA9), a real defect.
        from ..core.package_geometry import _copies
        from .footprint_checks import copper_box
        return _copies(copper_box(pads[0]), copper_box(pads[1]))
    if kind == 'shorting_items':
        return True
    if len(pads) != 2:
        return False
    first, second = pads
    if kind == 'clearance':
        # 0 mm between the coppers: they touch.
        outlines = [(SimpleNamespace(number=f'#{index}'), pad_copper(pad))
                    for index, pad in enumerate(pads)]
        return bool(overlapping_pad_pairs(outlines))
    holes = [_hole_capsule(first), _hole_capsule(second)]
    if kind == 'hole_to_hole' and all(holes):
        (a0, a1, ra), (b0, b1, rb) = holes
        return _segment_segment_distance(a0, a1, b0, b1) - ra - rb <= _TOUCH_EPS_MM
    if kind == 'hole_clearance':
        for hole, other in ((holes[0], second), (holes[1], first)):
            if hole is None or other.pad_type == 'np_thru_hole':
                continue
            start, end, radius = hole
            if segment_copper_distance(start, end, pad_copper(other)) - radius <= _TOUCH_EPS_MM:
                return True
    return False


def _unnumbered_labels():
    """The labels of unnumbered pads: a mechanical hole, any other pad."""
    return (tx('foro di montaggio'), tx('pad senza numero'))


def _pad_label(pad):
    """The pad's number; «foro di montaggio» for an unnumbered np_thru_hole, «pad senza numero» otherwise."""
    if str(pad.number):
        return str(pad.number)
    return tx('foro di montaggio') if pad.pad_type == 'np_thru_hole' else tx('pad senza numero')


def _pins_text(labels):
    """Pad/pin labels as ranges, the unnumbered pads named after the numbers."""
    special = _unnumbered_labels()
    numbers = [label for label in labels if label not in special]
    parts = [pin_ranges(numbers)] if numbers else []
    parts.extend(label for label in special if label in labels)
    return _join(', ', parts)


def _violation_groups(violations, resolve, ignore, zero=None):
    """Group KiCad violations by (type, class), deduplicated by (type, set of items).

    @param violations The JSON `violations` list of a DRC or ERC report.
    @param resolve item -> (label, instance key) of the pad/pin it is, or None.
    @param ignore (type, items) -> True for a violation never shown (a preview
        artefact, or one already reported by the program's own rule).
    @param zero (type, resolved items) -> True for a contact (`_zero_distance`), or None.
    @return [(type, class, [labels], [KiCad descriptions])], class «zero»
        (a contact: blocks the use), «error» or «warning», first-seen order.
    @note Only `type`, `severity`, `items[].uuid` and `items[].pos` are read:
        the descriptions are in KiCad's interface language and only shown.
        The duplicate key is the set of item uuids, or of positions for
        items that carry no uuid.
    """
    groups, seen = {}, set()
    for violation in violations or ():
        if not isinstance(violation, dict):
            continue
        kind, severity = str(violation.get('type') or ''), str(violation.get('severity') or '')
        if not kind or severity not in ('error', 'warning'):
            continue
        items = [item for item in violation.get('items') or () if isinstance(item, dict)]
        if ignore(kind, items):
            continue
        key = (kind, frozenset(_item_key(item) for item in items))
        if key in seen:
            continue
        seen.add(key)
        resolved = [found for found in (resolve(item) for item in items) if found is not None]
        unique = list({instance: (label, instance) for label, instance in resolved}.values())
        cls = 'zero' if zero is not None and zero(kind, unique) else severity
        group = groups.setdefault((kind, cls), ([], []))
        for label, _instance in unique:
            if label not in group[0]:
                group[0].append(label)
        description = str(violation.get('description') or '').strip()
        if description and description not in group[1]:
            group[1].append(description)
    return [(kind, cls, sorted(labels, key=_pin_sort), descriptions)
            for (kind, cls), (labels, descriptions) in groups.items()]


def _canonical_number(text):
    """True for a canonical ASCII decimal («12», not «012» nor «²»), as `pin_ranges` reads it."""
    return text.isascii() and text.isdigit() and str(int(text)) == text


def _pin_sort(value):
    """Sort key of pad/pin numbers: canonical decimals by value, then the rest as text.

    `str.isdigit` accepts «²», which `int` refuses: only the canonical test of
    `pin_ranges` decides a numeric key (review 2026-11-14).
    """
    text = str(value)
    return (0, int(text), '') if _canonical_number(text) else (1, 0, text)


def _violation_checks(groups, title, pins_template, remedy, code_prefix):
    """One row per violation group.

    A contact («zero» class) is an ERROR of importance 1: the part cannot be
    built whatever the rules. A KiCad error is a WARN of importance 2, a
    warning of importance 3, both with the note that the rules are KiCad's
    defaults, not the manufacturer's. Every row opens the check's preview
    («Mostra in KiCad»): a re-export cannot change the copies the check
    made, so a symbol-library mismatch gets the same view.
    """
    results = []
    for kind, cls, labels, descriptions in groups:
        what = tx(VIOLATION_TEXTS[kind]) if kind in VIOLATION_TEXTS else (
            descriptions[0] if descriptions else kind)
        detail = (tx(pins_template, what=what, pins=_pins_text(labels)) if labels
                  else tx('{what}.', what=what))
        if descriptions:
            detail += '\n' + tx('KiCad: {description}', description='; '.join(descriptions[:3]))
        if cls == 'zero':
            detail += '\n' + tx('Contatto reale fra le forme: il componente non si può realizzare così.')
        elif kind not in LIBRARY_TYPES:
            # A library or grid finding is not a manufacturing rule.
            detail += '\n' + tx('Regole predefinite di KiCad, non quelle del tuo produttore.')
        status, importance = (('ERROR', IMPORTANCE_BLOCKS_USE) if cls == 'zero' else
                              ('WARN', IMPORTANCE_BEFORE_PRODUCTION) if cls == 'error' else
                              ('WARN', IMPORTANCE_FINAL_REVIEW))
        pins = tuple(label for label in labels if label not in _unnumbered_labels())
        results.append(Check(status, title, detail, 'technical', importance, remedy,
                             ACTION_SHOW_IN_KICAD, f'{code_prefix}_{kind}', pins))
    return results


def drc_checks(data, pads, overlap_pairs=(), outline_uuids=()):
    """@brief The rows of a `kicad-cli pcb drc` JSON report of the preview board.

    @param data The parsed JSON (its `violations` list).
    @param pads The pads of the same run's preview board (`preview_board_pads`).
    @param overlap_pairs Pad INDEX pairs (frozensets) the program's
        `footprint_pads_overlap` reported: a clearance, short or mask bridge
        between exactly those two pads is not shown twice.
    @param outline_uuids Uuids of the preview's own board outline: an edge
        violation is a preview artefact only when it names that outline; an
        Edge.Cuts drawing of the footprint itself stays visible.
    @return One row per (type, class) after the preview artefacts
        (`DRC_PREVIEW_ARTEFACTS`, `DRC_EDGE_TYPES`) and the duplicates are
        dropped, or one OK when nothing is left.
    """
    pairs = {frozenset(pair) for pair in overlap_pairs}
    outline = set(outline_uuids)

    def resolve(item):
        pad = _item_pad(item, pads)
        return None if pad is None else (_pad_label(pad), pad.index)

    def ignore(kind, items):
        if kind in DRC_PREVIEW_ARTEFACTS:
            return True
        if kind in DRC_EDGE_TYPES:
            return any(str(item.get('uuid') or '') in outline for item in items)
        if kind in DRC_OVERLAP_TYPES:
            found = frozenset(pad.index for pad in (_item_pad(item, pads) for item in items) if pad)
            return len(found) == 2 and found in pairs
        return False

    by_index = {pad.index: pad for pad in pads}
    groups = _violation_groups(
        data.get('violations'), resolve, ignore,
        lambda kind, found: _zero_distance(kind, [by_index[index] for _label, index in found]))
    title = tx('DRC del footprint in KiCad')
    results = _violation_checks(
        groups, title, '{what}: pad {pins}.',
        tx('Guarda il punto con «Mostra in KiCad», poi correggi il footprint nella scheda '
           'Anteprima footprint e ripeti la verifica.'), 'drc')
    return results or [Check('OK', title, tx('Nessuna violazione del footprint con le regole '
                                              'predefinite di KiCad.'), code='drc')]


def erc_checks(data, pin_uuids):
    """@brief The rows of a `kicad-cli sch erc` JSON report of the preview schematic.

    @param data The parsed JSON (its `sheets[].violations`).
    @param pin_uuids {uuid: pin number} of the preview schematic of the same run.
    @return (checks, power_pins): one row per (type, severity) after the
        artefacts of a symbol alone on its sheet (`ERC_PREVIEW_ARTEFACTS`),
        or one OK naming how many DISTINCT pins are unconnected; and the
        pins whose power input has no driver, for the manual row.
    """
    violations = [violation for sheet in data.get('sheets') or () if isinstance(sheet, dict)
                  for violation in sheet.get('violations') or () if isinstance(violation, dict)]

    def items_of(kind):
        return [item for violation in violations if violation.get('type') == kind
                for item in violation.get('items') or () if isinstance(item, dict)]

    # Distinct pins, an item the schematic does not name counted by its identity.
    unconnected = {pin_uuids.get(str(item.get('uuid') or ''), _item_key(item))
                   for item in items_of('pin_not_connected')}
    power = sorted({pin_uuids[str(item.get('uuid'))] for item in items_of('power_pin_not_driven')
                    if str(item.get('uuid')) in pin_uuids}, key=_pin_sort)

    def resolve(item):
        pin = pin_uuids.get(str(item.get('uuid') or ''))
        return None if pin is None else (pin, pin)

    groups = _violation_groups(violations, resolve, lambda kind, items: kind in ERC_PREVIEW_ARTEFACTS)
    title = tx('ERC del simbolo in KiCad')
    results = _violation_checks(
        groups, title, '{what}: pin {pins}.',
        tx('Guarda il punto con «Mostra in KiCad», poi correggi il simbolo nella scheda Simbolo e '
           'ripeti la verifica.'), 'erc')
    return (results or [Check('OK', title, tx(
        'Nessuna violazione del simbolo; {count} pin non collegati, normale per un simbolo da solo '
        'su un foglio.', count=len(unconnected)), code='erc')]), power


_SEXPR_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+')


def sexpr_tokens(text):
    """@brief The tokens of an S-expression: parentheses, quoted strings kept whole, atoms.

    @param text S-expression text.
    @return The token list: layout whitespace is gone, the content of a
        quoted string is untouched («Relay (5V)  coil» stays different from
        «Relay(5V) coil»).
    """
    return _SEXPR_TOKEN.findall(text)


def _file_digest(path):
    """(size, sha256 hex) of a file, or None when it cannot be read."""
    import hashlib
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return len(data), hashlib.sha256(data).hexdigest()


class CliStopped(BaseException):
    """@brief Raised inside a check when its kicad-cli command was killed by `stop_cli_commands`.

    @note A BaseException on purpose: the check catches `Exception` (and
        OSError) around single commands to report them, and a stop asked by
        the user or by a quit must end the run, not become one more row.
    """


#: kicad-cli processes of the running check, killed by `stop_cli_commands`.
_CLI_PROCESSES = set()
#: Processes `stop_cli_commands` killed, so their runner raises CliStopped.
_CLI_STOPPED = set()
_CLI_LOCK = threading.Lock()
#: Number of `stop_cli_commands` calls so far. A check records the value it
#: started with (`_RUN_STATE.epoch`, set by `_stoppable_run`); once a stop
#: changed it, that check starts no further kicad-cli command (review
#: 2026-11-16, R9-3/R4-1). Guarded by `_CLI_LOCK`.
_STOP_EPOCH = 0
#: Per-thread state of the running check: ``epoch`` is the `_STOP_EPOCH` it
#: started under, absent outside a check.
_RUN_STATE = threading.local()


def _run_stopped():
    """True when the check running in this thread began before the latest `stop_cli_commands` (call under `_CLI_LOCK`)."""
    epoch = getattr(_RUN_STATE, 'epoch', None)
    return epoch is not None and epoch != _STOP_EPOCH


def _stoppable_run(function):
    """@brief Make a stop sticky for one check run: record the stop epoch it started under.

    @param function `verify_component`.
    @return The wrapped function, same signature and docstring.
    @note Review 2026-11-16, R9-3 (= R4-1): `stop_cli_commands` used to kill
        only the commands already running, so a kicad-cli command the check
        started right after the stop (between two reads) ran to its budget,
        up to 240 s, after a project switch. Now every `_run_cli` of a run
        that began before the latest stop raises `CliStopped` instead of
        starting, and one started at the very moment of the stop is killed
        as soon as it is registered. A check started AFTER the stop is not
        affected. The previous value is restored on exit, so a nested call
        keeps the caller's epoch.
    """
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        """Run the check with this thread's stop epoch recorded."""
        previous = getattr(_RUN_STATE, 'epoch', None)
        # The epoch the CALLER captured when it created the run (its worker),
        # when given: a stop between that moment and this call then still
        # stops the run (review 2026-11-16, follow-up 3).
        given = kwargs.get('stop_epoch')
        with _CLI_LOCK:
            _RUN_STATE.epoch = _STOP_EPOCH if given is None else given
        try:
            return function(*args, **kwargs)
        finally:
            _RUN_STATE.epoch = previous
    return wrapper


def current_stop_epoch():
    """@brief The stop epoch now, to capture when a check is CREATED (passed as `stop_epoch`).

    @return The number of `stop_cli_commands` calls so far.
    @note A worker reads it in its constructor and passes it to
        `verify_component(stop_epoch=...)`: a stop asked after the worker was
        created but before the check began then still stops every kicad-cli
        command of that check (review 2026-11-16, follow-up 3).
    """
    with _CLI_LOCK:
        return _STOP_EPOCH


def _run_cli(command, *, timeout=60, cwd=None):
    """@brief Run one kicad-cli command like `subprocess.run(capture_output=True, text=True)`, killably.

    @param command The command line (list).
    @param timeout Seconds before the command is killed and TimeoutExpired raised.
    @param cwd Working folder of the command.
    @return subprocess.CompletedProcess with stdout and stderr as text.
    @throws subprocess.TimeoutExpired, OSError As subprocess.run.
    @throws CliStopped When `stop_cli_commands` killed it meanwhile.
    @note The process tree is killed on a timeout or a stop
        (`process_log.kill_process_tree`): a check left running when the
        window closes or the project changes must not leave kicad-cli behind
        (review 2026-11-15, M3).
    @note Any exception out of the wait -- a timeout, but also a
        KeyboardInterrupt or a `CliStopped` raised in this thread -- kills
        the process tree and reaps it before propagating; the Job Object
        handle is closed in every case (`release_process`), so no handle
        leaks per command and KILL_ON_JOB_CLOSE ends a leftover descendant
        (review 2026-11-15, L2; same shape as `kicad_models`' measuring child).
    @note stdin is the null device: kicad-cli must never wait for input.
    @note A stop is sticky for the check that was running (R9-3, see
        `_stoppable_run`): after it, no command of that check is started, and
        one started at the same moment is killed before its wait.
    """
    from ..cad3d.process_log import (contain_process, kill_process_tree, reap_killed_child,
                                     release_process)
    with _CLI_LOCK:
        if _run_stopped():
            raise CliStopped()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, errors='replace', cwd=cwd,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    # In a Job Object that dies with it: a descendant the process snapshot
    # cannot prove would otherwise keep the pipes open and the check running.
    contain_process(process)
    with _CLI_LOCK:
        # A stop between the test above and here found nothing to kill:
        # this command is then marked stopped and ended below at once.
        if _run_stopped():
            _CLI_STOPPED.add(process)
        _CLI_PROCESSES.add(process)
    try:
        if process in _CLI_STOPPED:
            try:
                kill_process_tree(process)
            except OSError:
                pass  # already gone
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            # Nothing may outlive the command: a timeout, a stop or an
            # interrupt all end the tree before the exception goes on.
            try:
                kill_process_tree(process)
            except OSError:
                pass  # already gone
            reap_killed_child(process)
            raise
    finally:
        release_process(process)
        with _CLI_LOCK:
            _CLI_PROCESSES.discard(process)
            stopped = process in _CLI_STOPPED
            _CLI_STOPPED.discard(process)
    if stopped:
        raise CliStopped()
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def stop_cli_commands() -> int:
    """@brief Kill the kicad-cli commands of the running check (a stop, a quit, a project switch).

    @return The number of commands killed.
    @note Their runners raise CliStopped, which ends the check: what a
        killed command would have reported is not a result.
    @note Sticky (review 2026-11-16, R9-3): every check running now starts
        no further command (`_STOP_EPOCH`, `_stoppable_run`); a check
        started afterwards runs normally.
    """
    global _STOP_EPOCH
    from ..cad3d.process_log import kill_process_tree
    with _CLI_LOCK:
        _STOP_EPOCH += 1
        running = [process for process in _CLI_PROCESSES if process.poll() is None]
        _CLI_STOPPED.update(running)
    for process in running:
        try:
            kill_process_tree(process)
        except OSError:
            pass  # already gone between the poll and the kill
    return len(running)


def _installed_symbol_blocks(name, library):
    """The blocks named `name` of an installed symbol library, or why it cannot be read.

    @return (blocks, None); ([], None) when the file does not exist;
        (None, reason) with reason «not_utf8» (KiCad reads UTF-8 only),
        «unopenable» (the file exists but cannot be opened) or «invalid»
        (its S-expression cannot be walked). An unreadable library is never
        a missing symbol: the installation refuses to rewrite it, so its
        remedy is another one (review 2026-11-15, L1).
    """
    from .kicad_library import _SYMBOL_HEAD, _atom_value, _direct_child_ranges, _read_utf8
    library = Path(library)
    if not library.exists():
        return [], None
    try:
        # The installation's own decoding (UTF-8 only, BOM dropped).
        text = _read_utf8(library)
    except KiCadLibraryError as exc:
        return None, 'not_utf8' if exc.code == 'file_not_utf8' else 'invalid'
    except OSError:
        return None, 'unopenable'
    try:
        return [text[start:end] for start, end in _direct_child_ranges(text, 'symbol')
                if (head := _SYMBOL_HEAD.match(text[start:end]))
                and _atom_value(head.group(2)) == name], None
    except (ValueError, RuntimeError):
        return None, 'invalid'


#: Italian words of why an installed symbol library cannot be read
#: (`_installed_symbol_blocks` reasons).
SYMBOL_LIBRARY_UNREADABLE_TEXTS = {
    'not_utf8': 'non è testo UTF-8',
    'unopenable': 'il file non si apre',
    'invalid': 'non è una libreria di simboli leggibile',
}


def _component_symbol_expected(name, symbol_path, library):
    """(symbol block an installation writes, None) or (None, reason it ships no symbol).

    `kicad_library._component_symbol` reads `<component_dir>/<name>.kicad_sym`,
    exactly the file an installation reads; the library's stem is the
    nickname the installation writes into the symbol's Footprint property.
    """
    from .kicad_library import _component_symbol
    symbol_path = Path(symbol_path)
    expected, _source, skipped = _component_symbol(
        SimpleNamespace(name=name, component_dir=symbol_path.parent), Path(library).stem)
    return expected, skipped


def _starts_with_bom(path):
    """True for a readable file that starts with a UTF-8 BOM; False for a missing or unreadable one."""
    try:
        with open(path, 'rb') as handle:
            return handle.read(len(UTF8_BOM)) == UTF8_BOM
    except OSError:
        return False


def _installed_symbol_state(name, symbol_path, library):
    """@brief How the component's symbol stands in an installed symbol library.

    @param name Component name, the symbol's name in the library.
    @param symbol_path The component's ``<name>.kicad_sym`` (None: not compared).
    @param library The installed ``<library>.kicad_sym`` next to the ``.pretty``.
    @return (state, detail): (None, None) when nothing is to be said;
        («missing», None) or («differs», None) for a symbol an installation
        would now write differently -- the reinstallation fixes it;
        («stale», None) when the component has no usable symbol but the
        library still holds one of its name from an earlier installation --
        a reinstallation leaves it there (`InstallPlan.symbol_left_stale`),
        so only repairing the component's symbol fixes it; («unreadable»,
        reason) for a library the installation cannot read either -- said
        whether or not the component's own symbol is usable (round 2, R9).
    @note The symbol expected is derived by the installation's OWN rule,
        `kicad_library._component_symbol` (same file, same root-version
        requirement, same naming), so the check and the install cannot
        disagree (review 2026-11-15, M1): a component symbol without a root
        ``(version)`` is not compared -- the install skips it -- and is
        reported only through what the library still holds.
    @note The comparison is on S-expression tokens of the one symbol block:
        the other symbols of the shared library are not this component's
        business.
    """
    if symbol_path is None:
        return None, None
    expected, skipped = _component_symbol_expected(name, symbol_path, library)
    blocks, unreadable = _installed_symbol_blocks(name, library)
    if unreadable is not None:
        # Checked BEFORE the component's own symbol (round 2, R9): with an
        # unusable component symbol too, an unreadable library would
        # otherwise read as «nothing to say» and the row as OK.
        return 'unreadable', unreadable
    if skipped is not None:
        # No usable symbol of the component: an installation ships none and
        # leaves an earlier one in place.
        return ('stale', skipped) if blocks else (None, None)
    if not blocks:
        return 'missing', None
    return (None, None) if sexpr_tokens(blocks[0]) == sexpr_tokens(expected) else ('differs', None)


def _table_row_problem(table, library, uri, project_dir, root):
    """@brief What keeps KiCad from finding an installed library through its table row, or None.

    @param table The ``fp-lib-table`` or ``sym-lib-table`` the installation
        registered the library in.
    @param library The library nickname (the ``.pretty``/``.kicad_sym`` stem).
    @param uri The URI the installation writes for it (``${KIPRJMOD}/…`` for
        a project, the absolute path for a global library).
    @param project_dir The folder ``${KIPRJMOD}`` stands for (project
        installation), None for a global one.
    @param root `kicad_library.FP_TABLE_ROOT` or `SYM_TABLE_ROOT`.
    @return None when an enabled ``KiCad`` row of that name points to the
        installed library; otherwise (kind, words): kind «table» for the
        table missing or unreadable, «row» for the row missing, disabled, of
        another type or pointing elsewhere; words translated.
    @note Review 2026-11-16, R9-4: the files alone said «Libreria
        aggiornata» while KiCad could not see the library (row removed or
        disabled by the user in «Gestisci librerie»). The rule is the
        installation's own (`kicad_library._same_place`, the same
        enabled-``KiCad``-row test as `_destination_row_already_there`), so
        the check and a reinstallation agree; a reinstallation adds a
        missing row and asks before enabling or repointing one.
    """
    from .kicad_library import _same_place, find_library_entry
    table = Path(table)
    if not table.is_file():
        return 'table', tx('tabella {file} mancante', file=table.name)
    try:
        entry = find_library_entry(table, library, root=root)
    except (KiCadLibraryError, OSError):
        return 'table', tx('tabella {file} non leggibile', file=table.name)
    if entry is None:
        return 'row', tx('riga {library} mancante in {file}', library=library, file=table.name)
    if entry.disabled:
        return 'row', tx('riga {library} disattivata in {file}', library=library, file=table.name)
    if (entry.library_type or '').casefold() != 'kicad' or not _same_place(entry.uri, uri, project_dir):
        return 'row', tx('riga {library} di {file} non punta a questa libreria', library=library,
                  file=table.name)
    return None


def library_checks(name, footprint_text, installs, step_path=None, model_offset=(0.0, 0.0, 0.0),
                   symbol_path=None, global_table_path=None):
    """@brief Whether the KiCad libraries the component was installed in hold what an install writes NOW.

    @param name Component name (the installed file is `<name>.kicad_mod`).
    @param footprint_text The .kicad_mod text of the component folder.
    @param installs None (no check: the caller records no installation), or
        a list of {"scope": "project"|"global", "pretty_dir": absolute folder,
        optionally "table_path" and "symbol_table_path": the fp-lib-table and
        sym-lib-table the installation registered the libraries in}.
    @param step_path The component's STEP (None or missing: delivered without model).
    @param model_offset The model offset the install would write
        (`kicad_library.model_offset_for_anchor`).
    @param symbol_path The component's ``<name>.kicad_sym``, whose symbol an
        installation adds to ``<library>.kicad_sym`` next to the ``.pretty``
        (user decision 2026-11-15, «opzione 1»); None compares no symbol.
    @param global_table_path The per-user global ``fp-lib-table`` a global
        installation registered its library in, when the CALLER knows it;
        None (the default, and what `verify_component` passes since round 3)
        when unknown: the global tables of a record without its own
        ``"table_path"`` are then not looked at. An install entry's own
        ``"table_path"`` (the ``fp-lib-table``) and ``"symbol_table_path"``
        (the ``sym-lib-table``; without it the ``sym-lib-table`` beside the
        ``fp-lib-table``) win. A project installation's tables are the
        ``fp-lib-table`` / ``sym-lib-table`` beside its ``.pretty``.
    @return [] for None; one WARN (importance 3) when the list is empty (never
        installed); a SKIP «non confrontabile» for an installation that
        cannot be compared (and when none can); otherwise one row per installation: OK when everything an
        install would write now is there, WARN importance 2 naming WHAT
        differs otherwise -- the footprint, its model link (presence,
        reference, offset) or the STEP file (size and SHA-256) -- with the
        action that opens the KiCad tab.
    @note The comparison is on S-expression tokens (`sexpr_tokens`): layout
        whitespace is ignored, quoted strings are compared exactly. The
        installed symbol is compared too (CONTRACT CHANGED 2026-11-15: the
        installation writes the symbol library now); a library installed
        before that holds no symbol and reads «simbolo mancante».
    @note The symbol follows the installation's own rule
        (`_installed_symbol_state`, `symbol_path` must be
        ``<component dir>/<name>.kicad_sym``): a component symbol the install
        would skip while the library still holds one of its name is a WARN
        importance 2 «vecchio» whose action re-exports the symbol (the
        reinstallation alone leaves it); a symbol library that cannot be
        read is a WARN importance 2 of its own, never «simbolo mancante».
        Neither ever yields an OK row for that installation.
    @note A leading UTF-8 BOM in a file the installation writes -- the
        installed ``.kicad_mod`` and the ``fp-lib-table``, and, when the
        component ships a symbol (`_component_symbol_expected`), the
        installed ``.kicad_sym`` and the ``sym-lib-table``; an installation
        that ships no symbol leaves those two alone, so they are not named
        -- is «not up to date» (round 2, R8): KiCad 10
        refuses such a file (kicad-cli 10.0 `sym export svg` exit 2, ERC
        `lib_symbol_issues`, tables whose rows it ignores), and a
        reinstallation rewrites it without the BOM. The comparisons above
        drop the BOM, so without this rule the row said «aggiornata».
    @note CONTRACT CHANGED 2026-11-16 (R9-4): the table rows are read too
        (`_table_row_problem`) -- the ``fp-lib-table`` row of the ``.pretty``
        and, when the installation ships the symbol, the ``sym-lib-table``
        row of the ``.kicad_sym``. A table missing or unreadable, a row
        missing, disabled or pointing elsewhere is «not up to date» naming
        it: the files alone never make the row «aggiornata».
    """
    if installs is None:
        return []
    title = tx('Libreria KiCad')
    if not installs:
        return [Check('WARN', title, tx('Componente non ancora installato in una libreria KiCad.'),
                      'final', IMPORTANCE_FINAL_REVIEW,
                      tx('Installalo dalla scheda KiCad quando è pronto.'), ACTION_OPEN_KICAD_TAB,
                      'kicad_library')]
    from .kicad_library import attach_3d_model

    def not_comparable(reason):
        return [Check('SKIP', title, tx('Libreria KiCad non confrontabile: {reason}', reason=reason),
                      code='kicad_library')]
    try:
        named = name_footprint(footprint_text, name)
    except (RuntimeError, ValueError) as exc:
        return not_comparable(str(exc))
    with_model = step_path is not None and Path(step_path).is_file()
    results = []
    for install in installs:
        if not isinstance(install, dict) or not install.get('pretty_dir'):
            continue
        project = install.get('scope') == 'project'
        scope = tx('del progetto') if project else tx('globale')
        pretty = Path(install['pretty_dir'])
        installed = pretty / f'{name}.kicad_mod'
        shapes = pretty.parent / f'{pretty.stem}.3dshapes'
        installed_step = shapes / f'{name}.step'
        reference = (f'${{KIPRJMOD}}/{pretty.stem}.3dshapes/{name}.step' if project
                     else installed_step.as_posix())
        try:
            expected = (attach_3d_model(named, reference, tuple(model_offset)) if with_model
                        else detach_3d_model(named))
        except (RuntimeError, ValueError) as exc:
            results.extend(not_comparable(str(exc)))
            continue
        differs = []
        try:
            text = installed.read_text(encoding='utf-8-sig')
        except (OSError, ValueError):
            text = None
        if text is None:
            differs.append(tx('footprint mancante'))
        else:
            try:
                if sexpr_tokens(detach_3d_model(text)) != sexpr_tokens(detach_3d_model(expected)):
                    differs.append(tx('footprint'))
                if ([sexpr_tokens(node) for node in _children(text, 'model')]
                        != [sexpr_tokens(node) for node in _children(expected, 'model')]):
                    differs.append(tx('collegamento al modello STEP'))
            except (RuntimeError, ValueError):
                differs.append(tx('footprint'))
        if with_model and _file_digest(installed_step) != _file_digest(step_path):
            differs.append(tx('modello STEP'))
        symbol_library = pretty.parent / f'{pretty.stem}.kicad_sym'
        # A leading BOM in a file the installation writes: KiCad 10 refuses
        # the file, the reinstallation drops the BOM (round 2, R8). The
        # symbol library and its table only when the installation ships the
        # symbol: otherwise it does not touch them.
        # The tables the record names win (round 3, item 6: the window
        # records `table_path` and `symbol_table_path` of an installation);
        # a project installation's are beside its .pretty; a global one
        # WITHOUT a recorded table is not looked up (another KiCad version's
        # table may be «the current» one) unless the caller declares it.
        fp_table = (Path(install['table_path']) if install.get('table_path')
                    else pretty.parent / 'fp-lib-table' if project
                    else Path(global_table_path) if global_table_path else None)
        sym_table = (Path(install['symbol_table_path']) if install.get('symbol_table_path')
                     else fp_table.with_name('sym-lib-table') if fp_table is not None else None)
        ships_symbol = (symbol_path is not None and
                        _component_symbol_expected(name, symbol_path, symbol_library)[1] is None)
        with_bom = [installed] + ([fp_table] if fp_table is not None else [])
        if ships_symbol:
            with_bom.append(symbol_library)
            if sym_table is not None:
                with_bom.append(sym_table)
        for path in with_bom:
            if _starts_with_bom(path):
                differs.append(tx('BOM UTF-8 in {file}', file=path.name))
        # The rows KiCad finds the libraries through (review 2026-11-16,
        # R9-4): a row removed or disabled leaves the files in place and the
        # library invisible. Only tables KNOWN for this record are read (a
        # global record without its table is not looked up, as above); the
        # symbol table only when the installation ships the symbol.
        from .kicad_library import FP_TABLE_ROOT, SYM_TABLE_ROOT
        project_dir = pretty.parent if project else None
        rows_to_check = []
        if fp_table is not None:
            rows_to_check.append((fp_table, f'${{KIPRJMOD}}/{pretty.name}' if project
                                  else pretty.as_posix(), FP_TABLE_ROOT))
        if ships_symbol and sym_table is not None:
            rows_to_check.append((sym_table, f'${{KIPRJMOD}}/{symbol_library.name}' if project
                                  else symbol_library.as_posix(), SYM_TABLE_ROOT))
        #: A GLOBAL table missing or unreadable (follow-up 5): a reinstall
        #: cannot fix it (the global install never creates KiCad's table),
        #: so the row's remedy sends the user to KiCad's own table setup.
        global_table_unusable = False
        for table, uri, table_root in rows_to_check:
            problem = _table_row_problem(table, pretty.stem, uri, project_dir, table_root)
            if problem is not None:
                kind, words = problem
                differs.append(words)
                global_table_unusable = global_table_unusable or (kind == 'table' and not project)
        symbol_state, unreadable = _installed_symbol_state(name, symbol_path, symbol_library)
        if symbol_state == 'missing':
            differs.append(tx('simbolo mancante'))
        elif symbol_state == 'differs':
            differs.append(tx('simbolo'))
        elif symbol_state == 'stale':
            # A reinstallation cannot fix it (it ships no symbol and leaves
            # this one): the remedy repairs the component's symbol first
            # (review 2026-11-15, M1). Never an OK row.
            results.append(Check('WARN', title, tx(
                'Libreria {scope} con un simbolo {name} vecchio: il simbolo del componente non è '
                'utilizzabile, quindi l\'installazione non lo aggiorna: {path}', scope=scope,
                name=name, path=symbol_library),
                'technical', IMPORTANCE_BEFORE_PRODUCTION,
                tx('Usa «Riesporta il simbolo» per riscrivere il .kicad_sym del componente dal simbolo '
                   'applicato (correggilo prima nella scheda «Simbolo elettrico» se serve), poi '
                   'reinstalla dalla scheda KiCad.'),
                ACTION_REEXPORT_SYMBOL, 'kicad_library'))
        elif symbol_state == 'unreadable':
            # Not a missing symbol: the installation refuses to rewrite a
            # library it cannot read, so «reinstall» alone would fail (L1).
            results.append(Check('WARN', title, tx(
                'Libreria simboli {scope} non leggibile ({reason}): {path}', scope=scope,
                reason=tx(SYMBOL_LIBRARY_UNREADABLE_TEXTS[unreadable]), path=symbol_library),
                'technical', IMPORTANCE_BEFORE_PRODUCTION,
                tx('Ripristina il file da una copia oppure spostalo altrove, poi reinstalla dalla '
                   'scheda KiCad: l\'installazione non riscrive una libreria che non sa leggere.'),
                ACTION_OPEN_KICAD_TAB, 'kicad_library'))
        if symbol_state in ('stale', 'unreadable') and not differs:
            continue
        if not differs:
            results.append(Check('OK', title, tx('Libreria {scope} aggiornata: {path}',
                                                 scope=scope, path=installed), code='kicad_library'))
        else:
            results.append(Check('WARN', title, tx(
                'Libreria {scope} non aggiornata ({what}): {path}', scope=scope,
                what=_join(', ', differs), path=installed),
                'technical', IMPORTANCE_BEFORE_PRODUCTION,
                tx('Apri KiCad e la sua impostazione delle librerie (Preferenze > Gestisci librerie '
                   'dei simboli / dei footprint) perché ricrei la tabella globale, poi reinstalla '
                   'dalla scheda KiCad.') if global_table_unusable else
                tx('Reinstallala dalla scheda KiCad: la reinstallazione sovrascrive anche le modifiche '
                   'fatte nell\'editor di KiCad.'), ACTION_OPEN_KICAD_TAB, 'kicad_library'))
    # Recorded installations none of which could be compared: said, never silent.
    return results or not_comparable(tx('nessuna installazione registrata è leggibile'))


class _NoStep(Exception):
    """The component has no STEP model: the 3D checks are skipped (already reported)."""


class _ComponentFileBusy(Exception):
    """A file of the component exists but another program holds it: it cannot be read NOW.

    Carries the translated `detail` and `remedy` of the SKIP that says so
    (round 3, item 3): a file locked by an editor, a backup or an antivirus
    is neither «missing» nor a defect of the component.
    """

    def __init__(self, path, error):
        """@brief The busy file and the operating system's error, rendered as the SKIP's texts.

        @param path The component file that could not be read.
        @param error The PermissionError raised when reading it.
        """
        super().__init__(str(error))
        self.detail = tx('Il file {path} è in uso da un altro programma e non si può leggere ora '
                         '({error}): controllo non eseguito.', path=path, error=error)
        self.remedy = tx('Chiudi il programma che tiene aperto il file (editor di KiCad, backup, '
                         'antivirus), poi ripeti il controllo.')


def _read_component_file(path):
    """The bytes of a component file; `_ComponentFileBusy` when another program holds it.

    A PermissionError on a REGULAR file is a lock (Windows reports a
    sharing violation that way); on anything else -- a folder in the
    file's place, which Windows also refuses with PermissionError -- the
    original error goes on and keeps its former meaning. FileNotFoundError
    and the other OSErrors go on unchanged.
    """
    path = Path(path)
    try:
        return path.read_bytes()
    except PermissionError as exc:
        if path.is_file():
            raise _ComponentFileBusy(path, exc) from exc
        raise


class _ToolSideFailure(Exception):
    """A kicad-cli read could not be prepared on the checking side (output folder, input copy).

    Its only argument is the translated reason: a SKIP with `cli_failure`
    «scratch» (round 4, item 6; «not_run» before), never a verdict on the
    component (round 2, R2).
    """


def symbol_library_problem(text, name):
    """@brief What a symbol library lacks for the component, read by the check's own parser.

    @param text The ``.kicad_sym`` text (BOM already dropped).
    @param name The component's name, the symbol it must hold.
    @return None when the library holds a top-level symbol named `name`, or
        when it cannot be walked at all (then kicad-cli's own verdict
        stands); otherwise the translated detail: no symbol at all, or the
        names it holds instead.
    @note kicad-cli 10.0 `sym export svg` exits 0 on both (measured
        2026-11-15): with no output for a library holding no symbol, with
        another symbol's drawing for a library holding another name. That
        is a problem of the component's file, said as such, never «not run»
        (round 2, R10).
    """
    from .kicad_library import _SYMBOL_HEAD, _atom_value
    try:
        names = [_atom_value(head.group(2)) for start, end in _direct_child_ranges(text, 'symbol')
                 if (head := _SYMBOL_HEAD.match(text[start:end]))]
    except (ValueError, RuntimeError, IndexError):
        return None
    if name in names:
        return None
    if not names:
        return tx('La libreria non contiene alcun simbolo.')
    return tx('La libreria non contiene il simbolo {name}, ma: {names}.', name=name,
              names=', '.join(names))


#: Symbol properties that belong to the metadata (written by «Applica ai file»,
#: checked by the KLC gate), left out when the exported file is compared with
#: the applied symbol -- the same set the window leaves out
#: (`main_window.SYMBOL_METADATA_PROPERTIES`).
SYMBOL_METADATA_PROPERTIES = ("Reference", "Description", "ki_keywords", "Datasheet", "ki_fp_filters")


def symbol_text_without_metadata(text: str) -> str:
    """@brief A .kicad_sym text with its metadata property lines removed and whitespace normalised.

    @param text The library text (file or rendered).
    @return The lines that describe the symbol itself (name, Footprint link,
        pins, graphics, property positions), stripped, joined by newlines.
    @note The one comparison of «exported file» against «applied symbol»
        (real runs 2026-11-13: comparing the full texts flagged every fresh
        export as stale, because the applied text was rebuilt without the
        metadata and the relay graphics the export writes).
    """
    kept = []
    for line in str(text).replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(f'(property "{key}"') for key in SYMBOL_METADATA_PROPERTIES):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def estimated_pad_checks(findings, pad_numbers=None):
    """@brief The component-check rows of the estimated-pad findings of the footprint tab.

    @param findings None, or the findings of
        `footprint_checks.check_estimated_pads` (code «estimated_pad»,
        translated `message`, `pads` as INDICES into the footprint's pads).
        Findings of another code are ignored.
    @param pad_numbers The pad numbers of the SAME `Footprint` the findings
        were computed on, in its `pads` order (index i -> pad_numbers[i]),
        or None when the caller has none: then no pad is named.
    @return One WARN per finding: title «Pad stimati», the finding's text as
        detail, its pad numbers as `pins`, code «estimated_pads», category
        «final», importance 2 (to verify before production), and the
        footprint tab's own remedy for «estimated_pad»
        (`footprint_checks.GEOMETRY_HINTS`, one wording for both places).
        [] for None or an empty list.
    @note CONTRACT CHANGED 2026-11-15 (round 2, R6): the indices are no
        longer named through the pad order of the FILE on disk. KiCad's
        footprint editor may save the pads in another order (the window's
        own alignment gate compares pads regardless of order), so the file's
        i-th pad may be another pad than the one the finding means; the
        right pad would not be highlighted. Only the numbers of the very
        footprint the findings were computed on are used; without them, or
        with an index they do not have, no pin is named (never a guessed
        pad); the detail still names the pads.
    @note The detail is the finding's text as `footprint_checks` rendered
        it, in the language active then: the finding keeps neither a
        template nor the per-pad values, so it is not recomposed after a
        language switch (round 2, reported to the coordinator).
    """
    from .footprint_checks import GEOMETRY_HINTS
    results = []
    for finding in findings or ():
        if getattr(finding, 'code', None) != 'estimated_pad':
            continue
        indices = tuple(getattr(finding, 'pads', ()) or ())
        pins = ()
        if pad_numbers is not None and all(
                isinstance(index, int) and 0 <= index < len(pad_numbers) for index in indices):
            # Unnumbered pads (mechanical holes) have no pin to name.
            pins = tuple(dict.fromkeys(str(pad_numbers[index]) for index in indices
                                       if str(pad_numbers[index] or '')))
        results.append(Check(
            'WARN', tx('Pad stimati'), str(getattr(finding, 'message', '') or ''), 'final',
            IMPORTANCE_BEFORE_PRODUCTION, tx(GEOMETRY_HINTS['estimated_pad']), None,
            'estimated_pads', pins))
    return results


@_stoppable_run
def verify_component(directory, name, rows, *, pending=False, issues=(), emit=lambda result: None,
                     footprint_pending=False, model_offset=(0.0, 0.0, 0.0), document=None,
                     applied_symbol=None, footprint_missing=None, contact_witness=None,
                     pad_leads=None, installs=None, describe_error=None, estimated_pads=None,
                     estimated_pad_numbers=None, stop_epoch=None):
    """@brief Use scratch copies only; failed checks do not suppress independent checks.

    @param directory Component folder.
    @param name Component name.
    @param rows Applied symbol rows.
    @param pending True when the symbol editor holds unapplied edits.
    @param issues Review messages of the symbol editor: (kind, text[, pins])
        tuples (`SymbolTab.review_issue_items`; the kinds reported per row are
        dropped, the text overlaps carry their pins) or bare texts (dropped
        when equal to a per-row message).
    @param emit Callback receiving each Check as it is produced.
    @param footprint_pending True when the footprint preview holds an
        unapplied draft: the footprint checked is the applied one on disk,
        and a warning says the draft is not included.
    @param model_offset Model offset, mm, KiCad 3D axes (Y up), of the
        footprint on disk (`kicad_library.model_offset_for_anchor` of the
        anchor it was written with): the 3D import test places the model as
        the preview and the installs will, (0, 0, 0) for an unmoved anchor.
    @pre `model_offset` must be the offset of the anchor the .kicad_mod ON
        DISK was written with. The file does not record its anchor, so this
        function cannot detect a stale file (written with another anchor, an
        «Applica» not yet rewritten to disk): the import test would then
        place the model off the pads and still report OK. The caller must
        run it only when the file on disk matches the applied footprint
        (the KiCad tab's alignment gate), or report the mismatch instead.
    @param document The document's pinout and the user's choices about it
        (see `document_checks`), or None when there is none: then no
        document check is made.
    @param applied_symbol The applied symbol as the window exports it
        (graphics and metadata included), or None to rebuild it from `rows`
        (no graphics, no metadata). The exported file is compared with it
        through `symbol_text_without_metadata`.
    @param footprint_missing Translated reason the footprint does not exist
        yet (e.g. an open pad-layout question), or None: shown instead of the
        operating system's «file not found».
    @param contact_witness How the NC/NO functions of a switch contact were
        confirmed (`contact_function_checks`), None without a switch contact.
    @param pad_leads {pad number: declared terminal} of the applied package
        (`lead_hole_checks`), None or {} when no terminal is declared.
    @param installs KiCad libraries the component was installed in
        (`library_checks`), None for no library row.
    @param describe_error Callable translating a `KiCadLibraryError` for the
        SKIP rows (the window's `_kicad_error_text`), None for its own text.
    @param estimated_pads None, or `footprint_checks.check_estimated_pads` of
        the applied footprint: one WARN «Pad stimati» per finding
        (`estimated_pad_checks`); None or [] gives no row.
    @param estimated_pad_numbers The pad numbers of the SAME `Footprint`
        `estimated_pads` was computed on, in its `pads` order (the window
        passes ``[pad.number for pad in footprint.pads]``); None names no
        pad (round 2, R6: never guessed from the file's pad order).
    @param stop_epoch `current_stop_epoch()` captured when the caller
        created this run (its worker), or None for the epoch at entry; read
        by `_stoppable_run`, not by the body.
    @note The three kicad-cli reads (footprint, applied symbol, symbol on
        disk) and the 3D import run on scratch COPIES, never on the project's
        files in place, each attempt in a fresh input and output folder whose
        output is probed writable and whose input copy is opened first (a
        failure there is SKIP «not run»). Their outcome is classified by
        kicad-cli's declared exit code (`cli_read_verdict`): input refused
        (`CLI_INPUT_REFUSED`) -> one more run on fresh copies, given only the
        time the first attempt left (at least `CLI_RETRY_MIN_TIMEOUT_S`), the
        read's normal outcome when it then succeeds; refused again -> the
        differential control of that kind (`CONTROL_READS`, once per kind and
        run, `CLI_CONTROL_TIMEOUT_S`): read -> ERROR (round 2, R2: kicad-cli 10
        gives the same codes for its own failures), refused too -> SKIP with
        `cli_failure` «environment», no verdict -> SKIP «not run» (round 3);
        any other code, a crash or no output -> one more run the same way,
        and a timeout at once (never repeated), then the differential control
        of the same kind (round 4, item 1; a timeout's control only before
        `CLI_TIMEOUT_CONTROL_LIMIT_S` of the run, unless one was already
        made): control read -> ERROR with `cli_failure` «component» and a
        remedy (rename, when the name holds a character KiCad mis-handles,
        `output_names.kicad_unsafe_characters`) -- for a timeout only after
        ONE extended read (`CLI_SLOW_READ_TIMEOUT_S`,
        `CLI_SLOW_READS_PER_RUN` per run) timed out too (round 5, item 1):
        read on it -> the read's outcome with a «slow read» note; no
        extended read left -> SKIP «timeout»; control failed or not run
        -> SKIP with the cause («not_run» or «timeout») and, for a completed
        process, its exit code in `Check.cli_exit_code`; a kicad-cli that
        cannot start -> SKIP «start»; a failure of the check's own scratch
        folder -> SKIP «scratch» (round 4, item 6). Exit 0 with an
        output -> OK, or WARN showing KiCad's log when it contains error or
        warning words (never an ERROR from text). Exit 0 on a symbol library
        that lacks the component's symbol is that problem, from the check's
        own parser (`symbol_library_problem`), never «not run» (R10).
    @note Every check that can emit an ERROR is declared in
        `BLOCKING_CHECKS` with the rows that conclude it: a run that stopped
        early is judged by `concluded_required`. The stages publish in a
        fixed order (footprint, symbol, document and contacts, STEP, DRC,
        ERC, libraries, manual checks) that the table's stage closers rely on.
    @note A component file another program holds (PermissionError on a
        regular file: the footprint, the saved symbol) is a SKIP
        with a «file in uso» cause and remedy, never «missing» nor an ERROR
        (round 3, item 3); so is an OSError on the check's own scratch output.
    @note DRC and ERC run AUTOMATICALLY (user decision 2026-11-14) on a
        preview project written in the scratch folder: the APPLIED symbol and
        the footprint on disk, through `create_preview_project`, created once
        and shared with the 3D import test. A failure of kicad-cli, a timeout
        or an unreadable report is a SKIP, never an ERROR; the preview's own
        artefacts are filtered (`drc_checks`, `erc_checks`).
    @return The list of Check results.
    @note The process-wide «Solo nome principale» choice
        (`primary_pin_names_enabled`) is read ONCE when the check starts and
        passed to `symbol_inputs` (review 2026-11-16, R11-5): the check runs
        on a worker thread for tens of seconds, and a toggle in the window
        meanwhile must not make it build a symbol of the other mode.
    @note No exception escapes for a footprint without electrical pads or an
        incomplete STEP file: the ValueError raised internally is caught and
        reported as an ERROR check.
    @note A kicad-cli older than `kicad_library.MINIMUM_KICAD_VERSION` is an
        ERROR «Versione KiCad» before the exports, unless a supported KiCad
        is installed too: that one then runs the exports, with a WARN naming
        both. An unknown version gives no verdict.
    """
    results = []
    # Read once, at the start (R11-5): the whole check uses one pin-name mode.
    primary_names = primary_pin_names_enabled()
    def add(status, title, detail, *, category='technical', importance=None, remedy='',
            action=None, code='', pins=(), cli_failure='', cli_exit_code=None, file_busy=False):
        # A generator error (SymbolError) carries its own translatable text.
        # No lookup argument: `tx` also formats, so the error renders itself
        # through the catalogue (passing `tx` would fall back to Italian).
        render_again = getattr(detail, 'localized', None)
        text = None
        if callable(render_again):
            try:
                # Kept with its recipe: `localized` asks the error again
                # after a language switch.
                text = LocalizedStr(str(render_again()), ('call', detail))
            except Exception:  # noqa: BLE001 - fall back to the plain message
                text = None
        if text is None:
            if isinstance(detail, str):
                text = detail
            elif (isinstance(detail, BaseException) and len(detail.args) == 1
                  and isinstance(detail.args[0], LocalizedStr)):
                # An exception raised with a `tx` message keeps its recipe.
                text = detail.args[0]
            else:
                text = str(detail)
        if importance is None:
            # An error blocks the use; everything else is a final review
            # unless the caller ranks it.
            importance = IMPORTANCE_BLOCKS_USE if status == 'ERROR' else IMPORTANCE_FINAL_REVIEW
        result = Check(status, title, text, category,
                       importance, remedy, action, code, tuple(pins), cli_failure=cli_failure,
                       cli_exit_code=cli_exit_code, file_busy=file_busy)
        publish(result)

    def publish(result):
        results.append(result)
        emit(result)

    root = Path(directory)
    if not isinstance(name, str) or not name.strip() or name in ('.', '..') or any(
            c in name for c in '/\\:\x00'):
        add('ERROR', tx('Componente'), tx('Nome componente non valido.'), code='component_name')
        return results
    fp = root / f'{name}.pretty' / f'{name}.kicad_mod'
    step = root / f'{name}.3dshapes' / f'{name}.step'
    cli = find_kicad_cli()
    if not cli:
        publish(Check('SKIP', tx('KiCad'), tx('KiCad CLI non disponibile: apertura reale non verificata.'),
                      code='kicad_cli', needs_cli=True))
    else:
        add('OK', tx('KiCad CLI'), cli)
        # The exports below only prove something when this KiCad reads the
        # file format written (review 2026-11-03, D2); unknown -> no verdict.
        verdict = check_kicad_version(cli)
        if verdict.supported is False and verdict.newer_cli:
            add('WARN', tx('Versione KiCad'), tx(
                'KiCad {version} ({path}) è più vecchio del minimo {minimum}: '
                'la verifica usa KiCad {newer} ({newer_path}).',
                version=verdict.version, path=cli, minimum=verdict.minimum,
                newer=verdict.newer_version, newer_path=verdict.newer_cli), code='kicad_version')
            cli = verdict.newer_cli
        elif verdict.supported is False:
            add('ERROR', tx('Versione KiCad'), tx(
                'KiCad {version} ({path}) è più vecchio del minimo {minimum}: '
                'non legge i file generati. Installare KiCad {minimum} o successivo.',
                version=verdict.version, path=cli, minimum=verdict.minimum), code='kicad_version')
    if pending:
        # Said in plain words, with the way to fix it (user, 2026-11-15: the
        # former «Verificato il simbolo applicato; la bozza non è inclusa»
        # could not be understood).
        add('WARN', tx('Modifiche non applicate'),
            tx('Il simbolo ha modifiche non ancora salvate: il controllo usa il simbolo salvato.'),
            remedy=tx('Premi «Applica» nella scheda Simbolo elettrico per salvarle, poi ricontrolla.'),
            code='symbol_draft_pending')
    if footprint_pending:
        add('WARN', tx('Modifiche non applicate'),
            tx('Verificato il footprint applicato; la bozza del footprint non è inclusa.'))
    # Inspect the applied snapshot even when the editor has an unrelated draft.
    # UI review messages describe the draft and must not be its only evidence.
    # The per-pin checks carry their code and pin, so the dialog groups them
    # without parsing translated texts; an editor message that repeats one of
    # them word for word is not emitted twice.
    row_checks = []
    for row in rows:
        number = str(row.get('pin', ''))
        if not str(row.get('name') or '').strip():
            row_checks.append((tx('Nome mancante: {number}', number=number), 'missing_name', number))
        if row.get('electrical_type_reviewed') is False or (
                row.get('electrical_type', 'passive') in ('passive', 'unspecified')
                and not row.get('electrical_type_reviewed')):
            row_checks.append((tx('Tipo elettrico da verificare: {number}', number=number),
                               'electrical_type', number))
    row_messages = {message for message, _code, _pin in row_checks}
    row_kinds = {code for _message, code, _pin in row_checks}
    for issue in issues:
        # (kind, text) from `SymbolTab.review_issue_items`: a kind the per-row
        # checks below already report is skipped whatever the language of its
        # text (the editor words «Tipo elettrico da verificare: 1» its own
        # way); a bare text is compared as before.
        if isinstance(issue, tuple) and len(issue) in (2, 3):
            kind, text, pins = (issue + ((),))[:3] if len(issue) == 2 else issue
        else:
            kind, text, pins = None, issue, ()
        if (kind is not None and kind in row_kinds) or str(text) in row_messages:
            continue
        if kind == 'text_overlap':
            # Folded by the dialog into ONE row with every pin concerned.
            add('WARN', tx('Revisione simbolo'), text, category='final', code='text_overlap',
                pins=tuple(str(pin) for pin in pins))
            continue
        add('WARN', tx('Revisione simbolo'), text, category='final')
    for message, code, number in row_checks:
        # No per-pin remedy: the dialog merges the group into one row and
        # words its remedy once, with the pins as ranges, in that row's
        # tooltip (`component_check_dialog.PIN_GROUPS`).
        add('WARN', tx('Revisione simbolo'), message, category='final', code=code, pins=(number,))

    # A scratch file held by another program (a virus scanner, a lingering
    # kicad-cli) must not turn the end of the check into «Verifica interrotta».
    with tempfile.TemporaryDirectory(prefix='kicadgen-check-', ignore_cleanup_errors=True) as scratch:
        scratch = Path(scratch)
        #: Serial of the attempt folders: every kicad-cli read gets a folder
        #: of its own, never one an earlier attempt used.
        attempt_serial = iter(range(1, 1_000_000))
        #: When the kicad-cli reads of this run began: a timeout's control is
        #: skipped past `CLI_TIMEOUT_CONTROL_LIMIT_S` (round 4, item 1).
        run_started = _clock()
        #: Extended reads still allowed in this run (round 5, item 1): a
        #: one-item list so the nested `run` can spend it.
        slow_reads_left = [CLI_SLOW_READS_PER_RUN]

        def prepare(folder, materialise, output_name):
            """A fresh input and output of one attempt inside `folder`: (input file, output path).

            Raises `_ToolSideFailure` when the output folder cannot be
            created or written, or the input copy cannot be written or
            opened: a failure on the checking side, never a verdict on the
            component (round 2, R2).
            """
            out_dir = folder / 'out'
            try:
                out_dir.mkdir(parents=True)
                # Written and removed: a folder that exists but refuses files
                # (an antivirus, a policy) is found before kicad-cli runs.
                probe = out_dir / '.kicadgen-probe'
                probe.write_bytes(b'probe')
                probe.unlink()
            except OSError as exc:
                raise _ToolSideFailure(tx(
                    'Cartella temporanea del controllo non scrivibile ({error}): lettura non '
                    'verificata.', error=exc)) from exc
            try:
                source = materialise(folder / 'in')
                with open(source, 'rb') as handle:
                    handle.read(1)
            except (OSError, ValueError) as exc:
                raise _ToolSideFailure(tx(
                    'Copia temporanea del file da leggere non apribile ({error}): lettura non '
                    'verificata.', error=exc)) from exc
            return source, (out_dir / output_name if output_name else out_dir)

        #: Outcome of the differential control per kind of read ('fp',
        #: 'sym', 'step'): one control per kind and per run (round 3, item 1).
        controls = {}

        def control_outcome(kind):
            """The differential control of one kind of read: SimpleNamespace(verdict, failure, exit_code, reason).

            Runs the same kind of kicad-cli command as the component's read,
            under the same scratch conditions (a fresh input and output
            folder prepared by `prepare`), on the program's own known-good
            minimal input (`CONTROL_READS`). ``verdict`` is 'read' (KiCad read
            it: the environment reads fresh files), 'refused' (an input
            refusal, `CLI_INPUT_REFUSED`, on a sound file: the environment
            refuses fresh files) or 'failed' (no verdict: the control could not
            be prepared, timed out, did not start, or ended with another
            code); ``failure`` and ``exit_code`` are what the read row
            declares for a 'failed' control, ``reason`` its translated cause.
            Cached in `controls`: one control per kind in one run.
            """
            if kind in controls:
                return controls[kind]
            relative, text, make_args, pattern, output_name = CONTROL_READS[kind]
            folder = scratch / f'control-{kind}'
            outcome = SimpleNamespace(verdict='failed', failure='not_run', exit_code=None, reason='')

            def materialise(target):
                """The control's main input plus the files it needs beside it (`CONTROL_EXTRA_FILES`)."""
                for extra, extra_text in CONTROL_EXTRA_FILES.get(kind, {}).items():
                    bytes_input(extra, extra_text.encode('utf-8'))(target)
                return bytes_input(relative, text.encode('utf-8'))(target)
            try:
                source, output = prepare(folder, materialise, output_name)
                proc = _run_cli([cli, *map(str, make_args(source, output))],
                                timeout=CLI_CONTROL_TIMEOUT_S, cwd=str(neutral_working_dir(cli)))
            except _ToolSideFailure as exc:
                # The control's own scratch side (round 4, item 6).
                outcome.failure, outcome.reason = 'scratch', exc.args[0]
            except subprocess.TimeoutExpired:
                outcome.failure = 'timeout'
                outcome.reason = tx('nessuna risposta entro {seconds} secondi',
                                    seconds=CLI_CONTROL_TIMEOUT_S)
            except OSError as exc:
                outcome.failure = 'start'
                outcome.reason = tx('kicad-cli non avviato: {error}', error=exc)
            else:
                try:
                    produced = _output_produced(output, pattern)
                except OSError as exc:
                    produced = None
                    outcome.failure = 'scratch'
                    outcome.reason = tx('Risultato di kicad-cli non leggibile nella cartella '
                                        'temporanea ({error}): lettura non verificata.', error=exc)
                if produced is not None:
                    verdict = cli_read_verdict(proc.returncode, produced)
                    if verdict == 'read':
                        outcome.verdict, outcome.failure = 'read', ''
                    elif verdict == 'unreadable':
                        outcome.verdict, outcome.failure = 'refused', 'environment'
                    else:
                        outcome.exit_code = proc.returncode
                        outcome.reason = tx('codice {code}', code=_exit_code_text(proc.returncode))
            controls[kind] = outcome
            return outcome

        def run(title, materialise, make_args, *, pattern=None, output_name=None, code='',
                explain=None, control=None):
            """One kicad-cli read on fresh scratch copies; returns its output path when KiCad read it, else None.

            `materialise(folder)` writes a fresh copy of the input inside
            `folder` and returns the file to open; `make_args(input, output)`
            builds the arguments; the output is a folder searched with
            `pattern`, or the file `output_name`. `explain()` returns the
            component problem the check's own parser finds in the input
            (status, detail, importance, remedy, action), or None. `control`
            is the kind of read (a key of `CONTROL_READS`) whose differential
            control decides a failure that repeats; None attributes a
            repeated refusal to the component without a control, and leaves
            any other failure unattributed.
            """
            if not cli:
                publish(Check('SKIP', title, tx('Richiede KiCad CLI.'), code=code, needs_cli=True))
                return None
            retry = tx('Ripeti il controllo; se si ripete, chiudi gli altri programmi KiCad.')
            # Round 4, item 6: a failure of the check's OWN scratch side is
            # declared «scratch», so the window names a temporary folder or
            # an antivirus, never KiCad.
            scratch_remedy = tx("Ripeti il controllo; se si ripete, controlla che l'antivirus non "
                                'blocchi la cartella temporanea e che il disco non sia pieno.')
            def attempt(budget):
                """One kicad-cli run of the read on fresh scratch copies: (proc, log, verdict, output, problem), or None.

                None means the attempt already published the read's SKIP
                («scratch» or «start»); ``verdict`` is 'timeout' with no
                process when kicad-cli did not answer within `budget`;
                ``problem`` is the check's own parser's verdict on an exit 0
                (`explain`), published by `run` itself so every row that may
                be an ERROR is emitted where its `code` is a parameter.
                """
                folder = scratch / f'read-{next(attempt_serial)}'
                try:
                    source, output = prepare(folder, materialise, output_name)
                    proc = _run_cli([cli, *map(str, make_args(source, output))], timeout=budget,
                                    cwd=str(neutral_working_dir(cli)))
                except _ToolSideFailure as exc:
                    add('SKIP', title, exc.args[0], code=code, remedy=scratch_remedy,
                        cli_failure='scratch')
                    return None
                except subprocess.TimeoutExpired:
                    return None, '', 'timeout', None, None
                except OSError as exc:
                    # kicad-cli could not be STARTED: the control would not
                    # start either, so none is made.
                    add('SKIP', title, tx('kicad-cli non avviato: {error}', error=exc), code=code,
                        remedy=tx('Ripeti il controllo; se si ripete, verifica che KiCad sia '
                                  "installato e non bloccato dall'antivirus."),
                        cli_failure='start')
                    return None
                # The scratch folder is shortened in both spellings KiCad
                # prints (Windows separators and forward slashes).
                log = ((proc.stdout or '') + '\n' + (proc.stderr or '')).strip()
                log = log.replace(str(scratch), '…').replace(scratch.as_posix(), '…')
                try:
                    # Round 3, item 3: inside the protected block -- an
                    # antivirus quarantining the SVG made this OSError escape
                    # into the footprint's «no footprint» ERROR.
                    valid = _output_produced(output, pattern)
                except OSError as exc:
                    add('SKIP', title, tx('Risultato di kicad-cli non leggibile nella cartella '
                                          'temporanea ({error}): lettura non verificata.', error=exc),
                        code=code, remedy=scratch_remedy, cli_failure='scratch')
                    return None
                # Exit 0 is not the whole story (round 2, R10): a library
                # holding no symbol, or not the component's, exits 0 with no
                # output (or with another symbol's drawing). The check's own
                # parser says what the component lacks; never «not run» for
                # it, and no repeat is needed to say so.
                problem = (explain() if proc.returncode == CLI_EXIT_SUCCESS and explain is not None
                           else None)
                return proc, log, cli_read_verdict(proc.returncode, valid), output, problem

            # Round 3, item 4: the repeat of a failure gets only what the first
            # attempt left of the read's budget (never less than
            # CLI_RETRY_MIN_TIMEOUT_S): a refusal after 59 s followed by a
            # timeout no longer doubles the worst case.
            budget = CLI_READ_TIMEOUT_S
            proc, log, verdict, output, problem = None, '', 'not_run', None, None
            for number in (1, 2):
                started = _clock()
                result = attempt(budget)
                if result is None:
                    return None
                proc, log, verdict, output, problem = result
                # Round 4, item 1: a timeout is no longer a verdict of its
                # own -- the differential control below decides whether the
                # file or the tool does not answer. Never repeated with the
                # normal budget: this attempt used all of it, so the repeat
                # would get only the floor (`CLI_RETRY_MIN_TIMEOUT_S`), and a
                # shortened repeat that times out proves less than this full
                # attempt did.
                # A failure is the COMPONENT's only when it repeats (round 2,
                # R2; round 4, item 1 for every failure, not only a refusal):
                # kicad-cli 10 gives the same codes 2 and 3 for its own
                # failures (an output it cannot write, an input another
                # process holds; `CLI_INPUT_REFUSED`), and a crash may be
                # transient. One more run on a fresh copy into a fresh output
                # folder settles it.
                if problem is not None or verdict in ('read', 'timeout') or number == 2:
                    break
                budget = max(CLI_RETRY_MIN_TIMEOUT_S, budget - (_clock() - started))
            # Round 5, item 1: a timeout whose control of the same kind READS
            # gets ONE extended read (`CLI_SLOW_READ_TIMEOUT_S`, at most
            # `CLI_SLOW_READS_PER_RUN` per run) before it is blamed on the
            # component: a sound but large file (a STEP model of tens of MB
            # imported on a slow PC) may need more than the normal budget,
            # and one 60 s criterion alone made it a permanent ERROR. Read on
            # the extended attempt -> the read's normal outcome, with a note
            # that it is slow; timed out again -> ERROR «component» below (a
            # file that keeps KiCad busy far longer than a sound control of
            # its kind is the file's); any other outcome is judged like a
            # first attempt's against the control already read.
            # DECLARED FRAGILITY: the extended read only says that the file
            # took longer than `CLI_SLOW_READ_TIMEOUT_S` on THIS computer at
            # THIS moment; a sound file slower than that bound (or a PC busy
            # with something else during those minutes) is still declared
            # the component's. The bound is one of patience, not a
            # measurement of the file; what would make it superfluous is a
            # read whose progress kicad-cli reports, which it does not.
            slow_note = ''
            extended = False
            if (verdict == 'timeout' and control is not None
                    and slow_reads_left[0] > 0
                    and (control in controls
                         or _clock() - run_started <= CLI_TIMEOUT_CONTROL_LIMIT_S)
                    and control_outcome(control).verdict == 'read'):
                slow_reads_left[0] -= 1
                extended = True
                first_budget, budget = budget, CLI_SLOW_READ_TIMEOUT_S
                result = attempt(budget)
                if result is None:
                    return None
                proc, log, verdict, output, problem = result
                if verdict == 'read':
                    slow_note = tx('Lettura lenta: KiCad non aveva finito entro {seconds} secondi '
                                   'e ha letto il file solo con il tempo esteso di {extended} '
                                   'secondi.', seconds=math.ceil(first_budget),
                                   extended=CLI_SLOW_READ_TIMEOUT_S)
            if problem is not None:
                # The check's own parser's verdict on an exit 0 (round 2,
                # R10), from the attempt that ended the reads.
                status, detail, importance, remedy, action = problem
                add(status, title, detail, importance=importance, remedy=remedy,
                    action=action, code=code)
                return None
            if verdict == 'unreadable':
                # Refused twice, on two fresh copies. Round 3, item 1: an
                # environment that refuses EVERY fresh file (an antivirus
                # holding new files in %TEMP%, a broken KiCad user
                # configuration) gives the same two refusals, so the program's
                # own known-good file of the same kind is read under the same
                # conditions: read -> the defect is the component's (ERROR);
                # refused too -> the environment's (SKIP «environment»); no
                # verdict from the control -> not attributed (SKIP «not run»).
                outcome = control_outcome(control) if control is not None else None
                if outcome is None or outcome.verdict == 'read':
                    add('ERROR', title, log or tx('KiCad dichiara il file non valido (codice {code}).',
                                                  code=proc.returncode),
                        code=code)
                elif outcome.verdict == 'refused':
                    add('SKIP', title, tx(
                        'KiCad rifiuta anche un file di controllo sicuramente valido, creato dal '
                        'programma nella stessa cartella temporanea: il rifiuto viene dal computer '
                        '(antivirus, configurazione di KiCad), non dal componente. Lettura non '
                        'verificata.') + ('\n' + log if log else ''), code=code,
                        remedy=tx("Controlla che l'antivirus non blocchi i file nuovi nella cartella "
                                  'temporanea e che KiCad si apra normalmente, poi ripeti il '
                                  'controllo.'),
                        cli_failure='environment')
                else:
                    add('SKIP', title, tx(
                        'KiCad ha rifiutato due volte il file del componente, ma non ha letto '
                        'neppure il file di controllo del programma ({reason}): non si può dire '
                        'se il difetto è del componente. Lettura non verificata.',
                        reason=outcome.reason) + ('\n' + log if log else ''), code=code,
                        remedy=retry, cli_failure=outcome.failure, cli_exit_code=outcome.exit_code)
                return None
            if verdict in ('not_run', 'timeout'):
                # A failure kicad-cli did not declare as a refusal: a crash
                # (e.g. 0xC0000409), another exit code, exit 0 without an
                # output (KiCad 10 exits 0 when it cannot write its own
                # output, observed 2026-11-15), or no answer in time. Round 4,
                # item 1: the component's OWN input can cause every one of
                # them -- a footprint or symbol named «x%NOPE%y» makes
                # kicad-cli 10 crash on every read while the DRC and ERC of
                # the same run finish -- so the differential control of the
                # same kind decides: read -> the failure is the component's
                # (ERROR, `cli_failure` «component»); failed too, or not run
                # -> the tool's or the computer's (SKIP with the cause, as
                # before round 4).
                outcome = None
                if control is not None and (verdict != 'timeout' or control in controls
                                            or _clock() - run_started <= CLI_TIMEOUT_CONTROL_LIMIT_S):
                    outcome = control_outcome(control)
                # Round 5, item 1: a TIMEOUT is the component's only after the
                # extended read timed out too; one that got no extended read
                # (the run's `CLI_SLOW_READS_PER_RUN` already spent) stays
                # unattributed below even when its control reads.
                attributed = (outcome is not None and outcome.verdict == 'read'
                              and (verdict != 'timeout' or extended))
                if attributed:
                    if verdict == 'timeout':
                        detail = tx('KiCad non ha finito di leggere questo file entro {seconds} '
                                    'secondi, mentre ha letto un file di controllo dello stesso tipo '
                                    'creato dal programma: è il file del componente a bloccare '
                                    'KiCad.', seconds=math.ceil(budget))
                    elif proc.returncode == CLI_EXIT_SUCCESS:
                        detail = tx('KiCad termina senza errori ma non produce nulla leggendo questo '
                                    'file, mentre legge normalmente un file di controllo dello stesso '
                                    'tipo creato dal programma: il difetto è del file del componente.')
                    else:
                        detail = tx('KiCad si chiude leggendo questo file (codice {code}), mentre '
                                    'legge normalmente un file di controllo dello stesso tipo creato '
                                    'dal programma: il difetto è del file del componente.',
                                    code=_exit_code_text(proc.returncode))
                    unsafe = kicad_unsafe_characters(name)
                    remedy = (tx('Il nome del componente contiene «{characters}», che KiCad non '
                                 'gestisce nei nomi: rinomina il progetto senza questo carattere '
                                 '(File > Rinomina progetto...), rigenera i file e ripeti il '
                                 'controllo.', characters=' '.join(unsafe)) if unsafe else
                              tx('Rigenera i file del componente, poi ripeti il controllo; se si '
                                 'ripete, il file contiene qualcosa che KiCad non sa leggere.'))
                    add('ERROR', title, detail + ('\n' + log if log else ''), code=code,
                        remedy=remedy, cli_failure='component')
                    return None
                # Not attributed: the tool's own failure, said as before
                # round 4, with the control's confirmation when it failed too.
                if verdict == 'timeout':
                    reason = tx('kicad-cli non ha risposto entro {seconds} secondi.',
                                seconds=math.ceil(budget))
                    failure, exit_code = 'timeout', None
                else:
                    reason = (tx('kicad-cli è terminato con il codice {code} senza dichiarare il '
                                 'file non valido: lettura non verificata.',
                                 code=_exit_code_text(proc.returncode))
                              if proc.returncode else
                              tx('kicad-cli non ha prodotto alcun risultato: lettura non verificata.'))
                    failure, exit_code = 'not_run', proc.returncode
                if outcome is not None and outcome.verdict == 'read':
                    # Only a timeout gets here with a control that read: the
                    # run's extended read was already spent on another file.
                    reason += '\n' + tx('KiCad ha letto un file di controllo dello stesso tipo, ma '
                                        'il tempo esteso di questo controllo è già stato dato a un '
                                        "altro file: ripeti il controllo per dare più tempo a "
                                        'questo.')
                elif outcome is not None:
                    reason += '\n' + tx('Anche il file di controllo del programma non è stato letto '
                                        '({reason}): il problema viene da KiCad o dal computer, non '
                                        'dal componente.',
                                        reason=outcome.reason or tx('rifiutato da KiCad'))
                # A failure of the CHECKING TOOL is «not run», never an ERROR:
                # an ERROR forbids the installation in the guided KiCad tab,
                # and a slow PC or an antivirus holding kicad-cli would then
                # block a sound component for good (review 2026-11-15, H1).
                # The exit code is declared on the row (round 3, item 5).
                add('SKIP', title, reason + ('\n' + log if log else ''), code=code, remedy=retry,
                    cli_failure=failure, cli_exit_code=exit_code)
                return None
            # Exit 0 with an output: KiCad read the file. Its log lines are in
            # KiCad's interface language and are only SHOWN: words that look
            # like an error or a warning give at most a WARN with the log,
            # never an ERROR (an error is recognised by its code, not by
            # translated text). A passed read says so in the interface
            # language; the scratch folder is shortened in the log.
            worded = bool(re.search(
                r'^[\s*]*(?:error|errore|fehler|erreur|fallita|fallito|impossibile|échec|echec|'
                r'warning|avviso|attenzione|warnung|avertissement)\b|\b(?:failed|unable)\s+to\b|'
                r'\bcannot\s+(?:read|open|load|import)\b', log, re.I | re.M))
            detail = log if worded else tx('File letto da KiCad.')
            if slow_note:
                # Round 5, item 1: read only on the extended attempt.
                detail = detail + '\n' + slow_note
            add('WARN' if worded else 'OK', title, detail, code=code)
            return output

        def bytes_input(relative, payload):
            """A `materialise` writing `payload` at `relative` inside the attempt's input folder."""
            def materialise(folder):
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                return path
            return materialise

        pad_ids = None
        footprint_text = None
        #: The footprint bytes read ONCE for the checks (R9-6): the preview
        #: of the 3D import, DRC and ERC is built from them, never from a
        #: second read of the file the window may rewrite meanwhile.
        footprint_bytes = None
        overlap_pairs = []
        symbol = None
        try:
            data = _read_component_file(fp)
            footprint_bytes = data
            if data.startswith(UTF8_BOM):
                # A BOM made the root check fail with a generic «File footprint
                # non valido» («invalid footprint file») although the file is
                # readable; kicad-cli refuses it, the install strips it
                # (review 2026-10-29, L4). Named here, the checks go on.
                add('WARN', tx('Footprint su disco'),
                    tx('Il file {path} inizia con un BOM UTF-8 (byte EF BB BF): KiCad non legge questa copia nella cartella del componente. La copia installata nella libreria viene scritta senza BOM.', path=fp),
                    code='footprint_bom')
            # Same decoding as the install (BOM dropped, universal newlines),
            # so the check reads the footprint the library will receive.
            text = _decode_utf8(data, fp)
            _single_footprint(text)
            pads = [_pad_geometry(pad) for pad in _children(text, 'pad')]
            pad_ids = {p[0] for p in pads if len(p) > 2 and p[1] != 'np_thru_hole' and p[0]}
            if not pad_ids:
                raise ValueError(tx('Nessun pad elettrico trovato.'))
            add('OK', tx('Footprint su disco'), str(fp), code='footprint_file')
            # The copper rules of the footprint tab, on the file the library
            # receives (real run 2026-11-14: overlapping pads were not seen).
            footprint_text = text
            for result in footprint_geometry_checks(text):
                publish(result)
            # The overlapping pad pairs by INSTANCE (index), for the DRC
            # dedupe: two pads may share a number.
            from .footprint_checks import check_pads_overlap
            overlap_pairs.extend(frozenset(finding.pads) for finding in
                                 check_pads_overlap(SimpleNamespace(pads=_file_copper_pads(text))))
            for result in courtyard_checks(text):
                publish(result)
            # The declared terminals against the holes the library receives.
            for result in lead_hole_checks(text, pad_leads):
                publish(result)
            if any(not p[0] and p[1] != 'np_thru_hole' for p in pads):
                add('WARN', tx('Pad senza numero'), tx('Pad di rame senza numero esclusi dalla corrispondenza elettrica.'))
            models = _children(text, 'model')
            if not models and step.is_file() and installs is None:
                # By design the component-folder .kicad_mod carries no model
                # node: the installation and the preview add it (coordinator,
                # 2026-11-13). An informational final review with NO automatic
                # fix (KiCad workflow review): a written ${KIPRJMOD} link would
                # only turn this into an inconclusive SKIP, and a regeneration
                # drops it anyway. With the «Libreria KiCad» row (`installs`
                # given) it is not made: that row already says to install and
                # compares the installed model link (practical test 2026-11-14:
                # two rows, one thing to do).
                add('WARN', tx('Collegamento 3D originale'), tx('Il footprint su disco non contiene un riferimento 3D; il test di importazione lo aggiunge soltanto nella copia temporanea.'),
                    remedy=tx('Installa la libreria dalla scheda KiCad e verifica il modello STEP nella libreria installata.'),
                    code='model_link_missing', importance=IMPORTANCE_FINAL_REVIEW)
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
                        add('ERROR', tx('Collegamento 3D originale'), tx('Trasformazione {field} non valida.', field=field), code='model_link')
                    elif tuple(values) != default:
                        changed_transform = True
                if changed_transform:
                    add('WARN', tx('Collegamento 3D originale'), tx('Trasformazione 3D originale non verificata: la prova temporanea usa scala e orientamento normalizzati.'), code='model_link')
                args = _node_arguments(model)
                reference = args[0] if args else ''
                expanded = os.path.expandvars(reference)
                if not reference:
                    add('ERROR', tx('Collegamento 3D originale'), tx('Riferimento modello vuoto.'), code='model_link')
                elif '${' in expanded or re.search(r'%[^%]+%', expanded):
                    add('SKIP', tx('Collegamento 3D originale'), tx('Variabile KiCad da risolvere nel progetto di destinazione: {reference}', reference=reference), code='model_link')
                elif not Path(expanded).is_absolute():
                    add('SKIP', tx('Collegamento 3D originale'), tx('Percorso relativo da verificare nel progetto di destinazione: {reference}', reference=reference), code='model_link')
                elif not Path(expanded).is_file():
                    add('ERROR', tx('Collegamento 3D originale'), tx('Modello non trovato: {reference}', reference=reference), code='model_link')
                else:
                    add('OK', tx('Collegamento 3D originale'), reference, code='model_link')
            # kicad-cli reads a scratch COPY of the bytes checked above, never
            # the project's .pretty in place: the window may write the folder
            # meanwhile (a Windows sharing violation, or a file read half
            # written), and the copy is exactly what the rules above saw
            # (review 2026-11-15, M4).
            # Without a leading BOM (round 2, R11): kicad-cli 10 refuses a
            # file that starts with one (exit 2, measured), the installation
            # writes the library copy without it, and the WARN above already
            # names it -- an ERROR here would block a footprint the install
            # repairs. Every other byte (newlines included) is kept.
            fp_bytes = data[len(UTF8_BOM):] if data.startswith(UTF8_BOM) else data
            # The footprint is looked up by FILE name (kicad-cli 10.0, measured:
            # a different `(footprint "...")` inside still exports), and the
            # copy is named after the component, so exit 0 without an output
            # cannot come from the file's content: no `explain` here.
            run(tx('Lettura footprint in KiCad'),
                bytes_input(Path(f'{name}.pretty') / f'{name}.kicad_mod', fp_bytes),
                lambda source, output: ['fp', 'export', 'svg', '--footprint', name, '-o', output,
                                        source.parent],
                pattern='*.svg', code='kicad_footprint_read', control='fp')
        except _ComponentFileBusy as exc:
            # Round 3, item 3: the footprint exists but cannot be read now
            # (held by another program): not «no footprint», not a defect.
            add('SKIP', tx('Footprint su disco'), exc.detail, remedy=exc.remedy, code='footprint_file',
                file_busy=True)
        except FileNotFoundError:
            # No footprint yet: said in words, with its cause when the caller
            # knows it (an open pad-layout question), never as an errno.
            add('ERROR', tx('Footprint su disco'),
                footprint_missing or tx('Nessun footprint nella cartella del componente.'),
                remedy=tx('Rispondi alla domanda con «Rigenera footprint»: il footprint viene '
                          'scritto dopo la risposta.') if footprint_missing else
                tx('Genera il footprint, poi ripeti la verifica.'),
                code='footprint_missing', importance=IMPORTANCE_BLOCKS_USE)
        except (OSError, ValueError, RuntimeError) as exc:
            add('ERROR', tx('Footprint su disco'), exc, code='footprint_file')

        try:
            pins, body = symbol_inputs(rows, primary_names=primary_names)
            from ..generators.symbol import symbol_pin_names_hidden
            symbol = applied_symbol if isinstance(applied_symbol, str) and applied_symbol else \
                serialize_symbol(name, pins, footprint=f'KiCadGen:{name}', body=body,
                                 properties=symbol_property_positions(rows),
                                 pin_names_hidden=symbol_pin_names_hidden(rows))
            ids = {str(p.number) for p in pins}
            if pad_ids is None:
                add('SKIP', tx('Corrispondenza pin-pad'), tx('Footprint non leggibile.'), code='pin_pad')
            elif ids != pad_ids:
                add('ERROR', tx('Corrispondenza pin-pad'),
                    tx('Solo simbolo: {symbol_only}; solo footprint: {footprint_only}', symbol_only=sorted(ids - pad_ids), footprint_only=sorted(pad_ids - ids)),
                    code='pin_pad')
            else:
                add('OK', tx('Corrispondenza pin-pad'), tx('{count} numeri elettrici corrispondenti.', count=len(ids)),
                    code='pin_pad')
            def sym_args(source, output):
                """The `sym export svg` arguments of one attempt (every symbol of the library)."""
                return ['sym', 'export', 'svg', '-o', output, source]

            def symbol_problem(text, saved_file):
                """The component problem of a symbol library that lacks the component's symbol, or None."""
                detail = symbol_library_problem(text, name)
                if detail is None:
                    return None
                if saved_file:
                    # The file the installation ships: re-exported from the
                    # applied symbol, like a stale export (same remedy).
                    return ('WARN', detail, IMPORTANCE_BEFORE_PRODUCTION, export_remedy,
                            ACTION_REEXPORT_SYMBOL)
                return ('ERROR', detail, IMPORTANCE_BLOCKS_USE,
                        tx('Correggi il simbolo nella scheda «Simbolo elettrico» e premi «Applica», '
                           'poi ripeti la verifica.'), None)

            # Defined before the reads: `symbol_problem` names it.
            # A stale or missing exported symbol is to be fixed before
            # production (user decision 2026-11-13): importance 2, «DA RIVEDERE
            # PRIMA DELL’USO» until fixed. Other technical warnings, the
            # missing model link included, stay final reviews (importance 3).
            # The KiCad tab has no export command since 2026-11-15 (the
            # installation brings the symbol): the row's own action rewrites it.
            export_remedy = tx('Usa «Riesporta il simbolo»: riscrive il .kicad_sym nella cartella '
                               'del componente dal simbolo applicato.')
            run(tx('Simbolo applicato: lettura in KiCad'),
                bytes_input(f'{name}.kicad_sym', symbol.encode('utf-8')), sym_args,
                pattern='*.svg', code='kicad_symbol_read', control='sym',
                explain=lambda: symbol_problem(symbol, False))
            saved = root / f'{name}.kicad_sym'
            saved_bytes, busy = None, None
            try:
                saved_bytes = _read_component_file(saved) if saved.is_file() else None
            except _ComponentFileBusy as exc:
                busy = exc
            except FileNotFoundError:
                saved_bytes = None  # removed between the test and the read: as missing
            if busy is not None:
                # Round 3, item 3: held by another program -- the KiCad read
                # of it is not made now: a SKIP with the cause (it closes the
                # symbol stage like the read itself), never an ERROR.
                add('SKIP', tx('Simbolo su disco: lettura in KiCad'), busy.detail, remedy=busy.remedy,
                    code='kicad_symbol_file_read', file_busy=True)
            elif saved_bytes is not None:
                # Read ONCE: the comparison and kicad-cli see the same bytes,
                # and kicad-cli reads a scratch copy, never the component's
                # file in place (review 2026-11-15, M4). A leading BOM is
                # dropped, as the installation's own reading drops it
                # (`_component_symbol`): kicad-cli 10 refuses such a copy
                # (exit 2, measured), and an ERROR would block a symbol the
                # installation ships correctly (round 2, R11, same class).
                if saved_bytes.startswith(UTF8_BOM):
                    saved_bytes = saved_bytes[len(UTF8_BOM):]
                saved_text = saved_bytes.decode('utf-8')
                if (symbol_text_without_metadata(saved_text)
                        != symbol_text_without_metadata(symbol)):
                    add('WARN', tx('Simbolo esportato'), tx('Il file nella cartella componente differisce dal simbolo applicato: riesportarlo.'),
                        remedy=export_remedy, action=ACTION_REEXPORT_SYMBOL, code='symbol_export_stale',
                    importance=IMPORTANCE_BEFORE_PRODUCTION)
                run(tx('Simbolo su disco: lettura in KiCad'),
                    bytes_input(f'{name}.kicad_sym', saved_bytes), sym_args,
                    pattern='*.svg', code='kicad_symbol_file_read', control='sym',
                    explain=lambda: symbol_problem(saved_text, True))
            else:
                # The same export writes a missing file: the same fix applies.
                add('WARN', tx('Simbolo esportato'), tx('Nessun .kicad_sym nella cartella componente; verificata la versione applicata in memoria.'),
                    remedy=export_remedy, action=ACTION_REEXPORT_SYMBOL, code='symbol_export_missing',
                    importance=IMPORTANCE_BEFORE_PRODUCTION)
        except (OSError, ValueError, TypeError) as exc:
            add('ERROR', tx('Simbolo'), exc, code='symbol_error')

        # Pads whose values the program derived or estimated (review
        # 2026-11-15, A5): never silent in the component check.
        # Their pads are named through the numbers of the footprint they were
        # computed on, never through the file's pad order (round 2, R6).
        for result in estimated_pad_checks(estimated_pads, estimated_pad_numbers):
            publish(result)

        # The document pinout against the pads on disk and the applied names.
        for result in document_checks(document, rows, pad_ids):
            publish(result)
        # The NC/NO functions of a switch contact, as the analysis confirmed them,
        # and pin names that contradict them.
        # The pin roles first (round 3): their ERRORs then come before the
        # witness row, so nothing of this stage follows a blocking row late.
        # Their conclusion is still declared by the next stage's first row
        # (`BLOCKING_CHECKS['pin_role_contradiction']`).
        for result in pin_role_checks(rows):
            publish(result)
        for result in contact_function_checks(contact_witness, rows):
            publish(result)

        # ONE preview project for the 3D import, the DRC and the ERC
        # (user decision 2026-11-14): the footprint on disk and the APPLIED
        # symbol, written to scratch/applied so the preview schematic shows
        # what the user applied, not an older exported file. Works without
        # a STEP (the board then has no model node).
        preview, preview_error = None, ''
        if footprint_bytes is not None:
            try:
                applied_dir = scratch / 'applied'
                applied_dir.mkdir(parents=True, exist_ok=True)
                if isinstance(symbol, str) and symbol:
                    _atomic_write_text(applied_dir / f'{name}.kicad_sym', symbol)
                # The bytes the checks above read (review 2026-11-16, R9-6):
                # DRC and ERC judge the footprint those rows judged, even if
                # the window rewrote the file in between.
                checked_fp = applied_dir / f'{name}.pretty' / f'{name}.kicad_mod'
                checked_fp.parent.mkdir(parents=True, exist_ok=True)
                checked_fp.write_bytes(footprint_bytes)
                wrl = root / f'{name}.3dshapes' / f'{name}.wrl'
                artifacts = ComponentArtifacts(name, applied_dir, checked_fp, step,
                                               wrl if wrl.is_file() else None,
                                               model_offset=tuple(model_offset))
                preview = create_preview_project(artifacts, scratch / 'project')
            except (OSError, ValueError, RuntimeError) as exc:
                preview_error = (describe_error(exc) if callable(describe_error)
                                 and isinstance(exc, KiCadLibraryError) else str(exc))
        elif fp.is_file():
            # On disk but not read above (held by another program): the
            # «Footprint su disco» row already says why.
            preview_error = tx('Footprint non leggibile.')
        else:
            preview_error = tx('Footprint mancante.')

        try:
            if not step.is_file():
                # Delivery without STEP (user decision A, 2026-11-12): a PC
                # without FreeCAD delivers the footprint alone. A missing model
                # is a warning, never an ERROR («NON USARE»); a STEP that exists
                # but is truncated stays an ERROR below.
                add('WARN', tx('Modello STEP'), tx('Nessun modello STEP: il componente sarà consegnato a KiCad senza vista 3D.'),
                    code='step_missing')
                add('SKIP', tx('Importazione 3D in KiCad'), tx('Nessun modello STEP da importare.'))
                raise _NoStep()
            # A STEP held by another program stays a «Modello STEP» ERROR here
            # (round 3): as a SKIP the window, which waits for no read of the
            # STEP, would allow the installation with its integrity unchecked.
            if not step_file_is_complete(step):
                raise ValueError(tx('STEP incompleto o troncato.'))
            add('OK', tx('Integrità STEP'), str(step), code='step_integrity')
            if preview is not None:
                def board_input(folder, project=preview):
                    """A fresh copy of the whole preview project (its paths are ${KIPRJMOD})."""
                    import shutil
                    shutil.copytree(project.directory, folder)
                    return folder / project.board_path.relative_to(project.directory)

                output = run(tx('Importazione 3D in KiCad'), board_input,
                             lambda source, target: ['pcb', 'export', 'step', '--no-board-body',
                                                     '-o', target, source],
                             output_name='assembly.step', code='kicad_3d_import',
                             control='step')
                if output is not None:
                    try:
                        solid = re.search(r'\b(MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS)\s*\(',
                                          output.read_text(errors='replace'))
                    except OSError:
                        # The check's own scratch output became unreadable
                        # (round 3, item 3, same class): the solids are not
                        # confirmed, never a «Modello STEP» ERROR.
                        solid = None
                    if not solid:
                        add('WARN', tx('Solidi 3D'), tx('Esportazione riuscita, ma presenza di solidi non confermata.'),
                            code='solids_unconfirmed')
            elif fp.is_file():
                add('SKIP', tx('Importazione 3D in KiCad'),
                    tx('Anteprima KiCad non creata: {error}', error=preview_error), code='kicad_3d_import')
            else:
                add('SKIP', tx('Importazione 3D in KiCad'), tx('Footprint mancante.'))
        except _NoStep:
            pass
        except (OSError, ValueError, RuntimeError) as exc:
            add('ERROR', tx('Modello STEP'), exc, code='step_model')

        def run_json(args, output, key):
            """(report, '') of one kicad-cli JSON report, or (None, reason): never an ERROR."""
            try:
                proc = _run_cli([cli, *map(str, args)], timeout=60, cwd=str(neutral_working_dir(cli)))
            except subprocess.TimeoutExpired:
                return None, tx('kicad-cli non ha risposto entro 60 secondi.')
            except OSError as exc:
                return None, str(exc)
            if proc.returncode:
                first = next((line.strip() for line in (proc.stderr or '').splitlines() if line.strip()), '')
                return None, (tx('kicad-cli ha restituito il codice {code}: {line}', code=proc.returncode,
                                 line=first) if first
                              else tx('kicad-cli ha restituito il codice {code}.', code=proc.returncode))
            try:
                report = json.loads(Path(output).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return None, tx('kicad-cli non ha scritto un rapporto leggibile.')
            if not isinstance(report, dict) or not isinstance(report.get(key), list):
                return None, tx('kicad-cli non ha scritto un rapporto leggibile.')
            return report, ''

        # DRC of the preview board: KiCad's default rules on the footprint.
        drc_title = tx('DRC del footprint in KiCad')
        if preview is None:
            add('SKIP', drc_title, tx('DRC non eseguito: {reason}', reason=preview_error), code='drc')
        elif not cli:
            publish(Check('SKIP', drc_title, tx('Richiede KiCad CLI.'), code='drc', needs_cli=True))
        else:
            out = scratch / 'drc.json'
            report, reason = run_json(['pcb', 'drc', '--format', 'json', '--severity-error',
                                       '--severity-warning', '--units', 'mm', '-o', out,
                                       preview.board_path], out, 'violations')
            if report is None:
                add('SKIP', drc_title, tx('DRC non eseguito: {reason}', reason=reason), code='drc')
            else:
                try:
                    board_pads, outline = preview_board_pads(preview.board_path.read_text(encoding='utf-8'))
                except (OSError, ValueError, IndexError):
                    board_pads, outline = [], set()
                for result in drc_checks(report, board_pads, overlap_pairs, outline):
                    publish(result)

        # ERC of the preview schematic: the applied symbol placed alone.
        erc_title = tx('ERC del simbolo in KiCad')
        power_pins = []
        if preview is not None and not (isinstance(symbol, str) and symbol):
            add('SKIP', erc_title, tx('ERC non eseguito: {reason}', reason=tx(
                'il simbolo applicato non si può costruire; vedi la riga «Simbolo».')), code='erc')
        elif preview is None or preview.schematic_path is None:
            skipped = getattr(preview, 'schematic_skipped', None)
            reason = preview_error or (tx(SCHEMATIC_SKIP_TEXTS[skipped]) if skipped in SCHEMATIC_SKIP_TEXTS
                                       else tx('schema di anteprima non creato ({reason}).',
                                               reason=skipped or '?'))
            add('SKIP', erc_title, tx('ERC non eseguito: {reason}', reason=reason), code='erc')
        elif not cli:
            publish(Check('SKIP', erc_title, tx('Richiede KiCad CLI.'), code='erc', needs_cli=True))
        else:
            out = scratch / 'erc.json'
            report, reason = run_json(['sch', 'erc', '--format', 'json', '--severity-error',
                                       '--severity-warning', '--units', 'mm', '-o', out,
                                       preview.schematic_path], out, 'sheets')
            if report is None:
                add('SKIP', erc_title, tx('ERC non eseguito: {reason}', reason=reason), code='erc')
            else:
                try:
                    pins = _schematic_pin_uuids(preview.schematic_path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    pins = {}
                checks, power_pins = erc_checks(report, pins)
                for result in checks:
                    publish(result)

        # The KiCad libraries the component was installed in.
        if footprint_text is not None:
            # CONTRACT CHANGED 2026-11-15 (round 3): the global tables of a
            # global installation are the ones its record names
            # (`table_path`, `symbol_table_path`); «the current» global table
            # is no longer resolved for a record without them -- it may
            # belong to another KiCad version than the one the library was
            # registered in, and a BOM found there would name a file the
            # installation never wrote.
            for result in library_checks(name, footprint_text, installs, step, model_offset,
                                         symbol_path=root / f'{name}.kicad_sym'):
                publish(result)
    manual = tx('ERC del tuo schema e DRC della tua scheda in KiCad; controlla a vista posizione, '
                'scala e orientamento del modello STEP.')
    if power_pins:
        manual += ' ' + tx('I pin di alimentazione {pins} vanno pilotati nel tuo schema da '
                           'un\'uscita di alimentazione o da un PWR_FLAG.', pins=pin_ranges(power_pins))
    add('WARN', tx('Verifiche manuali'), manual, category='manual', code='manual_checks')
    return results
