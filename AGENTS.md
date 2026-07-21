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

## Security & Publication

Never commit `.env`, tokens, downloaded archives, or generated corpus artifacts. Preserve safe ZIP extraction and non-persistent Git authentication. Do not weaken a quality gate without a documented, expiring exception.
