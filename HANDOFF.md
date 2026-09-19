# Handoff — sessione 2026-09-19

Branch **`FIX-WITH_FREECAD`** (creato da `FIX` a `801dd5f`), allineato col remoto.
Suite: **1225 passed, 2 xfailed**. Albero pulito.

`FIX` resta fermo a `801dd5f` come punto di ritorno.

---

## 0. Cambio di approccio in corso: modelli 3D reali di KiCad

**Idea dell'utente, condivisa.** Smettere di approssimare i package a partire
dal bounding box e usare i modelli veri che KiCad installa sul disco.

Non e' un'API: non c'e' nessun servizio da interrogare. Sono file locali --
su questa macchina `C:/Program Files/KiCad.0\share\kicaddmodels`,
105 cartelle, **7251 modelli** STEP/WRL. Niente rete, niente costo per pezzo.

Cosa risolve: il vocabolario smette di essere la lista di 16 `kind` inventata
nel progetto e diventa la nomenclatura standard dell'industria (`SOT-223`,
`0402`, `USB_C_Receptacle`), che un modello linguistico conosce molto meglio.
Era il collo di bottiglia vero -- per il regolatore il modello scriveva
"voltage regulator" ma doveva ripiegare su `kind=ic`, che si disegna come
scatola liscia *per scelta esplicita* del codice.

### Fatto: `kicadgen/cad3d/kicad_models.py` (commit `e7cad70` e seguenti)

Risolutore autonomo, **non ancora agganciato alla generazione**. Verificato
con `grep`: nessun chiamante, `build_component` usa ancora `BUILDERS`.

- `find_library()` — trova la libreria, assenza non e' errore
- `candidates(package)` — ricerca per nome normalizzato, piu' attinenti prima
- `measure_models()` — misura con FreeCAD **in un solo processo per lotto**,
  indice su disco. I percorsi passano da un file di scambio perche'
  `freecadcmd` **non inoltra argv** allo script come farebbe python.
- `resolve_model()` — fra i candidati vince **il piu' vicino alla misura**,
  non il primo che rientra. La tolleranza (0.30) e' **derivata, non tarata**:
  fra due taglie adiacenti corre almeno il 60%, quindi perche' la scelta per
  vicinanza sbagli servirebbe un errore di misura oltre il 30%.

### Il problema che ha fatto perdere piu' tempo, e come e' stato risolto

Il modello KiCad di un SOT-223 ha bounding box **7.00 x 6.50** perche'
comprende i reofori aperti; la fotografia aveva misurato **6.55 x 3.21**,
cioe' il corpo. Scarto 102%, package giusto rifiutato.

Soluzione: misurare **due letture** per ogni modello e accettare la piu'
vicina. Il corpo si ottiene con una sezione **al 65% dell'altezza**, non a
meta': i reofori stanno bassi e a meta' rientrano ancora nella sezione.
Verificato -- al 65% un SOT-223 da' 3.47 x 6.47 (corpo reale 3.5 x 6.5) e un
SOIC-8 da' 3.75 x 4.75 (3.9 x 4.9); al 50% davano 5.25 e 5.02.

### Risultati sui pezzi reali della ESP32

    SOT-223           6.55 x 3.21  ->  SOT-223-8.step        scarto  7%
    USB_C_Receptacle  7.61 x 9.98  ->  USB_C_Receptacle_GCT  scarto 10%
    ESP32-S3-WROOM-1 20.01 x 17.97 ->  ESP32-S3-WROOM-1.step scarto 12%
    R_0402            1.00 x 0.50  ->  R_0402_1005Metric     scarto  0%
    R_0402 con quote di un 0603    ->  RIFIUTATO (38%)
    U.FL              4.57 x 4.05  ->  RIFIUTATO (32%)

Le due righe di rifiuto sono la prova che la discriminazione funziona.

### Aperto su questo fronte

1. **`SOT-223` risolve a `SOT-223-8.step`**, che e' un altro componente (otto
   reofori invece di quattro). Hanno lo stesso corpo, quindi la misura non
   puo' distinguerli: serve preferire la corrispondenza **esatta di nome**
   quando gli scarti sono comparabili, invece del solo piu' vicino.
