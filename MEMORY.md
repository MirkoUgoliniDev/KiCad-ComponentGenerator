# Memoria delle decisioni del progetto

## 2026-10-09 — Checkpoint autorizzato: pinout, preview e revisione

- L'utente autorizza commit, push, merge in main e ritorno su ADVANCED.
  Include tutte le modifiche codice/test/guida documentate qui sotto:
  revisione geometria/navigazione, header e riuso modelli, stime pad,
  pinout editabile e annotazioni 2D/3D con posizione finale approvata.
- Database locale, rows_none_rev.pkl e work/ restano esclusi e preservati.
  I titoli "non committato" sottostanti descrivono la cronologia precedente
  a questo checkpoint. Per hash ed esito pubblicazione consultare Git.

## 2026-10-09 — Ulteriore avvicinamento testi ai terminali (non committato)

- Su richiesta, spostamento verso gli strip aumentato da 2 a 3 mm,
  con limite proporzionale per schede strette. Font, scala e verso invariati.

## 2026-10-09 — Ridotto rientro etichette 3D (non committato)

- Screenshot utente: etichette troppo centrali, font e dimensione approvati.
  Spostate 2 mm verso gli strip (riduzione proporzionale su schede strette),
  senza cambiare scala del testo, orientamento o ancoraggi della mesh.

## 2026-10-09 — Margine etichette rispetto ai connettori (non committato)

- Testi planari ora allineati interamente verso l'interno rispetto al proprio
  ancoraggio, con ulteriore margine fino a 0.8 mm. Prima erano centrati:
  meta' della lunghezza poteva tornare sotto lo strip. Larghezza massima
  adattata allo spazio fra file, orientamento invariato. 38 test passati.

## 2026-10-09 — Etichette solidali alla scheda (non committato)

- Chiarimento screenshot utente: stessa disposizione rispetto alla scheda.
  Sostituiti billboard add_point_labels con testo planare nella sola scena
  VTK, direzione comune alle due file, leggibile dal basso. Ruota con il
  modello e non viene nascosto dal filtro collisioni delle etichette 2D.
  Lunghezza limitata rispetto alla distanza fra le file; altezza 0.65 mm.
- Restano solo annotazioni preview: niente scritte nei file CAD. Aggiornamento
  pinout senza ricostruire la mesh o spostare camera. 38 test passati.

## 2026-10-09 — Leggibilita' etichette 3D (non committato)

- Richiesta: niente scritte oblique, piu' distanti dai pin. Impostato
  italic=False esplicitamente (default PyVista corsivo); offset aggiuntivo
  etichette mesh da 1.2 a 3.2 mm nella direzione gia' indicata dall'ancoraggio,
  verso l'interno nei contatti del modulo. Nessuna modifica CAD.
- 38 test cache/vista passati, diff --check pulito. Riavvio per applicare.

## 2026-10-09 — Pinout editabile e nomi nella vista 3D (non committato)

- Preview3DVtk mostra numero (funzione) usando la tabella del progetto, solo
  dal lato pin come prima. set_pin_functions aggiorna solo attori etichette;
  camera/mesh/CAD intatti. clear cancella ancoraggi per evitare etichette
  del progetto precedente. Copertura aggiornamento nomi senza ricostruzione.
- PinoutDialog: Funzione editabile, numero bloccato, Applica/Annulla. Copie
  indipendenti preservano ordine originale, tipi dei numeri e metadati.
  MainWindow salva prima di aggiornare preview 2D/3D; su errore ripristina
  tabella precedente e avvisa. Nessuna chiamata AI/FreeCAD per modificare nomi.
- 47 test passati per integrazione vista 3D; 38 finali per editor, cache mesh,
  etichette 2D e i18n. Nessun commit, nessun file generato modificato.

## 2026-10-09 — Funzioni pin visibili nell'anteprima (non committato)

- Verificato docs/Pinout.png rispetto ai 44 nomi del project_config: sequenze
  coerenti; G abbrevia GND, U0TXD/U0RXD hanno alias GPIO43/GPIO44. La fonte
  non numera i pin: 1-44 sono assegnati dal progetto. Nessun dato utente cambiato.
