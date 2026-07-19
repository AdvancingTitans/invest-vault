"""Loopback-only HTTP surface for the local holding notebook."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .ai import (
    AIProvider,
    AIQuickNoteStore,
    AISettingsStore,
    AIUnavailableError,
    CodexAppServerProvider,
    MultiProviderAIProvider,
    ResearchChatStore,
)
from .ai_providers import PROVIDER_CATALOG, EncryptedCredentialStore
from .ai_roles import AI_ROLES, get_role
from .ai_skills import ResearchSkillLayer
from .evidence import EvidenceStore
from .exports import create_backup, export_holdings_xlsx, export_markdown
from .ledger import HoldingRecord, LedgerEntry, Vault, VaultSettings
from .providers import (
    current_market_date,
    fetch_company_announcements,
    fetch_financial_snapshot,
    fetch_fund_snapshot,
    fetch_global_index_overview,
    fetch_hkex_announcements,
    fetch_industry_money_flow,
    fetch_lhb,
    fetch_market_news,
    fetch_market_pulse,
    fetch_public_quote,
    fetch_security_historical_close,
    fetch_security_live_quote,
    market_report_stage,
    market_session_metadata,
    previous_trade_date,
    target_trade_date,
)
from .research import ResearchStore
from .runtime import diagnostics


def web_dist_directory() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root) / "web" / "dist" if frozen_root else Path(__file__).parents[2] / "web" / "dist"


class RefreshPayload(BaseModel):
    kind: Literal["company", "quote"]
    requested_as_of: date
    payload: dict[str, Any] = Field(default_factory=dict)


class MarketRefreshPayload(BaseModel):
    section: Literal["all", "indices", "lhb", "industry_flow", "pulse", "market_news"] = "all"


class LedgerImportPayload(BaseModel):
    entries: list[dict[str, str]] = Field(default_factory=list, max_length=10_000)


class HoldingRowPayload(BaseModel):
    row_id: str = Field(min_length=1, max_length=100)
    symbol: str = Field(min_length=1, max_length=16)
    asset_type: Literal["a_share", "hk_stock", "fund"]
    invested_amount_cny: str = Field(min_length=1, max_length=40)
    bought_on: date


class HoldingRowsPayload(BaseModel):
    rows: list[HoldingRowPayload] = Field(min_length=1, max_length=100)


class PortfolioRiskProfilePayload(BaseModel):
    cash_balance_cny: str = Field(min_length=1, max_length=40)
    max_drawdown_percent: str = Field(min_length=1, max_length=20)


class NotePayload(BaseModel):
    security_id: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=100_000)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    market_session: Literal["盘前", "盘中", "盘后"] | None = None


class ThesisPayload(NotePayload):
    thesis_id: str | None = None
    review_due_on: date | None = None


class MaterialPayload(BaseModel):
    security_id: str = Field(min_length=1, max_length=128)
    material_type: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=500)
    published_at: date
    source_name: str = Field(min_length=1, max_length=100)
    source_url: str = Field(min_length=1, max_length=2_000)
    excerpt: str = Field(default="", max_length=10_000)


class MaterialNotePayload(NotePayload):
    material_id: str = Field(min_length=1, max_length=128)
    quoted_text: str = Field(min_length=1, max_length=10_000)


class QuickNotePayload(BaseModel):
    security_id: str = Field(min_length=1, max_length=128)
    raw_text: str = Field(min_length=1, max_length=20_000)


class AcceptQuickNotePayload(BaseModel):
    body: str = Field(min_length=1, max_length=100_000)


class CreateChatPayload(BaseModel):
    security_id: str = Field(min_length=1, max_length=128)
    role_id: str = Field(default="general", min_length=1, max_length=64)
    mode: str = Field(default="assistant", pattern="^(assistant|committee)$")
    title: str = Field(min_length=1, max_length=200)


class ChatMessagePayload(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    role_id: str = Field(default="general", min_length=1, max_length=64)


class AIModelSettingPayload(BaseModel):
    provider_id: str | None = Field(default=None, max_length=32)
    model_id: str | None = Field(default=None, max_length=200)
    reasoning_effort: str | None = Field(default=None, max_length=20)


class AICredentialPayload(BaseModel):
    key: str = Field(min_length=1, max_length=1_000)


def _holding_record(payload: HoldingRowPayload) -> HoldingRecord:
    symbol = payload.symbol.strip().upper()
    if payload.asset_type in {"a_share", "fund"}:
        if not re.fullmatch(r"\d{6}", symbol):
            raise ValueError("A股和基金代码必须是6位数字")
        exchange = "SSE" if symbol.startswith(("5", "6", "9")) else "SZSE"
        instrument = "FUND" if payload.asset_type == "fund" else "STOCK"
        security_id = f"CN:{exchange}:{symbol}:{instrument}"
    elif payload.asset_type == "hk_stock":
        if not re.fullmatch(r"\d{1,5}", symbol):
            raise ValueError("港股代码必须是1至5位数字")
        security_id = f"HK:HKEX:{symbol.zfill(5)}:STOCK"
    else:
        raise ValueError("该证券类型暂未通过历史数据稳定性验证")
    try:
        amount = Decimal(payload.invested_amount_cny.replace(",", ""))
    except (InvalidOperation, AttributeError) as error:
        raise ValueError("买入金额必须是人民币数字") from error
    if amount <= 0:
        raise ValueError("买入金额必须大于0")
    if payload.bought_on > date.today():
        raise ValueError("买入日期不能晚于今天")
    return HoldingRecord(
        record_id=f"holding-{payload.row_id}",
        security_id=security_id,
        asset_type=payload.asset_type,
        invested_amount_cny=format(amount, "f"),
        bought_on=payload.bought_on.isoformat(),
    )


def _bootstrap(
    vault: Vault,
    evidence: EvidenceStore,
    research: ResearchStore,
    *,
    archive_target: date | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    account_rows = vault.connection.execute("SELECT account_id FROM accounts ORDER BY account_id").fetchall()
    positions = {
        position.security_id: position
        for account in account_rows
        for position in vault.project_positions(account["account_id"])
    }
    saved = {item["security_id"]: item for item in vault.holding_summaries()}
    fund_names = {
        str(row["security_id"]): str(json.loads(str(row["payload_json"])).get("name") or "")
        for row in vault.connection.execute(
            """SELECT f.* FROM fund_snapshots f JOIN (
                SELECT security_id, MAX(cutoff_date) cutoff_date FROM fund_snapshots GROUP BY security_id
            ) latest ON latest.security_id = f.security_id AND latest.cutoff_date = f.cutoff_date"""
        )
    }
    holding_entries = []
    for item in vault.holding_entries():
        live_row = vault.connection.execute(
            "SELECT payload_json FROM market_snapshots WHERE section = ? ORDER BY observed_at DESC LIMIT 1",
            (f"live_quote:{item['security_id']}",),
        ).fetchone()
        current_quote = (
            json.loads(str(live_row["payload_json"]))
            if live_row
            else evidence.latest_quote_payload(item["security_id"]) or {}
        )
        purchase_quote = evidence.quote_payload_for_date(item["security_id"], item["bought_on"]) or {}
        entry_profit: float | None = None
        entry_profit_percent: float | None = None
        if current_quote.get("price") is not None and purchase_quote.get("price") is not None:
            return_ratio = Decimal(str(current_quote["price"])) / Decimal(str(purchase_quote["price"])) - 1
            entry_profit = round(float(Decimal(item["invested_amount_cny"]) * return_ratio), 2)
            entry_profit_percent = round(float(return_ratio * 100), 2)
        holding_entries.append(
            {
                **item,
                "estimated_profit_cny": entry_profit,
                "estimated_profit_percent": entry_profit_percent,
                "profit_reason": None
                if entry_profit is not None
                else (
                    "最近完整交易日收盘价不可用"
                    if current_quote.get("price") is None
                    else "买入日无可核验收盘价"
                ),
            }
        )
    holdings: list[dict[str, object]] = []
    for security_id in sorted(set(positions) | set(saved)):
        position = positions.get(security_id)
        record = saved.get(security_id, {})
        live_row = vault.connection.execute(
            "SELECT payload_json FROM market_snapshots WHERE section = ? ORDER BY observed_at DESC LIMIT 1",
            (f"live_quote:{security_id}",),
        ).fetchone()
        quote = (
            json.loads(str(live_row["payload_json"]))
            if live_row
            else evidence.latest_quote_payload(security_id) or {}
        )
        records = [item for item in holding_entries if item["security_id"] == security_id]
        estimated_profit: float | None = None
        estimated_profit_percent: float | None = None
        profit_reason = ""
        if records and quote.get("price") is not None:
            purchase_quotes = [
                evidence.quote_payload_for_date(security_id, item["bought_on"]) for item in records
            ]
            if all(item and item.get("price") is not None for item in purchase_quotes):
                invested_total = sum(Decimal(item["invested_amount_cny"]) for item in records)
                estimated_value = sum(
                    Decimal(item["invested_amount_cny"])
                    * Decimal(str(quote["price"]))
                    / Decimal(str(purchase_quote["price"]))
                    for item, purchase_quote in zip(records, purchase_quotes)
                    if purchase_quote
                )
                estimated_profit = round(float(estimated_value - invested_total), 2)
                estimated_profit_percent = round(float((estimated_value / invested_total - 1) * 100), 2)
            else:
                profit_reason = "买入日无可核验收盘价"
        elif records:
            profit_reason = "最近完整交易日收盘价不可用"
        symbol = security_id.split(":")[2]
        region = security_id.split(":")[0]
        holdings.append(
            {
                "security_id": security_id,
                "name": fund_names.get(security_id) or quote.get("name") or symbol,
                "symbol": symbol,
                "asset_type": record.get("asset_type")
                or ("fund" if security_id.endswith(":FUND") else "a_share"),
                "invested_amount_cny": record.get("invested_amount_cny"),
                "bought_on": record.get("bought_on"),
                "quantity": position.quantity if position else None,
                "price": quote.get("price"),
                "previous_close": quote.get("previous_close"),
                "change_percent": quote.get("change_percent"),
                "amount": quote.get("amount"),
                "turnover": quote.get("turnover"),
                "pe_ttm": quote.get("pe_ttm"),
                "pb": quote.get("pb"),
                "market_cap_100m": quote.get("market_cap_100m"),
                "currency": quote.get("currency") or ("HKD" if region == "HK" else "CNY"),
                "trade_date": quote.get("trade_date"),
                "data_session": quote.get("data_session") or "盘后",
                "data_label": quote.get("data_label")
                or (
                    f"{str(quote.get('trade_date') or '')[5:7]}月{str(quote.get('trade_date') or '')[8:10]}日盘后收盘数据"
                    if quote.get("trade_date")
                    else "等待行情数据"
                ),
                "valuation_status": (
                    "最新行情"
                    if quote.get("price") is not None
                    else "该市场行情暂未接入"
                    if region != "CN"
                    else "尚无行情数据"
                ),
                "source": quote.get("source") or "local_holding",
                "estimated_profit_cny": estimated_profit,
                "estimated_profit_percent": estimated_profit_percent,
                "profit_basis": "按买入日收盘价估算" if estimated_profit is not None else None,
                "profit_reason": profit_reason or None,
            }
        )
    selected = holdings[0]["security_id"] if holdings else None
    thesis = research.current_thesis(str(selected)) if selected else None
    archived_dates = {
        item["security_id"]: str(quote["trade_date"])
        for item in holdings
        if (quote := evidence.latest_quote_payload(str(item["security_id"]))) and quote.get("trade_date")
    }
    report_as_of = (
        archive_target.isoformat()
        if archive_target
        else min(archived_dates.values())
        if archived_dates
        else None
    )
    current_count = sum(value == report_as_of for value in archived_dates.values()) if report_as_of else 0
    market = _latest_market_snapshots(vault)
    market["report_stage"] = market_report_stage(now)
    return {
        "mode": "vault",
        "report_as_of": report_as_of,
        "archive_coverage": {
            "target_date": report_as_of,
            "current": current_count,
            "total": len(holdings),
            "stale": len(holdings) - current_count,
        },
        "refreshed_at": max(
            (
                str(row["observed_at"])
                for row in vault.connection.execute("SELECT observed_at FROM evidence_snapshots")
            ),
            default=None,
        ),
        "disclaimer": "盈亏按每笔买入日收盘价与最近完整交易日收盘价估算，不代表实际成交成本。",
        "holdings": holdings,
        "holding_entries": holding_entries,
        "portfolio_profile": vault.portfolio_profile(),
        "market": market,
        "stable_facts": [],
        "optional_facts": [],
        "research": {
            "thesis": thesis.body if thesis else None,
            "review_due_on": thesis.review_due_on if thesis else None,
        },
    }


def _latest_market_snapshots(vault: Vault) -> dict[str, object]:
    result: dict[str, object] = {}
    for row in vault.connection.execute(
        """SELECT m.* FROM market_snapshots m JOIN (
            SELECT section, MAX(trade_date) AS trade_date FROM market_snapshots
            WHERE section NOT LIKE 'live_quote:%' GROUP BY section
        ) latest ON latest.section = m.section AND latest.trade_date = m.trade_date"""
    ):
        result[str(row["section"])] = json.loads(str(row["payload_json"]))
    return result


def create_app(
    vault_directory: Path,
    *,
    automatic_updates: bool = True,
    now_provider: Callable[[], datetime] | None = None,
    ai_provider: AIProvider | None = None,
    research_skill_layer: ResearchSkillLayer | None = None,
) -> FastAPI:
    """Create an API bound by the caller to loopback only."""

    VaultSettings()  # Explicitly enforce the local-first bind policy at the service boundary.
    vault = Vault(vault_directory / "vault.sqlite3")
    evidence = EvidenceStore(vault, vault_directory)
    research = ResearchStore(vault, vault_directory)
    credentials = EncryptedCredentialStore(vault, vault_directory / "ai-master.key")
    if ai_provider is None:
        provider = MultiProviderAIProvider(
            CodexAppServerProvider(vault_directory / "ai-runtime"), credentials
        )
    else:
        provider = ai_provider
    ai_settings = AISettingsStore(vault, provider)
    quick_notes = AIQuickNoteStore(vault, research)
    chats = ResearchChatStore(vault, provider, research_skill_layer)
    clock = now_provider or (lambda: datetime.now(timezone.utc))

    def market_holding_subjects() -> list[dict[str, str]]:
        subjects: list[dict[str, str]] = []
        for holding in vault.holding_summaries():
            security_id = str(holding["security_id"])
            symbol = security_id.split(":")[2]
            quote = evidence.latest_quote_payload(security_id) or {}
            fund_row = vault.connection.execute(
                "SELECT payload_json FROM fund_snapshots WHERE security_id = ? ORDER BY cutoff_date DESC LIMIT 1",
                (security_id,),
            ).fetchone()
            fund = json.loads(str(fund_row["payload_json"])) if fund_row else {}
            subjects.append({"symbol": symbol, "name": str(fund.get("name") or quote.get("name") or symbol)})
        return subjects

    def refresh_market_sections(selected: list[str]) -> tuple[list[str], dict[str, str]]:
        market_target = current_market_date(clock())
        report_stage = market_report_stage(clock())
        loaders: dict[str, Callable[[], dict[str, object]]] = {
            "indices": fetch_global_index_overview,
            "lhb": lambda: fetch_lhb(market_target),
            "industry_flow": lambda: fetch_industry_money_flow(market_target),
            "market_news": lambda: fetch_market_news(now=clock()),
            "pulse": lambda: fetch_market_pulse(
                date.fromisoformat(str(report_stage["report_date"])),
                session=str(report_stage["session"]),
                holdings=market_holding_subjects(),
                now=clock(),
            ),
        }
        completed: list[str] = []
        failed: dict[str, str] = {}
        for section in selected:
            try:
                payload = loaders[section]()
                snapshot_date = str(payload.get("date") or market_target.isoformat())
                vault.connection.execute(
                    "INSERT OR REPLACE INTO market_snapshots VALUES (?, ?, ?, ?, ?)",
                    (
                        section,
                        snapshot_date,
                        str(payload.get("source") or "公开行情"),
                        json.dumps(payload, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                vault.connection.commit()
                completed.append(section)
            except Exception as error:
                failed[section] = str(error)
        return completed, failed

    def archive_completed_closes(
        *,
        force: bool = False,
        refresh_market: bool = True,
        refresh_quotes: bool = True,
        refresh_research: bool = True,
    ) -> date | None:
        if not automatic_updates and not force:
            return None
        target = target_trade_date(clock())
        # ponytail: prevent duplicate requests inside one bootstrap while allowing a
        # later automatic check to retry a provider that was not ready yet.
        attempted_quotes: set[tuple[str, date]] = set()
        accounts = vault.connection.execute("SELECT account_id FROM accounts").fetchall()
        security_ids = {
            position.security_id
            for account in accounts
            for position in vault.project_positions(account["account_id"])
        } | {item["security_id"] for item in vault.holding_summaries()}
        for security_id in security_ids:
            if not security_id.startswith(("CN:", "HK:")):
                continue
            symbol, instrument = security_id.split(":")[2], security_id.split(":")[3]
            if refresh_quotes:
                try:
                    if security_id.startswith("CN:"):
                        live_quote = fetch_public_quote(symbol)
                        market = "CN"
                    elif security_id.startswith("HK:") and instrument == "STOCK":
                        live_quote = fetch_security_live_quote(security_id)
                        market = "HK"
                    else:
                        live_quote = None
                        market = "CN"
                    if live_quote and live_quote.get("price") is not None and live_quote.get("trade_date"):
                        session = market_session_metadata(market, str(live_quote["trade_date"]), clock())
                        live_quote = {
                            **live_quote,
                            "data_session": session["session"],
                            "data_label": session["label"],
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                        }
                        vault.connection.execute(
                            "INSERT OR REPLACE INTO market_snapshots VALUES (?, ?, ?, ?, ?)",
                            (
                                f"live_quote:{security_id}",
                                str(live_quote["trade_date"]),
                                str(live_quote.get("source") or "公开行情"),
                                json.dumps(live_quote, ensure_ascii=False),
                                datetime.now(timezone.utc).isoformat(),
                            ),
                        )
                        vault.connection.commit()
                except Exception:
                    pass
            attempt_key = (security_id, target)
            if (
                refresh_quotes
                and not evidence.has_quote_for_date(security_id, target)
                and attempt_key not in attempted_quotes
            ):
                attempted_quotes.add(attempt_key)
                try:
                    quote = fetch_security_historical_close(security_id, target)
                except Exception:
                    fallback = previous_trade_date(target)
                    if evidence.latest_quote_payload(security_id) is None:
                        try:
                            quote = fetch_security_historical_close(security_id, fallback)
                        except Exception:
                            quote = None
                        if quote is not None:
                            evidence.refresh_quote_history(
                                quote,
                                requested_as_of=fallback,
                                observed_at=datetime.now(timezone.utc).isoformat(),
                            )
                else:
                    evidence.refresh_quote_history(
                        quote,
                        requested_as_of=target,
                        observed_at=datetime.now(timezone.utc).isoformat(),
                    )
            for holding in (
                item
                for item in vault.holding_entries()
                if refresh_quotes and item["security_id"] == security_id
            ):
                bought_on = date.fromisoformat(holding["bought_on"])
                purchase_key = (security_id, bought_on)
                if (
                    not evidence.has_quote_for_date(security_id, bought_on)
                    and purchase_key not in attempted_quotes
                ):
                    attempted_quotes.add(purchase_key)
                    try:
                        purchase_quote = fetch_security_historical_close(security_id, bought_on)
                    except Exception:
                        continue
                    evidence.refresh_quote_history(
                        purchase_quote,
                        requested_as_of=bought_on,
                        observed_at=datetime.now(timezone.utc).isoformat(),
                    )
            if refresh_research and security_id.startswith("CN:") and instrument == "STOCK":
                existing_financials = vault.connection.execute(
                    "SELECT 1 FROM financial_snapshots WHERE security_id = ? AND cutoff_date = ?",
                    (security_id, target.isoformat()),
                ).fetchone()
                if existing_financials is None:
                    try:
                        financials = fetch_financial_snapshot(symbol, target)
                    except Exception:
                        financials = None
                    if financials is not None:
                        vault.connection.execute(
                            "INSERT OR IGNORE INTO financial_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                str(uuid4()),
                                security_id,
                                target.isoformat(),
                                financials["source"],
                                json.dumps(financials, ensure_ascii=False),
                                datetime.now(timezone.utc).isoformat(),
                            ),
                        )
                        vault.connection.commit()
            if refresh_research and security_id.startswith("CN:") and instrument == "FUND":
                existing_fund = vault.connection.execute(
                    "SELECT 1 FROM fund_snapshots WHERE security_id = ? AND cutoff_date = ?",
                    (security_id, target.isoformat()),
                ).fetchone()
                if existing_fund is None:
                    try:
                        fund = fetch_fund_snapshot(symbol, target)
                    except Exception:
                        fund = None
                    if fund is not None:
                        vault.connection.execute(
                            "INSERT OR IGNORE INTO fund_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                str(uuid4()),
                                security_id,
                                target.isoformat(),
                                fund["source"],
                                json.dumps(fund, ensure_ascii=False),
                                datetime.now(timezone.utc).isoformat(),
                            ),
                        )
                        vault.connection.commit()
            if (
                refresh_research
                and security_id.startswith(("CN:", "HK:"))
                and instrument == "STOCK"
                and not research.materials_synced(security_id, target)
            ):
                try:
                    materials = (
                        fetch_company_announcements(symbol, target)
                        if security_id.startswith("CN:")
                        else fetch_hkex_announcements(symbol, target)
                    )
                except Exception:
                    continue
                for material in materials:
                    research.add_material(
                        security_id=security_id,
                        material_type=material["material_type"],
                        title=material["title"],
                        published_at=date.fromisoformat(material["published_at"]),
                        source_name=material["source_name"],
                        source_url=material["source_url"],
                        excerpt=material["excerpt"],
                    )
                research.mark_materials_synced(security_id, target)

        if refresh_market:
            refresh_market_sections(["indices", "lhb", "industry_flow", "pulse", "market_news"])
        return target

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        provider.close()
        chats.close()
        with vault.lock:
            vault.close()

    app = FastAPI(title="投资札记", version="0.3.29", lifespan=lifespan)

    @app.get("/api/data-quality/{security_id:path}")
    def data_quality(security_id: str) -> dict[str, object]:
        with vault.lock:
            quality = evidence.data_quality(security_id)
        if quality is None:
            raise HTTPException(status_code=404, detail="No persisted evidence for this security")
        return quality

    @app.get("/api/provider-health")
    def provider_health() -> list[dict[str, object]]:
        with vault.lock:
            return evidence.provider_health()

    @app.get("/api/security/{security_id:path}/timeline")
    def timeline(security_id: str, limit: int = 50, cursor: str | None = None) -> dict[str, object]:
        with vault.lock:
            return research.timeline(security_id, limit=limit, cursor=cursor)

    @app.get("/api/bootstrap")
    def bootstrap(refresh: bool = True) -> dict[str, object]:
        with vault.lock:
            archive_target = archive_completed_closes() if refresh else target_trade_date(clock())
            return _bootstrap(vault, evidence, research, archive_target=archive_target, now=clock())

    @app.post("/api/holdings/refresh")
    def refresh_holdings(scope: Literal["all", "quotes", "materials"] = "all") -> dict[str, object]:
        with vault.lock:
            archive_target = archive_completed_closes(
                force=True,
                refresh_market=False,
                refresh_quotes=scope != "materials",
                refresh_research=scope != "quotes",
            )
        return {
            "refreshed": True,
            "scope": scope,
            "target_date": archive_target.isoformat() if archive_target else None,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }

    @app.post("/api/ledger/import")
    def import_ledger(payload: LedgerImportPayload) -> dict[str, int]:
        try:
            entries = [LedgerEntry(**entry) for entry in payload.entries]
            with vault.lock:
                return vault.import_json(entry.as_dict() for entry in entries)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/holdings")
    def add_holdings(payload: HoldingRowsPayload) -> dict[str, int]:
        try:
            records = [_holding_record(row) for row in payload.rows]
            with vault.lock:
                return vault.import_holdings(records)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/portfolio/risk-profile")
    def save_portfolio_risk_profile(
        payload: PortfolioRiskProfilePayload,
    ) -> dict[str, str | None]:
        try:
            with vault.lock:
                return vault.set_portfolio_profile(
                    cash_balance_cny=payload.cash_balance_cny,
                    max_drawdown_percent=payload.max_drawdown_percent,
                )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.put("/api/holdings/{holding_id}")
    def revise_holding(holding_id: str, payload: HoldingRowPayload) -> dict[str, str]:
        try:
            with vault.lock:
                return vault.revise_holding(holding_id, _holding_record(payload))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.delete("/api/holdings/{holding_id}")
    def delete_holding(holding_id: str) -> dict[str, bool]:
        try:
            with vault.lock:
                vault.delete_holding(holding_id)
            return {"deleted": True}
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/holdings/export.xlsx")
    def export_holdings_excel() -> Response:
        with vault.lock:
            content = export_holdings_xlsx(vault)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="holdings.xlsx"'},
        )

    @app.get("/api/ledger/export")
    def export_ledger() -> JSONResponse:
        with vault.lock:
            return JSONResponse(
                vault.export_json(), headers={"Content-Disposition": 'attachment; filename="ledger.json"'}
            )

    @app.post("/api/research/notes")
    def add_note(payload: NotePayload) -> dict[str, str]:
        with vault.lock:
            return {
                "note_id": research.add_note(
                    security_id=payload.security_id,
                    body=payload.body,
                    title=payload.title,
                    market_session=payload.market_session,
                )
            }

    @app.put("/api/research/notes/{note_id}")
    def revise_note(note_id: str, payload: NotePayload) -> dict[str, str]:
        try:
            with vault.lock:
                return {
                    "revision_id": research.revise_note(
                        note_id, security_id=payload.security_id, body=payload.body
                    )
                }
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.delete("/api/research/notes/{note_id}")
    def delete_note(note_id: str) -> dict[str, bool]:
        try:
            with vault.lock:
                research.delete_note(note_id)
            return {"deleted": True}
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/research/materials")
    def add_material(payload: MaterialPayload) -> dict[str, str]:
        try:
            with vault.lock:
                return {
                    "material_id": research.add_material(
                        security_id=payload.security_id,
                        material_type=payload.material_type,
                        title=payload.title,
                        published_at=payload.published_at,
                        source_name=payload.source_name,
                        source_url=payload.source_url,
                        excerpt=payload.excerpt,
                    )
                }
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/research/notes/from-material")
    def add_note_from_material(payload: MaterialNotePayload) -> dict[str, str]:
        try:
            with vault.lock:
                return {
                    "note_id": research.add_note_from_material(
                        security_id=payload.security_id,
                        material_id=payload.material_id,
                        quoted_text=payload.quoted_text,
                        body=payload.body,
                    )
                }
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/research/{security_id:path}")
    def research_workspace(security_id: str) -> dict[str, object]:
        with vault.lock:
            return research.workspace(security_id)

    @app.post("/api/research/theses")
    def revise_thesis(payload: ThesisPayload) -> dict[str, object]:
        with vault.lock:
            revision = research.revise_thesis(
                security_id=payload.security_id,
                body=payload.body,
                thesis_id=payload.thesis_id,
                review_due_on=payload.review_due_on,
            )
            return revision.__dict__

    @app.delete("/api/research/theses/{thesis_id}")
    def delete_thesis(thesis_id: str) -> dict[str, bool]:
        try:
            with vault.lock:
                research.delete_thesis(thesis_id)
            return {"deleted": True}
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/exports/research.md")
    def research_export() -> FileResponse:
        destination = vault_directory / "exports" / "invest-vault.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with vault.lock:
            export_markdown(vault, destination, data_cutoff=datetime.now(timezone.utc).isoformat())
        return FileResponse(destination, filename="invest-vault.md")

    @app.get("/api/exports/backup.zip")
    def backup_export() -> FileResponse:
        destination = vault_directory / "exports" / "invest-vault-backup.zip"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with vault.lock:
            create_backup(vault_directory, destination)
        return FileResponse(destination, filename="invest-vault-backup.zip")

    @app.get("/api/diagnostics")
    def local_diagnostics() -> dict[str, object]:
        return diagnostics(vault_directory)

    @app.get("/api/ai/status")
    def ai_status() -> dict[str, object]:
        return provider.status()

    @app.post("/api/ai/login/chatgpt")
    def ai_login_chatgpt() -> dict[str, object]:
        try:
            return provider.start_chatgpt_login()
        except AIUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/ai/logout")
    def ai_logout() -> dict[str, object]:
        try:
            return provider.logout()
        except AIUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/ai/models")
    def ai_models() -> list[dict[str, object]]:
        try:
            return provider.list_models()
        except AIUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/ai/providers")
    def ai_providers() -> dict[str, object]:
        configured = credentials.list_configured()
        return {
            "providers": [
                {
                    "provider_id": provider_id,
                    "name": value["name"],
                    "auth_kind": value["auth_kind"],
                    "models": value["models"],
                    # Codex authentication is reported by /api/ai/status. Keeping the
                    # static catalog independent prevents a slow CLI startup from
                    # hiding every BYOK option in Settings.
                    "configured": provider_id == "codex" or provider_id in configured,
                    "masked": configured.get(provider_id, {}).get("masked"),
                    "updated_at": configured.get(provider_id, {}).get("updated_at"),
                }
                for provider_id, value in PROVIDER_CATALOG.items()
            ]
        }

    @app.put("/api/ai/providers/{provider_id}/credential")
    def put_ai_credential(provider_id: str, payload: AICredentialPayload) -> dict[str, object]:
        try:
            return credentials.set_api_key(provider_id, payload.key)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.delete("/api/ai/providers/{provider_id}/credential")
    def delete_ai_credential(provider_id: str) -> dict[str, object]:
        try:
            credentials.delete_api_key(provider_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"provider_id": provider_id, "deleted": True}

    @app.get("/api/ai/settings")
    def get_ai_settings() -> dict[str, object]:
        return ai_settings.get()

    @app.put("/api/ai/settings/models/{task}")
    def put_ai_model_setting(task: str, payload: AIModelSettingPayload) -> dict[str, object]:
        try:
            return ai_settings.put(
                task,
                provider_id=payload.provider_id,
                model_id=payload.model_id,
                reasoning_effort=payload.reasoning_effort,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/ai/quick-notes")
    def create_quick_note(payload: QuickNotePayload) -> dict[str, object]:
        try:
            draft = provider.quick_note(payload.raw_text.strip(), payload.security_id)
            with vault.lock:
                saved = quick_notes.create(
                    security_id=payload.security_id,
                    raw_text=payload.raw_text,
                    draft=draft,
                )
            return saved.__dict__
        except AIUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/ai/quick-notes/{draft_id}/accept")
    def accept_quick_note(draft_id: str, payload: AcceptQuickNotePayload) -> dict[str, str]:
        try:
            with vault.lock:
                return {"note_id": quick_notes.accept(draft_id, body=payload.body)}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/ai/roles")
    def ai_roles() -> list[dict[str, object]]:
        return list(AI_ROLES)

    @app.get("/api/ai/skills")
    def ai_skills() -> list[dict[str, str]]:
        return chats.skill_layer.catalog()

    @app.get("/api/ai/chats")
    def list_ai_chats(security_id: str | None = None) -> list[dict[str, object]]:
        with vault.lock:
            return chats.list(security_id)

    @app.post("/api/ai/chats")
    def create_ai_chat(payload: CreateChatPayload) -> dict[str, object]:
        try:
            get_role(payload.role_id)
            with vault.lock:
                return chats.create(
                    security_id=payload.security_id,
                    role_id=payload.role_id,
                    title=payload.title,
                    mode=payload.mode,
                )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/ai/chats/{thread_id}")
    def get_ai_chat(thread_id: str) -> dict[str, object]:
        try:
            with vault.lock:
                return chats.get(thread_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/ai/chats/{thread_id}/archive")
    def archive_ai_chat(thread_id: str) -> dict[str, bool]:
        try:
            with vault.lock:
                chats.archive(thread_id)
            return {"archived": True}
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/ai/chats/{thread_id}/messages")
    def send_ai_chat_message(thread_id: str, payload: ChatMessagePayload) -> dict[str, object]:
        try:
            return chats.send(
                thread_id=thread_id, content=payload.content.strip(), role=get_role(payload.role_id)
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except AIUnavailableError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/refresh-jobs/{job_id}")
    def refresh_job(job_id: str) -> dict[str, object]:
        try:
            with vault.lock:
                return evidence.get_job(job_id).__dict__
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Unknown refresh job") from error

    @app.post("/api/refresh-jobs")
    def refresh(payload: RefreshPayload) -> dict[str, object]:
        observed_at = datetime.now(timezone.utc).isoformat()
        with vault.lock:
            if payload.kind == "company":
                job = evidence.refresh_company_pack(
                    payload.payload, requested_as_of=payload.requested_as_of, observed_at=observed_at
                )
            else:
                job = evidence.refresh_quote_history(
                    payload.payload, requested_as_of=payload.requested_as_of, observed_at=observed_at
                )
        return job.__dict__

    @app.post("/api/market/refresh")
    def refresh_market(payload: MarketRefreshPayload) -> dict[str, object]:
        selected = (
            ["indices", "lhb", "industry_flow", "pulse", "market_news"]
            if payload.section == "all"
            else [payload.section]
        )
        with vault.lock:
            completed, failed = refresh_market_sections(selected)
            market = _latest_market_snapshots(vault)
            market["report_stage"] = market_report_stage(clock())
        return {
            "completed": completed,
            "failed": failed,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "report_stage": market_report_stage(clock()),
            "market": market,
        }

    web_dist = web_dist_directory()
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app
