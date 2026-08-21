# PRD — italia-corpus-script

Data audit: 2026-08-12
Stato: pipeline attiva in sviluppo locale; pubblicazione non eseguita in questo audit.

## Intento

- Utenti: manutentori di Italia Corpus e consumatori che hanno bisogno di snapshot legali riproducibili e verificati.
- Job principale: acquisire Normattiva, scegliere/renderizzare atti canonici, produrre artifact e pubblicare solo dopo quality gate fail-closed.
- Non-obiettivi: editing manuale del corpus, pubblicazione non verificata, bypass dei gate, o una garanzia di completezza quando una collezione è irraggiungibile.

## Maturità attuale

### Capacità del repository

La pipeline Python 3.13 documenta estrazione ZIP sicura, selezione canonica, frontmatter v4, manifest/indici/delta/artifact, cache con checksum, snapshot immutabili, CLI `download/verify/get/search`, dry-run e recovery diretto AKN per payload troncati a 1 MiB. Sono presenti test, ruff, mypy e gate di pubblicazione.

### Evidenza d'uso reale

Non è stata eseguita una pipeline completa, uno snapshot completo o una pubblicazione: sarebbero operazioni esterne e potenzialmente pubblicanti. Il worktree contiene modifiche preesistenti non mie in sei file, inclusi recovery diretto AKN, gate e test; sono state lasciate intatte.

## Stato del lavoro

- Completato: architettura fail-closed, gestione cache/resume, controlli di provenienza e CLI documentata.
- Attivo: modifiche locali preesistenti per recupero dei payload troncati e relativa copertura.
- Bloccato: decidere se mantenere quelle modifiche, poi eseguire test mirati/dry-run isolato con accesso upstream controllato.
- Congelato/indeciso: publish, commit/tag/release e corpus generato sono fuori scope.

## Prossima azione / decisione owner

Il maintainer deve prima confermare le sei modifiche locali preesistenti, poi eseguire il test focalizzato e un dry-run isolato; nessuna pubblicazione va inferita da questa documentazione.

## Audit anti-slop

### Testo

Nessun difetto confermato con confidenza almeno 75%. La documentazione è operativa e distingue chiaramente dry-run da publish.

### Codice

Nessun difetto anti-slop confermato con la soglia. I controlli di sicurezza, provenance e fail-closed sono vincoli comportamentali, non boilerplate da eliminare. Il test completo non è stato lanciato per non alterare o interpretare il worktree preesistente oltre il necessario.

### Design

Non applicabile: pipeline e CLI senza interfaccia visuale di prodotto.

## Fonti di evidenza

- [README.md](README.md)
- [AGENTS.md](AGENTS.md)
- [docs/upstream-anomalies.md](docs/upstream-anomalies.md)
- [quality-exceptions.json](quality-exceptions.json)
- `src/italia_corpus/`, `tests/`, workflow CI e diff locale preesistente.
