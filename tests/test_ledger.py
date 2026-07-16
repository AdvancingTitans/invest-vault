from datetime import datetime
from pathlib import Path

import pytest

from invest_vault import HoldingRecord, LedgerEntry, Vault, VaultSettings


def _entry(**overrides: str) -> LedgerEntry:
    values = {
        "record_id": "trade-001",
        "idempotency_key": "broker:trade-001",
        "kind": "trade",
        "account_id": "brokerage",
        "security_id": "CN:SSE:600519:STOCK",
        "occurred_at": "2026-07-11T09:30:00+08:00",
        "quantity": "10",
        "cash_amount": "-15000.00",
        "currency": "CNY",
        "action": "buy",
    }
    values.update(overrides)
    return LedgerEntry(**values)


def test_loopback_only_configuration() -> None:
    assert VaultSettings().host == "127.0.0.1"
    with pytest.raises(ValueError, match="loopback"):
        VaultSettings(host="0.0.0.0")


def test_append_only_ledger_replays_positions_and_cash(tmp_path: Path) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.append(_entry())
        vault.append(
            _entry(
                record_id="trade-002",
                idempotency_key="broker:trade-002",
                quantity="-3",
                cash_amount="4800.00",
                action="sell",
            )
        )
        vault.append(
            _entry(
                record_id="split-001",
                idempotency_key="broker:split-001",
                kind="corporate_action",
                quantity="7",
                cash_amount="0",
                action="stock_split_adjustment",
            )
        )
        vault.append(
            _entry(
                record_id="cash-001",
                idempotency_key="bank:cash-001",
                kind="cash",
                security_id="",
                quantity="0",
                cash_amount="20000.00",
                action="deposit",
            )
        )

        positions = vault.project_positions("brokerage")
        cash = vault.project_cash("brokerage")

        assert [(item.security_id, item.quantity, item.valuation_status) for item in positions] == [
            ("CN:SSE:600519:STOCK", "14", "unavailable")
        ]
        assert cash == {"CNY": "9800.00"}
        assert vault.count_entries() == 4


def test_ledger_rows_cannot_be_updated_or_deleted(tmp_path: Path) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.append(_entry())
        with pytest.raises(Exception, match="append-only"):
            vault.connection.execute("UPDATE ledger_entries SET quantity = '0' WHERE record_id = 'trade-001'")
        with pytest.raises(Exception, match="append-only"):
            vault.connection.execute("DELETE FROM ledger_entries WHERE record_id = 'trade-001'")


def test_json_round_trip_is_idempotent_and_csv_export_remains_available(tmp_path: Path) -> None:
    source_path = tmp_path / "source.sqlite3"
    with Vault(source_path) as source:
        source.append(_entry())
        source.append(
            _entry(
                record_id="cash-001",
                idempotency_key="bank:cash-001",
                kind="cash",
                security_id="",
                quantity="0",
                cash_amount="100.00",
                action="deposit",
            )
        )
        json_payload = source.export_json()
        csv_path = tmp_path / "ledger.csv"
        source.export_csv(csv_path)

    with Vault(tmp_path / "json-target.sqlite3") as target:
        assert target.import_json(json_payload) == {"inserted": 2, "skipped": 0}
        assert target.import_json(json_payload) == {"inserted": 0, "skipped": 2}
        assert target.export_json() == json_payload

    assert "record_id,idempotency_key" in csv_path.read_text(encoding="utf-8")


def test_conflicting_idempotency_key_is_rejected(tmp_path: Path) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.append(_entry())
        with pytest.raises(ValueError, match="idempotency"):
            vault.append(_entry(record_id="trade-002", quantity="11"))


def test_entry_validates_timestamp_and_record_shape() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _entry(occurred_at=datetime(2026, 7, 11, 9, 30).isoformat())
    with pytest.raises(ValueError, match="security_id"):
        _entry(security_id="", kind="trade")


def test_holding_corrections_and_deletions_are_append_only(tmp_path: Path) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        original = HoldingRecord("record-1", "CN:SSE:600519:STOCK", "a_share", "10000", "2026-07-09", holding_id="holding-1")
        vault.import_holdings([original])
        vault.revise_holding("holding-1", HoldingRecord("replacement", original.security_id, "a_share", "12000", "2026-07-10"))
        assert vault.holding_entries()[0]["invested_amount_cny"] == "12000"
        assert vault.connection.execute("SELECT COUNT(*) FROM holding_records").fetchone()[0] == 2
        vault.delete_holding("holding-1")
        assert vault.holding_entries() == []
        assert vault.connection.execute("SELECT COUNT(*) FROM holding_records").fetchone()[0] == 3