2. **Chiedere il nome del package al modello.** Oggi `resolve_model` riceve il
   nome a mano nei test; in produzione non esiste -- `kind=ic` non e' un
   package. Serve un campo aperto nello schema del classificatore, come si e'
   fatto per `mating`.
3. **Agganciare alla generazione**: caricare lo STEP invece della forma
   sintetica, con le forme attuali come ripiego. `freecad_parts` gira dentro
   FreeCAD e puo' fare `Part.read`; la risoluzione va fatta fuori e il
   percorso passato nel dizionario del componente.
4. **U.FL**: la misura fotografica (4.57) e' lontana dal pezzo vero (3.10),
   probabilmente perche' il rettangolo ha incluso la nicchia attorno. Va
   guardato dopo, non forzando la tolleranza.

**Le forme sintetiche non si buttano**: diventano il ripiego, che e' il loro
posto naturale.

---

## 1. Il filo conduttore della sessione

Il problema di partenza era: *l'inventario fotografico non trovava i componenti*.
La catena di cause, scoperta a strati, è stata:

1. **`find_board` scartava la vista superiore** per una soglia di deriva in pixel
   assoluta invece che proporzionale → risolto (`1f703cd`).
2. **Accettava in silenzio un falso positivo** da 119×44 px su una foto da 4 Mpx
   → risolto con soglie di risoluzione minima e frazione minima d'immagine.
3. **L'inventario era limitato dai pixel, non dall'algoritmo**: a 12.2 px/mm un
   0402 misura 6 px → aggiunto avviso esplicito (`_warn_if_too_coarse`).
4. **Serviva una foto vera ad alta risoluzione** → aggiunto il quarto ruolo
   `hires` (`7815b22`).
5. **La foto vera veniva comunque scartata**: `find_board` cercava "il rettangolo
   scuro più grande", ma su una fotografia lo schermo metallico dell'ESP32 sta a
   201 di grigio come lo sfondo (204), e un'ombra sul piano sta a 73 come il PCB
   (49). Il blob risultante non era un rettangolo e il suo angolo era privo di
   senso → aggiunta segmentazione **per tessitura** come ripiego (`1ac342e`).

**Risultato finale**: `hires.jpg` calibra a **55.1 px/mm** contro i 12.2 del
disegno quotato. Un 0402 passa da 6 a 27 px sul lato corto.

### Lezione da non perdere
Due difetti erano **fallimenti silenziosi**: il falso positivo e il raccordo OCC
che ripiegava sullo spigolo vivo. Entrambi producevano un risultato sbagliato
senza dirlo. Ogni volta che un filtro scarta o un'operazione geometrica fallisce,
va detto — è il motivo per cui ora esistono `_warn_if_too_coarse`,
`_fillet_best` e gli indicatori ✓/⚠/✗ sulle card.

---

## 2. Cosa è stato fatto, per area

### Costi e credito OpenRouter (`fbc5419`)
- `check_credit()` prima di ogni estrazione: interroga `/credits` e `/auth/key` e
  applica **il minimo dei due** (sono vincoli indipendenti: l'account può avere
  11.92 USD e la chiave un tetto residuo di 13.73).
- Sotto 0.50 USD blocca **senza effettuare alcuna chiamata**; sotto 2.00 avvisa;
  se il controllo stesso fallisce **prosegue** — un controllo di cortesia non
  deve rendere l'app meno utilizzabile di quando non c'era.
- `UsageTotals` accumula il costo **reale** (`usage.include` → `usage.cost`), non
  una stima sui listini. Conta anche le chiamate fallite (sono state pagate) e
  dichiara il totale **sottostimato** se una risposta non riporta il costo.

### Rilevamento scheda (`1f703cd`, `1ac342e`)
- Soglia di deriva proporzionale al lato lungo, non assoluta.
- Filtri anti falso positivo: `MIN_BOARD_SCALE_PX_PER_MM`, `MIN_BOARD_IMAGE_FRACTION`.
- Doppia segmentazione: `_board_mask` (luminosità, esatta sui disegni) e
  `_texture_mask` (gradiente, per le fotografie). **La seconda è un ripiego**, non
  il criterio principale.
