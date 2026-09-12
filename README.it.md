# KiCadGen-Tool

Tool Python per generare automaticamente componenti KiCad completi
(impronta 2D `.kicad_mod` + modello 3D parametrico `.step`) a partire da
dimensioni/quote/immagini inseriti in una GUI.

## Struttura del progetto

```
KicadWizard/
  main.py                # entry point GUI: python main.py
  kicadgen/
    core/
      sexpr.py            # serializzatore S-expression generico (stile KiCad)
      footprint.py         # modello dati footprint (Pad, FpLine/Rect/Circle/Text) -> .kicad_mod
    generators/
      pin_header.py        # generatore parametrico: header/modulo THT RxC (+ pin_positions() condivisa)
      castellated_module.py # generatore per moduli con piazzole a mezzo foro (pad SMD per lato)
    cad3d/
      body_params.py        # Body3DParams: corpo scatolare + pin cilindrici
      freecad_build_body.py # script eseguito DENTRO freecadcmd (Part.export STEP/VRML)
      step_generator.py     # orchestrazione: trova freecadcmd, lancia il subprocess, valida l'output
    services/
      datasheet_extractor.py # estrazione parametri (2D + 3D) da datasheet via OpenRouter
      openrouter.py          # client OpenRouter: elenco modelli con prezzi + tool calling
      component_types.py     # tipologie di componente (THT/SMD, singolo/aggregato, castellated)
    gui/
      theme.py             # QSS Catppuccin Mocha (stessa palette di Pm3ToWav)
      preview_3d_vtk.py     # anteprima 3D interattiva (VTK/PyVista)
      app_config.py         # config.json: impostazioni globali persistenti
      settings_dialog.py    # dialog di configurazione (modello di default + prompt di sistema)
      model_dialog.py       # scelta del modello LLM con filtro per costo
      main_window.py        # QMainWindow con le 5 tab
  examples/
    generate_pin_header.py   # esempio da riga di comando: header THT 2x4 passo 2.54mm
  tests/
    test_footprint.py
  requirements.txt
```

## Stato attuale: modulo 2D (kicadgen.core)

Implementato e testato:

