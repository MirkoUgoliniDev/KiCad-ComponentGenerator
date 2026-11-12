---
paths:
  - "kicadgen/services/photo_*.py"
  - "kicadgen/services/smd_component_classifier.py"
  - "kicadgen/services/edge_*.py"
  - "kicadgen/services/contact_zones.py"
  - "kicadgen/gui/photo_review_dialog.py"
  - "tools/evaluate_photo_inventory.py"
  - "docs/photo-mechanical-workflow.md"
---

# Pipeline fotografica: obiettivo e strategia

Moved here from `.claude/CLAUDE.md` (sections «Obiettivo concordato con
l'utente» and «Strategia implementata») on 2026-11-10; the point numbers are
unchanged because code comments cite them («strategy point 4»).
Procedure, paid calls versus replay and verification: `photo-pipeline-change`
skill.

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

## Costs

New inferences cost money: always say whether a run is an offline replay from
`<project>/photo-cache` or a real call. Changing a prompt or a schema
invalidates the cache, so the next run is paid again. A real call needs the
user's agreement for that run.
