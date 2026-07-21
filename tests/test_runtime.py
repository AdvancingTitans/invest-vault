from pathlib import Path

import pytest

from invest_vault.__main__ import default_vault_directory
from invest_vault.runtime import VaultLock, create_sample_vault, diagnostics, process_is_alive, watch_parent


def test_lock_and_sample_vault_are_local_and_recoverable(tmp_path: Path) -> None:
    lock = VaultLock(tmp_path / "vault")
    lock.acquire()
    with pytest.raises(RuntimeError, match="数据服务已在运行"):
        VaultLock(tmp_path / "vault").acquire()
    lock.release()
    create_sample_vault(tmp_path / "sample")
    report = diagnostics(tmp_path / "sample")
    assert report["database_exists"] is True
    assert (tmp_path / "sample" / "sample-vault.json").exists()


def test_default_vault_directory_is_not_the_install_directory() -> None:
    directory = default_vault_directory()
    assert directory.name == "Invest Vault"
    assert directory.is_absolute()


def test_diagnostics_identifies_the_desktop_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INVEST_VAULT_RUNTIME_TOKEN", "desktop-run-123")

    report = diagnostics(tmp_path)

    assert report["product_name"] == "投资札记"
    assert report["api_protocol"] == "holding-notebook-v1"
    assert report["runtime_token"] == "desktop-run-123"


def test_stale_lock_is_recovered(tmp_path: Path) -> None:
    directory = tmp_path / "vault"
    directory.mkdir()
    (directory / ".invest-vault.lock").write_text("999999999", encoding="utf-8")
    lock = VaultLock(directory)
    lock.acquire()
    lock.release()


def test_current_process_is_alive() -> None:
    import os

    assert process_is_alive(os.getpid()) is True


def test_parent_watchdog_exits_when_desktop_parent_disappears() -> None:
    states = iter([True, False])
    waits: list[float] = []
    exits: list[int] = []

    watch_parent(
        12345,
        is_alive=lambda _pid: next(states),
        sleep=waits.append,
        exit_process=exits.append,
        interval=0.25,
    )

    assert waits == [0.25]
    assert exits == [0]


def test_parent_watchdog_releases_its_lock_before_exit(tmp_path: Path) -> None:
    lock = VaultLock(tmp_path / "vault")
    lock.acquire()

    watch_parent(
        12345,
        is_alive=lambda _pid: False,
        before_exit=lock.release,
        exit_process=lambda _code: None,
    )

    assert not (tmp_path / "vault" / ".invest-vault.lock").exists()