- **`sexpr.py`** — albero di nodi S-expression con serializzatore che
  replica la formattazione di KiCad 10 (indentazione a tab, distinzione
  tra stringhe vere `"F.SilkS"` sempre quotate e simboli/keyword bare
  `yes`/`no`/`solid`/`thru_hole` mai quotati — un errore comune che rompe
  l'import in pcbnew se non gestito).
- **`footprint.py`** — classi ad alto livello:
  - `Pad(number, pad_type, shape, at, size, layers, drill=None, roundrect_rratio=None, rotation=0)`
  - `FpLine`, `FpRect`, `FpCircle` (grafica silkscreen/fab/courtyard)
  - `FpText` (testo libero, oltre a Reference/Value generati automaticamente)
  - `Footprint(name, description, tags, attr)` con `add_pad()`, `add_graphic()`,
    `render()` (stringa) e `save(path)` (scrive il `.kicad_mod`, crea le
    cartelle `.pretty` mancanti)

Il formato di output è stato verificato per confronto diretto con un
footprint reale generato da KiCad 10 (`ESP32-S3-N16R8/ESP32_S3_N16R8.pretty`)
in `../ESP32-S3-N16R8/`: versione file (`20260206`), struttura property
Reference/Value/Datasheet/Description, e token bare vs stringhe quotate
combaciano esattamente.

### Provare il modulo core (da riga di comando)

```bash
cd KicadWizard
python examples/generate_pin_header.py
python -m pytest tests/ -q
```

Il file generato va in `examples/output/PinHeader_2x04_P2.54mm_THT.pretty/`
e puo' essere aperto direttamente nel Footprint Editor di KiCad
(o in una libreria `.pretty` puntata da KiCad) per verifica visiva.

## GUI (kicadgen.gui)

```bash
cd KicadWizard
pip install -r requirements.txt
python main.py
```

Cinque tab:

1. **📁 Progetto** — area di lavoro + nome del componente
   (il campo "Nome" vive qui: la cartella è
   `generated/NomeComponente/`, con la stessa struttura usata nel resto
   del repo, es. `ESP32-S3-N16R8/ESP32_S3_N16R8.pretty/`; il tab
   Footprint 2D ne mostra solo uno specchio in sola lettura). Gestione
   della documentazione: bottone "Aggiungi documentazione..." copia i
   file scelti (datasheet PDF, immagini, ecc.) in `NomeComponente/docs/`;
   lista con rimozione (richiede conferma) e apertura cartella.
   Prima dell'estrazione si dichiarano **tipologia del componente**
   (singolo/aggregato, THT/SMD/castellated) e **modello LLM** da usare.
   Bottone **"✨ Estrai parametri con AI dal datasheet"**: manda i file
   selezionati in lista a OpenRouter
   (`kicadgen.services.datasheet_extractor`) e pre-compila
   righe/colonne/passo/pad/drill nel tab Footprint 2D — vedi sezione
   "Estrazione da datasheet (AI)" sotto.
2. **▦ Footprint 2D** — parametri del generatore: nome, descrizione, tag,
   righe/colonne, passo (`pitch`, tra pin nella stessa riga), **distanza
   tra righe** (indipendente dal pitch — nei moduli DIP-style come i
   convertitori DC/DC e' quasi sempre molto maggiore, es. 10.16mm contro
   2.54mm), **pin popolati** (lista opzionale per pinout parziali, es.
   `1,7,8,9,10,16` su una matrice 2x8), diametro pad, drill. Sotto, il
   gruppo opzionale **"Modello 3D"** (checkbox per attivarlo): lunghezza/
   larghezza/altezza del corpo, offset X/Y del corpo rispetto al pin 1,
   lunghezza pin (protrusione 3D).
3. **⚙ Genera** — riepilogo parametri correnti, pulsante "Genera
   footprint" che lancia `kicadgen.generators.pin_header.build_pin_header()`
   in un `QThread` (`GenerateWorker`, stesso pattern di `ConvertWorker`
   in `Pm3ToWav/main.py`); se il gruppo "Modello 3D" e' attivo, lo stesso
   worker chiama poi `kicadgen.cad3d.step_generator.generate_3d_model()`
   (freecadcmd headless) per lo `.step` — un fallimento della generazione
   3D non invalida il footprint 2D gia' scritto, viene solo loggato a
   parte. Log esito/errori per entrambi. Pulsante **"Esporta in
   libreria..."**: copia la cartella del componente dall'area di lavoro
   (`generated/`) alla libreria scelta — la generazione resta sempre
   locale, l'export è un passo separato ed esplicito così la libreria
   vera riceve solo componenti già verificati.
4. **👁 Anteprima FootPrint** — disegno 2D a scala (silkscreen + pad) via
   `QGraphicsScene`, con quote complete in stile disegno tecnico: margini
   bordo→pin, span tra i pin estremi, passo, distanza righe e totali.
   Spunta **"Griglia"** per sovrapporre una griglia di riferimento, con
   passo selezionabile tra **1 mm** e **0.1 in (2.54 mm)**: con il passo
   in decimi di pollice i pad dei componenti THT standard cadono
   esattamente sulle intersezioni, quindi si verifica a colpo d'occhio
   l'allineamento. Linea sottile a ogni passo, marcata ogni 5. Entrambe
   le scelte (attiva/unità) sono ricordate in `config.json`.
   Ogni pad porta stampato il **numero del pin**; il pulsante **"Tabella
   pinout..."** apre una finestra con la corrispondenza numero → funzione
   estratta dalla documentazione (sta in una modale, non sotto il
   disegno, per lasciare al footprint quotato tutta l'altezza del tab).
   Se il documento elenca più configurazioni (es. colonne *Single* e
   *Dual*), l'alternativa finisce tra parentesi — es. il pin 8 del TEL8WI
   risulta `NC (Dual: Common)`.

### Posizione dei pin: mappa esplicita e bottom view

I pin header numerano colonna per colonna (1,2 poi 3,4...), ma i package
DIP-style numerano **a U** (1..N lungo una fila, poi a ritroso sull'altra):
usare la prima convenzione su un modulo DIP produce pad con i numeri
sbagliati, quindi collegamenti errati in KiCad. Per questo l'estrazione
legge dal disegno la **posizione esplicita di ogni pin** (`pin_map`), che
ha la precedenza sulla numerazione automatica.

C'è una seconda insidia: i datasheet dei moduli mostrano quasi sempre una
**BOTTOM VIEW**, mentre il footprint KiCad si disegna dal lato componenti.
Senza specchiare si otterrebbe un footprint speculare, con i fori in
posizioni che non coincidono col pezzo reale. Il modello riporta quindi
solo *cosa vede* (`pin_map` così com'è disegnata) più *da che lato*
(`pin_map_view`), e **la riflessione la fa il codice**
(`_pin_map_seen_from_top`): leggere è un compito percettivo, ribaltare è
una trasformazione geometrica esatta e va tenuta deterministica.

Copertura in `tests/test_pin_map.py`, sul layout reale del TEL8WI.
5. **🧊 Modello 3D** — anteprima 3D interattiva (VTK/PyVista,
   `kicadgen/gui/preview_3d_vtk.py`), ruotabile col mouse, in due
   modalità: *prima* della generazione mostra un'approssimazione a
   parallelepipedo costruita dai soli parametri (senza bisogno di
   FreeCAD); *dopo* la generazione mostra la **geometria reale** —
   FreeCAD esporta anche una mesh tassellata accanto allo `.step`
   (`<Nome>.preview.json`, un colore per pezzo), quindi fillet e
   serigrafie si vedono davvero. Sotto la vista, i pulsanti **Iso /
   Sopra / Sotto / Fronte / Retro / Sinistra / Destra** orientano la
   camera sulle viste standard e **Centra** re-inquadra il pezzo.
   In alto a sinistra la spunta **"Numeri pin (vista da sotto)"**
   etichetta ogni piedino col suo numero, ancorato alla punta. Le
   etichette compaiono **solo guardando dal lato dei pin** (camera sotto
   il piano Z=0): dall'alto il corpo li copre e farebbero solo
   confusione. Funziona sia coi pulsanti di vista sia ruotando col mouse.
   Con la spunta attiva compare anche il pulsante **"Tabella pinout..."**,
   per sapere a quale funzione corrisponde il numero letto sul piedino.
   Guardando il modello da sotto si ritrova esattamente la bottom view
   del datasheet: è una verifica pratica che la specchiatura sia andata
   a buon fine.
   Il tab è dentro una `QScrollArea`: il contenuto supera in altezza la
   finestra e senza scorrimento i pulsanti in fondo restavano tagliati.
   Sotto, il gruppo **"Annotazioni"** per aggiungere
   fillet su spigoli e testo inciso su facce, con tabella di revisione
   prima di generare — vedi sezione "Annotazioni (fillet + testo
   inciso)" sotto. Il pulsante **"Rigenera modello 3D (STEP)"** rifà solo
   il solido con le annotazioni correnti (`Cad3DWorker`), senza riscrivere
   il footprint: è il modo normale di iterare sulle annotazioni.

### Menu

- **File** — apri cartella componente, esporta in libreria, esci.
- **Configurazione** — *Modello e prompt di sistema LLM...* apre
  l'editor del **modello di default** (scelto dall'elenco OpenRouter,
  filtrabile per costo) e delle istruzioni globali inviate al modello
  (salvate in `config.json`, che è in `.gitignore`); *Genera anche i pin
  NC* (vedi sotto); *Apri config.json* per modificarlo a mano.
- **?** — cartella del progetto tool, About.

### Due livelli di istruzioni per l'LLM

- **Globale** (menu Configurazione) — il system prompt, vale per ogni
  componente e persiste tra le sessioni. Si usa per correggere
  comportamenti sistematici del modello, es. una quota letta dalla vista
  sbagliata del disegno.
- **Per componente** (prompt specifico nel tab Progetto, sotto il
  pulsante di estrazione) — il blocco della **tipologia** selezionata
  (inserito automaticamente e modificabile) più le istruzioni sul
  singolo pezzo, es. "il case è metallico" o "ignora il connettore
  laterale". Vengono accodate al messaggio con i documenti e **hanno la
  precedenza** se contraddicono quanto il modello deduce dai file.

Lo **schema dei campi** restituiti resta fisso e non editabile da nessuno
dei due — è quello che garantisce risposte sempre in JSON strutturato
valido.

### Pin NC (Not Connected)

Opzione **Configurazione → "Genera anche i pin NC"**, attiva di default.
Un pin marcato NC nel pinout non è collegato elettricamente, ma il
piedino **è fisicamente presente**: senza il suo pad il footprint non
avrebbe il foro e il componente non entrerebbe nella scheda. Disattiva
l'opzione solo se sai che quelle posizioni sono davvero vuote.

Verificato sul pinout del TEL8WI (pin 7 e 8 marcati NC): con l'opzione
attiva l'estrazione restituisce `[1, 7, 8, 9, 10, 16]`, disattivandola
`[1, 8, 9, 10, 16]`.

**Stile grafico**: stessa identità visiva di `Pm3ToWav`
(`C:\Lavori\Python\Pm3ToWav`, PySide6) — tema scuro Catppuccin Mocha
applicato a tutta l'app (non solo ai tab) in `kicadgen/gui/theme.py`
(`APP_QSS`), con la stessa palette esadecimale (`#1e1e2e`, `#313244`,
`#45475a`, `#cdd6f4`, `#89b4fa`) e lo stesso QSS per i tab (bordo
inferiore blu sulla tab attiva) di `_apply_tab_style()` in
`Pm3ToWav/main.py`. Framework diverso (PyQt6 qui, PySide6 la), stessa
QSS: e' Qt, la sintassi dei fogli di stile e' identica.

## Estrazione da datasheet (AI)

Usa un modello multimodale via **OpenRouter** (legge PDF e immagini di
disegni meccanici quotati) per estrarre i parametri del pin header da un
datasheet, tramite tool calling forzato (`report_pin_header_params`) cosi'
la risposta e' sempre JSON strutturato valido, mai testo da fare il
parsing a mano. Implementato in
`kicadgen/services/datasheet_extractor.py`, chiamato dalla GUI in un
`QThread` (`ExtractWorker`, stesso pattern degli altri worker).

Setup della API key (una delle due):

1. **File `.env`** (consigliato): copia `.env.example` in `.env` nella
   cartella del progetto e inserisci `OPENROUTER_API_KEY=sk-or-v1-...`.
   Il file `.env` e' in `.gitignore`, non finisce mai in git. Viene
   caricato automaticamente da `main.py` (richiede `python-dotenv`,
   gia' in `requirements.txt`).
2. **Variabile d'ambiente di sistema**: se preferisci non usare `.env`,
   imposta `OPENROUTER_API_KEY` nell'ambiente prima di lanciare
   `python main.py`.

Con OpenRouter una sola chiave da' accesso ai modelli di tutti i
fornitori (Anthropic, OpenAI, Google, ...): le chiamate HTTP usano solo
la stdlib, non serve alcun SDK.

### Scelta del modello

**Configurazione -> Modello e prompt di sistema LLM** contiene le due
impostazioni globali: il **modello di default** (salvato in
`config.json` come `model`) e il prompt di sistema. Il modello si
sceglie da un elenco scaricato da OpenRouter, con **ricerca e filtro sul
costo massimo di input** ($/milione di token) e, di default, solo i
modelli realmente utilizzabili (tool calling + immagini + PDF). L'elenco
e' messo in cache un giorno in `.openrouter_models.json` (ignorato da
git); "Aggiorna elenco" lo riscarica.

Nel tab **Progetto** la tendina "Modello LLM" mostra il default; il
pulsante **"Modello diverso..."** permette di usare un modello piu'
capace **solo per questa sessione** (datasheet difficile), senza toccare
il default globale: alla chiusura dell'app la scelta si perde.

### Tipologia di componente

Prima di lanciare l'estrazione si dichiara nel tab **Progetto** che cosa
si sta leggendo, perche' da questo dipendono le quote che il modello
deve cercare:

- **Componente singolo a fori passanti (THT)** — pad con foro, quote da
  "Recommended PCB layout";
- **Componente singolo SMD** — nessun foro, dimensioni dal
  "Recommended Land Pattern";
- **Componente aggregato a fori passanti** — moduli/schede DIP-style,
  numerazione a U, bottom view, `row_spacing` molto maggiore del pitch;
- **Componente aggregato con piazzole a mezzo foro (castellated
  holes)** — moduli tipo ESP32: semi-fori metallizzati sul bordo,
  saldati come SMD.

Il testo della tipologia scelta viene inserito nel **prompt specifico
del componente** (la casella di testo sotto il pulsante di estrazione),
delimitato da marcatori: resta **liberamente modificabile** e cambiando
tipologia si riscrive solo quel blocco, lasciando intatte le note
aggiunte a mano (definizioni in
`kicadgen/services/component_types.py`).

> Nota: per i componenti **SMD singoli** la tipologia guida ancora solo la
> lettura del datasheet (le quote del land pattern finiscono nelle note,
> con `drill = 0`): la generazione dei relativi pad SMD non e' implementata.
> I moduli **castellated** hanno invece il loro generatore, descritto qui
> sotto.

## Moduli castellated (piazzole a mezzo foro)

Un modulo su PCB con semi-fori metallizzati sul bordo non entra in una
matrice righe x colonne: ogni lato ha il suo numero di contatti e il suo
passo. Un convertitore RS485/TTL isolato da 42.8 x 15.2 mm, per esempio,
ha 3 contatti a 5.08 mm sul lato sinistro e 5 a 2.54 mm su quello destro;
forzandolo nella matrice si ottengono pin fuori posto e pad THT dove
servono piazzole SMD.

Per questi componenti c'e' `kicadgen/generators/castellated_module.py`,
che descrive il modulo **lato per lato**:

- origine del footprint al **centro del corpo** (convenzione SMD KiCad),
  X = lunghezza, Y = larghezza con Y verso il basso;
- ogni lato (`left`/`right`/`top`/`bottom`) ha numero di contatti, passo,
  offset lungo il bordo e numerazione propri (sequenziale, invertita o
  esplicita);
- pad rettangolari su `F.Cu`/`F.Paste`/`F.Mask`, **senza drill**,
  sporgenti di `pad_overhang` oltre il bordo del modulo perche' il menisco
  di saldatura resti ispezionabile;
- contorno del corpo su `F.SilkS`/`F.Fab` e courtyard che comprende i pad
  sporgenti.

Nella GUI si usa il gruppo **"Modulo castellated"** del tab Footprint 2D:
e' alternativo alla matrice (attivandolo il gruppo a matrice si disabilita)
e contiene le quote del corpo, quelle dei pad e la tabella dei lati.
Selezionando la tipologia "aggregato con piazzole a mezzo foro" nel tab
Progetto il gruppo si attiva da solo, e l'estrazione AI (layout
`castellated`) lo riempie con i lati letti dal datasheet.

La validazione rifiuta i casi che darebbero un footprint inutilizzabile:
pad sovrapposti (passo minore della larghezza pad), pad che non entrano
nel bordo, numeri di pin duplicati tra lati.

### Modello 3D del modulo

Il 3D di questi moduli non e' il corpo con pin cilindrici del pin header:
un modulo e' un **PCB sottile con sopra i componenti**, e i contatti sono
semi-fori sul bordo, non piedini che sporgono. `generate_castellated_3d_model()`
costruisce quindi due solidi:

- il **PCB**, spesso `pcb_thickness` (campo "Spessore PCB"), con i
  **semi-fori tagliati** sul bordo in corrispondenza di ogni contatto
  (diametro dal campo "Diametro semi-foro");
- il **blocco dei componenti**, dal PCB fino all'altezza totale del modulo
  (campo "Altezza corpo" del gruppo Modello 3D, es. 4.75mm), rientrato di
  `component_inset` dal bordo, dove i semi-fori non lasciano spazio.

Con il gruppo castellated attivo, i campi lunghezza/larghezza/offset e
lunghezza pin del gruppo "Modello 3D" si disabilitano: vengono dal modulo,
cosi' il solido non puo' divergere dal footprint. Gli eventuali fillet
letti dalla foto si applicano agli spigoli del PCB.

Limiti applicativi (in `datasheet_extractor.py`): max 5 file per
chiamata, max 20 MB per file, formati supportati PDF/PNG/JPG/WEBP/GIF.
La risposta include sempre un livello di confidenza e note testuali
sulle assunzioni fatte — **controlla sempre i valori pre-compilati nel
tab "Footprint 2D" prima di generare**, l'estrazione può sbagliare.

Se il datasheet include un disegno meccanico con l'ingombro del corpo
(sezione tipo "Outline Dimensions"), l'estrazione compila anche i campi
3D (`body_length_mm`, `body_width_mm`, `body_height_mm`,
`body_offset_x_mm`, `body_offset_y_mm`, `pin_length_3d_mm`) e attiva
automaticamente il gruppo "Modello 3D" nel tab Footprint 2D.

Se tra i file selezionati c'è anche una **foto reale del prodotto** (non
il disegno tecnico) con spigoli visibilmente arrotondati, l'estrazione
compila `edge_fillets` e le righe compaiono già pronte nella tabella
annotazioni del tab "Modello 3D" (restano modificabili/rimovibili). Il
raggio è una stima visiva: le foto prodotto non riportano mai un raggio
quotato.

