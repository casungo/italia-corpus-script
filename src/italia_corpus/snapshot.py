"""Snapshot validation, manifests and deterministic distribution artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import stat
import tarfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZipInfo

from .converter import Candidate, ConversionReport

SCHEMA_VERSION = 3


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


def _allowed_regressions(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    today = date.today().isoformat()
    return [row for row in rows if row.get("reason") and row.get("expires", "") >= today]


def _is_allowed(rows: list[dict], metric: str, collection: str, actual: int) -> bool:
    return any(
        row.get("metric") == metric
        and row.get("collection") == collection
        and row.get("expected_value") == actual
        for row in rows
    )


def _error_is_allowed(rows: list[dict], metric: str, collection: str, source: str) -> bool:
    return any(
        row.get("metric") == metric
        and row.get("collection") == collection
        and isinstance(row.get("expected_value"), list)
        and any(str(value) in source for value in row["expected_value"])
        for row in rows
    )


def validate_report(report: ConversionReport, previous: dict | None = None,
                    exceptions_path: Path | None = None) -> None:
    allowed = _allowed_regressions(exceptions_path)
    failures: list[str] = []
    unallowed = [
        error for error in report.errors
        if not _error_is_allowed(allowed, error.metric, error.collection, error.source)
    ]
    accounted_skips = sum(
        1 for error in report.errors if error.metric in {"invalid_xml", "render_error"}
    )
    if report.skipped > accounted_skips:
        failures.append(f"{report.skipped - accounted_skips} skipped XML lack an error record")
    if unallowed:
        failures.append(f"{report.skipped} XML skipped, {len(unallowed)} unexcepted errors")
        failures.extend(f"{error.source}: {error.message}" for error in unallowed[:20])
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
            if new < prior and not _is_allowed(allowed, metric, "*", new):
                failures.append(f"{metric} regressed from {prior} to {new}")
        prior_external = int(old.get("external_links", 0))
        if report.external_links > prior_external and not _is_allowed(
            allowed, "external_links", "*", report.external_links
        ):
            failures.append(f"external_links increased from {prior_external} to {report.external_links}")
        for path, digest in previous.get("files", {}).items():
            if path not in report.hashes and not _is_allowed(allowed, "removed_files", "*", 1):
                failures.append(f"previous document disappeared: {path} ({digest[:12]})")
        for collection, counts in previous.get("by_collection", {}).items():
            current = report.collections.get(collection, {})
            for metric in ("xml_received", "converted", "articles"):
                old_value = int(counts.get(metric, 0))
                new_value = int(current.get(metric, 0))
                if new_value < old_value and not _is_allowed(
                    allowed, metric, collection, new_value
                ):
                    failures.append(
                        f"{collection}.{metric} regressed from {old_value} to {new_value}"
                    )
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
    code_index: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        urn = candidate.metadata.urn or ""
        memberships.setdefault(candidate.collection, set()).add(urn)
        type_counts[candidate.metadata.tipo or "ignoto"] += 1
        year_counts[(candidate.metadata.data or "ignoto")[:4]] += 1
        urn_index[urn] = {
            "path": candidate.repo_path,
            "codice_redazionale": candidate.metadata.codice_redazionale or "",
        }
        code_index[candidate.metadata.codice_redazionale or ""] = {
            "path": candidate.repo_path,
            "urn": urn,
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
        json.dumps({"schema_version": SCHEMA_VERSION, "documents": urn_index,
                    "by_codice_redazionale": code_index},
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
        "by_collection": dict(sorted(report.collections.items())),
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
            CREATE TABLE articles (
              document_urn TEXT NOT NULL REFERENCES documents(urn), anchor TEXT NOT NULL,
              version INTEGER NOT NULL, valid_from TEXT, valid_to TEXT, text TEXT NOT NULL,
              PRIMARY KEY (document_urn, anchor, version)
            );
            CREATE INDEX articles_validity ON articles(document_urn, valid_from, valid_to);
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
            matches = list(re.finditer(
                r'<a id="([^"]+)" data-akn-name="article"(?: data-valid-from="([^"]+)")?'
                r'(?: data-valid-to="([^"]+)")?></a>\n(?=#+ )', body
            ))
            versions: Counter[str] = Counter()
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
                article_text = body[match.end():end].strip()
                versions[match[1]] += 1
                connection.execute(
                    "INSERT INTO articles VALUES (?,?,?,?,?,?)",
                    (row[0], match[1], versions[match[1]], match[2], match[3], article_text),
                )
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
    with jsonl.open("rb") as stream:
        longest_record = max(map(len, stream), default=0)
    table = pajson.read_json(
        jsonl,
        read_options=pajson.ReadOptions(block_size=max(1 << 20, longest_record + 1)),
    )
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


def build_legacy_archive(repository: Path, legacy_dirs: list[str], artifacts: Path) -> Path | None:
    """Archive legacy collection directories once, before the atomic snapshot replaces them."""
    sources = [repository / name for name in legacy_dirs if (repository / name).is_dir()]
    if not sources:
        return None
    import zstandard

    tar_path = artifacts / "legacy-corpus.tar"
    with tarfile.open(tar_path, "w") as archive:
        for source in sorted(sources):
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(repository).as_posix()
                    info = archive.gettarinfo(str(path), relative)
                    info.mtime = 0
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
    destination = artifacts / "legacy-corpus.tar.zst"
    with tar_path.open("rb") as input_stream, destination.open("wb") as output_stream:
        zstandard.ZstdCompressor(level=10, threads=0).copy_stream(input_stream, output_stream)
    tar_path.unlink()
    sums = artifacts / "SHA256SUMS"
    with sums.open("a", encoding="ascii") as stream:
        stream.write(f"{hashlib.sha256(destination.read_bytes()).hexdigest()}  {destination.name}\n")
    return destination
