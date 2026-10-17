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

All production Python code and maintained examples must use Doxygen-compatible
documentation. Every module, public class, public function, public method and
Qt override must have a docstring beginning with `@brief`. Document parameters
with `@param`, return values with `@return`, raised exceptions with `@throws`,
and important invariants with `@note` where applicable. Private helpers may use
short explanatory docstrings when their behavior is non-obvious.

Comments must explain intent, contracts, safety boundaries or engineering
assumptions; do not restate the implementation line by line. Keep documentation
in English in source code, use precise units for geometry, and update the
docstring whenever behavior, inputs, outputs or side effects change. New public
APIs without Doxygen-compatible documentation are not complete and must not be
merged. Run the documentation test together with the relevant functional tests.

**Comments in English — user rule, binding (2026-10-16).** Every `#` and `#:`
comment in production code, tools, examples and `main.py` is written in
English, exactly like the docstrings. Verbatim user quotes, UI labels and
literal program output stay in Italian inside «…» on ONE line, with an English
gloss next to them. `tests/test_documentation.py`
(`test_comments_are_written_in_english`) enforces it: an Italian comment fails
the suite. Write new comments in English from the start; never translate a
quote or reword English only to satisfy the checker.

Use the project skills in `.claude/skills/` for generation and verification workflows. Use the reviewer agent for read-only audits before publishing a component.

# Strategia del progetto e consegna agli agenti

## Obiettivo concordato con l'utente

Ricostruire da fotografie gli **ingombri meccanici** di un modulo PCB per KiCad.
Non fare reverse engineering: nomi esatti, valori elettrici e marcature dei
componenti non sono prioritari. Il percorso standard genera volumi generici
senza scritte. Conservare invece geometria, posizione, orientamento e pinout
del modulo. Le altezze da una sola foto superiore sono stime, non misure.

## Strategia implementata

1. Usare una sola foto superiore come riferimento geometrico. Le altre viste
   aiutano quote e pinout, ma non aggiungono copie degli stessi componenti.
2. Rilevare e ritagliare la PCB, calibrandola con dimensioni note e valide.
   Rifiutare inclinazioni significative: non è implementata una rettifica
   prospettica. Origine al centro, X a destra, Y in basso.
3. Localizzare corpi visibili con una vista completa e tre strisce sovrapposte.
   Usare la vista completa per i grandi corpi e zone di competenza per i piccoli.
4. Associare i rilevamenti geometricamente, uno-a-uno sull'intera vista, senza
   dipendere dall'ordine di risposta. Non assorbire piccoli passivi in grandi IC.
5. Verificare la presenza dei corpi, non la loro identità elettronica. Non
   inventare coordinate durante la verifica; mantenere come dubbi i risultati
   non verificabili. Escludere pad vuoti, vie e serigrafie.
6. Convertire deterministicamente pixel in mm; campionare i colori dalla foto.
   Anche il fallback globale deve restare generico, senza marcature e con
   provenienza/incertezza delle altezze esplicita.
7. Esporre log dei passi e confronto fotografico modificabile: sposta, modifica
   quote, aggiungi, elimina, Applica/Annulla. I nuovi allegati sono spuntati
   per default. Applica e nuova estrazione invalidano la vecchia anteprima;
   risultati CAD tardivi non devono ripristinarla.
8. Validare semanticamente risposte e cache: numeri finiti non booleani,
   rettangoli validi, ID attesi completi e univoci. Non memorizzare risposte
   incomplete come successi. Conservare gli elementi validi di risposte parziali.

OpenCV serve alla preparazione/calibrazione, non è un rilevatore universale di
componenti. Le prove di aggancio ai bordi e classificazione dettagliata hanno
mostrato regressioni: mantenerle fuori dal percorso standard finché non validate.
Non ottimizzare il semplice conteggio dei componenti a scapito della geometria.

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
is 8909 passed,
12 skipped and 19 subtests (2026-10-17, branch `ADVANCED` merged into `main`: status bar
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
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/claude-test-run
```

Usare una directory temporanea dedicata libera per ogni esecuzione. Non leggere
o pubblicare segreti di `.env`; non committare cache, foto private o artefatti
generati. Le inferenze nuove possono avere costi: distinguere sempre replay
offline da chiamate reali. Preservare modifiche utente e correggere difetti
confermati senza espandere inutilmente lo scopo.