- Anteprima castellated e package/header: funzione completa tra parentesi
  verso l'interno, verticale su top/bottom, numero distinto. Testo lungo
  ridimensionato e tooltip completo. Solo annotazione GUI, non export.
- App genera footprint/3D, non simboli schematici. Package/header non esporta
  i nomi come testi; castellated ha sigle serigrafate opzionali e abbreviate.
  Non presentare l'annotazione GUI come nome elettrico nel simbolo KiCad.
- 23 test passati su etichette, tooltip, geometria preview. Nessun commit.

## 2026-10-09 — Stima pad incompatibile con sbalzo (non committato)

- Errore reale 17:27:02: lato inferiore stimato 1.0 mm, sbalzo modulo
  1.4 mm. La validazione bloccava la generazione; non era un errore FreeCAD.
- Il resolver usa il riferimento di lunghezza del modulo solo quando una
  lunghezza NON dichiarata del lato non supera lo sbalzo e il riferimento
  lo supera. Rimane stimata, passa dai controlli di clearance e viene
  segnalata nelle note (IT/EN/DE/FR). Input originali preservati; quote lette
  incompatibili e riferimenti inutilizzabili restano errori.
- 83 test passati (stime, GUI, host, castellated, i18n); dopo traduzioni
  ricontrollati 37 test. Diff --check pulito. Nessun commit/push o modifica
  ai file del progetto generato; riavvio necessario per caricare il fix.

## 2026-10-09 — Fix riuso con foto caricata (non committato)

- Segnalazione reale: due generazioni riuscite ma nessuna cache locale.
  I ComponentBox con foto caricata contengono descrittori OpenCV binari;
  la serializzazione dell'impronta li rifiutava silenziosamente. Ora bytes
  e bytearray sono codificati in base64 con tag, mantenendoli nel confronto.
- Regressione per entrambi i tipi: cambio soli header ammesso, descrittore
  modificato invalida il riuso. 124 test integrati passati, compileall e
  diff --check puliti. Riproduzione isolata
  anche con stato/foto reali letti dal progetto e output in cartella temporanea:
  cache creata. Nessun file del progetto generato modificato.
- Riavviare l'app e completare una generazione per creare la cache mancante;
  dai successivi cambi dei soli header rimane solo FreeCAD. Nessun commit.

## 2026-10-09 — Riuso abbinamenti per cambio dei soli strip (non committato)

- L'utente segnala che togliere/mettere strip ripete consolidati e ricerca
  KiCad. Aggiunto cad3d/header_reuse.py: snapshot locale dopo generazione
  riuscita, con impronta dei parametri completa tranne show_pins_3d.
  Se cambia solo quel flag, i generatori castellated e populated THT saltano
  attach_library_models e riutilizzano i payload componenti gia' scelti.
- RESTA la costruzione/esportazione FreeCAD: non e' un toggle istantaneo della
  mesh e non riutilizza il solido FCStd. Modale ridotta al solo passo FreeCAD
  quando il riuso viene confermato. Nessuna modifica al footprint o chiamata AI.
- Prima generazione completa con questa versione necessaria per preparare
  .header-model-reuse.json nel progetto. Cache assente/alterata, stesso flag,
  cambi a geometria/annotazioni/selezioni o file modello/output modificati
  riportano alla pipeline normale. Cache best effort, scrittura atomica;
  un errore CAD non pubblica nuovi dati di riuso.
- L'impronta ammette anche l'esatto feedback model_match/rotazioni copiato dal
  report alla GUI dopo una generazione; delta rotazione e scelte manuali
  rimangono nel confronto. Test di integrazione col caricamento report reale.
- 122 test passati (riuso, GUI, pipeline, CAD, progress, abort, header, i18n).
  Compileall e diff --check puliti; guida aggiornata. Nessun commit/push.

## 2026-10-09 — Strip selezionabili anche nel tab 3D (non committato)

- Richiesta utente: scegliere se montare gli strip direttamente guardando il
  modello. Aggiunta spunta Mostra gli header nel modello 3D nella toolbar,
  accanto all'icona di rigenerazione esistente. Stessa opzione show_pins_3d
  del wizard, sincronizzata nei due sensi e al ripristino del progetto.
