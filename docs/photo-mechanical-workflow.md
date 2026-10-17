# Inventario fotografico per ingombri meccanici

## Obiettivo e utilizzo

Ricostruire gli ingombri del modulo, **non fare reverse engineering**: il
percorso standard produce parallelepipedi semplici, senza scritte, con XY
dalla fotografia calibrata, oppure modelli della libreria 3D di KiCad scelti
per somiglianza di misure. La classificazione elettrica precisa non serve. I
colori sono campionati dai pixel, le altezze sono stime della classe visiva.

Vale solo per le due tipologie **Scheda con componenti** (connettori passanti
o castellated): per i componenti singoli l'inventario non viene eseguito.

1. Nella scheda **Progetto**, passo **Documenti**, caricare una foto
   dall'alto e spuntarle il ruolo **Alta risoluzione** (è l'unico ruolo che va
   al rilevatore e alla calibrazione); le viste quotate e il datasheet
   servono alle quote e al pinout, non aggiungono corpi.
2. Nel passo **Montaggio e analisi** scegliere la tipologia e, nel riquadro
   **Strumenti AI**, il rilevatore. Premere **Estrai parametri con AI** (a
   pagamento). La finestra di avanzamento elenca le fasi e chi lavora in
   ciascuna; il **Log AI** (Ctrl+L) mostra sorgente, localizzazioni,
   riconciliazioni, esclusioni e ingombri finali, e viene scritto in
   `analisi-ai.log` nel progetto.
3. La **finestra di revisione** si apre due volte (vedi sotto): prima sulle
   proposte del rilevatore locale, poi — se la pausa dopo l'inventario è
   attiva — sui corpi riconciliati. Selezionare e spostare i rettangoli,
   modificarne le quote, aggiungere o eliminare elementi. **Annulla** non
   modifica i dati; **Applica correzioni** aggiorna la tabella e invalida la
   vecchia anteprima, da rigenerare.
4. Verificare le altezze con una misura diretta, poi **Rigenera modello STEP
   con queste annotazioni** (F5, gratuito) nella scheda Modello STEP.

Un rettangolo indica un rilevamento, **non una misura certificata**. I
marcatori della lista dicono se la riga ha almeno un avviso (`[!]`, di solito
«modello KiCad non confermato»), se il tipo non è stato stabilito (`[?]`) o se
la classe del rilevatore contraddice quella dell'AI. Anche un inventario senza
marcatori richiede il controllo delle quote e delle altezze prima di
verificare interferenze meccaniche. Non sono introdotti margini di sicurezza
impliciti.

## Configurazione dei modelli e dei prompt

- **Configurazione → Default generali AI…**: il modello AI **unico** usato
  per documenti, localizzazione, verifica e lettura dei ritagli, con la sua
  sorgente (OpenRouter o DeepSeek API diretta) nella scheda **Modello**, e i
  prompt base nelle schede **Datasheet**, **Localizzazione foto**, **Verifica
  foto**. Predefinito: `deepseek-api/deepseek-flash` dai sorgenti (database
  dei profili), `anthropic/claude-opus-5` via OpenRouter nel programma
  installato dall'MSI. Il modello si sceglie solo da qui o dal riquadro
  «Modello AI», mai modificando a mano il database.
- **Configurazione → Gestione tipologie e profili AI…**: per ogni tipologia,
  le stesse tre sezioni con i sotto-tab **Prompt base**, **Istruzioni foto**,
  **Terminali**, **Reofori**, **Lettura ritagli** (localizzazione) e **Schede
  e moduli**, **Componente singolo**, **Numerazione pin** (datasheet), più
  **Istruzioni tipologia**; ogni campo può ereditare il default generale. Le
  istruzioni foto si aggiungono ai prompt fotografici, verifica compresa, ma
  non alla lettura dei ritagli, che descrive un corpo solo.
- Il pulsante accanto a **Modello AI** nel riquadro Strumenti AI cambia il
  modello per la sola sessione (filtro per costo); il default non cambia.
- Gli schemi JSON restano nel codice. Una modifica al prompt invalida la cache
  delle relative chiamate e può comportare nuove inferenze a pagamento.

## Pipeline

- **Una sola foto di riferimento** dall'alto; le altre viste servono a quote e
  pinout e non vengono sommate come se contenessero altri componenti.
- **Calibrazione** (OpenCV): ritaglio della PCB sulle quote note, asse per
  asse (anche foto con proporzioni diverse dalla scheda). La foto deve avere
  il lato lungo orizzontale ed essere quasi ortogonale: la prospettiva non
  viene rettificata, un'inclinazione significativa viene rifiutata e la foto
  rifiutata non calibra nulla. Le fasi OpenCV sono annunciate nella finestra
  di avanzamento. Ogni immagine è ricodificata senza metadati prima
  dell'invio; se i metadati non si possono togliere, l'immagine è rifiutata.
