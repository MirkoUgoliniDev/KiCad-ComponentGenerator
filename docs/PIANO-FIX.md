# Piano del branch FIX — rendere l'app professionale

Branch: `FIX`, creato da `main` (8a9d7e3) il 25/09/2026.
Obiettivo: le cose che un utente nota nei primi 30 secondi, più l'affidabilità
che serve per distribuire l'installer. Le voci sono in ordine di impatto.
Priorità concordata: partire da **1, 2 e 3** (poco lavoro, grande differenza al
primo impatto). La **6** (firma digitale) è l'unica che richiede una spesa.

Regole che valgono per ogni voce (da `.claude/CLAUDE.md`): prima si aggiorna
`HANDOFF.md` con cosa si sta per fare; testi in 4 lingue nel catalogo; ogni
icona ha un tooltip; test positivi e negativi; nessun commit su `main` senza
richiesta esplicita.

---

## 1. Primo avvio e schermata vuota

Oggi, all'apertura senza progetto, si vedono tre riquadri «Nessun file», un
campo nome disabilitato e un grande pulsante che non si può usare.

- [ ] **Schermata iniziale** quando nessun progetto è aperto: «Nuovo
      progetto», «Apri progetto», **elenco dei progetti recenti**, e
      facoltativamente un **progetto di esempio**.
- [ ] Voce **«Progetti recenti»** nel menu File.

Fatto quando: aprendo il programma senza progetto non c'è nessun controllo
disabilitato senza spiegazione, e un progetto recente si riapre con un clic.

## 2. Chiave API gestita dal programma

Oggi la chiave OpenRouter va scritta a mano in un file `.env`: per un utente
normale è il primo ostacolo.

- [ ] Finestra **«Account AI»** dove incollare la chiave e **verificarla**.
- [ ] Chiave salvata nel **Gestore credenziali di Windows**, non in chiaro.
- [ ] Il `.env` resta valido per chi lo usa già (compatibilità).

Fatto quando: un utente appena installato arriva alla prima analisi senza
aprire nessun file a mano.

## 3. Identità coerente

- [ ] Titolo della finestra con progetto e versione, per esempio
      **«YOL — KiCadGen 1.0.0»** (oggi dice «KiCadGen-Tool»).
- [ ] Testi incoerenti da correggere:
  - [ ] «Anteprima FootPrint» → «Anteprima footprint»;
  - [ ] «About» rimasto in inglese nel menu italiano → **«Informazioni su
        KiCadGen»**;
  - [ ] le lineette «—» sotto i riquadri vuoti dei documenti.

Fatto quando: nome del prodotto uguale ovunque (titolo, installer, About,
documentazione) e nessun testo in una lingua diversa da quella scelta.

## 4. Percorso guidato

Il flusso ha fasi precise: documenti → analisi → revisione dei corpi → 3D →
KiCad.

- [ ] **Barra dei passi** in alto: passo corrente evidenziato, passi fatti
      spuntati.
- [ ] **Barra di stato** in basso (oggi assente): «Pronto», «Analisi in
      corso…», saldo, progetto aperto.

## 5. Aiuto e assistenza

Nel menu «?» mancano:

- [ ] **Guida d'uso**: una pagina o un PDF con un esempio completo;
- [ ] **«Segnala un problema»**: apre GitHub con versione e log già allegati;
- [ ] **«Apri cartella dei log»**.
- [ ] Decidere dove ridare un posto alle voci tolte il 25/09 (vedi sotto),
      per esempio in un menu **«Strumenti»**.

## 6. Affidabilità percepita

- [ ] **Firma digitale** dell'installer: senza, Windows mostra «Editore
      sconosciuto» e molti si fermano lì. Richiede l'acquisto di un
      certificato (decisione e spesa dell'utente).
- [ ] **Controllo aggiornamenti** all'avvio, confrontando con l'ultima
      release su GitHub.
- [ ] **Log su file e rapporto di crash**, per capire cosa è successo sul
      computer di un utente.

## 7. Finiture

- [ ] **Scorciatoie da tastiera** per le azioni principali, con l'elenco nel
      menu.
- [ ] **Tema chiaro** oltre a quello scuro.
- [ ] Controllo a schermi ad **alta risoluzione** (150% e 200%).

---

## Voci di menu tolte il 25/09 (decisione rimandata)

Tolte su richiesta dell'utente; le funzioni esistono ancora. Resta da decidere
se dare loro una voce di menu dedicata (per esempio «Strumenti»):

- «?» → «Apri cartella progetto tool» (cartella dei dati dell'utente);
- Configurazione → «Apri config.json» (il metodo `_open_config_file` è
  rimasto nel codice);
- File → «Apri cartella componente», «Prova in KiCad…», «Installa nel
  progetto KiCad…», «Installa come libreria globale…» (restano nella scheda
  KiCad e nell'icona della cartella).

## Altri punti aperti (dettagli in `HANDOFF.md`)

- [ ] **Crash intermittente all'uscita della suite di test** (codice 139),
      dopo che tutti i test sono passati. Causa non ancora dimostrata; ipotesi
      in prova: pulizia della memoria (`gc.collect`) a fine sessione
      (HANDOFF, punto 42). Conta perché la stessa suite gira nel rilascio.
- [ ] **Licenze in sospeso**: ID Roboflow del modello Large
      (`compdetect-dnwth-2-yolo26l-t1`) e licenza mostrata da Roboflow;
      dataset «Detecting the PCB object» (Arshiasbn, MIT) da confermare per
      il modello `yolov8m_best.pt`; preprocessing reale del dataset CompDetect.
- [ ] **Installer**: prova completa (build + installazione + aggiornamento +
      disinstallazione) in una macchina virtuale; SHA-256 ufficiale di WiX
      come variabile di repository `WIX314_SHA256`; come offrire i sorgenti
      dei componenti GPL/LGPL a ogni rilascio.
- [ ] **Decisioni dell'utente**:
  - risposte vuote del rilevamento AI in cache (ora salvate e marcate nel
    log) oppure mai salvate;
  - pin alfanumerici (A1, EP): oggi l'estrazione accetta solo numeri interi.
