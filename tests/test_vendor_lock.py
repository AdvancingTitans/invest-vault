from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPOSITORY_ROOT / "vendor-lock.json"


def _files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name != ".DS_Store"
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    )


def _tree_hash(root: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    files = _files(root)
    for path in files:
        relative = path.relative_to(root).as_posix().encode()
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest().encode()
        digest.update(relative + b"\0" + content_hash + b"\n")
    return len(files), digest.hexdigest()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in _files(root)}


def test_bundled_stock_analysis_matches_vendor_lock() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    assert lock["schema_version"] == 1
    assert len(lock["upstream"]["commit"]) == 40
    for tree in lock["trees"].values():
        root = REPOSITORY_ROOT / tree["path"]
        assert _tree_hash(root) == (tree["files"], tree["sha256"])


def test_portable_skill_only_rewrites_the_pinned_local_path() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    transformation = lock["allowed_transformations"][0]
    portable = _snapshot(REPOSITORY_ROOT / lock["trees"]["portable_app_skill"]["path"])

    assert transformation["scope"] == "portable_app_skill"
    assert transformation["source"].encode() not in b"".join(portable.values())
    assert transformation["replacement"].encode() in b"".join(portable.values())