- **Rilevatore locale** (`Rilevamento foto`): con *YOLO + rete SMD locale
  (v3)* (default dai sorgenti) o *YOLO locale + verifica AI* (default nel
  programma installato) la rete YOLO propone i corpi sulla vista completa e su
  tasselli ingranditi da 640/1280 px (confidenza 0.25, NMS IoU 0.5, modificabili
  in **Parametri YOLO**). Le proposte aprono la finestra di revisione nella
  **fase rilevatore**: OK manda avanti quelle tenute, Annulla ferma tutto
  prima di pagare. Con *Solo YOLO locale (pretest, nessuna AI)* ci si ferma
  qui: nessuna chiamata, la scheda viene calibrata con i campi Lunghezza /
  Larghezza corpo. Dettagli in `yolo-photo-workflow.md` e
  `CLASSIFICATORE_SMD.md`.
- **Inventario AI globale**: una chiamata con la foto intera, l'overlay
  numerato e una griglia di dettagli ingranditi calcolata dalle quote della
  scheda (il corpo più piccolo assunto presente, il lato corto di un 0402,
  deve avere risoluzione sufficiente). Il prompt separa la forma del package
  dalla funzione e chiede i terminali per lato; per i moduli castellated
  conta i semi-fori già in questa chiamata, con le strisce dei bordi
  ingrandite. L'immagine è adattata ai limiti del provider del modello
  (DeepSeek e OpenRouter hanno limiti diversi, letti dall'identificativo del
  modello, mai dal nome).
- **Riconciliazione** uno-a-uno fra rilevatore e AI: IoU oppure contenimento
  con centri coincidenti entro l'incertezza in pixel; un piccolo passivo non
  viene assorbito da un grande IC; l'assegnazione considera l'intera vista.
  La lettura del rilevatore (classe, confidenza, riquadro) resta accanto a
  quella dell'AI e non viene mai sovrascritta; la classe del rilevatore passa
  nelle righe riconciliate. I corpi proposti dalla sola AI sono confrontati
  con il rilevatore e partono **esclusi** finché non vengono tenuti.
- **Rete SMD locale** (solo *YOLO + rete SMD locale*): classifica i corpi
  tenuti senza chiamate remote; solo le risposte `_other` o sotto soglia
  passano alla lettura AI dei ritagli.
- **Lettura ritagli** (a pagamento): ogni corpo non risolto viene riletto
  **due volte** dal suo ritaglio ingrandito con un questionario visivo
  (osservazioni, non nomi di classe); la classe la deriva il programma e si
  adotta solo ciò su cui le due letture concordano. Le letture fallite sono
  tenute separate dai disaccordi; le contraddizioni con l'inventario sono
  segnalate per la revisione. La famiglia di un package a terminali viene
  ricavata dal numero di piedini e dall'ingombro misurato quando il nome
  dichiarato contraddice la misura, con i limiti JEDEC delle famiglie SOIC.
- **Lettura sigle**: marcature leggibili riportate come dato, mai usate come
  regola di classificazione.
- **Conversione pixel → mm** deterministica, origine al centro, X a destra, Y
  in basso. I volumi comprendono l'ingombro visibile delle terminazioni; i
  terminali osservati vengono disegnati nel 3D. Per ogni piedino il modello
  dice se lo vede attaccato al corpo, staccato o se non lo distingue:
  attaccato viene unito alla faccia piu' vicina coprendo solo lo spazio
  visto; staccato resta come in foto, senza metallo aggiunto; se la foto
  mostra uno spazio troppo grande per un piedino «attaccato», nessun ponte e
  un dubbio nel registro; senza risposta decide la geometria. Le misure stimate non vengono
  mai usate come misure: le piazzole stimate vengono ridotte per starci.
- **Contatti castellated** confrontati con la foto (`edge_notches`): le tacche
  del contorno contano i semi-fori di ogni lato in modo strutturale (il fondo
  che appare attraverso il contorno), con soglia dal rumore del contorno
  stesso; un lato dichiarato su un bordo senza tacche viene escluso, un
  numero diverso viene ricontato dal modello sull'ingrandimento di quel bordo
  (solo i lati contraddetti) e adottato solo se i contatti ricontati stanno
  dove la foto mostra le tacche; una serie che tocca l'estremità del bordo
  dà un limite inferiore. Sui lati confermati le piazzole vengono
  **misurate** (`edge_lands`): colore del soldermask campionato fra due
  contatti, «non maschera» separato con Otsu, serigrafie e piste tolte con
  un'apertura morfologica, larghezza come quartile superiore delle righe della
  piazzola, con l'incertezza della scala. La misura sostituisce le stime del
  modello (`value_sources` «measured»), mai una quota letta nei documenti; un
  interasse non letto non posiziona i fori passanti, e una larghezza di
  piazzola pari al passo non è una misura.
- **Pausa dopo l'inventario** (Configurazione → Opzioni di generazione…,
  attiva per default): la revisione si riapre nella **fase inventario** prima
  dell'abbinamento dei modelli KiCad e del 3D. Gratuita.
- **Modelli KiCad e FreeCAD**: per ogni corpo si cercano modelli della
  libreria 3D di KiCad con misure compatibili (vedi
  `interactive-kicad-models.md`), poi FreeCAD costruisce il solido; i
  controlli CAD di bordo e sovrapposizione supportano rotazioni non multiple
  di 90°.