- La spunta invalida solo il 3D; l'icona rigenera STEP/anteprima senza AI.
  Footprint/host_mounting e contatti fisici del modulo non vengono modificati.
  Nascosta per componenti singoli, disabilitata durante rigenerazione e per
  geometria 3D non supportata. Guida aggiornata.
- Verifica: 153 test passati (nuove regressioni controllo, header geometrici,
  toolbar/rigenerazione offline, traduzioni e guida); compileall/diff puliti.
  Nessun commit o push, restano anche le correzioni della revisione precedente.

## 2026-10-09 — Revisione approfondita con tre agenti (correzioni NON committate)

- Baseline pubblicata: ADVANCED a0942d7, merge main 7d91f75. Tornati su
  ADVANCED. L'utente ha chiesto revisione con agenti esperti; nessuna nuova
  autorizzazione a commit/push/merge per queste correzioni.
- Navigazione: salvataggio verificato prima di Nuovo/Apri/Recenti, cambio
  area lavoro e chiusura, oltre al ritorno ai progetti. In caso di errore
  il progetto resta aperto. Config rifiutata invariata puo' essere chiusa;
  modifiche non salvabili richiedono conferma di abbandono. Gestiti recenti
  omonimi in root diverse e ripristino del percorso mostrato.
- Geometria: la collisione circle-circle usa le distanze reali, non i
  rettangoli circoscritti (falso overlap diagonale eliminato). Header anche
  per board PackageGeometry THT esplicite: coordinate/frame/numeri e NPTH
  preservati; nessun foro inventato su SMD/connect. Distanze minime validate.
- Anteprima: fori visibili anche nel percorso PinHeaderParams; contorni
  matrix dal builder, body offset/margin/pad sporgenti rispettati. Quote
  reali senza margine griglia; numeri e quote non sovrapposti, quote catena
  omesse quando troppo strette. Eccezioni builder assorbite nello slot Qt.
- 3D fisico invariato rispetto alle scelte host. Test di errori export
  verificano conservazione footprint precedente, STEP/WRL/mesh.
- Guida HTML aggiornata al wizard e alle nuove opzioni. Test obsoleti
  allineati; i due test rilancio AI ora usano ReviewBundle valido, controllano
  blocco senza chiave e somma costi reale sul ledger temporaneo.
- Suite completa iniziale: 6790 passati, 5 falliti, 11 skipped, 2 xfailed,
  19 subtest passati (960 s). Quattro segnalazioni erano fixture/guida/layout
  precedenti; la quinta catalogo i18n gia' importato mentre gli agenti
  modificavano il sorgente. Tutte risolte e ricontrollate in processo nuovo.
  Verifica finale integrata: 309 passati, 1 skipped; gruppo account/costi/
  rilancio/interruzioni: 240 passati. Nuove regressioni in tre file review
  (57 casi). Compilazione e diff --check puliti. Non rieseguita l'intera
  suite dopo le correzioni: verifiche mirate sopra sullo stato finale.
- QA visiva con Segoe UI e tema reale in work/qa/review-preview-verified-*.png.
  Database locale e rows_none_rev.pkl conservati, hash SHA256 invariati.

## 2026-10-09 — Checkpoint wizard autorizzato per commit, push e merge

- L'utente ha richiesto commit e push, merge in main e ritorno su ADVANCED.
  Questo checkpoint comprende wizard, navigazione, montaggio/diametri dal
  progetto e dall'anteprima, fori visibili e pagina iniziale piu' larga.
- Pulsante dell'anteprima ridotto a icona wf-regenerate.svg con tooltip
  tradotto "Aggiorna solo il footprint". Ultima verifica dedicata: 29 test.
- Esclusi il database locale modificato data/analysis_profiles.sqlite3,
  rows_none_rev.pkl e work/: restano locali. Non autorizzati nuovi commit
  per modifiche successive. Esito della pubblicazione verificabile nel log Git.

## 2026-10-09 — Montaggio modificabile dall'anteprima (non committato)

- Nell'anteprima footprint aggiunti scelta montaggio diretto/header, diametro
  pad e foro e Aggiorna solo il footprint. Controlli sincronizzati con il
  wizard e con il ripristino del progetto; errori visibili anche qui.
  Anteprima immediata, esportazione senza AI/CAD, geometria fisica 3D invariata.
