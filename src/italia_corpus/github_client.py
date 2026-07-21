import os
import time
from typing import cast

from github import Auth, Github, GithubException, RateLimitExceededException
from github.AuthenticatedUser import AuthenticatedUser
from github.Repository import Repository
from urllib3.util import Retry

from .config import GITHUB_USERNAME, logger


def primary_token() -> str:
    """Return the first available token (GITHUB_TOKEN_1/2/... or GITHUB_TOKEN)."""
    i = 1
    while True:
        t = os.getenv(f"GITHUB_TOKEN_{i}", "").strip()
        if t:
            return t
        if i > 20:
            break
        i += 1
    return os.getenv("GITHUB_TOKEN", "").strip()


def github_client() -> Github:
    """Create a PyGithub client with retries disabled."""
    no_retry = Retry(total=0)
    token = primary_token()
    if not token:
        logger.warning("No GITHUB_TOKEN set; private repos will fail")
        return Github(retry=no_retry)
    return Github(auth=Auth.Token(token), retry=no_retry)


def _github_error_detail(exc: GithubException) -> str:
    data = getattr(exc, "data", None)
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])
    return str(exc)


def _log_github_rate_limit(exc: RateLimitExceededException) -> None:
    headers = exc.headers or {}
    remaining = headers.get("X-RateLimit-Remaining")
    reset_s = headers.get("X-RateLimit-Reset")
    reset_hint = ""
    if reset_s and str(reset_s).isdigit():
        try:
            reset_ts = int(reset_s)
            wait = max(0, reset_ts - int(time.time()))
            reset_hint = f" X-RateLimit-Reset in ~{wait}s ({wait // 60}m)."
        except (TypeError, ValueError, OSError):
            pass
    logger.error(
        "GitHub API rate limit exceeded (remaining=%s).%s "
        "Slow down or wait for the hourly window; very bursty automation can trigger abuse limits — "
        "see https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api",
        remaining,
        reset_hint,
    )


def verify_github_session(gh: Github) -> None:
    """Log the authenticated user; warn if GITHUB_USERNAME doesn't match."""
    if not primary_token():
        return
    try:
        login = gh.get_user().login
    except GithubException as e:
        if isinstance(e, RateLimitExceededException):
            _log_github_rate_limit(e)
        else:
            logger.error(
                "GitHub token rejected or insufficient (status=%s): %s",
                getattr(e, "status", None),
                _github_error_detail(e),
            )
        raise
    logger.info("GitHub authenticated as %s", login)
    want = (GITHUB_USERNAME or "").strip()
    if want and login.casefold() != want.casefold():
        logger.warning(
            "GITHUB_USERNAME is %r but the token is for %r — "
            "get_repo / clone use the wrong namespace and often return 403.",
            GITHUB_USERNAME,
            login,
        )


def get_or_create_repo(gh: Github, repo_name: str) -> Repository:
    """Return the repo if it exists, otherwise create it (private, auto-init)."""
    full_name = f"{GITHUB_USERNAME}/{repo_name}"
    try:
        repo = gh.get_repo(full_name)
        logger.info("Using existing repo %s", full_name)
        return repo
    except GithubException as e:
        status = getattr(e, "status", None)
        if status != 404:
            logger.error(
                "GitHub GET %s failed (status=%s): %s",
                full_name,
                status,
                _github_error_detail(e),
            )
            if isinstance(e, RateLimitExceededException):
                _log_github_rate_limit(e)
            elif status in (401, 403):
                logger.error(
                    "Fix: use a classic PAT with scope 'repo', or a fine-grained token with "
                    "access to this user and repository creation; ensure GITHUB_USERNAME matches the token owner."
                )
            raise
    logger.info("Creating repo %s", full_name)
    user = cast(AuthenticatedUser, gh.get_user())
    repo = user.create_repo(
        name=repo_name,
        private=True,
        auto_init=True,
        description=f"Normattiva Open Data – {repo_name}",
    )
    logger.info("Created repo %s (default branch: %s)", full_name, repo.default_branch)
    return repo
