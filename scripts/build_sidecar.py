"""Place a PyInstaller service binary at Tauri's target-specific sidecar path."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).parents[1]
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm is required to build the bundled Web UI before freezing the sidecar")
    subprocess.run([npm, "run", "build"], cwd=root / "web", check=True)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "sidecar.spec"],
        cwd=root,
        check=True,
    )
    target = subprocess.check_output(["rustc", "--print", "host-tuple"], text=True).strip()
    extension = ".exe" if sys.platform == "win32" else ""
    source = root / "dist" / f"invest-vault-service{extension}"
    destination = root / "src-tauri" / "binaries" / f"invest-vault-service-{target}{extension}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(0o755)
    print(destination)


if __name__ == "__main__":
    main()
