# KiCad Component Generator project guidance

This repository generates KiCad component libraries from mechanical and electrical data.

## Important invariants

- Keep symbol pin numbers and names aligned with the source pinout.
- Distinguish pin pitch from row spacing; never reuse one value for the other without evidence.
- Keep generated footprint, symbol and 3D model names consistent.
- Treat estimated mechanical dimensions as estimates and record them in notes.
- Do not commit `.env`, `config.json`, Python caches, generated output or local KiCad state.

## Validation

Run the focused test suite with `pytest`. For changes affecting geometry, run the relevant CAD/footprint tests and inspect generated output under `generated/` locally.

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

Eseguire la suite dalla radice del repository:

```powershell
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/claude-test-run
```

Usare una directory temporanea dedicata libera per ogni esecuzione. Non leggere
o pubblicare segreti di `.env`; non committare cache, foto private o artefatti
generati. Le inferenze nuove possono avere costi: distinguere sempre replay
offline da chiamate reali. Preservare modifiche utente e correggere difetti
confermati senza espandere inutilmente lo scopo.