- `deskew_board` raddrizza fino a 20°; oltre è prospettiva e una rotazione rigida
  non la correggerebbe. `find_board` da solo continua a rifiutare ogni
  inclinazione: è `prepare_photo` a raddrizzare prima di richiamarlo.

### Forme 3D (`d465d7b`, `7815b22`)
- Connettore: asola a stadio, guscio con fondo piatto e cielo stondato, linguetta
  dei contatti, due bugne. La linguetta arriva **solo se la bocca è larga e
  bassa** — regola sulla forma misurata, non sul nome.
- Il verso della bocca **compensa la rotazione del chiamante**: deciso dalla sola
  posizione globale, a 180° la bocca si ribaltava verso l'interno della scheda.
- `_fillet_best` riprova a raggio minore: il raggio pari a metà del lato corto è
  il caso degenere per OCC e produceva un foro squadrato in silenzio.
- Nuovo kind `switch` (tactile): il modello li riconosceva già come
  `"label": "tactile switch"` ma ripiegava su `generic` perché il kind non
  esisteva. Un test blocca l'allineamento fra le **tre copie** della lista dei kind.

### Documenti e ruoli (`7815b22`, `1dccb42`)
- Quarto ruolo `hires` ("Foto ad alta risoluzione"). `INVENTORY_ROLES =
  ('hires', 'top')` è una **preferenza, non un'esclusiva**: si passano tutte le
  viste dall'alto e vince la più dettagliata *fra quelle calibrabili*.
- Rinominato `top` in "Immagine con quote" — **solo l'etichetta**, la chiave
  interna resta `top` perché i progetti esistenti hanno `top.png` nei manifesti.
- I dialoghi ricordano l'ultima cartella sorgente (non quella del progetto, che è
  la destinazione delle copie).
- Indicatore ✓/⚠/✗ sotto ogni card con i px/mm; spiegazione nel tooltip; cache su
  file e quote (la verifica costa ~150 ms su 4 Mpx).

### Interruzioni rimosse (`72d0d3e`, `2e55830`)
- La rinomina avviene **da sola** (scelta dell'utente fra le alternative).
- Il conflitto di nomi è un **avviso nel Log AI**, non una modale: non c'è nulla
  da decidere e nulla è stato toccato.
- La protezione contro il ciclo di eventi annidato **resta** e il suo test non
  dipende più da quale modale sopravvive.

### Costo visibile (`b287057`, `2e55830`)
- Il costo compare **dentro la modale**, in riga propria sotto il tempo trascorso
  — non nello stato, che i messaggi della generazione sovrascriverebbero.
- Si azzera all'inizio di una **nuova analisi**, non quando la modale riparte per
  FreeCAD: quello lo faceva sparire una frazione di secondo dopo averlo mostrato.

---

## 3. Aperto — richiesto dall'utente, non ancora fatto

### 3.1 Connettore antenna (U.FL / IPEX) — RISOLTO, ma con una euristica

**Correzione a quanto avevo scritto prima**: il modello *lo identifica*. La
ricerca per parole chiave (`u.fl`, `ipex`, `antenna`) era il metodo sbagliato:
l'etichetta e' il generico `"connector"`, e nei crop di dettaglio le coordinate
sono relative al ritaglio, non alla scheda.

Cercando **per posizione**, nella corsa delle 11:51, `Dettaglio 3/3`:

    kind=connector  label=connector  crop x=70%  ->  scheda x=90%

e nel modello finale `Cmp42` a (+23.76, -6.01), 4.57 x 4.05 x 2.35 mm.

Il difetto era la **forma**: `_connector` gli dava una bocca laterale, mentre un
U.FL si innesta dall'alto. Aggiunto `_coaxial_connector` (base squadrata +
barilotto metallico con cavita' centrale), scelto quando il connettore e'
piccolo e quasi quadrato.

**Lezione di metodo**: cercare per nome nelle rilevazioni e' inaffidabile. Il
modello etichetta genericamente, e le coordinate dei crop sono locali. Cercare
per **posizione nel modello finale** (`preview.json`) e' il metodo giusto.

### 3.2 Nomi dei pin sulla serigrafia superiore
L'utente vuole evitarli: su una 2×22 castellated i nomi lunghi (`GPIO38`,
`GPIO37`…) si sovrappongono fra loro e vanno sopra i componenti.

