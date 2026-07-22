# Rollout snapshot v3

Il rollout operativo usa quattro gate, tutti ripetibili sullo stesso codice:

1. CI parser e conversione multi-collezione;
2. smoke giornaliero su tutte le collezioni, tollerante ai download esauriti ma fail-closed sui contenuti ricevuti;
3. snapshot mensile completo, validazione e artifact;
4. pubblicazione atomica di commit, tag e release dopo il caricamento degli artifact.

La prima pubblicazione v3 include `legacy-corpus.tar.zst` se il branch del corpus contiene ancora
le vecchie directory per collezione. Solo dopo aver creato questo archivio la pipeline sostituisce
quelle directory con `atti/`, `collections/`, manifest e indici. Le release successive non
rigenerano l'archivio perché le directory legacy non esistono più.

Una release draft non è visibile agli utenti. La pipeline la rende pubblica solo dopo il push
atomico del commit e del tag; se il push o l'aggiornamento della release fallisce, esegue il
rollback del branch, elimina il tag e rimuove la draft.
