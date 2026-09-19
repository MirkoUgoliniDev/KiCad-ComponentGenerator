# Handoff — sessione 2026-09-19

Branch **`FIX-WITH_FREECAD`**, allineato col remoto. Ultimo commit `e05724c`.
Suite: **1235 passed, 2 xfailed**. Albero pulito.

`FIX` resta fermo a `801dd5f` come punto di ritorno.

> **STATO ONESTO.** Il meccanismo dei modelli reali KiCad è completo e
> verificato pezzo per pezzo, ma **non è mai stato visto funzionare in
> un'esecuzione completa dell'applicazione**. L'utente non è soddisfatto e ha
> ragione: nel 3D vede ancora forme sintetiche. Leggere **0.4** prima di
> qualunque altra cosa.

---

## 0. Modelli 3D reali dalla libreria KiCad

Idea dell'utente: smettere di approssimare i package dal bounding box e usare
i modelli veri che KiCad installa già sul disco.

Non è un'API: sono file locali. Su questa macchina
`C:/Program Files/KiCad/10.0/share/kicad/3dmodels`, 105 cartelle,
**7251 modelli** STEP/WRL. Niente rete, niente costo per pezzo.

### 0.1 Com'è fatto

`kicadgen/cad3d/kicad_models.py`, modulo nuovo:

- `find_library()` — trova la libreria; l'assenza **non è un errore**, si ripiega
- `candidates(package)` — ricerca per nome normalizzato (`SOT-223`, `SOT_223`,
  `U.FL` collassano sulla stessa forma), i più attinenti prima
- `measure_models()` — misura con FreeCAD **un solo processo per lotto**,
  indice su disco versionato (`.kicadgen-model-sizes-v3.json`)
- `resolve_model()` — sceglie **e verifica**
- `attach_models(components)` — aggiunge `model_path` ai componenti risolti

**Due trappole già pagate, da non ripetere:**

1. **`freecadcmd` NON inoltra `sys.argv` allo script.** I percorsi passano da
   un file di scambio (`in.json` / `out.json`), cartella in
   `KICADGEN_MEASURE_DIR`.
2. **La risoluzione deve avvenire FUORI da FreeCAD.** Misurare un modello
   richiede FreeCAD a sua volta, e non si può lanciarlo dall'istanza che sta
   già costruendo il solido. Per questo `attach_models` sta in
   `step_generator`, prima di `_run_freecad`, e `freecad_parts` si limita a
   fare `Part.read` di un percorso già risolto.

### 0.2 Come sceglie, e perché così

- Fra i candidati vince **il più vicino alla misura**, non il primo.
- A parità di prove (entro il 5%) decide il **nome esatto**: `SOT-223` e
  `SOT-223-8` hanno lo stesso corpo, li distingue solo il nome.
- Tolleranza **0.30 derivata, non tarata**: fra due taglie adiacenti corre
  almeno il 60%, quindi perché la scelta per vicinanza sbagli servirebbe un
  errore di misura oltre il 30%.
- Ogni modello porta **due letture**, `body` e `total`, e vale la più vicina:
  il rettangolo sulla foto può aver seguito il corpo oppure l'estensione coi
  reofori. Il corpo si misura con una sezione **al 65% dell'altezza**, non a
  metà — i reofori stanno bassi e a metà rientrano ancora nella sezione.
  Verificato: al 65% `SOT-223` dà 3.47×6.47 contro 3.5×6.5 reali; al 50%
  dava 5.25 sul lato corto.
- **Rifiutare è l'esito sicuro**: meglio una forma dichiaratamente
  approssimata di un modello reale ma sbagliato.

### 0.3 Il campo `package`: dove va chiesto DAVVERO

Errore commesso e corretto. Il campo era finito in `classify_fixed_packages`,
che sui moduli **castellated non viene mai chiamato**: quella famiglia passa
da `mechanical_envelopes`, che usa `verify_mounted_bodies` e forza
`kind = "generic"` di proposito.

Sintomo che lo ha rivelato: nel log reale **tutti i 43 componenti erano
`[generic]`** e nessun package veniva raccolto.

**Emendamento esplicito a una decisione di progetto.** `verify_mounted_bodies`
chiedeva solo occupazione per non certificare identità elettriche; ora chiede
anche `package`. Motivazione: una sigla di package è una **sagoma meccanica**
standard (un SOT-223 è un contorno JEDEC), non una funzione di circuito. Il
divieto sull'identità elettrica resta nel prompt e nella descrizione del
campo, e il test del contratto lo verifica. Registrato in `MEMORY.md`.

### 0.4 CHE COSA È VERIFICATO E CHE COSA NO

**Verificato con prove dirette:**

