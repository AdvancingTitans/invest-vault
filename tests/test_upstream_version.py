from __future__ import annotations

import io
import json

import pytest

from invest_vault.upstream_version import check_upstream_version


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _opener(payload: dict[str, object]):
    def open_request(request: object, timeout: int) -> _Response:
        assert timeout == 5
        return _Response(json.dumps(payload).encode())

    return open_request


def test_upstream_version_check_is_advisory_and_does_not_replace_runtime() -> None:
    status = check_upstream_version(_opener({
        "tag_name": "v4.15.0",
        "html_url": "https://github.com/AdvancingTitans/stock-analysis/releases/tag/v4.15.0",
    }))

    assert status.bundled_version == "4.15.0"
    assert status.latest_version == "4.15.0"
    assert status.update_available is False
    assert status.update_policy == "app_release"


def test_upstream_version_check_rejects_incomplete_release_metadata() -> None:
    with pytest.raises(ValueError, match="上游版本信息不完整"):
        check_upstream_version(_opener({"tag_name": "v4.15.0"}))