- Schermata iniziale allargata da 96 a 120 caratteri per il testo tedesco.
- Verifica: 91 test mirati passati; compilazione e diff --check puliti.
  Nessun nuovo commit/push/merge. Database e rows_none_rev.pkl lasciati intatti.

## 2026-10-09 — Wizard B e montaggio sulla scheda ospitante (non committato)

- Navigazione affinata su richiesta dell'utente: tornando indietro si deve
  raggiungere la schermata iniziale Nuovo/Apri/Recenti. Eliminata la tappa
  isolata "Progetto" (quasi vuota): nome/cartella nel pannello richiudibile
  Dati del progetto, sopra i Documenti. Il percorso ora e' Documenti ->
  Montaggio e analisi; da Documenti "Torna ai progetti" salva e chiude il
  progetto corrente. Se il salvataggio fallisce o c'e' un lavoro in corso,
  non abbandona il progetto. Nessuna modifica a footprint o modello 3D.

- Correzione successiva segnalata dall'utente: l'anteprima PackageGeometry
  disegnava ogni pad come rettangolo pieno, ignorando forma e drill. Ora
  disegna cerchi/ovali/roundrect e fori; numeri esterni ai pad forati per non
  coprire l'apertura. File ESP32 verificato in sola lettura: gia' presenti
  44 pad THT circolari e 44 drill da 1 mm. Nessuna modifica al 3D o ai file
  generati dell'utente. Nuove regressioni test_package_preview_drills.py;
  61 test mirati passati e cattura Qt verificata in work/qa/header-holes-fixed.png.

- Pubblicazione precedente completata: commit ADVANCED `18561b6`, merge main
  `5762554`, entrambi sul remoto. Tornati su ADVANCED.
- L'utente ha scelto il mockup B e autorizzato l'implementazione, da affinare
  dopo. Nessuna nuova autorizzazione a commit, push o merge.
- Il tab Progetto ora ha due pagine: Documenti, Montaggio e analisi.
  Indietro/Continua non avviano AI. Il pulsante di analisi e' fuori dallo
  scorrimento nella seconda pagina. Strumenti AI e' chiuso di default, con
  riepilogo del rilevatore/modello; si apre per gli errori di configurazione.
- Decisione esplicita: il 3D della BOARD resta fedele alla foto, rame e fori
  compresi. Il tipo di montaggio NON cambia quel modello. La visualizzazione
  opzionale degli header resta una scelta separata (`show_pins_3d`).
- `host_mounting` in project_config.json sceglie le piazzole della scheda
  ospitante: comportamento precedente per saldatura diretta oppure pad
  circolari THT ai centri dei fori chiusi per gli header. Diametro pad/foro
  editabili, inizialmente 1,7/1,0 mm da verificare. Nessun foro inventato sui
  contatti che hanno soltanto un mezzo foro; nessuna modifica ai parametri
  fisici del modulo. Le vecchie configurazioni restano in modalita' diretta.
- `GenerateWorker.footprint_params` separa l'esportazione 2D dai parametri
  fisici passati al 3D. Il pulsante Aggiorna solo il footprint evita AI e CAD:
  conserva STEP e mesh esistenti e richiede dati gia' acquisiti.
- Nuove regressioni in test_host_mounting.py e test_project_wizard.py:
  coordinate/numeri invariati, nessuna mutazione del modulo, worker 3D con
  geometria originale, STEP/mesh conservati, navigazione, persistenza e
  configurazioni legacy. Test di visibilita' SMD adattato al pannello chiuso.
- Mockup e cattura della GUI implementata sotto work/ (non tracciati).
  `rows_none_rev.pkl` resta il diagnostico preesistente: non includerlo.
- Verifica finale delle suite interessate: 339 passati, 2 saltati; controllo
  compilazione e git diff --check puliti. GUI verificata anche con cattura Qt
  e tema reale. Nessuna chiamata AI o ricostruzione FreeCAD reale nei test.
- A fine lavoro compare nuovamente modificato `data/analysis_profiles.sqlite3`:
  origine non verificata, preservato. Nessun commit eseguito.

## 2026-10-09 ? Pubblicazione delle correzioni

