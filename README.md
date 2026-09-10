# italia-corpus-script

Pipeline fail-closed che scarica le collezioni Akoma Ntoso di Normattiva e pubblica snapshot verificati di [Italia Corpus](https://github.com/ahmeabd/italia-corpus).

## Garanzie

Ogni esecuzione completa tutte le collezioni prima di modificare il repository:

1. scarica e apre gli ZIP con protezione da path traversal e symlink;
2. estrae i metadati di tutti gli XML;
3. sceglie deterministicamente un documento canonico per URN;
4. costruisce l'indice globale e renderizza `atti/<codice_redazionale>.md`, aggiungendo un
   suffisso stabile derivato dalla URN quando Normattiva riutilizza lo stesso codice;
5. genera manifest, indici, delta e artifact;
6. applica i quality gate e pubblica un solo commit, tag e release.

XML scartati, metadati obbligatori mancanti, documenti scomparsi, regressioni nei conteggi o nei link interrompono la pubblicazione. I link esterni (riferimenti ad atti fuori dal corpus) possono crescere solo se i link interni crescono almeno quanto loro: un aumento alimentato da espansione organica passa, un aumento esterno-dominato no. **Fallback per collezione in regressione**: se l'edizione nuova di una collezione copre meno atti di quella pubblicata (es. Normattiva ritira il formato VIGENTE e pubblica solo gli ORIGINALI), la collezione non viene aggiornata — lo snapshot mantiene il suo ultimo contenuto valido, il resto si aggiorna, e il manifest lo marca nel campo `fallbacks` con un errore nei log. Le regressioni sull'insieme totale (atti, articoli, link complessivi) restano fail-closed. Le eccezioni intenzionali devono essere registrate in `quality-exceptions.json` con `metric`, `collection`, `expected_value`, `reason` ed `expires`: un valore diverso resta fail-closed.

Se un payload vigente è troncato esattamente a 1 MiB, la pipeline prova l'esportazione Akoma Ntoso
del singolo atto e la accetta solo dopo la normale validazione.

Gli atti fondamentali segnalati nelle issue #2 e #3 hanno gate dedicati. Se DPR 380/2001, DPR 151/2011
o D.Lgs. 152/2006 non arrivano dalle collezioni, vengono acquisiti dal testo vigente Normattiva.
Stesso trattamento per i target di riferimento più ricorrenti fuori collezione (censimento sui
render): legge 889/1931, r.d.l. 1071/1935, legge 1643/1962 (ENEL), legge 689/1981, legge 400/1988,
legge 241/1990 e legge 196/2009. Limitazione nota: la Costituzione non è acquisibile — Normattiva
la cataloga con lo stesso codice redazionale (`047U0001`) della legge cost. 1/1947 già presente in
collezione, e la collisione sposterebbe entrambi gli atti su path suffissati; i riferimenti alla
Costituzione restano quindi link esterni. Le NTC 2018 (`18A00716`) vengono estratte dal PDF
ufficiale della Gazzetta e marcate come testo originario, non consolidato con il decreto
modificativo del 2023.

## Formato

Il frontmatter v4 espone lo stato temporale dell'atto e gli articoli riportano intervalli risolti:

```yaml
schema_version: 4
urn: urn:nir:stato:decreto.legislativo:2003-06-30;196
codice_redazionale: 003G0218
stato_atto: vigente
versione_data: 2026-07-18
entrata_in_vigore: 2004-01-01
abrogazione_data: null
fonte_versione: vigente
vigente: true # compatibilità, deprecato
```

`manifest.json` è la fonte dei conteggi pubblici, inclusi quelli in `by_collection`.
`collections/*.json` descrive l'appartenenza logica alle collezioni; `urn-index.json` risolve ogni
URN verso il percorso canonico e ogni codice redazionale verso uno o più atti. `corpus.sqlite`
espone gli intervalli interrogabili nella tabella `articles`.

## Artifact della release

- `markdown.tar.zst`
- `corpus.jsonl.zst`
- `corpus.parquet`
- `corpus.sqlite` con FTS5
- `manifest.json`, `urn-index.json`, `delta.json`
- `SHA256SUMS`

Le release sono immutabili e denominate `snapshot-YYYY-MM-DD`.

## Container persistente

Il container sostituisce il runner GitHub per le esecuzioni complete. Il loop di controllo vive
interamente nel processo Python (`python -m italia_corpus --loop`), quindi `docker stop` termina
snapshot e attese in modo pulito. Tiene ZIP verificati e download `.partial` in un volume durante
i retry; dopo uno snapshot riuscito elimina le edizioni superate e conserva solo i dati della
versione corrente. Non espone porte HTTP e non contiene token nell'immagine.

Il container gira come utente non privilegiato con UID/GID 1000: la directory montata in `/data`
deve essere scrivibile per quell'utente (`chown -R 1000:1000`). Il loop aggiorna `/data/.heartbeat`
a ogni iterazione e il healthcheck di `compose.yaml` segnala il container come unhealthy se il
file resta fermo più di tre giorni.

```bash
cp .env.example .env
printf '%s' 'github_pat_...' > github_token
chown 1000:1000 github_token
docker compose up -d --build
docker compose logs -f
```

Per pubblicare nel tuo corpus, lascia nell'`.env`:

```dotenv
GITHUB_USERNAME=your-github-username
GITHUB_TARGET_REPO=italia-corpus
ITALIA_CORPUS_DATA_DIR=/mnt/storage/DATA/italia-corpus-runner
CHECK_INTERVAL_SECONDS=86400
FULL_RUN_INTERVAL_SECONDS=2592000
RETRY_DELAY_SECONDS=3600
```

Il token sta nel file `github_token`, montato come Docker secret. Deve poter scrivere nel repository
target. Il container controlla le edizioni Normattiva ogni giorno e fa il run completo solo quando
trova una variazione; `FULL_RUN_INTERVAL_SECONDS` forza comunque un controllo completo mensile.
Il container riprova gli errori dopo `RETRY_DELAY_SECONDS`, conservando la cache nel volume. Per una prova manuale:

```bash
docker compose run --rm italia-corpus --once
```

### Destinazioni di pubblicazione

`PUBLISH_TARGET=github` crea o aggiorna una repository GitHub, pubblica release e carica gli
artifact. `PUBLISH_TARGET=git` supporta GitLab, Codeberg, Gitea e qualsiasi server Git:

```dotenv
PUBLISH_TARGET=git
GIT_TARGET_URL=https://git.example.com/owner/italia-corpus.git
GIT_TARGET_BRANCH=main
GIT_TARGET_USERNAME=x-access-token
PUBLISH_TOKEN_FILE=/run/secrets/github_token
```

Con il target Git generico, il container fa clone, commit e tag atomico. Non crea repository,
release o asset perché quelle API non sono standard tra i provider. Per SSH, ometti il token e usa
un URL `ssh://` con le credenziali Git disponibili nel container.

## Installazione locale e pipeline

Richiede Python 3.13 e Git.

```bash
python -m pip install -e '.[dev]'
cp .env.example .env
italia-corpus-pipeline /percorso/con-spazio-sufficiente
italia-corpus-pipeline --dry-run --baseline /percorso/snapshot-precedente /percorso/con-spazio-sufficiente
italia-corpus-pipeline --dry-run --smoke-test /percorso/con-spazio-sufficiente
italia-corpus-pipeline --dry-run --download-cache /percorso/cache /percorso/con-spazio-sufficiente
```

Variabili obbligatorie: `GITHUB_USERNAME`, `GITHUB_TARGET_REPO` e un token tra `GITHUB_TOKEN_1` …
`GITHUB_TOKEN_20`, `GITHUB_TOKEN`, oppure un token file letto da `GITHUB_TOKEN_FILE` o
`PUBLISH_TOKEN_FILE` (che vale anche per `GIT_TARGET_TOKEN`). `GITHUB_TARGET_REPO` accetta sia
`italia-corpus` sia `owner/italia-corpus`; se deve creare la repository, la crea pubblica. Il token
viene passato a Git tramite configurazione di processo e non viene inserito nel clone URL.

Con `--dry-run` la pipeline non inizializza GitHub e non crea commit, tag o release. Snapshot e artifact restano nella directory `italia-corpus-dry-run-*` stampata a fine esecuzione; `--baseline` abilita i controlli di regressione contro un manifest precedente.

`--smoke-test` prova tutte le collezioni disponibili, ma converte al massimo i primi 1.000 XML di ciascun archivio. Se una collezione resta irraggiungibile, vuota o non valida dopo tutti i retry, registra un warning e continua con le altre; gli XML scaricati restano invece soggetti agli stessi controlli fail-closed della pipeline completa. Verifica conversione, manifest e artifact senza applicare i gate di copertura che richiedono il corpus completo. La pipeline completa non salta mai una collezione non disponibile.

Gli ZIP validi vengono conservati per nome, formato e `dataCreazione` upstream. Ogni archivio ha
un checksum SHA-256 ed è registrato in `inventory.json`; prima del riuso vengono verificati
checksum e inventario, mentre i CRC dei membri vengono controllati alla lettura durante la
conversione, così il riuso non ri-decomprime gli archivi. Un retry dello stesso snapshot riusa
quindi solo pacchetti integri della medesima edizione; `--download-cache` permette di collocare
esplicitamente questa cache fuori dalla directory di lavoro. Il container conserva la cache anche
dopo un tentativo fallito e ripete il full run in autonomia. I log riportano avanzamento per
collezione, formato effettivo, cache hit/miss, XML letti e tempi.

## CLI per gli utenti

```bash
italia-corpus download
italia-corpus verify
italia-corpus get --urn 'urn:nir:stato:decreto.legislativo:2003-06-30;196'
italia-corpus get --urn 'urn:nir:stato:regio.decreto:1930-10-19;1398' --article art-575 --vigente-al 2024-01-01
italia-corpus search 'protezione dati' --vigente-al 2024-01-01
```

I comandi restituiscono `0` per successo, `1` per assenza/verifica fallita e `2` per errore d'uso o configurazione. Aggiungere `--json` prima del sottocomando per output machine-readable.

## Sviluppo

```bash
python -m pytest
python -m ruff check .
python -m mypy
```

La CI esegue parser, golden multi-collezione, sicurezza ZIP, riproducibilità, manifest, SQLite e controlli statici su Linux e Windows, oltre ad audit delle dipendenze e secret scanning. Le pubblicazioni complete sono affidate al container persistente; il rollout v4 e la transizione dal layout legacy sono descritti in `docs/rollout.md`.
