"""App-owned adapters for explicitly supplied public-source payloads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from .contract import (
    Availability,
    EvidenceItem,
    EvidenceSnapshot,
    InstrumentId,
    Provenance,
    RawManifestEntry,
    SourceEvent,
    raw_payload_hash,
)

PRODUCER_VERSION = "invest-vault@0.1.0"


def _date(value: str) -> date:
    normalized = value.replace("/", "-")
    if "-" not in normalized:
        normalized = f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:8]}"
    return date.fromisoformat(normalized)


def _instrument(symbol: str, market: str, asset_type: str = "stock") -> InstrumentId:
    normalized = {
        "a": "CN",
        "cn_market": "CN",
        "fund": "CN",
        "hk": "HK",
        "us": "US",
        "us_market": "US",
    }.get(market.lower(), market.upper())
    exchange = {"CN": "SSE" if symbol.startswith(("5", "6", "9")) else "SZSE", "HK": "HKEX", "US": "NASDAQ"}.get(
        normalized, "UNKNOWN"
    )
    return InstrumentId(market=normalized, exchange=exchange, symbol=symbol, asset_type=asset_type)


def _availability(available: bool, gaps: tuple[str, ...] = ()) -> Availability:
    gaps = tuple(str(gap) for gap in gaps if gap)
    if available and not gaps:
        return Availability(state="available")
    return Availability(state="partial" if available else "unavailable", missing_fields=gaps or ("value",))


def quote_payload_to_snapshot(
    quote: Mapping[str, Any], *, requested_as_of: str, observed_at: str
) -> EvidenceSnapshot:
    subject = _instrument(
        str(quote["symbol"]), str(quote.get("market") or "a"), str(quote.get("asset_type") or "stock")
    )
    effective = str(quote.get("trade_date") or quote.get("date") or requested_as_of)
    missing = tuple(field for field in ("price", "currency", "trade_date") if quote.get(field) in (None, ""))
    availability = _availability(quote.get("price") is not None, missing)
    provider = str(quote.get("source") or "supplied_payload")
    flags = tuple(str(flag) for flag in quote.get("quality_flags") or ())
    status = "partial" if "nearest_available_kline" in flags else availability.state
    return EvidenceSnapshot(
        subject=subject,
        requested_as_of=_date(requested_as_of),
        effective_as_of=_date(effective),
        observed_at=datetime.fromisoformat(observed_at),
        producer_version=PRODUCER_VERSION,
        items=(
            EvidenceItem(
                kind="market.quote",
                instrument_id=subject,
                value=quote.get("price"),
                unit=quote.get("currency"),
                period_end=_date(effective),
                provenance=Provenance(
                    provider=provider,
                    source_ref=str(quote.get("source_ref") or provider),
                    source_chain=tuple(str(item) for item in quote.get("source_chain") or ()),
                ),
                availability=availability,
                validation=flags,
            ),
        ),
        source_events=(SourceEvent(provider=provider, status=status, detail=quote.get("fallback_reason")),),
        availability=availability,
        raw_manifest=(RawManifestEntry(sha256=raw_payload_hash(quote), provider=provider),),
    )


def company_payload_to_snapshot(
    pack: Mapping[str, Any], *, requested_as_of: str, observed_at: str
) -> EvidenceSnapshot:
    subject = _instrument(str(pack["symbol"]), str(pack.get("market") or "a"))
    facts = list(pack.get("financial_facts") or ())
    quote = dict(pack.get("quote") or {})
    items = tuple(
        EvidenceItem(
            kind=f"company.financial.{fact['metric']}",
            instrument_id=subject,
            value=fact.get("value"),
            unit=fact.get("currency"),
            period_end=_date(str(fact["period"])) if fact.get("period") and fact["period"] != "unknown" else None,
            provenance=Provenance(
                provider=str(fact.get("source") or "supplied_payload"),
                source_ref=str(fact.get("source_type") or "financial_fact"),
            ),
            availability=_availability(fact.get("value") is not None),
        )
        for fact in facts
    )
    quote_item = EvidenceItem(
        kind="market.quote",
        instrument_id=subject,
        value=quote.get("value"),
        unit=quote.get("currency"),
        period_end=_date(str(quote.get("period") or requested_as_of)),
        provenance=Provenance(
            provider=str(quote.get("source") or "supplied_payload"),
            source_ref=str(quote.get("source_type") or "quote"),
        ),
        availability=_availability(quote.get("value") is not None),
    )
    gaps = tuple(
        f"{module}:{gap}"
        for module, detail in dict(pack.get("modules") or {}).items()
        for gap in detail.get("gaps") or ()
    )
    available = any(detail.get("available") for detail in dict(pack.get("modules") or {}).values())
    provider = "supplied_company_payload"
    return EvidenceSnapshot(
        subject=subject,
        requested_as_of=_date(requested_as_of),
        effective_as_of=_date(str(quote.get("period") or pack.get("trade_date") or requested_as_of)),
        observed_at=datetime.fromisoformat(observed_at),
        producer_version=PRODUCER_VERSION,
        items=(*items, quote_item),
        source_events=(SourceEvent(provider=provider, status="partial" if gaps else "available"),),
        availability=_availability(available, gaps),
        raw_manifest=(RawManifestEntry(sha256=raw_payload_hash(pack), provider=provider),),
    )
