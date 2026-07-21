"""Small, non-persistent Git subprocess wrapper."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import re
from datetime import date
from pathlib import Path

from .config import logger
from .github_client import primary_token
from .snapshot import QualityGateError


def redact(text: str) -> str:
    token = primary_token()
    return text.replace(token, "***") if token else text


def git(args: list[str], cwd: str, check: bool = True, github_auth: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if github_auth and (token := primary_token()):
        credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credential}",
        })
    logger.debug("git %s (cwd=%s)", redact(" ".join(args)), cwd)
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if check and result.returncode:
        raise RuntimeError(f"git {redact(' '.join(args))} failed: {redact(result.stderr)}")
    return result


def stage_snapshot(snapshot: Path, clone_dir: Path, collection_dirs: list[str]) -> tuple[str, str] | None:
    """Replace generated paths and create one local commit and tag."""
    previous_sha = git(["rev-parse", "HEAD"], str(clone_dir)).stdout.strip()
    for name in ["atti", "collections", "manifest.json", "urn-index.json", *collection_dirs]:
        target = clone_dir / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for source in sorted(snapshot.iterdir()):
        target = clone_dir / source.name
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    git(["add", "-A"], str(clone_dir))
    status = git(["status", "--porcelain"], str(clone_dir), check=False)
    if not status.stdout.strip():
        return None
    modified = git(["diff", "--cached", "--diff-filter=M", "--name-only", "--", "*.md"], str(clone_dir))
    changed = [line for line in modified.stdout.splitlines() if line]
    if changed:
        link = re.compile(r"\]\([^)]+\)")
        link_only = True
        for relative in changed:
            old = git(["show", f"HEAD:{relative}"], str(clone_dir), check=False).stdout
            new = (clone_dir / relative).read_text(encoding="utf-8", errors="replace")
            if link.sub("](LINK)", old) != link.sub("](LINK)", new):
                link_only = False
                break
        if link_only:
            raise QualityGateError("refusing a snapshot made only of Markdown link-target changes")
    tag = f"snapshot-{date.today().isoformat()}"
    git(["commit", "-m", f"snapshot: {date.today().isoformat()}"], str(clone_dir))
    git(["tag", "-a", tag, "-m", tag], str(clone_dir))
    return tag, previous_sha


def push_snapshot(clone_dir: Path, branch: str, tag: str) -> None:
    git(["push", "--atomic", "origin", f"HEAD:{branch}", tag], str(clone_dir), github_auth=True)


def rollback_snapshot(clone_dir: Path, branch: str, tag: str, previous_sha: str) -> None:
    """Best-effort rollback if publishing the release fails after the Git push."""
    git(["push", "--force-with-lease", "origin", f"{previous_sha}:{branch}"], str(clone_dir), github_auth=True)
    git(["push", "origin", ":refs/tags/" + tag], str(clone_dir), check=False, github_auth=True)


def sync_collection_to_clone(*args, **kwargs) -> None:
    raise RuntimeError("per-collection publication was removed; publish one validated snapshot")
