"""Local process safety, diagnostics and a reproducible sample vault."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ledger import LedgerEntry, Vault
from .research import ResearchStore


def terminate_current_process(exit_code: int) -> None:
    # PyInstaller's one-file bootloader can remain alive with a zombie child when
    # the frozen child sends SIGTERM to itself. A direct exit lets the bootloader
    # reap the child and terminate normally.
    os._exit(exit_code)


def do_nothing() -> None:
    pass


def process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if os.name != "nt":
        ps = "/bin/ps" if Path("/bin/ps").exists() else "ps"
        try:
            result = subprocess.run(
                [ps, "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                check=False,
                text=True,
                timeout=1,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        state = result.stdout.strip()
        if result.returncode != 0 or not state or state.startswith("Z"):
            return False
    return True


def watch_parent(
    parent_pid: int,
    *,
    is_alive: Callable[[int], bool] = process_is_alive,
    sleep: Callable[[float], None] = time.sleep,
    before_exit: Callable[[], object] = do_nothing,
    exit_process: Callable[[int], object] = terminate_current_process,
    interval: float = 1.0,
) -> None:
    """Terminate a managed sidecar after its desktop parent disappears."""
    while is_alive(parent_pid):
        sleep(interval)
    before_exit()
    exit_process(0)


@dataclass
class VaultLock:
    directory: Path
    _path: Path | None = None

    def acquire(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path = self.directory / ".invest-vault.lock"
        self._clear_stale_lock()
        try:
            descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RuntimeError("投资札记的数据服务已在运行") from error
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(str(os.getpid()))

    def _clear_stale_lock(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            pid = int(self._path.read_text(encoding="utf-8"))
            if not process_is_alive(pid):
                self._path.unlink(missing_ok=True)
        except ValueError:
            self._path.unlink(missing_ok=True)

    def release(self) -> None:
        if self._path:
            self._path.unlink(missing_ok=True)
            self._path = None


def diagnostics(vault_directory: Path) -> dict[str, object]:
    database = vault_directory / "vault.sqlite3"
    return {
        "product_name": "投资札记",
        "api_protocol": "holding-notebook-v1",
        "runtime_token": os.environ.get("INVEST_VAULT_RUNTIME_TOKEN", ""),
        "platform": platform.platform(),
        "vault_directory": str(vault_directory),
        "database_exists": database.exists(),
        "raw_count": len(list((vault_directory / "raw").glob("*.json"))) if (vault_directory / "raw").exists() else 0,
        "attachment_count": len(list((vault_directory / "attachments").glob("*"))) if (vault_directory / "attachments").exists() else 0,
    }


def create_sample_vault(vault_directory: Path) -> Path:
    """Create a clearly labelled local sample without contacting any provider."""
    vault_directory.mkdir(parents=True, exist_ok=True)
    with Vault(vault_directory / "vault.sqlite3") as vault:
        vault.append(
            LedgerEntry(
                record_id="sample-buy-001", idempotency_key="sample:buy-001", kind="trade", account_id="sample",
                security_id="CN:SSE:600519:STOCK", occurred_at="2026-07-11T09:30:00+08:00", quantity="14",
                cash_amount="-21000", currency="CNY", action="buy",
            )
        )
        ResearchStore(vault).revise_thesis(
            security_id="CN:SSE:600519:STOCK", body="Sample thesis: verify demand and guidance at the next filing."
        )
    (vault_directory / "sample-vault.json").write_text(
        json.dumps({"label": "Sample vault", "network_access": False}, indent=2), encoding="utf-8"
    )
    return vault_directory
