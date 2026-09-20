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

Le sole dimensioni non certificano un'identita'. Un candidato meccanico unico
puo' essere applicato automaticamente quando presenza, famiglia, terminali e
base di misura sono supportati, e la ricerca non e' troncata o incompleta.
Il report distingue `auto_matched` da `confirmed` (conferma manuale): identita'
e polarita' restano non certificate nel primo caso. I casi ambigui o irrisolti
restano segnaposti magenta. Un modello scelto manualmente, o un rifiuto, ha
precedenza e non viene sostituito in modo silenzioso, neppure se il file sparisce.
Se il package non e' specificato, il matcher cerca nella famiglia usando le
dimensioni, con un budget limitato di misure CAD; non forza una scelta solo
perche' un candidato e' primo nella lista.

La ricerca considera anche dichiarazioni strutturali come SMD/THT, pulsante
tattile e ricettacolo USB-C. Queste permettono di misurare modelli senza quote
nel nome, nello stesso budget di 64; non identificano marca o variante.
La cache delle misure usa scritture atomiche e ripiega sulla cartella utente
se la cartella della libreria non e' scrivibile.

Una taglia proposta come `0603 (approx)` non viene trasformata in un'identita'
certa. Per R/C con presenza e famiglia supportate e due endcap osservati,
la ricerca confronta tutte le taglie e varianti dei chip standard della stessa
famiglia, misurandone i solidi anche a cache fredda. Il JSON dichiara l'ambito
`discovery_scope: standard_chip_endcaps`: non e' una ricerca universale di ogni
alloggiamento possibile. Varianti multiple, misure mancanti o il superamento
del budget mantengono la revisione manuale; le taglie adiacenti non vengono
eliminate per privilegiare quella nominata dall'AI. Tolleranza invariata.

`terminal_count` conta superfici visibili, anche alette: non e' il totale
dei piedini del package. Il prompt v5 distingue `lead_count`, completezza
e confidenza; solo un conteggio completo e supportato limita i piedini ordinari.
Per singoli chip R/C standard, due endcap visibili costituiscono evidenza della
topologia meccanica a due terminali, non del valore elettrico.

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
corpo, `leads/body_and_leads` l'ingombro completo. Nei chip R/C/LED Metric
standard il corpo comprende gli endcap: si misura l'intero modello, non solo
la sezione superiore. Per LED speciali, pulsanti, connettori e moduli la
sezione al 65% non viene usata come misura del corpo: potrebbe essere solo
una lente, un attuatore o una schermatura. Sugli altri package questa sezione
resta una stima dichiarata e non abilita da sola un abbinamento automatico.
Il JSON espone `measurement_method` per distinguere queste basi. La tolleranza resta quella
preesistente (30%); per ricerca sono misurati al massimo64 file compatibili e
un report segnala eventuale troncamento. Modelli assenti non vengono inventati.

La lettura di package, le altezze stimate, il pin1 e la polarita' restano da
verificare. I test software/replay non certificano che ogni componente della
foto sia stato individuato correttamente.