**Selezione file**: seleziona in lista *tutti* i file da usare
(Ctrl+click per la selezione multipla) — datasheet **e** foto. Con il
solo disegno meccanico il modello non può valutare gli smussi e omette
`edge_fillets`.

**Rinomina automatica**: l'estrazione propone anche il nome del
componente, che determina la cartella. Poiché `docs/` vive dentro quella
cartella, alla rinomina la GUI chiede conferma e **sposta i documenti**
nella nuova cartella — senza questo passaggio i file caricati prima
resterebbero nella vecchia posizione e sparirebbero dalla lista
(`tests/test_docs_migration.py` copre il caso).

## Generatore 3D (kicadgen.cad3d)

Modello geometrico volutamente semplice: un parallelepipedo per il corpo
(ingombro meccanico) più un cilindro per ogni pin THT effettivamente
popolato, con la stessa geometria del footprint 2D
(`kicadgen.generators.pin_header.pin_positions()`, unica fonte di verità
per entrambi). Il piano Z=0 è il piano di saldatura: i pin si estendono
da `Z=-pin_length_mm` a `Z=0`, il corpo da `Z=0` a `Z=height_mm`.

- **`freecad_build_body.py`** gira *dentro* l'interprete Python embedded
  di FreeCAD (non è importabile da codice normale): legge un JSON con
  corpo+pin ed esporta `.step` (sempre) e `.wrl` (best-effort, un
  fallimento VRML non blocca lo STEP).
