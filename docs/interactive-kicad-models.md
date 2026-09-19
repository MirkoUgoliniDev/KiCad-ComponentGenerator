# Revisione interattiva dei modelli KiCad

Il percorso `YOLO locale + verifica AI` usa il localizzatore ONNX locale, poi
una richiesta fotografica globale con foto completa, overlay numerato, tre
strisce contestuali e JSON dei candidati. L'analisi iniziale dei documenti per
quote/pinout resta separata. Non ci sono ulteriori chiamate automatiche per
i terminali in questa modalita'. Il default AI e' `anthropic/claude-opus-5`.

Il prompt richiede corpi completi, terminali e linguette, package meccanico,
marcature leggibili e incertezze; non prova la funzione elettrica. La versione,
l'hash, il prompt effettivo e il JSON di risposta sono nel log/audit sotto
`generated/photo-cache/yolo-audit`. Una risposta parziale viene segnalata e
non entra nella cache delle risposte complete.

## Uso

La presenza fisica e la classificazione sono valutate separatamente: un corpo
non identificato resta revisionabile come generico. La verifica richiede una
motivazione fisica strutturata per eliminare un candidato (per esempio pad
vuoto o serigrafia); un package sconosciuto non autorizza l'eliminazione.
Questo privilegia la copertura ma non garantisce assenza di falsi positivi:
controllare sempre i riquadri sulla fotografia prima di accettare il CAD.
I package dichiarati in frasi descrittive vengono normalizzati dal matcher;
alternative ambigue restano irrisolte, senza inventare un'identita'.

1. Avvia l'analisi e apri **Rivedi foto / scegli modelli KiCad…** (anche senza Advanced).
2. Fai doppio clic sul rettangolo o sulla riga del componente, oppure selezionalo
   e premi **Scegli modello KiCad…**. Nel 3D puoi usare il tasto destro sul componente.
3. Confronta il ritaglio con le geometrie STEP reali dei candidati. Vengono
   proposti **fino a cinque** file compatibili, non un numero minimo obbligatorio.
   Scarto geometrico e motivazioni non sono probabilita' di identita' elettrica.
4. Conferma il modello e, se necessario, la rotazione locale del CAD. La rotazione
   del package sulla scheda e' distinta. In alternativa scegli un file STEP
   manualmente oppure **Nessuno corrisponde** per conservare il segnaposto.
5. Premi **Applica correzioni**, poi **Rigenera modello 3D**. Annulla non cambia
   il componente originale. Non occorre ripetere l'analisi AI.

Le associazioni dimensionali da sole non importano un modello automaticamente:
i segnaposto non confermati sono magenta. Un modello scelto manualmente non
viene sostituito da un altro in modo silenzioso, neppure se il file sparisce.

## JSON e persistenza

Nella cartella del componente:

- `kicad-model-candidates.json`: elenco dei componenti e report del matcher usati
  nella generazione, con stato, candidati, percorsi, dimensioni, scarto, motivazioni
  e scelta manuale.
- `kicad-model-choices.json`: scelte applicate dall'editor, inclusi i rifiuti,
  legate all'hash della foto calibrata e alla geometria/package del componente.

La scelta sopravvive alla rigenerazione e viene ripristinata dopo una nuova
analisi solo se foto e geometria coincidono. Una foto diversa o una geometria
cambiata richiedono nuova revisione. Non e' un riconoscimento per posizione
approssimata: evita trasferimenti di scelte al componente sbagliato.

Il matcher filtra la famiglia prima delle dimensioni: un condensatore non puo'
diventare un fusibile, un resistore singolo non diventa un array e `0603` non
corrisponde a una sequenza di cifre nel codice di un distanziale. `body` usa il
corpo, `leads/body_and_leads` l'ingombro completo. La tolleranza resta quella
preesistente (30%); per ricerca sono misurati al massimo64 file compatibili e
un report segnala eventuale troncamento. Modelli assenti non vengono inventati.

La lettura di package, le altezze stimate, il pin1 e la polarita' restano da
verificare. I test software/replay non certificano che ogni componente della
foto sia stato individuato correttamente.
