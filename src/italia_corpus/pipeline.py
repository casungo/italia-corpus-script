"""Fail-closed, all-collections snapshot pipeline."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import requests
from github import Github

from .config import (
    DOWNLOAD_MAX_ATTEMPTS, DOWNLOAD_RETRY_SLEEP_SEC, DOWNLOAD_TIMEOUT, ENDPOINT_URL,
    GIT_AUTHOR_EMAIL, GIT_AUTHOR_NAME, GITHUB_USERNAME, TARGET_REPO_NAME, logger,
)
from .converter import (
    Candidate, ConversionReport, discover_candidate, discover_candidates, render_candidates,
    select_canonical,
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
    "User-Agent": "italia-corpus/2 (+https://github.com/ahmeabd/italia-corpus-script)",
    "Accept": "application/zip,application/octet-stream",
}
SMOKE_XML_PER_COLLECTION = 1_000
CACHE_INVENTORY = "inventory.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_zip(path: Path) -> tuple[str, int]:
    """Read every member so truncated data and CRC errors fail before cache reuse."""
    with ZipFile(path) as archive:
        members = list(safe_zip_members(archive))
        if not members:
            raise ValueError("ZIP archive has no files")
        if bad_member := archive.testzip():
            raise ValueError(f"corrupt ZIP member: {bad_member}")
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
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            with requests.get(ENDPOINT_URL, params=params, headers=HEADERS,
                              timeout=DOWNLOAD_TIMEOUT, stream=True) as response:
                response.raise_for_status()
                with destination.open("wb") as stream:
                    for chunk in response.iter_content(8 * 1024 * 1024):
                        stream.write(chunk)
            if destination.stat().st_size == 0:
                destination.unlink()
                raise ValueError("empty download")
            digest, members = _verify_zip(destination)
            if cache:
                _cache_zip(cache, destination, params, digest, members)
            logger.info(
                "Download collection=%r format=%s cache_hit=false attempt=%d elapsed=%.2fs",
                params.get("nome"), params.get("formatoRichiesta"), attempt,
                time.perf_counter() - started,
            )
            return False
        except (
            requests.RequestException, OSError, BadZipFile, QualityGateError, ValueError
        ) as exc:
            last_error = exc
            destination.unlink(missing_ok=True)
            if attempt < DOWNLOAD_MAX_ATTEMPTS:
                time.sleep(DOWNLOAD_RETRY_SLEEP_SEC * attempt)
    name = params.get("nome", "unknown collection")
    raise RuntimeError(
        f"{name}: download failed after {DOWNLOAD_MAX_ATTEMPTS} attempts: {last_error}"
    )


def _load_previous_manifest(clone: Path) -> dict | None:
    path = clone / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


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
        cache_name = "-".join((
            safe_repo_name(attempt["nome"]), source_format,
            str(collection.get("dataCreazione") or "unknown"),
        )) + ".zip"
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


def _stage_release(repo, tag: str, artifacts: list[Path]):
    release = repo.create_git_release(tag=tag, name=tag, message="Validated Italia Corpus snapshot", draft=True)
    try:
        for artifact in artifacts:
            release.upload_asset(str(artifact), label=artifact.name)
        return release
    except Exception:
        release.delete_release()
        raise


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
    if not dry_run and (not TARGET_REPO_NAME or not GITHUB_USERNAME or gh is None):
        raise RuntimeError("GITHUB_TARGET_REPO and GITHUB_USERNAME are required")
    if not dry_run:
        assert gh is not None
    repo = get_or_create_repo(gh, TARGET_REPO_NAME) if gh else None
    branch = (repo.default_branch or "main") if repo else "main"
    clone_url = f"https://github.com/{GITHUB_USERNAME}/{TARGET_REPO_NAME}.git"

    workspace = (
        nullcontext(tempfile.mkdtemp(prefix="italia-corpus-dry-run-", dir=work_root))
        if dry_run
        else tempfile.TemporaryDirectory(prefix="italia-corpus-", dir=work_root)
    )
    with workspace as temporary:
        root = Path(temporary)
        clone = root / "repo"
        if not dry_run:
            git(["clone", "--depth=1", clone_url, str(clone)], str(root), github_auth=True)
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
                target = spool / f"{hashlib.sha256(urn.encode()).hexdigest()}.xml"
                target.write_text(candidate.content, encoding="utf-8")
                candidates_by_urn[urn] = Candidate(
                    target,
                    candidate.collection,
                    candidate.source_format,
                    candidate.metadata,
                    "",
                    candidate.source_articles,
                    candidate.source,
                )

        report = ConversionReport()
        collections_downloaded = 0
        for number, collection in enumerate(collections, 1):
            collection_started = time.perf_counter()
            name = collection_download_params(collection)["nome"]
            logger.info(
                "Collection %d/%d start name=%r formats=%s",
                number, len(collections), name,
                ",".join(collection.get("formatiDisponibili") or []),
            )
            archive = root / f"{safe_repo_name(name)}.zip"
            params, cache_hit = _download_collection(collection, archive, cache_root)
            collections_downloaded += 1
            with ZipFile(archive) as zf:
                xml_seen = 0
                for member in safe_zip_members(zf):
                    if not member.filename.casefold().endswith(".xml"):
                        continue
                    if smoke_test and xml_seen >= SMOKE_XML_PER_COLLECTION:
                        break
                    xml_seen += 1
                    raw = zf.read(member)
                    source = f"{name}/{member.filename}"
                    candidate = discover_candidate(
                        name,
                        params["formatoRichiesta"],
                        source,
                        raw,
                        report,
                    )
                    if candidate:
                        retain([candidate])
                    elif dry_run:
                        rejected_dir = root / "rejected"
                        rejected_dir.mkdir(exist_ok=True)
                        digest = hashlib.sha256(source.encode()).hexdigest()
                        path = rejected_dir / f"{digest}.xml"
                        path.write_bytes(raw)
                        rejected.append({
                            "collection": name,
                            "source": member.filename,
                            "path": path.relative_to(root).as_posix(),
                            "error": report.errors[-1].message,
                        })
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
            else fetch_missing_sources(article_counts, root / "sources" / "supplemental")
        )
        retain(discover_candidates(supplemental, report))
        duplicates = report.duplicates
        canonical = select_canonical(list(candidates_by_urn.values()), report)
        report.duplicates = duplicates
        snapshot = root / "snapshot"
        render_candidates(canonical, snapshot, report)
        requirements = Path(__file__).parents[2] / "coverage-requirements.json"
        known_gaps = [] if smoke_test else validate_required_coverage(report, requirements)
        manifest = write_indexes(
            snapshot,
            canonical,
            report,
            len(collections),
            collections_downloaded + len(supplemental),
            known_gaps,
            memberships,
        )
        write_delta(previous, manifest, snapshot)
        validate_report(report, previous, Path(__file__).parents[2] / "quality-exceptions.json")
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
            release = _stage_release(repo, tag, artifacts)
            try:
                push_snapshot(clone, branch, tag)
                release.update_release(name=tag, message="Validated Italia Corpus snapshot", draft=False)
            except Exception:
                rollback_snapshot(clone, branch, tag, previous_sha)
                release.delete_release()
                raise
        logger.info("Published %s acts from %s XML files", report.converted, report.xml_received)
        return root
