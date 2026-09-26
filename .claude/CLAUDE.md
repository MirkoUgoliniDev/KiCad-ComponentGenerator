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
HANDOFF.md points 71-83) is 5150 passed,
4 skipped, 2 expected failures and 19 subtests. The last packaging integration was merge
commit `692ca95` on `main`. Keep these values aligned with HANDOFF.md.

Eseguire la suite dalla radice del repository:

```powershell
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/claude-test-run
```

Usare una directory temporanea dedicata libera per ogni esecuzione. Non leggere
o pubblicare segreti di `.env`; non committare cache, foto private o artefatti
generati. Le inferenze nuove possono avere costi: distinguere sempre replay
offline da chiamate reali. Preservare modifiche utente e correggere difetti
confermati senza espandere inutilmente lo scopo.