**Dove nascono**: `kicadgen/cad3d/step_generator.py:285`, da
`params.silk_labels`, con `size_mm: 0.7` fisso e `label_gap = radius + 0.55`.

**Attenzione a non confondersi**: esiste già `show_pin_numbers_3d`
(`app_config.py:70` → `main_window.py:2977`) ma controlla
`preview_3d.show_pin_numbers`, cioè l'**overlay della preview VTK**, non il testo
serigrafico inciso nello STEP. Sono due cose diverse.

Opzioni: opzione di generazione per ometterli, oppure emetterli solo quando c'è
spazio (passo vs lunghezza del testo).

---

## 4. Aperto — segnalato da me, mai autorizzato

### 4.1 `build_component` risolleva nel fallback *(preesistente, non mio)*
`kicadgen/cad3d/freecad_parts.py`, blocco `except` finale: intercetta l'eccezione
e ripiega su `_generic`, ma `_generic` chiama `_box` con le **stesse quote non
valide** e risolleva **dentro il gestore**. Con `length_mm ≤ 0` fallisce l'intera
generazione 3D invece di semplificare il componente.

Raggiungibilità: il decode AI scarta già le quote ≤ 0, quindi la via realistica è
la modifica manuale nella tabella componenti, dove non ho trovato un controllo di
positività. Severità bassa, fix di una riga.

### 4.2 Contorno del footprint ESP32
`fp_rect` su F.SilkS da (-1,-1) a (54.34, 23.86) = **55.34 × 24.86 mm**, mentre la
scheda è **63.30 × 25.40**. È il bounding box dei pad, non il corpo: si perdono
gli offset asimmetrici 5.05 / 4.91 e il modulo sporge ~4 mm per lato senza che
nulla lo indichi. Manca anche il **courtyard** (`F.CrtYd`): il generatore
castellated ne emette uno, `pin_header.py` no.

### 4.3 Sovrapposizioni residue nell'inventario
Risolti i doppioni *fra viste diverse* (tetto rapporto aree 2.5 → 3.5, **soglia
empirica** tarata su un caso osservato). Restano:
- **doppioni nella stessa vista**: l'associazione per progetto lavora solo fra
  viste diverse ("due pezzi della stessa vista restano distinti"). Toccare quella
  regola ha rotto 7 test al primo tentativo — serve un intervento pensato.
- **sovrapposizioni 6–14%**: sono pezzi veri, unirli li cancellerebbe.

---

## 5. Cose da sapere prima di toccare il codice

- **Riavvio**: `freecad_parts.py` gira dentro `freecadcmd` come sottoprocesso e
  **non** richiede riavvio della GUI. Tutto il resto (`main_window`,
  `guided_documents`, `analysis_overlay`, `i18n`, i servizi) sì.
- **Traduzioni**: regola di progetto, IT/EN/FR/DE **complete**. Due test lo
  verificano (guida dei documenti e etichette dei ruoli). Le chiavi UI usano il
  prefisso `ui:<italiano>`.
- **`data/analysis_profiles.sqlite3`** è in Git e contiene il catalogo
  traduzioni. **Verificare l'assenza di credenziali prima di pubblicarlo** (fatto
  questa sessione: solo 32 righe di traduzione aggiunte).
- **La suite completa** va lanciata come `pytest tests`, non `pytest`: la seconda
  forma tenta di collezionare le cartelle sotto `generated/` e fallisce con
  PermissionError.
- **Test flaky noto**: `test_i18n.py::test_model_picker...` può fallire su
  `Cancel`/`Annulla` per ordine di esecuzione Qt. Verificato indipendente dalle
  modifiche.
- **Chiave OpenRouter in scadenza**: `2026-12-11`.

---

## 6. Decisioni prese dall'utente in questa sessione

- Quarto ruolo documenti: **sì** (supera la decisione del 2026-09-13 "solo tre
  ruoli", registrata in `MEMORY.md`).
- `docs/ESP32/` **fuori da Git** (5 MB di immagini di lavoro), aggiunto a
  `.gitignore`.
- Rinomina componente: **sempre automatica**, senza chiedere.
