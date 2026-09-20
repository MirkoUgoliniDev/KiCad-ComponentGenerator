# 2026-09-20 (7) — l'orientamento si deduce da un riferimento, non da una regex

Branch `YOLO-SOLUTION`, sopra `0b99df0`. Richiamo dell'utente: «non devi fare
una pezza per far funzionare questa scheda, deve inferire la rotazione da dei
riferimenti, per esempio un punto di marcatura oppure in questo caso
l'antenna». Ha ragione, e il checkpoint precedente lo ammetteva già: la regola
dei 180° era ancorata al nome del file e valeva per un modulo solo.

## 1. Cosa c'era e perché era sbagliato

`_native_model_rotation_override` decideva il verso con due `re.search` inglesi
su tre campi concatenati (`terminal_layout + package_evidence + package`), e
scattava solo per un modello il cui nome file coincideva con la marcatura.
Tre difetti, tutti dimostrati: è una regola sui nomi, che il CLAUDE.md vieta;
le due condizioni possono essere soddisfatte da campi diversi, quindi il
`package` di un modulo con U.FL rendeva la seconda sempre vera e bastava un
«bottom right» riferito ad altro per far ruotare; ed è cieca a qualunque lingua
diversa dall'inglese.

**È stata rimossa**, insieme ai suoi due test, come deciso dall'utente.

## 2. Cosa c'è adesso

Il verso di un modello nativo è la composizione di due informazioni
indipendenti, e servono **entrambi i lati** per stabilirlo:

**a. Lato osservato — si chiede, non si deduce.** Nuovo campo
`asymmetry_landmark` nello schema del global inventory: `feature` (vocabolario
chiuso: coaxial_jack, connector_opening, cable_exit, polarity_marker,
pin1_marker, chamfer, notch, tab, other), `side` (left/right/top/bottom della
scatola di *quel* componente, nel frame della prima foto, X a destra Y in
basso) e `confidence`. `null` è una risposta legittima e frequente: un corpo
simmetrico non ha un capo distinguibile dall'altro. Una dichiarazione
incompleta o a confidenza bassa vale come assenza — qui un errore vale mezzo
giro, non poco. `PROMPT_VERSION` sale a `global-yolo-inventory-6`.

**b. Lato nativo — si misura, col baricentro.** In `freecad_parts`,
`_native_landmark_direction` confronta il centro di massa del solido col centro
del suo ingombro e restituisce la direzione dominante dello scarto,
normalizzata sul semilato (quindi adimensionale: vale per un modulo da 20 mm e
per un chip da 1). Sotto `NATIVE_LANDMARK_MIN_OFFSET = 0.02` il pezzo è
simmetrico quanto basta perché la direzione sia rumore di tassellazione, e la
risposta è `None`. Criterio fisico, nessun nome, nessuna soglia tarata su un
esemplare.

**c. Derivazione.** `_verse_correction` cerca **solo 0 e 180**: l'asse l'ha già
deciso la misura dell'ingombro e una correzione di 90 lo smentirebbe. Se né 0
né 180 portano il riferimento nativo dalla parte osservata, i due dati si
contraddicono e non si corregge nulla. Il ribaltamento Y fra frame fotografico
(Y in basso) e frame CAD (Y in alto) è applicato esplicitamente.

Il report porta ora `orientation_landmark` quando il riferimento c'è, e
`orientation_unverified` quando manca, invece di una nota che descriveva una
rotazione decisa da una regex.

## 3. Verifica

* Suite senza GUI: **2413 passati**, con i soliti 2 falliti e 1 errore di
  raccolta identici alla baseline.
* Moduli GUI uno per uno: verdi.
* Coppie positivo/negativo nuove: baricentro spostato contro corpo simmetrico;
  verso che segue il lato osservato contro dati che si contraddicono; campo
  dichiarato per intero contro mezza dichiarazione, confidenza bassa,
  vocabolario violato e risposta in cache che il campo non ce l'ha.
* **NON verificato sul modello vero.** Il baricentro del WROOM-1U non è ancora
  stato misurato: il server MCP di FreeCAD sulla macchina dell'utente non trova
  `freecadcmd` (FreeCAD 1.1 è in `C:\Program Files\FreeCAD 1.1`, quindi il
  binario è in `...\bin\freecadcmd.exe`) e la porta RPC rifiuta la connessione
  perché la GUI non è aperta. **È la prova che manca**, ed è alla portata: con
  uno dei due canali attivi bastano dieci righe di script.

## 4. Resta aperto

* **Misurare il baricentro del WROOM-1U** e confermare che lo scarto superi la
  soglia e punti dal lato del connettore U.FL. Se il modulo risultasse troppo
  simmetrico, il verso resterà non verificato e servirà il secondo criterio
  (il materiale sopra il piano dello schermo), che è più fragile e va deciso.
* La prossima estrazione **paga una chiamata nuova**: il prompt è cambiato,
  quindi la cache non risponde più. È voluto, ma va saputo.
* Il campo `mating`, già presente in altri due schemi, resta fuori da quello
  del global inventory.
* Quante feature abbia un contatto castellated è ancora un default e non una
  lettura della fotografia (vedi checkpoint precedente).

---

# 2026-09-20 (6) — i contatti castellated hanno due appigli, non uno

Branch `YOLO-SOLUTION`, sopra `ee4c564`. Segnalazione dell'utente con
fotografia: «i connettori che fa il wizard sono troppo aperti, qui è un foro
chiuso e un mezzo foro aperto».

## 1. Cosa mancava

Il wizard generava **una sola feature per contatto**: un cilindro col centro a
`0.76 · raggio` dentro il bordo, che tagliato dal bordo lascia una U spalancata.
Nel footprint 2D non c'era nulla di tutto ciò: solo piazzole SMD rettangolari,
nessun foro.

Sulla scheda dell'utente ogni contatto ne ha **due**: il mezzo foro sul bordo,
che serve alla rifusione SMD, e un **foro passante chiuso** poco più dentro, in
cui si salda uno strip a passo 2,54. È il doppio aggancio tipico delle schede
di sviluppo, e senza il secondo il bordo resta tutto aperto e i pin non si
possono saldare.

## 2. Decisioni dell'utente

* **Due feature per posizione**: foro chiuso più mezzo foro al bordo.
* **Il footprint 2D cambia di conseguenza**: i pad THT entrano davvero nel
  `.kicad_mod`, non solo nel 3D.
* **Le quote sono campi del wizard con default standard**, non rapporti
  derivati dal passo né misure chieste al modello.

## 3. Cosa è cambiato

`CastellatedModuleParams` ha tre campi nuovi: `thru_hole_drill` (default 1,0
mm), `thru_pad_diameter` (1,7 mm) e `thru_hole_offset` (1,27 mm, distanza del
centro dal bordo). La proprietà `has_thru_holes` è vera quando foro e piazzola
sono entrambi dichiarati; **mettere il diametro a zero è il modo di dire «questo
modulo ha solo i semi-fori»** — un foro di diametro nullo non esiste, quindi lo
zero non può essere scambiato per una quota.

Nel footprint ogni contatto riceve un pad `thru_hole` circolare su `*.Cu` e
`*.Mask`, **con lo stesso numero del pad SMD**: sono due appigli di un contatto,
non due contatti, e KiCad li tratta come una sola rete. Nel solido 3D si
sottrae un secondo cilindro, quello del foro chiuso, accanto a quello del
semi-foro.