L'utente ha confermato il funzionamento e richiesto esplicitamente commit,
push e merge in main del lavoro attuale. Questo commit raccoglie i fix
e i test descritti nel riepilogo precedente, insieme alle regole e alla
memoria di collaborazione. Include lo stato corrente del database profili
`data/analysis_profiles.sqlite3`, senza attribuirne la modifica ai fix.
Il diagnostico preesistente `rows_none_rev.pkl` resta escluso.
Verifiche dei fix: vedere il riepilogo 08/10; diff --check pulito al commit.
Procedura richiesta: pubblicare ADVANCED, unire in main e pubblicare main,
quindi tornare su ADVANCED. Verificare gli hash effettivi nel log Git.
Nessuna autorizzazione a commit di modifiche successive.

## 2026-10-08 — Ripresa domani mattina, 09/10/2026

L'utente ha chiesto di fermarsi e salvare la memoria per riprendere domani.
Le correzioni sono gia' applicate: ripartire da questo stato, senza rifarle.

- Branch attivo: `ADVANCED`, HEAD e origin/ADVANCED `3270158`.
  Il checkpoint e' stato committato e pubblicato PRIMA dei fix; merge gia'
  pubblicato su main: `8c45366`.
- **Regola permanente dell'utente (AGENTS.md): nessun nuovo commit, push o
  merge senza una nuova istruzione esplicita.** I fix restano non committati;
  la richiesta di salvare memoria non autorizza un commit.
- Revisione con tre agenti completata; dieci difetti corretti: pad custom
  non rappresentabili rifiutati, package salvati/ripristinati, 3D improprio
  bloccato per package, passo corretto per pin THT sparsi, controllo foro/rame,
  offset rame rispettato, rifiuto totale YOLO autorevole, bundle fotografico
  atomico, risposte HTTP incomplete contabilizzate, GIF supportate.
- Completamento collegato: la revisione vuota viene salvata esplicitamente,
  non ripopolata dalla cache; si possono disegnare nuovi riquadri, ma il
  rilancio AI e' bloccato finche' la selezione e' vuota. Nome package coerente
  anche dopo rinomina e riapertura; traduzioni aggiornate.
