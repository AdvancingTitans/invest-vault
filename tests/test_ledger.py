import sqlite3
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


def test_holding_corrections_remain_revisioned_but_deletion_is_permanent(tmp_path: Path) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        original = HoldingRecord("record-1", "CN:SSE:600519:STOCK", "a_share", "10000", "2026-07-09", holding_id="holding-1")
        vault.import_holdings([original])
        vault.revise_holding("holding-1", HoldingRecord("replacement", original.security_id, "a_share", "12000", "2026-07-10"))
        assert vault.holding_entries()[0]["invested_amount_cny"] == "12000"
        assert vault.connection.execute("SELECT COUNT(*) FROM holding_records").fetchone()[0] == 2
        vault.delete_holding("holding-1")
        assert vault.holding_entries() == []
        assert vault.connection.execute("SELECT COUNT(*) FROM holding_records").fetchone()[0] == 0


def test_permanent_delete_migration_purges_legacy_holding_tombstones(tmp_path: Path) -> None:
    database = tmp_path / "vault.sqlite3"
    with Vault(database) as vault:
        vault.import_holdings(
            [
                HoldingRecord("record-1", "CN:SSE:600519:STOCK", "a_share", "10000", "2026-07-09", holding_id="holding-1"),
                HoldingRecord(
                    "record-2",
                    "CN:SSE:600519:STOCK",
                    "a_share",
                    "10000",
                    "2026-07-09",
                    holding_id="holding-1",
                    revision_number=2,
                    is_deleted=True,
                ),
            ]
        )
        vault.connection.execute("DELETE FROM schema_migrations WHERE version = 11")
        vault.connection.commit()

    with Vault(database) as migrated:
        assert migrated.connection.execute("SELECT COUNT(*) FROM holding_records").fetchone()[0] == 0
        assert migrated.connection.execute("SELECT 1 FROM schema_migrations WHERE version = 11").fetchone()


def test_research_performance_v16_preserves_v15_metrics_and_adds_summary(tmp_path: Path) -> None:
    database = tmp_path / "vault.sqlite3"
    now = "2026-07-22T10:00:00+00:00"
    run_id = "run-v15"
    with Vault(database) as vault:
        vault.connection.execute(
            """INSERT INTO research_threads
            (thread_id, thread_type, title, security_id, portfolio_id, provider_type,
             provider_thread_ref, role_id, status, created_at, updated_at)
            VALUES ('thread-v15', 'committee', 'v15 migration', NULL, NULL, 'fixture',
                    NULL, 'general', 'completed', ?, ?)""",
            (now, now),
        )
        vault.connection.execute(
            """INSERT INTO research_runs
            (run_id, thread_id, workflow_version, status, current_stage, user_request_json,
             plan_json, started_at, completed_at, failure_json)
            VALUES (?, 'thread-v15', 'v15', 'completed', 'completed', '{}', NULL, ?, ?, NULL)""",
            (run_id, now, now),
        )
        vault.connection.commit()

    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 16")
        connection.execute("DROP TABLE research_performance_summaries")
        connection.execute(
            "ALTER TABLE research_call_metrics RENAME TO research_call_metrics_v16"
        )
        connection.executescript(
            """
            CREATE TABLE research_call_metrics (
                metric_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                task_id TEXT,
                stage TEXT NOT NULL,
                role_id TEXT,
                section_key TEXT,
                provider_type TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER,
                system_tokens INTEGER,
                evidence_tokens INTEGER,
                schema_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_tokens INTEGER,
                estimated_input_tokens INTEGER,
                evidence_count INTEGER,
                domain_tokens_json TEXT NOT NULL DEFAULT '{}',
                latency_ms INTEGER,
                timed_out INTEGER NOT NULL DEFAULT 0,
                skill_invoked INTEGER NOT NULL DEFAULT 0,
                cited_evidence_count INTEGER,
                available_evidence_count INTEGER,
                framework_coverage_json TEXT,
                covered_expert_count INTEGER,
                error_json TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE,
                FOREIGN KEY (task_id) REFERENCES research_tasks(task_id) ON DELETE SET NULL
            );
            DROP TABLE research_call_metrics_v16;
            CREATE INDEX research_call_metrics_run_stage
            ON research_call_metrics(run_id, stage, created_at);
            """
        )
        connection.execute(
            """INSERT INTO research_call_metrics
            (metric_id, run_id, stage, role_id, provider_type, model,
             estimated_input_tokens, evidence_count, domain_tokens_json, latency_ms,
             timed_out, skill_invoked, cited_evidence_count, available_evidence_count,
             started_at, completed_at)
            VALUES ('metric-v15', ?, 'expert_packet', 'buffett', 'fixture', 'fixture-model',
                    1234, 4, '{"company-financial-quality": 900}', 2500, 0, 0, 2, 4, ?, ?)""",
            (run_id, now, now),
        )
        connection.commit()

    with Vault(database) as migrated:
        assert migrated.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 16"
        ).fetchone()
        metric_columns = {
            str(row["name"])
            for row in migrated.connection.execute("PRAGMA table_info(research_call_metrics)")
        }
        assert {
            "node_id",
            "token_budget",
            "retry_count",
            "usage_source",
            "estimated_system_tokens",
            "estimated_context_tokens",
            "estimated_output_tokens",
        } <= metric_columns
        metric = migrated.connection.execute(
            """SELECT stage, role_id, estimated_input_tokens, retry_count, usage_source,
                      node_id, token_budget FROM research_call_metrics
               WHERE metric_id = 'metric-v15'"""
        ).fetchone()
        assert tuple(metric) == (
            "expert_packet",
            "buffett",
            1234,
            0,
            "estimated",
            None,
            None,
        )
        assert migrated.connection.execute(
            "SELECT status FROM research_runs WHERE run_id = ?", (run_id,)
        ).fetchone()["status"] == "completed"

        migrated.connection.execute(
            """INSERT INTO research_performance_summaries
            (run_id, evidence_count, domain_distribution_json, unused_evidence_count,
             cited_evidence_count, citation_rate, packet_count, semantic_revision_count,
             duration_ms, retry_count, section_count, section_llm_call_count,
             final_context_tokens, completion_status, failure_stage, failure_agent,
             failure_node_id, failure_provider, failure_token_estimate,
             failure_token_budget, failure_json, updated_at)
            VALUES (?, 7, '{"financial": 4}', 1, 6, 0.95, 18, 8, 600000, 1, 8, 0,
                    22802, 'completed', 'final_edit', 'report_editor', 'final-edit',
                    'fixture', 30001, 30000, '{"message": "token budget"}', ?)""",
            (run_id, now),
        )
        migrated.connection.commit()
        summary = migrated.connection.execute(
            """SELECT evidence_count, packet_count, semantic_revision_count,
                      section_llm_call_count, final_context_tokens, failure_node_id,
                      failure_token_estimate, failure_token_budget
               FROM research_performance_summaries WHERE run_id = ?""",
            (run_id,),
        ).fetchone()
        assert tuple(summary) == (7, 18, 8, 0, 22802, "final-edit", 30001, 30000)

    with Vault(database) as reopened:
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM research_call_metrics WHERE metric_id = 'metric-v15'"
        ).fetchone()[0] == 1
        assert reopened.connection.execute(
            "SELECT COUNT(*) FROM research_performance_summaries WHERE run_id = ?", (run_id,)
        ).fetchone()[0] == 1
