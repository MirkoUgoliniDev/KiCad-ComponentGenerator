# Memoria delle decisioni del progetto

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