- Verifiche: suite completa in due processi (4202 passati + 8 saltati + 19
  sottotest nella prima meta', 2555 passati + 2 saltati + 2 xfail nella seconda).
  L'unico fallimento era il formato @brief di una docstring nuova: corretto,
  2 test documentazione ripassati. Dopo gli ultimi aggiustamenti, batteria
  finale di 211 test tutti passati. Nessun fallimento residuo noto.
- Nuovi test ancora non tracciati: `test_incomplete_provider_response.py`,
  `test_package_project_gui.py`, `test_review_bundle_atomic.py`,
  `test_review_geometry_regressions.py`, tutti sotto `tests/`.
- `rows_none_rev.pkl` e' un file diagnostico preesistente non tracciato,
  lasciato intatto: non includerlo automaticamente in futuri commit.
- Al salvataggio di questa memoria risulta modificato anche
  `data/analysis_profiles.sqlite3`, che non compariva nello stato alla fine
  dei fix. Origine non verificata: preservarlo, senza ripristinarlo o attribuirlo
  automaticamente alle correzioni.
- Alla ripresa: leggere questo riepilogo e l'inizio di `HANDOFF.md`, controllare
  `git status` per eventuali cambiamenti successivi e seguire la prossima
  indicazione dell'utente. Nessun lavoro/test/processo di questa sessione
  lasciato in esecuzione; nessuna ripresa automatica o promemoria programmato.

## 2026-09-19 — Modelli 3D reali dalla libreria KiCad

Idea dell'utente: smettere di approssimare i package dal bounding box e usare
i modelli che KiCad installa gia' sul disco (105 cartelle, 7251 file).

- Non e' un'API: sono file locali. Nessuna rete, nessun costo per pezzo.
- `resolve_model` NON si fida del nome: confronta l'ingombro del candidato con
  quello misurato sulla fotografia. Fra i candidati vince il piu' vicino; a
  parita' di prove (entro il 5%) decide la corrispondenza esatta di nome,
  perche' SOT-223 e SOT-223-8 hanno lo stesso corpo.
- Tolleranza 0.30 **derivata**: fra due taglie adiacenti corre almeno il 60%.
- Ogni modello porta due letture, corpo ed estensione coi reofori, e vale la
  piu' vicina: il rettangolo sulla foto puo' aver seguito l'una o l'altra. Il
  corpo si misura con una sezione al **65%** dell'altezza, non a meta'.
- Rifiutare e' l'esito sicuro: meglio una forma dichiaratamente approssimata
  di un modello reale ma sbagliato. Senza KiCad si ripiega e basta.

### Emendamento a una decisione precedente

`verify_mounted_bodies` chiedeva solo occupazione (`id`, `present`,
`confidence`), per non certificare identita' elettriche. Ora chiede anche
`package`.

Motivazione: la sigla di package e' una **sagoma meccanica** standard (un
SOT-223 e' un contorno JEDEC), non una funzione di circuito. Sta quindi dentro
il mandato "verify physical mounted bodies". Il divieto sull'identita'
elettrica resta in vigore, nel prompt e nella descrizione del campo, e il test
del contratto lo verifica.

Senza questo campo tutto il resto era inerte: in modalita' ingombri `kind` e'
forzato a `generic` di proposito, e "generic" non corrisponde ad alcun file
della libreria.

## 2026-09-19 — Quarto ruolo: foto ad alta risoluzione

Supera la decisione del 2026-09-13 "solo tre ruoli, non inventare nuove
categorie": richiesta esplicita dell'utente.

- Nuovo ruolo `hires` ("Foto ad alta risoluzione") fra `top` e `front`,
  opzionale come gli altri. Etichetta tradotta IT/EN/FR/DE.
- Serve SOLO a individuare meglio i componenti piccoli. Non deve essere
  quotata: la calibrazione ricava i px/mm dal contorno della scheda piu' le
  quote del datasheet, non legge mai annotazioni dall'immagine.
- Deve essere una fotografia del pezzo reale: un disegno tecnico a tratto ad
  alta risoluzione non serve all'inventario (caso Dimensioni2.jpeg).
- `INVENTORY_ROLES = ('hires', 'top')` e' una PREFERENZA, non un'esclusiva:
  si passano tutte le viste dall'alto assegnate e vince la piu' dettagliata
  FRA QUELLE CALIBRABILI. Una foto ravvicinata puo' non inquadrare tutta la
  scheda: senza il ripiego si resterebbe senza inventario.
- Il limite dell'inventario e' la risoluzione, non l'algoritmo: a 12 px/mm un
  0402 misura 6 px sul lato corto. `_warn_if_too_coarse` lo dichiara invece
  di lasciar credere che l'inventario sia completo.
- Le asserzioni dei test sui ruoli sono guidate da `ROLES`, non dal numero 3.

## 2026-09-13 — Tab KiCad e consegna verificabile

- Aggiungere un tab permanente **KiCad**, disponibile anche in modalità standard,
  per separare verifica, installazione nel progetto e installazione globale.
- **Prova in KiCad** crea un progetto isolato sotto `.kicad-preview`, copia il
  modello STEP e apre il PCB Editor senza modificare `fp-lib-table`; la vista 3D
  si controlla in KiCad con `Alt+3`.
- L'installazione nel progetto crea `KiCadGen.pretty` e `KiCadGen.3dshapes`,
  aggiorna la tabella del progetto e usa riferimenti `${KIPRJMOD}` portabili.
- L'installazione globale usa un percorso assoluto per il modello e aggiorna
  solo l'entry `KiCadGen` nella tabella globale di KiCad. Le entry estranee sono
  conservate; la sovrascrittura richiede conferma e la tabella esistente riceve
  un backup `.kicadgen-backup`.
- Staging atomico e rollback devono impedire componenti parziali o file
  temporanei lasciati dopo un errore. Il tab deve restare disabilitato finché
  footprint e STEP non sono entrambi disponibili.
- La logica è generale per ogni componente generato: non introdurre assunzioni
  specifiche per il PCB RS485 usato nei test.
- Verifica corrente: KiCad 10 ha esportato il footprint e renderizzato il
  progetto di anteprima reale; suite completa `1061 passed, 2 xfailed, 19
  subtests passed`. Il database contiene 480 chiavi × 4 lingue = 1920 righe.
- Commit pubblicati: implementazione `0513e89` su `FIX`, merge `692ca95` su
  `main`; dopo il merge il lavoro riprende su `FIX`.

## 2026-09-13 — Griglia dell'anteprima 2D senza variazioni di zoom

- Spunta e passo della griglia devono aggiornare solo le linee di riferimento,
  senza ridisegnare il footprint o richiamare l'adattamento dell'inquadratura.
- Mantenere scala, posizione e rettangolo della scena indipendenti dalla griglia.
  L'inquadratura iniziale usa solo pad, corpo e quote con un margine costante.
- Applicare la stessa regola a matrice, castellated e package a pad espliciti;
  nessuna modifica ai file generati o alle geometrie 3D.

## 2026-09-13 — Documentazione guidata: solo tre ruoli

- Per ora offrire esclusivamente Pinout, Vista superiore e Vista laterale.
- Copiare in `docs/` come `pinout`, `top`, `front`, preservando l'estensione reale
  e i file originali: non rinominare un PDF o JPEG in PNG senza conversione.
- Salvare le associazioni per progetto in `docs/.document-roles.json`; passare i
  ruoli esplicitamente all'AI. `front` è un nome convenzionale per la vista laterale.
- La vista top scelta dall'utente prevale sull'indice proposto dall'AI; non usare
  pinout o vista laterale come sostituti. Per l'inventario foto serve top raster
  PNG/JPEG/WEBP; i PDF restano utilizzabili nell'analisi generale.
- Ogni ruolo richiede un file oppure la scelta esplicita “Non disponibile”.
  Serve almeno un documento. Non inventare nuove categorie in questa fase.
- Anteprima, sostituzione e rimozione per ciascun ruolo; spiegazioni nella modale Info.
- Confermare sostituzioni/rimozioni; conservare copie recuperabili in `docs/.history`.
- I documenti precedenti restano intatti e richiedono assegnazione con Carica:
  non classificare automaticamente vecchi file dal nome o dall'ordine.
- Conservare il manifest quando si spostano i documenti per rinomina del componente;
  in caso di collisione mantenere nome e cartella correnti e proseguire con i dati
  estratti: non trasformare un conflitto di rinomina in un fallimento dell'AI.
  Anche rifiutare la rinomina mantiene il progetto corrente. Non sovrascrivere
  documenti dell'altro progetto; la conferma per gli output esistenti resta attiva. Gli archivi
  `.history` già esistenti restano nella cartella del progetto originario.

- “Rivedi rilevamento foto…” è il nome del precedente “Confronta foto”: revisione
  degli ingombri sulla foto, non comando di generazione. Mantenere margini interni
  e separazione tra i pulsanti degli strumenti AI.
- [SUPERATO 2026-09-26: la modalità Advanced user è stata tolta, vedi sotto]
  Mostrare “Rivedi rilevamento foto…” solo con Advanced user attivo. Il cambio
  di modalità nasconde il pulsante senza cancellare foto, risultati o funzionalità;
  non modificare l'analisi fotografica automatica. Log AI resta disponibile a tutti.
- Nel Log AI, un'icona Copia per ciascun riquadro copia l'intero contenuto in testo
  semplice (azioni oppure elementi, nell'ordine visualizzato), senza cancellarlo.
  I comandi restano disabilitati quando il rispettivo riquadro è vuoto.

## 2026-09-13 — Comandi manuali di affinamento 3D nascosti

L'utente considera soddisfacente il risultato attuale e ha chiesto di togliere
dall'interfaccia i pulsanti **Rileva terminali dalla foto...** e
**Affina reofori / saldature...**, senza eliminare il codice sottostante.

- I due pulsanti sono volutamente nascosti, non mancanti per errore.
- Conservare servizi, worker, handler, validazioni, test e strumenti diagnostici.
- Non modificare l'analisi automatica dei terminali durante l'estrazione.
- Lasciare disponibile **Rigenera modello 3D (STEP)** e gli altri comandi.
- Non riproporre i due pulsanti nella GUI senza una nuova richiesta dell'utente.

La decisione riguarda l'interfaccia, non certifica la precisione metrologica
delle geometrie ricavate dalle foto e non autorizza commit, merge o push automatici.

## 2026-09-13 — Spiegazioni delle analisi nelle modali Info

- Tenere le spiegazioni estese fuori dagli editor dei prompt.
- Per Datasheet, Localizzazione foto e Verifica foto, usare l'icona Info
  già presente negli asset dell'app per aprire una modale con testo in sola lettura.
- Modello e prompt restano visibili nella scheda; consultare la guida non li modifica.
- Mantenere questa impostazione anche nella nuova gestione CRUD dei profili SQLite.

## 2026-09-13 — Tipologie e profili AI SQLite

- Fonte delle tipologie e dei profili: `data/analysis_profiles.sqlite3`.
- Su richiesta esplicita dell'utente, includere questo database in Git con i
  profili, prompt e modelli configurati. Escludere ancora backup di migrazione,
  journal/WAL, `.env` e configurazioni locali; verificare l'assenza di credenziali
  prima di pubblicare aggiornamenti del database.
- Tre analisi con LLM indipendenti: Datasheet, Localizzazione foto, Verifica foto.
- Default generali ed override per tipologia; selezioni di sessione non persistenti.
- Gestione CRUD in Configurazione, con icone coerenti e spiegazioni solo nelle modali Info.
- Migrare il JSON una sola volta conservandone un backup; le successive scritture
  JSON riguardano le preferenze UI. Le chiavi API restano in `.env`.
- Conservare le quattro famiglie geometriche già supportate: nessuna regola dedicata
  a questo PCB, nessuna modifica ai generatori 3D o ai comandi manuali nascosti.
- I worker ricevono una copia risolta di modelli e prompt; non connessioni SQLite.
- Il prompt tecnico di dettaglio terminali resta configurabile senza ripristinare
  i pulsanti manuali nascosti; la sua analisi automatica resta attiva.

## 2026-09-13 — Modalità standard e Advanced user

**SUPERATO il 2026-09-26 (richiesta dell'utente):** la modalità Advanced user,
la sua spunta, i tab Footprint 2D e Genera, i pannelli Annotazioni 3D e
Componenti sul modulo e l'editor delle traduzioni sono stati TOLTI, non
nascosti. La generazione è sempre automatica. I dati che quei widget tenevano
stanno in valori non grafici (`kicadgen/gui/component_state.py`, `_sides`,
`_photo_rows`); i corpi si modificano con «Modifica componenti…»
(`component_editor_dialog.py`). Le note qui sotto descrivono la situazione
precedente e restano solo come storia (vedi HANDOFF.md, punti 50-59).

- Opzione persistente `advanced_user` nelle Opzioni di generazione (preferenza UI JSON).
- Default standard: nascondere i tab Footprint 2D e Genera, senza distruggerne widget o dati.
- Al termine di un'estrazione valida avviare automaticamente footprint e 3D usando
  i generatori esistenti; mai avviare la generazione per il solo cambio di modalità.
- Controllare i dati necessari al 3D prima dell'automatismo; nessun riutilizzo silenzioso
  di dimensioni del componente precedente. Geometrie con 3D non supportato richiedono
  il percorso avanzato (il footprint manuale resta disponibile).
- Chiedere conferma prima di sovrascrivere output esistenti nell'automatismo.
- Mostrare progresso, risultati parziali ed errori anche con il tab Genera nascosto.
- Advanced user riabilita il flusso manuale già esistente. Info modali per le spiegazioni.
- Anche i pannelli Annotazioni 3D e Componenti sul modulo sono riservati ad Advanced
  user, nascondendo l'intera area scorrevole per lasciare spazio all'anteprima.
  Conservare dati, annotazioni e logica di generazione; l'estrazione non deve
  riaprire questi pannelli in modalità standard. Anteprima e barra azioni restano disponibili.
- La generazione deve aspettare sia la fine del worker AI sia l'applicazione completa
  del risultato alla GUI. Le modali (es. rinomina/spostamento documenti) eseguono un
  event loop Qt annidato: un segnale `finished` ricevuto lì va differito, non deve
  cancellare la richiesta automatica o generare da campi compilati solo in parte.
