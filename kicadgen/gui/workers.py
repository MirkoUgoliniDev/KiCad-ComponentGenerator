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
from functools import partial
import re
import json
import threading
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication

from ..cad3d.body_params import Body3DParams
from ..cad3d.kicad_models import measuring_listener, archive_progress_listener
from ..cad3d.step_generator import Cad3DError
from ..core.package_geometry import PackageGeometry
from ..generators.castellated_module import CastellatedModuleParams
from ..generators.pin_header import PinHeaderParams
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
    """@brief Dice se si puo' passare `name=` a questa funzione senza TypeError.

    @param function Funzione che si sta per chiamare.
    @param name Nome del parametro facoltativo da passare.
    @return True quando la firma lo dichiara, oppure quando ha un `**kwargs`
        insieme ad almeno un parametro con un nome.
    @note Regole e motivazioni sono quelle di `phase_kwargs`, di cui questa e'
        la parte generale: un `**kwargs` DA SOLO e' l'impronta di un wrapper
        scritto senza `functools.wraps`, quindi non dimostra nulla.
    """
    try:
        import inspect
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):  # builtin o oggetto non introspezionabile
        return False
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
    """@brief `{'cancel': evento}` solo se il generatore 3D accetta il canale.

    @param function Generatore 3D che si sta per chiamare.
    @param cancel Evento con cui la modale chiede di fermare il figlio FreeCAD.
    @return Dizionario da espandere nella chiamata, vuoto se il canale non e'
        accettato.
    @note Stessa ragione di `phase_kwargs`: la funzione chiamata puo' essere
        sostituita (prove, strumenti) da una che non conosce questo parametro,
        e un TypeError li' farebbe fallire una generazione buona per colpa di
        una funzionalita' accessoria.
    @note A differenza dell'annuncio della fase, qui la rinuncia NON si perde
        in silenzio: chi chiama deve dirlo nel log (vedi
        `GenerateWorker._generation_kwargs`), perche' una modale che offre
        «Interrompi» su un lavoro non interrompibile prometterebbe il falso.
    """
    return {"cancel": cancel} if accepts_keyword(function, "cancel") else {}


