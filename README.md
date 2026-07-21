# italia-corpus-script

Pipeline fail-closed che scarica le collezioni Akoma Ntoso di Normattiva e pubblica snapshot verificati di [Italia Corpus](https://github.com/ahmeabd/italia-corpus).

## Garanzie

Ogni esecuzione completa tutte le collezioni prima di modificare il repository:

1. scarica e apre gli ZIP con protezione da path traversal e symlink;
2. estrae i metadati di tutti gli XML;
3. sceglie deterministicamente un documento canonico per URN;
4. costruisce l'indice globale e renderizza `atti/<codice_redazionale>.md`;
5. genera manifest, indici, delta e artifact;
6. applica i quality gate e pubblica un solo commit, tag e release.

XML scartati, metadati obbligatori mancanti, documenti scomparsi, regressioni nei conteggi o nei link interrompono la pubblicazione. Le eccezioni intenzionali devono essere registrate in `quality-exceptions.json` con `metric`, `collection`, `expected_value`, `reason` ed `expires`: un valore diverso resta fail-closed.

Gli atti fondamentali segnalati nelle issue #2 e #3 hanno gate dedicati. Se DPR 380/2001, DPR 151/2011 o D.Lgs. 152/2006 non arrivano dalle collezioni, vengono acquisiti dal testo vigente Normattiva. Le NTC 2018 (`18A00716`) vengono estratte dal PDF ufficiale della Gazzetta e marcate come testo originario, non consolidato con il decreto modificativo del 2023.

## Formato

Il frontmatter v3 espone lo stato temporale dell'atto e gli articoli riportano intervalli risolti:

```yaml
schema_version: 3
urn: urn:nir:stato:decreto.legislativo:2003-06-30;196
codice_redazionale: 003G0218
stato_atto: vigente
versione_data: 2026-07-18
entrata_in_vigore: 2004-01-01
abrogazione_data: null
fonte_versione: vigente
vigente: true # compatibilità, deprecato
```

`manifest.json` è la fonte dei conteggi pubblici, inclusi quelli in `by_collection`. `collections/*.json` descrive l'appartenenza logica alle collezioni; `urn-index.json` risolve sia URN sia codice redazionale verso il percorso canonico. `corpus.sqlite` espone gli intervalli interrogabili nella tabella `articles`.

## Artifact della release

- `markdown.tar.zst`
- `corpus.jsonl.zst`
- `corpus.parquet`
- `corpus.sqlite` con FTS5
- `manifest.json`, `urn-index.json`, `delta.json`
- `SHA256SUMS`

Le release sono immutabili e denominate `snapshot-YYYY-MM-DD`.

## Installazione e pipeline

Richiede Python 3.13 e Git.

```bash
python -m pip install -e '.[dev]'
cp .env.example .env
italia-corpus-pipeline /percorso/con-spazio-sufficiente
italia-corpus-pipeline --dry-run --baseline /percorso/snapshot-precedente /percorso/con-spazio-sufficiente
italia-corpus-pipeline --dry-run --smoke-test /percorso/con-spazio-sufficiente
italia-corpus-pipeline --dry-run --download-cache /percorso/cache /percorso/con-spazio-sufficiente
```

Variabili obbligatorie: `GITHUB_USERNAME`, `GITHUB_TARGET_REPO` e un token tra `GITHUB_TOKEN_1` … `GITHUB_TOKEN_20` o `GITHUB_TOKEN`. Il token viene passato a Git tramite configurazione di processo e non viene inserito nel clone URL.

Con `--dry-run` la pipeline non inizializza GitHub e non crea commit, tag o release. Snapshot e artifact restano nella directory `italia-corpus-dry-run-*` stampata a fine esecuzione; `--baseline` abilita i controlli di regressione contro un manifest precedente.

`--smoke-test` prova tutte le collezioni disponibili in modalità fail-closed, ma converte al massimo i primi 1.000 XML di ciascun archivio. Anche un archivio vuoto interrompe l'esecuzione. Verifica conversione, manifest e artifact senza applicare i gate di copertura che richiedono il corpus completo.

Gli ZIP validi vengono conservati per nome, formato e `dataCreazione` upstream. Ogni archivio ha
un checksum SHA-256 ed è registrato in `inventory.json`; prima del riuso vengono verificati
checksum, inventario e CRC di tutti i membri. Un retry dello stesso snapshot riusa quindi solo
pacchetti integri della medesima edizione; `--download-cache` permette di collocare esplicitamente
questa cache fuori dalla directory di lavoro. I log riportano avanzamento per collezione, formato
effettivo, cache hit/miss, XML letti e tempi.

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

La CI esegue parser, golden multi-collezione, sicurezza ZIP, riproducibilità, manifest, SQLite e controlli statici su Linux e Windows, oltre ad audit delle dipendenze e secret scanning. Il workflow snapshot esegue uno smoke globale giornaliero e una pubblicazione completa mensile; il rollout v3 e la transizione dal layout legacy sono descritti in `docs/rollout.md`.