Tre validazioni nuove, tutte geometriche: la piazzola dev'essere più larga del
foro (senza corona non c'è rame su cui saldare); il centro dev'essere distante
dal bordo almeno quanto il raggio della piazzola (altrimenti non è un foro
chiuso, è un secondo semi-foro); i fori dei due lati opposti devono starci
dentro la dimensione più corta del corpo.

Il `hole_inset` del semi-foro **non è stato toccato**: resta il parametro già
documentato. Se la U va resa più profonda si abbassa quello.

## 4. Verifica

* Suite senza GUI: **2386 passati**, con i soliti 2 falliti e 1 errore di
  raccolta identici alla baseline.
* Moduli GUI uno per uno: verdi, tranne `test_board_pipeline` (2) che fallisce
  **identico anche sulla baseline** (`body.pin_color_hex` con body None: è
  l'ambiente, non il codice).
* Contratti aggiornati capendo perché: `test_footprint_has_smd_pads_without_drill`
  e l'asserzione `"thru_hole" not in content` esprimevano il vecchio contratto
  «solo SMD» e ora verificano il footprint ibrido.
* Coppie positivo/negativo nuove: contatto con entrambi i fori contro modulo
  che dichiara zero fori passanti; cilindri che attraversano il bordo contro
  cilindri interamente dentro il corpo.
* **Non verificato**: nessun run end-to-end, serve FreeCAD.

## 5. Resta aperto su questo fronte

* **Quante feature abbia un contatto è una cosa che si vede nella fotografia**,
  e secondo la regola del CLAUDE.md andrebbe chiesta al modello invece che
  decisa da un default. Oggi il default è «tutte e due», che è il caso comune
  delle schede di sviluppo ma non di tutti i moduli: un convertitore RS485 con
  i soli semi-fori ora riceve dei fori che non ha, finché non si mette il
  diametro a zero a mano. È un ripiego dichiarato, non una regola generale.
* L'attributo del footprint resta `smd` anche col pad passante: il montaggio
  primario del modulo è la rifusione sui semi-fori. Se si volesse coerenza con
  KLC per i footprint ibridi andrebbe deciso a parte.
* Le quote di default (1,0 / 1,7 / 1,27) sono i valori standard di uno strip a
  passo 2,54. Su una scheda fuori standard vanno corrette a mano nei tre campi.

---

# 2026-09-20 (5) — USB-C, regolatore e orientamento ESP32

Branch `YOLO-SOLUTION`, sopra `cb7c185`. Tre difetti segnalati dall'utente sul
run reale, indagati da tre agenti di sola analisi su ambiti separati
(connettori / semiconduttori / orientamento) e integrati qui.

## 0. Il verdetto che ribalta un'ipotesi: l'ESP32 era storto di 90°, non di 180°

Il checkpoint precedente lasciava aperto se la regola dei 180° «non scatti o
scatti al contrario». **Nessuna delle due: scatta, ed è corretta nel suo
ambito.** L'errore è a monte ed è di **90 gradi**, quindi nessun valore fra 0 e
180 poteva rimetterlo dritto — ed è il motivo per cui il connettore d'antenna
restava dalla parte sbagliata sia prima sia dopo la correzione del trattino.

Due difetti distinti, entrambi corretti:

**a. L'asse si decideva con una metrica non invariante di scala.**
`freecad_parts.py` sceglieva fra 0° e 90° minimizzando `|x/L−1| + |y/W−1|`.
Con l'ingombro fotografico gonfiato (24.40 × 20.43 contro 19.2 × 18.0 reali) i
due punteggi diventano 0.32276 e 0.33231: **la scelta si ribalta con un margine
dell'1%**. Lo STEP del WROOM-1U ha l'asse lungo su Y, l'ingombro fotografico ce
l'ha su X per contratto (`length_mm = max(sx, sy)`), quindi la risposta giusta è
**90, sempre, a qualunque scala**. Ora l'asse si decide confrontando *quale*
lato è il più lungo — la stessa lettura ordinata che `size_distance` usa già per
dichiarare quel modello compatibile. Prima le due fasi si contraddicevano in
silenzio: il matcher calcolava `relative_xy_error` assumendo la mappatura a 90°,
il costruttore ne sceglieva un'altra.

**b. La correzione automatica era un angolo assoluto e cancellava l'asse.**
`_native_model_rotation_override` scriveva in `model_rotation_deg`, che
`freecad_parts` legge **al posto** dell'angolo dedotto. Ora va in
`model_rotation_native_delta_deg`, che si **somma**: asse 90 + verso 180 = 270.
Resta assoluta solo la scelta esplicita dell'utente, che nomina insieme il
modello e l'angolo. L'angolo assoluto di una corsa precedente continua a essere
ripulito, come prima.

## 1. Il regolatore: un dubbio elettrico cancellava una dichiarazione meccanica

`photo_global_inventory.py` azzerava il campo `package` quando
`class_confidence` era `low`. Per U4 il modello aveva scritto
`large-tab power package (TO-252/DPAK or SOT-223-like), 3 leads + wide tab`,
con `presence_confidence=high`, `bbox_basis=body_and_leads` e
`confidence=medium`: l'evidenza **meccanica** era buona, era bassa solo la
fiducia su *che cosa faccia* il pezzo. Buttando il campo la ricerca perdeva la
sua chiave e scandiva l'intera famiglia.

Misurato sulla libreria vera: da **79 rigetti + ricerca troncata +
`topology_incomplete`** a **9 percorsi, 1 rigetto, ricerca completa**. Asimmetria
che rendeva evidente il difetto: `package_evidence` (la prosa) veniva
conservata, il campo strutturato no.

Ora a cancellare il package restano la fiducia complessiva bassa e un contorno
irrisolto. **Q1, J2 e J3 non ne beneficiano**: hanno `confidence=low`, e lì
azzerare è corretto — il modello stesso dichiara l'identità irrisolta.

## 2. USB-C: la logica premiava le liste ambigue e puniva quelle nitide

`_provisional_geometry_choice` agganciava un'anteprima **solo** se i primi due
candidati erano indistinguibili. J1 aveva 4 candidati a 4% e 16%: nessuna
anteprima. Dimostrato dagli agenti eseguendo la funzione: aggiungendo un
doppione identico del migliore l'anteprima compariva; togliendo il doppione
spariva.

Pareggio e dominanza sono le due facce della stessa cosa — il pareggio dice «la
fotografia non distingue queste varianti», la dominanza dice «le ha distinte, e
una sola regge» — e in entrambi i casi l'incertezza residua riguarda identità e
variante, non la geometria mostrata. Ora anche la dominanza aggancia
un'anteprima, **a due condizioni**: la ricerca dev'essere completa (superare
alternative che nessuno ha misurato non è superarle) e i candidati almeno due
(**un candidato solo non è dominanza, è assenza di confronto** — questa
restrizione evita di promuovere cinque casi che i test esistenti tenevano in
revisione a ragione).

## 3. Verifica

* Suite senza GUI: **2382 passati**, con i soliti 2 falliti e 1 errore di
  raccolta **identici alla baseline** dello stesso ambiente.
* Contratti aggiornati capendo perché, non allentati: i due test
  sull'orientamento ora verificano il canale additivo; quello sulle confidenze
  indipendenti ha finalmente il caso `presence=high, class=low, conf=medium`,
  che è esattamente il regolatore e non era mai stato parametrizzato.
* Coppie positivo/negativo nuove: asse invariante di scala contro asse già
  giusto; dominanza contro «né pareggio né dominanza»; dominanza su ricerca
  completa contro dominanza su ricerca troncata.
* **Non verificato**: nessun run end-to-end. Serve FreeCAD, che qui non c'è.

## 4. Resta aperto, in ordine di peso

* **Il verso dei 180° resta un'euristica basata sul nome**, quindi vietata dal
  CLAUDE.md, e va rimossa non tarata. Due difetti già dimostrati: le due
  condizioni (`lower right` e `rf|u.fl|ipex`) sono cercate nella **stessa
  stringa concatenata** di tre campi, quindi il `package` di un modulo con U.FL
  rende la seconda sempre vera e basta un «bottom right» riferito ad altro per
  far ruotare; ed è cieca a qualunque lingua diversa dall'inglese. La cura
  strutturale è chiedere al modello un `asymmetry_landmark` (feature + angolo o
  bordo nel frame della foto) e misurare il lato nativo dallo STEP, invece di
  dedurre da prosa. **Dopo la correzione dell'asse va riguardato se i 180
  servano ancora o siano ora di troppo**: 90 e 270 mettono il connettore ai due
  capi opposti dello stesso bordo corto, e senza il file STEP non si può dire
  quale sia giusto.
* **`terminal_count_basis` è scritto incondizionatamente** a
  `visible_including_tabs` per ogni riga del global inventory. Rende
  `_provisional_terminal_count` codice morto per tutti i componenti, e con esso
  il conteggio osservato: è la ragione per cui `_observed_count` è vuoto per i
  connettori e `auto_matched` è di fatto irraggiungibile fuori da R/C chip
  (misurato: 23 su 23 agganciati automaticamente sono R/C).
* **`_declared_count` legge solo la nomenclatura IC.** Dei 3332 modelli di
  famiglia connettore, 2982 dichiarano le posizioni nel nome (`_6P_`,
  `_1x04_`) e ne riconosce **zero**. Da ancorare alla famiglia della
  tassonomia: fuori dai connettori `_<N>P_` non è un conteggio
  (controesempio reale: `Potentiometer_Bourns_3296P_Horizontal`).
* **Scoperta senza chiave.** Con `package` vuoto e classe a bassa fiducia si
  scandisce comunque l'intera famiglia (J2/J3: 3332 percorsi, 3310 scartati per
  solo nome) e il fallimento viene riportato come `search_truncated`, che
  descrive un budget esaurito e non l'assenza di qualunque chiave.
* **Il regex del suggerimento dimensionale legge un passo come un ingombro**:
  `P2.29x1.90mm` di un DSUB entra nella coda CAD come se fosse 2.29 × 1.90 mm.
  Sono tutti falsi positivi e consumano budget.
* **Il campo `mating` esiste in due schemi ma non in quello del global
  inventory**: è l'attributo che distingue un connettore da un passivo stampato
  di pari ingombro, ed è l'esempio citato dal CLAUDE.md. J2/J3 non erano
  distinguibili nemmeno in linea di principio.
* **Una sola misura per componente.** Il modello scrive in prosa due rettangoli
  (corpo e inviluppo reofori) per U3, U4 e Q1; lo schema ne trasporta uno. Il
  16% di U3 è questo, non un errore d'identità.
* **Il log registra il prompt ma non la risposta AI.** Le risposte grezze stanno
  solo in `generated/photo-cache/yolo-audit/global-inventory-*.json`: senza
  quel file un'analisi come questa sarebbe stata ricostruttiva.
* Vicolo cieco dell'indice misure, `omitted` incoerente, scarto del 28% sul
  modulo: invariati.

---

# 2026-09-20 (4) — il nome lo decide l'utente, e la modale dice chi lavora

Branch `YOLO-SOLUTION`, sopra `9b56d4f`. Tre richieste dell'utente dopo la
prima prova reale della correzione precedente, piu' il conto di quella prova.

## 0. La correzione dei passivi funziona: 0 → 23 agganciati

Stesso modulo, stessa foto, prima e dopo `9b56d4f`:

| | prima | dopo |
|---|---|---|
| componenti | 53 | 45 |
| `auto_matched` | **0** | **23** |
| `provisional_matched` | 6 | 5 |
| `review_required` | 35 | 6 |
| `unresolved` | 12 | 11 |

Il magenta passa da quasi tutto a circa un terzo. Restano fuori gli 11
irrisolti e i 6 in revisione: non e' stato ancora indagato quali siano ne'
perche'. E' il prossimo fronte sul matching.

## 1. Perche' comparivano tre cartelle in area di lavoro

L'utente scriveva "Test" e si ritrovava `Test/`, `PinHeader_2x04_P2.54mm_THT/`
e `Module_ESP32-S3-DEV-KIT-NXRX_63.3x25.4mm_Castellated/`. Nessuna e' un
residuo accidentale: erano tre regole che si sommavano.

* Il campo Nome parte con un valore predefinito (`PinHeader_2x04_P2.54mm_THT`),
  e il log dell'analisi si scrive nella cartella del nome in uso.
* `Test/` nasce quando l'utente scrive il suo nome e allega i documenti.
* A fine estrazione il nome proposto **sovrascriveva** quello scritto e
  spostava `docs/` sotto il nome nuovo. Ogni analisi che proponeva un nome
  diverso lasciava indietro la cartella precedente.

**Decisione dell'utente: comanda il nome che scrive lui.** Ora l'estrazione
propone e basta: la proposta compare accanto al campo con un pulsante "Usa
questo nome", e solo premendolo il progetto cambia nome portandosi dietro i
documenti e il log. Tutto cio' che si genera -- `.pretty`, `.3dshapes`, file --
porta il nome in uso. Se il campo e' vuoto la proposta viene adottata senza
chiedere: non c'e' nulla da rispettare e `NUOVO_COMPONENTE` non serve a nessuno.

Il valore predefinito del campo **non e' stato toccato**: cambiarlo e' una
decisione a se', e comunque sposterebbe il problema invece di chiuderlo.

## 2. La modale dice chi sta lavorando

Prima mostrava al posto dello stato le righe che il programma scrive per se'
(`AI_CALL_PROMPT_END id=1 model=... sha256=...`): occupavano il posto di cio'
che stava accadendo senza dirlo a nessuno.

* **Strumento in chiaro, con un segno grafico diverso per ciascuno**: nuvola
  per il modello remoto (esce dalla macchina e si paga), riquadro con gli
  angoli staccati per YOLO (e' cio' che disegna), cubo isometrico per FreeCAD,
  piazzole per KiCad. Il colore da solo non basterebbe a distinguerli.
* **I marcatori di servizio non arrivano piu' sulla scheda**, restano nel log.
  Il pulsante "Apri log AI" resta dov'era.
* **Barra di avanzamento solo dove esiste un conteggio vero.** L'unico passo
  della generazione con un totale onesto e' l'abbinamento dei modelli, che
  scorre i componenti e per ognuno puo' pagare una misura CAD: li' si vede
  `27% · 12/45 componenti`. Per la chiamata al modello no, e resta l'anello
  animato: una percentuale stimata sul tempo si ferma al 90% e dice meno del
  nulla, perche' sembra un'informazione. **Scelta esplicita dell'utente.**

Meccanica: i worker hanno un segnale `phase(strumento, dettaglio, fatti,
totale)` accanto a `progress(str)`; `attach_models` accetta un callback
`progress(done, total)` e lo chiama a ogni componente. Un totale non positivo
significa "nessun conteggio" e la barra sparisce.

## 3. Verifica

* Suite senza GUI: **2370 passati**, 2 falliti e 1 errore di raccolta,
  **identici alla baseline** misurata nello stesso ambiente.
* Moduli GUI, eseguiti uno per uno: tutti verdi
  (`test_automatic_generation` 25, `test_photo_inventory_gui` 61,
  `test_board_edit_lifecycle` 26, `test_photo_detector_ui` 46,
  `test_live_log_lifecycle` 14, `test_docs_migration` 5,
  `test_analysis_overlay` 8, e gli altri). Fanno eccezione
  `test_board_pipeline` (2) e `test_preview_component_picking` (4), che
  falliscono **identici anche sulla baseline**: sono artefatti dell'ambiente,
  non del codice.
* **Ambiente**: la suite gira in un contenitore Linux. VTK non parte senza
  OpenGL, quindi i moduli GUI usano uno stub di `pyvistaqt` che non fa parte
  del repository, e con lo stub alcuni moduli vanno eseguiti separatamente.
  **Va rieseguita su Windows** con
  `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider` prima di
  considerare chiuso il lavoro.
* Contratti di test aggiornati, non allentati: `test_docs_migration`,
  `test_live_log_lifecycle` e `test_automatic_generation` verificavano la
  rinomina automatica; ora verificano che la proposta non tocchi nulla e che
  i documenti seguano **quando viene accettata**.

## 4. Resta aperto

* **Gli 11 irrisolti e i 6 in revisione del run reale non sono stati
  indagati.** E' il prossimo passo sul magenta.
* L'avanzamento reale copre solo l'abbinamento modelli. Il rilevamento YOLO
  lavora per riquadri e l'inventario per viste: entrambi hanno un totale
  ottenibile, nessuno dei due e' stato cablato.
* `_on_phase` passa "componenti" come unita' del conteggio perche' oggi quello
  e' l'unico passo contato. Se se ne aggiunge un altro, l'unita' deve viaggiare
  col conteggio invece di stare nel gestore.
* Il valore predefinito del campo Nome continua a creare una cartella se si
  analizza prima di dare un nome.
* Vicolo cieco dell'indice misure, `omitted` incoerente, connettore RF, scarto
  del 28%, USB-C: invariati, vedi i checkpoint precedenti.

---

# 2026-09-20 (3) — perché ogni passivo restava magenta

Branch `YOLO-SOLUTION`. Punto di partenza `027ee40`. Segnalazione dell'utente
con schermata: nell'anteprima 3D della ESP32-S3-DEV-KIT quasi tutti i corpi
piccoli sono porpora, cioè senza modello KiCad agganciato.

## 0. La diagnosi precedente era giusta come osservazione, sbagliata come cura

Il checkpoint precedente indicava `_package_token` che «restituisce stringa
vuota quando c'è prosa attorno alla sigla» come il difetto da correggere, con
la prova C1 `"0603"` agganciato contro C6 `"0603 (long axis vertical)"` in
revisione. **Ho provato quella correzione e i dati reali l'hanno smentita.**

Rigiocando il run vero (`generated/ESP32-.../kicad-model-candidates.json`,
53 componenti):

* 21 componenti su 53 hanno package **`"0603 chip"`**, non la forma fra
  parentesi. Promuovere la sigla in testa al campo dà loro il token `0603` e
  li manda sulla **ricerca per nome**.
* Per **4 di quei 21** (LED_PWR, R5, C13, R13) il candidato migliore di oggi è
  uno **0805**, non uno 0603: la misura fotografica smentisce la taglia
  nominata di una classe intera. Cercando per nome un 0603 quei quattro
  sarebbero rimasti **senza alcun candidato**, cioè peggio di prima.
* Soprattutto: **tutti e 21 escono con `search_truncated: True`**, e una
  ricerca troncata non può dimostrare unicità, quindi `_automatic_mechanical`
  non aggancia mai. Il token non era il collo di bottiglia; il troncamento sì.

Quella correzione è stata quindi **ritirata**, non solo non committata. Resta
valida l'osservazione di partenza: una sigla dentro una descrizione si comporta
diversamente da una sigla nuda. È la direzione della cura a essere opposta.

## 1. La catena vera

`"0603 chip"` non è una sigla nuda, quindi `_package_token` torna `""`, quindi
`fallback` è vero e si va in scoperta per famiglia. Lì `_tentative_endcap_family`
avrebbe dovuto restringere la ricerca al catalogo dei chip SMD — è esattamente
ciò per cui esiste — ma pretendeva la forma `0603 (approx)` con la parentesi.
Il modello quella forma non la scrive quasi mai.

Senza quel ramo la ricerca scandisce l'intera famiglia nella libreria vera:
**478 modelli per un condensatore, 108 per un resistore, 93 per un LED**. Di
quei 478, **477 non hanno quote nel nome**: restano non misurati, marcano
`omitted`, e `omitted` diventa `search_truncated`. Da lì in poi nessun passivo
può essere agganciato automaticamente, qualunque sia il suo scarto.

Ecco perché la schermata è quasi tutta magenta: non è un problema di misure né
di soglie, è che la ricerca si dichiara incompleta per costruzione.

## 2. Cosa è cambiato

**a. Una taglia nominata dentro una descrizione apre il catalogo dei chip.**
`_tentative_endcap_family` non pretende più la parentesi. Il criterio è
strutturale, non lessicale: il campo non è una sigla nuda, non risolve ad alcun
package dichiarato, e nomina **una sola** taglia. `"0603/0805"` e `""` restano
fuori come prima. Effetto misurato sulla libreria vera: da 478 percorsi con
`omitted=True` a **17 percorsi con ricerca completa**; 21 per il LED.

**b. Il catalogo vale anche per i LED.** `LED_SMD.3dshapes` ha le stesse sigle
di taglia (`LED_0603_1608Metric`) e la stessa morfologia a due endcap. I
**diodi restano deliberatamente fuori**: condividono il catalogo a sigle ma
hanno anche i MELF cilindrici, che in fotografia mostrano gli stessi due
terminali metallizzati e non entrerebbero in un elenco di sole sigle.
Dichiarare completa una ricerca che li esclude significherebbe poter agganciare
da soli il modello sbagliato. Separazione fragile, dichiarata come tale.

**c. Il conteggio dei reofori ordina, non fa solo da veto.** Entro
`NAME_TIEBREAK_MARGIN` la fotografia non distingue due solidi: lì un conteggio
dichiarato che coincide con quello osservato vale più di una frazione di
percento di scarto. Corregge il caso U3 (6 reofori osservati, unico candidato a
6 terminali in fondo alla classifica). Fuori dalla banda comanda ancora la
misura: un conteggio che coincide non promuove mai un solido già scartato.

## 3. Verifica

* Suite completa: **2224 passati**, 14 falliti e 14 errori di raccolta —
  **identici alla baseline** di `027ee40` misurata nello stesso ambiente, che
  ne aveva 2218/14/14. I 6 test in più sono quelli aggiunti qui.
* **Attenzione, limite dell'ambiente**: la suite è stata eseguita in una VM
  Linux senza PyQt6 (manca `libEGL`, niente root per installarlo) e senza
  KiCad. I 14 falliti e i 14 errori sono tutti di lì. **Va rieseguita su
  Windows** con `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`
  prima di considerare chiuso il lavoro.
* Verifica su libreria reale (`C:\Program Files\KiCad\10.0\share\kicad\3dmodels`),
  non su fixture: `"0603 chip"` → 17 percorsi, `omitted=False`;
  `"0603 chip LED"` → 21, `omitted=False`; `"0603"` nudo → ricerca per nome;
  `"0603/0805"` e `""` → invariati.

## 4. Resta aperto

* **Non è stato provato un run vero end-to-end.** Senza FreeCAD nell'ambiente
  non si arriva allo stato finale dei 53 componenti: è dimostrato che il blocco
  (`search_truncated`) sparisce, non quanti componenti diventino `auto_matched`.
  Serve una nuova generazione sulla macchina dell'utente per contarli.
* **Il vicolo cieco dell'indice delle misure NON è corretto.** Il numero
  dell'handoff precedente è confermato sui file veri: 91 LED e 116 diodi hanno
  un nome senza alcuna quota, non vengono mai misurati, quindi non entrano in
  cache, quindi non verranno mai misurati. Ho provato ad ammetterli tutti nella
  coda CAD e **l'ho ritirato**: per un condensatore la coda passa a 478
  percorsi, sfonda `MAX_CANDIDATE_MEASUREMENTS` e il troncamento torna da
  un'altra porta, pagando anche 64 misure FreeCAD per componente. Serve una
  scoperta *limitata* prima, non una coda più grande. Il ramo dei chip la
  fornisce per i passivi; per connettori, moduli e semiconduttori no.
* **`omitted` è incoerente e resta tale**: un nome con quote lontane viene
  saltato (ci si fida del nome per non misurare) e contemporaneamente marca la
  ricerca incompleta (non ci si fida del nome per concludere). O il nome è una
  dichiarazione o non lo è. Toccarlo cambia quando l'aggancio automatico è
  lecito su tutte le famiglie: va deciso, non fatto di straforo.
* `_native_model_rotation_override`, lo scarto del 28% sul modulo, USB-C e il
  conteggio terminali come veto: invariati, vedi i checkpoint precedenti.

## 5. Trappole per chi riprende

* **Un'altra sessione stava committando sullo stesso checkout** durante questo
  lavoro: `027ee40` ha inglobato modifiche non sue (il `.gitattributes` scritto
  qui) e due gitlink spuri `.claude/worktrees/agent-*`, che andrebbero tolti
  dall'indice e ignorati. Non far lavorare due sessioni sullo stesso checkout.
* Le fini di riga ora sono normalizzate da `.gitattributes`. Prima un
  `git status` mostrava 103 file modificati di cui 4 veri.
* Il file `kicad-model-candidates.json` nella cartella del componente è un
  *report ridotto*: non contiene `class`, `kind`, `terminal_layout` né le
  confidenze, quindi da solo non basta a rigiocare la scelta. Per farlo
  servono i campi originali del componente.

---

# 2026-09-20 (2) — il modulo diventava porpora dopo una rotazione

Tre difetti segnalati dall'utente su una sessione reale, tutti riprodotti e
corretti.

## 1. Ruotare l'ESP32 gli toglieva il modello KiCad

Sintomo: l'utente corregge l'orientamento del modulo dal dialogo di revisione
fotografica, rigenera, e il blocco diventa **porpora** (nessun modello nativo).
La rigenerazione FreeCAD riusciva (`exit=0`, STEP/FCStd/WRL scritti): il
problema era a monte.

Nel log le due corse differiscono per una riga sola:

```
12:46:23  shielded SMD module ...: 1 dimensional candidates; provisional_matched
12:49:43  Footprint KiCad: 0 letture collegate; 0 footprint ... senza ingombro
12:49:43  shielded SMD module ...: 1 dimensional candidates; review_required
```

Quella riga intermedia compare solo quando `basis in ("unknown", "pads")`
(`kicad_models.py:1605`). Provato in isolamento sul componente vero:

| `bbox_basis` | esito |
|---|---|
| `body` | **provisional_matched** (renderizzato) |
| `leads` / `total` | auto_matched |
| `unknown` o assente | **review_required** (porpora) |

Causa: `photo_review_dialog._geometry_changed` azzerava `bbox_basis` dopo
**qualunque** modifica geometrica, rotazione compresa.

**La regola era giusta per sposta e ridimensiona, sbagliata per ruota.**
`bbox_basis` risponde a *"il contorno segue il corpo o il corpo coi reofori"*.
Spostare o ridimensionare ridisegna il contorno e quella risposta decade;
girarlo no — il rettangolo mantiene centro e misure e racchiude la stessa
cosa. In piu' `"unknown"` non e' l'assenza di informazione: e' un'affermazione
diversa, che manda il componente sul ramo delle piazzole e gli nega il modello
nativo.

Ora una rotazione conserva `bbox_basis` e `package_evidence`; continua a
scartare `terminal_geometry`, che localizza il metallo *dentro* il rettangolo
e dipende da come e' posato. Sposta e ridimensiona non cambiano.

Effetto collaterale utile: `package_evidence` alimenta anche la regola dei
180° sul connettore RF (`_native_model_rotation_override`), che prima si
perdeva insieme al resto.

## 2. Il tasto destro non apriva il menu

Due cose distinte.

**La prima**: il processo dell'utente era partito prima che il menu esistesse
(commit `084d721` delle 12:50:07, sessione in corso dalle 12:46). Serviva
riavviare.

**La seconda, vera**: passare solo da `contextMenuEvent` e' fragile. Qt lo
sintetizza dal messaggio di sistema e sopra la finestra nativa che VTK si crea
non e' garantito che arrivi. Ora il clic e' servito dal **rilascio del
pulsante**, che l'interattore Qt di VTK inoltra sempre essendo un QWidget
normale; `contextMenuEvent` resta come via alternativa (tasto menu della
tastiera) con una guardia contro il doppio menu sullo stesso gesto.

Il tasto destro di VTK e' anche lo zoom a trascinamento: il menu si apre solo
se il mouse non si e' spostato oltre pochi pixel. Sei casi coperti da test
(clic fermo, clic con 2 px, trascinamento, tasto sinistro, doppia consegna,
tasto menu della tastiera, vuoto).

## 3. Il costo AI compariva durante una rigenerazione senza AI

`"Rigenera modello 3D"` non chiama il modello, ma la modale riproponeva
`Costo ultima analisi: 0.0500 USD …`, che li' si legge come il costo di cio'
che sta accadendo.

Azzerato nella modale **solo** per la rigenerazione manuale. Nella generazione
automatica il costo resta: li' estrazione e generazione sono un'unica
operazione continua, ed era il motivo per cui `clear_cost` non stava in
`start()`. La label sotto il pulsante conserva il dato, e dice esplicitamente
"ultima analisi".

## Resta aperto

* `_native_model_rotation_override` decide i 180° con una regex inglese su
  prosa libera (`lower right` + `rf|u.fl|ipex|coax|antenna`) e vale di fatto
  per il solo WROOM-1U. Nella foto dell'utente il connettore finisce comunque
  dalla parte sbagliata: non ho ancora verificato se la regola non scatta o
  scatta al contrario. Intanto la rotazione dal menu contestuale corregge
  l'orientamento **senza** distruggere l'abbinamento.
* Il modulo resta a 28% di scarto perche' la misura fotografica da' 20,68 mm
  sul lato corto contro 18,0 reali: il rettangolo include le castellature
  oltre lo schermo.
* USB-C, `_package_token` sulla prosa, vicolo cieco dell'indice e conteggio
  terminali: invariati, vedi la sezione precedente.

---

# 2026-09-20 — perché mancano componenti, e menu contestuale sul 3D

Branch **`YOLO-SOLUTION`**. Il lavoro non committato di un'altra sessione
(849 righe, 17 file) è stato messo in salvo intatto in `58633f7` prima di
toccare qualunque cosa, per avere un punto di ritorno.

## 1. Dove si perdono i componenti: non nel rilevamento

Censimento visivo sulla foto ad alta risoluzione dell'ESP32-S3-DEV-KIT:
**44 corpi realmente montati**. Le 51 righe finali sono 44 componenti più
7 frammenti YOLO non renderizzati.

Bilancio onesto: **42 individuati, 2 persi, 2 fantasma, 1 con geometria
gravemente sbagliata** (il regolatore, 7,2×3,0 mm invece di ~8,2×8,9 mm).

Il collo di bottiglia **non è YOLO**: ha proposto almeno un box su 44 corpi
su 44, recall ~100%. Tutta la perdita netta avviene nella **fusione e
riconciliazione**: 111 proposte → 46 componenti, con **zero aggiunte
autonome** del modello nonostante il prompt lo chieda esplicitamente.

Numeri del percorso: 252.000 predizioni → 2.265 sopra la soglia di audit →
1.301 eleggibili → 194 per vista → 63 globali → 111 consegnate all'LLM.
Costo della corsa: 1,0166 USD, **due sole chiamate LLM**.

### 1.1 Difetto corretto: presenza e provenienza confuse

`photo_global_inventory.py:_validate_geometry` cancellava **l'intero
componente** quando una sola proposta sorgente non si sovrapponeva
all'inviluppo dichiarato, a meno che `presence_confidence` fosse `high`.

Così sono spariti due componenti veri:

* **Q1** (regolatore): 5 sorgenti, 4 dentro, 1 fuori — il tab stagnato, che
  l'AI ha collegato correttamente al pezzo ma ha lasciato fuori dal
  rettangolo. Cancellato.
* **C14** (condensatore): 2 sorgenti, 1 dentro, 1 fuori — un secondo
  condensatore, fuso per errore nello stesso corpo. Cancellato.

**La correzione non è quella che sembrava ovvia.** Allargare la condizione
a `medium` avrebbe rotto un contratto deliberato e testato
(`test_outside_source_invalidates_parent_even_for_one_source`), che pretende
la cancellazione quando l'**unica** sorgente è fuori.

La distinzione giusta non è quanto è sicura la lettura, ma **che cosa
sopravvive al legame sbagliato**:

* restano proposte dentro l'inviluppo → il corpo è comunque rilevato, si
  toglie il legame e si tiene la riga;
* non ne resta nessuna → la riga si regge solo sulla parola del modello, e
  lì serve davvero la dichiarazione esplicita di corpo visibile.

Così i due contratti convivono: il test esistente continua a passare e
**Q1 e C14 tornano**, verificato rigiocando `_validate_geometry` sui dati
veri della corsa (`global-inventory-9f1bea73….json`). Le righe grigie non
renderizzate scendono da 7 a 2.

Corretta anche un'**incoerenza interna** nella stessa funzione: l'`assembly`
(pezzi distinti sotto un inviluppo, affermazione debole) accettava
`medium`, mentre il `single_body` (corpo unico, affermazione forte)
pretendeva `high`. Ora entrambi usano la stessa asticella.

### 1.2 Cosa resta aperto su questo fronte

1. **Nessun recupero dei corpi mancati dal rilevatore**: la copertura finale
   è limitata dall'alto dalle proposte YOLO. Oggi non costa pezzi, ma rende
   fatale ogni futuro buco del detector.
2. **I 48 componenti della prima chiamata AI vengono sovrascritti senza
   confronto** (`datasheet_extractor.py:987`): si paga una seconda opinione
   e la si butta.
3. **Nessuna verifica piazzola popolata vs vuota**: `R16` accettato su due
   pad nudi, `R17` su una goccia di stagno. Le categorie esistono solo per i
   *reject*, non c'è controllo a valle sui *keep*.
4. **Inviluppo del regolatore mai ricostruito**: nessun candidato copre il
   corpo plastico nero.
5. **Dipendenza da confidenze infime**: il regolatore sopravvive grazie a una
   sola proposta a confidenza 0,055, sotto la soglia operativa 0,25.
6. **`clipped_by_tile` è un vicolo cieco**: 331 proposte scartate prima
   della NMS che non entrano nemmeno nel pool di review.

### 1.3 Sul matching dei modelli KiCad (analisi, non ancora corretto)

Su 51 componenti: 10 auto_matched, 4 provisional, 1 confermato, **27
review_required**, 9 irrisolti. Il blocco dominante è `not complete`
(26 su 27), non `len(choices)!=1` come avevo ipotizzato — **ipotesi mia
sbagliata**, corretta dall'analisi.

Prova decisiva: **C1** con package `"0603"` è auto_matched con scarto 10,0%;
**C6** con `"0603 (long axis vertical)"` è review_required con scarto
**7,8%**, sullo **stesso identico STEP**. `_package_token` restituisce
stringa vuota quando c'è prosa attorno alla sigla, e il componente scende
dalla ricerca per nome a quella per famiglia, che marca la ricerca troncata.

Altri due difetti strutturali trovati e **non ancora corretti**:

* **vicolo cieco nell'indice delle misure**: un modello senza quote nel nome
  non viene mai misurato, quindi non entra in cache, quindi non verrà mai
  misurato. **91 LED e 117 diodi permanentemente invisibili.**
* **il conteggio dei terminali è raccolto e mai usato per ordinare**, solo
  come veto: U3 ha 6 reofori osservati e l'unico candidato che ne dichiara 6
  è ultimo in classifica.

## 2. Menu contestuale sulla vista 3D

Tasto destro su un componente: **elimina dal 3D** (falso positivo del
rilevatore), **ruota** (+90/180/270 e azzeramento), **cambia modello KiCad**.
Testi tradotti in italiano, inglese, francese e tedesco.

Punti di progetto:

* Si passa da `contextMenuEvent` di Qt e non da un osservatore VTK sul tasto
  destro: quello lascerebbe l'interattore a metà di una manovra di zoom
  mentre il menu tiene il mouse. Fuori dai componenti il tasto destro resta
  quello di VTK.
* Le voci agiscono sui **metadati della tabella componenti**, che resta la
  fonte di verità; la vista 3D serve solo a indicare quale riga.
* L'eliminazione **non cancella la riga**: scrive `render_3d_override=False`.
  Un falso positivo scartato va comunque documentato, altrimenti alla corsa
  successiva nessuno saprebbe che era stato proposto.
* La rotazione è un **delta**, non un angolo assoluto: l'angolo di partenza
  lo deduce FreeCAD confrontando il modello nativo con la misura, e la GUI
  non lo conosce. Un delta resta valido anche cambiando modello.

**Trappola pagata:** la prima stesura aveva messo i campi nel costruttore di
payload sbagliato (`_generate_populated_tht`), mentre il caso reale è un
modulo **castellated**, che usa un costruttore diverso. Ora entrambi passano
dallo stesso helper `_model_placement`, e c'è un test che copre proprio il
percorso castellated.

## 3. "Rigenera modello 3D" non tocca l'AI

Requisito ribadito dall'utente. Verificato **strutturalmente**, non a
comportamento: l'intero pacchetto `kicadgen/cad3d/` non ha alcun import verso
OpenRouter o gli altri servizi AI, né legge la chiave API.

`tests/test_regeneration_is_offline.py` lo blinda: fallisce nel momento in cui
qualcuno aggiunge l'import, non mesi dopo. Include il proprio controllo
negativo, perché un test del genere passerebbe anche a vuoto.

Le scelte manuali (esclusione e rotazione) fanno il giro completo
foto → tabella → `kicad-model-choices.json` → rigenerazione, e
`attach_models` non le sovrascrive più: il ripristino a `render_3d=True`
ripuliva lo stato calcolato della corsa precedente **e anche la scelta
dell'utente**.

## 4. Altre correzioni di questa sessione

* **Regressione nel diff non committato**: `_synchronize_preview_variants`
  iterava la lista grezza senza il guard `isinstance` che `attach_models` ha,
  quindi una voce malformata sollevava `AttributeError`.
* **Contratto di test lasciato indietro** dall'audit dei prompt: il test del
  worker terminali contava le righe di progresso. Ora verifica il ciclo di
  vita per **posizione relativa**, così un canale facoltativo può aggiungere
  righe senza che sia una regressione.
* **`.FCStd` perso in silenzio su Windows**: `saveAs` usa le API Win32
  classiche e si ferma a MAX_PATH (260 caratteri) mentre l'export STEP passa;
  il caso reale è a 228. Ora si salva su un percorso corto e si copia a
  destinazione col prefisso esteso. E soprattutto il fallimento **si vede**:
  `KICADGEN_FCSTD_FAILED` non finiva fra le righe che arrivano all'utente.

---

# 11:16 — prompt LLM reso auditabile e verifica indipendente bloccata dal limite agenti

Richiesta utente: scrivere nel log il prompt realmente inviato a ogni chiamata
LLM e ricontrollare la logica senza dare un esito di facciata. Il wrapper
centrale `kicadgen/services/ai_diagnostics.py` ora accetta `log_prompt=True`.
Nell'app questa modalità è attiva sia per l'estrazione principale sia per il
passaggio di dettaglio terminali, quindi copre anche l'inventario globale YOLO
perché riceve lo stesso `diagnostic_call`.

Ogni chiamata ora scrive immediatamente nel `analisi-ai.log`:
`AI_CALL_PROMPT_BEGIN/END` con un hash della richiesta, il system prompt esatto,
tutti i blocchi testuali esatti, lo schema tool JSON e le opzioni effettive
(`max_tokens`, PDF, retry). Le immagini e gli eventuali file binari non vengono
copiati nel log: restano rappresentati da SHA-256 e lunghezza codificata. Sono
state aggiunte redazioni minime per bearer/API key/`sk-...`; il resto del prompt
testuale resta leggibile e verificabile. Un retry HTTP 400/408/409/429/5xx
scrive anche il secondo prompt, con `compatibility_retry=true`.

Prove locali: `tests/test_ai_diagnostics.py` passa (28 test); suite mirata
prompt + matching + GUI passa **204 test in 4.43s**. `py_compile` e `git diff
--check` puliti. I due test specifici WROOM/lower-right passano (**2 passed**).
Il tentativo di includere `tests/test_terminal_metadata_flow.py` è stato
interrotto perché il test Qt non ha prodotto avanzamento; non viene dichiarato
passato e non è un errore del wrapper dimostrato.

Audit agenti richiesto dall'utente: non eseguito, perché il servizio agenti ha
risposto con il limite operativo di utilizzo prima di avviare i tre audit. Non
inventare quindi un risultato di agenti. La verifica locale indipendente resta
valida ma non sostituisce quell'audit.

Gli artefatti `generated/` non sono presenti nella working copy in questo
momento (cartella vuota); perciò non è stato possibile fare un nuovo replay
FreeCAD sul file prodotto dall'ultima esecuzione in questa sessione. Le evidenze
del run precedente restano sotto questo handoff: il vecchio run era stato
eseguito prima della correzione WROOM e mostrava `model_rotation_deg=null` e
`local_rotation_deg=0`. Dopo il riavvio dell'app e un nuovo run, il log deve
mostrare i blocchi `AI_CALL_PROMPT_*`; il report WROOM atteso è 180°.
Nessun nuovo AI a pagamento, commit o push effettuato.

# 11:07 — correzione orientamento WROOM-1U dopo l'ultimo run Opus

L'ultimo run utente ha confermato il problema: il JSON globale descriveva il
connettore RF come `lower-right` con trattino, mentre la regola precedente
riconosceva soltanto `lower right` con spazio. Di conseguenza il report salvava
`model_rotation_deg=null` e FreeCAD registrava `local_rotation_deg=0`, lasciando
il modello ESP32-S3-WROOM-1U ruotato di 180° rispetto alla foto.

Corretta la regola per accettare entrambe le forme (`lower right` e
`lower-right`). Il test mirato aggiunto copre la forma con trattino; suite
mirata aggiornata: 176 passed in 4.10s. Non serve una nuova chiamata AI:
premere soltanto **Rigenera modello 3D** dopo il riavvio/applicazione del codice.
Il prossimo report deve mostrare per `MOD1 (rf_module)`
`model_rotation_deg=180.0`, `native_model_rotation_deg=180.0` e il log FreeCAD
`local_rotation_deg=180`.

L'ultimo run AI è comunque terminato senza HTTP 400: 53 ipotesi, 5 importazioni
KiCad reali, 11 ipotesi YOLO escluse dal 3D, STEP completato. Il report originale
non è stato riscritto manualmente; la correzione verrà applicata dalla
rigenerazione locale.

# 10:49 — scelte modello e rotazione consolidate nei JSON; replay FreeCAD riuscito

Implementata la persistenza completa dello stato CAD locale richiesta dall'utente.
`kicad-model-choices.json` ora conserva, per ogni componente e per la stessa foto
calibrata, oltre a `model_selection`/`model_match`, anche `model_path`,
`model_rotation_deg`, `rotation_deg` e `render_3d`. Il ripristino automatico continua
ad applicare soltanto le scelte esplicite `confirmed`/`rejected`: i percorsi automatici
non vengono trattati come autorizzazione cieca e vengono ricalcolati dal matcher.
`kicad-model-candidates.json` salva gli stessi campi insieme a dimensioni, package e
lista delle alternative; la finestra 3D ricarica questi valori nel metadata della riga.

Il doppio clic su un solido continua ad aprire il selettore dei modelli KiCad; scelta
e rotazione vengono quindi riportate nel JSON quando la revisione viene applicata e
restano disponibili al successivo replay. La rotazione locale automatica del modello
esatto `ESP32-S3-WROOM-1U.STEP` è stata verificata a 180° per il connettore RF osservato
in basso a destra; non altera la rotazione fotografica del componente.

Replay locale dell'inventario globale già salvato, senza AI e senza modificare l'output
utente: `C:\Users\mirko\AppData\Local\Temp\kicadgen-freecad-replay-z73qt5jo`.
FreeCAD ha generato STEP, FCStd, WRL e preview JSON reali; nessun import STEP fallito.
Il report replay contiene 51 righe: U5 mantiene il modello SOT-223 confermato, M1
usa il modello Espressif WROOM-1U con `model_rotation_deg=180`, mentre le 10 righe
`YOLO candidate ...` restano nel JSON come ipotesi di revisione ma hanno
`render_3d=false` e non vengono più disegnate come fantasmi magenta. La preview
non contiene parti YOLO; M1 e U5 sono presenti come geometria reale.

Test mirati dopo la modifica: 148 passed (scelte JSON, report GUI, flusso scelta
modello, doppio clic, matcher KiCad). Nessun nuovo modello, chiamata AI, commit o
push effettuato; output originali preservati. Restano da fare solo il riavvio
dell'app e una prova manuale del doppio clic con una nuova scelta, così da verificare
visivamente il salvataggio nel JSON della cartella del componente. La suite completa
avviata in seguito è stata interrotta dopo oltre due minuti senza avanzamento o
output del runner; non è stata usata come evidenza di successo. `py_compile` e
`git diff --check` restano puliti.

# 09:05 — libreria Espressif utente rilevata e WROOM-1U importato in FreeCAD

La libreria locale installata dall'utente contiene il modello esatto:
`C:\Users\mirko\Documents\KiCad\10.0\3rdparty\3dmodels\com_github_espressif_kicad-libraries\espressif.3dshapes\ESP32-S3-WROOM-1U.STEP`.
Il resolver ora cerca anche i repository KiCad `Documents\KiCad\*\3rdparty\3dmodels`,
usa il codice modello letterale presente nella marcatura per restringere la ricerca
e considera il file una proposta di modulo solo dopo il controllo dimensionale.
Per una corrispondenza letterale unica e coerente, l'importazione automatica e'
abilitata; orientamento e posizione restano comunque verificabili.

Replay offline del JSON salvato, senza nuove chiamate AI:
`outputs/espressif-library-replay-fixed2` — 49 componenti, 6 `auto_matched`,
20 `review_required`, 23 `unresolved`. C21 e' stato associato al file
`ESP32-S3-WROOM-1U.STEP` con dimensioni misurate 18.00 x 19.20 x 3.20 mm.
FreeCAD ha generato STEP/FCStd/WRL con 6 importazioni native e colori reali.
Suite completa: 2646 passed, 2 xfailed, 19 subtests, 136.49s.
Test mirato aggiunto: `test_literal_marking_selects_user_installed_module_model`.
Nessun commit/push e nessuna nuova AI pagata.

# Handoff — sessione 2026-09-19

## 08:53 — correzione tutto-magenta verificata sul nuovo JSON

Suite COMPLETA con codice finale: 2644 passed, 2 xfailed, 19 subtests, 159.79s.
L'esecuzione intermedia aveva 2 failure sul caso class=unknown con modulo gia'
caricato prima della correzione; entrambe risolte e incluse nella suite finale.
84 nuove regressioni chip (piu' documentazione) passate. Reviewer Heisenberg
approva dopo aver richiesto veto alle frasi negate: solo descrizioni affermative
dei due endcap, classi coerenti e prove indipendenti attivano il nuovo ambito.

Replay FINALE outputs/morning-verified-replay: 49 ipotesi, 26 con candidati,
5 auto_matched (C5,C7,C9 -> C0603; C11 -> R0603; C32 -> R0805), 21 review,
23 unresolved. CAD-verification.json conferma tutti e cinque i solidi nativi,
colori reali non magenta, misure e rotazioni. STEP11.237.211byte. Rendering
top/3D ispezionato; DIAGNOSI.md spiega causa, limiti e inventario completo.
Nessuna nuova AI pagata, output app originale intatto, branch/HEAD invariati.
Confucius aggiunto test_tentative_passive_gui.py: test sintetico reale
GUI->worker->FreeCAD passa insieme al precedente real_board_pipeline (2 passed,
12.05s); nessuna selezione manuale/preboundpath, C0603 auto_matched con colori
nativi e dimensioni1.6x0.8x0.8, trasporto evidenze confermato. Ultima suite
con anche questo test appena aggiunto in corso; produzione congelata.
Non far ripagare l'analisi per dimostrare questo fix: JSON nuovo gia' riprodotto.
Riavvio necessario per caricare codice; app NON ripristina l'intero stato
di estrazione alla riapertura. Nella finestra vecchia resta possibile scelta
manuale dei candidati e Rigenera, senza AI. Nessun hot-reload introdotto.

## 08:46 — nuovo run riprodotto, 5 importazioni reali recuperate

Fix locale matcher: per una taglia chip R/C approssimata con endcap osservati
e evidenze indipendenti, discovery_scope=standard_chip_endcaps enumera tutte
le taglie/varianti STEP della famiglia; non impone il numero nominale e misura
anche taglie fredde lontane. Budget64/tolleranza30% invariati, ambigui manuali.
100 test mirati passati; suite completa79510 in corso, Boyle aggiunge nuove
regressioni in tests/test_tentative_passive_discovery.py, Heisenberg review.
Replay produzione dell'audit NUOVO:49 ipotesi,26 con candidati,5auto,21review,
23unresolved. Mesh reale verifica5 nativeimports, STEP11.237.211byte;
output isolato workspacechat outputs/morning-run-replay, render ispezionato.
Preservato run utente originale; nessuna nuova AI a pagamento o commit/push.

Audit Godel separato (non causa tutto magenta): _library_model conserva scala
STEP ma puo' centrare sulla sezione65% anche con bbox_basis total. Testmodulo
asimmetrico sposta centro0.2mm; da correggere in seguito con riferimento
coerente, non modificato in questo fix. Scelta STEP esterno gia' presente.

## 08:40 — nuovo run utente tutto magenta, diagnosi

Run reale 08:29-08:36, cartella generated/ESP32-S3-DEV-KIT-NXRX_2x22_P2.54mm_Castellated:
49 ipotesi, 26 review_required e 23 unresolved, ZERO STEP applicati. AI e FreeCAD
completati, nessun errore HTTP/credito; prompt v5/schema4 effettivamente usati.
Audit global-inventory-8b8dea146c894f3a85cda70bb8feaa71.json, YOLO yolo-aqlku78r.json.
Differenza dal vecchio replay: package passivi "0603 (approx)" porta a ricerca
di tutta la famiglia, poi search_truncated blocca anche candidati unici con
misure e due terminali completi. NON basta togliere il flag o imporre 0603:
eventuali taglie adiacenti compatibili devono restare alternative. Correzione
generale da verificare su QUESTO JSON; preservare output utente, nessuna nuova AI.
Esperimento WROOM manuale sospeso per priorita' diagnosi ultima richiesta utente.

## Verifica finale 08:10 — codice testato, limiti residui espliciti

Suite COMPLETA finale:2560 passed,2 xfailed,19 subtests,268.66s.
ReviewerHeisenberg nessunblocco nuovecorrezioni (cacheultima43test+doc).
git diff --check pulito, branchYOLO-SOLUTION HEAD53492f6 invariato,
nessun nuovo commit/push. Generazione e meshconfermate9importazioni native.
Nuovo reportoutputs/night-json-cad-replay:38candidati,9auto,29review,
26irrisolti,64ipotesitotali noncertificate. Preservatioutputoriginali.
App modificata, necessita riavvio; nuovo promptv5 richiede futuraanalisi
per migliorare vecchiedimensioni/identita', nessuna nuova AI pagata eseguita.

STEPufficiale esatto WROOM1U verificatoFreeCAD:18x19.2x3.2mm, compoundvalido,
231solidi/3474facce (NONcomponenti rilevati); colorsnativepreservati.
Misure, renderPNG e fonte/licenza in outputs/official-espressif-cad.
Non installato, non associato automaticamente su scheda. La suapresenza
non certifica orientamento/posizioneAI. Nuovi processiterminati senza
toccare apputente, filetemporaneimateriali noneliminati. Handoff attuale
prevale su tuttistoriciprecedenti. Heartbeatnotturno rimaneattivofino09:00
salvo nuova indicazione dell'utente: nessun permesso pernuoveanalisi pagate.

## Checkpoint 08:05 — replay notturno completato e verificato

Cache Windows corretta: aperturaesclusiva UUID con3retry solo collisione,
PermissionError passa al fallback senza loop; reviewerapprova,43testcache
(44con doc). Cache utente ora presente115347byte. Replay80662 completato:
64ipotesi,38con candidati (prima34),9auto_matched,29review,26irrisolti.
FreeCAD STEP17.185.979byte,9componenti importatinativi verificati nelmesh;
nuovi preview eMATCHING-REPORT in outputs/night-json-cad-replay del workspace.
Vista reale ispezionata: nessuna scritta none/?spuria, moltepartirestanomagenta
perche'non scelte; non dichiarare completa la ricostruzione.

La rimozione dei falsi conteggiesatti nei JSONlegacy allarga le alternative
di Q1/U4 (ora anche SO/BGA a quotacompatibile): e'un limite delle evidenze
vecchie, NONsono identita' certificate. SOT223presenteperU4; altrerichiedono
scelta/futuri dati con lead_count completo. Non ripristinare uguaglianza
fra terminalivisibili e pin totali per farapparirelista migliore.
Suite completa aggiornata1729ancoraincorso, faulthandlersegnala45s nel test
scanfootprint reale (nonblocco: proseguito). Agenzeproduttivecongelate,
GodelverificaSTEPufficialeesatto standalone, senza inserirloscheda.

## Checkpoint 08:00 — correzioni in verifica, replay da ripetere

Fix locali hanno recuperato candidati REALI per SW1 (5), SW2 (5), USB-C (3)
e LED D2 (2 varianti0805), prima tutti senza candidati. D1 correttamente
non accetta1206: LED0805total2x1.25 compatibile, LED1206total3.2x1.6 no.
Helper model_measurement normalizza raw/cache e conserva metodo di misura.
Nuovo contratto terminali e sentinel implementato;145testintegrazionepassati.
Suitecompleta finale58813 in corso; reviewerHeisenberg01a0bd60-5a65-7760-9166-e9684ae39485.

Replay61480 e probe31250 BLOCCATI in nuovo helpercache: faulthandler prova
tempfile.NamedTemporaryFile -> tempfile._mkstemp_inner loopCPU su Windows
ProgramFiles ACL (os.access diceTrue). Fermati soltanto processi di prova
4880/13772, confermati per commandline. Boyle corregge helpercache con
uuid+openx, retries limitatiFileExistsError, immediato fallbackPermissionError.
Rieseguire testcache, replay e fullsuite dopo questa correzione. Non dire
ancora verificato replay finale. Vecchi output latest-json-cad-replay intatti;
nuovo night-json-cad-replay parziale in workspacechat outputs.

Verificato online che STEP esatto WROOM1U esiste nella libreria UFFICIALE
Espressif (non nella locale): github.com/espressif/kicad-libraries, path
3dmodels/espressif.3dshapes/ESP32-S3-WROOM-1U.STEP. Scaricato solo in
outputs/official-espressif-cad conLICENSE.md; SHA256
7be82bdafece2891b297546eb0643ff254e3d8161074ebdc972e0d78e79a4bdb.
Non installato, non inserito nel CAD, da misurare/verificare. Nessuna AI
pagata o cambio detector/modelloLLM. Non inventare posizione o totale pin.

## Prosecuzione 07:36–08:00 del 20/09 — in corso

Suite completa prima di nuove correzioni: 2399 passed, 2 xfailed,
19 subtests, 282.59s. Nessun nuovo commit/push/AI pagata. Branch invariato.
Audit esperti Godel e Confucius: cache fredda esclude switch/USB/LED senza
quote nel nome; corpo misurato come sezione al 65% e' sbagliato per LED
(0805 reale 2x1.25 ma sezione 1.25x1.25, falso rifiuto), e puo' accettare
LED1206 con lente piccola (falso positivo). Non allargare ricerca prima
di correggere semantica delle misure. S3-WROOM-1U esatto assente in locale.

Ulteriore bug confermato: terminal_count della risposta globale conta solo
superfici visibili, aletta inclusa; non equivale a totale piedini ordinari.
In correzione con nuovo metadato terminal_count_basis e schema completo
lead_count/lead_count_complete/lead_count_confidence. Vecchi JSON restano
replayabili senza promuovere conteggi incompleti a fatti. Eccezione strutturale
esplicita per singolo R/C standard con entrambi gli endcap visibili.
Confucius modifica global_inventory/extractor+test (anche sentinel none/?);
Godel helper model_measurement+test; main matcher, trasporto GUI/CAD e replay.
Nuovo test LED Metric passato (2); non basta ancora per risolvere D2 a causa
del difetto sezione-corpo. Conservare precedente output; nuova prova isolata.

## Checkpoint notturno 20/09 — leggere prima dei checkpoint storici

Utente chiede prosecuzione durante la notte e handoff prima dei limiti.
Heartbeat attivo: miglioramento-pcb-yolo-e-kicad, orario, stessa attivita;
terminare entro le 09:00 Europe/Rome del 20/09 con riepilogo onesto.
Nessuna nuova chiamata AI a pagamento, nessun nuovo commit/push autorizzato
in questa prosecuzione. Branch YOLO-SOLUTION, modifiche ancora non committate.

Replay finale completato usando risposta JSON salvata e pipeline produzione:
64 ipotesi (NON 64 componenti certificati), 34 con candidati, 9 auto_matched,
25 review_required, 30 unresolved. Verifica mesh: 9 importazioni native KiCad
effettive, STEP 21.424.718 byte. Rendering e report rigenerati nel workspace
chat outputs/latest-json-cad-replay (preview-top.png, preview-3d.png,
MATCHING-REPORT.md). Nessuna sovrascrittura del run originale.

Ultimi test mirati: 342 passed. Suite completa precedente 2345 passed,
2 failed poi corretti e ritestati, 2 xfailed: rieseguire la suite completa
dopo le ultime modifiche prima di dichiarare verifica completa.
Matcher: conteggi terminali vendor-prefixed e varianti HTSSOP/AQFN/TDSON;
topologia IC non interpretabile esclusa se count osservato, ricerca marcata
incompleta per non dichiarare falsa unicita. SOT/TO senza quote nel nome
entrano nel budget di misura CAD esistente: U4 ora propone SOT-223, resta
ambiguo e NON applicato automaticamente. Test nuovi variant_terminal_counts.

Restano importanti limiti: grandi componenti ancora da scegliere, bbox del
modulo nella risposta AI sovrastimata (CAD rispetta esattamente tale bbox),
proposte deboli conservate non equivalgono a corpi reali. Non ridurre vincoli
per colorare il rendering. Migliorare evidenze/matching generale, esaminare
foto e rendering, testare senza nuova AI pagata. Prompt legacy incompatibile
rimosso per futuri run; replay non ripara misure errate nella risposta vecchia.

## Nuova correzione20/09: replay reale ultimo JSON, in corso

Ultimo checkpoint codice finale:331 test mirati passati (16.80s), include
GUItransport, prompt/merge, matcher, test FreeCAD reali e pipelineGUIreale.
Reviewer2blocchi risolti con regressionitest: famigliecompatibili enumerate
primaunicita', groupingunresolved indipendente numeroorigini. Conta terminali
ora riconosce HTSSOP e packagevendor-prefixed; evita proposte24/98pin per4.
Agentimodifiche congelate. Rendering/replayfinale in corso per riallineare
artefatti; nuove modifiche NON committate/pushate (ultimo remoto53492f6).
Nessuna nuovaAIpagata. Questo replay corregge validazione/matching della
risposta esistente, non le sue misurefoto inventate o ambigue: non dichiarare
identificazione completa. Per nuovi run promptlegacy incompatibile rimosso.

Checkpoint: replay produzione FreeCAD64ipotesi=43corpi+21proposte irrisolte;
34con candidati,9auto_matched importati,25review,30irrisolti. Nessuna AI pagata.
Rendering in outputs/latest-json-cad-replay del workspacechat, non nel repo.
MOD1 mesh24.40215x20.701mm corrisponde bboxAI ed entroPCB: non bug scalaCAD.
Corrette9marcature che fuoriuscivano con fitglyph, testFreeCADrealepassato.
Fullsuite2345pass2fail2xfail19subtests: uno testoCADversioneintermedia,
unoasserzionelogvecchia; entrambi corretti/verificati con7testreali passati.
Reviewer segnala2ultimiblocchi in correzione: unionfamilyic/semiconductor per
non dichiarareunicita' falsa, grouping_basisunresolved anche0/1source.
Trasporto class osservata aggiunto a evidenceGUI/CAD per evitare perdita
famigliepotenza; rerun finale necessario dopoquesteultimepatch.

HEAD53492f6 suYOLO-SOLUTION, precedente push verificato. Utente autorizza
fix e agenti usando JSON esistente, nessuna nuova AI pagata. Run00:12-00:20:
111proposte ->45corpiAI+2fallback=47; report10review_required,37unresolved,
0modelli applicati. Restrizione conferma manuale per TUTTI introdotta prima
non corrisponde all'intento utente (manuale solo ambigui); da correggere.
Audit global-inventory-9192fbb92e084110ae701802800b896a.json in generated:
prompt effettivo contiene legacycontraddittorio 'Do NOT identify...' e
istruzioni tipologia modulo inappropriate perinventario montato. Nuovi agenti:
Hypatia matcher/fallback+auto meccanico, Lovelace validazione merge/prompt,
Newton geometriaCAD. Main replay produzione e GUItransport evidenze.
R13 e gruppi incerti non devono diventare corpo gigante; no coordinate
inventate per far combaciare esempio. Output prova separato nel workspacechat.
Non attribuire zero matching attuale al precedente difetto stringhe soltanto:
10 candidati ci sono ma bloccati, altri package assenti/nonstandard.

## Consegna20/09 — stato corrente (precede tutti i checkpoint storici)

Branch YOLO-SOLUTION. Commit implementazione09ac162, seguito dal presente
checkpoint documentale. Commit include81 file del lavoro cumulativo autorizzato;
foto, pesi, output generati e configurazione privata esclusi. Scansione177 file
senza pattern credenziali; DB contiene profili/traduzioni. Review finale Faraday
senza blocchi residui. Test:2248 completi al checkpoint,394 mirati dopo ultime
patch;2 xfailed noti,19 subtests. Nessuna nuova chiamata AI a pagamento.
Utente puo' riavviare python main.py e rilanciare YOLO locale+verificaAI:
cache prompt precedente invalidata, analisi richiede credito del provider.
La nuova verifica reale e il controllo visivo finale sono ancora da eseguire
dall'utente; completezza e classificazione non certificate. Segnaposti magenta
restano intenzionali fino alla scelta del modello KiCad. Nessun lavoro extra
non autorizzato avviato. Push richiesto su origin/YOLO-SOLUTION subito dopo
questo checkpoint; verificarne esito remoto prima di dichiararlo concluso.

## Copertura dei corpi prima della classificazione — integrazione20/09

Ultimo stato: implementate e ritestate anche le correzioni del reviewer.
Suite completa checkpoint2248 passed,2 xfailed,19 subtests; dopo le ultime
patch394 test integrazione superati (28.48s), agente parser499, detector183.
Detectorcall produzione include_review_candidates=True: replay locale63
ordinarie+48 deboli=111 ipotesi (NON111 componenti reali). Include i due corpi
esaminati nel replay, senza garantire completezza sulla foto dell'utente.
Nessun cambio pesi/soglia ordinaria; candidati.05-.25 distinti con metadata.
Promptv3/schema2 invalida cache precedenti: prossimo avvio verifica AI a pagamento
normale, payload maggiore ma stesso numero di viste/chiamate. UI conserva
incerti generici. Test API simulata e inferenza detector reale, NON nuova
verifica AI reale. Handoff storico seguente descrive checkpoint precedenti.
Commit/push richiesti, in preparazione dopo controllo segreti e revisione.

Checkpoint integrazione20/09: suite completa2248 passed,2 xfailed,19 subtests
(159.44s). Restano ultime patch/retest integrazione candidati deboli e filtro
nomi legacy: review read-only ha trovato corpi globali persi quando ID/nome
contiene 'pad', e shorthand0603-class non normalizzato. Bernoulli corregge.
Feynman replay locale sul crop storico: due corpi vicini IC con score.205/.177
persi primaNMS, da confrontare con screenshot attuale; coda review da.05
ora collegata al detectorcall produzione, metadata review_only espliciti.
Laplace promptv3/schema2: presenza separata da classe e cancellazione solo con
categoria fisica esplicita, fallback conserva geometrie. Nessuna nuova API pagata.

Utente prioritizza rilevare tutti i corpi fisici, mantenendo sconosciuti quelli
non classificabili; nessun cambio modello. Metadati del file ONNX verificati:
21 classi elettroniche, input640, SHA256
bfc398bb93ce61d9d7d00389db51b651bd90c4e2141163a0ae6dad79a247ae29.
Non dimostrano accuratezza sui package. Agenti attivi: Feynman detector/audit,
Laplace contratto verifica globale, Bernoulli normalizzazione package/matcher.
Screenshot utente mostra due piccoli corpi senza box; causa ancora da provare.
generated risulta vuota nella verifica corrente: non dichiarare ispezionato
l'audit della nuova esecuzione. Precedente diagnosi package descrittivi da
confermare con replay disponibile. Commit e push sul branch YOLO-SOLUTION
richiesti e autorizzati, ancora NON eseguiti; escludere segreti e artefatti.
Ultima suite completa resta quella documentata sotto, non quella delle
correzioni attualmente in corso.

## Integrazione candidati interattivi — completata, pronta per prova utente

Branch YOLO-SOLUTION, HEAD 2ffc123, worktree preesistente sporco da preservare.
Utente richiede integrazione app: JSON fino a5 modelli plausibili, revisione
grafica con scelta/nessuno e rotazione persistenti, prompt globale aggiornato,
default anthropic/claude-opus-5. Agenti Einstein matcher, Cicero dialogo,
Linnaeus inventario globale; coordinatore integrazione/persistenza/test.
Nessun commit/push. Default Opus aggiornato anche nel DB reale via ProfileStore,
nessun override per tipologia presente. Inventario globale integrato nel ramo
YOLO; observe_terminals automatico escluso in questa modalita' (resta manuale).
Scelte/report passano da foto a tabella/ComponentBox/CAD e sono salvati in JSON
con hash foto e chiave geometria/package; cambi geometria invalidano le scelte.
Dialogo candidati con ritaglio e STEP thumbnail reale; doppio clic foto apre la
scelta, bottone revisione ora visibile anche senza Advanced. Integrazione click
3D completata (tasto destro), inclusi solidi/serigrafie con source_row originale
preservato quando altre righe sono escluse. Una scelta confermata con rotazione
null usa inferenza assi nativa;0 e' override esplicito. Report JSON danneggiati
non bloccano anteprima. Import STEP fallito -> segnaposto magenta + stato
import_failed nel report e avviso GUI; aprire/annullare non cancella l'avviso.

Verifica finale:2195 passed,2 xfailed noti,19 subtests (138.16s), poi52 test
mirati sulle ultime correzioni audit (5.89s). Include generazione FreeCAD reale
con modello confermato, colori/geometria verificati, e file STEP corrotto.
Primi run rossi erano contratto autoattach obsoleto + regressioni ora corrette.
git diff --check pulito al controllo intermedio. Nessuna chiamata AI pagata
durante integrazione, nessun cambio branch/commit/push.

Replay offline risposta Opus v2: controllo link piu' rigoroso trova candidato33
incoerente;50 corpi+1 proposta irrisolta conservata=51 ipotesi,33 con candidati.
Non e' una nuova analisi ne' prova di accuratezza. Regolatore offre SOT-223;
SOT-223-8 escluso per terminali incompatibili. Non promettere tutti i corpi
corretti: foto/bbox/angoli restano ipotesi e WROOM-1U non ha STEP preciso locale.
Il nuovo flusso richiede conferma manuale per i match dimensionali; fino a
conferma le geometrie sono segnaposto, anche se un candidato e' unico.

Guida:docs/interactive-kicad-models.md. Ripartire con python main.py; default
Opus e YOLO. Rivedi foto/scegli modelli -> doppio clic su corpo -> seleziona
STEP o nessuno, rotazione -> Applica correzioni -> Rigenera modello 3D.
Stato su disco kicad-model-choices.json, report kicad-model-candidates.json
nella cartella componente. Restore solo foto/geometria/package identici,
nessun riutilizzo per geometria duplicata ambigua. Crop/prompt/risposta audit
in generated/photo-cache/yolo-audit. Screenshot dialogo reale verificato nel
workspace chat outputs/app-candidates-replay/candidate-dialog.png.

Prova standalone v2 completata: Opus50 componenti,37 candidati dimensionali,
13 irrisolti,0.459675USD. Regolatore ora SOT-223, candidato STEP con erroreXY21%,
non identita' elettrica certificata. Prompt e output in workspace chat
outputs/ESP32-global-inventory-Opus-v2. Precedente CAD Opus generato con38 STEP
e14 placeholder,10 sovrapposizioni; WROOM-1U placeholder. Correzione rispetto
alla sezione storica seguente: CAD standalone e seconda prova sono avvenuti,
ma non erano ancora integrati nell'app prima di questa richiesta.

## Confronto modello Opus — completato

Utente richiede modello migliore. Catalogo live OpenRouter conferma
anthropic/claude-opus-5 con immagini e tools, prezzo 5/25 USD per M token.
Stessa richiesta globale (63 candidati,5 immagini,prompt/schema invariati),
output separato outputs/ESP32-global-inventory-Opus; una chiamata autorizzata.
Input JSON/prompt/schema identici verificati byte per byte. Opus:52 componenti,
63 decisioni senza errori strutturali, costo0.419895USD,108.07s. Sonnet45,
0.162124USD,86.07s. Matcher identico:38 candidati dimensionali Opus contro20;
conflitti angolo/bbox nel controllo grossolano1 contro12 (non validazione CAD).
Opus propone2transistor, mantiene WROOM; U4 ancora ic con evidenza crystal/
oscillator incerta,14matching irrisolti. Non prova accuratezza dei52 oggetti.
CONFRONTO.md,comparison.json,RISULTATI.md,MATCHING-KICAD.md salvati nella
cartella output Opus. Nessun cambio del modello predefinito app, nessun CAD.

## Matching standalone JSON globale — completato

Richiesto utente prima del CAD. work/map_global_inventory.py usa misure del
matcher KiCad con prefiltro esplicito famiglia/package (no substring generiche,
no array per singoli passivi, no fusibili/distanziali). Confronto rispetta
bbox_basis body o body_and_leads; tolleranza preesistente 30%. 27 STEP distinti
esaminati, 20/45 candidati dimensionali, 25 irrisolti; non sono 20 identita'
certificate. Nessun S3-WROOM-1U nella libreria RF installata (presenti 1 e 2,
non sostituiti). 12 angoli AI confliggono con asse lungo bbox; rotazione KiCad
resta null. Output chat outputs/ESP32-global-inventory/kicad-matching.json e
MATCHING-KICAD.md. Nessuna modifica funzionale app o nuova API pagata; CAD non
assemblato: richiesta corrente era provare prima il mapping del JSON.

## Prova inventario globale unico — in corso

Utente autorizza una richiesta unica con JSON YOLO, classe/package/rotazione e
relazioni. Script standalone nel workspace chat work/global_inventory_trial.py;
output outputs/ESP32-global-inventory. Modello sonnet-5 come precedente prova,
63 candidati ricreati localmente (generated era stato svuotato), 5 immagini:
foto, overlay e tre pannelli. Quote 63.3x25.4 riprese dalla precedente analisi
e dichiarate stimate. Schema impone decisione per ogni ID e riferimenti a
componenti finali. Nessuna integrazione app o generazione CAD in questo test.

## Audit risultato 20:05 — difetti riprodotti, non corretti in questo audit

Branch YOLO-SOLUTION HEAD 2ffc123. Utente richiede diagnosi con agenti esperti.
Boole verifica omissione WROOM: coverage restituisce solo box [590,1010,660,1070]
fuori 0..1000, scartato; review elimina #2/#23/#35 come frammenti di un modulo
"presente altrove", ma nessun modulo sopravvive. apply_review elimina present=false
high senza ID sostitutivo o controllo globale. FreeCAD riceve gia' 51 componenti.
Cilindro identificato nel freecad.log: Cmp25_capacitor_30 importa
Mounting_Wuerth_WA-SMSE-ExternalM3_H6mm_9771060360.step. Ricerca 0603 per
sottostringa intercetta il codice 9771060360; famiglia Mounting_Wuerth non
riconosciuta, filtro non pone veto; confronto body 3x3 accetta misura 2.49x2.82
pur importando ingombro totale 6x6. Averroes conferma nel mesh un solido
6x6x12.5mm e path distanziale (H6 nel nome non e' l'altezza totale).
Altri importi errati: capacitor51 -> Fuse_0402; resistor19/45 -> R_Array.
size_distance sceglie body/total/lands senza rispettare bbox_basis; famiglie
sconosciute ammesse anche da test esistente. Audit completati, nessuna patch
funzionale in questo turno diagnostico; occorrono regressioni su questi casi.
Nessuna nuova inferenza pagata, nessun file generato sostituito durante audit.

## HTTP 400 copertura — causa riprodotta e corretta

Prova isolata autorizzata su docs/ESP32/AltaRisoluzione.jpg: PNG base64
11904936 bytes rifiutato dai provider (limiti 5/10 MB). OpenRouter esponeva
solo ultimo errore Azure Content-Length, occultando i precedenti in metadata.
Ora image_block comprime immagini oltre 5 MiB base64 in JPEG, prima di ridurre
eventualmente risoluzione; immagini piccole restano PNG lossless. Prova reale
con stessa foto e schema: 2553639 caratteri URL, 3487x1391 invariati, SUCCESS.
Probe usa due copie foto invece dell'overlay e verifica accettazione API,
non accuratezza o completezza inventario. Log errori ora include categorie
metadata.raw/previous_errors, limiti numerici e dimensioni richiesta/immagini.
Nessun contenuto immagini o chiave registrato nei log applicativi.

## Moduli castellated senza altezza complessiva

Rimosso su richiesta il blocco automatico body_height_mm per castellated:
il modello usa spessore PCB e componenti. Eliminato anche il confronto finale
con altezza estratta assente; editor inizializzato allo spessore PCB anziche'
riutilizzare un'altezza precedente. Dimensioni XY e validazioni dei corpi
non castellated conservate. Verifica: 25 test automatic-generation/documentation
passati. Nessuna nuova analisi pagata eseguita.

## Fix numeri pad duplicati — completato

Su YOLO-SOLUTION: first_pin espliciti sovrapposti passavano il parser e
fallivano soltanto dopo tutta l'analisi foto. Ora il parser verifica gli
intervalli globalmente, riserva i numeri espliciti prima di assegnare quelli
mancanti e richiede una sola correzione AI sui documenti prima della foto.
La correzione importa solo first_pin/reverse, preservando geometria e sigle;
il generatore continua a rifiutare duplicati. Nessuna nuova API reale eseguita.
HTTP400 e matching package restano aperti, fuori da questo fix richiesto.
Verifica mirata: 108 test passati, inclusa correzione riuscita e secondo
tentativo ancora duplicato (rifiutato senza ulteriori chiamate).

## Semplificazione AI, prompt crop v3 e retry HTTP — completati

Branch attuale `YOLO-SOLUTION`; nessun commit o push. La schermata principale
usa ora un solo `Modello AI` per analisi documenti, foto e verifica dei crop;
YOLO resta il localizzatore locale separato. I campi di configurazione legacy
per modello foto/verifica restano nei dati per compatibilita', ma il runtime
della nuova analisi usa sempre il modello unico selezionato.

I documenti guidati sono ora soltanto pinout, immagine quotata e foto ad alta
risoluzione. Eliminati il quarto ruolo e i relativi testi, traduzioni, prompt,
test e documentazione. Le altezze non deducibili vengono richieste come misura
diretta.

Prompt revisione YOLO aggiornato a `yolo-package-review-3` e riallineato al
test standalone Opus v3: misure calibrate prima del package, distinzione corpo /
terminali, end-cap inclusi per i passivi, marcature solo letterali, package vuoto
se incerto. Corretto il difetto principale: le note profilo dell'intera board e
le istruzioni utente sul modulo non vengono piu' accodate a ogni singolo crop,
evitando conflitti come "tratta il modulo RF come un solo oggetto" durante la
classificazione dei componenti montati.

`OpenRouterError` espone ora `retryable`. Gli HTTP 400/408/409/429/5xx previsti
generano un solo retry applicativo, sempre visibile nel log con
`AI_CALL_RETRY ... attempt=2`; il secondo tentativo usa `tool_choice=required`,
piu' compatibile fra provider. Un secondo errore termina esplicitamente con
`AI_CALL_FAILURE ... attempts=2`; errori di autenticazione/credito e risposte
strutturalmente non valide non vengono ripetuti. Nessun loop e nessuna chiamata
AI reale eseguita durante questa modifica.

Verifiche: suite completa prima dell'ultima pulizia 1997 passed, 2 xfailed e 19
subtests; suite finale mirata dopo la rimozione documentale 282 passed.
`git diff --check` senza errori (soli avvisi CRLF del working tree).

## Prova talisma YOLOv5 — pesi assenti, test non eseguito

Utente autorizza prova prima sulla propria foto, senza integrazione app.
Branch PCB-CD invariato. Verifica GitHub API: best.pt e last.pt sono ENTRAMBI
0 byte, blob e69de29bb2d1d6434b8b29ae775ad8c2e48c5391. Anche unico commit
storico per best.pt (ec1b16f,17/08/2022) contiene0byte. Nessuna release trovata.
Issue2 'Weights missing' conferma segnalazione pubblica. Corregge nostra precedente
affermazione 'pesi disponibili': erano soltanto nomi file, non checkpointusabili.
Non installato/eseguito modello; nessuna foto inviata, nessun costoAI, nessuna
modifica pipeline. Serve checkpoint effettivo o sceltaaltrorepo (SRPCB_GUI ha
asset reale158MB, ma modello alternativo non ancora eseguito).

## PCB-CD — verifica preliminare, premessa tecnica smentita

Branch attuale PCB-CD, creato da YOLO-SOLUTION su richiesta; HEAD2ffc123,
modifiche non committate conservate. Utente propone integrazione repository
s39674/PCB-Component-Detection descrivendolo come Faster R-CNN e autorizza agenti.
Lettura diretta upstream train.py/predict.py: NON Faster R-CNN. CNN con due
Conv2d e tre Linear, immagini32x32,20classi; predict.py legge boundingbox da
CSV preesistente. Nessuna RPN o regressione boundingbox nel modello indicato.
Fonti https://github.com/s39674/PCB-Component-Detection e raw main/train.py,
main/predict.py. Audit indipendente Descartes conferma: peso.pth presente248KB
per classificatore,20classi senza background, nessuna licenza esplicita trovata
per codice/pesi. Contenuto checkpoint e accuratezza non verificati.
Nessun codice esterno eseguito, nessun peso deserializzato, nessuna API pagata.
Nessuna integrazione app implementata: scegliere fra classificatorePCB-CD su
ritagli gia localizzati e ricerca di un vero detectorFasterRCNN con pesiPCB.
La descrizione utente non dimostra superiorita geometrica; necessaria prova.
Test non rilanciati: solo verifica read-only e questo aggiornamento handoff.

## Default GUI YOLO — richiesto e applicato

Su richiesta utente ogni nuovo avvio seleziona YOLO locale + verifica AI.
Supera il precedente default AI: nessun cambio ai modelli LLM e nessun intervento
sull'analisi gia in esecuzione. Scelta AI resta disponibile; preflight invariato.
Default dei servizi Python lasciati compatibili; GUI passa scelta esplicita.
Branch YOLO-SOLUTION; 108 test GUI/inventario/documentazione passati in41.33s.

## Avvio semplice python main.py — completato

Utente chiede comando standard senza attivazione manuale. Causa verificata:
app avviata con Python313 globale, privo di onnxruntime; .venv ha runtime1.30.
main.py ora rilancia con .venv prima degli import GUI/dotenv, preservando args,
cwd, ambiente e exitcode. Guardia contro cicli; nessuna installazione globale.
--check-runtime verifica dipendenze senza GUI/.env/API. Branch YOLO-SOLUTION.
7 test launcher/documentazione passati. Prova reale `python main.py --check-runtime`
passata: rilancio nel .venv, PyQt6/PIL/cv2/pyvista/pyvistaqt/numpy/onnxruntime OK.
Nessuna nuova GUI aperta e nessuna API invocata. Suite completa non ripetuta per
questo solo cambio entrypoint; precedente suite1987passed2xfailed19subtests.

## Integrazione YOLO nell'app + anteprima FreeCAD — completata, da provare con AI reale

Branch rinominato su richiesta esplicita utente: FIX-WITH_FREECAD -> YOLO-SOLUTION.
HEAD invariato 2ffc123; modifiche locali preservate, nessun commit/push.
Suite finale: 1987 passed, 2 xfailed, 19 subtests in137.83s; ulteriore controllo
overflow overrideCAD 7passed. Audit indipendente347passed: tutti5rilievi risolti
(copertura involucroespanso, rifiutiAIincompleti, overflow, cachebboxinvertiti,
NaNnegli audit). GUIroundtrip123passed. Agenti conclusi echiusi.
App pronta per riavvio e selezione YOLO locale+verificaAI. Prova AI reale ancora
non eseguita: il consenso autorizza il flusso futuro nell'app, non dichiara
verificata la precisione finale. Preview/output reali eguidaITALIANA in
chat outputs/ESP32-YOLO-FreeCAD/INTEGRAZIONE.md. Nessun modello pagato imposto.

Blocco egress RISOLTO: utente ha esplicitamente autorizzato overview/ritagli a
OpenRouter quando seleziona YOLO+verifica AI e avvia analisi. Modulo
photo_yolo_review.py implementato dopo consenso. Nessuna nuova chiamata pagata
eseguita in questo turno: prove API con mock, CAD con inventario revisionato.
Prima suite completa: 1947 passed, 2 xfailed, 19 subtests (143.15s).
Successive aggiunte in verifica: controllo AI di copertura prima dei ritagli,
audit univoci con prompt/risposte, override nativo rotazione/Z e sporgenza di
connettori con mating=edge. 17 test revisione/doc e 27 test CAD/doc passati.

Smoke detector reale sull'attuale prepare_photo: 3487x1391, 30 viste, 6.827s,
63 PROPOSTE (non 63 componenti), 2265 proposte grezze registrate. Modulo RF
intero sotto soglia (~0.176) e frammenti/FP presenti. Per questo aggiunta una
verifica globale AI dei corpi mancanti, non abbassata soglia sul singolo caso.
La nuova verifica globale non e stata ancora valutata con chiamata reale.

Demo FreeCAD completata sotto chat outputs/ESP32-YOLO-FreeCAD: 43 elementi,
41 candidati dimensionali STEP da14file e due segnaposto (WROOM-1U ciano,
LED2020 magenta). STEP/FCStd riaperti validi; volumi dei41importi invariati,
orientamento terminali verificato geometricamente. NON41identita elettroniche.
Demo usa annotazioni e orientamenti revisionati, NON nuova analisi autonoma.
Integrazione non implementa omografia manuale del demo; calibrazione app resta
quella esistente. Guida nuova docs/yolo-photo-workflow.md.

GUIagentAmpere completato: sceltaAIdefault/YOLO+verificaAI eONNX session-only,
preflightprimadiAPI. 79testGUI/documentazione passati. RuntimeONNX1.30installato
.venv; pesi copiati localmodels ehashverificato, ignoratiGit.

Nuova autorizzazione utente: integrare approccio locale nell'app e mostrare3D.
Contratto photo_detector='ai' defaultcompatibile oppure'yolo', yolo_model_path
opzionale. Preflightlocale prima diAPIpagate. ModelloONNX locale repo/models/
pcb_yolov8s.onnx (da ignorareGit, no ridistribuzione finchelicenzaMIT/AGPLambigua).
Agenti file-disgiunti: Goodall photo_yolo.py/tests/requirements-yolo;
Ampere main_window/app_config/UItests; Helmholtz solo script/output demoFreeCAD.
Main gestisce datasheet_extractor/photo_inventory/revisioneYOLO/docs/testintegrazione.
Replaydemo43usa annotazioni revisionate, NON nuova estrazioneAIautonoma.
Nessuna nuovaAPIpagata prevista in turno; futuro clickapp usa verificatoreAIconfigurato.
Nessun commit/push/installazioneKiCad globale. BranchFIX-WITH_FREECAD HEAD2ffc123.

## Matching inventario YOLO -> KiCad — prova conclusa, non integrata

Consegna: outputs/ESP32-KiCad-matching/CONFRONTO.html (43schede foto+renderSTEP),
CORRISPONDENZE.md (tabella43 misure/assi/candidati), matching.json (alternative e
provenienza), matching.log/freecad-measure.log. Tutto nel workspacechat corrente.
Esito finale:41/43 passano filtroXY nelle famiglie selezionate visivamente;
59STEP effettivamente validi misurati,15modelli distinti renderizzati. Proposte
passivi:9x0402 e20x0603;R27 resta vicino anche0805. Non41identita certificate.
8test sperimentali passati;92test test_kicad_model_compatibility.py+
test_kicad_model_footprints.py passati in134,49s (ultimo scansiona libreriareale).
Nessuna suite completa app eseguita: nessuna modifica codiceproduzione in turno.
Noether audit read-only completato echiuso. Risolti: geometrie YOLO/revisionate
separate in JSON, poligoni/centri coerenti; V010 corpo revisionato invece di
spanterminali tagliato; ambiguity rinominata score_tie; package_uncertain sempre
vero; SOT/TSOT altezza ignota esplicita; cristalli2/4pin ammessi conteggioignoto;
coordinateframe esplicito e kicad_rotation_deg=null; associazioni multiple ai
footprint preservate. Immagini galleria di geometrieSTEP reali in grigio, non
montaggio board ne colori reali. Nessun nuovo STEPassemblato o installazione.
Aperto richiesto nel percorso complessivo: validare varianti/quote incerte,
recuperare STEP esatti mancanti WROOM1U/LED2020, verificare assi/pin1/mounting prima
di applicare modelli al layout FreeCAD. App non modificata; zeroassegnazioni.

Prima analisi completata: 43elementi,41con almeno candidato STEP entro filtro
dimensionale30% preesistente;58STEP letti validi conFreeCAD1.1.1. NON41identita
verificate: classificazione/bodybounds assistente in visual-observations.json.
7casi a pari merito geometrico; altezze/EP/pin1 non osservabili. SOT/TSOT differenze
di sezione65% non provano altezza: non promuovere vincitore numerico aidentita.
Calibrazione4fori manuali+omografia sperimentale, controllo passo2,542/2,533mm.
WROOM1U eLED2020 hanno footprint maSTEP assente. Non sostituire1U con1.
Correzione: regioni non numerate sottoIC/a sinistraquarzo erano gia escluse dal
riferimento come possibili piazzole non popolate; NON componenti mancanti provati.
Log live matching.log efreecad-measure.log; matching.json conalternative/quote/hash.
Stato iniziale58STEP superato da59 dopo inclusione cristalli2pin. App non modificata.

Branch FIX-WITH_FREECAD, HEAD2ffc123, worktree sporco preservato. Utente autorizza
prova sui43ritagli: calibrazione con Dimensioni.png e fori della foto, famiglie/
terminali, ricerca STEP locale e report perID con incertezze. Output isolato nel
workspacechat outputs/ESP32-KiCad-matching; non installare librerie ne sovrascrivere
modelloapp. Nessuna nuova AIpagata prevista. Suite nuova non ancora eseguita.
Dimensioni.png indica63,30x25,40, fori a1,80 dai bordi (59,70x21,80 fra centri).
La foto quotata e una variante diversa: verificare scala tramite passo contatti,
non usare la disposizione componenti di quella foto come prova del package.
Riferimento43 non esaustivo; non dichiarare completata identificazione di tutti.

## YOLO nuovo riesame multiscala/rotazioni — concluso, NON completo

Inferenza 708 viste completata in137,7s. A conf0,25:123proposte,
41abbinati/2mancanti/82extra aIoU0,5 contro43annotazioni assistente.
Consenso multivista64proposte:40abbinati/3mancanti/24extra. Non migliora precisione.
work/review_yolo_inventory.py seleziona43osservazioni usando il riferimento:
REVISIONE ASSISTITA, non43rilevamenti autonomi verificati. QA4contactsheet eoverlay.
Overlay finale mostra ulteriori piccoli elementi non numerati sottoICgrande e
a sinistra quarzo: riferimento43 NON esaustivo, non dichiarare trovati tutti.
Boxcorpo non sempre terminali completi (V025 aletta esclusa). Identita/package
non verificati, WROOM1U indicato utente; associazioniKiCad0. Nessuna modificaapp.
Report outputs/ESP32-YOLO-riesame/ESITO.md con lista43, overlay eJSONprovenienza.
Script riesame supporta --replay dopo fix riferimenti circolari NMS shallowcopy.

### Ricetta del riesame

Utente chiede rielaborare con YOLO per estrarre tutti i componenti visibili.
Script isolato work/reprocess_yolo.py, sessione57615; outputs/ESP32-YOLO-riesame.
Stessi pesi ONNX locali.177crop/708viste: overview e320/640/1280px, overlap25%,
rotazioni0/90/180/270. Soglie prefissate0,10proposte/0,25primaria; NMS0,5.
Consenso almeno2crop econf>=0,25 marcato ipotesi supportata, NON verifica fisica.
Mai cancellare candidati incerti solo per mancato consenso. Test inversione
rotazioni/coprertura griglia/NMSvicini passati. NoAIpagate, noapp/CAD/library edits.
Previsto confronto riferimento dopo inferenza e QAcontatti; non promettere
completezza sulla base del conteggio. LibrerieKiCad ancora nessuna nuova associazione.

## Grounding DINO + SAM — conclusa, NON promossa (17:02)

Completate41visteDINO e75maschereSAM su47contesti. CPU: DINO419,7s,
SAM456,8s (~14,6min esclusi setup/download), nessuna API pagata/foto inviata.
Confronto box IoU>=0,5 contro43annotazioni assistente: DINOcombinato0,25
75proposte/9abbinati/34mancanti/66extra; YOLOcombinato0,25 52/41/2/11.
Box derivati dalle75maschere: ancora9abbinati/34mancanti/66extra; non e
metrica di accuratezza maschere (nessuna groundtruthpixel). QAoverlay conferma
fori/sfondo/porzioniPCB scambiati per corpi. Non adottare questa configurazione.
Prova Tiny+ViTBase con prompt congiunto Transformers, NON esatta replica wrapper
Autodistill percaption, NON SAM2. Non generalizzare risultato a tutti GroundedSAM.
Report CONFRONTO.md, overlaybox/maschere,75PNG, raw e log live completi sotto
outputs/ESP32-test-GroundedSAM nel workspacechat. Metadata revisioni/hash presenti.
Script corretto dopo run solo per warninglabels e replaycache senzadelfvariabile
noninizializzata; nessuna modifica risultati. Nessuna associazioneKiCad/CAD/app.

Runtime isolato pronto: torch2.14.0+cpu, transformers4.57.6. GPU AMD integrata,
CPU4thread. Pesi safetensors ufficiali scaricati, revisioni in
work/grounded-sam-models/revisions.json. Foto non inviata. Sessione exec8860,
script work/test_grounded_sam.py; output outputs/ESP32-test-GroundedSAM.
Al16:53 DINO30/41viste processate, ~10s/vista. Log live inference.log;
risposte pervista in detections/. SAM seguira sui candidati combinati0,25.
Prompt congiunto12categorie, non loop Autodistill percaption. NMS/coordinate/
copertura tile smokecheck passati. Reportgenerator work/report_grounded_sam.py.
Non dichiarare successo/comparazione finale prima di risultati e QAvisuale.

Utente chiede eseguire pipeline Grounded-SAM sulla foto ad alta risoluzione.
Prova isolata work/grounded-sam-env e pesi ufficiali HuggingFace in work; nessuna
API pagata, foto solo locale, nessuna modifica pipeline app. Pianificata versione
Transformers Grounding DINO tiny + SAM ViT-base (non SAM2, non wrapper Autodistill),
con crop per SMD e ontologia esplicita; report deve dichiarare modelli/limiti.
Abbinamento KiCad richiesto prima ma NON eseguito: solo disponibilita candidati
verificata (SOT23/SOT223/R/C0402/0603); modello esatto WROOM1U non trovato locale.
Non confondere41localizzazioni YOLO con41modelli KiCad associati: associazioni0.

## Prova locale YOLOv8s conclusa — promettente, NON integrata

Esito16:36:41viste CPU in7,04s, costoAPI0. A conf0,25 overview+tile52proposte,
41/43abbinamenti IoU>=0,5 contro riferimento visivo assistente,2mancanti,11extra.
Solooverview40proposte/35abbinati; solotile42/35. A conf0,10 combinato64/42,
22extra: non adottare soglia bassa come soluzione. Riferimento NON BOM certificata.
Due transistor campione localizzati/classificati transistor; anche un due-terminali
falsamente transistor. Mancanti V005/V006 piccoli passivi sinistra IC grande.
Numerosi SMD rilevati; quarzo classificatoIC, RF localizzatoIC non WROOMidentificato.
Report/foto/log/raw/metriche: outputs/ESP32-test-YOLOv8s nel workspace chat.
Script work/test_pcb_yolo.py; smoke test NMS/letterbox/copertura passati.
Nessun codice app o predefiniti cambiati; suite app non rilanciata.
Runtime isolato infine include-system-site-packages=false, dipendenze solo nel
venv esperimento. ModelcardMIT vs metadatiONNX AGPL3: chiarire prima distribuzione.

Utente chiede provare Arshia82sbn/pcb-yolov8s-detection con attenzione agli SMD.
Autorizzata prova locale, NON integrazione app o addestramento. Scaricati metadata
HuggingFace e best.onnx sotto Documents/Codex/2026-09-19/new-chat/work/pcb-yolo-model.
Runtime separato work/pcb-yolo-env; foto resta locale, nessuna API a pagamento.
Previsto confronto overview e tile nativi, esclusione pads/pins dal conteggio corpi,
QA su passivi SMD e transistor. Risultati ancora da verificare, nessuna promessa
su 0402/package nominali; le 21 classi del modello sono famiglie, non package.

## Pausa richiesta utente — valutazione detector specializzati (dopo run 16:26)

Branch verificato FIX-WITH_FREECAD, HEAD 2ffc123; nessun commit/push.
Utente ha chiesto agenti per inventario TOP, poi ha fermato il lavoro mostrando
repository YOLO. Fermate nuove modifiche applicative e chiamate pagate; nessun
detector scaricato/installato. Verifica fonti: SanderGi/PCB-Detection rileva la PCB
intera, NON i suoi componenti; amraskar/PCB-Defect-Detection rileva sei tipi di
difetti del rame/fori, NON componenti. talisma-cassoma propone realmente YOLOv5
per componenti, ancora da valutare per pesi, licenza e copertura sul campione.

Lavoro sperimentale presente, NON integrato nella GUI: photo_top_inventory.py
(overview + tile nativi + review + ricerca omissioni) e photo_detection_contract.py
(agente Socrates: trasformazioni, core/halo, validazione conservativa ID/parent).
Test del primo:17 passed insieme a documentation; agente riporta289 passed per
contratto/documentation/regressioni. Suite completa NON rilanciata in questa fase.
Run Opus5 completata:56chiamate,1,3225USD,78 candidati mantenuti,28 scarti,
0stadi falliti. NON successo qualitativo: duplicati, pin modulo contati come corpi,
aletta regolatore separata. Non promuovere nel percorso standard.
Output e log live: Documents/Codex/2026-09-19/new-chat/outputs/ESP32-inventory-strategy/run-1.
Riferimento indipendente agente Turing:43 corpi annotati in reference-reviewer.json,
NON BOM/ground truth certificata. Le metriche vanno lette dal suo report se presente.
Sorgente run: docs/ESP32/AltaRisoluzione.jpg (4000x1848); vecchia generated/.../hires.jpg
non era piu presente. Non ricreare/ripristinare dati generated senza richiesta.
Richiesto ma aperto: strategia affidabile completa TOP e successivo tipo/package/CAD.
Non autorizzato da questa pausa: installazione/addestramento di nuovi detector.
Preservare tutte le modifiche pregresse. Non presentare il conteggio come recall.

## Concluse prove prompt Opus v2/v3 — NON promosse (16:13)

v3:38 proposte,0 box locali fuori limite,2chiamate parallele,0,2795USD. Risolto
spostamento sistematico v2, ma ID10 corpo potenza interpretato header con bbox
parziale; ID36-38 header non confermati/null box; omessi piccoli passivi.
Nessuna promozione dei prompt nell'app, nessuna modifica codice app o predefiniti.
Costo iterazione v2+v3=0,7389USD. Registro finale outputs/ESP32-test-Opus-v3/TRACCIA.md,
prompt e raw per versione, QA visuale annotata. STOP retry a pagamento per questa
iterazione. Non presentare più candidati come miglioramento o inferire completezza.

Aggiornamento16:11: v2 fallita qualitativamente:47 proposte,0,4594USD,166,97s,
coordinate parte superiore sembrano in scala overview0,6; bbox formalmente validi
ma su PCB vuoto/serigrafie. NON promossa. Traccia conservata STORICO-PROMPT.md.
v3 in corso, due chiamate indipendenti UN crop nativo ciascuna, coordinate LOCALI,
conversione fullframe in codice. Prompt più breve; confronto cambia metodologia,
non sostenere che si misura effetto solo-prompt. Output ESP32-test-Opus-v3.

Utente chiede forte miglioramento prompt e tracciabilità. Creata prova sperimentale
separata ESP32-test-Opus-v2: prompt-v2.txt, STORICO-PROMPT.md, metadata con hash
prompt/foto, risposta e log. Stesso modello/foto/crop/schema/20k token della v1;
prompt v2 distingue proprietà corpo/terminali, scala indipendente esplicita,
limiti coordinate, body/lead/pad, pacchetti condizionati alle quote e orientamenti.
Nessun conteggio/ID corretto v1 dato al modello. Prompt app ancora invariati in
questa iterazione, finché la prova non è valutata. Script rifiuta sovrascrittura
risposte pagate e report-only non modifica metadata; scarti v1 non trasferiti a v2.

## Prova Opus 5 — inventario completo richiesto (16:03)

Utente non vuole Astra; autorizzata prova Opus con elenco, orientamenti, quote/package.
Una chiamata `anthropic/claude-opus-5`: overview + 3 crop original-pixel, inventario
TOP completo richiesto. 46 proposte, costo 0,3171 USD, 102,8s. Risposta conservata.
QA visivo: ID27/28/29 sono piedini ID7, ID30 sua aletta, ID46 zona vuota: 5 scarti.
41 candidati residui NON sono distinta verificata; mancano alcuni piccoli passivi.
ID1 modulo bbox oltre larghezza foto (4230>4000): quote invalide, non clampare.
J3Y letto; SOT23 proposto per coppia; SOT223 grande. Box della coppia troppo larghi
(includono probabilmente piazzole), LED RGB nominale proposto incompatibile con bbox.
Report evidenzia discrepanze corpo/nominale >30%, non forza match KiCad.
Orientamento è asse modulo180, NON rotazione KiCad/pin1; altezze non misurate.
Scala locale derivata indipendentemente con OpenCV: primo/ultimo di 12 centri
contatti x543,5..2072,5; 11 passi da2,54mm =>54,72px/mm. Scala isotropa resta
approssimata; passo coerente con disegno (63,30-5,05-4,91)/21.
Output Documents/Codex/2026-09-19/new-chat/outputs/ESP32-test-Opus/INVENTARIO.md,
inventario-numerato.jpg, response.json, request-metadata.json, analisi-ai.log.
Primo rendering report fermato da validazione bbox: risolto localmente segnalando
quote NON VALIDE, nessuna seconda chiamata AI. Nessuna modifica app/predefiniti/CAD.

## Confronto DeepSeek richiesto dall'utente (15:56)

Provato ultimo modello vision disponibile `deepseek/deepseek-v4.1-flash`, stessa
foto/prompt/schema/detail della prova Astra. Primo tentativo 10k: finish_reason=length,
10k token tutti reasoning, 147,9s, 0,004462 USD, nessuna risposta. Un solo retry
a 20k: successo, 13 voci, 177s, 0,005965 USD. Totale 0,010427 USD.
Le 13 voci NON significano quattro componenti reali in più rispetto ai 9 Astra:
campionamento non fissato, elementi fuori obiettivo e coordinate errate per i
candidati transistor; J3Y non letto; SOT-223 rimane ambiguo. Astra più utile in
questa prova mirata, non benchmark generale. Nessun modello salvato modificato.
Report: Documents/Codex/2026-09-19/new-chat/outputs/ESP32-test-DeepSeek-20k/CONFRONTO.md.

## Completato — prompt marcature e prova mirata 0402/transistor

Nuova richiesta esplicita: usare le marcature visibili come evidenza per tipo/package.
Il vecchio divieto di lettura marcature era troppo restrittivo. Aggiornato il prompt
di verifica: testo osservato distinto da ipotesi in `package_evidence`, numero e
disposizione terminali, corpo distinto da piazzole/saldature/serigrafia PCB, nessuna
identità certa da una sigla ambigua. Migrazione in memoria delle due clausole legacy;
nessuna riscrittura dei profili salvati. Test mirati: **313 passed** (grounding,
verified_family, mechanical_metadata, documentation); diff --check pulito.
Primo lancio interrotto per errori setup nel percorso temporaneo non scrivibile:
rilanciare con --basetemp nuovo sotto Documents/Codex e -p no:cacheprovider.
0402 rifiutato dal resolver per incongruenza con rettangoli fotografici: NON prova
che il componente reale non sia 0402. Tolleranza non modificata.
Categoria `transistor` assente dallo schema: per ora il prompt conserva l'ipotesi
in evidenza e richiede generic + package meccanico, evitando falsi SOIC/diodi.
Prova isolata GPT-6 Astra su hires.jpg 4000x1848 detail=original, una chiamata,
autorizzazione esplicita utente a invio OpenRouter e costo. Log/risposta in
Documents/Codex/2026-09-19/new-chat/outputs/ESP32-test-Astra. Nessun cambio modello
predefinito. Nessuna rigenerazione CAD in questa prova.
Esito: una chiamata riuscita, 85,4 s, **0,3452 USD**. Due probabili transistor
SOT-23 (inferiore marcato J3Y), un SOT-223 con tre piedini e aletta, due passivi
campionati compatibili con 0402 e altri con 0603. Interpretazioni fotografiche,
non identificazioni commerciali certificate. La prova cambia anche il prompt:
NON benchmark controllato che dimostri superiorità del modello. Report ESITO.md.
Avvertenza: nella risposta diagnostica le quote `estimated_body_mm` dei passivi
riguardano il nucleo senza metallizzazioni; usare il chip completo per il package.

## Conferma utente successiva alla prova

Variante confermata esplicitamente: **ESP32-S3-WROOM-1U**. Non è più un'ipotesi.
Corretto il package del modulo nell'inventario diagnostico
`extracted-gemini-verification.json`, con provenienza `user_confirmed`.
Verifica dell'intera libreria 3D KiCad 10 locale: presenti S3-WROOM-1 e
S3-WROOM-2, **nessun modello S3-WROOM-1U**. Non sostituirlo con WROOM-1,
WROOM-32U o C3-WROOM-02U. Il CAD già prodotto resta un'approssimazione per
questo modulo; non è stato rigenerato né è stato aggiunto un modello esterno.
La conferma riguarda questo componente, non è una regola hardcoded del resolver.

## In corso — Codex, revisione completa e prova live autorizzate

L'utente ha autorizzato la correzione di tutti i punti aperti e la verifica
foto ad alta risoluzione → modelli locali KiCad → assemblaggio FreeCAD.
Quattro agenti hanno coperto risolutore libreria, importazione CAD, inventario
fotografico/revisione e footprint. Il coordinatore integra GUI, estrazione,
logging e prova completa. Nessun commit/push richiesto; albero MODIFICATO.

Diagnosi confermata: `package` veniva perso nella conversione AI → ComponentBox
→ tabella GUI → payload CAD. Inoltre il passaggio fotografico era limitato ai
castellated: una scheda THT con layout `matrix` poteva restare senza inventario.
Ora i metadati arrivano al risolutore e la famiglia `aggregate_tht` attiva
inventario e assemblaggio PCB anche senza componenti rilevati.

Implementati: confronto con inviluppi piazzole footprint, alias cristalli,
colori nativi STEP (marker `KICADGEN_STEP_COLORS_OK` evita recoloring distruttivo),
substrato THT forato e piazzole, contorni corpo footprint, diagnostica duplicati
senza eliminazione automatica, evidenze invalidate dopo revisione geometrica,
anteprima invalidata dopo modifica manuale. Etichette 3D disattivate per default.
`analisi-ai.log` ora riceve tutti i messaggi GUI con timestamp e flush immediato;
`freecad.log` riceve stdout/stderr in streaming durante la generazione.

Verifiche in corso: 281 test prima integrazione; 195 test CAD con vero roundtrip
STEP SOT-223 (tre colori conservati), 83 resolver e 49 revisione foto.
Prima suite completa: 1330 pass, 68 errori da fixture AST non aggiornata;
fixture corretta, relativi 68 test passano (2 xfail preesistenti).
Non usare questi conteggi come risultato della suite finale.

Prova live e confronto Gemini autorizzati dall'utente il 19/09/2026: output isolati in
`C:/Users/mirko/Documents/Codex/2026-09-19/new-chat/outputs/ESP32-verifica-live`.
Log leggibile durante la prova: `analisi-ai.log` nella stessa cartella.
I documenti sorgente e le preferenze reali non vengono modificati.
Il controllo credito ha risposto positivamente. Preferenze reali non modificate.

Risultati diagnostici IMPORTANTI:
- Gemini `google/gemini-3.1-pro-preview` ha localizzato 44 candidati. La verifica
  iniziale `anthropic/claude-sonnet-5` non forniva l'array richiesto. Un retry
  circoscritto ha fornito package ma confondeva molti passivi con SOD-123.
- I vecchi prompt salvati non ricevevano il supplemento meccanico. Ora si
  aggiungono sempre contratto JSON, famiglia visibile PRIMA del package,
  dimensioni calibrate per ID e conteggi terminali disponibili. Il prompt
  storico incompatibile viene corretto solo nella sua frase nota, non nel DB.
- Nel confronto Gemini il limite 4096 era consumato interamente dal
  ragionamento: `finish_reason=length`, nessuna tool call. Il limite della
  verifica è ora 16384; l'errore distingue troncamento da tool non supportato.
- Con il limite corretto Gemini ha restituito 44 candidati e 33 proposte di
  package, distinguendo molte famiglie di passivi. NON certifica identità o
  variante esatta. Per esempio il modulo RF proposto ha un'antenna diversa:
  non promuovere la sola corrispondenza di dimensioni a identificazione certa.
- La pipeline GUI → resolver reale → FreeCAD è stata eseguita sia con un
  fixture offline R_0603 sia con l'inventario fotografico. STEP/preview riportano
  provenienza `kicad_library` vs `approximation` e colori nativi.
- Aggiunti veto per famiglia/topologia/terminali incompatibili e controllo
  dell'ingombro TOTALE dei moduli su PCB: una sezione alta del solo schermo RF
  e le sole piazzole non certificano l'intero modulo.

Log completo: timestamp/flush per riga in `analisi-ai.log`, stdout/stderr CAD in
`freecad.log`, eventi AI_CALL_START/END/FAILURE con modello/tool/tempo/conteggi.
Anche errori e avvisi di generazione entrano nel dialogo Log AI e nel file;
rinomina senza docs conserva il log, fallimento del trasferimento blocca la
rinomina. Credenziali, immagini codificate e testo grezzo degli errori provider
sono esclusi dalla diagnostica; errori conservano codici/finish_reason sicuri.

Suite completa finale: **1643 passed, 2 xfailed, 19 subtests passed** in 102.57s.
JUnit salvato in `outputs/ESP32-verifica-live/test-results.xml` della chat.
Un passaggio precedente ha mostrato un intermittente nel pulsante Cancel in
francese (`test_i18n`): i 7 test i18n isolati e la successiva suite completa
sono passati senza modificare il codice i18n. Non nascondere questa osservazione.
Ultima protezione aggiunta dopo la suite: una famiglia IC verificata dalla foto
non autorizza terminali sintetici inventati; senza osservazioni si conserva
l'ingombro IC e la famiglia per il resolver. Due regressioni matrice/castellated
aggiunte, verifica focalizzata CAD/terminali/GUI separata: **27 passed** in 11.27s.
NON committato/pushato.

Prova reale finale completata: **29 modelli KiCad / 44 candidati**, 15
approssimazioni. Output STEP (5.27 MB), FCStd, VRML e preview presenti in
`outputs/ESP32-verifica-live/gemini/ESP32_Verifica_Gemini/` sotto la cartella
chat citata sopra. Colori nativi STEP verificati. Il nome diagnostico corto
evita i limiti di percorso di FreeCAD su Windows; i documenti rimangono intatti.
Rifiutati modulo RF sovradimensionato e QFN senza topologia disambiguata.
Costo complessivo registrato delle prove: 0.7841 USD su 20 chiamate.
Rapporto leggibile: `outputs/ESP32-verifica-live/VERIFICA.md`.

## Baseline storico Claude — prima delle modifiche descritte sopra

Branch **`FIX-WITH_FREECAD`**, allineato col remoto. Ultimo commit `e05724c`.
Baseline Claude precedente alle modifiche: **1235 passed, 2 xfailed**.

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
# 10:00 — fix preview 3D dei candidati KiCad verificato offline

Il difetto che lasciava USB-C, pulsanti, U1 e WROOM-1U magenta non era nel
rilevatore YOLO: `model_candidates()` produceva candidati validi ma li lasciava
tutti `review_required`, quindi `attach_models()` non impostava `model_path` e
FreeCAD costruiva correttamente il fallback sintetico. Il fix introduce lo
stato esplicito `provisional_matched`: il primo STEP viene usato per il preview
solo quando la presenza e la classe sono ad alta confidenza e almeno i primi
due candidati hanno la stessa famiglia e lo stesso ingombro XY entro il 5%.
Un singolo modello e' ammesso in preview soltanto quando il nome corrisponde a
un identificatore letterale nella marcatura (caso ESP32-S3-WROOM-1U). Lo stato
non equivale a una conferma: il report conserva tutte le alternative, il
warning UI invita alla revisione e il doppio clic continua ad aprire il picker.
Ambiguita' esplicite (`SOT23-6 / SC70-6`), topologie incomplete, componenti a
bassa confidenza e J2 senza candidati restano sintetici/non risolti.

Replay del JSON gia' salvato, senza nuove chiamate AI:
`generated/photo-cache/yolo-audit/global-inventory-0192402ad16544769111d433229af590.json`
— 54 componenti, 30 modelli KiCad agganciati: 24 `auto_matched`, 6
`provisional_matched`, 9 `review_required`, 15 `unresolved`. I componenti
richiesti risultano: U1 -> QFN-24 5x5, SW1 -> EVQP7A, SW2 -> TS-1088-xR020,
J1 -> USB4125, M1 -> `ESP32-S3-WROOM-1U.STEP`; J2 resta `unresolved`.

Prova FreeCAD locale reale: output temporaneo
`C:\Users\mirko\AppData\Local\Temp\kicadgen-provisional-ni7nt3kw\PROVISIONAL_PREVIEW.3dshapes\PROVISIONAL_PREVIEW.preview.json`.
Il preview contiene importazioni `source=kicad_library`, vertici/triangoli e
colori nativi per tutti i cinque componenti selezionati; STEP/FCStd/WRL sono
stati prodotti. Questo dimostra che il nuovo `model_path` arriva fino a
FreeCAD e non si ferma al JSON.

Test mirati: **176 passed**. `git diff --check` pulito. La suite completa e'
stata avviata ma interrotta dopo oltre sette minuti senza output finale,
probabilmente durante un test di integrazione FreeCAD; non viene dichiarata
verde. Nessuna chiamata AI a pagamento, nessun commit o push aggiuntivo.

Per provare dall'app: riavviare la GUI per caricare il codice, rilanciare la
generazione 3D sul progetto ESP32 gia' analizzato, poi aprire `Rivedi foto /
scegli modelli KiCad…` per confermare o sostituire le proposte provvisorie.
# 10:16 — prima prova completa dell'app dopo il fix: SUCCESSO

L'utente ha rilanciato l'app con Opus e `YOLO locale + verifica AI`. Il log
reale `generated/Module_ESP32-S3-DEV-KIT-NXRX_Castellated_P2.54mm/analisi-ai.log`
mostra: credito controllato a 7.98 USD,
prima chiamata Opus conclusa in 65.867 s, YOLO locale 111 ipotesi globali,
seconda chiamata `report_global_inventory` conclusa in 196.081 s e FreeCAD terminato con
`KICADGEN_STEP_OK`, `KICADGEN_FCSTD_OK`, `KICADGEN_MESH_OK` e
`KICADGEN_WRL_OK`. Nessun HTTP 400 o errore credito.

Output reale:
`generated/Module_ESP32-S3-DEV-KIT-NXRX_Castellated_P2.54mm/Module_ESP32-S3-DEV-KIT-NXRX_Castellated_P2.54mm.3dshapes/`.
Il report `kicad-model-candidates.json` contiene 21 `auto_matched`, 6
`provisional_matched`, 8 `review_required` e 16 `unresolved`. Sono provati
come proposte reali: U1 -> QFN-24, M1 -> `ESP32-S3-WROOM-1U.STEP`, SW1 ->
`SW_SPST_EVQP7A.step`, SW2 -> `SW_SPST_TS-1088-xR020.step`, J1 -> USB-C
`USB4125...step`. J2 resta correttamente irrisolto.

Il preview JSON contiene **328 parti `kicad_library`**, 38 approssimazioni e
114 elementi di supporto/serigrafia; `native_colors=true` sui modelli importati.
Questa e' la prima verifica end-to-end nell'app: il fix non si limita piu' a
preparare candidati, ma li porta realmente in FreeCAD e nell'export finale.
# 11:36 — causa del rendering tutto-magenta isolata e corretta offline

Richiesta utente: il nuovo rendering mostrava quasi soltanto riquadri magenta;
analizzare log, risposta KiCad e soluzione senza dare un esito di facciata.

Evidenza del run reale:

- `generated/Module_ESP32-S3-DEV-KIT-NxRX_2x22_P2.54mm_Castellated/analisi-ai.log`
  conferma che Opus ha completato entrambe le chiamate senza HTTP 400 o errore
  di credito. La chiamata globale ha restituito 48 componenti semantici.
- La risposta salvata in
  `generated/photo-cache/yolo-audit/global-inventory-50beb4c67c2f46e48307aeb756e8e545.json`
  contiene dati validi per U1 (`QFN-24`, marcatura `CH334F`), U2, U3, U4,
  M1 (`ESPRESSIF ESP32-S3-WROOM-1U`), USB-C e SW1/SW2.
- Il report CAD prodotto da quel run contiene 62 righe ma zero `model_path`;
  il log riporta `Attached KiCad models ...: 0`. Le righe erano state
  degradate a `class=unknown`, `package=''`, quindi il magenta era un effetto
  del fallback CAD, non una mancata rilevazione nell'immagine.

Causa concreta: `photo_global_inventory._lead_observation_valid()` rifiutava
ogni riga in cui il provider ometteva `lead_count_confidence` oppure lo
restituiva `null`. Il conteggio `lead_count` incompleto/non osservabile è un
caso valido e non deve cancellare classe, package, marcatura e bbox. `_validate`
trasformava poi ogni riga rifiutata in un generico `unknown`, impedendo il
matching KiCad.

Correzione applicata in `kicadgen/services/photo_global_inventory.py`:

- `_repair_nullable_lead_observation()` ripara solo l'osservazione terminali
  quando il campo è assente/null e il conteggio è coerente; imposta confidenza
  `low` su una copia interna, lasciando intatta la risposta raw nell'audit.
- Tipi errati, conteggi fuori range, conflitti e bbox invalide continuano a
  essere rifiutati.
- Aggiunta regressione in `tests/test_photo_global_inventory.py` per un modulo
  ESP32 con `lead_count=null`, `lead_count_complete=false` e confidenza
  mancante/null.

Verifica senza rete e senza nuova AI a pagamento:

- suite inventario/validazione/matching terminali: **126 passed, 1 skipped**;
- replay della risposta salvata: **47 componenti semantici** conservati e 8
  sole ipotesi generiche, invece di 48 righe degradate a `unknown`;
- matching locale KiCad sui cinque componenti chiave: **5/5 modelli provvisori
  agganciati** — U1 QFN-24, M1 ESP32-S3-WROOM-1U, SW1, SW2 e USB-C.
  Il matcher ha lasciato le alternative nel JSON e li marca come
  `provisional_matched`, quindi non sono identità elettriche certificate.

Gli agenti esperti richiesti non hanno prodotto un audit: il servizio agenti ha
restituito il limite operativo di utilizzo. Non viene inventato alcun risultato
di agenti. Nessun commit, push, cambio branch o nuova chiamata AI effettuata.

Stato operativo: la GUI già aperta non ricarica il codice modificato. Serve
riavviare l'app e rilanciare l'elaborazione (oppure rigenerare il 3D dopo che il
nuovo report è stato creato). Il problema del tutto-magenta è risolto nel
percorso dati; resta da verificare nel nuovo run reale la rotazione nativa del
WROOM e l'eventuale scelta manuale delle varianti marcate provvisorie.
# 11:43 — nuovo run reale dopo il fix del parser: modelli KiCad agganciati

Run utente verificato in
`generated/Module_ESP32-S3-DEV-KIT-NXRX_2x22_P2.54mm_Castellated/analisi-ai.log`.

- Credito letto prima della chiamata: 4.94 USD residui sul tetto chiave;
  nessun HTTP 400, errore credito o `AI_CALL_FAILURE`.
- Opus: chiamata principale 73.037 s; inventario globale 196.969 s, 46 righe
  semanticamente restituite.
- YOLO: 111 proposte iniziali (63 operative + 48 review-only). Il parser non
  ha più trasformato in `unknown` le righe valide con `lead_count_confidence`
  assente/null: il risultato finale conserva 51 ipotesi, con 46 componenti
  globali e 5 proposte provvisorie aggiuntive; 7 sole righe review-only sono
  marcate `render_3d=false`.
- Global inventory è `FAILED/PARTIAL` solo per due collegamenti geometrici
  incoerenti (Q1 e C14) più proposte unresolved; le righe valide non vengono
  più degradate. Le incertezze esplicite riguardano U5/Q1 sovrapposti, Q3/Q4
  non classificabili e passivi in ombra.
- Matching: report `kicad-model-candidates.json` contiene 15 `model_path`
  reali: 10 `auto_matched`, 5 `provisional_matched`, 27 `review_required`,
  9 `unresolved`.
- Import FreeCAD verificati: U1 QFN-24, U2 DFN, ESP32-S3-WROOM-1U, due
  pulsanti PTS810 e passivi C/R 0603. Il WROOM riporta
  `local_rotation_deg=180` e `native_model_rotation_deg=180`, quindi la
  correzione dell'orientamento non è stata persa.
- Export STEP, mesh preview e WRL completati (`KICADGEN_STEP_OK`,
  `KICADGEN_MESH_OK`, `KICADGEN_WRL_OK`). Resta il warning noto
  `KICADGEN_FCSTD_FAILED: Failed to open file`: non ha impedito gli export
  utilizzabili, ma il formato FCStd non è stato prodotto in questo run.

Conclusione: il difetto che causava il rendering tutto-magenta è risolto nel
run reale. Il risultato non è ancora un'identificazione completa: varianti
QFN/DFN, USB-C e semiconduttori ambigui restano in revisione, come previsto
dal matching prudente. Nessun commit/push effettuato.
