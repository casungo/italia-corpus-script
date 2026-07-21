"""Fail-closed, all-collections snapshot pipeline."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from zipfile import ZipFile

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
    build_artifacts, safe_zip_members, validate_report, validate_required_coverage,
    write_delta, write_indexes,
)
from .supplemental import fetch_missing_sources

HEADERS = {
    "User-Agent": "italia-corpus/2 (+https://github.com/ahmeabd/italia-corpus-script)",
    "Accept": "application/zip,application/octet-stream",
}
SMOKE_XML_PER_COLLECTION = 1_000


def _download(params: dict, destination: Path, *, allow_empty: bool = False) -> bool:
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
                if allow_empty and attempt < DOWNLOAD_MAX_ATTEMPTS:
                    continue
                if allow_empty:
                    return False
                raise ValueError("empty download")
            if not destination.read_bytes()[:4].startswith(b"PK"):
                raise ValueError("download is not a ZIP archive")
            return True
        except (requests.RequestException, OSError, ValueError) as exc:
            last_error = exc
            destination.unlink(missing_ok=True)
            if attempt < DOWNLOAD_MAX_ATTEMPTS:
                time.sleep(DOWNLOAD_RETRY_SLEEP_SEC * attempt)
    raise RuntimeError(f"download failed after {DOWNLOAD_MAX_ATTEMPTS} attempts: {last_error}")


def _load_previous_manifest(clone: Path) -> dict | None:
    path = clone / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


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
) -> Path:
    """Build a validated snapshot and publish it unless ``dry_run`` is set."""
    if smoke_test and not dry_run:
        raise ValueError("smoke_test requires dry_run")
    work_root = Path(root_path)
    work_root.mkdir(parents=True, exist_ok=True)
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
        for collection in collections:
            params = collection_download_params(collection)
            name = params["nome"]
            archive = root / f"{safe_repo_name(name)}.zip"
            if not _download(params, archive, allow_empty=smoke_test):
                logger.warning("Skipping empty collection %r in smoke test", name)
                continue
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
