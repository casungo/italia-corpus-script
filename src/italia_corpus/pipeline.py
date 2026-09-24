"""Fail-closed, all-collections snapshot pipeline."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile, ZipInfo

import requests
from github import Github

from .config import (
    DOWNLOAD_MAX_ATTEMPTS, DOWNLOAD_RETRY_SLEEP_SEC, DOWNLOAD_TIMEOUT, ENDPOINT_URL,
    GIT_AUTHOR_EMAIL, GIT_AUTHOR_NAME, GITHUB_USERNAME, GIT_TARGET_BRANCH, GIT_TARGET_TOKEN,
    GIT_TARGET_URL, GIT_TARGET_USERNAME, PUBLISH_TARGET, TARGET_REPO_NAME, logger,
)
from .akn import AknFrontmatter
from .converter import (
    UPSTREAM_TRUNCATION_ERROR, Candidate, ConversionError, ConversionReport, discover_candidate,
    discover_candidates, render_candidates, select_canonical,
)
from .filename import collection_subdir_name, safe_repo_name
from .git_ops import git, push_snapshot, rollback_snapshot, stage_snapshot
from .github_client import get_or_create_repo
from .normattiva import collection_download_params, fetch_predefined_collections, merge_collections_by_name
from .snapshot import (
    QualityGateError, build_artifacts, build_legacy_archive, safe_zip_members, validate_report,
    validate_required_coverage, write_delta, write_indexes,
)
from .supplemental import fetch_missing_sources

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://dati.normattiva.it",
}
NORMATTIVA_DETAIL_URL = "https://www.normattiva.it/atto/caricaDettaglioAtto"
NORMATTIVA_AKN_URL = "https://www.normattiva.it/do/atto/caricaAKN"
_MEMBER_ID = re.compile(r"^(\d{4}-\d{2}-\d{2})_([0-9A-Z]+)_")
SMOKE_XML_PER_COLLECTION = 1_000
CACHE_INVENTORY = "inventory.json"
FINGERPRINTS_FILE = "fingerprints.json"
DISCOVERY_CACHE_VERSION = 2
MAX_RELEASE_ASSET_BYTES = 2 * 1024 * 1024 * 1024 - 1
DISCOVERY_WORKERS = min(8, max(1, (os.cpu_count() or 1) * 2))
_DISCOVERY_ARCHIVE: ZipFile | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_zip(path: Path) -> tuple[str, int]:
    """Checksum the bytes and validate the central directory.

    Member CRC errors surface as BadZipFile when discovery reads each member,
    so decompressing everything here would only double the cost of cache hits.
    """
    with ZipFile(path) as archive:
        members = list(safe_zip_members(archive))
        if not members:
            raise ValueError("ZIP archive has no files")
    return _sha256(path), len(members)


def _load_cache_inventory(cache_root: Path) -> dict:
    path = cache_root / CACHE_INVENTORY
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") == 1 and isinstance(value.get("archives"), dict):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return {"schema_version": 1, "archives": {}}


def _write_cache_inventory(cache_root: Path, inventory: dict) -> None:
    path = cache_root / CACHE_INVENTORY
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _cache_zip(cache: Path, source: Path, params: dict, digest: str, members: int) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_suffix(".tmp")
    shutil.copy2(source, temporary)
    temporary.replace(cache)
    cache.with_suffix(cache.suffix + ".sha256").write_text(
        f"{digest}  {cache.name}\n", encoding="ascii"
    )
    inventory = _load_cache_inventory(cache.parent)
    inventory["archives"][cache.name] = {
        "collection": params.get("nome"),
        "format": params.get("formatoRichiesta"),
        "members": members,
        "sha256": digest,
        "size": cache.stat().st_size,
    }
    _write_cache_inventory(cache.parent, inventory)


def _archive_cache_name(collection: dict, params: dict) -> str:
    return "-".join((
        safe_repo_name(params["nome"]), params["formatoRichiesta"],
        str(collection.get("dataCreazione") or "unknown"),
    )) + ".zip"


def _prune_download_cache(
    cache_root: Path, archive_names: set[str], discovery_names: set[str]
) -> None:
    """Keep only inputs used by a successful snapshot."""
    inventory = _load_cache_inventory(cache_root)
    removed = 0
    for archive in cache_root.glob("*.zip"):
        if archive.name not in archive_names:
            archive.unlink()
            archive.with_suffix(archive.suffix + ".sha256").unlink(missing_ok=True)
            inventory["archives"].pop(archive.name, None)
            removed += 1
    for partial in cache_root.glob("*.zip.partial*"):
        partial.unlink()
    discovery_root = cache_root / "discovery"
    for discovery in discovery_root.glob(f"*-v{DISCOVERY_CACHE_VERSION}.json.gz"):
        if discovery.name not in discovery_names:
            discovery.unlink()
            removed += 1
    _write_cache_inventory(cache_root, inventory)
    if removed:
        logger.info("Pruned %s stale download-cache entries", removed)


def _restore_cached_zip(cache: Path, destination: Path) -> bool:
    if not cache.is_file():
        return False
    checksum = cache.with_suffix(cache.suffix + ".sha256")
    inventory = _load_cache_inventory(cache.parent)
    entry = inventory["archives"].get(cache.name)
    try:
        expected_line = checksum.read_text(encoding="ascii").strip()
        digest, members = _verify_zip(cache)
        if (
            expected_line != f"{digest}  {cache.name}"
            or not isinstance(entry, dict)
            or entry.get("sha256") != digest
            or entry.get("size") != cache.stat().st_size
            or entry.get("members") != members
        ):
            raise ValueError("cache checksum or inventory mismatch")
        shutil.copy2(cache, destination)
        return True
    except (OSError, BadZipFile, QualityGateError, ValueError):
        logger.warning("Discarding invalid cached ZIP %s", cache)
        cache.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)
        inventory["archives"].pop(cache.name, None)
        _write_cache_inventory(cache.parent, inventory)
        return False


def _download(params: dict, destination: Path, cache: Path | None = None) -> bool:
    started = time.perf_counter()
    if cache and cache.is_file():
        if _restore_cached_zip(cache, destination):
            logger.info(
                "Download collection=%r format=%s cache_hit=true elapsed=%.2fs",
                params.get("nome"), params.get("formatoRichiesta"), time.perf_counter() - started,
            )
            return True
    partial = (
        cache.parent / (
            f"{safe_repo_name(str(params.get('nome')))}-"
            f"{params.get('formatoRichiesta')}.zip.partial"
        )
        if cache else destination
    )
    partial_metadata = partial.with_suffix(partial.suffix + ".json")
    partial.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            headers = HEADERS | ({"Range": f"bytes={offset}-"} if offset else {})
            with requests.get(ENDPOINT_URL, params=params, headers=headers,
                              timeout=DOWNLOAD_TIMEOUT, stream=True) as response:
                response.raise_for_status()
                response_headers = getattr(response, "headers", {})
                resumed = offset > 0 and getattr(response, "status_code", 200) == 206
                if resumed and not response_headers.get("Content-Range", "").startswith(
                    f"bytes {offset}-"
                ):
                    raise ValueError("invalid range response")
                if resumed and cache:
                    try:
                        previous = json.loads(partial_metadata.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        previous = {}
                    current_etag = response_headers.get("X-ETag") or response_headers.get("ETag")
                    if (
                        previous.get("edition") != cache.name
                        and (not current_etag or current_etag != previous.get("etag"))
                    ):
                        raise ValueError("partial download belongs to a different archive")
                    partial_metadata.write_text(json.dumps({
                        "edition": cache.name,
                        "etag": current_etag,
                    }), encoding="utf-8")
                elif cache:
                    partial_metadata.write_text(json.dumps({
                        "edition": cache.name,
                        "etag": response_headers.get("X-ETag") or response_headers.get("ETag"),
                    }), encoding="utf-8")
                with partial.open("ab" if resumed else "wb") as stream:
                    for chunk in response.iter_content(8 * 1024 * 1024):
                        stream.write(chunk)
            if partial.stat().st_size == 0:
                raise ValueError("empty download")
            digest, members = _verify_zip(partial)
            shutil.copy2(partial, destination)
            if cache:
                _cache_zip(cache, destination, params, digest, members)
            partial.unlink(missing_ok=True)
            partial_metadata.unlink(missing_ok=True)
            logger.info(
                "Download collection=%r format=%s cache_hit=false resumed=%s "
                "attempt=%d elapsed=%.2fs",
                params.get("nome"), params.get("formatoRichiesta"), str(resumed).lower(),
                attempt, time.perf_counter() - started,
            )
            return False
        except requests.RequestException as exc:
            last_error = exc
            if attempt < DOWNLOAD_MAX_ATTEMPTS:
                time.sleep(DOWNLOAD_RETRY_SLEEP_SEC * attempt)
        except (OSError, BadZipFile, QualityGateError, ValueError) as exc:
            last_error = exc
            partial.unlink(missing_ok=True)
            partial_metadata.unlink(missing_ok=True)
            if attempt < DOWNLOAD_MAX_ATTEMPTS:
                time.sleep(DOWNLOAD_RETRY_SLEEP_SEC * attempt)
    name = params.get("nome", "unknown collection")
    raise RuntimeError(
        f"{name}: download failed after {DOWNLOAD_MAX_ATTEMPTS} attempts: {last_error}"
    )


def _load_previous_manifest(clone: Path) -> dict | None:
    path = clone / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _discovery_cache_path(cache_root: Path, collection: dict) -> Path:
    params = collection_download_params(collection)
    edition = str(collection.get("dataCreazione") or "unknown")
    name = safe_repo_name(params["nome"])
    return cache_root / "discovery" / (
        f"{name}-{params['formatoRichiesta']}-{edition}-v{DISCOVERY_CACHE_VERSION}.json.gz"
    )


def _load_fingerprints(cache_root: Path) -> dict:
    path = cache_root / FINGERPRINTS_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") == 1 and isinstance(value.get("collections"), dict):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return {"schema_version": 1, "collections": {}}


def _collection_fingerprint(collection: dict) -> dict:
    """Probe the download endpoint with a one-byte range instead of pulling the package."""
    params = collection_download_params(collection)
    response = requests.get(
        ENDPOINT_URL, params=params, headers=HEADERS | {"Range": "bytes=0-0"},
        timeout=DOWNLOAD_TIMEOUT,
    )
    response.raise_for_status()
    total = response.headers.get("Content-Range", "").rpartition("/")[2]
    return {
        "format": params["formatoRichiesta"],
        "numero_atti": int(collection.get("numeroAtti") or 0),
        "etag": response.headers.get("ETag") or response.headers.get("X-ETag") or "",
        "length": int(total) if total.isdigit() else 0,
    }


def store_upstream_fingerprints(cache_root: Path, collections: list[dict]) -> None:
    """Record the upstream packages a successful snapshot was built from."""
    fingerprints_by_name: dict[str, dict] = {}
    for collection in collections:
        params = collection_download_params(collection)
        fingerprints_by_name[params["nome"]] = _collection_fingerprint(collection)
    fingerprints = {"schema_version": 1, "collections": fingerprints_by_name}
    path = cache_root / FINGERPRINTS_FILE
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(fingerprints, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    logger.info("Stored upstream fingerprints for %d collections", len(fingerprints_by_name))


def upstream_collections_are_cached(cache_root: Path) -> bool:
    """Return false when upstream metadata needs a full, validated snapshot."""
    collections = merge_collections_by_name(fetch_predefined_collections())
    missing = [
        collection_download_params(collection)["nome"]
        for collection in collections
        if not _discovery_cache_path(cache_root, collection).is_file()
    ]
    if missing:
        logger.info("Upstream check requires a full snapshot: %s", ", ".join(missing))
        return False
    stored = _load_fingerprints(cache_root)["collections"]
    for collection in collections:
        params = collection_download_params(collection)
        fingerprint = _collection_fingerprint(collection)
        if stored.get(params["nome"]) != fingerprint:
            logger.info(
                "Upstream check: %s changed (stored %s, current %s)",
                params["nome"], stored.get(params["nome"]), fingerprint,
            )
            return False
    logger.info("Upstream check: %d collections unchanged", len(collections))
    return True


def _cache_discovery(path: Path, candidates: list[Candidate], report: ConversionReport) -> None:
    payload = {
        "version": DISCOVERY_CACHE_VERSION,
        "candidates": [{
            "collection": candidate.collection,
            "source_format": candidate.source_format,
            "metadata": vars(candidate.metadata),
            "source_articles": candidate.source_articles,
            "source": candidate.source,
            "path_suffix": candidate.path_suffix,
            "xml_path": candidate.xml_path.as_posix(),
            "archive_name": candidate.archive_name,
            "member_name": candidate.member_name,
        } for candidate in candidates],
        "report": report.to_dict(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
    temporary.replace(path)


def _restore_discovery(path: Path) -> tuple[list[Candidate], ConversionReport] | None:
    if not path.is_file():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
        if payload.get("version") != DISCOVERY_CACHE_VERSION:
            return None
        report_data = payload["report"]
        report = ConversionReport(
            xml_received=int(report_data["xml_received"]),
            skipped=int(report_data["skipped"]),
            errors=[ConversionError(**error) for error in report_data["errors"]],
            unsupported_tags=set(report_data["unsupported_tags"]),
            collections=report_data["collections"],
        )
        candidates = [Candidate(
            Path(item["xml_path"]), item["collection"], item["source_format"],
            AknFrontmatter(**item["metadata"]), None, int(item["source_articles"]),
            item["source"], item.get("path_suffix", ""), item.get("archive_name"),
            item.get("member_name"),
        ) for item in payload["candidates"]]
        return candidates, report
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("Discarding invalid discovery cache %s", path)
        path.unlink(missing_ok=True)
        return None


def _restore_discovery_sources(
    collection: dict,
    candidates: list[Candidate],
    destination: Path,
    cache_root: Path,
) -> set[str] | None:
    """Restore and validate the ZIP archive referenced by a discovery checkpoint."""
    archive_candidates = [candidate for candidate in candidates if candidate.archive_name is not None]
    if not archive_candidates:
        return set()
    archive_names = {candidate.archive_name for candidate in archive_candidates}
    formats = {candidate.source_format for candidate in archive_candidates}
    if len(archive_names) != 1 or len(formats) != 1:
        return None
    params = collection_download_params(collection) | {"formatoRichiesta": formats.pop()}
    archive_name = archive_names.pop()
    if archive_name != _archive_cache_name(collection, params):
        return None
    _download(params, destination, cache_root / archive_name)
    try:
        with ZipFile(destination) as archive:
            for candidate in archive_candidates:
                if candidate.member_name is None:
                    return None
                archive.getinfo(candidate.member_name)
    except (BadZipFile, KeyError):
        return None
    return {archive_name}


def _release_assets(artifacts: list[Path]) -> list[Path]:
    """Split artifacts that GitHub Releases cannot accept as one upload."""
    assets: list[Path] = []
    for artifact in artifacts:
        if artifact.name == "SHA256SUMS" or artifact.stat().st_size <= MAX_RELEASE_ASSET_BYTES:
            assets.append(artifact)
            continue
        remaining = artifact.stat().st_size
        with artifact.open("rb") as source:
            for number in range(1, 10_000):
                part = artifact.with_name(f"{artifact.name}.part{number:03d}")
                part_size = min(MAX_RELEASE_ASSET_BYTES, remaining)
                with part.open("wb") as destination:
                    while part_size:
                        block = source.read(min(8 * 1024 * 1024, part_size))
                        if not block:
                            raise OSError(f"unexpected EOF while splitting {artifact}")
                        destination.write(block)
                        part_size -= len(block)
                assets.append(part)
                remaining -= part.stat().st_size
                if not remaining:
                    break
        artifact.unlink()
        logger.info("Split release asset %s into %d parts", artifact.name, number)
    return assets


def _download_collection(collection: dict, destination: Path, cache_root: Path) -> tuple[dict, bool]:
    params = collection_download_params(collection)
    preferred = params["formatoRichiesta"]
    available = collection.get("formatiDisponibili") or [preferred]
    failures = []
    formats = [
        preferred,
        *(value for value in ("V", "O", "M") if value in available and value != preferred),
    ]
    for source_format in formats:
        attempt = params | {"formatoRichiesta": source_format}
        cache_name = _archive_cache_name(collection, attempt)
        try:
            cache_hit = _download(attempt, destination, cache_root / cache_name)
            if source_format != preferred:
                logger.warning(
                    "Using %s instead of unavailable preferred format %s for %r",
                    source_format, preferred, attempt["nome"],
                )
            return attempt, cache_hit
        except RuntimeError as exc:
            failures.append(str(exc))
    raise RuntimeError("all advertised formats failed:\n- " + "\n- ".join(failures))


def _download_collection_for_run(
    collection: dict,
    destination: Path,
    cache_root: Path,
    *,
    smoke_test: bool,
) -> tuple[dict, bool] | None:
    try:
        return _download_collection(collection, destination, cache_root)
    except RuntimeError as exc:
        if not smoke_test:
            raise
        logger.warning("Skipping unavailable collection during smoke test: %s", exc)
        return None


def _open_discovery_archive(path: Path) -> None:
    global _DISCOVERY_ARCHIVE
    _DISCOVERY_ARCHIVE = ZipFile(path)


def _discover_member(
    payload: tuple[str, str, str, str],
) -> tuple[Candidate | None, ConversionReport, bytes | None]:
    collection, source_format, archive_name, member = payload
    if _DISCOVERY_ARCHIVE is None:
        raise RuntimeError("discovery archive was not initialized")
    raw = _DISCOVERY_ARCHIVE.read(member)
    source = f"{collection}/{member}"
    report = ConversionReport()
    candidate = discover_candidate(collection, source_format, source, raw, report)
    if candidate:
        candidate = replace(
            candidate, content=None, archive_name=archive_name, member_name=member
        )
    return candidate, report, raw if candidate is None else None


def _merge_discovery_report(target: ConversionReport, source: ConversionReport) -> None:
    target.xml_received += source.xml_received
    target.skipped += source.skipped
    target.errors.extend(source.errors)
    for collection, counts in source.collections.items():
        for metric, amount in counts.items():
            target.increment(collection, metric, amount)


def _member_id(source: str) -> tuple[str, str]:
    match = _MEMBER_ID.match(PurePosixPath(source).name)
    if not match:
        raise ValueError(f"cannot identify Normattiva act from {source!r}")
    return match.group(1), match.group(2)


def _fetch_direct_akn(session: requests.Session, source: str, coverage_date: str) -> bytes:
    publication_date, editorial_code = _member_id(source)
    detail = session.get(NORMATTIVA_DETAIL_URL, params={
        "atto.dataPubblicazioneGazzetta": publication_date,
        "atto.codiceRedazionale": editorial_code,
        "atto.articolo.numero": "0",
        "atto.articolo.sottoArticolo": "1",
        "atto.articolo.sottoArticolo1": "0",
    }, timeout=DOWNLOAD_TIMEOUT)
    detail.raise_for_status()
    response = session.get(NORMATTIVA_AKN_URL, params={
        "dataGU": publication_date.replace("-", ""),
        "codiceRedaz": editorial_code,
        "dataVigenza": coverage_date.replace("-", ""),
    }, headers={"Referer": detail.url}, timeout=DOWNLOAD_TIMEOUT)
    response.raise_for_status()
    return response.content


def _recover_truncated_member(
    session: requests.Session,
    collection: str,
    source: str,
    coverage_date: str,
    recovery_cache: Path | None = None,
) -> tuple[Candidate, ConversionReport] | None:
    last_error = "invalid AKN payload"
    expected_code = _member_id(source)[1]
    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            report = ConversionReport()
            raw = _fetch_direct_akn(session, source, coverage_date)
            candidate = discover_candidate(collection, "V", f"{collection}/direct/{source}", raw, report)
            if candidate and candidate.metadata.codice_redazionale == expected_code:
                if recovery_cache:
                    recovery_cache.parent.mkdir(parents=True, exist_ok=True)
                    temporary = recovery_cache.with_suffix(".tmp")
                    temporary.write_text(candidate.content or "", encoding="utf-8")
                    temporary.replace(recovery_cache)
                    candidate = replace(candidate, xml_path=recovery_cache, content=None)
                return candidate, report
            last_error = (
                report.errors[-1].message
                if report.errors
                else f"direct AKN identity mismatch for {expected_code}"
            )
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
        if attempt < DOWNLOAD_MAX_ATTEMPTS:
            time.sleep(DOWNLOAD_RETRY_SLEEP_SEC * attempt)
    logger.warning("Direct AKN recovery failed for %s: %s", source, last_error)
    return None


def _stage_release(repo, tag: str, artifacts: list[Path]):
    release = repo.create_git_release(tag=tag, name=tag, message="Validated Italia Corpus snapshot", draft=True)
    try:
        for artifact in artifacts:
            release.upload_asset(str(artifact), label=artifact.name)
        return release
    except Exception:
        release.delete_release()
        raise


def _detect_collection_fallbacks(
    candidates_by_urn: dict[str, Candidate],
    previous: dict,
    source_dir: Path,
) -> tuple[list[str], dict[str, int]]:
    """Collections whose coverage regressed vs the published snapshot.

    Two regression shapes: fewer distinct URNs than published (collapse), or acts
    missing from the new edition (upstream switched package contents, es. V -> O).
    """
    current_urns: dict[str, set[str]] = {}
    per_collection: dict[str, int] = {}
    for candidate in candidates_by_urn.values():
        current_urns.setdefault(candidate.collection, set()).add(candidate.metadata.urn or "")
        per_collection[candidate.collection] = per_collection.get(candidate.collection, 0) + 1
    fallbacks = []
    for collection, counts in previous.get("by_collection", {}).items():
        prior = int(counts.get("converted", 0))
        collapsed = per_collection.get(collection, 0) < prior
        slug = "-".join(collection.casefold().split())
        membership = source_dir / "collections" / f"{slug}.json"
        lost_acts = False
        if membership.is_file():
            published_urns = set(json.loads(membership.read_text(encoding="utf-8"))["urns"])
            lost_acts = bool(published_urns - current_urns.get(collection, set()))
        if prior > 0 and (collapsed or lost_acts):
            fallbacks.append(collection)
    return sorted(fallbacks), per_collection


def _carry_previous_collection(
    source_dir: Path,
    collection: str,
    snapshot: Path,
    report: ConversionReport,
    previous: dict,
    carried_index: dict[str, str],
    memberships: dict[str, set[str]],
) -> dict:
    """Freeze a regressed collection at its published content and account for it in the report."""
    slug = "-".join(collection.casefold().split())
    membership = json.loads(
        (source_dir / "collections" / f"{slug}.json").read_text(encoding="utf-8")
    )["urns"]
    documents = json.loads(
        (source_dir / "urn-index.json").read_text(encoding="utf-8")
    )["documents"]
    carried = {"collection": collection, "acts": 0, "internal_links": 0, "external_links": 0}
    carried_internal = 0
    carried_external = 0
    carried_acts = 0
    missing_index_entries = 0
    for urn in membership:
        entry = documents.get(urn)
        if not entry:
            missing_index_entries += 1
            continue
        path = entry["path"]
        source = source_dir / path
        if not source.is_file():
            continue
        markdown = source.read_text(encoding="utf-8")
        target = snapshot / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8", newline="\n")
        anchors = re.findall(r'<a id="([^"]+)"', markdown)
        internal = len(re.findall(r"\]\((?!https://www\.normattiva\.it)[^)]+\)", markdown))
        external = len(re.findall(r"\]\(https://www\.normattiva\.it[^)]*\)", markdown))
        report.hashes[path] = hashlib.sha256(markdown.encode()).hexdigest()
        report.document_anchors[path] = anchors
        report.document_articles[path] = len([a for a in anchors if a.startswith("art-")])
        report.internal_links += internal
        report.external_links += external
        carried_internal += internal
        carried_external += external
        report.converted += 1
        report.urns += 1
        report.editorial_codes += 1
        carried_index[urn] = path
        memberships.setdefault(collection, set()).add(urn)
        carried_acts += 1
    carried.update(acts=carried_acts, internal_links=carried_internal, external_links=carried_external)
    if missing_index_entries:
        logger.warning(
            "FALLBACK %s: %d previous urns have no urn-index entry and were skipped",
            collection, missing_index_entries,
        )
    previous_counts = previous.get("by_collection", {}).get(collection, {})
    current_counts = dict(report.collections.get(collection, {}))
    current_counts["converted"] = int(previous_counts.get("converted", carried_acts))
    current_counts["articles"] = int(previous_counts.get("articles", 0))
    report.collections[collection] = current_counts
    # il contenuto carried e' identico a quello pubblicato: il totale articoli della
    # collezione si ripristina verbatim dal manifest precedente
    report.articles += int(previous_counts.get("articles", 0))
    return carried


def extract_and_push(
    root_path: str,
    gh: Github | None,
    *,
    dry_run: bool = False,
    baseline: Path | None = None,
    smoke_test: bool = False,
    download_cache: Path | None = None,
) -> Path:
    """Build a validated snapshot and publish it unless ``dry_run`` is set."""
    if smoke_test and not dry_run:
        raise ValueError("smoke_test requires dry_run")
    work_root = Path(root_path)
    work_root.mkdir(parents=True, exist_ok=True)
    cache_root = download_cache or work_root / "download-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    collections = merge_collections_by_name(fetch_predefined_collections())
    if PUBLISH_TARGET not in {"github", "git"}:
        raise RuntimeError("PUBLISH_TARGET must be github or git")
    using_github = PUBLISH_TARGET == "github"
    if not dry_run and using_github and (not TARGET_REPO_NAME or not GITHUB_USERNAME or gh is None):
        raise RuntimeError("GITHUB_TARGET_REPO and GITHUB_USERNAME are required")
    if not dry_run and not using_github and not GIT_TARGET_URL:
        raise RuntimeError("GIT_TARGET_URL is required when PUBLISH_TARGET=git")
    repo = get_or_create_repo(gh, TARGET_REPO_NAME) if using_github and gh else None
    branch = (repo.default_branch or "main") if repo else GIT_TARGET_BRANCH
    clone_url = f"https://github.com/{repo.full_name}.git" if repo else GIT_TARGET_URL

    workspace = (
        nullcontext(tempfile.mkdtemp(prefix="italia-corpus-dry-run-", dir=work_root))
        if dry_run
        else tempfile.TemporaryDirectory(prefix="italia-corpus-", dir=work_root)
    )
    with workspace as temporary:
        root = Path(temporary)
        active_archives: set[str] = set()
        clone = root / "repo"
        if not dry_run:
            git(
                ["clone", "--depth=1", clone_url, str(clone)], str(root),
                github_auth=using_github, auth_token=GIT_TARGET_TOKEN,
                auth_username=GIT_TARGET_USERNAME,
            )
            git(["config", "user.email", str(GIT_AUTHOR_EMAIL)], str(clone))
            git(["config", "user.name", str(GIT_AUTHOR_NAME)], str(clone))
        previous = _load_previous_manifest(baseline or clone)
        spool = root / "spool"
        spool.mkdir()
        candidates_by_urn: dict[str, Candidate] = {}
        memberships: dict[str, set[str]] = {}
        article_counts: dict[str, int] = {}
        rejected: list[dict[str, str]] = []

        def retain(candidates: list[Candidate]) -> None:
            for candidate in candidates:
                urn = candidate.metadata.urn or ""
                code = candidate.metadata.codice_redazionale or ""
                memberships.setdefault(candidate.collection, set()).add(urn)
                article_counts[code] = max(article_counts.get(code, 0), candidate.source_articles)
                previous_candidate = candidates_by_urn.get(urn)
                if previous_candidate:
                    report.duplicates += 1
                if previous_candidate and previous_candidate.rank() <= candidate.rank():
                    continue
                if candidate.archive_name is not None or candidate.content is None:
                    candidates_by_urn[urn] = replace(candidate, content=None)
                    continue
                target = spool / f"{hashlib.sha256(urn.encode()).hexdigest()}.xml"
                target.write_text(candidate.content, encoding="utf-8")
                candidates_by_urn[urn] = replace(candidate, xml_path=target, content=None)

        report = ConversionReport()
        collections_downloaded = 0
        logger.info("Discovery workers: %d", DISCOVERY_WORKERS)
        for number, collection in enumerate(collections, 1):
            collection_started = time.perf_counter()
            name = collection_download_params(collection)["nome"]
            logger.info(
                "Collection %d/%d start name=%r formats=%s",
                number, len(collections), name,
                ",".join(collection.get("formatiDisponibili") or []),
            )
            discovery_cache = _discovery_cache_path(cache_root, collection)
            if cached_discovery := _restore_discovery(discovery_cache):
                cached_candidates, cached_report = cached_discovery
                archive = root / f"{safe_repo_name(name)}.zip"
                active = _restore_discovery_sources(
                    collection, cached_candidates, archive, cache_root
                )
                archive.unlink(missing_ok=True)
                if active is not None:
                    active_archives.update(active)
                    _merge_discovery_report(report, cached_report)
                    retain(cached_candidates)
                    collections_downloaded += 1
                    logger.info(
                        "Collection %d/%d done name=%r discovery_cache_hit=true candidates=%d",
                        number, len(collections), name, len(cached_candidates),
                    )
                    continue
                logger.warning("Discarding incoherent discovery cache %s", discovery_cache)
                discovery_cache.unlink(missing_ok=True)
            archive = root / f"{safe_repo_name(name)}.zip"
            download = _download_collection_for_run(
                collection, archive, cache_root, smoke_test=smoke_test
            )
            if download is None:
                continue
            params, cache_hit = download
            archive_name = _archive_cache_name(collection, params)
            active_archives.add(archive_name)
            collections_downloaded += 1
            pending_truncations: list[tuple[ZipInfo, ConversionReport]] = []

            def reject(
                member: ZipInfo, partial_report: ConversionReport, raw: bytes | None
            ) -> None:
                if not dry_run:
                    return
                assert raw is not None
                source = f"{name}/{member.filename}"
                rejected_dir = root / "rejected"
                rejected_dir.mkdir(exist_ok=True)
                digest = hashlib.sha256(source.encode()).hexdigest()
                path = rejected_dir / f"{digest}.xml"
                path.write_bytes(raw)
                rejected.append({
                    "collection": name,
                    "source": member.filename,
                    "path": path.relative_to(root).as_posix(),
                    "error": partial_report.errors[-1].message,
                })

            with ZipFile(archive) as zf:
                members = [
                    member for member in safe_zip_members(zf)
                    if member.filename.casefold().endswith(".xml")
                ]
            if smoke_test:
                members = members[:SMOKE_XML_PER_COLLECTION]
            xml_seen = len(members)
            collection_candidates: list[Candidate] = []
            collection_report = ConversionReport()
            progress_step = max(1, (xml_seen + 19) // 20)
            next_progress = progress_step
            with ProcessPoolExecutor(
                max_workers=DISCOVERY_WORKERS,
                initializer=_open_discovery_archive,
                initargs=(archive,),
            ) as discovery_pool:
                batch_size = max(DISCOVERY_WORKERS * 4, 1)
                for offset in range(0, xml_seen, batch_size):
                    batch = members[offset:offset + batch_size]
                    payloads = [
                        (name, params["formatoRichiesta"], archive_name, member.filename)
                        for member in batch
                    ]
                    results = discovery_pool.map(_discover_member, payloads, chunksize=8)
                    for member, (candidate, partial_report, rejected_raw) in zip(
                        batch, results, strict=True
                    ):
                        if candidate:
                            _merge_discovery_report(report, partial_report)
                            _merge_discovery_report(collection_report, partial_report)
                            retain([candidate])
                            collection_candidates.append(candidate)
                        elif (
                            partial_report.errors[-1].message == UPSTREAM_TRUNCATION_ERROR
                        ):
                            pending_truncations.append((member, partial_report))
                        else:
                            _merge_discovery_report(report, partial_report)
                            _merge_discovery_report(collection_report, partial_report)
                            assert rejected_raw is not None
                            reject(member, partial_report, rejected_raw)
                    processed = offset + len(batch)
                    if processed >= next_progress or processed == xml_seen:
                        logger.info(
                            "Collection %d/%d discovery=%d/%d (%.0f%%)",
                            number, len(collections), processed, xml_seen,
                            processed * 100 / xml_seen,
                        )
                        next_progress = processed + progress_step
            if pending_truncations:
                recovered_count = 0
                recovery_step = max(1, (len(pending_truncations) + 19) // 20)
                with requests.Session() as session, ZipFile(archive) as primary_archive:
                    session.headers["User-Agent"] = HEADERS["User-Agent"]
                    for recovery_number, (member, partial_report) in enumerate(pending_truncations, 1):
                        fallback = _recover_truncated_member(
                            session,
                            name,
                            member.filename,
                            str(collection.get("dataCreazione") or ""),
                            cache_root / "recovery" / (
                                hashlib.sha256(
                                    f"{name}\0{member.filename}\0{collection.get('dataCreazione') or ''}".encode()
                                ).hexdigest()
                                + ".xml"
                            ),
                        )
                        if fallback:
                            candidate, fallback_report = fallback
                            _merge_discovery_report(report, fallback_report)
                            _merge_discovery_report(collection_report, fallback_report)
                            retain([candidate])
                            collection_candidates.append(candidate)
                            recovered_count += 1
                        else:
                            _merge_discovery_report(report, partial_report)
                            _merge_discovery_report(collection_report, partial_report)
                            reject(
                                member,
                                partial_report,
                                primary_archive.read(member) if dry_run else None,
                            )
                        if (
                            recovery_number % recovery_step == 0
                            or recovery_number == len(pending_truncations)
                        ):
                            logger.info(
                                "Collection %d/%d recovery=%d/%d (%.0f%%)",
                                number, len(collections), recovery_number,
                                len(pending_truncations),
                                recovery_number * 100 / len(pending_truncations),
                            )
                logger.info(
                    "Recovered %d/%d truncated payloads from direct AKN export for %r",
                    recovered_count, len(pending_truncations), name,
                )
            _cache_discovery(discovery_cache, collection_candidates, collection_report)
            archive.unlink()
            logger.info(
                "Collection %d/%d done name=%r format=%s cache_hit=%s xml=%d elapsed=%.2fs",
                number, len(collections), name, params["formatoRichiesta"],
                str(cache_hit).lower(), xml_seen, time.perf_counter() - collection_started,
            )

        if rejected:
            (root / "rejected" / "index.json").write_text(
                json.dumps(rejected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

        supplemental = (
            []
            if smoke_test
            else fetch_missing_sources(article_counts, cache_root / "supplemental")
        )
        retain(discover_candidates(supplemental, report))
        carried_index: dict[str, str] = {}
        fallbacks: list[dict] = []
        fallback_collections: list[str] = []
        per_collection_urns: dict[str, int] = {}
        if previous is not None:
            fallback_collections, per_collection_urns = _detect_collection_fallbacks(
                candidates_by_urn, previous, baseline or clone
            )
            for name in fallback_collections:
                for urn in [u for u, c in candidates_by_urn.items() if c.collection == name]:
                    del candidates_by_urn[urn]
                info = _carry_previous_collection(
                    baseline or clone, name, root / "snapshot", report, previous,
                    carried_index, memberships,
                )
                fallbacks.append(info)
                logger.error(
                    "FALLBACK %s: the new edition covers %d acts vs %d published; carried the "
                    "published content into the snapshot and marked it in the manifest",
                    name, per_collection_urns.get(name, 0),
                    int(previous.get("by_collection", {}).get(name, {}).get("converted", 0)),
                )
        duplicates = report.duplicates
        canonical = select_canonical(list(candidates_by_urn.values()), report)
        report.duplicates = duplicates
        snapshot = root / "snapshot"
        render_candidates(canonical, snapshot, report, cache_root, carried_index=carried_index)
        requirements = Path(__file__).parents[2] / "coverage-requirements.json"
        # prima la coerenza del report: un render error reale non deve essere sepolto dal gate di copertura
        validate_report(report, previous, Path(__file__).parents[2] / "quality-exceptions.json")
        known_gaps = [] if smoke_test else validate_required_coverage(report, requirements)
        manifest = write_indexes(
            snapshot,
            canonical,
            report,
            len(collections),
            collections_downloaded + len(supplemental),
            known_gaps,
            memberships,
            fallbacks,
            {urn: {"path": path} for urn, path in carried_index.items()},
        )
        write_delta(previous, manifest, snapshot)
        artifacts = build_artifacts(snapshot, root / "artifacts")
        if dry_run:
            shutil.rmtree(root / "sources", ignore_errors=True)
            for archive in root.glob("*.zip"):
                archive.unlink()
            logger.info("Dry-run output: %s", root)
            return root
        legacy_dirs = [collection_subdir_name(c["nomeCollezione"]) for c in collections]
        legacy_archive = build_legacy_archive(clone, legacy_dirs, root / "artifacts")
        if legacy_archive:
            artifacts.append(legacy_archive)
        staged = stage_snapshot(snapshot, clone, legacy_dirs)
        if staged:
            tag, previous_sha = staged
            if using_github:
                assert repo is not None
                release = _stage_release(repo, tag, _release_assets(artifacts))
                try:
                    push_snapshot(clone, branch, tag)
                    release.update_release(name=tag, message="Validated Italia Corpus snapshot", draft=False)
                except Exception:
                    rollback_snapshot(clone, branch, tag, previous_sha)
                    release.delete_release()
                    raise
            else:
                push_snapshot(
                    clone, branch, tag, github_auth=False, auth_token=GIT_TARGET_TOKEN,
                    auth_username=GIT_TARGET_USERNAME,
                )
        _prune_download_cache(
            cache_root,
            active_archives,
            {_discovery_cache_path(cache_root, collection).name for collection in collections},
        )
        store_upstream_fingerprints(cache_root, collections)
        logger.info("Published %s acts from %s XML files", report.converted, report.xml_received)
        return root