- `build_component` carica davvero lo STEP: senza `model_path` **8 vertici**
  (scatola), con `model_path` **125 vertici**, posizione X +10.00 Y −5.00 e
  base Z 0.96 esatte. Percorso rotto → ripiega sulla scatola con avviso.
- La catena `package` → componente → `_components_3d` → `attach_models` →
  `model_path` passa tutti e quattro i passaggi.
- Nella corsa reale il modello **ha compilato `package`** con nomi di
  libreria, non descrizioni: R_0603 (12), SOD-323 (11), R_0402 (6), SOT-23
  (6), SW_TACT (3), SOIC-8 (2), SMD_Crystal, SOT-223, ESP32-S3-WROOM-1,
  C_0805, U.FL.
- Di quelle 11 sigle **7 si risolvono** con le quote reali: SOD-323, SOT-23,
  SW_TACT, SOIC-8, ESP32-S3-WROOM-1, C_0805, **U.FL**.

**NON verificato — ed è il punto dolente:**

- **Nessuna esecuzione completa dell'applicazione ha mai prodotto un modello
  3D contenente STEP reali.** L'utente vede ancora forme sintetiche. Cause
  possibili, da escludere in quest'ordine:
  1. l'app non è stata riavviata dopo i commit che agganciano il risolutore;
  2. la generazione 3D è stata lanciata su dati estratti **prima** che il
     campo `package` esistesse — in quel caso non c'è nulla da risolvere e il
     ripiego è corretto;
  3. un difetto non ancora trovato fra `attach_models` e `build_component`.
- La riga `Modelli reali dalla libreria KiCad: N su M componenti` **non è mai
  stata vista comparire**. Era chiamata senza log (corretto in `b83ae40`).

### 0.5 Diario per la diagnosi

Ogni riga del Log AI finisce in **`analisi-ai.log` dentro la cartella del
componente**, con intestazione datata per corsa. Serve esattamente a
rispondere alle domande di 0.4 **senza dipendere dal copia-incolla**
dell'utente. Il diario segue il componente quando viene rinominato.

### 0.6 Aperto su questo fronte

1. **Le quote fotografiche sono sistematicamente più grandi dei corpi**,
   perché i rettangoli includono le piazzole di saldatura:

   ```
   R_0603   misurato 2.62 x 1.25   reale 1.60 x 0.80   +64%
   R_0402   misurato 1.50 x 0.84   reale 1.00 x 0.50   +68%
   SOT-223  misurato 4.26 x 1.63   reale 6.50 x 3.50   -35%
   ```

   Sono **25 componenti su 45**, il grosso della scheda. **Non** si risolve
   allentando la tolleranza: un 0402 diventerebbe indistinguibile da un 0603.
   Si risolve aggiungendo una **terza lettura** — l'estensione con le
   piazzole, dal footprint KiCad accanto al modello — e confrontando
   grandezze omogenee. È lo stesso ragionamento che ha portato SOT-223 da
   102% a 7%, applicato all'altro estremo.
2. **`SMD_Crystal` non esiste in libreria**: i cristalli si chiamano
   `Crystal_SMD_3225-4Pin_3.2x2.5mm`. Unico caso di sigla plausibile ma non
   reale; una normalizzazione dei nomi più comuni aiuterebbe.
3. **Colori**: `Part.read` restituisce geometria senza colori, quindi il
   modello reale prende il colore campionato dalla foto. Accettabile, ma i
   modelli KiCad sono multicolore e l'informazione si perde.

**Le forme sintetiche non si buttano**: sono il ripiego quando manca KiCad,
manca il package o l'ingombro non conferma.

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

- **Diario del componente**: `analisi-ai.log` nella cartella del componente
  contiene tutto il Log AI, con intestazione datata per corsa. E' la via piu'
  rapida per diagnosticare senza chiedere il copia-incolla.
- **`pytest tests`, non `pytest`**: la seconda forma tenta di collezionare le
  cartelle sotto `generated/` e fallisce con PermissionError.
- **Controllare l'esito di pytest, non dell'ultimo comando della pipe.** Un
  commit rotto (`14b05da`) e' stato pushato perche' `tail` riusciva anche con
  i test rossi.
- **Attenzione all'escaping quando si genera codice o testo con script.**
  Sequenze come `
`, `.0`, `dmodels` sono state interpretate piu'
  volte, producendo file corrotti. Preferire edit per riga o la scrittura
  diretta del file.

## 6. Decisioni prese dall'utente in questa sessione

- Quarto ruolo documenti: **sì** (supera la decisione del 2026-09-13 "solo tre
  ruoli", registrata in `MEMORY.md`).
- `docs/ESP32/` **fuori da Git** (5 MB di immagini di lavoro), aggiunto a
  `.gitignore`.
- Rinomina componente: **sempre automatica**, senza chiedere.