- **`step_generator.generate_3d_model()`** trova `freecadcmd`
  (variabile d'ambiente `FREECAD_CMD_PATH`, altrimenti cerca sotto
  `Program Files\FreeCAD*\bin\freecadcmd.exe` su Windows), lancia il
  subprocess, verifica che i file di output esistano davvero e solleva
  `Cad3DError` con l'output di freecadcmd se qualcosa va storto.
- Output in `<cartella_componente>/<Nome>.3dshapes/<Nome>.step` — stessa
  convenzione `.3dshapes` già usata nel resto del repo (es.
  `ESP32-S3-N16R8/ESP32_S3_N16R8.3dshapes/`).

### Annotazioni (fillet + testo inciso)

Il corpo è sempre un parallelepipedo assi-allineato, quindi facce e
spigoli hanno nomi fissi e prevedibili (niente picker 3D, fragile da
implementare in modo affidabile sull'anteprima matplotlib):

- **Facce (6)**: `top`, `bottom`, `left`, `right`, `front`, `back`.
- **Spigoli (12)**: combinazione di due facce adiacenti, es. `top-front`,
  `front-left` (vedi `kicadgen/cad3d/annotations.py` per l'elenco
  completo).

Nel tab **👁 Anteprima**, sotto l'anteprima 3D, un gruppo "Annotazioni"
permette di aggiungere:

- **Fillet** — raccordo con raggio su uno spigolo (`EdgeFilletAnnotation`).
- **Serigrafia** — scritta su una faccia (`FaceTextAnnotation`): non è
  un'incisione ma uno strato sottile di "vernice" (0.06mm di default)
  appoggiato sopra la superficie, come la serigrafia stampata sui case
  reali. Generata con `Part.makeWireString` (font di sistema, default
  `arial.ttf`) ed estrusa; resta un **solido separato dal corpo** proprio
  perché così può avere un colore proprio nell'export.

### Colori

`Body3DParams.color_hex` / `.pin_color_hex` e `FaceTextAnnotation.color_hex`
danno colore a case, pin e serigrafie. Se tra i file dell'estrazione c'è una
foto del prodotto, l'AI ricava `body_color_hex` e `text_color_hex` e i campi
si pre-compilano da soli.

I colori finiscono nel **`.wrl`** (scritto direttamente da
`freecad_build_body.py`, uno `Shape` con `Material` per pezzo): `Mesh.export()`
di FreeCAD scriverebbe tutto in un grigio unico, perché in headless non
esistono i `ViewObject` da cui l'export prende l'aspetto. Le coordinate del
VRML sono in **unità da 0.1 pollici** come da convenzione KiCad (mm / 2.54),
mentre lo `.step` resta in millimetri.

Le annotazioni finiscono in una tabella di revisione (tipo/target/dettagli,
con rimozione) prima di premere "Genera": vengono applicate dallo script
FreeCAD (`_apply_fillets`/`_apply_face_text` in
`freecad_build_body.py`), che classifica geometricamente facce e spigoli
del solido a runtime (confronto centro di massa/punto medio con i bordi
noti del box) — nessun ID salvato, funziona anche se il corpo cambia
dimensioni tra una generazione e l'altra. I fillet vengono applicati uno
alla volta (la geometria degli spigoli cambia dopo ogni fillet), poi il
testo viene inciso sulle facce risultanti.

**Nota tecnica**: il file JSON temporaneo passato a `freecadcmd` usa
l'estensione `.kicadgen_params` invece di `.json` — FreeCAD registra
`.json` come formato importabile (mesh FEM) e tenterebbe di aprirlo come
documento dopo l'esecuzione dello script, producendo un'eccezione
rumorosa (innocua, il file veniva comunque scritto correttamente, ma
l'output era da controllare più attentamente).

Testato end-to-end con l'API reale su un datasheet vero (convertitore
DC/DC TRACO TEL8WI, corpo 23.8×13.7×8.0mm, 6 pin popolati su 16
posizioni) — vedi `tests/test_cad3d.py` per il test di integrazione
(skippato automaticamente se FreeCAD non è installato).

## Prossimi moduli (non ancora implementati)

1. **Altri generatori 2D** (`kicadgen/generators/`) — SMD (roundrect),
   connettori a passo non standard, import immagine con Pillow/OpenCV
   per calibrazione scala/riferimento.
2. **Body 3D non rettangolari** — al momento `cad3d` genera solo un
   parallelepipedo; forme più complesse (cilindriche, con smussi,
   multi-corpo) richiederebbero un modello geometrico più ricco in
   `Body3DParams` e script FreeCAD dedicati.

## Note tecniche sul formato `.kicad_mod`

- Versione file: `(version 20260206)` — corrisponde al formato KiCad 10.x
  osservato nei footprint esistenti in questo repo. Da aggiornare se si
  genera per una versione KiCad diversa.
- Layer THT tipici: `["*.Cu", "*.Mask"]`; layer SMD tipici:
  `["F.Cu", "F.Paste", "F.Mask"]` (o `B.*` per il retro).
- `pad_type` ammessi: `thru_hole`, `smd`, `np_thru_hole`.
- `shape` ammesse: `circle`, `rect`, `oval`, `roundrect` (+ `trapezoid`,
  `custom` dichiarate ma non ancora gestite con geometria dedicata).
