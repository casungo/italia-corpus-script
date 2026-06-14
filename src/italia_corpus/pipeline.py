import errno
import os
import random
import shutil
import tempfile
import time
from pathlib import Path
from zipfile import ZipFile

import requests
from github import Github

from .config import (
    DOWNLOAD_MAX_ATTEMPTS,
    DOWNLOAD_RETRY_SLEEP_SEC,
    DOWNLOAD_TIMEOUT,
    ENDPOINT_URL,
    GIT_AUTHOR_EMAIL,
    GIT_AUTHOR_NAME,
    GITHUB_USERNAME,
    TARGET_REPO_NAME,
    logger,
)
from .converter import convert_akn_dir_to_md
from .filename import collection_subdir_name, safe_repo_name
from .frontmatter import build_urn_index, update_urn_index_from_paths
from .git_ops import git, sync_collection_to_clone
from .github_client import get_or_create_repo, primary_token
from .normattiva import (
    collection_download_params,
    fetch_predefined_collections,
    merge_collections_by_name,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/zip,application/octet-stream,*/*",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.normattiva.it/",
}


def extract_and_push(root_path: str, gh: Github) -> None:
    """Main pipeline: fetch collections, download ZIPs, convert XML to MD, push to GitHub."""
    os.makedirs(root_path, exist_ok=True)
    raw = fetch_predefined_collections()
    collections = merge_collections_by_name(raw)
    logger.info("Processing %d unique collection(s)", len(collections))

    if not TARGET_REPO_NAME:
        logger.error(
            "Imposta GITHUB_TARGET_REPO (nome della repo unica, es. italia-corpus)."
        )
        return

    repo = get_or_create_repo(gh, TARGET_REPO_NAME)
    token = primary_token()
    clone_url = (
        f"https://{token}@github.com/{GITHUB_USERNAME}/{TARGET_REPO_NAME}.git"
        if token
        else repo.clone_url
    )
    branch = repo.default_branch or "main"
    logger.info(
        "All collections will sync under %s/%s (one subfolder per collection, spaces in folder names).",
        GITHUB_USERNAME,
        TARGET_REPO_NAME,
    )

    with tempfile.TemporaryDirectory(prefix="italia-legal-clone-") as clone_work:
        clone_dir = os.path.join(clone_work, "repo")
        logger.info("Cloning %s (shallow, once for all collections)", TARGET_REPO_NAME)
        git(["clone", "--depth=1", clone_url, clone_dir], cwd=clone_work)
        git(["config", "user.email", GIT_AUTHOR_EMAIL], cwd=clone_dir)
        git(["config", "user.name", GIT_AUTHOR_NAME], cwd=clone_dir)

        urn_index = build_urn_index(Path(clone_dir))

        for collection in collections:
            params = collection_download_params(collection)
            nome = params["nome"]
            if not nome:
                continue

            logger.info(
                "Downloading collection %r (formatoRichiesta=%s)",
                nome,
                params["formatoRichiesta"],
            )

            try:
                zip_path: str | None = None
                download_ok = False
                for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
                    try:
                        tmp = tempfile.NamedTemporaryFile(
                            suffix=".zip", dir=root_path, delete=False
                        )
                        zip_path = tmp.name
                        with requests.get(
                            ENDPOINT_URL,
                            params=params,
                            headers=HEADERS,
                            timeout=DOWNLOAD_TIMEOUT,
                            stream=True,
                        ) as r:
                            if r.status_code != 200:
                                preview = (r.text or "")[:200]
                                logger.warning(
                                    "Download attempt %d/%d for %r returned HTTP %d: %r",
                                    attempt,
                                    DOWNLOAD_MAX_ATTEMPTS,
                                    nome,
                                    r.status_code,
                                    preview,
                                )
                                tmp.close()
                                if zip_path and os.path.exists(zip_path):
                                    os.unlink(zip_path)
                                zip_path = None
                                if attempt == DOWNLOAD_MAX_ATTEMPTS:
                                    logger.error(
                                        "Failed to download collection %r after %d attempt(s) — skipping",
                                        nome,
                                        DOWNLOAD_MAX_ATTEMPTS,
                                    )
                                else:
                                    backoff = 5 * attempt
                                    logger.warning(
                                        "Retrying download for %r in %.0fs",
                                        nome,
                                        backoff,
                                    )
                                    time.sleep(backoff)
                                continue

                            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                                tmp.write(chunk)
                        tmp.close()
                        download_ok = True
                        break
                    except requests.RequestException as e:
                        try:
                            tmp.close()
                        except Exception:
                            pass
                        if zip_path and os.path.exists(zip_path):
                            os.unlink(zip_path)
                        zip_path = None
                        if attempt == DOWNLOAD_MAX_ATTEMPTS:
                            logger.error(
                                "Failed to download collection %r after %d attempt(s): %s — skipping",
                                nome,
                                DOWNLOAD_MAX_ATTEMPTS,
                                e,
                            )
                        else:
                            logger.warning(
                                "Download attempt %d/%d failed for %r: %s — retrying in %.0fs",
                                attempt,
                                DOWNLOAD_MAX_ATTEMPTS,
                                nome,
                                e,
                                DOWNLOAD_RETRY_SLEEP_SEC,
                            )
                            time.sleep(DOWNLOAD_RETRY_SLEEP_SEC)
                if not download_ok:
                    continue

                coll_dir = os.path.join(root_path, safe_repo_name(nome))
                md_dir = coll_dir + "_md"
                try:
                    os.makedirs(coll_dir, exist_ok=True)

                    with open(zip_path, "rb") as f:
                        header = f.read(512)
                    if not header.startswith(b"PK"):
                        logger.error(
                            "Collection %r is not a valid ZIP file\nFirst 512 bytes: %r",
                            nome,
                            header,
                        )
                        continue
                    with ZipFile(zip_path) as zf:
                        zf.extractall(coll_dir)

                    os.unlink(zip_path)
                    zip_path = None

                    os.makedirs(md_dir, exist_ok=True)
                    count = convert_akn_dir_to_md(
                        coll_dir, md_dir, urn_index, nome
                    )
                    logger.info(
                        "Converted %d AKN XML file(s) to markdown for %r", count, nome
                    )

                    shutil.rmtree(coll_dir, ignore_errors=True)

                    if count > 0:
                        sync_collection_to_clone(
                            md_dir, nome, clone_dir, branch, TARGET_REPO_NAME
                        )
                        sub = collection_subdir_name(nome)
                        dest_root = Path(clone_dir) / sub
                        update_urn_index_from_paths(
                            urn_index,
                            Path(clone_dir),
                            list(dest_root.rglob("*.md")),
                        )
                    else:
                        logger.warning(
                            "No AKN XML files found in %r, skipping push", nome
                        )

                except OSError as e:
                    if e.errno == errno.ENOSPC:
                        logger.error(
                            "Disk full while processing collection %r — free space on this "
                            "volume or set ROOT_PATH to a folder on a larger disk.",
                            nome,
                        )
                    else:
                        logger.error(
                            "Error processing collection %r: %s", nome, e, exc_info=True
                        )
                except Exception as e:
                    logger.error(
                        "Error processing collection %r: %s", nome, e, exc_info=True
                    )
                finally:
                    if zip_path and os.path.exists(zip_path):
                        os.unlink(zip_path)
                    shutil.rmtree(coll_dir, ignore_errors=True)
                    shutil.rmtree(md_dir, ignore_errors=True)
            finally:
                time.sleep(random.uniform(2.0, 5.0))
