"""Snapshot validation, manifests and deterministic distribution artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import stat
import tarfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZipInfo

from .converter import Candidate, ConversionReport

SCHEMA_VERSION = 2


class QualityGateError(RuntimeError):
    pass


def safe_zip_members(zf: ZipFile) -> list[ZipInfo]:
    """Return regular ZIP members after rejecting traversal and symlinks."""
    members = []
    for member in zf.infolist():
        path = PurePosixPath(member.filename.replace("\\", "/"))
        mode = member.external_attr >> 16
        if path.is_absolute() or ".." in path.parts or stat.S_ISLNK(mode):
            raise QualityGateError(f"unsafe ZIP member: {member.filename!r}")
        if not member.is_dir():
            members.append(member)
    return members


def safe_extract_zip(zf: ZipFile, destination: Path) -> None:
    """Extract regular ZIP members without zip-slip or symlink traversal."""
    root = destination.resolve()
    members = safe_zip_members(zf)
    for member in members:
        path = PurePosixPath(member.filename.replace("\\", "/"))
        target = (root / Path(*path.parts)).resolve()
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)


def _allowed_regressions(path: Path | None) -> dict[str, dict]:
    if not path or not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    today = date.today().isoformat()
    return {
        row["metric"]: row for row in rows
        if row.get("reason") and row.get("expires", "") >= today
    }


def validate_report(report: ConversionReport, previous: dict | None = None,
                    exceptions_path: Path | None = None) -> None:
    allowed = _allowed_regressions(exceptions_path)
    failures: list[str] = []
    if report.skipped or report.errors:
        failures.append(f"{report.skipped} XML skipped, {len(report.errors)} errors")
        failures.extend(f"{error.source}: {error.message}" for error in report.errors[:20])
    if report.converted != report.urns or report.converted != report.editorial_codes:
        failures.append("not every document has one URN and editorial code")
    if report.converted == 0:
        failures.append("empty corpus")
    if previous:
        old = previous.get("counts", {})
        checks = {
            "acts": (report.converted, int(old.get("acts", 0))),
            "articles": (report.articles, int(old.get("articles", 0))),
            "internal_links": (report.internal_links, int(old.get("internal_links", 0))),
        }
        for metric, (new, prior) in checks.items():
            if new < prior and metric not in allowed:
                failures.append(f"{metric} regressed from {prior} to {new}")
        prior_external = int(old.get("external_links", 0))
        if report.external_links > prior_external and "external_links" not in allowed:
            failures.append(f"external_links increased from {prior_external} to {report.external_links}")
        for path, digest in previous.get("files", {}).items():
            if path not in report.hashes and "removed_files" not in allowed:
                failures.append(f"previous document disappeared: {path} ({digest[:12]})")
    if failures:
        raise QualityGateError("quality gate failed:\n- " + "\n- ".join(failures))


def validate_required_coverage(report: ConversionReport, requirements_path: Path) -> list[str]:
    config = json.loads(requirements_path.read_text(encoding="utf-8"))
    failures = []
    for requirement in config.get("required", []):
        path = f"atti/{requirement['codice_redazionale']}.md"
        actual = report.document_articles.get(path)
        minimum = int(requirement.get("min_articles", 1))
        if actual is None:
            failures.append(f"required act missing: {requirement['codice_redazionale']}")
        elif actual < minimum:
            failures.append(f"{path} has {actual} articles, expected at least {minimum}")
        anchors = set(report.document_anchors.get(path, []))
        for anchor in requirement.get("required_anchors", []):
            if anchor not in anchors:
                failures.append(f"{path} is missing required anchor {anchor}")
    if failures:
        raise QualityGateError("coverage gate failed:\n- " + "\n- ".join(failures))
    return [gap["description"] for gap in config.get("known_gaps", [])]


def write_indexes(output: Path, candidates: list[Candidate], report: ConversionReport,
                  collections_requested: int, collections_downloaded: int,
                  known_gaps: list[str] | None = None,
                  memberships: dict[str, set[str]] | None = None) -> dict:
    memberships = memberships or {}
    type_counts: Counter[str] = Counter()
    year_counts: Counter[str] = Counter()
    urn_index: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        urn = candidate.metadata.urn or ""
        memberships.setdefault(candidate.collection, set()).add(urn)
        type_counts[candidate.metadata.tipo or "ignoto"] += 1
        year_counts[(candidate.metadata.data or "ignoto")[:4]] += 1
        urn_index[urn] = {
            "path": candidate.repo_path,
            "codice_redazionale": candidate.metadata.codice_redazionale or "",
        }
    collections_dir = output / "collections"
    collections_dir.mkdir(exist_ok=True)
    for name, urns in sorted(memberships.items()):
        slug = "-".join(name.casefold().split())
        (collections_dir / f"{slug}.json").write_text(
            json.dumps({"schema_version": SCHEMA_VERSION, "name": name, "urns": sorted(urns)},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (output / "urn-index.json").write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "documents": urn_index},
                   ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "coverage_date": date.today().isoformat(),
        "source_format": "AKN",
        "collections": {"requested": collections_requested, "downloaded": collections_downloaded},
        "counts": {
            "xml_received": report.xml_received, "acts": report.converted,
            "articles": report.articles, "skipped": report.skipped,
            "duplicates": report.duplicates, "internal_links": report.internal_links,
            "external_links": report.external_links,
            "unresolved_links": report.unresolved_links,
        },
        "by_type": dict(sorted(type_counts.items())),
        "by_year": dict(sorted(year_counts.items())),
        "errors": [vars(error) for error in report.errors],
        "unsupported_tags": sorted(report.unsupported_tags),
        "known_gaps": known_gaps or [],
        "files": dict(sorted(report.hashes.items())),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def write_delta(previous: dict | None, current: dict, output: Path) -> Path:
    before = (previous or {}).get("files", {})
    after = current.get("files", {})
    delta = {
        "schema_version": SCHEMA_VERSION,
        "from": (previous or {}).get("coverage_date"),
        "to": current.get("coverage_date"),
        "added": [{"path": p, "sha256": after[p]} for p in sorted(after.keys() - before.keys())],
        "removed": [{"path": p, "sha256": before[p]} for p in sorted(before.keys() - after.keys())],
        "modified": [
            {"path": p, "before": before[p], "after": after[p]}
            for p in sorted(before.keys() & after.keys()) if before[p] != after[p]
        ],
    }
    path = output / "delta.json"
    path.write_text(json.dumps(delta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def build_sqlite(snapshot: Path, destination: Path) -> None:
    connection = sqlite3.connect(destination)
    try:
        connection.executescript("""
            CREATE TABLE documents (
              urn TEXT PRIMARY KEY, codice_redazionale TEXT UNIQUE NOT NULL,
              tipo TEXT, data TEXT, titolo TEXT, stato_atto TEXT,
              valid_from TEXT, valid_to TEXT, path TEXT NOT NULL, text TEXT NOT NULL
            );
            CREATE INDEX documents_filters ON documents(tipo, data, stato_atto, valid_from, valid_to);
            CREATE VIRTUAL TABLE documents_fts USING fts5(urn UNINDEXED, titolo, text);
        """)
        from .frontmatter import read_frontmatter
        for path in sorted((snapshot / "atti").glob("*.md")):
            metadata = read_frontmatter(path)
            raw = path.read_text(encoding="utf-8")
            body = raw.split("\n---\n", 1)[-1].strip()
            row = (
                metadata.get("urn"), metadata.get("codice_redazionale"), metadata.get("tipo"),
                metadata.get("data"), metadata.get("titolo"), metadata.get("stato_atto"),
                metadata.get("entrata_in_vigore"), metadata.get("abrogazione_data"),
                path.relative_to(snapshot).as_posix(), body,
            )
            connection.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)", row)
            connection.execute("INSERT INTO documents_fts VALUES (?,?,?)", (row[0], row[4], body))
        connection.commit()
    finally:
        connection.close()


def _write_jsonl(snapshot: Path, destination: Path) -> None:
    from .frontmatter import read_frontmatter
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        for path in sorted((snapshot / "atti").glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            record = read_frontmatter(path) | {
                "path": path.relative_to(snapshot).as_posix(),
                "text": raw.split("\n---\n", 1)[-1].strip(),
            }
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def build_artifacts(snapshot: Path, artifacts: Path) -> list[Path]:
    """Build release files. zstandard and pyarrow are required distribution dependencies."""
    import pyarrow.json as pajson
    import pyarrow.parquet as parquet
    import zstandard

    artifacts.mkdir(parents=True, exist_ok=True)
    tar_path = artifacts / "markdown.tar"
    with tarfile.open(tar_path, "w") as archive:
        for path in sorted(snapshot.rglob("*")):
            if path.is_file():
                info = archive.gettarinfo(str(path), path.relative_to(snapshot).as_posix())
                info.mtime = 0
                with path.open("rb") as source:
                    archive.addfile(info, source)
    compressor = zstandard.ZstdCompressor(level=10, threads=0)
    outputs: list[Path] = []
    for archive_source, name in ((tar_path, "markdown.tar.zst"),):
        target = artifacts / name
        with archive_source.open("rb") as source, target.open("wb") as destination:
            compressor.copy_stream(source, destination)
        outputs.append(target)
        archive_source.unlink()
    jsonl = artifacts / "corpus.jsonl"
    _write_jsonl(snapshot, jsonl)
    jsonl_zst = artifacts / "corpus.jsonl.zst"
    with jsonl.open("rb") as source, jsonl_zst.open("wb") as destination:
        compressor.copy_stream(source, destination)
    outputs.append(jsonl_zst)
    table = pajson.read_json(jsonl)
    parquet_path = artifacts / "corpus.parquet"
    parquet.write_table(table, parquet_path, compression="zstd")
    outputs.append(parquet_path)
    jsonl.unlink()
    sqlite_path = artifacts / "corpus.sqlite"
    build_sqlite(snapshot, sqlite_path)
    outputs.append(sqlite_path)
    for name in ("manifest.json", "urn-index.json", "delta.json"):
        target = artifacts / name
        shutil.copy2(snapshot / name, target)
        outputs.append(target)
    sums = artifacts / "SHA256SUMS"
    sums.write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in outputs
    ), encoding="ascii")
    return outputs + [sums]
