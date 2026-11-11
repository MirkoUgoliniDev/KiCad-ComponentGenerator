# KiCad Component Generator project guidance

This repository generates KiCad component libraries from mechanical and electrical data.

## Important invariants

- Keep symbol pin numbers and names aligned with the source pinout.
- Distinguish pin pitch from row spacing; never reuse one value for the other without evidence.
- Keep generated footprint, symbol and 3D model names consistent.
- Treat estimated mechanical dimensions as estimates and record them in notes.
- Do not commit `.env`, `config.json`, Python caches, generated output or local KiCad state.
- Keep generated KiCad output isolated until it has been reviewed in the KiCad tab.
- Preview must not modify `fp-lib-table`; project installation uses `${KIPRJMOD}` and
  global installation uses explicit absolute paths with overwrite confirmation.
- Preserve unrelated library entries and make packaging operations rollback-safe.

## Handoff sempre aggiornato

**Regola dell'utente, vincolante.** `HANDOFF.md` nella radice del progetto va
tenuto allineato al lavoro in corso, non scritto alla fine.

- **Prima di partire con le modifiche, l'handoff si aggiorna** (regola
  dell'utente del 2026-09-24): quando l'utente dice di applicare delle
  modifiche, PRIMA si scrive nell'handoff l'elenco di cio' che si sta per
  fare (richieste, decisioni, interpretazioni e dubbi), POI si tocca il
  codice. Se il lavoro si interrompe a meta', chi riprende sa cosa era in
  corso e cosa mancava.
- **Aggiornarlo appena lo stato cambia in modo sostanziale**: un pezzo
  completato, una diagnosi che ribalta quella precedente, una decisione presa
  dall'utente, un difetto trovato e non risolto.
- **Prima di qualunque interruzione prevedibile** (fine del credito, fine
  sessione, cambio di argomento) l'handoff deve gia' essere valido: chi
  riprende non deve ricostruire nulla dalla conversazione.
- **Deve contenere**, come minimo: branch e ultimo commit, esito della suite,
  la catena di cause dei problemi affrontati, il lavoro aperto diviso fra
  *richiesto dall'utente* e *segnalato ma mai autorizzato*, le decisioni prese
  dall'utente, e le trappole operative che farebbero perdere tempo a chi
  riprende.
- **Correggere l'handoff quando si scopre che conteneva un errore**, citando
  la correzione: una diagnosi sbagliata lasciata li' e' peggio di nessuna
  diagnosi.
- I numeri in testa al file (commit, conteggio test) vanno riallineati quando
  si committa, altrimenti invecchiano in silenzio.

## Correzioni: generalizzare, non far combaciare il caso corrente

**Regola dell'utente, vincolante.** Ogni correzione deve risolvere la *classe*
di problema, non il singolo esemplare in lavorazione. È vietato tarare il
codice perché torni il componente che si ha sotto mano.

In pratica:

- **Nessuna soglia scelta perché il caso osservato ci ricada dentro.** Se un
  valore viene alzato o abbassato per far passare un pezzo specifico, è un
  sintomo, non una soluzione: va trovato il criterio fisico che distingue
  davvero i due casi.
- **Nessuna regola basata su nomi, sigle o marche.** Le regole si fondano su
  grandezze misurate o su attributi dichiarati, mai su `if name == "ESP32"`.
- **Quando l'informazione non è deducibile dai dati disponibili, chiederla**
  invece di indovinarla. Esempio concreto: il verso di innesto di un
  connettore non si ricava dall'ingombro — un coassiale U.FL e un jack da
  bordo possono misurare uguale — ma si vede nella fotografia, quindi va
  chiesto al modello (campo `mating`) e non dedotto da soglie dimensionali.
- **Se resta un'euristica, va dichiarata tale**: commento che spiega perché è
  un ripiego, su quali dati è stata tarata e cosa la renderebbe superflua. Non
  va spacciata per una regola generale.
- **Un test che passa solo sul caso osservato non è una verifica.** Servono un
  caso positivo e un caso negativo che la regola deve distinguere per ragioni
  strutturali, non per coincidenza numerica.
- **Riconoscere quando una separazione è fragile.** Se due casi vengono
  distinti da un solo criterio su due, o con un margine risicato, va detto
  all'utente invece di lasciarlo scoprire dopo.

Vale anche per i test: se una modifica li rompe, si aggiorna il *contratto*
che esprimono capendo perché è cambiato — non si allenta l'asserzione finché
non diventa verde.

