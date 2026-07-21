"""Run Invest Vault's local loopback service."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import uvicorn

from .api import create_app
from .runtime import VaultLock, watch_parent


def default_vault_directory() -> Path:
    """Return a per-user data directory without requiring a platform helper package."""
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "Invest Vault"


def main() -> None:
    directory = Path(os.environ.get("INVEST_VAULT_HOME", default_vault_directory())).resolve()
    port = int(os.environ.get("INVEST_VAULT_PORT", "8765"))
    lock = VaultLock(directory)
    lock.acquire()
    server = uvicorn.Server(uvicorn.Config(create_app(directory), host="127.0.0.1", port=port))
    parent_pid = os.environ.get("INVEST_VAULT_PARENT_PID")
    if parent_pid:
        threading.Thread(
            target=watch_parent,
            args=(int(parent_pid),),
            kwargs={
                "before_exit": lock.release,
                "exit_process": lambda _code: setattr(server, "should_exit", True),
            },
            daemon=True,
        ).start()
    try:
        server.run()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
