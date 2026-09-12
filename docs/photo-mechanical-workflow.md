# Inventario fotografico per ingombri meccanici

## Obiettivo e utilizzo

Ricostruire gli ingombri del modulo, **non fare reverse engineering**: il percorso
standard produce parallelepipedi semplici, senza scritte, con XY dalla fotografia
calibrata. La classificazione elettrica precisa non serve. I colori sono campionati
dai pixel, le altezze sono stime grossolane della classe visiva.

1. Caricare una foto dall'alto e le eventuali viste quotate. I nuovi allegati sono
   selezionati con una spunta per default.
2. Premere **Estrai parametri con AI**. Il pulsante **≡ Log AI** nel tab Progetto
   apre una finestra modale con sorgente, localizzazioni, duplicati riconciliati,
   esclusioni e ingombri finali. Il log si aggiorna anche mentre è aperto e resta
   disponibile dopo la chiusura. Il pulsante **ⓘ** accanto alla cartella componente
   apre le informazioni sulla cartella e sull'esportazione.
3. Aprire **Confronta foto**: selezionare/spostare i rettangoli, modificarne quote,
   aggiungere o eliminare elementi. Annulla non modifica i dati; Applica aggiorna
   la tabella e invalida la vecchia anteprima, da rigenerare.
4. Verificare le altezze con una vista laterale o una misura, poi rigenerare il 3D.

Il verde indica un rilevamento, **non una misura certificata**. L'arancione indica
un dubbio automatico sulla presenza/geometria. Anche un inventario senza elementi
arancioni richiede controllo delle quote e delle altezze prima di verificare
interferenze meccaniche. Non sono introdotti margini di sicurezza impliciti.

## Pipeline

- Un solo riferimento superiore; le altre foto servono a quote e pinout, non
  vengono sommate come se contenessero altri componenti.
- Ritaglio PCB calibrato con dimensioni note. La foto deve avere lato lungo
  orizzontale ed essere quasi ortogonale: non viene rettificata la prospettiva.
  Inclinazioni significative vengono rifiutate; resta una piccola tolleranza
  di rasterizzazione. Una foto rifiutata non viene usata per calibrare le quote.
- Localizzazione dedicata: una vista completa e tre strisce sovrapposte. I grandi
  package restano nella vista completa; i piccoli sono assegnati per centro alla
  propria zona, evitando il conteggio ripetuto tra strisce.
- Associazione geometrica uno-a-uno tra viste: IoU oppure contenimento con limiti
  su rapporto delle aree e distanza dei centri. Un piccolo passivo non viene
  assorbito da un grande IC solo perché gli sta vicino. L'assegnazione considera
  l'intera vista per evitare duplicati causati dall'ordine dei rilevamenti.
- Verifica della sola presenza di corpi montati, in gruppi di massimo 24.
  Piazzole vuote/vie/scritte possono essere escluse; non vengono inventate nuove
  coordinate. In caso di errore la geometria disponibile resta come dubbia.
- Conversione pixel → mm deterministica, origine al centro, X destra e Y basso.
  I volumi comprendono l'ingombro visibile delle terminazioni.

Il modello fotografico predefinito è `google/gemini-3.1-pro-preview`, modificabile
nelle impostazioni LLM. Il modello datasheet scelto dall'utente viene usato anche
per la verifica di presenza (nella prova: `anthropic/claude-sonnet-5`). Non si cambia
il modello datasheet già configurato. Il percorso standard con 42 candidati usa
4 chiamate di localizzazione e 2 di presenza, oltre alla prima estrazione generale.

Le risposte vengono salvate in `generated/photo-cache`, senza chiave API e senza
duplicare i dati immagine. La chiave include modello, schema, prompt, parametri e
pixel; una ripetizione identica riusa la cache. Risposte incomplete non sono
trattate come successi riutilizzabili. La cache può contenere informazioni estratte
dai documenti: resta locale e ignorata da Git. Per una nuova inferenza degli stessi
input si può rimuovere esplicitamente la cache locale.