### Cache e costi

Le risposte vengono salvate in `<progetto>/photo-cache` (gli script di
`examples/` usano `<output>/photo-cache`), senza chiave API e senza duplicare
i dati immagine; `photo-cache/yolo-audit/` contiene gli audit del rilevatore.
La chiave della cache include modello, schema, prompt, parametri e pixel: una
ripetizione identica riusa la cache e il log lo dice. Risposte incomplete non
sono trattate come successi riutilizzabili, ma gli elementi validi di una
risposta parziale vengono conservati. La cache può contenere informazioni
estratte dai documenti: resta locale e ignorata da Git. Per una nuova
inferenza degli stessi input si rimuove esplicitamente la cache.

Ogni chiamata a pagamento finisce in `ai-costs.json` nel progetto, divisa per
provider (OpenRouter: costo riportato; DeepSeek: token × prezzo ufficiale).
La finestra di analisi mostra il costo dell'analisi corrente e il totale del
progetto; la riga **Credito AI** il saldo del conto.

### Dopo l'analisi

Dalla scheda Progetto: **Revisione componenti…** riapre gratis la fase
salvata in `photo-review/` (proposte del rilevatore prima dell'inventario,
corpi riconciliati dopo); **Rilancia inventario AI (a pagamento)…** rifà solo
l'inventario sulle proposte salvate. Dalla scheda Modello STEP: **Modifica
componenti…** (Ctrl+E) modifica ogni corpo a mano, anche senza foto, e il
tasto destro su un corpo offre eliminazione, ripristino, rotazione e cambio
del modello KiCad. Nuove estrazioni e correzioni applicate invalidano
l'anteprima precedente: completamenti CAD riferiti ai dati vecchi non la
ripristinano.

## Prova storica sull'immagine di riferimento (settembre 2026)

Immagine `Screenshot_1.png`, PCB quotata 42.8 × 15.2 mm, ritaglio
`[315,117,1435,519]` dell'originale 1597 × 576 pixel. Il riferimento contiene
42 corpi annotati separatamente dall'assistente guardando l'originale; **non
è una BOM o una validazione metrologica umana** e non entra in prompt o
codice produttivo. Con associazione uno-a-uno e IoU ≥ 0.5 il percorso a
ingombri produsse 42 rettangoli, 36 corrispondenti (precisione e richiamo
85.7 %), errore medio dei centri 0.159 mm. Il numero 42 da solo non prova
completezza: sei rettangoli non raggiungevano la soglia. Il modello FreeCAD
fu generato con 42 volumi; spessore PCB 1 mm e altezze erano **stime**; i
passi dei contatti erano espliciti nella figura (sinistra 3 × 5.08 mm, destra
4 × 2.54 mm). La prova precede il rilevatore locale, la rete SMD e la lettura
dei ritagli: descrive il percorso AI-only di allora, non le prestazioni
attuali, e va ripetuta su più moduli prima di attribuire prestazioni generali.

## Riproduzione da riga di comando

La foto di riferimento `Screenshot_1.png` **non è nel repository** (lo è solo
la sua annotazione, `tests/fixtures/pcb_reference.json`, che ne registra
dimensioni e SHA-256): senza quel file la prova storica non si può ripetere.
I comandi sotto valgono anche per un'altra foto, ma la valutazione offline
confronta con il riferimento solo la foto annotata.

```powershell
# Rete/API: prima esecuzione a pagamento, successive identiche dalla cache
.venv/Scripts/python.exe examples/inspect_photo_inventory.py <foto>.png --length 42.8 --width 15.2 --output generated/mechanical-envelope

# Offline, sullo stesso riferimento fotografico
.venv/Scripts/python.exe tools/evaluate_photo_inventory.py generated/mechanical-envelope/inventory.json --photo <foto>.png --output generated/mechanical-envelope/metrics.json

# Offline, richiede FreeCAD: STEP, FCStd, VRML e anteprime del modello reale
.venv/Scripts/python.exe examples/build_inventory_envelopes.py generated/mechanical-envelope/inventory.json --side left:3:5.08 --side right:4:2.54

.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=.venv/claude-test-run
```

`inspect_photo_inventory.py` accetta anche `--model` e `--classifier-model`;
`build_inventory_envelopes.py` anche `--output`, `--observed-packages` e
`--pcb-thickness`. Gli artefatti locali finiscono in
`generated/mechanical-envelope`. Il fixture `tests/fixtures/pcb_reference.json`
è esclusivamente di valutazione.

## Riferimenti tecnici

- Il formato nativo di localizzazione `box_2d=[ymin,xmin,ymax,xmax]`,
  normalizzato 0–1000, è documentato da
  [Google Image Understanding](https://ai.google.dev/gemini-api/docs/image-understanding).
- La separazione tra calibrazione, preparazione e ispezione è coerente con
  [Cognex Inspection Concepts](https://docs.cognex.com/apasap_431/web/EN/ConceptsGuide/Content/Topics/Concept/Apps/Inspection/Inspection%20Concept.htm).
  Qui si lavora comunque su foto di prodotto, non su un sistema ottico
  industriale calibrato e validato.
