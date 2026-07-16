"""Provider-neutral immutable evidence owned by Invest Vault."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "2.0"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InstrumentId(FrozenModel):
    market: str = Field(min_length=2, max_length=16)
    exchange: str = Field(min_length=2, max_length=16)
    symbol: str = Field(min_length=1, max_length=64)
    asset_type: str = Field(min_length=1, max_length=32)

    @property
    def canonical_id(self) -> str:
        return ":".join((self.market.upper(), self.exchange.upper(), self.symbol.upper(), self.asset_type.upper()))


class Availability(FrozenModel):
    state: Literal["available", "partial", "unavailable"]
    missing_fields: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_state(self) -> Availability:
        if self.state == "available" and (self.missing_fields or self.reasons):
            raise ValueError("available evidence cannot declare gaps")
        if self.state == "unavailable" and not (self.missing_fields or self.reasons):
            raise ValueError("unavailable evidence must declare a gap")
        return self


class Provenance(FrozenModel):
    provider: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    source_chain: tuple[str, ...] = ()


class EvidenceItem(FrozenModel):
    kind: str = Field(min_length=1)
    instrument_id: InstrumentId
    value: str | int | float | bool | None
    unit: str | None = None
    period_end: date | None = None
    provenance: Provenance
    availability: Availability
    validation: tuple[str, ...] = ()


class SourceEvent(FrozenModel):
    provider: str = Field(min_length=1)
    status: Literal["available", "partial", "unavailable"]
    source_ref: str | None = None
    detail: str | None = None


class RawManifestEntry(FrozenModel):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider: str = Field(min_length=1)
    source_ref: str | None = None


class EvidenceSnapshot(FrozenModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    snapshot_id: UUID = Field(default_factory=uuid4)
    subject: InstrumentId
    requested_as_of: date
    effective_as_of: date
    observed_at: datetime
    producer_version: str = Field(min_length=1)
    items: tuple[EvidenceItem, ...]
    source_events: tuple[SourceEvent, ...]
    availability: Availability
    raw_manifest: tuple[RawManifestEntry, ...]

    @field_validator("observed_at")
    @classmethod
    def observed_at_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone offset")
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> EvidenceSnapshot:
        if self.effective_as_of > self.requested_as_of:
            raise ValueError("effective_as_of cannot be after requested_as_of")
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def raw_payload_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def write_snapshot_manifest(snapshot: EvidenceSnapshot, destination: Path) -> Path:
    payload = snapshot.canonical_json().encode()
    manifest = json.dumps(
        {
            "manifest_schema_version": "1.0",
            "payload": json.loads(payload),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(manifest)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination
