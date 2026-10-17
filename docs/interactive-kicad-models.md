# Revisione interattiva dei modelli KiCad

Per ogni corpo trovato sulla foto di una scheda, KiCadGen cerca nella
libreria 3D di KiCad installata un modello STEP con misure compatibili e lo
usa nel solido al posto del volume generico. La scelta si rivede e si
conferma a mano; **le sole dimensioni non certificano un'identità**: un
abbinamento dimensionale non è la prova di marca, variante o polarità.

L'analisi iniziale dei documenti (quote, pinout) e l'inventario fotografico
(rilevatore locale, inventario AI, lettura dei ritagli) sono descritti in
`photo-mechanical-workflow.md`; qui si parla solo di che cosa succede dopo,
quando i corpi riconciliati vengono abbinati ai modelli. Il modello AI è
quello unico configurato in **Configurazione → Default generali AI…**
(predefinito: `deepseek-api/deepseek-flash` dai sorgenti, `anthropic/claude-opus-5`
via OpenRouter nel programma installato; si sceglie dall'interfaccia, mai
modificando il database); la versione
del prompt, l'hash, il prompt effettivo e il JSON di risposta sono nel log e
negli audit sotto `<progetto>/photo-cache`. Una risposta parziale viene
segnalata e non entra nella cache delle risposte complete.

## Uso

La presenza fisica e la classificazione sono valutate separatamente: un
corpo non identificato resta revisionabile come generico. La verifica
richiede una motivazione fisica strutturata per eliminare un candidato (per
esempio pad vuoto o serigrafia); un package sconosciuto non autorizza
l'eliminazione. Questo privilegia la copertura ma non garantisce assenza di
falsi positivi: controllare sempre i riquadri sulla fotografia prima di
accettare il CAD. I corpi aggiunti dal solo modello AI partono esclusi e si
tengono con **Tieni o lascia** (T) nella revisione.

1. Avvia l'analisi. Con la pausa dopo l'inventario attiva (Configurazione →
   Opzioni di generazione…) la **finestra di revisione** si apre nella fase
   inventario; più tardi si riapre gratis con **Revisione componenti…** nella
   scheda Progetto.
2. Seleziona un corpo nell'elenco o sulla foto e premi **Scegli modello
   KiCad…** (nella barra, con doppio clic sulla riga, oppure da
   **Parametri…**, la modale con le sezioni rilevatore locale, AI e modello
   KiCad e le correzioni modificabili). Dalla scheda **Modello STEP** lo stesso
   selettore si apre con il tasto destro sul corpo e **Cambia modello
   KiCad…**, anche senza la foto.
3. La finestra **Scelta del modello STEP** confronta il ritaglio della foto
   con le geometrie STEP reali dei candidati: **fino a cinque** file
   compatibili, con le loro misure, non un numero minimo obbligatorio.
   Scarto geometrico e motivazioni non sono probabilità di identità
   elettrica. L'anteprima del candidato è orientata come verrà montato; la
   rotazione si imposta a mano oppure con **Automatico: deduci gli assi CAD
   dall'ingombro**. La spunta **Colori** mostra l'anteprima con i colori del
   file STEP (spenta è a tinta unica e più veloce; la scelta viene
   ricordata).
4. Conferma il modello e, se serve, la rotazione locale del CAD, distinta
   dalla rotazione del package sulla scheda. In alternativa scegli un file
   STEP manualmente, oppure **Nessuno corrisponde** per conservare il
   segnaposto.
5. Premi **Applica correzioni**, poi **Rigenera modello STEP con queste
   annotazioni** (F5) nella
   scheda Modello STEP. Annulla non cambia il componente. Non occorre ripetere
   l'analisi AI: queste operazioni sono gratuite.

**Accetto, mi piace — ricorda questi modelli…** (scheda Modello STEP, dopo una
generazione riuscita) salva gli abbinamenti corpo/modello con un opt-out per
riga; in una scheda futura vengono proposti subito, da confermare. L'archivio
si rivede e si ripulisce con **Configurazione → Archivio abbinamenti
accettati…**, perché un abbinamento accettato per errore si propaga.

