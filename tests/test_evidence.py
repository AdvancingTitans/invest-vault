import json
from datetime import date
from pathlib import Path

from invest_vault import EvidenceStore, Vault
from invest_vault.adapters import company_payload_to_snapshot, quote_payload_to_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "evidence_contract"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_successful_and_partial_refresh_preserve_immutable_raw_evidence(tmp_path: Path) -> None:
    quote = _fixture("quote_available.json")
    partial = _fixture("company_partial.json")
    with Vault(tmp_path / "vault.sqlite3") as vault:
        store = EvidenceStore(vault)
        quote_snapshot = quote_payload_to_snapshot(
            quote, requested_as_of="2026-07-12", observed_at="2026-07-12T15:00:00+08:00"
        )
        first = store.run_refresh(
            security_id=quote_snapshot.subject.canonical_id,
            refresh_kind="quote",
            requested_as_of=date(2026, 7, 12),
            provider="fixture_quote",
            loader=lambda: (quote_snapshot, [("fixture_quote", quote, None)]),
        )
        company_snapshot = company_payload_to_snapshot(
            partial, requested_as_of="2026-07-12", observed_at="2026-07-12T15:01:00+08:00"
        )
        second = store.run_refresh(
            security_id=company_snapshot.subject.canonical_id,
            refresh_kind="company",
            requested_as_of=date(2026, 7, 12),
            provider="fixture_company",
            loader=lambda: (company_snapshot, [("fixture_company", partial, None)]),
        )
        assert first.status == "succeeded"
        assert second.status == "partial"
        assert (tmp_path / "raw" / f"{quote_snapshot.raw_manifest[0].sha256}.json").exists()
        assert (tmp_path / "snapshots" / f"{first.snapshot_id}.json").exists()
        quality = store.data_quality(quote_snapshot.subject.canonical_id)
        assert quality and quality["requested_as_of"] == "2026-07-12"
        assert quality["effective_as_of"] == "2026-07-11"
        assert quality["source_count"] == 1


def test_failed_refresh_retains_readable_error_and_does_not_overwrite_snapshot(tmp_path: Path) -> None:
    quote = _fixture("quote_available.json")
    with Vault(tmp_path / "vault.sqlite3") as vault:
        store = EvidenceStore(vault)
        snapshot = quote_payload_to_snapshot(
            quote, requested_as_of="2026-07-12", observed_at="2026-07-12T15:00:00+08:00"
        )
        good = store.run_refresh(
            security_id=snapshot.subject.canonical_id,
            refresh_kind="quote",
            requested_as_of=date(2026, 7, 12),
            provider="fixture_quote",
            loader=lambda: (snapshot, [("fixture_quote", quote, None)]),
        )
        failed = store.run_refresh(
            security_id=snapshot.subject.canonical_id,
            refresh_kind="quote",
            requested_as_of=date(2026, 7, 12),
            provider="fixture_quote",
            loader=lambda: (_ for _ in ()).throw(RuntimeError("provider timeout for requested date")),
        )
        assert good.snapshot_id
        assert failed.status == "failed"
        assert failed.error_detail == "provider timeout for requested date"
        assert store.data_quality(snapshot.subject.canonical_id)["snapshot_id"] == good.snapshot_id
        assert store.provider_health()[0]["status"] == "unavailable"


def test_scoped_adapter_helpers_only_use_explicit_payloads(tmp_path: Path) -> None:
    quote = _fixture("quote_available.json")
    with Vault(tmp_path / "vault.sqlite3") as vault:
        job = EvidenceStore(vault).refresh_quote_history(
            quote,
            requested_as_of=date(2026, 7, 12),
            observed_at="2026-07-12T15:00:00+08:00",
        )
        assert job.status == "succeeded"
        assert job.refresh_kind == "quote"


def test_fund_quote_uses_the_same_cn_canonical_id_as_the_holding() -> None:
    snapshot = quote_payload_to_snapshot(
        {
            "symbol": "512480",
            "market": "fund",
            "asset_type": "fund",
            "name": "半导体ETF国联安",
            "price": 1.2925,
            "currency": "CNY",
            "trade_date": "2026-07-13",
            "source": "eastmoney_fund_nav",
            "source_ref": "https://fundf10.eastmoney.com/F10DataApi.aspx",
        },
        requested_as_of="2026-07-13",
        observed_at="2026-07-13T17:31:00+08:00",
    )

    assert snapshot.subject.canonical_id == "CN:SSE:512480:FUND"
