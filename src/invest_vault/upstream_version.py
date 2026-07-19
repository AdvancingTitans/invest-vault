"""Read-only release check for the bundled research engine."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable

from stock_analysis import __version__ as BUNDLED_VERSION

LATEST_RELEASE_API = "https://api.github.com/repos/AdvancingTitans/stock-analysis/releases/latest"


def _version_tuple(value: str) -> tuple[int, ...]:
    normalized = value.strip().removeprefix("v")
    try:
        return tuple(int(part) for part in normalized.split("."))
    except ValueError as error:
        raise ValueError("上游版本号格式无效") from error


@dataclass(frozen=True)
class UpstreamVersionStatus:
    bundled_version: str
    latest_version: str
    update_available: bool
    release_url: str
    checked_at: str
    update_policy: str = "app_release"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def check_upstream_version(
    opener: Callable[..., object] = urllib.request.urlopen,
) -> UpstreamVersionStatus:
    """Check the signed upstream release metadata without downloading executable code."""

    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Invest-Vault/research-engine-version-check",
        },
    )
    with opener(request, timeout=5) as response:  # type: ignore[attr-defined]  # nosec B310
        payload = json.loads(response.read().decode("utf-8"))
    latest = str(payload.get("tag_name") or "").removeprefix("v")
    release_url = str(payload.get("html_url") or "")
    if not latest or not release_url:
        raise ValueError("上游版本信息不完整")
    return UpstreamVersionStatus(
        bundled_version=BUNDLED_VERSION,
        latest_version=latest,
        update_available=_version_tuple(latest) > _version_tuple(BUNDLED_VERSION),
        release_url=release_url,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