def phase_kwargs(function, callback) -> dict:
    """@brief `{'phase': callback}` solo se la funzione di servizio lo accetta.

    @param function Funzione di servizio che si sta per chiamare.
    @param callback Callable(tool, detail) da inoltrare alla modale.
    @return Dizionario da espandere nella chiamata: vuoto se il parametro non
        esiste ancora o se la firma non dice nulla di utile.
    @note I servizi e l'interfaccia evolvono separatamente: passare `phase=` a
        una firma che non lo prevede fa fallire l'analisi con un TypeError, e
        non passarlo quando c'e' fa perdere l'annuncio della fase. Si guarda
        la firma installata invece di scommettere sull'ordine di arrivo delle
        modifiche.
    @note `**kwargs` conta come consenso solo se la firma dichiara anche
        almeno un parametro con un nome. Una firma fatta di soli `*args,
        **kwargs` e' l'impronta di un decoratore scritto SENZA
        `functools.wraps`: `inspect.signature` vede il wrapper, non la
        funzione vera, quindi `**kwargs` non dimostra nulla e passare `phase=`
        ricrea esattamente il TypeError che questo aiutante esiste per
        evitare. In quel caso si rinuncia all'annuncio: perdere una riga in
        modale e' meno grave che far cadere l'analisi. Un decoratore con
        `functools.wraps` espone `__wrapped__`, `inspect.signature` lo segue e
        la firma vera torna visibile: e' quella la soluzione, non una regola
        piu' furba qui.
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
    except (TypeError, ValueError):  # builtin o oggetto non introspezionabile
        return {}
    named = [p for p in parameters if p.kind not in (
        inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)]
    if named and any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters):
        return dict(values)
    keyword = {p.name for p in parameters if p.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
    return {name: value for name, value in values.items() if name in keyword}


def matching_cancel_kwargs(function, cancel) -> dict:
    """@brief `{'model_cancel': evento}` solo se il generatore dichiara quel canale.

    @param function Generatore 3D che si sta per chiamare.
    @param cancel Evento con cui la modale chiede di fermare il lavoro.
    @return Dizionario da espandere nella chiamata, vuoto quasi sempre.
    @note L'abbinamento dei modelli KiCad (`kicad_models.attach_models`, che
        per misurare un candidato non ancora in cache avvia `freecadcmd`) puo'
        durare minuti da solo, e la sua interruzione sta nascendo in
        `cad3d/`: il canale ordinario e' lo stesso `cancel` che gia' arriva al
        figlio FreeCAD (vedi `cancel_kwargs`), ma se l'abbinamento ricevesse un
        canale PROPRIO questa e' la porta per passarglielo. Finche' la firma
        non lo dichiara non si passa nulla: mai un TypeError per una
        funzionalita' accessoria, e le due modifiche possono atterrare in
        qualunque ordine.
    """
    return {"model_cancel": cancel} if accepts_keyword(function, "model_cancel") else {}


def _photo_scale_kwargs(generator, scale) -> dict:
    """@brief `{'photo_scale_px_per_mm': scala}` solo se il generatore chiamato la accetta.

    @param generator Funzione 3D che il worker sta per chiamare (l'involucro o
        un sostituto di prova con la firma di prima).
    @param scale Scala della foto calibrata in px/mm, o None.
    """
    return ({"photo_scale_px_per_mm": scale}
            if accepts_keyword(generator, "photo_scale_px_per_mm") else {})


#: Riga con cui lo script FreeCAD annuncia il componente su cui lavora.
_FREECAD_COMPONENT = re.compile(r"KICADGEN_COMPONENT_START:(\d+)/(\d+)")


def freecad_progress(message: str):
    """@brief Avanzamento del 3D letto dalle righe di FreeCAD.

    @param message Riga di log, gia' prefissata con «FreeCAD: ».
    @return (completati, totale) quando la riga annuncia un componente,
        altrimenti None.
    @note Il conteggio in modale si fermava a «100% · 50/50» mentre FreeCAD
        era ancora al primo componente: quel numero era l'abbinamento dei
        modelli, finito prima che FreeCAD partisse, e nessuno lo aggiornava
        dopo. FreeCAD pero' annuncia ogni componente che comincia: i
        completati sono quelli prima di lui. Su un lavoro da cinque minuti la
        differenza fra «finito» e «al primo di cinquanta» e' tutto cio' che
        chi aspetta ha bisogno di sapere.
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

    Segnali comuni:
        warning(str)  -- annotazione non applicata (es. fillet impossibile)
        progress(str) -- riga informativa per il log e la modale
        cancellable(bool) -- vedi sotto
        phase(str, str, int, int) -- vedi sotto
    """

    warning = pyqtSignal(str)
    #: Annotazioni informative (es. quanti modelli reali di KiCad sono stati
    #: usati): vanno nel log senza il prefisso di avviso, perche' non c'e'
    #: nulla che richieda attenzione.
    progress = pyqtSignal(str)
    #: True/False all'inizio del 3D: dice alla finestra se «Interrompi» ha
    #: davvero un lavoro da fermare. Falso = il generatore in uso non conosce
    #: il canale, e la modale deve togliere il comando invece di prometterlo.
    cancellable = pyqtSignal(bool)
    #: (strumento, dettaglio, fatti, totale). Dice alla modale CHI sta
    #: lavorando, che dai messaggi di avanzamento non si capiva. Un totale non
    #: positivo significa "nessun conteggio": la modale resta animata invece di
    #: mostrare una percentuale inventata.
    phase = pyqtSignal(str, str, int, int)

    def __init__(self, params, project_dir: Path, body: Body3DParams | None,
                 fillets: list, face_texts: list, single_component: bool = False):
        """@brief Snapshot the inputs of one 3D run.

        @param params Component parameters (pin matrix, package or castellated module).
        @param project_dir Project folder the files are written to.
        @param body 3D body parameters, or None when no 3D model is requested.
        @param fillets Edge fillet annotations, as given by the caller.
        @param face_texts Face text annotations, as given by the caller.
        """
        super().__init__()
        self.params = params
        self.project_dir = project_dir
        self.body = body
        self.fillets = fillets
        self.face_texts = face_texts
        self.single_component = bool(single_component)
        #: Canale di rinuncia condiviso col processo FreeCAD figlio. Vive sul
        #: worker e non sulla finestra: e' la corsa a poter essere fermata, e
        #: una corsa successiva deve nascere con un canale pulito.
        self.cancel = threading.Event()
        #: Scala della foto calibrata (px/mm) o None: la imposta la finestra
        #: dopo la costruzione, vedi `MainWindow._photo_scale_px_per_mm`.
        self.photo_scale_px_per_mm = None

    def _cancel_kwargs(self, generator) -> dict:
        """@brief Il canale di rinuncia da passare al generatore, se lo accetta.

        @param generator La funzione di generazione 3D che si sta per chiamare.
        @return `{'cancel': evento}` oppure `{}`.
        @note Quando il canale non viene accettato la corsa NON e' fermabile, e
            la cosa si dice invece di scoprirla premendo un pulsante che non fa
            niente. La riga finisce nel log e sulla modale come ogni altro
            avviso di questo worker, e `cancellable(False)` fa TOGLIERE il
            comando dalla modale: un pulsante che non ferma niente e' peggio
            di nessun pulsante.
        @note La domanda si fa a `generation_accepts_cancel`, non alla sola
            firma dell'involucro `_generate_3d`: e' il generatore reale a
            dover conoscere il canale.
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

        @note Non uccide il QThread: l'ordine viaggia sul canale e il figlio
            viene ucciso da `process_log.run_logged`, cioe' dalla stessa
            macchina del watchdog. Il worker esce poi dalla sua via d'errore
            normale (segnale d'errore e `finished`), che e' cio' che riporta la
            finestra in quiete: terminare il thread di forza lascerebbe invece
            un processo figlio orfano e la GUI senza segnale di fine.
        """
        self.cancel.set()

    def _model_progress(self, done: int, total: int) -> None:
        """@brief Porta in modale l'unico conteggio vero della generazione.

        @param done Componenti gia' esaminati.
        @param total Componenti da esaminare.
        @note Ogni componente puo' costare una misura CAD, quindi il conto
            avanza al ritmo del lavoro vero e non di un timer.
        @note Lo strumento dichiarato e' `kicad` perche' la fase e' la scelta
            del file da usare nella libreria dei modelli di KiCad: e' quello
            che avanza componente per componente ed e' quello che spiega
            l'attesa. Non si dice che FreeCAD sia fermo -- `attach_models`
            avvia `freecadcmd` per misurare i candidati che non sono ancora in
            cache -- ma quelle misure servono all'abbinamento, non alla
            costruzione del solido: mostrare il solido di FreeCAD qui faceva
            credere che il modello finale fosse gia' in lavorazione.
        """
        self.phase.emit('kicad', tx("scelta del modello per ogni componente"), done, total)

    def _archive_progress(self, done, total, records):
        """@brief Show archive outcomes and persist the per-component report."""
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
        """@brief Inoltra la riga al log e, se annuncia un componente, al conteggio."""
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
        @note Il generatore si risolve UNA volta sola, da `main_window` (vedi
            `_patch_point`): introspezione e chiamata devono riguardare lo
            stesso oggetto, anche quando e' stato sostituito.
        @note La prima cosa che fa `_generate_3d` e' l'abbinamento dei modelli
            di KiCad, non la costruzione del solido: chi chiama annuncia
            `kicad`, e `_cad_progress` passa a `freecad` quando il solido
            comincia davvero.
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

    QThread che costruisce/salva un Footprint e, se richiesto, il modello 3D.

    Il footprint 2D e il modello 3D sono riportati separatamente: un
    fallimento della generazione 3D (es. freecadcmd non trovato) non deve
    invalidare un footprint 2D generato correttamente.

    Segnali (oltre a quelli di `_Cad3DRunWorker`):
        done(str)       -- percorso assoluto del file .kicad_mod scritto
        error(str)      -- errore nella generazione del footprint 2D
        cad3d_done(str) -- percorso assoluto del file .step scritto
        cad3d_error(str) -- errore nella generazione del modello 3D
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

    def run(self) -> None:
        """@brief QThread override: save the footprint, then generate the 3D model, reporting each through its own signals."""
        try:
            # Il file lo scrive questo programma: KiCad non e' in esecuzione.
            self.phase.emit('footprint', tx("scrittura del file .kicad_mod"), 0, 0)
            self.progress.emit(tx("Generazione del footprint KiCad…"))
            host = self.footprint_params if self.footprint_params is not None else self.params
            footprint = (
                _patch_point("build_castellated_module")(host)
                if isinstance(host, CastellatedModuleParams)
                else _patch_point("build_package")(host, datasheet=self.datasheet,
                                                   description=self.description)
                if isinstance(host, PackageGeometry)
                else _patch_point("build_pin_header")(host)
            )
            pretty_dir = self.project_dir / f"{self.params.name}.pretty"
            out_path = pretty_dir / f"{self.params.name}.kicad_mod"
            footprint.save(str(out_path))
            if self.body is None and isinstance(self.params, CastellatedModuleParams):
                # Solo footprint: le note delle stime ridotte (fori, piazzole)
                # altrimenti le porta al log solo il passo 3D (punto 140,
                # «riduci e segnala»: una riduzione non resta mai muta).
                for note in self.params.geometry_notes():
                    self.progress.emit(note)
            self.done.emit(str(out_path))
        except Exception as exc:  # noqa: BLE001 - riportato in UI, non e' fatale
            self.error.emit(str(exc))
            return

        if self.body is not None:
            try:
                # La prima cosa che fa `_generate_3d` e' l'abbinamento dei
                # modelli di KiCad, non la costruzione del solido: annunciare
                # qui `freecad` accendeva il cubo di FreeCAD per tutta la fase
                # che la correzione vuole attribuita a KiCad, per poi tornare
                # indietro. Da `freecad` ci passa `_cad_progress`, quando il
                # solido comincia davvero.
                if not self.single_component and not isinstance(self.params, PackageGeometry):
                    self.phase.emit('consolidated', tx("confronto con l'archivio condiviso"), 0, 0)
                self.progress.emit(tx("Generazione del modello 3D tramite FreeCAD…"))
                result = self._generate_model()
                for warning in result.get('warnings', []):
                    self.warning.emit(warning)
                # Le note della risoluzione modelli vanno nel log anche nella
                # generazione completa, non solo nella rigenerazione 3D.
                for note in result.get('notes', []):
                    self.progress.emit(note)
                self.cad3d_done.emit(str(result["step_path"]))
            except Cad3DError as exc:
                self.cad3d_error.emit(str(exc))
            except Exception as exc:  # noqa: BLE001 - riportato in UI, non e' fatale
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

    QThread che rigenera solo il modello 3D (STEP/VRML), senza rifare il footprint.

    Serve al pulsante "Rigenera modello 3D" del tab Modello 3D: dopo aver
    aggiunto/tolto annotazioni non ha senso riscrivere anche il .kicad_mod.

    Segnali (oltre a quelli di `_Cad3DRunWorker`):
        done(str)    -- percorso assoluto del file .step scritto
        error(str)   -- messaggio di errore leggibile
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
        """
        super().__init__(params, project_dir, body, fillets, face_texts, single_component)

    def run(self) -> None:
        """@brief QThread override: regenerate the 3D model and emit done, warning or error."""
        try:
            # Stessa ragione di GenerateWorker.run: `_generate_3d` comincia
            # dall'abbinamento dei modelli di KiCad. `_cad_progress` passa a
            # `freecad` quando il solido comincia davvero.
            if not self.single_component and not isinstance(self.params, PackageGeometry):
                self.phase.emit('consolidated', tx("confronto con l'archivio condiviso"), 0, 0)
            result = self._generate_model()
            for note in result.get("notes", []):
                self.progress.emit(note)
            for warning in result.get("warnings", []):
                self.warning.emit(warning)
            self.done.emit(str(result["step_path"]))
        except Cad3DError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - riportato in UI, non e' fatale
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
        """@brief QThread override: emit the `UpdateCheckResult` (the check never raises)."""
        from ..services import update_check
        self.done.emit(update_check.check_latest_release())


class TerminalDetailWorker(QThread):
    """@brief Optional photo detail pass; never regenerates the footprint."""
    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, photo, components, model, only_leads=False, system_prompt=None,
                 cache_dir=None):
        """@brief Prepare one terminal (or lead) pass on a copy of @p components.

        @param photo PNG bytes of the reference photo.
        @param components Photo components; deep-copied, never modified.
        @param model AI model identifier.
        @param only_leads True to refine the leads of observed terminals only.
        @param system_prompt Optional profile prompt for the pass.
        @param cache_dir Project photo cache, or None for no cache.
        @note Costs are charged by the window, which binds the run project
            before `start()` (`MainWindow._start_terminal_refinement`).
        """
        super().__init__()
        self.photo, self.components, self.model = photo, deepcopy(components), model
        self.only_leads = only_leads
        self.system_prompt = system_prompt
        # Cache del PROGETTO (vedi photo_inventory.PHOTO_CACHE_SUBDIR); None =
        # nessuna cache, mai una cartella comune.
        self.cache_dir = cache_dir

    def run(self):
        """@brief QThread override: observe terminals (or refine leads only) and emit the updated components."""
        from ..services.photo_terminals import observe_terminals
        from ..services.photo_leads import refine_leads
        from ..services.openrouter import call_tool
        from ..services.ai_diagnostics import make_logged_call
        try:
            operation = refine_leads if self.only_leads else observe_terminals
            self.done.emit(operation(self.photo, self.components, self.model,
                make_logged_call(call_tool, self.progress.emit, log_prompt=True), self.progress.emit,
                self.cache_dir,
                **({'system_prompt': self.system_prompt} if self.system_prompt else {})))
        except Exception as exc:
            self.error.emit(tx("Dettaglio terminali fallito: {exc}", exc=exc))


