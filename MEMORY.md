# Memoria delle decisioni del progetto

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
