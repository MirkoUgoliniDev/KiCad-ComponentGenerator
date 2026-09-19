# Memoria delle decisioni del progetto

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
- Mostrare “Rivedi rilevamento foto…” solo con Advanced user attivo. Il cambio
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
