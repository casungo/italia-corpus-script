# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/italia_corpus/`. `pipeline.py` orchestrates complete snapshots; `converter.py` performs two-pass canonical selection; `akn.py` renders Akoma Ntoso; `snapshot.py` validates and builds artifacts; `supplemental.py` imports required official sources; and `cli.py` provides end-user commands. Tests are under `tests/`, with minimized legal XML in `tests/fixtures/`. CI and dependency automation live in `.github/`. Coverage and temporary regression policies are versioned in `coverage-requirements.json` and `quality-exceptions.json`.

The adjacent `../italia-corpus/` checkout is generated data. Do not edit generated acts manually.

## Build, Test, and Development Commands

Use Python 3.13 or newer:

```bash
python -m pip install -e '.[dev]'
python -m pytest
python -m ruff check .
python -m mypy
python -m pip_audit
```

Run `italia-corpus get`, `search`, or `verify` to exercise the local CLI. Run `italia-corpus-pipeline /tmp/workdir` only with deliberate GitHub credentials: a successful run can publish a commit, tag, and release.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, `pathlib.Path`, dataclasses for structured results, and standard-library features before new dependencies. Ruff enforces Python 3.13 style and a 100-character line limit; mypy checks the full package. Use `snake_case` for functions and modules, `PascalCase` for classes, and uppercase names for constants.

## Testing Guidelines

Name tests `test_*.py` and test behavior, not implementation details. Parser changes require a minimized fixture and a golden assertion. Always cover deterministic output, fail-closed behavior, malformed input, stable anchors, and source provenance. Regressions involving critical acts belong in `coverage-requirements.json`, not only in prose.

## Commit & Pull Request Guidelines

History uses short Conventional Commit prefixes such as `feat:` and `refactor:`. Prefer focused messages like `fix: preserve code attachments`. Pull requests must describe dataset impact, link relevant issues, list validation commands, and include manifest/count changes. Call out migrations or link-only diffs explicitly; screenshots are unnecessary unless documentation rendering changes.

## Upstream & Operations Playbook

Hard-won facts about Normattiva and the deployment. Verify before relying on them; they were learned from real runs.

- `dataCreazione` (edition date) bumps daily even when the package bytes are frozen. Never use it as a content fingerprint: the nightly check probes each collection with a one-byte Range GET and compares `numeroAtti` + ETag + Content-Length against `fingerprints.json`, which is written only after a successful publish. Expect ~20 seconds per check.
- The collections metadata endpoint exposes only `nomeCollezione`, `formatoCollezione`, `descrizioneFormatoCollezione`, `dataCreazione`, `numeroAtti`. The download endpoint rejects HEAD (405) but serves Range requests (206 with ETag and full length).
- Formato `O` (ORIGINALE) members can be base64-wrapped XML; `_source_xml_text` unwraps them. The codice redazionale in the member filename is a hint — the metadata inside the XML is authoritative.
- Article-listing pages for the same act can differ between fetches. Supplemental requirements must be structural (`min_articles`), never specific deep anchors.
- Codici redazionali can collide across documents: `047U0001` is both the Costituzione (G.U. 27-12-1947) and legge cost. 1/1947 already present in the Leggi costituzionali collection. Never ingest a supplemental under a codice a collection already carries — the collision suffixes both paths, the base path disappears, and the gate refuses. The Costituzione therefore stays external by design (documented in README).
- References inside acts use alias URN forms (`codice.civile:1942-03-16;262` instead of the canonical `regio.decreto:...`, friendly types, sometimes empty segments). The invariant part of any form is the `date;number` tail; `refs.py` resolves through it when the exact URN misses.
- Gate order in `pipeline.py` is deliberate: `validate_report` (consistency, skips, regressions, removed files) runs before `validate_required_coverage` so a real render error surfaces with its message instead of being buried under coverage failures.
- `external_links` may grow only when `internal_links` grows at least as much (organic expansion passes, external-dominated growth fails). "previous document disappeared" means a published path is absent from `report.hashes` — diff the upstream edition before blaming the pipeline.
- The download cache (`download-cache/`) is persistent: per-edition discovery caches, `inventory.json` (archives with sha256), `supplemental/` (fetched XML, self-pruned to SOURCES), `recovery/` (truncated-member fixes). Prune runs only after a successful publish; failed runs intentionally keep their inputs for forensics and retry.
- Render pool workers each receive the full urn_index through the initializer: cap workers near `cpu_count()` or a 15 GB host OOMs (BrokenProcessPool, or the container silently restarted by its restart policy leaving a stale `italia-corpus-*` workspace in `/data/work`).
- The container runs as UID/GID 1000: chown the data volume before deploying. `docker compose restart` does not pick up a rebuilt image — use `docker compose up -d` (recreate). `docker stop` during a run is safe (fail-closed; pushes are atomic); hard-killed runs leave workspace leftovers to clean manually.
- Production publishes to `casungo/italia-corpus` via the `.env`/secret configuration; the public `ahmeabd/italia-corpus` is the downstream dataset, in the legacy layout.

## Security & Publication

Never commit `.env`, tokens, downloaded archives, or generated corpus artifacts. Preserve safe ZIP extraction and non-persistent Git authentication. Do not weaken a quality gate without a documented, expiring exception.