Anche il fallback dall'estrazione globale conserva ingombri generici senza
marcature e segnala le altezze come stimate. Quote opzionali non valide vengono
scartate e segnalate; un lato malformato non elimina gli altri lati validi.
Nuove estrazioni e correzioni applicate invalidano l'anteprima precedente:
completamenti CAD riferiti ai dati precedenti non la ripristinano. I controlli
CAD di bordo e sovrapposizione supportano anche rotazioni non multiple di 90°.

## Prova sull'immagine fornita

Immagine `Screenshot_1.png`, PCB quotata 42.8 × 15.2 mm, ritaglio
`[315,117,1435,519]` dell'originale 1597 × 576 pixel. Il riferimento contiene 42
corpi annotati separatamente dall'assistente guardando l'originale; **non è una
BOM o una validazione metrologica umana**. Non entra in prompt o codice produttivo.

Con associazione uno-a-uno e IoU ≥ 0.5:

| Risultato | Rettangoli | Corrispondenze | Precisione / richiamo |
|---|---:|---:|---:|
| Precedente prova Claude | 71 | 7 / 42 | 9.9% / 16.7% |
| Nuovo percorso a ingombri | 42 | 36 / 42 | 85.7% / 85.7% |

Errore medio dei centri sui 36 abbinamenti: **0.159 mm**. Nessuna coppia di
rettangoli finali risulta sospetta per sovrapposizione nel valutatore. Il numero
42 da solo non prova completezza: sei rettangoli non raggiungono la soglia di
sovrapposizione richiesta. La prova precedente non registrava l'origine del crop:
il confronto storico assume lo stesso ritaglio ricostruito. La nuova esportazione
registra crop, dimensioni e SHA-256 del file sorgente.

Il modello FreeCAD di esempio è stato realmente generato e renderizzato con
42 volumi, senza avvisi di generazione. Spessore PCB di esempio 1 mm e altezze
sono **stime**, non quote del produttore. I passi dei contatti, invece, sono
forniti esplicitamente dalla figura: sinistra 3 × 5.08 mm, destra 4 × 2.54 mm.

Le prove di aggancio automatico ai bordi OpenCV e di riclassificazione dettagliata
hanno mostrato regressioni: sono disponibili come strumenti sperimentali ma non
attive nel percorso standard. Il risultato va verificato su più moduli prima di
attribuirgli prestazioni generali o industriali.

## Riproduzione

```powershell
# Rete/API: prima esecuzione a pagamento, successive identiche dalla cache
.venv/Scripts/python.exe examples/inspect_photo_inventory.py C:/App/Screenshot_1.png --length 42.8 --width 15.2 --output generated/mechanical-envelope

# Offline, sullo stesso riferimento fotografico
.venv/Scripts/python.exe tools/evaluate_photo_inventory.py generated/mechanical-envelope/inventory.json --photo C:/App/Screenshot_1.png --output generated/mechanical-envelope/metrics.json

# Offline, richiede FreeCAD: STEP, FCStd, VRML e anteprime del modello reale
.venv/Scripts/python.exe examples/build_inventory_envelopes.py generated/mechanical-envelope/inventory.json --side left:3:5.08 --side right:4:2.54

.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
```

Gli artefatti locali sono in `generated/mechanical-envelope`: `inventory.json`,
`analysis.log`, `calibrated-photo.png`, `comparison.png` e la cartella `model`.
Il fixture `tests/fixtures/pcb_reference.json` è esclusivamente di valutazione.

## Riferimenti tecnici

- Il formato nativo di localizzazione `box_2d=[ymin,xmin,ymax,xmax]`, normalizzato
  0–1000, è documentato da [Google Image Understanding](https://ai.google.dev/gemini-api/docs/image-understanding).
- La separazione tra calibrazione, preparazione e ispezione è coerente con
  [Cognex Inspection Concepts](https://docs.cognex.com/apasap_431/web/EN/ConceptsGuide/Content/Topics/Concept/Apps/Inspection/Inspection%20Concept.htm).
  Qui si lavora comunque su foto di prodotto, non su un sistema ottico industriale
  calibrato e validato.
