"""Append-only SQLite ledger and deterministic position projections."""

from __future__ import annotations

import csv
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from uuid import uuid4

ENTRY_FIELDS = (
    "record_id",
    "idempotency_key",
    "kind",
    "account_id",
    "security_id",
    "occurred_at",
    "quantity",
    "cash_amount",
    "currency",
    "action",
)
ENTRY_KINDS = {"trade", "cash", "corporate_action"}


@dataclass(frozen=True)
class VaultSettings:
    host: str = "127.0.0.1"

    def __post_init__(self) -> None:
        if self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Invest Vault must bind to a loopback host")


@dataclass(frozen=True)
class LedgerEntry:
    record_id: str
    idempotency_key: str
    kind: Literal["trade", "cash", "corporate_action"]
    account_id: str
    security_id: str
    occurred_at: str
    quantity: str
    cash_amount: str
    currency: str
    action: str

    def __post_init__(self) -> None:
        if not self.record_id or not self.idempotency_key or not self.account_id or not self.action:
            raise ValueError("record_id, idempotency_key, account_id and action are required")
        if self.kind not in ENTRY_KINDS:
            raise ValueError(f"unsupported ledger kind: {self.kind}")
        if self.kind in {"trade", "corporate_action"} and not self.security_id:
            raise ValueError("security_id is required for trades and corporate actions")
        try:
            occurred_at = datetime.fromisoformat(self.occurred_at)
        except ValueError as error:
            raise ValueError("occurred_at must be an ISO-8601 timestamp") from error
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone offset")
        for field_name, value in (("quantity", self.quantity), ("cash_amount", self.cash_amount)):
            try:
                Decimal(value)
            except (InvalidOperation, ValueError) as error:
                raise ValueError(f"{field_name} must be a decimal string") from error
        if not self.currency:
            raise ValueError("currency is required")

    def as_dict(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in ENTRY_FIELDS}


@dataclass(frozen=True)
class PositionProjection:
    security_id: str
    quantity: str
    valuation_status: Literal["unavailable"] = "unavailable"
    missing_fields: tuple[str, ...] = ("market_price", "cost_basis")


@dataclass(frozen=True)
class HoldingRecord:
    record_id: str
    security_id: str
    asset_type: Literal["a_share", "hk_stock", "us_stock", "fund"]
    invested_amount_cny: str
    bought_on: str
    holding_id: str | None = None
    revision_number: int = 1
    is_deleted: bool = False

    def __post_init__(self) -> None:
        if not self.record_id or not self.security_id:
            raise ValueError("record_id and security_id are required")
        try:
            amount = Decimal(self.invested_amount_cny)
        except (InvalidOperation, ValueError) as error:
            raise ValueError("买入金额必须是数字") from error
        if amount <= 0:
            raise ValueError("买入金额必须大于0")
        try:
            date.fromisoformat(self.bought_on)
        except ValueError as error:
            raise ValueError("买入日期格式不正确") from error
        if self.revision_number < 1:
            raise ValueError("持仓修订版本必须大于0")


