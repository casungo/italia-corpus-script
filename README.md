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

XML scartati, metadati obbligatori mancanti, documenti scomparsi, regressioni nei conteggi o nei link interrompono la pubblicazione. Le eccezioni intenzionali devono essere registrate in `quality-exceptions.json` con `metric`, `reason` ed `expires`.

Gli atti fondamentali segnalati nelle issue #2 e #3 hanno gate dedicati. Se DPR 380/2001, DPR 151/2011 o D.Lgs. 152/2006 non arrivano dalle collezioni, vengono acquisiti dal testo vigente Normattiva. Le NTC 2018 (`18A00716`) vengono estratte dal PDF ufficiale della Gazzetta e marcate come testo originario, non consolidato con il decreto modificativo del 2023.

## Formato

Il frontmatter v2 espone lo stato temporale senza fingere precisione a livello di articolo:

```yaml
schema_version: 2
urn: urn:nir:stato:decreto.legislativo:2003-06-30;196
codice_redazionale: 003G0218
stato_atto: vigente
versione_data: 2026-07-18
entrata_in_vigore: 2004-01-01
abrogazione_data: null
fonte_versione: vigente
vigente: true # compatibilità, deprecato
```

`manifest.json` è la fonte dei conteggi pubblici. `collections/*.json` descrive l'appartenenza logica alle collezioni; `urn-index.json` risolve URN e codice redazionale verso il percorso canonico.

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
```

Variabili obbligatorie: `GITHUB_USERNAME`, `GITHUB_TARGET_REPO` e un token tra `GITHUB_TOKEN_1` … `GITHUB_TOKEN_20` o `GITHUB_TOKEN`. Il token viene passato a Git tramite configurazione di processo e non viene inserito nel clone URL.

Con `--dry-run` la pipeline non inizializza GitHub e non crea commit, tag o release. Snapshot e artifact restano nella directory `italia-corpus-dry-run-*` stampata a fine esecuzione; `--baseline` abilita i controlli di regressione contro un manifest precedente.

`--smoke-test` prova tutte le collezioni disponibili, ma converte al massimo i primi 1.000 XML di ciascun archivio. Le collezioni che rispondono con un archivio vuoto vengono registrate e saltate. Verifica conversione, manifest e artifact senza applicare i gate di copertura che richiedono il corpus completo.

## CLI per gli utenti

```bash
italia-corpus download
italia-corpus verify
italia-corpus get --urn 'urn:nir:stato:decreto.legislativo:2003-06-30;196'
italia-corpus search 'protezione dati' --vigente-al 2024-01-01
```

I comandi restituiscono `0` per successo, `1` per assenza/verifica fallita e `2` per errore d'uso o configurazione. Aggiungere `--json` prima del sottocomando per output machine-readable.

## Sviluppo

```bash
python -m pytest
python -m ruff check .
python -m mypy
```

La CI esegue parser, golden test, sicurezza ZIP, riproducibilità, manifest, SQLite e controlli statici su Linux e Windows, oltre all'audit delle dipendenze.
