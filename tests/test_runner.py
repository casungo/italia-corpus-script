from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from italia_corpus import runner


def test_sigterm_exits_instead_of_being_ignored_as_pid1() -> None:
    runner._install_sigterm_handler()
    with pytest.raises(SystemExit) as raised:
        os.kill(os.getpid(), signal.SIGTERM)
    assert raised.value.code == 143
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def _configure_git_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runner, "PUBLISH_TARGET", "git")
    monkeypatch.setenv("GIT_TARGET_URL", "https://git.example.test/owner/repo.git")
    monkeypatch.setenv("LAST_FULL_RUN_FILE", str(tmp_path / "state" / "last-full-run"))
    monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "state" / "heartbeat"))
    monkeypatch.delenv("RUN_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("FULL_RUN_INTERVAL_SECONDS", raising=False)


def _interrupt_on_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(runner.time, "sleep", fake_sleep)
    return sleeps


def test_seconds_rejects_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRY_DELAY_SECONDS", "domani")
    with pytest.raises(SystemExit, match="RETRY_DELAY_SECONDS"):
        runner._seconds("RETRY_DELAY_SECONDS", "3600")


def test_seconds_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RETRY_DELAY_SECONDS", raising=False)
    assert runner._seconds("RETRY_DELAY_SECONDS", "3600") == 3600


def test_read_last_full_run_tolerates_missing_or_garbage_state(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    assert runner._read_last_full_run(missing) == 0
    garbage = tmp_path / "garbage"
    garbage.write_text("not-a-timestamp\n", encoding="ascii")
    assert runner._read_last_full_run(garbage) == 0
    negative = tmp_path / "negative"
    negative.write_text("-5\n", encoding="ascii")
    assert runner._read_last_full_run(negative) == 0


def test_validate_requires_token_for_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "PUBLISH_TARGET", "github")
    monkeypatch.setattr(runner, "primary_token", lambda: "")
    with pytest.raises(SystemExit, match="GITHUB_TOKEN"):
        runner.validate_publish_environment()


def test_validate_requires_github_username(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "PUBLISH_TARGET", "github")
    monkeypatch.setattr(runner, "primary_token", lambda: "token")
    monkeypatch.delenv("GITHUB_USERNAME", raising=False)
    monkeypatch.setenv("GITHUB_TARGET_REPO", "italia-corpus")
    with pytest.raises(SystemExit, match="GITHUB_USERNAME"):
        runner.validate_publish_environment()


def test_validate_requires_url_for_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "PUBLISH_TARGET", "git")
    monkeypatch.delenv("GIT_TARGET_URL", raising=False)
    with pytest.raises(SystemExit, match="GIT_TARGET_URL"):
        runner.validate_publish_environment()


def test_validate_rejects_unknown_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "PUBLISH_TARGET", "s3")
    with pytest.raises(SystemExit, match="PUBLISH_TARGET"):
        runner.validate_publish_environment()


def test_run_once_creates_directories_and_runs_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_git_target(monkeypatch, tmp_path)
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        runner, "run_full_snapshot", lambda root, cache: calls.append((root, cache))
    )
    root = tmp_path / "nested" / "work"
    cache = tmp_path / "nested" / "cache"
    runner.run_once(str(root), cache)
    assert root.is_dir() and cache.is_dir()
    assert calls == [(str(root), cache)]


def test_run_loop_records_state_and_sleeps_after_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_git_target(monkeypatch, tmp_path)
    sleeps = _interrupt_on_sleep(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(runner, "run_full_snapshot", lambda root, cache: calls.append(root))
    root = tmp_path / "work"
    with pytest.raises(KeyboardInterrupt):
        runner.run_loop(str(root), tmp_path / "cache")
    assert calls == [str(root)]
    assert sleeps == [86400]
    state = tmp_path / "state" / "last-full-run"
    assert int(state.read_text(encoding="ascii")) >= int(time.time()) - 5
    assert (tmp_path / "state" / "heartbeat").is_file()


def test_run_loop_skips_snapshot_when_upstream_is_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_git_target(monkeypatch, tmp_path)
    state = tmp_path / "state" / "last-full-run"
    state.parent.mkdir(parents=True)
    state.write_text(f"{int(time.time()) - 60}\n", encoding="ascii")
    sleeps = _interrupt_on_sleep(monkeypatch)
    monkeypatch.setattr(runner, "upstream_collections_are_cached", lambda cache: True)
    calls: list[str] = []
    monkeypatch.setattr(runner, "run_full_snapshot", lambda root, cache: calls.append(root))
    with pytest.raises(KeyboardInterrupt):
        runner.run_loop(str(tmp_path / "work"), tmp_path / "cache")
    assert calls == []
    assert sleeps == [86400]


def test_run_loop_runs_snapshot_when_upstream_check_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_git_target(monkeypatch, tmp_path)
    state = tmp_path / "state" / "last-full-run"
    state.parent.mkdir(parents=True)
    state.write_text(f"{int(time.time()) - 60}\n", encoding="ascii")
    _interrupt_on_sleep(monkeypatch)

    def failing_check(cache: Path) -> bool:
        raise RuntimeError("network down")

    monkeypatch.setattr(runner, "upstream_collections_are_cached", failing_check)
    calls: list[str] = []
    monkeypatch.setattr(runner, "run_full_snapshot", lambda root, cache: calls.append(root))
    with pytest.raises(KeyboardInterrupt):
        runner.run_loop(str(tmp_path / "work"), tmp_path / "cache")
    assert calls == [str(tmp_path / "work")]


def test_run_loop_runs_snapshot_when_interval_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_git_target(monkeypatch, tmp_path)
    monkeypatch.setenv("FULL_RUN_INTERVAL_SECONDS", "30")
    state = tmp_path / "state" / "last-full-run"
    state.parent.mkdir(parents=True)
    state.write_text(f"{int(time.time()) - 60}\n", encoding="ascii")
    _interrupt_on_sleep(monkeypatch)
    monkeypatch.setattr(runner, "upstream_collections_are_cached", lambda cache: True)
    calls: list[str] = []
    monkeypatch.setattr(runner, "run_full_snapshot", lambda root, cache: calls.append(root))
    with pytest.raises(KeyboardInterrupt):
        runner.run_loop(str(tmp_path / "work"), tmp_path / "cache")
    assert calls == [str(tmp_path / "work")]


def test_run_loop_retries_after_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_git_target(monkeypatch, tmp_path)
    sleeps = _interrupt_on_sleep(monkeypatch)

    def failing_snapshot(root: str, cache: Path) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "run_full_snapshot", failing_snapshot)
    heartbeat = tmp_path / "state" / "heartbeat"
    with pytest.raises(KeyboardInterrupt):
        runner.run_loop(str(tmp_path / "work"), tmp_path / "cache")
    assert sleeps == [3600]
    assert heartbeat.is_file()


def test_run_loop_rejects_non_numeric_legacy_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_git_target(monkeypatch, tmp_path)
    monkeypatch.setenv("RUN_INTERVAL_SECONDS", "mensile")
    with pytest.raises(SystemExit, match="FULL_RUN_INTERVAL_SECONDS"):
        runner.run_loop(str(tmp_path / "work"), tmp_path / "cache")
