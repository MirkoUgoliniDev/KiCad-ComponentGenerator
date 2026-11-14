"""@brief Background workers (QThread) of the main window and their call-contract helpers.

@details Moved out of `main_window` (HANDOFF point 130, T6/G5) without
changing behaviour: extraction, YOLO pretest, reconciliation, footprint and 3D
generation, terminal detail and the update check. `main_window` re-exports
every name defined here, so existing imports keep working.

@note Patch points. Tests and tools replace some collaborators on the
    `main_window` module (`extract_pin_header_params`, `_generate_3d`,
    `build_pin_header`, the 3D generators ...). The workers therefore resolve
    those names through `main_window` at call time (`_patch_point`) instead
    of binding them here at import: a replacement made on `main_window` still
    reaches the code that runs, exactly as before the move. The helpers that
    nobody replaces (`accepts_keyword` and its relatives, `freecad_progress`)
    live here and are imported by `main_window`.
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache, partial
import re
import json
import threading
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication

from ..cad3d.body_params import Body3DParams
from ..cad3d.kicad_models import measuring_listener, archive_progress_listener
from ..cad3d.step_generator import Cad3DCancelled, Cad3DError
from ..core.package_geometry import PackageGeometry
from ..generators.castellated_module import CastellatedModuleParams
from ..generators.pin_header import PinHeaderParams
from ..services.ai_diagnostics import CallCancelled
from ..services.datasheet_extractor import ExtractionError
from ..services.messages import tx


def _patch_point(name: str):
    """@brief Resolve a collaborator through `main_window`, where it can be replaced.

    @param name Module-level name in `kicadgen.gui.main_window`.
    @return The object currently bound to that name.
    @note Imported lazily: `main_window` imports this module at load time, so
        a module-level import here would be circular. At call time the module
        is already loaded and the lookup is a dictionary access.
    """
    from . import main_window
    return getattr(main_window, name)


def accepts_keyword(function, name: str) -> bool:
    """@brief Tell whether `name=` can be passed to this function without a TypeError.

    @param function Function about to be called.
    @param name Name of the optional parameter to pass.
    @return True when the signature declares it, or when it has a `**kwargs`
        next to at least one named parameter.
    @note Rules and reasons are those of `phase_kwargs`, of which this is the
        general part: a `**kwargs` ALONE is the footprint of a wrapper written
        without `functools.wraps`, so it proves nothing.
    @note The answer depends only on the code object, so it is memoised on the
        underlying function and on whether it is bound (`_keyword_answer`):
        the window asked it a dozen times per keystroke (2026-10-17). A bound
        method is keyed by its function, never by the instance, so the cache
        keeps no window alive; a different (e.g. monkeypatched) function is
        a different key.
    """
    target = getattr(function, "__func__", None)
    try:
        if target is not None and hasattr(function, "__self__"):
            return _keyword_answer(target, True, name)
        return _keyword_answer(function, False, name)
    except TypeError:  # unhashable callable: answer without the memo
        return _keyword_answer.__wrapped__(function, False, name)


@lru_cache(maxsize=1024)
def _keyword_answer(function, bound: bool, name: str) -> bool:
    """Answer of `accepts_keyword` for `function`; `bound` drops its first parameter (self)."""
    try:
        import inspect
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):  # builtin or object that cannot be introspected
        return False
    if bound and parameters and parameters[0].kind in (
            inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
        parameters = parameters[1:]
    named = [p for p in parameters if p.kind not in (
        inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
    for parameter in parameters:
        if parameter.kind is inspect.Parameter.VAR_KEYWORD and named:
            return True
        if parameter.name == name and parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            return True
    return False


def cancel_kwargs(function, cancel) -> dict:
    """@brief `{'cancel': event}` only when the 3D generator accepts the channel.

    @param function 3D generator about to be called.
    @param cancel Event with which the modal asks to stop the FreeCAD child.
    @return Dictionary to expand into the call, empty when the channel is not
        accepted.
    @note Same reason as `phase_kwargs`: the called function can be replaced
        (tests, tools) by one that does not know this parameter, and a
        TypeError there would fail a good generation because of an accessory
        feature.
    @note Unlike the phase announcement, giving up here is NOT lost in
        silence: the caller must say so in the log (see
        `GenerateWorker._generation_kwargs`), because a modal offering
        «Interrompi» on a job that cannot be interrupted would promise a lie.
    """
    return {"cancel": cancel} if accepts_keyword(function, "cancel") else {}


def phase_kwargs(function, callback) -> dict:
    """@brief `{'phase': callback}` only when the service function accepts it.

    @param function Service function about to be called.
    @param callback Callable(tool, detail) to forward to the modal.
    @return Dictionary to expand into the call: empty when the parameter does
        not exist yet or when the signature says nothing useful.
    @note The services and the interface evolve separately: passing `phase=`
        to a signature that does not expect it fails the analysis with a
        TypeError, and not passing it when it is there loses the phase
        announcement. The installed signature is inspected instead of betting
        on the order in which the changes land.
    @note `**kwargs` counts as consent only when the signature also declares
        at least one named parameter. A signature made only of `*args,
        **kwargs` is the footprint of a decorator written WITHOUT
        `functools.wraps`: `inspect.signature` sees the wrapper, not the real
        function, so `**kwargs` proves nothing and passing `phase=` recreates
        exactly the TypeError this helper exists to avoid. In that case the
        announcement is given up: losing one line in the modal is less serious
        than bringing the analysis down. A decorator with `functools.wraps`
        exposes `__wrapped__`, `inspect.signature` follows it and the real
        signature becomes visible again: that is the fix, not a cleverer rule
        here.
    """
    return accepted_kwargs(function, phase=callback)


def accepted_kwargs(function, **values) -> dict:
    """@brief Keep only the keyword arguments the service function accepts.

    @param function Service function about to be called.
    @param values Candidate keyword arguments.
    @return The subset of `values` whose names the signature declares; all of
        them when the signature has `**kwargs` next to at least one named
        parameter; none when the signature cannot be inspected.
    @note Same rule and same reasons as `phase_kwargs` (see its notes): the
        interface and the services change separately, and an argument the
        installed service does not know must not turn into a TypeError.
    """
    try:
        import inspect
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):  # builtin or object that cannot be introspected
        return {}
    named = [p for p in parameters if p.kind not in (
        inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
    if named and any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters):
        return dict(values)
    keyword = {p.name for p in parameters if p.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
    return {name: value for name, value in values.items() if name in keyword}


def matching_cancel_kwargs(function, cancel) -> dict:
    """@brief `{'model_cancel': event}` only when the generator declares that channel.

    @param function 3D generator about to be called.
    @param cancel Event with which the modal asks to stop the job.
    @return Dictionary to expand into the call, empty almost always.
    @note Matching the KiCad models (`kicad_models.attach_models`, which
        launches `freecadcmd` to measure a candidate not yet in the cache) can
        take minutes on its own, and its interruption is being born in
        `cad3d/`: the ordinary channel is the same `cancel` that already
        reaches the FreeCAD child (see `cancel_kwargs`), but should the
        matching receive a channel of its OWN this is the door to pass it
        through. Until the signature declares it nothing is passed: never a
        TypeError for an accessory feature, and the two changes can land in
        any order.
    """
    return {"model_cancel": cancel} if accepts_keyword(function, "model_cancel") else {}


def _photo_scale_kwargs(generator, scale) -> dict:
    """@brief `{'photo_scale_px_per_mm': scale}` only when the called generator accepts it.

    @param generator 3D function the worker is about to call (the wrapper or
        a test substitute with the former signature).
    @param scale Scale of the calibrated photo in px/mm, or None.
    @return Dictionary to expand into the call, empty when the parameter is
        not accepted.
    """
    return ({"photo_scale_px_per_mm": scale}
            if accepts_keyword(generator, "photo_scale_px_per_mm") else {})


#: Line with which the FreeCAD script announces the component it is working on.
_FREECAD_COMPONENT = re.compile(r"KICADGEN_COMPONENT_START:(\d+)/(\d+)")


def freecad_progress(message: str):
    """@brief 3D progress read from the FreeCAD log lines.

    @param message Log line, already prefixed with «FreeCAD: ».
    @return (completed, total) when the line announces a component, otherwise
        None.
    @note The count in the modal used to stop at «100% · 50/50» while FreeCAD
        was still on the first component: that number was the model matching,
        finished before FreeCAD started, and nobody updated it afterwards.
        FreeCAD however announces every component it begins: the completed
        ones are those before it. On a five-minute job the difference between
        «finished» and «at the first of fifty» is everything the person
        waiting needs to know.
    """
    match = _FREECAD_COMPONENT.search(message or "")
    if not match:
        return None
    current, total = int(match.group(1)), int(match.group(2))
    return max(0, current - 1), total


class _Cad3DRunWorker(QThread):
    """@brief Shared half of the two workers that run the 3D generator.

    GenerateWorker (footprint, then 3D) and Cad3DWorker (3D only) call the
    same generator with the same abort channel, the same modal phases and the
    same progress parsing; only the signals that carry the outcome differ.
    This base holds everything they share, so the two cannot drift apart.

    Shared signals:
        warning(str)  -- annotation not applied (e.g. impossible fillet)
        progress(str) -- informative line for the log and the modal
        cancellable(bool) -- see below
        phase(str, str, int, int) -- see below
    """

    warning = pyqtSignal(str)
    #: Informative annotations (e.g. how many real KiCad models were used):
    #: they go to the log without the warning prefix, because there is
    #: nothing that requires attention.
    progress = pyqtSignal(str)
    #: True/False at the start of the 3D: tells the window whether «Interrompi»
    #: (stop) really has a job to stop. False = the generator in use does not
    #: know the channel, and the modal must remove the command instead of
    #: promising it.
    cancellable = pyqtSignal(bool)
    #: (tool, detail, done, total). Tells the modal WHO is working, which
    #: could not be understood from the progress messages. A non-positive
    #: total means "no count": the modal stays animated instead of showing
    #: an invented percentage.
    phase = pyqtSignal(str, str, int, int)

    def __init__(self, params, project_dir: Path, body: Body3DParams | None,
                 fillets: list, face_texts: list, single_component: bool = False):
        """@brief Snapshot the inputs of one 3D run.

        @param params Component parameters (pin matrix, package or castellated module).
        @param project_dir Project folder the files are written to.
        @param body 3D body parameters, or None when no 3D model is requested.
        @param fillets Edge fillet annotations, as given by the caller.
        @param face_texts Face text annotations, as given by the caller.
        @param single_component True for a single leaded part (no FreeCAD
            component lines, the phase is announced by the worker itself).
        """
        super().__init__()
        self.params = params
        self.project_dir = project_dir
        self.body = body
        self.fillets = fillets
        self.face_texts = face_texts
        self.single_component = bool(single_component)
        #: Abort channel shared with the child FreeCAD process. It lives on
        #: the worker and not on the window: it is the run that can be stopped,
        #: and a subsequent run must be born with a clean channel.
        self.cancel = threading.Event()
        #: Scale of the calibrated photo (px/mm) or None: the window sets it
        #: after construction, see `MainWindow._photo_scale_px_per_mm`.
        self.photo_scale_px_per_mm = None

    def _cancel_kwargs(self, generator) -> dict:
        """@brief The abort channel to pass to the generator, when it accepts it.

        @param generator The 3D generation function about to be called.
        @return `{'cancel': event}` or `{}`.
        @note When the channel is not accepted the run is NOT stoppable, and
            this is said instead of being discovered by pressing a button that
            does nothing. The line goes to the log and to the modal like every
            other warning of this worker, and `cancellable(False)` REMOVES the
            command from the modal: a button that stops nothing is worse than
            no button.
        @note The question is asked to `generation_accepts_cancel`, not to the
            signature of the `_generate_3d` wrapper alone: it is the real
            generator that must know the channel.
        """
        accepts = _patch_point("generation_accepts_cancel")
        kwargs = (cancel_kwargs(generator, self.cancel)
                  if accepts(generator, self.params) else {})
        if not kwargs:
            self.progress.emit(
                tx("ATTENZIONE: il generatore 3D in uso non accetta il canale di "
                   "interruzione; questa generazione non potrà essere fermata."))
        self.cancellable.emit(bool(kwargs))
        return kwargs

    def request_abort(self) -> None:
        """@brief Ask the FreeCAD child of this run to die.

        @note It does not kill the QThread: the order travels on the channel
            and the child is killed by `process_log.run_logged`, that is by
            the same machinery as the watchdog. The worker then leaves through
            its normal error path (error signal and `finished`), which is what
            brings the window back to rest: terminating the thread by force
            would instead leave an orphan child process and the GUI without
            an end signal.
        """
        self.cancel.set()

    def _model_progress(self, done: int, total: int) -> None:
        """@brief Bring the only real count of the generation to the modal.

        @param done Components already examined.
        @param total Components to examine.
        @note Every component may cost a CAD measurement, so the count
            advances at the pace of the real work and not of a timer.
        @note The declared tool is `kicad` because the phase is the choice of
            the file to use from the KiCad model library: that is what
            advances component by component and what explains the wait. It is
            not claimed that FreeCAD is idle -- `attach_models` launches
            `freecadcmd` to measure the candidates not yet in the cache -- but
            those measurements serve the matching, not the construction of
            the solid: showing the FreeCAD solid here made people believe the
            final model was already being built.
        """
        self.phase.emit('kicad', tx("scelta del modello per ogni componente"), done, total)

    def _archive_progress(self, done, total, records):
        """@brief Show archive outcomes and persist the per-component report.

        @param done Components already compared with the shared archive.
        @param total Components to compare.
        @param records Per-component outcome rows, each with a `status` key.
        """
        counts = {key: sum(row['status'] == key for row in records)
                  for key in ('matched', 'not_found', 'review_required', 'skipped', 'unavailable')}
        detail = tx("{matched} trovati · {not_found} non trovati · {review} da verificare",
                    matched=counts['matched'], not_found=counts['not_found'],
                    review=counts['review_required'])
        if counts['skipped']:
            detail += tx(" · {count} esclusi", count=counts['skipped'])
        if counts['unavailable']:
            detail += tx(" · {count} non disponibili", count=counts['unavailable'])
        self.phase.emit('consolidated', detail, done, total)
        if done == total:
            self.progress.emit(tx("Confronto consolidati completato: {summary}", summary=detail))
            path = Path(self.project_dir) / 'consolidated-matches.json'
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.json.tmp')
                temporary.write_text(json.dumps({'complete': True, 'total': total,
                                     'counts': counts, 'components': records}, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary.replace(path)
                self.progress.emit(tx("Resoconto consolidati: {path}", path=str(path)))
            except OSError as exc:
                self.progress.emit(tx("Resoconto consolidati non salvato: {error}", error=str(exc)))

    def _cad_progress(self, message: str) -> None:
        """@brief Forward the line to the log and, when it announces a component, to the count.

        @param message Progress line from the 3D generator.
        """
        from ..cad3d.header_reuse import REUSE_PROGRESS
        if message == REUSE_PROGRESS:
            self.phase.emit('headers', tx("Modelli già scelti riutilizzati: aggiornamento degli header."), 0, 0)
            return
        self.progress.emit(message)
        step = freecad_progress(message)
        if step is not None:
            done, total = step
            self.phase.emit('freecad', tx("costruzione del solido 3D"), done, total)

    def _generate_model(self) -> dict:
        """@brief Run the 3D generator once, with progress, abort channel and photo scale.

        @return The generator's result mapping (`step_path`, `warnings`, `notes`).
        @throws Cad3DError and any other exception of the generator; the
            caller reports them through its own error signal.
        @note The generator is resolved ONCE, from `main_window` (see
            `_patch_point`): introspection and call must concern the same
            object, even when it has been replaced.
        @note The first thing `_generate_3d` does is the matching of the KiCad
            models, not the construction of the solid: the caller announces
            `kicad`, and `_cad_progress` switches to `freecad` when the solid
            really begins.
        """
        if self.single_component and not isinstance(self.params, PackageGeometry):
            # No component lines come from FreeCAD for a single part, so the
            # phase is announced here (review B: the modal stayed on step 0).
            self.phase.emit('freecad', tx("costruzione del solido 3D"), 0, 0)
            generator = _patch_point("generate_single_tht_3d_model")
            return generator(
                self.params, self.body, self.project_dir,
                fillets=self.fillets, face_texts=self.face_texts,
                progress=self._cad_progress,
                **self._cancel_kwargs(generator),
                **_photo_scale_kwargs(generator, self.photo_scale_px_per_mm),
            )
        generator = _patch_point("_generate_3d")
        with measuring_listener(partial(_announce_freecad_measuring, self)), archive_progress_listener(self._archive_progress):
            return generator(
                self.params, self.body, self.project_dir,
                self.fillets, self.face_texts,
                progress=self._cad_progress,
                model_progress=self._model_progress,
                **self._cancel_kwargs(generator),
                **_photo_scale_kwargs(generator, self.photo_scale_px_per_mm),
            )


class GenerateWorker(_Cad3DRunWorker):
    """@brief Worker thread that builds and saves the footprint and, when requested, the 3D model.

    QThread that builds/saves a Footprint and, when requested, the 3D model.

    The 2D footprint and the 3D model are reported separately: a failure of
    the 3D generation (e.g. freecadcmd not found) must not invalidate a 2D
    footprint generated correctly.

    Signals (besides those of `_Cad3DRunWorker`):
        done(str)       -- absolute path of the written .kicad_mod file
        error(str)      -- error in the 2D footprint generation
        cad3d_done(str) -- absolute path of the written .step file
        cad3d_error(str) -- error in the 3D model generation
    """

    done = pyqtSignal(str)
    error = pyqtSignal(str)
    cad3d_done = pyqtSignal(str)
    cad3d_error = pyqtSignal(str)

    def __init__(
        self, params: PinHeaderParams | CastellatedModuleParams | PackageGeometry, project_dir: Path,
        body: Body3DParams | None = None,
        fillets: list | None = None, face_texts: list | None = None,
        single_component: bool = False,
    ):
        """@brief Snapshot one footprint (and optional 3D) generation.

        @param params Component parameters.
        @param project_dir Project folder the files are written to.
        @param body 3D body parameters; None generates the footprint only.
        @param fillets Edge fillet annotations; None means none.
        @param face_texts Face text annotations; None means none.
        @param single_component True for a single leaded part (see `_Cad3DRunWorker`).
        """
        super().__init__(params, project_dir, body, fillets or [], face_texts or [], single_component)
        # Only the host footprint may differ: self.params remains the physical
        # module passed to FreeCAD, matching and all 3D regenerations.
        self.footprint_params = None
        #: Datasheet URL or path for the footprint's Datasheet property. The
        #: pin-header and castellated params carry their own; an explicit
        #: PackageGeometry has no such field, so the window sets it here.
        self.datasheet = ""
        #: Project description for the footprint's `descr` and "Description";
        #: "" keeps the generator's generic text (same reason as `datasheet`).
        self.description = ""
        #: Project keywords for the footprint's `tags` on the PackageGeometry
        #: path (explicit packages and modules mounted with headers); ""
        #: keeps the package family. The pin-header and castellated params
        #: carry their own `tags` (review 2026-10-15, R2-M2).
        self.tags = ""
        #: KiCad attribute flags of the footprint (`Footprint.attributes`,
        #: e.g. "exclude_from_bom"): set on the built footprint whatever the
        #: generator, as the flags belong to the component, not to its
        #: geometry. () writes none.
        self.attributes = ()
        #: Chosen positions of the Reference and Value texts, {key: (x, y)}
        #: mm (user, 2026-10-13); {} keeps the generator's defaults.
        self.text_positions = {}
        #: User drawings of the footprint preview (validated records, see
        #: core.footprint.validate_footprint_drawings); [] adds nothing.
        self.drawings = []
        #: Footprint anchor (x, y) mm in the generator's coordinates (plan
        #: F2): the written file puts this point at (0, 0)
        #: (`Footprint.shift_origin`); (0, 0) writes exactly as before.
        self.anchor = (0.0, 0.0)

    def run(self) -> None:
        """@brief QThread override: save the footprint, then generate the 3D model, reporting each through its own signals."""
        try:
            # This program writes the file: KiCad is not running.
            self.phase.emit('footprint', tx("scrittura del file .kicad_mod"), 0, 0)
            self.progress.emit(tx("Generazione del footprint KiCad…"))
            host = self.footprint_params if self.footprint_params is not None else self.params
            footprint = (
                _patch_point("build_castellated_module")(host)
                if isinstance(host, CastellatedModuleParams)
                else _patch_point("build_package")(host, datasheet=self.datasheet,
                                                   description=self.description,
                                                   tags=self.tags)
                if isinstance(host, PackageGeometry)
                else _patch_point("build_pin_header")(host)
            )
            footprint.attributes = tuple(self.attributes or ())
            footprint.place_texts(self.text_positions)
            # User drawings (preview palette): records already validated,
            # added after the generated ones so they do not touch the
            # computed geometry (pads, courtyard, outlines).
            from ..core.footprint import drawing_graphics
            for item in drawing_graphics(self.drawings):
                footprint.add_graphic(item)
            # Once, on the footprint built for this file only (nothing else
            # holds it): every pad, graphic, zone and text moves by -anchor.
            footprint.shift_origin(*self.anchor)
            pretty_dir = self.project_dir / f"{self.params.name}.pretty"
            out_path = pretty_dir / f"{self.params.name}.kicad_mod"
            footprint.save(str(out_path))
            if self.body is None and isinstance(self.params, CastellatedModuleParams):
                # Footprint only: the notes of the reduced estimates (holes,
                # pads) would otherwise reach the log only through the 3D step
                # (point 140, «riduci e segnala» (reduce and report): a
                # reduction never stays silent).
                for note in self.params.geometry_notes():
                    self.progress.emit(note)
            self.done.emit(str(out_path))
        except Exception as exc:  # noqa: BLE001 - reported in the UI, not fatal
            self.error.emit(str(exc))
            return

        if self.body is not None:
            try:
                # The first thing `_generate_3d` does is matching the KiCad
                # models, not building the solid: announcing `freecad` here
                # lit the FreeCAD cube for the whole phase that the correction
                # wants attributed to KiCad, only to go back afterwards.
                # `_cad_progress` switches to `freecad` when the solid really
                # starts.
                if not self.single_component and not isinstance(self.params, PackageGeometry):
                    self.phase.emit('consolidated', tx("confronto con l'archivio condiviso"), 0, 0)
                self.progress.emit(tx("Generazione del modello STEP tramite FreeCAD…"))
                result = self._generate_model()
                for warning in result.get('warnings', []):
                    self.warning.emit(warning)
                # The notes of the model resolution go to the log also in the
                # full generation, not only in the 3D regeneration.
                for note in result.get('notes', []):
                    self.progress.emit(note)
                self.cad3d_done.emit(str(result["step_path"]))
            except Cad3DError as exc:
                # The window tells a user's interruption from a fault by this
                # flag, never by the (translated) text (second review 2026-11-03, C6).
                self.cad3d_cancelled = isinstance(exc, Cad3DCancelled)
                self.cad3d_error.emit(str(exc))
            except Exception as exc:  # noqa: BLE001 - reported in the UI, not fatal
                self.cad3d_error.emit(tx("Errore inatteso: {exc}", exc=exc))


def _announce_freecad_measuring(worker, started: bool, count: int) -> None:
    """@brief Switch the modal to FreeCAD while it measures candidate models, then back.

    @param worker CAD worker whose `phase` signal reaches the modal.
    @param started True when a `freecadcmd` child is about to measure.
    @param count Models in that batch.
    @note Called by `kicad_models.measuring_listener` only when FreeCAD is
        really launched: models already in the measurement cache launch
        nothing, and the modal then keeps showing the KiCad library.
    """
    if started:
        worker.phase.emit('freecad_measure',
                          tx("misura di {count} modelli candidati", count=count), 0, 0)
    else:
        worker.phase.emit('kicad', tx("scelta del modello per ogni componente"), 0, 0)


class Cad3DWorker(_Cad3DRunWorker):
    """@brief Worker thread that regenerates only the 3D model (STEP/VRML), not the footprint.

    QThread that regenerates only the 3D model (STEP/VRML), without redoing the footprint.

    It serves the "Rigenera modello 3D" button of the Modello 3D tab: after
    adding/removing annotations there is no point in rewriting the .kicad_mod
    too.

    Signals (besides those of `_Cad3DRunWorker`):
        done(str)    -- absolute path of the written .step file
        error(str)   -- readable error message
    """

    done = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(
        self, params: PinHeaderParams | CastellatedModuleParams | PackageGeometry, body: Body3DParams,
        project_dir: Path, fillets: list, face_texts: list,
        single_component: bool = False,
    ):
        """@brief Snapshot one 3D regeneration.

        @param params Component parameters.
        @param body 3D body parameters.
        @param project_dir Project folder the model is written to.
        @param fillets Edge fillet annotations, kept as given.
        @param face_texts Face text annotations, kept as given.
        @param single_component True for a single leaded part (see `_Cad3DRunWorker`).
        """
        super().__init__(params, project_dir, body, fillets, face_texts, single_component)

    def run(self) -> None:
        """@brief QThread override: regenerate the 3D model and emit done, warning or error."""
        try:
            # Same reason as GenerateWorker.run: `_generate_3d` starts from
            # matching the KiCad models. `_cad_progress` switches to
            # `freecad` when the solid really starts.
            if not self.single_component and not isinstance(self.params, PackageGeometry):
                self.phase.emit('consolidated', tx("confronto con l'archivio condiviso"), 0, 0)
            result = self._generate_model()
            for note in result.get("notes", []):
                self.progress.emit(note)
            for warning in result.get("warnings", []):
                self.warning.emit(warning)
            self.done.emit(str(result["step_path"]))
        except Cad3DError as exc:
            # See GenerateWorker: the interruption is a flag, not a text.
            self.cad3d_cancelled = isinstance(exc, Cad3DCancelled)
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - reported in the UI, not fatal
            self.error.emit(tx("Errore inatteso: {exc}", exc=exc))


class UpdateCheckWorker(QThread):
    """@brief Ask GitHub for the latest release off the GUI thread.

    @note The thread is owned by QApplication, not by the window that starts
        it, the same pattern as `model_dialog._ModelsWorker`: closing the
        window during the check (up to the 5 s HTTP timeout) must not destroy
        a running QThread, which would abort the whole process. Application
        exit waits for it through `aboutToQuit`, bounded by that timeout, and
        the thread deletes itself once finished.
    """
    done = pyqtSignal(object)

    def __init__(self, parent=None):
        """@brief Prepare one update check owned by QApplication.

        @param parent Owner of the thread; defaults to QApplication.instance()
            so that no window can destroy the check while it is running.
        """
        application = QApplication.instance()
        super().__init__(parent if parent is not None else application)
        if application is not None:
            application.aboutToQuit.connect(self.wait)
        self.finished.connect(self._release)

    def _release(self) -> None:
        """Drop the exit-time wait and delete the finished thread.

        The `wait` promise is valid only while the object exists: left
        connected, Qt would call it on a thread already removed by
        `deleteLater` at exit.
        """
        application = QApplication.instance()
        if application is not None:
            try:
                application.aboutToQuit.disconnect(self.wait)
            except TypeError:
                pass  # never connected, or already released
        self.deleteLater()

    def run(self):
        """@brief QThread override: always emit one `UpdateCheckResult`.

        @note The check itself never raises; the guard also covers a failed
            import, so an unexpected error becomes a "network" result instead
            of reaching sys.excepthook (which ends the process).
        """
        try:
            from ..services import update_check
            result = update_check.check_latest_release()
        except Exception as exc:  # noqa: BLE001 - the signal must always fire
            from ..services.update_check import ERROR_NETWORK, UpdateCheckResult
            from .. import __version__
            result = UpdateCheckResult(str(__version__), error_kind=ERROR_NETWORK,
                                       error=f"{type(exc).__name__}: {exc}")
        self.done.emit(result)


class PreviewRenderWorker(QThread):
    """@brief Render the «Prova in KiCad» preview board to a 3D image off the GUI thread.

    @note Same ownership as `UpdateCheckWorker`: the thread belongs to
        QApplication, so closing the window during a render (bounded by
        `kicad_library.PREVIEW_RENDER_TIMEOUT_S`) never destroys a running
        QThread; application exit waits for it, and the finished thread
        deletes itself.
    @note `render_preview_image` is looked up on `kicad_library` at run time,
        so a test that replaces it there reaches the code that runs.
    @note `done` carries (result, token): the receiver is a bound method of
        the window, so the queued connection delivers it on the GUI thread,
        and the token says which start the result belongs to.
    """
    done = pyqtSignal(object, object)

    def __init__(self, preview, kicad_cli, *, has_step: bool, token=None, parent=None):
        """@brief Prepare one render.

        @param preview `kicad_library.PreviewProject` to render.
        @param kicad_cli Path of kicad-cli, or None.
        @param has_step Whether the preview carries a STEP model.
        @param token Opaque value emitted back with the result.
        @param parent Owner of the thread; defaults to QApplication.instance().
        """
        application = QApplication.instance()
        super().__init__(parent if parent is not None else application)
        self.preview = preview
        self.kicad_cli = kicad_cli
        self.has_step = bool(has_step)
        self.token = token
        if application is not None:
            application.aboutToQuit.connect(self.wait)
        self.finished.connect(self._release)

    def _release(self) -> None:
        """Drop the exit-time wait and delete the finished thread (see `UpdateCheckWorker._release`)."""
        application = QApplication.instance()
        if application is not None:
            try:
                application.aboutToQuit.disconnect(self.wait)
            except TypeError:
                pass  # never connected, or already released
        self.deleteLater()

    def run(self):
        """@brief QThread override: always emit one `kicad_library.PreviewRender`.

        @note `render_preview_image` never raises for kicad-cli problems; the
            guard turns any other error into a ``render_failed`` result, so
            nothing reaches sys.excepthook from this thread.
        """
        from ..services import kicad_library
        try:
            result = kicad_library.render_preview_image(
                self.preview, self.kicad_cli, has_step=self.has_step)
        except Exception as exc:  # noqa: BLE001 - the signal must always fire
            result = kicad_library.PreviewRender(None, "render_failed",
                                                 f"{type(exc).__name__}: {exc}")
        self.done.emit(result, self.token)


class TerminalDetailWorker(QThread):
    """@brief Optional photo detail pass; never regenerates the footprint."""
    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, photo, components, model, only_leads=False, system_prompt=None,
                 cache_dir=None, step_models=None):
        """@brief Prepare one terminal (or lead) pass on a copy of @p components.

        @param photo PNG bytes of the reference photo.
        @param components Photo components; deep-copied, never modified.
        @param model AI model identifier: the single (or session) model, used
            when the step has no model of its own.
        @param only_leads True to refine the leads of observed terminals only.
        @param system_prompt Optional profile prompt for the pass.
        @param cache_dir Project photo cache, or None for no cache.
        @param step_models The model of every analysis step
            (ProfileStore.step_models), copied; forwarded as `step_models=`
            when the service accepts it, which resolves the terminal or lead
            step's model. None = every call on `model`.
        @note Costs are charged by the window, which binds the run project
            before `start()` (`MainWindow._start_terminal_refinement`).
        """
        super().__init__()
        self.photo, self.components, self.model = photo, deepcopy(components), model
        self.only_leads = only_leads
        self.system_prompt = system_prompt
        #: Step models of this pass (a copy); None = no step choice.
        self.step_models = dict(step_models) if step_models is not None else None
        # PROJECT cache (see photo_inventory.PHOTO_CACHE_SUBDIR); None = no
        # cache, never a shared folder.
        self.cache_dir = cache_dir

    def run(self):
        """@brief QThread override: observe terminals (or refine leads only) and emit the updated components."""
        try:
            # Imported inside the guard: a failed import must reach the error
            # signal, not sys.excepthook (which ends the process).
            from ..services.photo_terminals import observe_terminals
            from ..services.photo_leads import refine_leads
            from ..services.openrouter import call_tool
            from ..services.ai_diagnostics import make_logged_call
            operation = refine_leads if self.only_leads else observe_terminals
            self.done.emit(operation(self.photo, self.components, self.model,
                make_logged_call(call_tool, self.progress.emit, log_prompt=True), self.progress.emit,
                self.cache_dir,
                **({'system_prompt': self.system_prompt} if self.system_prompt else {}),
                **(accepted_kwargs(operation, step_models=self.step_models)
                   if getattr(self, "step_models", None) is not None else {})))
        except Exception as exc:
            self.error.emit(tx("Dettaglio terminali fallito: {exc}", exc=exc))


class ExtractWorker(QThread):
    """@brief Extract datasheet parameters with session-selected photo detection.

    `model` is the OpenRouter id to use for this extraction: it can be the
    default of the SQLite profile or a different model chosen only for the
    current session.

    Signals:
        done(ExtractedParams) -- parameters extracted successfully
        error(str)            -- readable error message
        progress(str)        -- progress of the AI analysis, every crop included
    """

    done = pyqtSignal(object)
    error = pyqtSignal(str)
    #: Progress rows. `object`, not `str`: a row may be a messages.Message
    #: with its type (warning), and a `str` signal would reduce it to plain
    #: text, losing it (point 136).
    progress = pyqtSignal(object)
    #: See GenerateWorker.phase.
    phase = pyqtSignal(str, str, int, int)
    #: (PNG photo, detector proposals, length mm, width mm). Emitted between
    #: the local detection and the paid reconciliation: the worker stops here
    #: until the interface answers with deliver_review, because a proposal
    #: removed now is neither sent nor paid for. The dimensions are those the
    #: photo was calibrated with, not those of the fields: during extraction
    #: the fields have not been filled in yet.
    review_requested = pyqtSignal(object, object, float, float)

    def __init__(
        self, doc_paths: list[Path], system_prompt: str | None = None,
        extra_instructions: str | None = None, include_nc_pins: bool = True,
        model: str | None = None, vision_model: str | None = None,
        grounding_prompt: str | None = None, verification_prompt: str | None = None,
        verification_model: str | None = None, terminal_prompt: str | None = None,
        document_roles: dict | None = None,
        component_family: str | None = None,
        *, photo_detector: str = "ai", yolo_model_path: Path | None = None,
        yolo_confidence: float | None = None, yolo_settings: dict | None = None,
        crop_prompt: str | None = None,
        crop_model: str | None = None,
        smd_model_path: Path | None = None,
        numbering_prompt: str | None = None,
        pad_model: str | None = None,
        part_number: str | None = None,
        prompts: dict | None = None,
        step_models: dict | None = None,
        answers: dict | None = None,
    ):
        """@brief Snapshot extraction settings before starting background work.

        @param doc_paths Documents selected for extraction.
        @param system_prompt Optional datasheet system prompt.
        @param extra_instructions Optional component instructions.
        @param include_nc_pins Whether to include physically present NC pins.
        @param model Datasheet AI model identifier.
        @param vision_model Photo AI model identifier.
        @param grounding_prompt Optional photo localization prompt.
        @param verification_prompt Optional photo verification prompt.
        @param verification_model Configured AI verifier model identifier.
        @param terminal_prompt Optional terminal observation prompt.
        @param document_roles Selected document roles, copied for this request.
        @param component_family Selected component geometry family.
        @param photo_detector Photo proposal backend, ai by default or yolo.
        @param yolo_model_path Optional session ONNX path; None uses the service default.
        @param yolo_confidence YOLO operating threshold; None uses the service default.
        @param yolo_settings Other YOLO detector settings; None uses the defaults.
        @param crop_prompt Optional per-body crop reading prompt (profile field
            'crop_prompt'); None uses the service's factory default. Forwarded
            only when the extraction function accepts it.
        @param crop_model Optional model of the per-body crop reading (profile
            field 'crop_model', HANDOFF 150); None or blank reads the crops
            with the model of the paid call on the photograph
            (`verification_model`). Forwarded like `crop_prompt`.
        @param smd_model_path Weights file of the local SMD network chosen in
            the «Modello rete SMD» row; None uses the default in models/smd.
        @param numbering_prompt Prompt of the dedicated pin-numbering call;
            None uses the factory default.
        @param pad_model Model of the dedicated pad-position reading of a
            single part (profile field 'pad_model'); None or blank uses
            `model`. Forwarded like `crop_prompt`.
        @param part_number Part number of the component (GUI field «Sigla»):
            the KiCad library footprints it names confirm the pad geometry of
            a single part; None or blank searches nothing. Forwarded like
            `crop_prompt`.
        @param prompts The profile's prompt texts, {field: text}
            (prompt_catalog.profile_prompts of ProfileStore.resolve), copied;
            forwarded as `prompts=` like `crop_prompt`. None sends the defaults.
        @param step_models The model of every analysis step, {prompt field:
            model or "" for the single model} (ProfileStore.step_models,
            «Modello AI» table), copied; forwarded as `step_models=` like
            `crop_prompt`. None = every step on its usual model. The session
            model replaces only `model`, never a step's own choice.
        @param answers The project's saved answers to structured questions
            ({question key: value}), copied when the analysis starts, so a
            later project switch cannot change them; forwarded as `answers=`
            when the extraction function accepts it. None or empty forwards
            nothing.
        @note YOLO changes photo proposals only; AI verification remains configured.
        @note Root cause of saved answers asked again (overnight review
            2026-11-14): the window set `answers` on the worker but `run`
            never forwarded them, so the extractor's tolerant re-application
            (shape signature, row spacing by value) never ran.
        """
        super().__init__()
        #: The project's saved answers of this run (a copy); None = none.
        self.answers = dict(answers) if isinstance(answers, dict) and answers else None
        #: Step models of this run (a copy); None = no step choice.
        self.step_models = dict(step_models) if step_models is not None else None
        self.doc_paths = doc_paths
        self.system_prompt = system_prompt
        #: Prompt of the dedicated pin-numbering call; None = factory default.
        self.numbering_prompt = numbering_prompt
        #: Model of the dedicated pad-position reading; None = `model`.
        self.pad_model = pad_model
        #: Part number of the component (field «Sigla»); None = no library search.
        self.part_number = part_number
        #: Profile prompt texts of this run (a copy); None = the defaults.
        self.prompts = dict(prompts) if prompts is not None else None
        self.extra_instructions = extra_instructions
        self.include_nc_pins = include_nc_pins
        self.model = model
        self.vision_model = vision_model
        self.grounding_prompt = grounding_prompt
        self.verification_prompt = verification_prompt
        self.verification_model = verification_model
        self.terminal_prompt = terminal_prompt
        # Copy of the lists too: since 26/09 every file carries a list of roles.
        self.document_roles = ({name: (list(roles) if isinstance(roles, (list, tuple)) else roles)
                                for name, roles in document_roles.items()}
                               if document_roles is not None else None)
        self.component_family = component_family
        self.photo_detector = photo_detector
        self.yolo_model_path = yolo_model_path
        # SMD network file chosen in the «Modello rete SMD» (SMD network model)
        # row (None: the default in models/smd).
        self.smd_model_path = smd_model_path
        self.yolo_confidence = yolo_confidence
        self.yolo_settings = dict(yolo_settings) if yolo_settings is not None else None
        self.crop_prompt = crop_prompt
        self.crop_model = crop_model
        # «Interrompi» (stop) raises it: the service checks it before every
        # paid call, so that after the stop no other one starts (check of
        # 25/09, D8). The one already in flight cannot be recalled.
        self.cancel_event = threading.Event()
        self._review_answered = threading.Event()
        self._review_result = None

    #: Seconds between two checks of `cancel_event` while the worker waits
    #: for the photo review to be answered (see `_review_proposals`).
    REVIEW_WAIT_SLICE_S = 0.5

    def _review_proposals(self, photo_png, proposals, length_mm, width_mm):
        """@brief Ask the interface which bodies to keep, and wait.

        @param photo_png Calibrated photograph, already encoded as PNG.
        @param proposals Proposals of the local detector.
        @param length_mm Board extent along X, in mm.
        @param width_mm Board extent along Y, in mm.
        @return The proposals to keep, or None when the review is cancelled
            or the extraction is stopped while the review is open.
        @note Runs on the worker thread: the signal crosses the threads and
            the answer comes back through deliver_review. The wait is
            deliberate -- the paid call must not start before someone has
            looked.
        @note Review 2026-10-27, C6: the wait is sliced so that `cancel_event`
            («Interrompi» (stop), or the window closing) ends it even when no
            answer ever arrives; an unbounded wait would leave the thread
            blocked for ever, and the application exit waiting for it. The
            slice only bounds the reaction time to the stop; an answer is
            seen as soon as it is delivered.
        """
        self._review_result = None
        self._review_answered.clear()
        self.review_requested.emit(photo_png or b"", [dict(p) for p in proposals],
                                   float(length_mm), float(width_mm))
        while not self._review_answered.wait(self.REVIEW_WAIT_SLICE_S):
            if self.cancel_event.is_set():
                return None
        return self._review_result

    def deliver_review(self, kept) -> None:
        """@brief Deliver the outcome of the review to the worker and unblock it.

        @param kept Proposals to keep, or None to cancel the extraction.
        @note It must ALWAYS be called, even when something goes wrong: a
            worker left waiting never finishes.
        """
        self._review_result = kept
        self._review_answered.set()

    def _forward_phase(self, tool: str, detail: str = "", done: int = 0, total: int = 0) -> None:
        """@brief Bring to the modal the tool the service declares at work.

        @param tool Key in analysis_overlay.TOOLS, e.g. yolo or llm.
        @param detail Free text: the detector's label or the model.
        @param done Items done in the phase (HANDOFF 156: bodies read).
        @param total Items expected; 0 = no count.
        @note Service contract: `phase(tool, detail, done=0, total=0)`.
            Without a count (0, 0) the modal stays animated instead of showing
            an invented percentage. It serves above all the local ONNX
            detector, which used to run without anything announcing it: the
            viewer saw the cloud of the paid model even while nothing was
            being spent.
        """
        self.phase.emit(str(tool or ""), str(detail or ""), int(done or 0), int(total or 0))

    def run(self) -> None:
        """@brief Forward the captured settings and emit extracted data or an error."""
        try:
            # Resolved only once by `main_window` (see `_patch_point`): the
            # queried signature and the called function are the same object.
            extract = _patch_point("extract_pin_header_params")
            params = extract(
                self.doc_paths,
                model=self.model,
                vision_model=self.vision_model,
                grounding_prompt=self.grounding_prompt, verification_prompt=self.verification_prompt,
                verification_model=self.verification_model, terminal_prompt=self.terminal_prompt,
                document_roles=self.document_roles,
                component_family=self.component_family,
                photo_detector=self.photo_detector,
                yolo_model_path=self.yolo_model_path,
                **accepted_kwargs(extract, smd_model_path=self.smd_model_path),
                system_prompt=self.system_prompt,
                extra_instructions=self.extra_instructions,
                include_nc_pins=self.include_nc_pins,
                progress=self.progress.emit,
                review_proposals=self._review_proposals,
                # PROJECT folder assigned by the caller; None = no cache.
                **accepted_kwargs(extract,
                                  photo_cache_dir=getattr(self, "photo_cache_dir", None),
                                  yolo_confidence=self.yolo_confidence,
                                  yolo_settings=self.yolo_settings,
                                  crop_prompt=self.crop_prompt,
                                  crop_model=self.crop_model,
                                  numbering_prompt=self.numbering_prompt,
                                  pad_model=self.pad_model,
                                  part_number=self.part_number,
                                  cancel=self.cancel_event,
                                  # The saved answers: the extractor applies the
                                  # ones it can match itself (none = no keyword).
                                  **({"answers": dict(self.answers)}
                                     if getattr(self, "answers", None) else {}),
                                  # None = the defaults: not forwarded at all.
                                  **({"prompts": self.prompts}
                                     if getattr(self, "prompts", None) is not None else {}),
                                  **({"step_models": self.step_models}
                                     if getattr(self, "step_models", None) is not None else {})),
                **phase_kwargs(extract, self._forward_phase),
            )
            self.done.emit(params)
        except ExtractionError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - network/API errors are reported in the UI
            self.error.emit(tx("Errore API: {exc}", exc=exc))


class YoloPretestWorker(ExtractWorker):
    """@brief Local YOLO detection on the top view only: the «Solo YOLO locale» pretest.

    Reuses ExtractWorker's review hand-off (review_requested /
    deliver_review) and its signals; nothing else of the extraction runs.
    """

    def __init__(self, doc_paths, document_roles, length_mm, width_mm,
                 yolo_model_path, cache_dir, yolo_confidence=None, yolo_settings=None):
        """@brief Snapshot the pretest inputs.

        @param doc_paths Selected documents.
        @param document_roles Document roles by file name, or None.
        @param length_mm Board X extent used to calibrate the photo, in mm.
        @param width_mm Board Y extent, in mm.
        @param yolo_model_path Weights path, or None for the default model.
        @param cache_dir Folder for the detector evidence: the window passes
            the pretest's own `yolo-pretest/photo-cache`, never the
            extraction's, so this evidence is never read as an extraction's.
        @param yolo_confidence YOLO operating threshold; None uses the default.
        @param yolo_settings Other YOLO detector settings; None uses the defaults.
        """
        super().__init__(list(doc_paths), document_roles=document_roles,
                         photo_detector="yolo_only", yolo_model_path=yolo_model_path,
                         yolo_confidence=yolo_confidence, yolo_settings=yolo_settings)
        self.length_mm, self.width_mm = float(length_mm), float(width_mm)
        self.photo_cache_dir = cache_dir

    def _top_views(self):
        """Indexes of the documents declared as a photograph, or [-1] when none is.

        Only a file ticked «Foto» (`document_roles.PHOTO_ROLE`) is a photograph
        of the board: a dimensioned drawing is never searched for bodies.
        """
        if self.document_roles is None:
            return None
        # Same choice as the extractor when its helper exists: the pretest
        # must try exactly the photos the analysis would try.
        from ..services import datasheet_extractor
        indices = getattr(datasheet_extractor, "photo_document_indices", None)
        if callable(indices):
            return indices(self.doc_paths, self.document_roles)
        from ..services.document_roles import PHOTO_ROLE, role_list
        return [i for i, path in enumerate(self.doc_paths)
                if PHOTO_ROLE in role_list(self.document_roles.get(path.name))] or [-1]

    def run(self) -> None:
        """@brief Detect, let the user review, emit the report (None if cancelled).

        @note Nothing escapes: the inner `no_ai` stub raises RuntimeError if
            the yolo_only branch ever tried to call an AI model (that would be
            a defect, and it must stop instead of spending), and that error is
            caught like any other worker error and reported through `error`.
        """
        try:
            # Imported inside a guard: a failed import must reach the error
            # signal, not sys.excepthook (which ends the process). Kept apart
            # from the main guard because its `except ReviewCancelled` needs
            # the name bound.
            from ..services.datasheet_extractor import COMPONENT_KINDS
            from ..services.photo_inventory import ReviewCancelled, extract_inventory
        except Exception as exc:  # noqa: BLE001 - reported in the UI like every worker error
            self.error.emit(str(exc))
            return

        def no_ai(**_kwargs):
            # The yolo_only branch never calls the model: if it happened it
            # would be a defect, and it must stop instead of spending.
            raise RuntimeError("the YOLO pretest must not call any AI model")

        try:
            report = extract_inventory(
                self.doc_paths, "", self.length_mm, self.width_mm, COMPONENT_KINDS, no_ai,
                progress=self.progress.emit, phase=self._forward_phase,
                top_photo_index=self._top_views(), top_photo_source="utente",
                cache_dir=self.photo_cache_dir, detector="yolo_only",
                yolo_model_path=self.yolo_model_path,
                yolo_confidence=self.yolo_confidence,
                yolo_settings=self.yolo_settings,
                review_proposals=self._review_proposals)
            self.done.emit(report)
        except ReviewCancelled:
            self.done.emit(None)
        except Exception as exc:  # noqa: BLE001 - reported in the UI like every worker error
            self.error.emit(str(exc))


class ReconcileWorker(QThread):
    """@brief Re-run only the paid reconciliation on proposals already reviewed.

    It is the tail of ExtractWorker without the head: no documents, no local
    detector, no contact filter nor edge measurement. It receives the
    proposals kept in the «Corpi rilevati» window, reopened on the last save,
    and makes ONE request to the model that looks at the photograph, then
    re-reads every body from its crop, samples the colours, re-reads the
    markings and saves the evidence as the normal extraction would.

    Signals:
        done(InventoryResult) -- reconciled inventory
        error(str)            -- readable error message
        progress(str)         -- progress, the line of the call included
        phase(str, str, int, int) -- see GenerateWorker.phase
    """

    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(object)   # see ExtractWorker.progress
    phase = pyqtSignal(str, str, int, int)

    def __init__(self, photo_png, proposals, length_mm, width_mm, model, cache_dir,
                 *, declared=None, verification_prompt=None, extra_instructions=None,
                 detector='yolo', crop_prompt=None, crop_model=None, reviewed=True,
                 smd_model_path=None, prompts=None, step_models=None):
        """@brief Snapshot the approved proposals before leaving the GUI thread.

        @param photo_png Calibrated photograph on which the proposals were measured.
        @param proposals Proposals kept by the review, forwarded as they are.
        @param length_mm Board extent along X, in mm (calibration).
        @param width_mm Board extent along Y, in mm.
        @param model Paid model of the reconciliation.
        @param cache_dir Folder of the answer cache and of the evidence.
        @param declared Declared board dimensions, only for the consistency note.
        @param verification_prompt Profile notes, as in the normal path.
        @param extra_instructions User notes, as in the normal path.
        @param detector Name of the detector to record in the calibration.
        @param crop_prompt Prompt of the per-crop reading (profile field
            'crop_prompt'), as for ExtractWorker: the re-run performs the same
            reading of the single bodies (HANDOFF 146 V2). None or empty:
            factory text.
        @param crop_model Model of the per-crop reading (profile field
            'crop_model', HANDOFF 150); None or empty: the same `model` as the
            request on the photograph.
        @param reviewed Whether the proposals come from a review made by a
            person (ReviewBundle.reviewed: False for the fallback bundle from
            the cache).
        @param smd_model_path Weights file of the local SMD network, or None
            for the default.
        @param prompts The profile's prompt texts ({field: text}), copied, as
            for ExtractWorker: the inventory and marking prompts reach
            reconcile_saved_proposals, and every request is reported in the
            log (prompt_catalog.SentPromptLog). None sends the defaults.
        @param step_models The model of every analysis step, copied, as for
            ExtractWorker: forwarded as `step_models=` when
            reconcile_saved_proposals accepts it. None = no step choice.
        """
        super().__init__()
        self.prompts = dict(prompts) if prompts is not None else None
        #: Step models of this re-run (a copy); None = no step choice.
        self.step_models = dict(step_models) if step_models is not None else None
        self.photo_png = photo_png
        self.proposals = deepcopy(proposals)
        self.length_mm, self.width_mm = float(length_mm), float(width_mm)
        self.model = model
        self.cache_dir = cache_dir
        self.declared = deepcopy(declared) if declared else None
        self.verification_prompt = verification_prompt
        self.extra_instructions = extra_instructions
        self.detector = detector
        # Do the proposals come from a review made by a person?
        # (ReviewBundle.reviewed: False for the fallback bundle from the cache).
        self.reviewed = bool(reviewed)
        self.smd_model_path = smd_model_path
        self.crop_prompt = crop_prompt
        self.crop_model = crop_model
        # Like ExtractWorker.cancel_event: raised by «Interrompi» (stop).
        self.cancel_event = threading.Event()

    def _forward_phase(self, tool: str, detail: str = "", done: int = 0, total: int = 0) -> None:
        """@brief Forward to the modal the phase declared by the service.

        @param tool Key in analysis_overlay.TOOLS.
        @param detail Free text next to the tool name.
        @param done Items done in the phase.
        @param total Items expected; 0 = no count.
        @note See ExtractWorker._forward_phase: same contract
            `phase(tool, detail)`, so the re-run announces its phases exactly
            like the full extraction.
        """
        self.phase.emit(str(tool or ""), str(detail or ""), int(done or 0), int(total or 0))

    def _raise_if_cancelled(self) -> None:
        """@brief Checker for make_logged_call: stop before a paid call once «Interrompi» was pressed.

        @throws CallCancelled when the cancel event is set, so that no further
            paid request starts after the interruption.
        """
        if self.cancel_event.is_set():
            from ..services.ai_diagnostics import CallCancelled
            raise CallCancelled(tx("Rilancio dell'inventario interrotto dall'utente: nessuna "
                                   "nuova richiesta."))

    def run(self) -> None:
        """@brief Run the reconciliation with the same logged call as the extraction."""
        try:
            # Imported inside the guard: a failed import must reach the error
            # signal, not sys.excepthook (which ends the process).
            from ..services.photo_inventory import reconcile_saved_proposals
            from ..services.openrouter import call_tool
            from ..services.ai_diagnostics import make_logged_call
            from ..services import providers
            # Key and credit of every DIRECT provider the rerun uses: the
            # inventory model and, if different, the one of the crop readings
            # (OpenRouter is already verified by _ensure_api_key).
            direct = [m for m in (self.model, (self.crop_model or '').strip() or self.model)
                      if providers.provider_of(m) == providers.PROVIDER_DEEPSEEK]
            if direct:
                providers.ensure_providers_ready(direct, self.progress.emit)
            if self.detector != 'yolo_smd':
                # With the SMD network there is no paid inventory: the service
                # announces the phase when it really starts.
                self.phase.emit('llm_inventory', self.model, 0, 0)
            call = make_logged_call(call_tool, self.progress.emit, log_prompt=True,
                                    **accepted_kwargs(make_logged_call,
                                                      cancel=self._raise_if_cancelled))
            prompts = getattr(self, "prompts", None)
            report = reconcile_saved_proposals(
                self.photo_png, self.proposals, self.length_mm, self.width_mm, self.model,
                _reporting_call(call, prompts, self.progress.emit),
                self.progress.emit, self.cache_dir, declared=self.declared,
                verification_prompt=self.verification_prompt,
                extra_instructions=self.extra_instructions, detector=self.detector,
                crop_prompt=self.crop_prompt,
                **accepted_kwargs(reconcile_saved_proposals, crop_model=self.crop_model,
                                  cancel=self.cancel_event, reviewed=self.reviewed,
                                  smd_model_path=self.smd_model_path,
                                  **({"prompts": prompts} if prompts is not None else {}),
                                  **({"step_models": self.step_models}
                                     if getattr(self, "step_models", None) is not None else {})),
                **phase_kwargs(reconcile_saved_proposals, self._forward_phase))
            self.done.emit(report)
        except Exception as exc:  # noqa: BLE001 - network/API errors are reported in the UI
            self.error.emit(tx("Rilancio dell'inventario fallito: {exc}", exc=exc))


def _reporting_call(call, prompts, log):
    """The logged call of a partial run, reporting each profile prompt it sends; `call` itself without a profile.

    The full extraction wraps its own call (datasheet_extractor with
    `prompts`); the inventory re-run and the partial re-read build their call
    here, so they wrap it here: one log line per prompt sent, with its status
    and registry number (prompt_catalog.SentPromptLog), never the text.
    """
    if prompts is None:
        return call
    from ..services.prompt_catalog import SentPromptLog
    return SentPromptLog(prompts, log).wrap(call)


#: Field of an extraction question (ExtractedParams.questions[*]["field"]) ->
#: name of the datasheet_extractor function that re-reads ONLY that datum
#: from the stored main answer. Resolved at run time: a missing function is
#: reported through `error`, never raised (the GUI must not crash when the
#: extractor is older than the interface).
REREAD_FUNCTIONS = {
    "body_height_mm": "reread_body_height",
    "pad_layout": "reread_pad_positions",
}


class RereadWorker(QThread):
    """@brief Re-read ONE datum of a single-part extraction without paying the whole analysis.

    The partial re-run of review 2026-11-04 (M16: «nessuna ri-esecuzione
    parziale» = every clarification repaid the whole extraction). It is the
    tail of ExtractWorker for one field: it receives the stored main answer
    (the dict the extraction kept with the project), the documents and
    their roles, and calls the extractor's `reread_body_height` or
    `reread_pad_positions` with the same logged call, cancellation and cost
    accounting as ReconcileWorker. The model of the re-read is chosen
    exactly as the full extraction chooses it (`model` for the height,
    `_pad_reading_model` for the pads) and written to the log before the
    call, so that the AI log says which model answered (M13).

    Signals:
        done(ExtractedParams) -- the parameters updated by the re-read
        error(str)            -- readable error message (also for a missing API)
        progress(object)      -- see ExtractWorker.progress
        phase(str, str, int, int) -- see GenerateWorker.phase
    """

    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(object)   # see ExtractWorker.progress
    phase = pyqtSignal(str, str, int, int)

    def __init__(self, stored: dict, doc_paths: list[Path], document_roles: dict | None,
                 *, field: str, model: str | None = None, pad_model: str | None = None,
                 component_family: str | None = None, system_prompt: str | None = None,
                 extra_instructions: str | None = None, part_number: str | None = None,
                 answers: dict | None = None, prompts: dict | None = None,
                 step_models: dict | None = None):
        """@brief Snapshot the stored answer and the settings before leaving the GUI thread.

        @param stored Main answer of the extraction as kept with the project
            (the dict of the tool arguments, `_candidates` included); copied.
        @param doc_paths Documents of the extraction, as for ExtractWorker.
        @param document_roles Roles of every document, copied like ExtractWorker.
        @param field Question field to re-read: a key of REREAD_FUNCTIONS
            («body_height_mm» or «pad_layout»); any other value is reported
            through `error` when the worker runs.
        @param model Main datasheet model, as ExtractWorker receives it; None
            = the extractor's DEFAULT_MODEL.
        @param pad_model Model of the dedicated pad-position reading (profile
            field 'pad_model'); None or blank = `model`. Resolved by the
            extractor's own rule, so the re-read never uses a model the full
            extraction would not have used.
        @param component_family Selected geometry family (the pad model is
            only ever chosen for a single part).
        @param system_prompt Optional datasheet system prompt, forwarded
            when the re-read function accepts it.
        @param extra_instructions Optional user notes, forwarded likewise.
        @param part_number Part number (field «Sigla»), forwarded likewise.
        @param answers The project's saved answers to structured questions
            ({question key: value}), copied; forwarded as `answers=` when the
            re-read function accepts it, as ExtractWorker does (review
            2026-11-11, K4: a re-read used to ask again what was answered).
        @param prompts The profile's prompt texts ({field: text}), copied:
            forwarded as `prompts=` when the re-read function accepts it (the
            pad and body readings send 'pad_positions_prompt' and
            'body_dimensions_prompt'), and every request is reported in the
            log (prompt_catalog.SentPromptLog). None sends the defaults.
        @param step_models The model of every analysis step (ProfileStore.
            step_models), copied. When given and the re-read function accepts
            it, the function receives `model` = the MAIN model that produced
            `stored` and resolves the reader of the pad or body step itself
            (its usability and `same_model` included); `pad_model` is then
            unused. None keeps the `_pad_reading_model` rule.
        """
        super().__init__()
        self.prompts = dict(prompts) if prompts is not None else None
        #: Step models of this re-read (a copy); None = the old pad-model rule.
        self.step_models = dict(step_models) if step_models is not None else None
        self.stored = deepcopy(stored) if isinstance(stored, dict) else {}
        self.answers = dict(answers) if isinstance(answers, dict) and answers else None
        self.doc_paths = list(doc_paths or [])
        self.document_roles = ({name: (list(roles) if isinstance(roles, (list, tuple)) else roles)
                                for name, roles in document_roles.items()}
                               if document_roles is not None else None)
        self.field = str(field or "")
        self.model = model
        self.pad_model = pad_model
        self.component_family = component_family
        self.system_prompt = system_prompt
        self.extra_instructions = extra_instructions
        self.part_number = part_number
        # Like ExtractWorker.cancel_event: raised by «Interrompi» (stop).
        self.cancel_event = threading.Event()

    def _forward_phase(self, tool: str, detail: str = "", done: int = 0, total: int = 0) -> None:
        """@brief Forward to the modal the phase declared by the service.

        @param tool Key in analysis_overlay.TOOLS.
        @param detail Free text next to the tool name.
        @param done Items done in the phase.
        @param total Items expected; 0 = no count.
        @note Same contract as ExtractWorker._forward_phase.
        """
        self.phase.emit(str(tool or ""), str(detail or ""), int(done or 0), int(total or 0))

    def _raise_if_cancelled(self) -> None:
        """@brief Checker for make_logged_call: stop before a paid call once «Interrompi» was pressed.

        @throws CallCancelled when the cancel event is set, so that no further
            paid request starts after the interruption.
        """
        if self.cancel_event.is_set():
            from ..services.ai_diagnostics import CallCancelled
            raise CallCancelled(tx("Rilettura interrotta dall'utente: nessuna nuova richiesta."))

    def _reread_model(self, extractor, main_model: str) -> str:
        """@brief The model of this re-read, chosen by the extractor's own rule.

        @param extractor The datasheet_extractor module.
        @param main_model Effective main model (default already applied).
        @return For both the pads and the body height the model
            `_pad_reading_model` picks (the dedicated reader when its
            provider is ready, else the main one, with the reason in the
            log), or `main_model` when the extractor has no such rule. The
            height is re-read by the dedicated reader as in the full
            extraction: the stored main list came from the main model, and
            two lists of one model are one witness (generality audit
            2026-11-05, A2); when the choice falls back to the main model,
            `run` says so with `same_model=True`.
        """
        choose = getattr(extractor, "_pad_reading_model", None)
        if choose is None:
            return (self.pad_model or "").strip() or main_model
        return choose(self.pad_model, main_model, self.component_family,
                      self.progress.emit, None)

    def run(self) -> None:
        """@brief Run the one re-read with the same logged call as the extraction."""
        try:
            # Imported inside the guard: a failed import must reach the error
            # signal, not sys.excepthook (which ends the process).
            from ..services import datasheet_extractor as extractor
            from ..services.openrouter import DEFAULT_MODEL, call_tool
            from ..services.ai_diagnostics import make_logged_call
            from ..services import providers
            name = REREAD_FUNCTIONS.get(self.field)
            if name is None:
                self.error.emit(tx("Rilettura non prevista per il campo {field}.", field=self.field))
                return
            # getattr at run time: the extractor's API and this worker may
            # not be at the same version (parallel fixes); the GUI reports it.
            reread = getattr(extractor, name, None)
            if reread is None:
                self.error.emit(tx("Ri-esecuzione parziale non disponibile: funzione {name} "
                                   "assente nell'estrattore.", name=name))
                return
            main_model = self.model or DEFAULT_MODEL
            step_models = getattr(self, "step_models", None)
            # The step table goes to a re-read that takes it: that function
            # resolves the pad or body step's reader from the MAIN model
            # that produced `stored` (contract of agent A, 2026-11-12).
            with_table = step_models is not None and "step_models" in accepted_kwargs(
                reread, step_models=step_models)
            if with_table:
                model = main_model
                from ..services.prompt_models import usable_model
                step_field = ("body_dimensions_prompt" if self.field == "body_height_mm"
                              else "pad_positions_prompt")
                # The model that will really answer (review finding C): the
                # step's reader when it can run, else the main model. Resolved
                # without a log line: the re-read resolves it again at its
                # call site and writes the fallback reason there, once.
                shown = usable_model(step_field, step_models, main_model)
            else:
                model = shown = self._reread_model(extractor, main_model)
            # Key and credit of every DIRECT provider the re-read uses, as
            # ReconcileWorker does (OpenRouter is verified by _ensure_api_key);
            # a step reader on another source is checked by the re-read itself.
            direct = [m for m in (model,) if providers.provider_of(m) == providers.PROVIDER_DEEPSEEK]
            if direct:
                providers.ensure_providers_ready(direct, self.progress.emit)
            # M13: the log says which model re-reads which datum (the step's
            # reader; the re-read logs a fallback when it cannot run).
            self.progress.emit(tx("Rilettura di {field} con il modello {model}.",
                                  field=self.field, model=shown))
            self.phase.emit('llm', shown, 0, 0)
            self._raise_if_cancelled()
            prompts = getattr(self, "prompts", None)
            call = make_logged_call(call_tool, self.progress.emit, log_prompt=True,
                                    **accepted_kwargs(make_logged_call,
                                                      cancel=self._raise_if_cancelled))
            params = reread(
                self.stored, self.doc_paths, model=model,
                call=_reporting_call(call, prompts, self.progress.emit),
                progress=self.progress.emit,
                **accepted_kwargs(reread,
                                  # With the table the re-read decides it itself.
                                  **({"step_models": step_models} if with_table
                                     else {"same_model": model == main_model}),
                                  **({"prompts": prompts} if prompts is not None else {}),
                                  document_roles=self.document_roles,
                                  component_family=self.component_family,
                                  system_prompt=self.system_prompt,
                                  extra_instructions=self.extra_instructions,
                                  part_number=self.part_number,
                                  cancel=self.cancel_event,
                                  # None when the project has no answer.
                                  **({"answers": dict(self.answers)} if self.answers else {})),
                **phase_kwargs(reread, self._forward_phase))
            self.done.emit(params)
        except CallCancelled as exc:
            self.error.emit(str(exc))
        except ExtractionError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - network/API errors are reported in the UI
            self.error.emit(tx("Rilettura fallita: {exc}", exc=exc))