## Abbinamento automatico

Un candidato meccanico unico può essere applicato automaticamente quando
presenza, famiglia, terminali e base di misura sono supportati e la ricerca
non è troncata o incompleta. Il rapporto distingue `auto_matched` da
`confirmed` (conferma manuale): identità e polarità restano non certificate
nel primo caso. I casi ambigui o irrisolti restano segnaposti. Un modello
scelto manualmente, o un rifiuto, ha precedenza e non viene sostituito in
modo silenzioso, neppure se il file sparisce. Se il package non è
specificato, il matcher cerca nella famiglia usando le dimensioni, con un
budget limitato di misure CAD; non forza una scelta solo perché un candidato
è primo nella lista.

La ricerca considera anche dichiarazioni strutturali come SMD/THT, pulsante
tattile e ricettacolo USB-C: permettono di misurare modelli senza quote nel
nome, nello stesso budget; non identificano marca o variante. La cache delle
misure usa scritture atomiche e ripiega sulla cartella dati dell'utente se la
cartella della libreria non è scrivibile.

Una taglia proposta come `0603 (approx)` non viene trasformata in
un'identità certa. Per R/C con presenza e famiglia supportate e due endcap
osservati, la ricerca confronta tutte le taglie e varianti dei chip standard
della stessa famiglia, misurandone i solidi anche a cache fredda; il JSON
dichiara l'ambito `discovery_scope`. Varianti multiple, misure mancanti o il
superamento del budget mantengono la revisione manuale; le taglie adiacenti
non vengono eliminate per privilegiare quella nominata dall'AI.

Il matcher filtra la famiglia prima delle dimensioni: un condensatore non
può diventare un fusibile, un resistore singolo non diventa un array e
`0603` non corrisponde a una sequenza di cifre nel codice di un distanziale.
`body` usa il corpo, `leads/body_and_leads` l'ingombro completo. Nei chip
R/C/LED Metric standard il corpo comprende gli endcap: si misura l'intero
modello. Per LED speciali, pulsanti, connettori e moduli la sezione al 65 %
non viene usata come misura del corpo (potrebbe essere una lente, un
attuatore o una schermatura); sugli altri package resta una stima dichiarata
che da sola non abilita un abbinamento automatico. Il JSON espone
`measurement_method` per distinguere queste basi. Tolleranza dimensionale
30 % (`DEFAULT_SIZE_TOLERANCE`); per ricerca sono misurati al massimo 64 file
compatibili (`MAX_CANDIDATE_MEASUREMENTS`) e il rapporto segnala l'eventuale
troncamento. Modelli assenti non vengono inventati.

La rotazione KiCad proposta dalla rete SMD locale (vedi
`CLASSIFICATORE_SMD.md`) è conservata separatamente dall'angolo
dell'ingombro e non forza l'orientamento del STEP nativo, che può avere una
trasformazione diversa nel footprint e va confermato nel selettore.

## JSON e persistenza

Nella cartella del progetto:

- `kicad-model-candidates.json`: elenco dei componenti e rapporto del matcher
  usati nella generazione, con stato, candidati, percorsi, dimensioni,
  scarto, motivazioni e scelta manuale.
- `kicad-model-choices.json`: scelte applicate dall'editor, inclusi i
  rifiuti, l'esclusione manuale dal 3D e la rotazione manuale, legate
  all'hash della foto calibrata e alla geometria/package del componente.

La scelta sopravvive alla rigenerazione e viene ripristinata dopo una nuova
analisi solo se foto e geometria coincidono: una foto diversa o una geometria
cambiata richiedono nuova revisione. Non è un riconoscimento per posizione
approssimata, così una scelta non si trasferisce al componente sbagliato; una
geometria duplicata non propaga la scelta di una riga all'altra. Un
abbinamento automatico senza scelta esplicita resta solo nell'audit e viene
ricalcolato, mai riprodotto come scelta dell'utente.

La lettura di package, le altezze stimate, il pin 1 e la polarità restano da
verificare. I test software e i replay non certificano che ogni componente
della foto sia stato individuato correttamente.