class Vault:
    """Local vault with append-only records; projections are always replayed from ledger rows."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        # ponytail: one serialized connection is sufficient for a local single-user app;
        # move to connection-per-request if concurrent background jobs are introduced.
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def __enter__(self) -> Vault:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS portfolios (
                portfolio_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL DEFAULT 'default',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id)
            );
            CREATE TABLE IF NOT EXISTS portfolio_preferences (
                portfolio_id TEXT PRIMARY KEY,
                max_drawdown_percent TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (portfolio_id) REFERENCES portfolios(portfolio_id)
            );
            CREATE TABLE IF NOT EXISTS securities (
                security_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ledger_entries (
                record_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK (kind IN ('trade', 'cash', 'corporate_action')),
                account_id TEXT NOT NULL,
                security_id TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL,
                quantity TEXT NOT NULL,
                cash_amount TEXT NOT NULL,
                currency TEXT NOT NULL,
                action TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(account_id)
            );
            CREATE TABLE IF NOT EXISTS holding_records (
                record_id TEXT PRIMARY KEY,
                holding_id TEXT,
                revision_number INTEGER NOT NULL DEFAULT 1,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                security_id TEXT NOT NULL,
                asset_type TEXT NOT NULL CHECK (asset_type IN ('a_share', 'hk_stock', 'us_stock', 'fund')),
                invested_amount_cny TEXT NOT NULL,
                bought_on TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS holding_records_security
            ON holding_records(security_id, bought_on, record_id);
            CREATE TRIGGER IF NOT EXISTS holding_records_no_update
            BEFORE UPDATE ON holding_records
            BEGIN SELECT RAISE(ABORT, 'holding_records is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS ledger_entries_no_update
            BEFORE UPDATE ON ledger_entries
            BEGIN SELECT RAISE(ABORT, 'ledger_entries is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS ledger_entries_no_delete
            BEFORE DELETE ON ledger_entries
            BEGIN SELECT RAISE(ABORT, 'ledger_entries is append-only'); END;

            CREATE TABLE IF NOT EXISTS evidence_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                security_id TEXT NOT NULL,
                requested_as_of TEXT NOT NULL,
                effective_as_of TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                producer_version TEXT NOT NULL,
                availability_state TEXT NOT NULL,
                missing_fields_json TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                manifest_path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS evidence_snapshots_security_observed
            ON evidence_snapshots(security_id, observed_at DESC);
            CREATE TABLE IF NOT EXISTS evidence_items (
                snapshot_id TEXT NOT NULL,
                item_index INTEGER NOT NULL,
                kind TEXT NOT NULL,
                value_json TEXT NOT NULL,
                unit TEXT,
                period_end TEXT,
                provider TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                availability_state TEXT NOT NULL,
                missing_fields_json TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                PRIMARY KEY (snapshot_id, item_index),
                FOREIGN KEY (snapshot_id) REFERENCES evidence_snapshots(snapshot_id)
            );
            CREATE INDEX IF NOT EXISTS evidence_items_kind ON evidence_items(kind);
            CREATE TABLE IF NOT EXISTS raw_manifest_refs (
                snapshot_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                provider TEXT NOT NULL,
                source_ref TEXT,
                raw_path TEXT NOT NULL,
                PRIMARY KEY (snapshot_id, sha256),
                FOREIGN KEY (snapshot_id) REFERENCES evidence_snapshots(snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS refresh_jobs (
                job_id TEXT PRIMARY KEY,
                security_id TEXT NOT NULL,
                refresh_kind TEXT NOT NULL,
                requested_as_of TEXT NOT NULL,
                provider TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'partial', 'failed')),
                snapshot_id TEXT,
                error_provider TEXT,
                error_detail TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                FOREIGN KEY (snapshot_id) REFERENCES evidence_snapshots(snapshot_id)
            );
            CREATE INDEX IF NOT EXISTS refresh_jobs_security_created ON refresh_jobs(security_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS provider_health (
                provider TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                last_success_at TEXT,
                last_failure_at TEXT,
                detail TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS theses (
                thesis_id TEXT PRIMARY KEY,
                security_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS thesis_revisions (
                revision_id TEXT PRIMARY KEY,
                thesis_id TEXT NOT NULL,
                revision_number INTEGER NOT NULL,
                body TEXT NOT NULL,
                cited_snapshot_ids_json TEXT NOT NULL,
                review_due_on TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(thesis_id, revision_number),
                FOREIGN KEY (thesis_id) REFERENCES theses(thesis_id)
            );
            CREATE TABLE IF NOT EXISTS notes (
                note_id TEXT PRIMARY KEY,
                security_id TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                title TEXT,
                market_session TEXT CHECK (market_session IN ('盘前', '盘中', '盘后') OR market_session IS NULL)
            );
            CREATE TABLE IF NOT EXISTS note_revisions (
                revision_id TEXT PRIMARY KEY,
                note_id TEXT NOT NULL,
                revision_number INTEGER NOT NULL,
                body TEXT NOT NULL,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(note_id, revision_number),
                FOREIGN KEY (note_id) REFERENCES notes(note_id)
            );
            CREATE TABLE IF NOT EXISTS thesis_status_events (
                event_id TEXT PRIMARY KEY,
                thesis_id TEXT NOT NULL,
                is_deleted INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (thesis_id) REFERENCES theses(thesis_id)
            );
            CREATE TABLE IF NOT EXISTS research_materials (
                material_id TEXT PRIMARY KEY,
                security_id TEXT NOT NULL,
                material_type TEXT NOT NULL,
                title TEXT NOT NULL,
                published_at TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                excerpt TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS research_materials_security_published
            ON research_materials(security_id, published_at DESC, material_id DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS research_materials_security_source
            ON research_materials(security_id, source_url);
            CREATE TABLE IF NOT EXISTS material_sync_dates (
                security_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (security_id, trade_date)
            );
            CREATE TABLE IF NOT EXISTS note_material_refs (
                note_id TEXT PRIMARY KEY,
                material_id TEXT NOT NULL,
                quoted_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (note_id) REFERENCES notes(note_id),
                FOREIGN KEY (material_id) REFERENCES research_materials(material_id)
            );
            CREATE TABLE IF NOT EXISTS attachments (
                attachment_id TEXT PRIMARY KEY,
                note_id TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                filename TEXT NOT NULL,
                media_type TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (note_id) REFERENCES notes(note_id)
            );
            CREATE TABLE IF NOT EXISTS fx_observations (
                observation_id TEXT PRIMARY KEY,
                base_currency TEXT NOT NULL,
                quote_currency TEXT NOT NULL,
                rate TEXT NOT NULL,
                effective_as_of TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                snapshot_id TEXT,
                FOREIGN KEY (snapshot_id) REFERENCES evidence_snapshots(snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS timeline_events (
                event_id TEXT PRIMARY KEY,
                security_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                reference_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                summary TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS timeline_events_security_occurred
            ON timeline_events(security_id, occurred_at DESC, event_id DESC);
            CREATE TABLE IF NOT EXISTS financial_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                security_id TEXT NOT NULL,
                cutoff_date TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE(security_id, cutoff_date)
            );
            CREATE TABLE IF NOT EXISTS market_snapshots (
                section TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY(section, trade_date)
            );
            CREATE TABLE IF NOT EXISTS fund_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                security_id TEXT NOT NULL,
                cutoff_date TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE(security_id, cutoff_date)
            );
            CREATE TABLE IF NOT EXISTS ai_quick_notes (
                draft_id TEXT PRIMARY KEY,
                security_id TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                draft_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('draft', 'accepted', 'discarded')),
                accepted_note_id TEXT,
                created_at TEXT NOT NULL,
                accepted_at TEXT,
                FOREIGN KEY (accepted_note_id) REFERENCES notes(note_id)
            );
            CREATE INDEX IF NOT EXISTS ai_quick_notes_security_created
            ON ai_quick_notes(security_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS ai_provider_settings (
                provider_id TEXT PRIMARY KEY, provider_type TEXT NOT NULL, enabled INTEGER NOT NULL,
                model_config_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_provider_credentials (
                provider_id TEXT PRIMARY KEY,
                encrypted_secret TEXT NOT NULL,
                masked_suffix TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_threads (
                thread_id TEXT PRIMARY KEY, thread_type TEXT NOT NULL, title TEXT NOT NULL,
                security_id TEXT, portfolio_id TEXT, provider_type TEXT NOT NULL,
                provider_thread_ref TEXT, role_id TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS research_threads_security_updated
            ON research_threads(security_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS research_runs (
                run_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, workflow_version TEXT NOT NULL,
                status TEXT NOT NULL, current_stage TEXT NOT NULL, user_request_json TEXT NOT NULL,
                plan_json TEXT, started_at TEXT NOT NULL, completed_at TEXT, failure_json TEXT,
                FOREIGN KEY (thread_id) REFERENCES research_threads(thread_id)
            );
            CREATE TABLE IF NOT EXISTS research_events (
                event_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, run_id TEXT,
                sequence_number INTEGER NOT NULL, event_type TEXT NOT NULL, actor_type TEXT NOT NULL,
                actor_id TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(thread_id, sequence_number),
                FOREIGN KEY (thread_id) REFERENCES research_threads(thread_id)
            );
            CREATE TABLE IF NOT EXISTS research_tasks (
                task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent_task_id TEXT,
                assigned_role TEXT NOT NULL, task_type TEXT NOT NULL, input_json TEXT NOT NULL,
                output_json TEXT, status TEXT NOT NULL, attempt INTEGER NOT NULL,
                started_at TEXT, completed_at TEXT,
                FOREIGN KEY (run_id) REFERENCES research_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS research_evidence_links (
                run_id TEXT NOT NULL, task_id TEXT, evidence_id TEXT NOT NULL, relation TEXT NOT NULL,
                PRIMARY KEY(run_id, task_id, evidence_id, relation)
            );
            CREATE TABLE IF NOT EXISTS research_reports (
                report_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, version INTEGER NOT NULL,
                report_json TEXT NOT NULL, rendered_markdown TEXT NOT NULL,
                frozen_at TEXT, created_at TEXT NOT NULL,
                UNIQUE(run_id, version), FOREIGN KEY (run_id) REFERENCES research_runs(run_id)
            );
            """
        )
        columns = {str(row["name"]) for row in self.connection.execute("PRAGMA table_info(holding_records)")}
        for name, definition in (
            ("holding_id", "TEXT"),
            ("revision_number", "INTEGER NOT NULL DEFAULT 1"),
            ("is_deleted", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE holding_records ADD COLUMN {name} {definition}")
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS holding_records_revision ON holding_records(holding_id, revision_number)"
        )
        # Holding corrections remain revisioned, but an explicit user delete is permanent.
        self.connection.execute("DROP TRIGGER IF EXISTS holding_records_no_delete")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (1)")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (2)")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (3)")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (4)")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (5)")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (6)")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (7)")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (8)")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (9)")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (10)")
        if self.connection.execute("SELECT 1 FROM schema_migrations WHERE version = 11").fetchone() is None:
            self._purge_legacy_deleted_records()
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (11)")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (12)")
        note_columns = {str(row["name"]) for row in self.connection.execute("PRAGMA table_info(notes)")}
        if "title" not in note_columns:
            self.connection.execute("ALTER TABLE notes ADD COLUMN title TEXT")
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (13)")
        note_columns = {str(row["name"]) for row in self.connection.execute("PRAGMA table_info(notes)")}
        if "market_session" not in note_columns:
            self.connection.execute("ALTER TABLE notes ADD COLUMN market_session TEXT")
        if self.connection.execute("SELECT 1 FROM schema_migrations WHERE version = 14").fetchone() is None:
            for session in ("盘前", "盘中", "盘后"):
                self.connection.execute(
                    """UPDATE notes SET market_session = ?
                    WHERE security_id = 'MARKET:GLOBAL:OVERVIEW' AND market_session IS NULL
                    AND (title LIKE ? OR body LIKE ?)""",
                    (session, f"% {session}报告（%", f"行情阶段%{session}%"),
                )
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (14)")
        self._migrate_research_pipeline_v15()
        self._migrate_research_performance_v16()
        self.connection.commit()

    def _migrate_research_pipeline_v15(self) -> None:
        """Add durable checkpoints for the packetized research pipeline."""
        # Plan P0-P3: persist complete evidence, incremental expert state, claims,
        # report sections, and per-call observability without changing legacy tables.
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS research_evidence_records (
                evidence_id TEXT PRIMARY KEY,
                security_id TEXT,
                domain TEXT NOT NULL,
                subtype TEXT NOT NULL,
                entity_id TEXT,
                as_of TEXT,
                observed_at TEXT NOT NULL,
                source_tier TEXT NOT NULL,
                provider TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                quality_status TEXT NOT NULL,
                value_json TEXT NOT NULL,
                compact_text TEXT NOT NULL,
                token_estimate INTEGER NOT NULL CHECK (token_estimate >= 0),
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(content_hash)
            );
            CREATE INDEX IF NOT EXISTS research_evidence_records_run_domain
            ON research_evidence_records(domain, observed_at DESC);

            CREATE TABLE IF NOT EXISTS research_evidence_packets (
                packet_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                sequence_number INTEGER NOT NULL CHECK (sequence_number >= 0),
                objective TEXT NOT NULL,
                required_outputs_json TEXT NOT NULL,
                evidence_ids_json TEXT NOT NULL,
                known_gaps_json TEXT NOT NULL,
                token_estimate INTEGER NOT NULL CHECK (token_estimate >= 0),
                status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_id, role_id, sequence_number),
                FOREIGN KEY (run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS research_evidence_packets_run_status
            ON research_evidence_packets(run_id, status, role_id);

            CREATE TABLE IF NOT EXISTS research_expert_states (
                state_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 0),
                processed_packet_id TEXT,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_id, role_id, revision),
                FOREIGN KEY (run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE,
                FOREIGN KEY (processed_packet_id) REFERENCES research_evidence_packets(packet_id)
                    ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS research_expert_states_latest
            ON research_expert_states(run_id, role_id, revision DESC);

            CREATE TABLE IF NOT EXISTS research_claims (
                claim_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                claim_key TEXT NOT NULL,
                role_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                claim_text TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence TEXT NOT NULL,
                supporting_evidence_ids_json TEXT NOT NULL,
                contradicting_evidence_ids_json TEXT NOT NULL,
                conditions_json TEXT NOT NULL,
                as_of TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_id, claim_key),
                FOREIGN KEY (run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS research_claims_run_topic
            ON research_claims(run_id, topic, status);

            CREATE TABLE IF NOT EXISTS research_claim_boards (
                board_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                board_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_id, revision),
                FOREIGN KEY (run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS research_claim_conflicts (
                conflict_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                claim_key TEXT NOT NULL,
                supporting_evidence_ids_json TEXT NOT NULL,
                contradicting_evidence_ids_json TEXT NOT NULL,
                roles_json TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_id, claim_key),
                FOREIGN KEY (run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS research_risk_reviews (
                review_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS research_report_sections (
                section_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                section_key TEXT NOT NULL,
                sequence_number INTEGER NOT NULL CHECK (sequence_number >= 0),
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                input_json TEXT NOT NULL,
                output_json TEXT,
                rendered_markdown TEXT,
                attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
                error_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                UNIQUE(run_id, section_key),
                UNIQUE(run_id, sequence_number),
                FOREIGN KEY (run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS research_report_sections_run_status
            ON research_report_sections(run_id, status, sequence_number);

            CREATE TABLE IF NOT EXISTS research_call_metrics (
                metric_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                task_id TEXT,
                stage TEXT NOT NULL,
                role_id TEXT,
                section_key TEXT,
                provider_type TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
                system_tokens INTEGER CHECK (system_tokens IS NULL OR system_tokens >= 0),
                evidence_tokens INTEGER CHECK (evidence_tokens IS NULL OR evidence_tokens >= 0),
                schema_tokens INTEGER CHECK (schema_tokens IS NULL OR schema_tokens >= 0),
                output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
                reasoning_tokens INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
                estimated_input_tokens INTEGER
                    CHECK (estimated_input_tokens IS NULL OR estimated_input_tokens >= 0),
                evidence_count INTEGER CHECK (evidence_count IS NULL OR evidence_count >= 0),
                domain_tokens_json TEXT NOT NULL DEFAULT '{}',
                latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
                timed_out INTEGER NOT NULL DEFAULT 0 CHECK (timed_out IN (0, 1)),
                skill_invoked INTEGER NOT NULL DEFAULT 0 CHECK (skill_invoked IN (0, 1)),
                cited_evidence_count INTEGER
                    CHECK (cited_evidence_count IS NULL OR cited_evidence_count >= 0),
                available_evidence_count INTEGER
                    CHECK (available_evidence_count IS NULL OR available_evidence_count >= 0),
                framework_coverage_json TEXT,
                covered_expert_count INTEGER
                    CHECK (covered_expert_count IS NULL OR covered_expert_count >= 0),
                error_json TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE,
                FOREIGN KEY (task_id) REFERENCES research_tasks(task_id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS research_call_metrics_run_stage
            ON research_call_metrics(run_id, stage, created_at);
            """
        )
        self.connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (15)")

    def _migrate_research_performance_v16(self) -> None:
        """Add run/node performance observability without rewriting v15 research data."""

        if self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 16"
        ).fetchone() is not None:
            return

        metric_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(research_call_metrics)")
        }
        additions = (
            ("node_id", "TEXT"),
            ("token_budget", "INTEGER CHECK (token_budget IS NULL OR token_budget >= 0)"),
            ("retry_count", "INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0)"),
            ("usage_source", "TEXT NOT NULL DEFAULT 'estimated'"),
            (
                "estimated_system_tokens",
                "INTEGER CHECK (estimated_system_tokens IS NULL OR estimated_system_tokens >= 0)",
            ),
            (
                "estimated_context_tokens",
                "INTEGER CHECK (estimated_context_tokens IS NULL OR estimated_context_tokens >= 0)",
            ),
            (
                "estimated_output_tokens",
                "INTEGER CHECK (estimated_output_tokens IS NULL OR estimated_output_tokens >= 0)",
            ),
        )
        for name, definition in additions:
            if name not in metric_columns:
                self.connection.execute(
                    f"ALTER TABLE research_call_metrics ADD COLUMN {name} {definition}"
                )

        self.connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS research_call_metrics_run_stage_node
            ON research_call_metrics(run_id, stage, node_id, created_at);

            CREATE TABLE IF NOT EXISTS research_performance_summaries (
                run_id TEXT PRIMARY KEY,
                evidence_count INTEGER NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
                domain_distribution_json TEXT NOT NULL DEFAULT '{}',
                unused_evidence_count INTEGER NOT NULL DEFAULT 0
                    CHECK (unused_evidence_count >= 0),
                cited_evidence_count INTEGER NOT NULL DEFAULT 0
                    CHECK (cited_evidence_count >= 0),
                citation_rate REAL CHECK (
                    citation_rate IS NULL OR (citation_rate >= 0 AND citation_rate <= 1)
                ),
                packet_count INTEGER NOT NULL DEFAULT 0 CHECK (packet_count >= 0),
                semantic_revision_count INTEGER NOT NULL DEFAULT 0
                    CHECK (semantic_revision_count >= 0),
                duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
                retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
                section_count INTEGER NOT NULL DEFAULT 0 CHECK (section_count >= 0),
                section_llm_call_count INTEGER NOT NULL DEFAULT 0
                    CHECK (section_llm_call_count >= 0),
                final_context_tokens INTEGER
                    CHECK (final_context_tokens IS NULL OR final_context_tokens >= 0),
                completion_status TEXT NOT NULL DEFAULT 'pending',
                usage_source TEXT NOT NULL DEFAULT 'estimated',
                failure_stage TEXT,
                failure_agent TEXT,
                failure_node_id TEXT,
                failure_provider TEXT,
                failure_token_estimate INTEGER CHECK (
                    failure_token_estimate IS NULL OR failure_token_estimate >= 0
                ),
                failure_token_budget INTEGER CHECK (
                    failure_token_budget IS NULL OR failure_token_budget >= 0
                ),
                failure_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (run_id) REFERENCES research_runs(run_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS research_performance_summaries_status
            ON research_performance_summaries(completion_status, updated_at);
            """
        )
        self.connection.execute("INSERT INTO schema_migrations(version) VALUES (16)")

    def _purge_legacy_deleted_records(self) -> None:
        """Remove tombstones created before explicit deletion became permanent."""
        connection = self.connection
        attachment_paths = [
            Path(str(row["storage_path"]))
            for row in connection.execute(
                """SELECT a.storage_path FROM attachments a WHERE a.note_id IN (
                SELECT n.note_id FROM notes n JOIN note_revisions r ON r.revision_id = (
                    SELECT revision_id FROM note_revisions WHERE note_id = n.note_id
                    ORDER BY revision_number DESC LIMIT 1
                ) WHERE r.is_deleted = 1)"""
            )
        ]
        deleted_notes = [
            str(row["note_id"])
            for row in connection.execute(
                """SELECT n.note_id FROM notes n JOIN note_revisions r ON r.revision_id = (
                SELECT revision_id FROM note_revisions WHERE note_id = n.note_id
                ORDER BY revision_number DESC LIMIT 1
                ) WHERE r.is_deleted = 1"""
            )
        ]
        for note_id in deleted_notes:
            evidence_prefix = f"EVIDENCE-NOTE-{note_id}%"
            connection.execute(
                "DELETE FROM research_evidence_links WHERE evidence_id LIKE ?",
                (evidence_prefix,),
            )
            connection.execute(
                "DELETE FROM research_evidence_records WHERE evidence_id LIKE ?",
                (evidence_prefix,),
            )
            connection.execute("DELETE FROM ai_quick_notes WHERE accepted_note_id = ?", (note_id,))
            connection.execute("DELETE FROM attachments WHERE note_id = ?", (note_id,))
            connection.execute("DELETE FROM note_material_refs WHERE note_id = ?", (note_id,))
            connection.execute(
                """DELETE FROM timeline_events WHERE reference_id = ? OR reference_id IN
                (SELECT revision_id FROM note_revisions WHERE note_id = ?)""",
                (note_id, note_id),
            )
            connection.execute("DELETE FROM note_revisions WHERE note_id = ?", (note_id,))
            connection.execute("DELETE FROM notes WHERE note_id = ?", (note_id,))

        deleted_theses = [
            str(row["thesis_id"])
            for row in connection.execute(
                """SELECT t.thesis_id FROM theses t JOIN thesis_status_events s ON s.event_id = (
                SELECT event_id FROM thesis_status_events WHERE thesis_id = t.thesis_id
                ORDER BY created_at DESC LIMIT 1
                ) WHERE s.is_deleted = 1"""
            )
        ]
        for thesis_id in deleted_theses:
            connection.execute(
                """DELETE FROM timeline_events WHERE reference_id IN
                (SELECT revision_id FROM thesis_revisions WHERE thesis_id = ?)
                OR reference_id IN (SELECT event_id FROM thesis_status_events WHERE thesis_id = ?)""",
                (thesis_id, thesis_id),
            )
            connection.execute("DELETE FROM thesis_status_events WHERE thesis_id = ?", (thesis_id,))
            connection.execute("DELETE FROM thesis_revisions WHERE thesis_id = ?", (thesis_id,))
            connection.execute("DELETE FROM theses WHERE thesis_id = ?", (thesis_id,))

        deleted_holdings = [
            str(row["logical_id"])
            for row in connection.execute(
                """SELECT COALESCE(h.holding_id, h.record_id) AS logical_id
                FROM holding_records h JOIN (
                    SELECT COALESCE(holding_id, record_id) AS logical_id,
                    MAX(revision_number) AS latest_revision
                    FROM holding_records GROUP BY COALESCE(holding_id, record_id)
                ) latest ON COALESCE(h.holding_id, h.record_id) = latest.logical_id
                AND h.revision_number = latest.latest_revision WHERE h.is_deleted = 1"""
            )
        ]
        for holding_id in deleted_holdings:
            connection.execute(
                "DELETE FROM holding_records WHERE COALESCE(holding_id, record_id) = ?",
                (holding_id,),
            )

        archived_threads = [
            str(row["thread_id"])
            for row in connection.execute("SELECT thread_id FROM research_threads WHERE status = 'archived'")
        ]
        for thread_id in archived_threads:
            connection.execute(
                "DELETE FROM research_reports WHERE run_id IN (SELECT run_id FROM research_runs WHERE thread_id = ?)",
                (thread_id,),
            )
            connection.execute(
                "DELETE FROM research_evidence_links WHERE run_id IN (SELECT run_id FROM research_runs WHERE thread_id = ?)",
                (thread_id,),
            )
            connection.execute(
                "DELETE FROM research_tasks WHERE run_id IN (SELECT run_id FROM research_runs WHERE thread_id = ?)",
                (thread_id,),
            )
            connection.execute("DELETE FROM research_events WHERE thread_id = ?", (thread_id,))
            connection.execute("DELETE FROM research_runs WHERE thread_id = ?", (thread_id,))
            connection.execute("DELETE FROM research_threads WHERE thread_id = ?", (thread_id,))

        connection.commit()
        for path in attachment_paths:
            if (
                connection.execute(
                    "SELECT 1 FROM attachments WHERE storage_path = ? LIMIT 1", (str(path),)
                ).fetchone()
                is None
            ):
                path.unlink(missing_ok=True)

    def append(self, entry: LedgerEntry) -> bool:
        """Append an entry; repeat imports skip only an exactly identical record."""

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT * FROM ledger_entries WHERE record_id = ? OR idempotency_key = ?",
                (entry.record_id, entry.idempotency_key),
            ).fetchone()
            if existing is not None:
                if self._row_matches_entry(existing, entry):
                    self.connection.commit()
                    return False
                raise ValueError("idempotency key or record_id conflicts with an existing append-only entry")
            self.connection.execute("INSERT OR IGNORE INTO portfolios(portfolio_id) VALUES ('default')")
            self.connection.execute(
                "INSERT OR IGNORE INTO accounts(account_id) VALUES (?)", (entry.account_id,)
            )
            # ponytail: security metadata stays out of Phase 2.
            # Phase 3 attaches facts without mutating ledger rows.
            if entry.security_id:
                self.connection.execute(
                    "INSERT OR IGNORE INTO securities(security_id) VALUES (?)", (entry.security_id,)
                )
            self.connection.execute(
                f"INSERT INTO ledger_entries ({', '.join(ENTRY_FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in ENTRY_FIELDS)})",
                tuple(entry.as_dict()[field] for field in ENTRY_FIELDS),
            )
            self.connection.commit()
            return True
        except BaseException:
            self.connection.rollback()
            raise

    def count_entries(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0])

    def import_holdings(self, records: Iterable[HoldingRecord]) -> dict[str, int]:
        inserted = skipped = 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for record in records:
                existing = self.connection.execute(
                    "SELECT * FROM holding_records WHERE record_id = ?", (record.record_id,)
                ).fetchone()
                values = (
                    record.record_id,
                    record.holding_id or record.record_id,
                    record.revision_number,
                    int(record.is_deleted),
                    record.security_id,
                    record.asset_type,
                    record.invested_amount_cny,
                    record.bought_on,
                )
                if existing is not None:
                    fields = (
                        "record_id",
                        "holding_id",
                        "revision_number",
                        "is_deleted",
                        "security_id",
                        "asset_type",
                        "invested_amount_cny",
                        "bought_on",
                    )
                    if tuple(str(existing[key]) for key in fields) == tuple(str(value) for value in values):
                        skipped += 1
                        continue
                    raise ValueError("该行记录与已保存内容冲突，请重新添加")
                self.connection.execute(
                    "INSERT INTO holding_records(record_id, holding_id, revision_number, is_deleted, security_id, asset_type, invested_amount_cny, bought_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                inserted += 1
            self.connection.commit()
            return {"inserted": inserted, "skipped": skipped}
        except BaseException:
            self.connection.rollback()
            raise

    def holding_entries(self) -> list[dict[str, str]]:
        rows = self.connection.execute(
            """SELECT h.* FROM holding_records h
            JOIN (
                SELECT COALESCE(holding_id, record_id) AS logical_id, MAX(revision_number) AS latest_revision
                FROM holding_records GROUP BY COALESCE(holding_id, record_id)
            ) latest
            ON COALESCE(h.holding_id, h.record_id) = latest.logical_id
            AND h.revision_number = latest.latest_revision
            WHERE h.is_deleted = 0
            ORDER BY h.bought_on, h.record_id"""
        ).fetchall()
        return [
            {
                "holding_id": str(row["holding_id"] or row["record_id"]),
                "security_id": str(row["security_id"]),
                "asset_type": str(row["asset_type"]),
                "invested_amount_cny": str(row["invested_amount_cny"]),
                "bought_on": str(row["bought_on"]),
                "revision_number": str(row["revision_number"]),
            }
            for row in rows
        ]

    def revise_holding(self, holding_id: str, replacement: HoldingRecord) -> dict[str, str]:
        current = self._latest_holding_row(holding_id)
        if current is None or int(current["is_deleted"]):
            raise ValueError("持仓记录不存在或已删除")
        revision = int(current["revision_number"]) + 1
        corrected = HoldingRecord(
            record_id=f"{holding_id}-r{revision}-{uuid4()}",
            security_id=replacement.security_id,
            asset_type=replacement.asset_type,
            invested_amount_cny=replacement.invested_amount_cny,
            bought_on=replacement.bought_on,
            holding_id=holding_id,
            revision_number=revision,
        )
        self.import_holdings([corrected])
        return self._holding_dict(corrected)

    def delete_holding(self, holding_id: str) -> None:
        cursor = self.connection.execute(
            "DELETE FROM holding_records WHERE COALESCE(holding_id, record_id) = ?",
            (holding_id,),
        )
        if cursor.rowcount == 0:
            self.connection.rollback()
            raise ValueError("持仓记录不存在或已删除")
        self.connection.commit()

    def _latest_holding_row(self, holding_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT * FROM holding_records WHERE COALESCE(holding_id, record_id) = ?
            ORDER BY revision_number DESC LIMIT 1""",
            (holding_id,),
        ).fetchone()

    @staticmethod
    def _holding_dict(record: HoldingRecord) -> dict[str, str]:
        return {
            "holding_id": str(record.holding_id or record.record_id),
            "security_id": record.security_id,
            "asset_type": record.asset_type,
            "invested_amount_cny": record.invested_amount_cny,
            "bought_on": record.bought_on,
            "revision_number": str(record.revision_number),
        }

    def holding_summaries(self) -> list[dict[str, str]]:
        rows = self.holding_entries()
        summaries: dict[str, dict[str, str | Decimal]] = {}
        for row in rows:
            security_id = str(row["security_id"])
            summary = summaries.setdefault(
                security_id,
                {
                    "security_id": security_id,
                    "asset_type": str(row["asset_type"]),
                    "invested_amount_cny": Decimal("0"),
                    "bought_on": str(row["bought_on"]),
                },
            )
            summary["invested_amount_cny"] = Decimal(str(summary["invested_amount_cny"])) + Decimal(
                str(row["invested_amount_cny"])
            )
            summary["bought_on"] = min(str(summary["bought_on"]), str(row["bought_on"]))
        return [
            {
                "security_id": str(summary["security_id"]),
                "asset_type": str(summary["asset_type"]),
                "invested_amount_cny": self._decimal_text(Decimal(str(summary["invested_amount_cny"]))),
                "bought_on": str(summary["bought_on"]),
            }
            for _, summary in sorted(summaries.items())
        ]

    def export_json(self) -> list[dict[str, str]]:
        rows = self.connection.execute(
            f"SELECT {', '.join(ENTRY_FIELDS)} FROM ledger_entries ORDER BY occurred_at, record_id"
        ).fetchall()
        return [{field: str(row[field]) for field in ENTRY_FIELDS} for row in rows]

    def export_csv(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=ENTRY_FIELDS)
            writer.writeheader()
            writer.writerows(self.export_json())
        return destination

    def import_json(self, entries: Iterable[Mapping[str, str]]) -> dict[str, int]:
        return self._import(LedgerEntry(**dict(entry)) for entry in entries)

    def project_positions(self, account_id: str) -> list[PositionProjection]:
        rows = self.connection.execute(
            """
            SELECT security_id, quantity FROM ledger_entries
            WHERE account_id = ? AND kind IN ('trade', 'corporate_action')
            ORDER BY occurred_at, record_id
            """,
            (account_id,),
        ).fetchall()
        quantities: dict[str, Decimal] = {}
        for row in rows:
            security_id = row["security_id"]
            quantities[security_id] = quantities.get(security_id, Decimal("0")) + Decimal(row["quantity"])
        return [
            PositionProjection(security_id=security_id, quantity=self._decimal_text(quantity))
            for security_id, quantity in sorted(quantities.items())
            if quantity != 0
        ]

    def project_cash(self, account_id: str) -> dict[str, str]:
        rows = self.connection.execute(
            """
            SELECT currency, cash_amount FROM ledger_entries
            WHERE account_id = ? AND kind IN ('cash', 'trade')
            ORDER BY occurred_at, record_id
            """,
            (account_id,),
        ).fetchall()
        balances: dict[str, Decimal] = {}
        for row in rows:
            currency = row["currency"]
            balances[currency] = balances.get(currency, Decimal("0")) + Decimal(row["cash_amount"])
        return {currency: self._decimal_text(amount) for currency, amount in sorted(balances.items())}

    def portfolio_profile(self) -> dict[str, str | None]:
        cash = sum(
            (
                Decimal(amount)
                for row in self.connection.execute("SELECT account_id FROM accounts")
                for currency, amount in self.project_cash(str(row["account_id"])).items()
                if currency == "CNY"
            ),
            Decimal("0"),
        )
        preference = self.connection.execute(
            "SELECT max_drawdown_percent FROM portfolio_preferences WHERE portfolio_id = 'default'"
        ).fetchone()
        return {
            "cash_balance_cny": self._decimal_text(cash),
            "max_drawdown_percent": str(preference["max_drawdown_percent"]) if preference else None,
        }

    def set_portfolio_profile(
        self, *, cash_balance_cny: str, max_drawdown_percent: str
    ) -> dict[str, str | None]:
        try:
            cash = Decimal(cash_balance_cny)
            threshold = Decimal(max_drawdown_percent)
        except (InvalidOperation, ValueError) as error:
            raise ValueError("现金余额和最大可承受回撤必须是数字") from error
        if cash < 0:
            raise ValueError("现金余额不能小于0")
        if threshold <= 0 or threshold > 100:
            raise ValueError("最大可承受回撤必须大于0且不超过100%")
        current = Decimal(str(self.portfolio_profile()["cash_balance_cny"] or "0"))
        delta = cash - current
        if delta:
            event_id = str(uuid4())
            self.append(
                LedgerEntry(
                    record_id=f"cash-profile-{event_id}",
                    idempotency_key=f"cash-profile:{event_id}",
                    kind="cash",
                    account_id="manual-cash",
                    security_id="",
                    occurred_at=datetime.now().astimezone().isoformat(),
                    quantity="0",
                    cash_amount=self._decimal_text(delta),
                    currency="CNY",
                    action="set_cash_balance_adjustment",
                )
            )
        self.connection.execute("INSERT OR IGNORE INTO portfolios(portfolio_id) VALUES ('default')")
        self.connection.execute(
            "INSERT INTO portfolio_preferences VALUES ('default', ?, ?) "
            "ON CONFLICT(portfolio_id) DO UPDATE SET max_drawdown_percent = excluded.max_drawdown_percent, "
            "updated_at = excluded.updated_at",
            (self._decimal_text(threshold), datetime.now().astimezone().isoformat()),
        )
        self.connection.commit()
        return self.portfolio_profile()

    def _import(self, entries: Iterable[LedgerEntry]) -> dict[str, int]:
        inserted = skipped = 0
        for entry in entries:
            if self.append(entry):
                inserted += 1
            else:
                skipped += 1
        return {"inserted": inserted, "skipped": skipped}

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        return format(value.quantize(Decimal("0.01")) if value.as_tuple().exponent < -2 else value, "f")

    @staticmethod
    def _row_matches_entry(row: sqlite3.Row, entry: LedgerEntry) -> bool:
        return all(str(row[field]) == entry.as_dict()[field] for field in ENTRY_FIELDS)