class ExtractWorker(QThread):
    """@brief Extract datasheet parameters with session-selected photo detection.

    `model` e' l'id OpenRouter da usare per questa estrazione: puo' essere
    il default del profilo SQLite oppure un modello diverso scelto solo per
    la sessione corrente.

    Segnali:
        done(ExtractedParams) -- parametri estratti con successo
        error(str)            -- messaggio di errore leggibile
        progress(str)        -- avanzamento dell'analisi AI, incluso ogni crop
    """

    done = pyqtSignal(object)
    error = pyqtSignal(str)
    #: Righe di avanzamento. `object`, non `str`: una riga puo' essere una
    #: messages.Message con il suo tipo (avviso), e un segnale `str` la
    #: ridurrebbe a testo semplice perdendolo (punto 136).
    progress = pyqtSignal(object)
    #: Vedi GenerateWorker.phase.
    phase = pyqtSignal(str, str, int, int)
    #: (foto PNG, proposte del rilevatore, lunghezza mm, larghezza mm). Emesso
    #: fra il rilevamento locale e la riconciliazione a pagamento: il worker si
    #: ferma qui finche' l'interfaccia non risponde con deliver_review, perche'
    #: una proposta tolta adesso non viene ne' inviata ne' pagata. Le quote
    #: sono quelle con cui la foto e' stata calibrata, non quelle dei campi:
    #: durante l'estrazione i campi non sono ancora stati compilati.
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
        @note YOLO changes photo proposals only; AI verification remains configured.
        """
        super().__init__()
        self.doc_paths = doc_paths
        self.system_prompt = system_prompt
        #: Prompt of the dedicated pin-numbering call; None = factory default.
        self.numbering_prompt = numbering_prompt
        self.extra_instructions = extra_instructions
        self.include_nc_pins = include_nc_pins
        self.model = model
        self.vision_model = vision_model
        self.grounding_prompt = grounding_prompt
        self.verification_prompt = verification_prompt
        self.verification_model = verification_model
        self.terminal_prompt = terminal_prompt
        # Copia anche delle liste: dal 26/09 ogni file porta una lista di ruoli.
        self.document_roles = ({name: (list(roles) if isinstance(roles, (list, tuple)) else roles)
                                for name, roles in document_roles.items()}
                               if document_roles is not None else None)
        self.component_family = component_family
        self.photo_detector = photo_detector
        self.yolo_model_path = yolo_model_path
        # File della rete SMD scelto nella riga «Modello rete SMD» (None: il
        # predefinito in models/smd).
        self.smd_model_path = smd_model_path
        self.yolo_confidence = yolo_confidence
        self.yolo_settings = dict(yolo_settings) if yolo_settings is not None else None
        self.crop_prompt = crop_prompt
        self.crop_model = crop_model
        # «Interrompi» lo alza: il servizio lo controlla prima di ogni chiamata
        # a pagamento, cosi' dopo l'interruzione non ne parte nessun'altra
        # (verifica del 25/09, D8). Quella gia' in volo non si puo' richiamare.
        self.cancel_event = threading.Event()
        self._review_answered = threading.Event()
        self._review_result = None

    def _review_proposals(self, photo_png, proposals, length_mm, width_mm):
        """@brief Chiede all'interfaccia quali corpi tenere, e aspetta.

        @param photo_png Fotografia calibrata, gia' codificata in PNG.
        @param proposals Proposte del rilevatore locale.
        @param length_mm Estensione della scheda lungo X, in mm.
        @param width_mm Estensione lungo Y, in mm.
        @return Le proposte da tenere, oppure None se la revisione e' annullata.
        @note Gira nel thread del worker: il segnale attraversa i thread e la
            risposta torna da deliver_review. L'attesa e' voluta -- la chiamata
            a pagamento non deve partire prima che qualcuno abbia guardato.
        """
        self._review_result = None
        self._review_answered.clear()
        self.review_requested.emit(photo_png or b"", [dict(p) for p in proposals],
                                   float(length_mm), float(width_mm))
        self._review_answered.wait()
        return self._review_result

    def deliver_review(self, kept) -> None:
        """@brief Consegna al worker l'esito della revisione e lo sblocca.

        @param kept Proposte da tenere, o None per annullare l'estrazione.
        @note Va chiamata SEMPRE, anche quando qualcosa va storto: un worker
            lasciato in attesa non finisce piu'.
        """
        self._review_result = kept
        self._review_answered.set()

    def _forward_phase(self, tool: str, detail: str = "", done: int = 0, total: int = 0) -> None:
        """@brief Porta in modale lo strumento che il servizio dichiara al lavoro.

        @param tool Chiave in analysis_overlay.TOOLS, es. yolo o llm.
        @param detail Testo libero: l'etichetta del rilevatore o il modello.
        @param done Elementi fatti della fase (HANDOFF 156: corpi letti).
        @param total Elementi previsti; 0 = nessun conteggio.
        @note Contratto del servizio: `phase(tool, detail, done=0, total=0)`.
            Senza conteggio (0, 0) la modale resta animata invece di mostrare
            una percentuale inventata. Serve soprattutto al rilevatore locale ONNX, che prima
            girava senza che nulla lo annunciasse: chi guardava vedeva la nuvola
            del modello a pagamento anche mentre non si spendeva niente.
        """
        self.phase.emit(str(tool or ""), str(detail or ""), int(done or 0), int(total or 0))

    def run(self) -> None:
        """@brief Forward the captured settings and emit extracted data or an error."""
        try:
            # Risolto una volta sola da `main_window` (vedi `_patch_point`):
            # firma interrogata e funzione chiamata sono lo stesso oggetto.
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
                # Cartella del PROGETTO assegnata da chi avvia; None = nessuna cache.
                **accepted_kwargs(extract,
                                  photo_cache_dir=getattr(self, "photo_cache_dir", None),
                                  yolo_confidence=self.yolo_confidence,
                                  yolo_settings=self.yolo_settings,
                                  crop_prompt=self.crop_prompt,
                                  crop_model=self.crop_model,
                                  numbering_prompt=self.numbering_prompt,
                                  cancel=self.cancel_event),
                **phase_kwargs(extract, self._forward_phase),
            )
            self.done.emit(params)
        except ExtractionError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - errori rete/API riportati in UI
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
        # Stessa scelta dell'estrattore quando c'e' il suo helper: il pretest
        # deve provare esattamente le foto che l'analisi proverebbe.
        from ..services import datasheet_extractor
        indices = getattr(datasheet_extractor, "photo_document_indices", None)
        if callable(indices):
            return indices(self.doc_paths, self.document_roles)
        from ..services.document_roles import PHOTO_ROLE, role_list
        return [i for i, path in enumerate(self.doc_paths)
                if PHOTO_ROLE in role_list(self.document_roles.get(path.name))] or [-1]

    def run(self) -> None:
        """@brief Detect, let the user review, emit the report (None if cancelled)."""
        from ..services.datasheet_extractor import COMPONENT_KINDS
        from ..services.photo_inventory import ReviewCancelled, extract_inventory

        def no_ai(**_kwargs):
            # Il ramo yolo_only non chiama mai il modello: se succedesse sarebbe
            # un difetto, e deve fermarsi invece di spendere.
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
        except Exception as exc:  # noqa: BLE001 - riportato in UI come ogni errore del worker
            self.error.emit(str(exc))


class ReconcileWorker(QThread):
    """@brief Rilancia la sola riconciliazione a pagamento su proposte gia' riviste.

    E' la coda di ExtractWorker senza la testa: niente documenti, niente
    rilevatore locale, niente filtro dei contatti ne' misura dei bordi. Riceve
    le proposte tenute nella finestra «Corpi rilevati», riaperta sull'ultimo
    salvataggio, e fa UNA richiesta al modello che guarda la fotografia, poi
    rilegge ogni corpo dal suo ritaglio, campiona i colori, rilegge le sigle e
    salva l'evidenza come farebbe l'estrazione normale.

    Segnali:
        done(InventoryResult) -- inventario riconciliato
        error(str)            -- messaggio di errore leggibile
        progress(str)         -- avanzamento, compresa la riga della chiamata
        phase(str, str, int, int) -- vedi GenerateWorker.phase
    """

    done = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(object)   # see ExtractWorker.progress
    phase = pyqtSignal(str, str, int, int)

    def __init__(self, photo_png, proposals, length_mm, width_mm, model, cache_dir,
                 *, declared=None, verification_prompt=None, extra_instructions=None,
                 detector='yolo', crop_prompt=None, crop_model=None, reviewed=True,
                 smd_model_path=None):
        """@brief Fotografa le proposte approvate prima di lasciare il thread GUI.

        @param photo_png Fotografia calibrata su cui le proposte furono misurate.
        @param proposals Proposte tenute dalla revisione, inoltrate cosi' come sono.
        @param length_mm Estensione della scheda lungo X, in mm (calibrazione).
        @param width_mm Estensione lungo Y, in mm.
        @param model Modello a pagamento della riconciliazione.
        @param cache_dir Cartella della cache delle risposte e dell'evidenza.
        @param declared Quote dichiarate della scheda, solo per la nota di coerenza.
        @param verification_prompt Note del profilo, come nel percorso normale.
        @param extra_instructions Note dell'utente, come nel percorso normale.
        @param detector Nome del rilevatore da registrare nella calibrazione.
        @param crop_prompt Prompt della lettura per ritaglio (campo del profilo
            'crop_prompt'), come per ExtractWorker: il rilancio esegue la stessa
            lettura dei singoli corpi (HANDOFF 146 V2). None o vuoto: testo di
            fabbrica.
        @param crop_model Modello della lettura per ritaglio (campo del profilo
            'crop_model', HANDOFF 150); None o vuoto: lo stesso `model` della
            richiesta sulla fotografia.
        """
        super().__init__()
        self.photo_png = photo_png
        self.proposals = deepcopy(proposals)
        self.length_mm, self.width_mm = float(length_mm), float(width_mm)
        self.model = model
        self.cache_dir = cache_dir
        self.declared = deepcopy(declared) if declared else None
        self.verification_prompt = verification_prompt
        self.extra_instructions = extra_instructions
        self.detector = detector
        # Le proposte vengono da una revisione fatta da una persona?
        # (ReviewBundle.reviewed: False per il pacchetto di ripiego dalla cache).
        self.reviewed = bool(reviewed)
        self.smd_model_path = smd_model_path
        self.crop_prompt = crop_prompt
        self.crop_model = crop_model
        # Come ExtractWorker.cancel_event: alzato da «Interrompi».
        self.cancel_event = threading.Event()

    def _forward_phase(self, tool: str, detail: str = "", done: int = 0, total: int = 0) -> None:
        """@brief Inoltra alla modale la fase dichiarata dal servizio.

        @param tool Chiave in analysis_overlay.TOOLS.
        @param detail Testo libero accanto al nome dello strumento.
        @note Vedi ExtractWorker._forward_phase: stesso contratto
            `phase(tool, detail)`, cosi' il rilancio annuncia le sue fasi
            esattamente come l'estrazione completa.
        """
        self.phase.emit(str(tool or ""), str(detail or ""), int(done or 0), int(total or 0))

    def _raise_if_cancelled(self) -> None:
        """Checker for make_logged_call: stop before a paid call once «Interrompi» was pressed."""
        if self.cancel_event.is_set():
            from ..services.ai_diagnostics import CallCancelled
            raise CallCancelled(tx("Rilancio dell'inventario interrotto dall'utente: nessuna "
                                   "nuova richiesta."))

    def run(self) -> None:
        """@brief Esegue la riconciliazione con la stessa chiamata registrata dell'estrazione."""
        from ..services.photo_inventory import reconcile_saved_proposals
        from ..services.openrouter import call_tool
        from ..services.ai_diagnostics import make_logged_call
        from ..services import providers
        try:
            # Chiave e credito di ogni fornitore DIRETTO che il rilancio usa:
            # il modello dell'inventario e quello, se diverso, delle letture
            # dei ritagli (OpenRouter e' gia' verificato da _ensure_api_key).
            direct = [m for m in (self.model, (self.crop_model or '').strip() or self.model)
                      if providers.provider_of(m) == providers.PROVIDER_DEEPSEEK]
            if direct:
                providers.ensure_providers_ready(direct, self.progress.emit)
            if self.detector != 'yolo_smd':
                # Con la rete SMD non c'e' inventario a pagamento: la fase la
                # annuncia il servizio quando parte davvero.
                self.phase.emit('llm_inventory', self.model, 0, 0)
            report = reconcile_saved_proposals(
                self.photo_png, self.proposals, self.length_mm, self.width_mm, self.model,
                make_logged_call(call_tool, self.progress.emit, log_prompt=True,
                                 **accepted_kwargs(make_logged_call,
                                                   cancel=self._raise_if_cancelled)),
                self.progress.emit, self.cache_dir, declared=self.declared,
                verification_prompt=self.verification_prompt,
                extra_instructions=self.extra_instructions, detector=self.detector,
                crop_prompt=self.crop_prompt,
                **accepted_kwargs(reconcile_saved_proposals, crop_model=self.crop_model,
                                  cancel=self.cancel_event, reviewed=self.reviewed,
                                  smd_model_path=self.smd_model_path),
                **phase_kwargs(reconcile_saved_proposals, self._forward_phase))
            self.done.emit(report)
        except Exception as exc:  # noqa: BLE001 - errori rete/API riportati in UI
            self.error.emit(tx("Rilancio dell'inventario fallito: {exc}", exc=exc))
