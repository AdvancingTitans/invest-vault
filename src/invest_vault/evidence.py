"""Immutable public-fact persistence and bounded archive jobs."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from .adapters import company_payload_to_snapshot, quote_payload_to_snapshot
from .contract import EvidenceSnapshot, raw_payload_hash, write_snapshot_manifest
from .ledger import Vault

JobStatus = str
RawPayload = tuple[str, object, str | None]
RefreshLoader = Callable[[], tuple[EvidenceSnapshot, Iterable[RawPayload]]]


@dataclass(frozen=True)
class RefreshJob:
    job_id: str
    security_id: str
    refresh_kind: str
    requested_as_of: str
    provider: str
    status: JobStatus
    snapshot_id: str | None
    error_provider: str | None
    error_detail: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


class EvidenceStore:
    """Keeps SQLite metadata and content-addressed raw files separately."""

    def __init__(self, vault: Vault, vault_directory: Path | None = None) -> None:
        self.vault = vault
        self.vault_directory = Path(vault_directory or vault.database_path.parent)
        self.raw_directory = self.vault_directory / "raw"
        self.snapshot_directory = self.vault_directory / "snapshots"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _canonical_payload(payload: object) -> bytes:
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _write_raw(self, payload: object) -> tuple[str, Path]:
        encoded = self._canonical_payload(payload)
        sha256 = hashlib.sha256(encoded).hexdigest()
        destination = self.raw_directory / f"{sha256}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            if destination.read_bytes() != encoded:
                raise ValueError("raw evidence hash collision with different content") from error
        else:
            with os.fdopen(descriptor, "wb") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
        return sha256, destination

    def persist_snapshot(self, snapshot: EvidenceSnapshot, raw_payloads: Iterable[RawPayload]) -> str:
        """Persist one immutable snapshot and its raw payloads, or reject conflicting reuse."""

        raw_paths: dict[str, Path] = {}
        raw_details: dict[str, tuple[str, str | None]] = {}
        for provider, payload, source_ref in raw_payloads:
            sha256, path = self._write_raw(payload)
            raw_paths[sha256] = path
            raw_details[sha256] = (provider, source_ref)
        for raw in snapshot.raw_manifest:
            if raw.sha256 not in raw_paths:
                raise ValueError(f"raw payload is missing for manifest hash {raw.sha256}")
            if raw_payload_hash(json.loads(raw_paths[raw.sha256].read_text(encoding="utf-8"))) != raw.sha256:
                raise ValueError("raw evidence content no longer matches manifest hash")

        snapshot_id = str(snapshot.snapshot_id)
        destination = self.snapshot_directory / f"{snapshot_id}.json"
        self.snapshot_directory.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing = json.loads(destination.read_text(encoding="utf-8"))["payload"]
            if existing != snapshot.model_dump(mode="json"):
                raise ValueError("snapshot ID already exists with different immutable content")
        else:
            write_snapshot_manifest(snapshot, destination)

        connection = self.vault.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                "SELECT manifest_path FROM evidence_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
            if existing is not None:
                if Path(existing["manifest_path"]) != destination:
                    raise ValueError("snapshot ID already exists with different manifest path")
                connection.commit()
                return snapshot_id
            availability = snapshot.availability
            connection.execute(
                """INSERT INTO evidence_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    snapshot_id,
                    snapshot.subject.canonical_id,
                    snapshot.requested_as_of.isoformat(),
                    snapshot.effective_as_of.isoformat(),
                    snapshot.observed_at.isoformat(),
                    snapshot.producer_version,
                    availability.state,
                    json.dumps(availability.missing_fields),
                    json.dumps(availability.reasons),
                    str(destination),
                ),
            )
            for index, item in enumerate(snapshot.items):
                connection.execute(
                    "INSERT INTO evidence_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        index,
                        item.kind,
                        json.dumps(item.value),
                        item.unit,
                        item.period_end.isoformat() if item.period_end else None,
                        item.provenance.provider,
                        item.provenance.source_ref,
                        item.availability.state,
                        json.dumps(item.availability.missing_fields),
                        json.dumps(item.validation),
                    ),
                )
            for raw in snapshot.raw_manifest:
                provider, source_ref = raw_details[raw.sha256]
                connection.execute(
                    "INSERT INTO raw_manifest_refs VALUES (?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        raw.sha256,
                        provider,
                        source_ref or raw.source_ref,
                        str(raw_paths[raw.sha256]),
                    ),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return snapshot_id

    def run_refresh(
        self,
        *,
        security_id: str,
        refresh_kind: str,
        requested_as_of: date,
        provider: str,
        loader: RefreshLoader,
    ) -> RefreshJob:
        """Run an explicitly requested single refresh; loaders are never called at startup."""

        job_id = str(uuid4())
        created_at = self._now()
        self.vault.connection.execute(
            "INSERT INTO refresh_jobs VALUES (?, ?, ?, ?, ?, 'queued', NULL, NULL, NULL, ?, NULL, NULL)",
            (job_id, security_id, refresh_kind, requested_as_of.isoformat(), provider, created_at),
        )
        self.vault.connection.execute(
            "UPDATE refresh_jobs SET status = 'running', started_at = ? WHERE job_id = ?",
            (self._now(), job_id),
        )
        self.vault.connection.commit()
        try:
            snapshot, raw_payloads = loader()
            if snapshot.subject.canonical_id != security_id:
                raise ValueError("refresh snapshot subject does not match the requested security")
            snapshot_id = self.persist_snapshot(snapshot, raw_payloads)
            status = "partial" if snapshot.availability.state == "partial" else "succeeded"
            completed_at = self._now()
            self.vault.connection.execute(
                """UPDATE refresh_jobs SET status = ?, snapshot_id = ?, completed_at = ? WHERE job_id = ?""",
                (status, snapshot_id, completed_at, job_id),
            )
            self._set_provider_health(provider, "available" if status == "succeeded" else "partial", None)
            self.vault.connection.commit()
        except Exception as error:
            completed_at = self._now()
            self.vault.connection.execute(
                """UPDATE refresh_jobs SET status = 'failed', error_provider = ?, error_detail = ?, completed_at = ?
                WHERE job_id = ?""",
                (provider, str(error), completed_at, job_id),
            )
            self._set_provider_health(provider, "unavailable", str(error))
            self.vault.connection.commit()
        return self.get_job(job_id)

    def _set_provider_health(self, provider: str, status: str, detail: str | None) -> None:
        now = self._now()
        if status in {"available", "partial"}:
            self.vault.connection.execute(
                """INSERT INTO provider_health(provider, status, last_success_at, last_failure_at, detail, updated_at)
                VALUES (?, ?, ?, NULL, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET status=excluded.status, last_success_at=excluded.last_success_at,
                detail=excluded.detail, updated_at=excluded.updated_at""",
                (provider, status, now, detail, now),
            )
        else:
            self.vault.connection.execute(
                """INSERT INTO provider_health(provider, status, last_success_at, last_failure_at, detail, updated_at)
                VALUES (?, ?, NULL, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET status=excluded.status, last_failure_at=excluded.last_failure_at,
                detail=excluded.detail, updated_at=excluded.updated_at""",
                (provider, status, now, detail, now),
            )

    def get_job(self, job_id: str) -> RefreshJob:
        row = self.vault.connection.execute(
            "SELECT * FROM refresh_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return RefreshJob(**dict(row))

    def data_quality(self, security_id: str) -> dict[str, object] | None:
        row = self.vault.connection.execute(
            "SELECT * FROM evidence_snapshots WHERE security_id = ? ORDER BY observed_at DESC LIMIT 1",
            (security_id,),
        ).fetchone()
        if row is None:
            return None
        source_count = self.vault.connection.execute(
            "SELECT COUNT(DISTINCT provider) FROM raw_manifest_refs WHERE snapshot_id = ?",
            (row["snapshot_id"],),
        ).fetchone()[0]
        return {
            "snapshot_id": row["snapshot_id"],
            "requested_as_of": row["requested_as_of"],
            "effective_as_of": row["effective_as_of"],
            "observed_at": row["observed_at"],
            "availability": row["availability_state"],
            "missing_fields": json.loads(row["missing_fields_json"]),
            "reasons": json.loads(row["reasons_json"]),
            "source_count": source_count,
        }

    def provider_health(self) -> list[dict[str, object]]:
        return [
            dict(row)
            for row in self.vault.connection.execute("SELECT * FROM provider_health ORDER BY provider")
        ]

    def latest_quote_payload(self, security_id: str) -> dict[str, object] | None:
        row = self.vault.connection.execute(
            """SELECT s.effective_as_of, r.raw_path
            FROM evidence_snapshots s JOIN raw_manifest_refs r ON r.snapshot_id = s.snapshot_id
            JOIN evidence_items i ON i.snapshot_id = s.snapshot_id
            WHERE s.security_id = ? AND i.kind = 'market.quote'
            ORDER BY s.effective_as_of DESC, s.observed_at DESC LIMIT 1""",
            (security_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(Path(row["raw_path"]).read_text(encoding="utf-8"))
        payload["trade_date"] = row["effective_as_of"]
        return payload

    def quote_payload_for_date(self, security_id: str, effective_as_of: str) -> dict[str, object] | None:
        row = self.vault.connection.execute(
            """SELECT s.effective_as_of, r.raw_path
            FROM evidence_snapshots s JOIN raw_manifest_refs r ON r.snapshot_id = s.snapshot_id
            JOIN evidence_items i ON i.snapshot_id = s.snapshot_id
            WHERE s.security_id = ? AND s.effective_as_of = ? AND i.kind = 'market.quote'
            ORDER BY s.observed_at DESC LIMIT 1""",
            (security_id, effective_as_of),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(Path(row["raw_path"]).read_text(encoding="utf-8"))
        payload["trade_date"] = row["effective_as_of"]
        return payload

    def has_quote_for_date(self, security_id: str, effective_as_of: date) -> bool:
        return self.vault.connection.execute(
            """SELECT 1 FROM evidence_snapshots s JOIN evidence_items i ON i.snapshot_id = s.snapshot_id
            WHERE s.security_id = ? AND s.effective_as_of = ? AND i.kind = 'market.quote' LIMIT 1""",
            (security_id, effective_as_of.isoformat()),
        ).fetchone() is not None

    def refresh_company_pack(
        self, pack: Mapping[str, object], *, requested_as_of: date, observed_at: str
    ) -> RefreshJob:
        """Persist one explicitly supplied A-share company-pack adapter result."""

        snapshot = company_payload_to_snapshot(
            pack, requested_as_of=requested_as_of.isoformat(), observed_at=observed_at
        )
        return self.run_refresh(
            security_id=snapshot.subject.canonical_id,
            refresh_kind="company",
            requested_as_of=requested_as_of,
            provider="supplied_company_payload",
            loader=lambda: (snapshot, [("supplied_company_payload", pack, None)]),
        )

    def refresh_quote_history(
        self, quote: Mapping[str, object], *, requested_as_of: date, observed_at: str
    ) -> RefreshJob:
        """Persist one explicitly supplied quote/history adapter result."""

        snapshot = quote_payload_to_snapshot(
            quote, requested_as_of=requested_as_of.isoformat(), observed_at=observed_at
        )
        return self.run_refresh(
            security_id=snapshot.subject.canonical_id,
            refresh_kind="quote",
            requested_as_of=requested_as_of,
            provider=str(quote.get("source") or "supplied_quote_payload"),
            loader=lambda: (snapshot, [(str(quote.get("source") or "supplied_quote_payload"), quote, None)]),
        )
