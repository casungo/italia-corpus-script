import logging
import os

from dotenv import load_dotenv

load_dotenv()

GITHUB_USERNAME = os.getenv("GITHUB_USERNAME")
EXTRACTION_BUFFER_PATH = os.getenv("EXTRACTION_BUFFER_PATH")
TARGET_REPO_NAME = (os.getenv("GITHUB_TARGET_REPO") or "").strip()
PUBLISH_TARGET = os.getenv("PUBLISH_TARGET", "github").strip().casefold()
GIT_TARGET_URL = os.getenv("GIT_TARGET_URL", "").strip()
GIT_TARGET_BRANCH = os.getenv("GIT_TARGET_BRANCH", "main").strip() or "main"
GIT_TARGET_USERNAME = os.getenv("GIT_TARGET_USERNAME", "x-access-token").strip()
GIT_TARGET_TOKEN = os.getenv("GIT_TARGET_TOKEN", "").strip()
BUFFER_PATH = os.getenv("BUFFER_PATH")
GIT_AUTHOR_NAME = os.getenv("GIT_AUTHOR_NAME", GITHUB_USERNAME)
GIT_AUTHOR_EMAIL = os.getenv(
    "GIT_AUTHOR_EMAIL", f"{GITHUB_USERNAME}@users.noreply.github.com"
)

BASE_URL = "https://api.normattiva.it/t/normattiva.api/bff-opendata/v1/api/v1"
ENDPOINT_URL = f"{BASE_URL}/collections/download/collection-preconfezionata"
COLLECTIONS_URL = f"{BASE_URL}/collections/collection-predefinite"

# Normattiva può impiegare molto per pacchetti grandi; breve connect, read lungo.
DOWNLOAD_TIMEOUT = (30.0, 300.0)
DOWNLOAD_MAX_ATTEMPTS = 3
DOWNLOAD_RETRY_SLEEP_SEC = 5.0

_level_name = os.getenv("LOGLEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _level_name, logging.INFO),
    format="%(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("italia_corpus")


def target_repo_full_name(repo_name: str, username: str | None = GITHUB_USERNAME) -> str:
    """Accept either ``owner/repository`` or the legacy repository-only value."""
    target = repo_name.strip().strip("/")
    if "/" in target:
        return target
    if not username:
        raise ValueError("GITHUB_USERNAME is required when GITHUB_TARGET_REPO has no owner")
    return f"{username}/{target}"
