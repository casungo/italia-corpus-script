import shutil
import subprocess
from datetime import date
from pathlib import Path

from .config import logger
from .filename import collection_subdir_name
from .github_client import primary_token


def redact(text: str) -> str:
    """Remove tokens from log strings."""
    token = primary_token()
    if token and token in text:
        text = text.replace(token, "***")
    return text


def git(args: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command with output logging (tokens redacted)."""
    cmd = ["git"] + args
    logger.debug("git %s (cwd=%s)", redact(" ".join(args)), cwd)
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.stdout.strip():
        logger.debug("stdout: %s", redact(result.stdout.strip()))
    if result.stderr.strip():
        logger.debug("stderr: %s", redact(result.stderr.strip()))
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(redact(a) for a in args)} failed (rc={result.returncode}):\n"
            f"{redact(result.stderr)}"
        )
    return result


def sync_collection_to_clone(
    md_dir: str,
    collection_name: str,
    clone_dir: str,
    branch: str,
    target_repo_name: str,
) -> None:
    """Commit and push one collection subfolder into an already-cloned working tree."""
    sub = collection_subdir_name(collection_name)

    md_files = list(Path(md_dir).rglob("*.md"))
    if not md_files:
        logger.warning("No .md files found in %s, skipping push", md_dir)
        return

    dest_root = Path(clone_dir) / sub
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)

    for md_file in md_files:
        dest = dest_root / md_file.relative_to(md_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(md_file, dest)

    logger.info(
        "Staged %d .md file(s) under %r in repo %s",
        len(md_files),
        sub,
        target_repo_name,
    )

    git(["add", "-A"], cwd=clone_dir)

    status = git(["status", "--porcelain"], cwd=clone_dir, check=False)
    if not status.stdout.strip():
        logger.info(
            "No changes to commit for collection %r, skipping push", collection_name
        )
        return

    today = date.today().strftime("%Y-%m-%d")
    git(["commit", "-m", f"{today} - {collection_name}"], cwd=clone_dir)
    logger.info("Pushing to %s (branch: %s)", target_repo_name, branch)
    git(["push", "origin", f"HEAD:{branch}"], cwd=clone_dir)
    logger.info("Pushed collection %r to %s", collection_name, target_repo_name)