## Validation

Run the focused test suite with `pytest`. For changes affecting geometry, run the relevant CAD/footprint tests and inspect generated output under `generated/` locally.
For KiCad packaging changes, also run `tests/test_kicad_library.py`, validate the
SQLite translation catalog for all four languages, and use KiCad CLI read-only
render/export checks when KiCad is installed.

## Documentation standard — mandatory Doxygen-compatible comments

Moved to `.claude/rules/python-documentation.md` (loaded for every `*.py`):
Doxygen `@brief` docstrings on every module, public class, function, method
and Qt override; every `#` and `#:` comment in English (binding user rule,
2026-10-16). `tests/test_documentation.py` enforces both.

## Rules by area

`.claude/rules/` holds the rules of one area each, loaded when files of that
area are worked on (`paths:` in the file's header). What is binding
everywhere stays in this file; a rule lives in ONE place.

- `python-documentation.md` — docstrings and English comments (`**/*.py`).
- `photo-pipeline.md` — goal and «Strategia implementata» of the photo pipeline.
- `document-extraction.md` — datasheet and drawing readings, witnesses, costs.
- `cad3d.md` — FreeCAD child process, staging, STEP geometry.
- `canvas.md` — symbol editor and footprint preview, shared components.
- `kicad-packaging.md` — generators, KiCad tab, installation, rollback.
- `ui-text.md` — visible strings and their four translations.
- `tests.md` — positive/negative cases, contracts, how the suite is valid.

Use the project skills in `.claude/skills/` for generation and verification workflows;
`generate-kicad-component` routes each area to its skill (`document-extraction-change`,
`photo-pipeline-change`, `cad3d-change`, `canvas-shared-change`, `add-ui-text`). Read-only
agents: `kicad-library-verifier` for one component's generated files before publishing it,
`kicad-workflow-reviewer` for KiCad tab / packaging changes, `generality-auditor` before
committing detection, geometry, extraction or validation fixes, `canvas-parity-reviewer` after
canvas changes, `release-readiness-reviewer` before a release, `adversarial-reviewer` (through
the `adversarial-review` skill) for full reviews. KiCad 9 guide notes:
`docs/reference/kicad9-getting-started-notes.md`.

# Strategia del progetto e consegna agli agenti

## Obiettivo concordato con l'utente

Ricostruire da fotografie gli **ingombri meccanici** di un modulo PCB per KiCad,
senza reverse engineering. Testo completo in `.claude/rules/photo-pipeline.md`.

## Strategia implementata

Spostata in `.claude/rules/photo-pipeline.md` (punti 1-8, numerazione
invariata: i commenti nel codice citano «strategy point 4»).

## Mappa del codice

- `kicadgen/services/photo_inventory.py`: calibrazione, viste, associazione.
- `kicadgen/services/photo_grounding.py`: localizzazione, presenza, cache.
- `kicadgen/services/photo_refine.py`: controlli fotografici e strumenti geometrici.
- `kicadgen/services/datasheet_extractor.py`: estrazione generale e fallback.
- `kicadgen/gui/photo_review_dialog.py`: revisione fotografica interattiva.
- `kicadgen/gui/main_window.py`: integrazione, log, worker e revisioni anteprima.
- `kicadgen/services/kicad_library.py`: isolated KiCad preview, portable project
  installation, global library registration and rollback-safe file handling.
- `kicadgen/cad3d/step_generator.py`: geometria CAD, bordo e sovrapposizioni;
  supportare anche rotazioni non multiple di 90 gradi.
- `docs/photo-mechanical-workflow.md`: dettagli, limiti e comandi di riproduzione.

## Agenti usati nell'ultima verifica e correzione chirurgica

Tre sottoagenti hanno lavorato in parallelo su ambiti di file separati;
il coordinatore ha integrato e verificato l'insieme. Non sono agenti permanenti
configurati nel repository.

| Agente | Ambito e contributo |
|---|---|
| Gibbs | GUI: invalidazione delle anteprime e rifiuto di risultati/errori CAD obsoleti; test dei segnali Qt tardivi. |
| Turing | Estrazione e cache: validazione numerica/strutturale, ID, fallback meccanico e conservazione dei lati validi. |
| Bernoulli | Geometria: limiti del PCB e sovrapposizioni per rotazioni arbitrarie; rifiuto di foto inclinate e test di regressione. |
| Coordinatore | Associazione globale uno-a-uno tra viste, integrazione, revisione delle modifiche, replay offline, prova FreeCAD, documentazione e Git. |

Per proseguire: assegnare file disgiunti, chiedere riproduzioni dei difetti e test,
poi integrare centralmente. Non far modificare contemporaneamente lo stesso file
a più agenti. Non delegare commit/push indipendenti sullo stesso checkout.
Per le modifiche al packaging KiCad, usare l'agente read-only
`kicad-workflow-reviewer` prima della pubblicazione.

## Skill e strumenti effettivamente usati

Nell'ultima verifica/correzione non sono state attivate skill `SKILL.md`:
era manutenzione ordinaria del repository. Non è stato usato ImageGen per
ricostruire la scheda. Non attribuire a skill disponibili ma non invocate il
lavoro svolto.

Sono stati usati sottoagenti, lettura del codice e ricerca testuale, patch mirate,
pytest/PyQt6, replay delle risposte AI dalla cache, FreeCAD per la generazione
reale e Git per commit/push. I modelli AI della pipeline applicativa sono cosa
diversa dai sottoagenti e dalle skill: vedere codice e documento del workflow
per configurazione e valori predefiniti.

## Verifiche e limiti alla consegna

- Commit delle correzioni: `7e49a79`.
- Suite locale: 610 test e 19 sottotest superati.
- Replay offline della foto campione: stessi 42 ingombri, ritaglio e geometrie
  invariati, senza nuove chiamate AI né marcature.
- Prova reale FreeCAD con volume ruotato di 45 gradi: ingombro entro il bordo
  previsto dopo il posizionamento corretto.
- Sul riferimento annotato dall'assistente, 36/42 rettangoli superano IoU 0.5:
  il conteggio 42 non dimostra da solo completezza o precisione certificata.
- Il riferimento non è una BOM validata né una misura metrologica. Verificare
  altezze e distanze critiche con viste quotate o misure; testare altri moduli
  prima di sostenere prestazioni generali o industriali.

## Current KiCad delivery baseline

The repository now includes a dedicated KiCad tab with three operations: test
the generated result in an isolated PCB Editor project, install a portable
project library, or install a global library. The current full-suite baseline
(2026-09-26 evening, branch `FIX` merged into `main`: PIANO-FIX start page and
recent projects, «Account AI...» with the Windows Credential Manager, workflow and
status bars, help/log/update/crash-report menu, «Strumenti» menu, STEP preview
mesh cache, no board silkscreen frame, YOLO model picker, wheel guard, analysis
blocked without a component type, fixes of the adversarial generality audit, see
HANDOFF.md points 71-83; then points 84-91: medium/low audit fixes, STEP preview
loader, per-side hole provenance, AI recount of self-contradictory sides, Qt buttons
in the chosen language; 2026-09-27, points 93-105: Piazzole label, photos with other
proportions calibrated axis by axis, Datasheet role and PDF-only-Datasheet, 3D double
click, view roll arrows, centred window; then points 107-112: inventory prompt with
package shape separate from function and leads per side, observed leads drawn in 3D,
cube buttons, pad tooltips, unclassified filter; points 114-115: no silkscreen outline
around 3D components, OpenCV phases announced; points 116-117: «Copia formato» in the 3D
view with a non-modal popup; points 118-123: marking reading, structural contact filter,
DeepSeek direct API as second provider; points 124-128: AI bodies checked against the
local detector, recount of contradicted sides only, DeepSeek cost as tokens x official
price, project cost split by provider; point 129: «Account AI» lists every provider's
key with its balance, DeepSeek key in the Credential Manager; point 131: all review-130
findings in blocks A-D — atomic preferences, DeepSeek retries, 3D output staging, test
infrastructure with network guard and Qt message handler, dead code removed, shared
validation helpers, workers module, mesh preparation off the GUI thread; point 132:
visible PCB thickness with its provenance; points 133-137: half-hole count on the photo
with an AI recount of disagreeing sides, current-analysis cost, phase list in the progress
card, one retry on unreadable inventory JSON, spin box arrows; points 138-139: adversarial
audit and fixes — data-derived half-hole count, relative-centre recount check, cost queue and
status bar, CI hang report; points 140-141: estimated sizes never used as measures, estimated
pads reduced to fit, AI-only bodies discarded or left out until kept; point 142: the AI
counts castellated contacts in its first call, with enlarged edge strips; points 144-146,
committed on `FIX` only, not merged: per-body enlarged crop reading read twice, image
limits per provider, crop prompt chosen on a bench and editable in the profile dialog,
failed readings kept apart from disagreements; points 147-148, `FIX` only: crop reading as a visual
questionnaire with the class derived by the program, contradictions with the inventory flagged for
review, JEDEC bounds for SOIC families; point 149, `FIX` only: detector class carried into the
reconciled rows, leaded family from lead count + measured envelope when the declared name
contradicts the measure; point 150, `FIX` only: profile field «Modello lettura ritagli», schema v5)
; points 161-169, committed on `FIX` as `ab0591e` and merged into `main`: DeepSeek reasoning
checkbox, one review window in two stages with the detector's reading kept beside the AI's,
three-section «Parametri» modal, phantom sides excluded by the photo, pulsing running mark,
unread row spacing ignored, pad width at the pitch downgraded, lands measured on the photo)
is 13605 passed,
13 skipped and 19 subtests (2026-11-11 night, branch `FOOTPRINT-ADVANCE`, NOT merged into `main`, halves 8287/9/19 and
5318/4: a drawn-hole contradiction of two agreeing pad readings always becomes the pad-layout question (never a block,
never a silent confirmation), a terminal read once is an estimate, every prompt sent to a model lives in the analysis
profile with its status, diff and restore (kicadgen/services/prompt_catalog.py, profile schema 9), footprint writes
ask an open pad-layout question first; see HANDOFF.md «STATO 11/11/2026 notte»; before that, 13393 passed,
13 skipped and 19 subtests (2026-11-11 afternoon, branch `FOOTPRINT-ADVANCE`, commit `677aa87`, NOT merged into `main`, halves 8255/9/19
and 5138/4: fixes of the regressions and defects that a full ten-agent review found in `1ca72ba`, and of what an
independent verification found in those fixes — one invariant for explicit pads (a valid footprint or unread geometry
with a conflict naming pads, values and remedy, never a paid answer lost to the program's own derivation), the body
check of the drawn-hole witness re-derived as a decision table, a third wording for pads the rules cannot settle, a
conflict-only height re-read that removes nothing; see HANDOFF.md «REVISIONE COMPLETA DEL CODICE — 11/11/2026», where
the findings NOT fixed are listed; before that, 13117 passed,
13 skipped and 19 subtests (2026-11-11, branch `FOOTPRINT-ADVANCE`, commit `1ca72ba`, NOT merged into `main`, halves 8057/9/19 and
5060/4: the eleven high findings of the 2026-11-10 adversarial review of document extraction and their own review —
per-pad terminal, printed hole and provenance with holes and lands derived by the program (kicadgen/core/tht_estimates.py),
estimated pads marked in tooltip, checks and description with «Valori verificati sul disegno», drawn-hole witness that
says whether it verified the scale (body_check, scale_verified), body plan sizes as an unordered pair, one writer for
every pad-layout path, partial re-reads merged by scope, contradicting saved answers asked again, rules by area in
`.claude/rules/`; see HANDOFF.md «RIPRESA RAPIDA» and «10/11 sera»; before that, 12850 passed,
13 skipped and 19 subtests (2026-11-10, branch `FOOTPRINT-ADVANCE` merged into `main`, commit `d8303a3`, halves 7862/9/19 and 4988/4: unfilled shapes hit on the outline like KiCad through kicadgen/gui/canvas_hit.py, one precedence and one pointer tracker for both canvases, one closing handle, pads the AI did not read block the footprint instead of failing the analysis, see docs/canvas-hit-plan.md and HANDOFF.md 09/11 sera; before that, 12666 passed,
13 skipped and 19 subtests (2026-11-09 evening, branch `FOOTPRINT-ADVANCE` merged into `main`, commit `e3eff26`, halves 7761/9/19 and 4905/4: back layers B.SilkS/B.Fab with mirrored texts, «Sposta ancora» with the STEP offset measured with kicad-cli, add/remove vertex in both canvases, keepout zones checked like KiCad's DRC, see docs/footprint-back-anchor-vertices-zones-plan.md and HANDOFF.md 09/11; before that, 12366 passed,
13 skipped and 19 subtests (2026-11-09, branch `FOOTPRINT-ADVANCE` merged into `main`, commit `33cbd08`, halves 7527/9/19 and 4839/4: the orthogonal dimension keeps its preview's axis inside the box as KiCad, SILK_MIN 0.10 mm sourced to KLC F5.1, five low findings of an adversarial review fixed; before that, 12362 passed,
13 skipped and 19 subtests (2026-11-08, branch `FOOTPRINT-ADVANCE`, commit `8bee44f`, halves 7523/9/19 and 4839/4:
KiCad-grade drawing tools in both canvases — ToolGrid with Select, live Properties panel, object snap with typed
markers, free/90/45 line modes on Shift+Space, KLC per-layer widths, polygon, Bezier, text box, radial/centre/leader
dimensions measured against kicad-cli 10, plus the fixes of a six-reviewer adversarial review, see
docs/footprint-drawing-kicad-plan.md and HANDOFF.md 08/11; before that, 11918 passed,
13 skipped and 19 subtests (2026-11-07, branch `FOOTPRINT-ADVANCE`, commit `4e977b1`, halves 7118/9/19 and 4800/4:
reference image under the footprint preview — calibrated photo centred on the body, image file with scale, offset,
opacity, two-click calibration, relative paths; footprint priority 3 closed; before that, 11909 passed,
13 skipped and 19 subtests (2026-11-06 night, branch `FOOTPRINT-ADVANCE`, commit `40b2f1d`, halves 7109/9/19 and
4800/4: exportable dimensions — «Quota» tool, aligned and orthogonal KiCad `dimension` writer whose side was
verified with kicad-cli, «Esporta come quota» from measures, one ink and label model shared by writer, checks and
preview; before that, 11900 passed,
13 skipped and 19 subtests (2026-11-06 night, branch `FOOTPRINT-ADVANCE`, commit `3c5b9bc`, halves 7100/9/19 and
4800/4: footprint polygons closed like the symbol's line tool (plan C12), fp_poly writer, fill from dialog and
menu, checks on filled areas and polygon pin-1 marks; before that, 11891 passed,
13 skipped and 19 subtests (2026-11-06 evening, branch `FOOTPRINT-ADVANCE`, commit `1bab5ea`, halves 7091/9/19
and 4800/4: footprint side panel «Layer / Controlli / Disegno» — layer visibility by LAYER_ROLE, courtyard and
F.Fab drawn, kicadgen/services/footprint_checks.py geometric rules with published thresholds, conftest post-test
collection made deterministic, camera goldens regenerated for the 250 px panel; before that, 11863 passed,
13 skipped and 19 subtests (2026-11-06, branch `FOOTPRINT-ADVANCE` from `main` d4a8ecd, commit `e67d2df`,
halves 7063/9/19 and 4800/4: footprint tab priority 1 — slots `(drill oval w h)`, pad type and top-view
rotation in the pad dialog, «Attributi» flags in the metadata group written after the mounting type,
generality audit fixes, see HANDOFF.md 06/11; before that, 11829 passed,
13 skipped and 19 subtests (2026-11-05, branch `RELAY` merged into `main` as `53c3507`, released as v1.0.7,
halves 7029/9/19 and 4800/4: every finding of the 2026-11-04 single-part review fixed by four fixers and the
coordinator — declared attributes in the schemas, structured questions with saved answers, drawn-hole witness
with slots and references, redacted AI log, partial re-reading — plus the generality audit of that diff; the
relay symbol composed from DECLARED pin functions and contact labels, coil + contacts as KiCad's Relay_SPDT,
hidden pin names, «Simbolo funzionale» toggle, see HANDOFF.md 05/11; before that, 11649 passed,
13 skipped and 19 subtests (2026-11-04 night, branch `ADVANCED`, halves 6892/9/19 and 4757/4: second
adversarial review by 12 read-only agents and the fix of every finding by 7 fixers plus the coordinator;
castellated sides read at the declared positions, a measured absence only when every position is read bare
and the offset is declared, partial or chance matches left for review (generality audit GA1-GA11); pin-1
mark on A1 only on a real grid; the body outline's menu keeps the drawing-origin entries; a chain reading
confirmed by the drawn holes beats a competitor that fits only distorted, an incomplete main answer is replaced
by the confirmed reading (fragile, declared); the body quotes of a single part are read a second time from the
printed dimensions (height from the side view), both answers list the printed vertical quotes and vote,
a tie-break reading on enlarged tiles, pin functions read from a drawn circuit symbol, no hand-typed height;
see HANDOFF.md 03/11 notte; before that, 11358 passed, 13 skipped and 19 subtests (2026-11-03, halves A 6723/9/19,
B 4635/4: full
adversarial review by 10 read-only agents and the fix of every finding by 7 fixers on disjoint files,
then a generality audit of the fixes; pin numbers as text end to end (A1/A2), pinout table with every
pad, editable body dimensions and a body outline moved and resized with the mouse in the footprint
preview, dimensions in the measures' teal, KLC layer widths and courtyards (pin header 0.5 mm,
packages 0.25 mm), FreeCAD children in a Job Object, KiCad install warnings (shadowing, open project,
too old KiCad), new skills document-extraction-change and cad3d-change; see HANDOFF.md 03/11; before
that, 11145 passed, 13 skipped and 19 subtests (2026-11-02 evening, halves A 6556/9/19, B 4589/4:
one grid only in the footprint preview (the old scene-item grid removed), a view-only drawing
origin — body corner, centre, footprint origin or pad — with marker, grid through it and the
symbol editor's pointer line measured from it with Y up, pad position typed from it, drill
holes that show the grid beneath, brighter dark-grid dots shared by both tabs, pad magnet on
the visible grid dots, see docs/canvas-origin-plan.md; before that, 11117 passed,
2026-11-02, halves A 6533/9/19, B 4584/4: shared
«Misura» ruler in both tabs with editable footprint dimensions («Modifica quota…», one pad moves) and
measures that follow their targets; package pads dragged in the footprint preview with selection,
a green/orange centring crosshair and a grid magnet; single-component grid answers checked against the
drawn holes, part-number field and KiCad library witnesses, symbol pins from the footprint pads;
before that, 10837 passed, 2026-10-31 midday, branch `ADVANCED`: DeepSeek calls sent in strict mode on the beta endpoint whenever the tool schema converts losslessly (every field nullable, normal resend on a 400/404/422), one corrective retry for a single-part answer with a broken shape; before that, 10812 passed, 2026-10-31, branch `ADVANCED`: single-part pad positions read by a dedicated model (profile field `pad_model`, default openai/gpt-5.6-sol, schema v7), readings checked against the holes found in the drawing image by `kicadgen/services/drawing_holes.py` (geometry only, never numbering), unconfirmed or disagreeing readings block generation, tool_choice compatibility levels for Claude 5.5; before that, 10723 passed, 2026-10-30 evening, branch `ADVANCED`: a side declared «through_hole»
whose edge the photo shows cut by notches as deep as a half-hole is corrected to half_hole, shallower
pad dents only warn; single-part pad positions summed by the program from the dimension chains the
model transcribes, compared by signed offsets, never adopted without an independent agreeing reading,
five generality audits; before that, 10678 passed, 2026-10-30, user guide in four languages: guida.en/fr/de.html opened by
the interface language with an Italian fallback, two stale «modello 3D» labels fixed; before that,
10639 passed, 2026-10-29 evening, branch `ADVANCED`, released as v1.0.3: second full
adversarial review — 10 read-only agents, 76 findings, 5 high reproduced — and its fixes by fixers on
disjoint files; release gate message, crashing STEP models remembered, cancellable HTTP and model
matching, shared YOLO SMD default, symbol click cost 1371 -> 64 ms, see HANDOFF.md 29/10; before
that, 10253 passed, 2026-10-28 evening, branch `ADVANCED` merged into `main` as `8cec8ee`:
model licenses reviewed, weights published as assets of the GitHub release «models», YOLO26
end-to-end and SMD network exported to ONNX and read by the program, setup shipping every
models/*/*.onnx fetched with SHA-256 verification; release v1.0.2 published by the workflow
(first automatic release, MSI 462 MB with the five models; v1.0.1 is a tag without a Release);
before that, 10207 passed, 2026-10-28 night: full adversarial review of the
whole codebase by ten read-only agents and its fixes by eleven fixing agents on disjoint files —
the GitHub suite failed only because tests assumed KiCad, FreeCAD and untracked weights (window
fixture fakes FreeCAD, hand-written glyph library fixture, AI detector in the prompt test); PDFs
uploaded without metadata (pypdf); linked fp-lib-table kept linked; failed STEP removal keeps the
block; recent project of another folder commits that folder; Cancel during marking reading
stops the analysis; KiCad text control characters refused; shared output-name rule in
kicadgen/core/output_names.py; every FreeCAD launch through run_logged; process tree kill
checks creation times; see HANDOFF.md 27/10-28/10. The run had 2 failures, both then fixed and
re-run green, not a second full run; before that, 9809 passed, 2026-10-27: FreeCAD and KiCad found wherever
they are installed — per-user `%LOCALAPPDATA%\Programs`, the declared `%ProgramFiles%`, the
folders of their «Uninstall» registry entries — through `kicadgen/services/installed_programs.py`,
tests isolated from the real registry by a conftest fixture; optional requirements moved into
`requirements/`; MEMORY.md and the 2026-10-09 review report moved into `docs/`; README with
screenshots (`tools/make_readme_screenshots.py`) and Mermaid diagrams; project skills and
read-only agents in `.claude/`; before that, 9796 passed, 2026-10-18 evening: ONE shared element selection, `kicadgen/gui/canvas_selection.py` (IndexSelection, RubberBand, encloses, is_click), for the symbol editor and the footprint preview: the footprint selects, band-selects, group-drags and acts on the selection with one undo step, background click and Escape clear in both tabs, selection cleared on undo/redo/delete, clipboards emptied on project switch, trembling click release is quiet, see docs/canvas-selection-plan.md; before that, 9714 passed: ONE shared element menu and clipboard, `kicadgen/gui/canvas_menus.py`, and shared point transforms, `kicadgen/core/drawing_transform.py`, for the symbol editor and the footprint preview; footprint drawings turn, mirror, copy/paste and have a properties dialog, texts carry `angle` 0/90 written as `(at x y 90)`; QMenu items padded so text is not cut; a right click on a selected symbol element keeps the selection; a saved text angle 180/270 is normalised modulo 180; see docs/canvas-menus-plan.md; before that, 9709 passed; before that, 9579 passed: ONE shared click-tool layer, `kicadgen/gui/canvas_clicktools.py`, drives wire/shape/text/draw tools in BOTH the symbol editor and the footprint preview, see docs/canvas-clicktools-plan.md; a tool cursor that QGraphicsView restores after the tool ended is dropped by `settle_cursor`; before that, 9361 passed: the symbol editor and the
footprint preview also share ONE camera (`kicadgen/gui/canvas_camera.py`: zoom clamped to
per-tab limits, wheel ±4 notches, scrollbar pan without drift, memoised fit) and ONE grid
painter (`kicadgen/gui/canvas_grid.py`: `ExposedRegion` + `paint_grid`, laid out only where
Qt paints, pixel-identical to before); parity tests
`tests/test_symbol_camera_grid_parity.py`, `tests/test_footprint_camera_grid_parity.py`,
unit tests `tests/test_canvas_camera.py`, `tests/test_canvas_grid.py`; the pixel goldens
under `tests/data/camera_golden/` are SHA hashes that depend on the Qt build; click tools
(wire, shapes, draw mode) still to unify, see docs/canvas-camera-grid-plan.md and
docs/canvas-gestures-plan.md; before that, 9075 passed, 2026-10-17 late night: one shared gesture
component, `kicadgen/gui/canvas_gestures.py`, drives drags, hover handles, key repeat, deferred
recovery writes and Escape/Hide cancel in BOTH the symbol editor and the footprint preview
(parity tests `tests/test_symbol_gesture_parity.py`, `tests/test_footprint_gesture_parity.py`);
camera and click tools still to unify, see docs/canvas-gestures-plan.md; before that, 8977
passed, same night, merged into `main`: the
Reference/Value texts of the symbol editor follow the pointer live and commit once on
release, as the footprint preview's texts; the plan for one shared gesture component is in
docs/canvas-gestures-plan.md; before that, 8970 passed, same evening: the
symbol editor lays out its grid only where Qt paints and keys the pin cache on the pin's
picture, same pixels; before that, 8961 passed, same day:
«il 3D» meaning the model renamed «modello STEP» in 24 messages, KiCad text markup and
escapes drawn as KiCad 10, symbol editor redrawing only changed pins and writing the
recovery draft on key release, THT spins saved at the end of the edit, profile cache keyed
on SQLite's change counter, FreeCAD children killed with their whole process tree; before
that, 8915 passed, same day: metadata
fields bordered red/yellow by their KLC findings with the reason in the tooltip and a
Datasheet hint; before that, same day, 8909 passed: status bar
right at start-up, Qt tab scroll buttons retranslated, desktop fonts only in a child test
process, every visible «modello 3D» renamed «modello STEP» in four languages, unmoved
Reference/Value written centred in footprint and symbol, opaque grey hints, and the
performance audit — metadata typing 450-870 ms -> 4-9 ms per key on a real board, photo
crops, profile store, costs, symbol drag, dialogs and document cards no longer redo
unchanged work). Before that, 8826 passed,
12 skipped and 19 subtests (2026-10-16 evening, branch `ADVANCED`, not merged: designator
in grey counts as filled, medium findings 12-16 closed, two full adversarial reviews and
their fixes, lead attachment declared by the vision model, default model chosen only from
the interface, every comment and docstring in English enforced by the documentation test,
detector's own strong/weak count, bottom-left anchors for the symbol and footprint property
texts with KiCad fixed, texts drawn at KiCad's width, pin numbers at KiCad's position, anchor
square on hover in both tabs). Before that, 8557 passed,
12 skipped, 2 expected failures and 19 subtests (the night between 2026-10-15 and 2026-10-16, after the full-codebase
audit and the fixes of its 11 medium findings: generated files aligned with the parameters,
every KiCad block naming an enabled remedy, user pin names kept across analyses, no invented
3D body, rename carrying the symbol, KiCad library robustness, photos uploaded without
metadata, package orientation doubts asked; four adversarial review rounds). Before that,
8244 passed,
11 skipped, 2 expected failures and 19 subtests (2026-10-15 late evening, after the
keywords field starting empty and following every extraction). Before that, 8237 passed,
11 skipped, 2 expected failures and 19 subtests (2026-10-15 evening, after required
metadata blocking installation, «Controlli KiCad (KLC)» with remedies, symbol property defaults
clearing top pins, project keywords in footprint tags and test windows freed instead of frozen;
three adversarial review rounds). Before that, 8096 passed,
11 skipped, 2 expected failures and 19 subtests (2026-10-15, after property and pin
texts drawn at KiCad's size in the symbol and footprint previews). Before that, 8093 passed,
11 skipped, 2 expected failures and 19 subtests (2026-10-15, after the symbol draft
applied into the open project on a working-folder change; branch `ADVANCED`). Before that, 8091 passed,
11 skipped, 2 expected failures and 19 subtests (2026-10-14 evening, after the footprint
draft with Applica/Annulla like the symbol and the fixes of its adversarial review; merged
into `main`). Before that, 8058 passed, 11 skipped, 2 expected failures and 19 subtests
(2026-10-14, after the residuals of the
four-cycle review, the four user decisions D1-D4, bottom-left anchored drawn texts at KiCad
size, the centred unapplied-symbol modal and the root cause of the flaky drill test; merged
into `main`). Before that, 7695 passed, 11 skipped, 2 expected failures and 19 subtests
(2026-10-14 night, after the four-cycle adversarial review and its fixes). Before that,
7443 passed, 11 skipped, 2 expected failures and 19 subtests plus 5 fixed failures
(2026-10-13 night, after the footprint drawing palette, fine dotted grid, KiCad layer colours
and kept camera; before that, 7344 passed, 11 skipped and 2 expected failures on 2026-10-12
night, after the adversarial review of the
start-up, lazy 3D viewer, glyph catalogue and symbol-editor work; before that, 2026-10-02 late
evening: 6630 passed, 8 skipped, 2 expected failures and 19 subtests after the adversarial
review of the SMD/KiCad/GUI/hole-rule work and the one-retry on unreadable document answers;
earlier the same evening, branch `ADVANCED`: local
SMD network as default detector, AI only for unresolved bodies, network package and pose
driving provisional KiCad models; single AI model in a «Modello» tab with DeepSeek/OpenRouter
source; models in models/yolo and models/smd), run in two halves (HANDOFF point 113:
the single-process run can die at random with a native Qt/VTK crash). The last packaging integration was merge
commit `692ca95` on `main`; the latest merge of `ADVANCED` into `main` is recorded in HANDOFF.md. Keep these values aligned with HANDOFF.md.

Eseguire la suite dalla radice del repository:

```powershell
.venv/Scripts/python.exe tools/run_test_halves.py both -- --basetemp=.venv/claude-test-run
```

(two separate processes: a single-process run can die with a native Qt/VTK
fail-fast, HANDOFF point 113; see the `run-test-suite` skill).

Usare una directory temporanea dedicata libera per ogni esecuzione. Non leggere
o pubblicare segreti di `.env`; non committare cache, foto private o artefatti
generati. Le inferenze nuove possono avere costi: distinguere sempre replay
offline da chiamate reali. Preservare modifiche utente e correggere difetti
confermati senza espandere inutilmente lo scopo.
