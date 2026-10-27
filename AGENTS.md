# Regole di collaborazione

- Quando si riprende il lavoro, leggere prima il riepilogo piu' recente
  all'inizio di docs/MEMORY.md e HANDOFF.md e verificare lo stato Git corrente.
- Non creare commit senza una richiesta esplicita dell'utente. L'autorizzazione
  a un commit riguarda soltanto il lavoro presente al momento della richiesta:
  non si estende alle modifiche successive.
- Dopo aver eseguito un commit, push o merge autorizzato, lasciare le nuove
  modifiche nel working tree finché l'utente non richiede un altro commit.
- Push e merge richiedono anch'essi un'istruzione esplicita dell'utente;
  una richiesta di revisione o correzione del codice non li autorizza.
- Regola richiesta dall'utente il 2026-10-08: prima pubblicare e unire in main
  il checkpoint corrente, poi correggere i difetti della revisione senza
  committare le correzioni fino a nuova istruzione.
