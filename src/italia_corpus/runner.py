"""Container run modes: one validated snapshot, or the check-then-run loop."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from .config import GIT_TARGET_TOKEN, PUBLISH_TARGET, logger
from .github_client import github_client, primary_token, verify_github_session
from .pipeline import extract_and_push, upstream_collections_are_cached

DEFAULT_CHECK_INTERVAL_SECONDS = 86400
DEFAULT_FULL_RUN_INTERVAL_SECONDS = 2592000
DEFAULT_RETRY_DELAY_SECONDS = 3600
DEFAULT_LAST_FULL_RUN_FILE = "/data/last-full-run"
DEFAULT_HEARTBEAT_FILE = "/data/.heartbeat"


def _seconds_until(anchor: str, now: float | None = None) -> int:
    """Seconds until the next occurrence of the wall-clock time ``anchor`` (HH:MM).

    Local time of the container. Raises SystemExit on a malformed anchor so a
    typo cannot silently disable the schedule.
    """
    parts = anchor.strip().split(":")
    if len(parts) != 2 or not all(p.isascii() and p.isdigit() and len(p) == 2 for p in parts):
        raise SystemExit(f"RUN_AT must be HH:MM (24h), got {anchor!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise SystemExit(f"RUN_AT must be HH:MM (24h), got {anchor!r}")
    current = time.localtime(now)
    target = time.struct_time(
        (current.tm_year, current.tm_mon, current.tm_mday, hour, minute, 0,
         current.tm_wday, current.tm_yday, current.tm_isdst)
    )
    delay = time.mktime(target) - (now if now is not None else time.time())
    if delay <= 0:
        target = time.struct_time(
            (current.tm_year, current.tm_mon, current.tm_mday + 1, hour, minute, 0,
             current.tm_wday, current.tm_yday, current.tm_isdst)
        )
        delay = time.mktime(target) - (now if now is not None else time.time())
    return max(int(delay), 0)


def _read_last_full_run(path: Path) -> int:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0
    return max(value, 0)


def _seconds(name: str, default: str) -> int:
    raw = os.getenv(name, "").strip() or default
    if not (raw.isascii() and raw.isdigit()):
        raise SystemExit(f"{name} must be a whole number of seconds, got {raw!r}")
    return int(raw)


def _touch_heartbeat(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError as exc:
        logger.warning("Cannot update heartbeat file %s: %s", path, exc)


def _install_sigterm_handler() -> None:
    """Exit on SIGTERM even as PID 1, where default-disposition signals are ignored."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))


def validate_publish_environment() -> None:
    """Fail before the first snapshot when the publish configuration is incomplete."""
    if PUBLISH_TARGET == "github":
        if not primary_token():
            raise SystemExit("Set GITHUB_TOKEN, GITHUB_TOKEN_FILE, or PUBLISH_TOKEN_FILE")
        for name in ("GITHUB_USERNAME", "GITHUB_TARGET_REPO"):
            if not os.getenv(name, "").strip():
                raise SystemExit(f"Set {name}")
    elif PUBLISH_TARGET == "git":
        if not os.getenv("GIT_TARGET_URL", "").strip():
            raise SystemExit("Set GIT_TARGET_URL when PUBLISH_TARGET=git")
        if not GIT_TARGET_TOKEN:
            logger.warning("GIT_TARGET_TOKEN is empty; only ssh:// remotes will authenticate")
    else:
        raise SystemExit("PUBLISH_TARGET must be github or git")


def run_full_snapshot(root_path: str, download_cache: Path) -> None:
    """Run one complete snapshot for the configured publish target."""
    gh = None
    if PUBLISH_TARGET == "github":
        gh = github_client()
        verify_github_session(gh)
    extract_and_push(root_path, gh, download_cache=download_cache)


def _upstream_fresh(download_cache: Path) -> bool:
    """Treat a failed upstream check as needing a full snapshot, like the shell did."""
    try:
        return upstream_collections_are_cached(download_cache)
    except Exception as exc:
        logger.warning("Upstream check failed (%s); running a full snapshot.", exc)
        return False


def run_once(root_path: str, download_cache: Path) -> None:
    """Validate configuration, run one full snapshot, and return."""
    _install_sigterm_handler()
    validate_publish_environment()
    Path(root_path).mkdir(parents=True, exist_ok=True)
    download_cache.mkdir(parents=True, exist_ok=True)
    run_full_snapshot(root_path, download_cache)


def run_loop(root_path: str, download_cache: Path) -> None:
    """Check Normattiva on an interval and run a full snapshot when needed."""
    _install_sigterm_handler()
    validate_publish_environment()
    check_interval = _seconds("CHECK_INTERVAL_SECONDS", str(DEFAULT_CHECK_INTERVAL_SECONDS))
    full_run_interval = _seconds(
        "FULL_RUN_INTERVAL_SECONDS",
        os.getenv("RUN_INTERVAL_SECONDS", "").strip() or str(DEFAULT_FULL_RUN_INTERVAL_SECONDS),
    )
    retry_delay = _seconds("RETRY_DELAY_SECONDS", str(DEFAULT_RETRY_DELAY_SECONDS))
    run_at = os.getenv("RUN_AT", "").strip()
    if run_at:
        # Fail fast on a malformed anchor before the first cycle starts.
        _seconds_until(run_at)
    last_full_run = Path(os.getenv("LAST_FULL_RUN_FILE", DEFAULT_LAST_FULL_RUN_FILE))
    heartbeat = Path(os.getenv("HEARTBEAT_FILE", DEFAULT_HEARTBEAT_FILE))
    Path(root_path).mkdir(parents=True, exist_ok=True)
    download_cache.mkdir(parents=True, exist_ok=True)

    def wake_delay() -> int:
        """RUN_AT pins wake-ups to a wall-clock time; otherwise sleep the interval."""
        return _seconds_until(run_at) if run_at else check_interval

    while True:
        _touch_heartbeat(heartbeat)
        last = _read_last_full_run(last_full_run)
        fresh_window = last > 0 and int(time.time()) - last < full_run_interval
        if fresh_window and _upstream_fresh(download_cache):
            time.sleep(wake_delay())
            continue
        try:
            run_full_snapshot(root_path, download_cache)
        except Exception as exc:
            logger.error("Snapshot failed: %s", exc)
            logger.warning("Retrying in %ds with the persisted cache.", retry_delay)
            _touch_heartbeat(heartbeat)
            time.sleep(retry_delay)
            continue
        last_full_run.parent.mkdir(parents=True, exist_ok=True)
        last_full_run.write_text(f"{int(time.time())}\n", encoding="ascii")
        time.sleep(wake_delay())
