import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from invest_vault.ai_roles import AI_ROLES
from invest_vault.api import create_app, web_dist_directory

ENTRY = {
    "record_id": "api-trade-001",
    "idempotency_key": "api:trade-001",
    "kind": "trade",
    "account_id": "brokerage",
    "security_id": "CN:SSE:600519:STOCK",
    "occurred_at": "2026-07-09T15:00:00+08:00",
    "quantity": "10",
    "cash_amount": "-11821.90",
    "currency": "CNY",
    "action": "buy",
}


def test_api_is_thread_safe_and_empty_vault_never_bootstraps_demo_data(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        assert client.get("/api/provider-health").status_code == 200
        response = client.get("/api/bootstrap")
        assert response.status_code == 200
        payload = response.json()
        assert payload["mode"] == "vault"
        assert payload["report_as_of"] is None
        assert payload["holdings"] == []


def test_ledger_and_research_actions_have_real_api_effects(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        imported = client.post("/api/ledger/import", json={"entries": [ENTRY]})
        assert imported.status_code == 200
        assert imported.json() == {"inserted": 1, "skipped": 0}

        exported = client.get("/api/ledger/export")
        assert exported.status_code == 200
        assert exported.json()[0]["record_id"] == "api-trade-001"

        note = client.post(
            "/api/research/notes",
            json={"security_id": "CN:SSE:600519:STOCK", "body": "关注 60 日回撤。"},
        )
        assert note.status_code == 200
        assert note.json()["note_id"]

        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["mode"] == "vault"
        assert bootstrap["holdings"][0]["quantity"] == "10"


def test_bootstrap_joins_a_persisted_close_to_the_matching_holding(tmp_path: Path) -> None:
    quote = {
        "symbol": "600519",
        "market": "a",
        "asset_type": "stock",
        "name": "贵州茅台",
        "price": 1204.98,
        "previous_close": 1182.19,
        "change": 22.79,
        "change_percent": 1.93,
        "currency": "CNY",
        "trade_date": "2026-07-10",
        "source": "tencent_kline",
        "source_ref": "https://web.ifzq.gtimg.cn/",
    }
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        assert client.post("/api/ledger/import", json={"entries": [ENTRY]}).status_code == 200
        refresh = client.post(
            "/api/refresh-jobs",
            json={"kind": "quote", "requested_as_of": "2026-07-10", "payload": quote},
        )
        assert refresh.status_code == 200
        holding = client.get("/api/bootstrap").json()["holdings"][0]
        assert holding["name"] == "贵州茅台"
        assert holding["price"] == 1204.98
        assert holding["change_percent"] == 1.93
        assert holding["trade_date"] == "2026-07-10"


def test_portfolio_cutoff_uses_the_oldest_holding_archive_date(tmp_path: Path) -> None:
    second = {
        **ENTRY,
        "record_id": "api-fund-001",
        "idempotency_key": "api:fund-001",
        "security_id": "CN:SSE:512480:FUND",
        "quantity": "1000",
        "cash_amount": "-1471.00",
    }
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        client.post("/api/ledger/import", json={"entries": [ENTRY, second]})
        for symbol, asset_type, price, trade_date in (
            ("600519", "stock", 1204.98, "2026-07-10"),
            ("512480", "fund", 1.292, "2026-07-13"),
        ):
            client.post(
                "/api/refresh-jobs",
                json={
                    "kind": "quote",
                    "requested_as_of": trade_date,
                    "payload": {
                        "symbol": symbol,
                        "market": "a",
                        "asset_type": asset_type,
                        "name": symbol,
                        "price": price,
                        "currency": "CNY",
                        "trade_date": trade_date,
                        "source": "tencent_kline",
                        "source_ref": "https://example.test",
                    },
                },
            )
        assert client.get("/api/bootstrap").json()["report_as_of"] == "2026-07-10"


def test_post_close_archive_reports_the_target_batch_and_partial_holding_coverage(
    tmp_path: Path, monkeypatch
) -> None:
    rows = [
        {
            "row_id": "current",
            "symbol": "600519",
            "asset_type": "a_share",
            "invested_amount_cny": "1000",
            "bought_on": "2026-07-13",
        },
        {
            "row_id": "stale",
            "symbol": "002808",
            "asset_type": "a_share",
            "invested_amount_cny": "1000",
            "bought_on": "2026-07-13",
        },
    ]

    def now() -> datetime:
        return datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def quote(security_id, trade_date):
        if "600519" not in security_id:
            raise ValueError("目标交易日无交易记录")
        return {
            "symbol": "600519",
            "market": "a",
            "asset_type": "stock",
            "name": "贵州茅台",
            "price": 1214.88,
            "currency": "CNY",
            "trade_date": trade_date.isoformat(),
            "source": "test",
            "source_ref": "https://example.test",
        }

    monkeypatch.setattr("invest_vault.api.fetch_security_historical_close", quote)
    for loader in (
        "fetch_public_quote",
        "fetch_financial_snapshot",
        "fetch_company_announcements",
        "fetch_a_share_index_overview",
        "fetch_cross_market_index_overview",
        "fetch_lhb",
        "fetch_industry_money_flow",
        "fetch_market_pulse",
        "fetch_market_news",
        "fetch_global_market_movers",
        "fetch_global_earnings_calendar",
        "fetch_global_theme_performance",
    ):
        monkeypatch.setattr(
            f"invest_vault.api.{loader}", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline"))
        )

    with TestClient(create_app(tmp_path, automatic_updates=True, now_provider=now)) as client:
        assert client.post("/api/holdings", json={"rows": rows}).status_code == 200
        payload = client.get("/api/bootstrap").json()

    assert payload["report_as_of"] == "2026-07-14"
    assert payload["archive_coverage"] == {"target_date": "2026-07-14", "current": 1, "total": 2, "stale": 1}
    assert {item["symbol"]: item["trade_date"] for item in payload["holdings"]} == {
        "002808": None,
        "600519": "2026-07-14",
    }


def test_failed_post_close_quote_is_retried_on_the_next_automatic_check(tmp_path: Path, monkeypatch) -> None:
    def now() -> datetime:
        return datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    attempts = 0

    def quote(security_id, trade_date):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("provider not ready")
        return {
            "symbol": "600519",
            "market": "a",
            "asset_type": "stock",
            "name": "贵州茅台",
            "price": 1214.88,
            "currency": "CNY",
            "trade_date": trade_date.isoformat(),
            "source": "test",
            "source_ref": "https://example.test",
        }

    monkeypatch.setattr("invest_vault.api.fetch_security_historical_close", quote)
    for loader in (
        "fetch_public_quote",
        "fetch_financial_snapshot",
        "fetch_company_announcements",
        "fetch_a_share_index_overview",
        "fetch_cross_market_index_overview",
        "fetch_lhb",
        "fetch_industry_money_flow",
        "fetch_market_pulse",
        "fetch_market_news",
        "fetch_global_market_movers",
        "fetch_global_earnings_calendar",
        "fetch_global_theme_performance",
    ):
        monkeypatch.setattr(
            f"invest_vault.api.{loader}", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline"))
        )

    with TestClient(create_app(tmp_path, automatic_updates=True, now_provider=now)) as client:
        client.post(
            "/api/holdings",
            json={
                "rows": [
                    {
                        "row_id": "retry",
                        "symbol": "600519",
                        "asset_type": "a_share",
                        "invested_amount_cny": "1000",
                        "bought_on": "2026-07-13",
                    }
                ]
            },
        )
        assert client.get("/api/bootstrap").json()["holdings"][0]["trade_date"] == "2026-07-13"
        assert client.get("/api/bootstrap").json()["holdings"][0]["trade_date"] == "2026-07-14"

    assert attempts == 3


def test_incomplete_industry_flow_archive_is_repaired_on_the_next_automatic_check(
    tmp_path: Path, monkeypatch
) -> None:
    def now() -> datetime:
        return datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    with TestClient(create_app(tmp_path, automatic_updates=False)):
        pass
    broken = {"date": "2026-07-14", "source": "test", "inbound": [{"name": "元件"}], "outbound": []}
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        connection.execute(
            "INSERT INTO market_snapshots VALUES (?, ?, ?, ?, ?)",
            ("industry_flow", "2026-07-14", "test", json.dumps(broken), "2026-07-14T09:31:00+00:00"),
        )

    repaired = {
        "date": "2026-07-14",
        "source": "test",
        "inbound": [{"name": "元件"}],
        "outbound": [{"name": "半导体"}],
    }
    monkeypatch.setattr("invest_vault.api.fetch_industry_money_flow", lambda _: repaired)
    monkeypatch.setattr(
        "invest_vault.api.fetch_market_pulse",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("offline")),
    )
    for loader in (
        "fetch_a_share_index_overview",
        "fetch_cross_market_index_overview",
        "fetch_lhb",
        "fetch_market_news",
        "fetch_global_market_movers",
        "fetch_global_earnings_calendar",
        "fetch_global_theme_performance",
    ):
        monkeypatch.setattr(
            f"invest_vault.api.{loader}", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline"))
        )

    with TestClient(create_app(tmp_path, automatic_updates=True, now_provider=now)) as client:
        flow = client.get("/api/bootstrap").json()["market"]["industry_flow"]

    assert flow["outbound"] == [{"name": "半导体"}]


def test_user_can_refresh_all_market_sections_and_receive_partial_failures(
    tmp_path: Path, monkeypatch
) -> None:
    indices = {
        "date": "2026-07-17",
        "source": "fixture",
        "rows": [
            {
                "code": "000001",
                "name": "上证指数",
                "close": 3764.15,
                "change": -118.26,
                "change_percent": -3.05,
                "volume": 650450984,
                "amount": 1246445450000,
                "trade_date": "2026-07-17",
                "market": "CN",
                "currency": "CNY",
            },
        ],
    }
    flow = {"date": "2026-07-17", "source": "fixture", "inbound": [], "outbound": [{"name": "白酒"}]}
    monkeypatch.setattr("invest_vault.api.fetch_a_share_index_overview", lambda: indices)
    monkeypatch.setattr("invest_vault.api.fetch_cross_market_index_overview", lambda: {
        "date": "2026-07-17", "source": "global fixture", "rows": []
    })
    monkeypatch.setattr(
        "invest_vault.api.fetch_lhb", lambda _: (_ for _ in ()).throw(ValueError("龙虎榜源不可用"))
    )
    monkeypatch.setattr("invest_vault.api.fetch_industry_money_flow", lambda _: flow)
    monkeypatch.setattr(
        "invest_vault.api.fetch_market_pulse",
        lambda *_args, **_kwargs: {
            "date": "2026-07-17",
            "source": "stock-analysis fixture",
            "kind": "limit_pools",
        },
    )
    monkeypatch.setattr(
        "invest_vault.api.fetch_market_news",
        lambda **_kwargs: {
            "date": "2026-07-18",
            "source": "富途 fixture",
            "items": [],
            "total_count": 0,
        },
    )
    monkeypatch.setattr("invest_vault.api.fetch_global_market_movers", lambda: {"date": "2026-07-18", "source": "fixture", "markets": []})
    monkeypatch.setattr("invest_vault.api.fetch_global_earnings_calendar", lambda: {"date": "2026-07-18", "source": "fixture", "markets": []})
    monkeypatch.setattr("invest_vault.api.fetch_global_theme_performance", lambda: {"date": "2026-07-18", "source": "fixture", "rows": []})
    monkeypatch.setattr("invest_vault.api.fetch_a_share_hot_themes", lambda: {"date": "2026-07-18", "source": "fixture", "rows": []})
    monkeypatch.setattr("invest_vault.api.fetch_a_share_earnings_calendar", lambda: {"date": "2026-07-18", "source": "fixture", "rows": []})

    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        response = client.post("/api/market/refresh", json={"section": "all"})
        market = client.get("/api/bootstrap").json()["market"]

    assert response.status_code == 200
    assert response.json()["completed"] == [
        "indices", "global_indices", "industry_flow", "pulse", "a_market_news", "a_share_themes", "a_share_earnings_calendar", "global_market_news",
        "global_market_movers", "global_earnings_calendar", "global_themes"
    ]
    assert response.json()["failed"] == {"lhb": "龙虎榜源不可用"}
    assert market["indices"]["rows"][0]["name"] == "上证指数"
    assert market["industry_flow"]["outbound"] == [{"name": "白酒"}]
    assert market["a_market_news"]["source"] == "富途 fixture"
    assert market["global_market_news"]["source"] == "富途 fixture"


def test_global_page_refresh_fetches_independent_sources_in_parallel(
    tmp_path: Path, monkeypatch
) -> None:
    from threading import Lock
    from time import sleep

    state = {"active": 0, "maximum": 0}
    state_lock = Lock()

    def payload(*_args, **_kwargs) -> dict[str, object]:
        with state_lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        sleep(0.04)
        with state_lock:
            state["active"] -= 1
        return {
            "date": "2026-07-20",
            "source": "fixture",
            "rows": [],
            "markets": [],
            "items": [],
        }

    monkeypatch.setattr("invest_vault.api.fetch_cross_market_index_overview", payload)
    monkeypatch.setattr("invest_vault.api.fetch_market_news", payload)
    monkeypatch.setattr("invest_vault.api.fetch_global_market_movers", payload)
    monkeypatch.setattr("invest_vault.api.fetch_global_earnings_calendar", payload)
    monkeypatch.setattr("invest_vault.api.fetch_global_theme_performance", payload)

    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        response = client.post("/api/market/refresh", json={"section": "global"})

    assert response.status_code == 200
    assert state["maximum"] >= 2


def test_daily_market_modules_fall_back_to_the_latest_complete_trading_day(
    tmp_path: Path, monkeypatch
) -> None:
    lhb_dates: list[str] = []
    flow_dates: list[str] = []

    def lhb(trade_day: date) -> dict[str, object]:
        lhb_dates.append(trade_day.isoformat())
        return {
            "date": trade_day.isoformat(),
            "source": "fixture",
            "rows": [{"name": "甲公司"}] if trade_day.isoformat() == "2026-07-17" else [],
        }

    def flow(trade_day: date) -> dict[str, object]:
        flow_dates.append(trade_day.isoformat())
        if trade_day.isoformat() != "2026-07-17":
            raise ValueError("目标日资金流尚未完整披露")
        return {
            "date": "2026-07-17",
            "source": "fixture",
            "inbound": [{"name": "医药"}],
            "outbound": [{"name": "白酒"}],
        }

    monkeypatch.setattr("invest_vault.api.fetch_lhb", lhb)
    monkeypatch.setattr("invest_vault.api.fetch_industry_money_flow", flow)
    def now() -> datetime:
        return datetime(2026, 7, 21, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    with TestClient(create_app(tmp_path, automatic_updates=False, now_provider=now)) as client:
        lhb_result = client.post("/api/market/refresh", json={"section": "lhb"}).json()
        flow_result = client.post("/api/market/refresh", json={"section": "industry_flow"}).json()

    assert lhb_dates == ["2026-07-20", "2026-07-17"]
    assert flow_dates == ["2026-07-21", "2026-07-20", "2026-07-17"]
    assert lhb_result["market"]["lhb"]["date"] == "2026-07-17"
    assert lhb_result["market"]["lhb"]["fallback_used"] is True
    assert flow_result["market"]["industry_flow"]["date"] == "2026-07-17"
    assert flow_result["market"]["industry_flow"]["fallback_used"] is True


def test_user_can_record_cash_balance_and_drawdown_limit_for_ai_evidence(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        saved = client.post(
            "/api/portfolio/risk-profile",
            json={
                "cash_balance_cny": "50000",
                "max_drawdown_percent": "12.5",
            },
        )
        bootstrap = client.get("/api/bootstrap").json()

    assert saved.status_code == 200
    assert bootstrap["portfolio_profile"] == {
        "cash_balance_cny": "50000",
        "max_drawdown_percent": "12.5",
    }


def test_bootstrap_refreshes_all_market_sections_instead_of_reusing_a_saved_day(
    tmp_path: Path, monkeypatch
) -> None:
    calls = {
        "indices": 0, "global_indices": 0, "lhb": 0, "industry_flow": 0,
        "pulse": 0, "a_market_news": 0, "global_market_news": 0,
        "global_market_movers": 0, "global_earnings_calendar": 0, "global_themes": 0,
    }

    def indices():
        calls["indices"] += 1
        return {
            "date": "2026-07-20",
            "session": "盘中",
            "session_label": "7月20日盘中实时数据",
            "source": "fixture",
            "rows": [{"name": "上证指数", "close": 3000 + calls["indices"]}],
        }

    def global_indices():
        calls["global_indices"] += 1
        return {
            "date": "2026-07-20", "source": "global fixture",
            "rows": [{"name": "日经225", "close": 40000 + calls["global_indices"]}],
        }

    def lhb(_):
        calls["lhb"] += 1
        return {"date": "2026-07-20", "source": "fixture", "rows": [{"name": "甲公司"}]}

    def flow(_):
        calls["industry_flow"] += 1
        return {
            "date": "2026-07-20",
            "source": "fixture",
            "inbound": [{"name": "医药"}],
            "outbound": [{"name": "白酒"}],
        }

    def pulse(*_args, **_kwargs):
        calls["pulse"] += 1
        return {"date": "2026-07-20", "source": "fixture", "kind": "holding_news", "news": []}

    def market_news(**_kwargs):
        key = "a_market_news" if _kwargs.get("regions") == ("A股",) else "global_market_news"
        calls[key] += 1
        return {"date": "2026-07-20", "source": "fixture", "items": [], "total_count": 0}

    monkeypatch.setattr("invest_vault.api.fetch_a_share_index_overview", indices)
    monkeypatch.setattr("invest_vault.api.fetch_cross_market_index_overview", global_indices)
    monkeypatch.setattr("invest_vault.api.fetch_lhb", lhb)
    monkeypatch.setattr("invest_vault.api.fetch_industry_money_flow", flow)
    monkeypatch.setattr("invest_vault.api.fetch_market_pulse", pulse)
    monkeypatch.setattr("invest_vault.api.fetch_market_news", market_news)
    def global_section(key: str) -> dict[str, object]:
        calls[key] += 1
        return {"date": "2026-07-20", "source": "fixture", "rows": [], "markets": []}
    monkeypatch.setattr("invest_vault.api.fetch_global_market_movers", lambda: global_section("global_market_movers"))
    monkeypatch.setattr("invest_vault.api.fetch_global_earnings_calendar", lambda: global_section("global_earnings_calendar"))
    monkeypatch.setattr("invest_vault.api.fetch_global_theme_performance", lambda: global_section("global_themes"))

    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=True,
            now_provider=lambda: datetime(2026, 7, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    ) as client:
        first = client.get("/api/bootstrap").json()["market"]
        second = client.get("/api/bootstrap").json()["market"]

    assert first["indices"]["rows"][0]["close"] == 3001
    assert second["indices"]["rows"][0]["close"] == 3002
    assert calls == {
        "indices": 2, "global_indices": 2, "lhb": 2, "industry_flow": 2,
        "pulse": 2, "a_market_news": 2, "global_market_news": 2,
        "global_market_movers": 2, "global_earnings_calendar": 2, "global_themes": 2,
    }


def test_bootstrap_uses_intraday_holding_quote_without_overwriting_completed_close(
    tmp_path: Path, monkeypatch
) -> None:
    def now() -> datetime:
        return datetime(2026, 7, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    monkeypatch.setattr(
        "invest_vault.api.fetch_public_quote",
        lambda *_: {
            "symbol": "600519",
            "name": "贵州茅台",
            "price": 120,
            "previous_close": 110,
            "change": 10,
            "change_percent": 9.09,
            "trade_date": "2026-07-20",
            "source": "fixture-live",
        },
    )
    monkeypatch.setattr(
        "invest_vault.api.fetch_security_historical_close",
        lambda _, trade_date: {
            "symbol": "600519",
            "name": "贵州茅台",
            "price": 110,
            "trade_date": trade_date.isoformat(),
            "source": "fixture-close",
            "source_ref": "https://example.test/close",
            "market": "a",
            "asset_type": "stock",
        },
    )
    for loader in (
        "fetch_financial_snapshot",
        "fetch_company_announcements",
        "fetch_a_share_index_overview",
        "fetch_cross_market_index_overview",
        "fetch_lhb",
        "fetch_industry_money_flow",
        "fetch_market_pulse",
        "fetch_market_news",
        "fetch_global_market_movers",
        "fetch_global_earnings_calendar",
        "fetch_global_theme_performance",
    ):
        monkeypatch.setattr(
            f"invest_vault.api.{loader}", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline"))
        )

    with TestClient(create_app(tmp_path, automatic_updates=True, now_provider=now)) as client:
        client.post(
            "/api/holdings",
            json={
                "rows": [
                    {
                        "row_id": "live",
                        "symbol": "600519",
                        "asset_type": "a_share",
                        "invested_amount_cny": "1000",
                        "bought_on": "2026-07-17",
                    }
                ]
            },
        )
        holding = client.get("/api/bootstrap").json()["holdings"][0]

    assert holding["price"] == 120
    assert holding["data_session"] == "盘中"
    assert holding["data_label"] == "7月20日盘中实时数据"


def test_user_facing_holding_form_saves_supported_markets_without_inventing_quantity(tmp_path: Path) -> None:
    rows = [
        {
            "row_id": "row-a",
            "symbol": "600519",
            "asset_type": "a_share",
            "invested_amount_cny": "10000.50",
            "bought_on": "2026-07-10",
        },
        {
            "row_id": "row-fund",
            "symbol": "512480",
            "asset_type": "fund",
            "invested_amount_cny": "8000",
            "bought_on": "2026-07-09",
        },
        {
            "row_id": "row-hk",
            "symbol": "700",
            "asset_type": "hk_stock",
            "invested_amount_cny": "12000",
            "bought_on": "2026-07-08",
        },
    ]
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        response = client.post("/api/holdings", json={"rows": rows})
        assert response.status_code == 200
        assert response.json() == {"inserted": 3, "skipped": 0}
        assert client.post("/api/holdings", json={"rows": rows}).json() == {"inserted": 0, "skipped": 3}
        holdings = client.get("/api/bootstrap").json()["holdings"]
        assert [(item["security_id"], item["quantity"]) for item in holdings] == [
            ("CN:SSE:512480:FUND", None),
            ("CN:SSE:600519:STOCK", None),
            ("HK:HKEX:00700:STOCK", None),
        ]
        assert holdings[0]["invested_amount_cny"] == "8000"
        assert holdings[0]["bought_on"] == "2026-07-09"
        assert holdings[0]["asset_type"] == "fund"


def test_saved_holdings_have_an_excel_export_and_no_csv_route(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        client.post(
            "/api/holdings",
            json={
                "rows": [
                    {
                        "row_id": "export-row",
                        "symbol": "600519",
                        "asset_type": "a_share",
                        "invested_amount_cny": "11821.90",
                        "bought_on": "2026-07-09",
                    }
                ]
            },
        )
        response = client.get("/api/holdings/export.xlsx")
        assert response.status_code == 200
        assert response.content.startswith(b"PK")
        assert "spreadsheetml" in response.headers["content-type"]
        assert client.get("/api/holdings/export.csv").status_code == 404


def test_holding_form_rejects_invalid_codes_and_non_positive_amounts(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        bad_code = client.post(
            "/api/holdings",
            json={
                "rows": [
                    {
                        "row_id": "bad-code",
                        "symbol": "AAPL",
                        "asset_type": "a_share",
                        "invested_amount_cny": "1",
                        "bought_on": "2026-07-10",
                    }
                ]
            },
        )
        assert bad_code.status_code == 422
        assert "6位数字" in bad_code.json()["detail"]
        bad_amount = client.post(
            "/api/holdings",
            json={
                "rows": [
                    {
                        "row_id": "bad-amount",
                        "symbol": "600519",
                        "asset_type": "a_share",
                        "invested_amount_cny": "0",
                        "bought_on": "2026-07-10",
                    }
                ]
            },
        )
        assert bad_amount.status_code == 422
        unsupported = client.post(
            "/api/holdings",
            json={
                "rows": [
                    {
                        "row_id": "unsupported",
                        "symbol": "AAPL",
                        "asset_type": "us_stock",
                        "invested_amount_cny": "1",
                        "bought_on": "2026-07-10",
                    }
                ]
            },
        )
        assert unsupported.status_code == 422


def test_holding_form_batch_rolls_back_when_a_row_conflicts(tmp_path: Path) -> None:
    original = {
        "row_id": "same-row",
        "symbol": "600519",
        "asset_type": "a_share",
        "invested_amount_cny": "1000",
        "bought_on": "2026-07-10",
    }
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        assert client.post("/api/holdings", json={"rows": [original]}).status_code == 200
        response = client.post(
            "/api/holdings",
            json={
                "rows": [
                    {
                        "row_id": "new-row",
                        "symbol": "512480",
                        "asset_type": "fund",
                        "invested_amount_cny": "2000",
                        "bought_on": "2026-07-09",
                    },
                    {**original, "invested_amount_cny": "9999"},
                ]
            },
        )
        assert response.status_code == 422
        holdings = client.get("/api/bootstrap").json()["holdings"]
        assert [(item["symbol"], item["invested_amount_cny"]) for item in holdings] == [("600519", "1000")]


def test_file_import_route_is_not_exposed(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        assert "/api/ledger/import-file" not in client.get("/openapi.json").json()["paths"]
        assert client.post("/api/ledger/import-file", content=b"anything").status_code in {404, 405}


def test_material_excerpt_is_saved_to_the_matching_security_note(tmp_path: Path) -> None:
    security_id = "CN:SSE:600519:STOCK"
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        material = client.post(
            "/api/research/materials",
            json={
                "security_id": security_id,
                "material_type": "财务报告",
                "title": "贵州茅台2026年第一季度报告",
                "published_at": "2026-04-25",
                "source_name": "公司公告",
                "source_url": "https://example.test/600519/2026q1",
                "excerpt": "营业收入同比增长。",
            },
        )
        assert material.status_code == 200
        note = client.post(
            "/api/research/notes/from-material",
            json={
                "security_id": security_id,
                "material_id": material.json()["material_id"],
                "quoted_text": "营业收入同比增长。",
                "body": "继续观察现金流与收入增速是否匹配。",
            },
        )
        assert note.status_code == 200
        workspace = client.get(f"/api/research/{security_id}").json()
        assert workspace["materials"][0]["title"] == "贵州茅台2026年第一季度报告"
        assert workspace["notes"][0]["body"] == "继续观察现金流与收入增速是否匹配。"
        assert workspace["notes"][0]["source_title"] == "贵州茅台2026年第一季度报告"
        assert workspace["notes"][0]["source_url"] == "https://example.test/600519/2026q1"
        assert workspace["notes"][0]["quoted_text"] == "营业收入同比增长。"


def test_holding_note_and_thesis_corrections_are_revisioned_but_deletions_are_permanent(
    tmp_path: Path,
) -> None:
    security_id = "CN:SSE:600519:STOCK"
    row = {
        "row_id": "editable",
        "symbol": "600519",
        "asset_type": "a_share",
        "invested_amount_cny": "10000",
        "bought_on": "2026-07-09",
    }
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        assert client.post("/api/holdings", json={"rows": [row]}).status_code == 200
        entry = client.get("/api/bootstrap").json()["holding_entries"][0]
        revised = {**row, "invested_amount_cny": "12000", "bought_on": "2026-07-10"}
        assert client.put(f"/api/holdings/{entry['holding_id']}", json=revised).status_code == 200
        assert client.get("/api/bootstrap").json()["holdings"][0]["invested_amount_cny"] == "12000"

        note_id = client.post(
            "/api/research/notes", json={"security_id": security_id, "body": "旧笔记"}
        ).json()["note_id"]
        assert (
            client.put(
                f"/api/research/notes/{note_id}", json={"security_id": security_id, "body": "新笔记"}
            ).status_code
            == 200
        )
        assert client.get(f"/api/research/{security_id}").json()["notes"][0]["body"] == "新笔记"
        assert client.delete(f"/api/research/notes/{note_id}").status_code == 200
        assert client.get(f"/api/research/{security_id}").json()["notes"] == []

        thesis = client.post(
            "/api/research/theses", json={"security_id": security_id, "body": "旧观点"}
        ).json()
        revised_thesis = client.post(
            "/api/research/theses",
            json={"security_id": security_id, "thesis_id": thesis["thesis_id"], "body": "新观点"},
        )
        assert revised_thesis.json()["revision_number"] == 2
        assert client.delete(f"/api/research/theses/{thesis['thesis_id']}").status_code == 200
        assert client.get(f"/api/research/{security_id}").json()["thesis"] is None

        assert client.delete(f"/api/holdings/{entry['holding_id']}").status_code == 200
        assert client.get("/api/bootstrap").json()["holdings"] == []

    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM holding_records").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM notes WHERE note_id = ?", (note_id,)).fetchone()[0] == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM note_revisions WHERE note_id = ?", (note_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM theses WHERE thesis_id = ?", (thesis["thesis_id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM thesis_revisions WHERE thesis_id = ?", (thesis["thesis_id"],)
            ).fetchone()[0]
            == 0
        )


def test_profit_is_estimated_from_exact_purchase_close_without_inventing_quantity(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        client.post(
            "/api/holdings",
            json={
                "rows": [
                    {
                        "row_id": "profit",
                        "symbol": "600519",
                        "asset_type": "a_share",
                        "invested_amount_cny": "10000",
                        "bought_on": "2026-07-09",
                    }
                ]
            },
        )
        for trade_date, price in (("2026-07-09", 1000), ("2026-07-13", 1100)):
            client.post(
                "/api/refresh-jobs",
                json={
                    "kind": "quote",
                    "requested_as_of": trade_date,
                    "payload": {
                        "symbol": "600519",
                        "market": "a",
                        "asset_type": "stock",
                        "name": "贵州茅台",
                        "price": price,
                        "currency": "CNY",
                        "trade_date": trade_date,
                        "source": "test",
                        "source_ref": "https://example.test",
                    },
                },
            )
        holding = client.get("/api/bootstrap").json()["holdings"][0]
        assert holding["quantity"] is None
        assert holding["estimated_profit_cny"] == 1000
        assert holding["estimated_profit_percent"] == 10
        assert holding["profit_basis"] == "按买入日收盘价估算"


def test_invest_vault_bundles_pinned_stock_analysis_runtime_without_external_install() -> None:
    app_root = Path(__file__).parents[1]
    pyproject = (app_root / "pyproject.toml").read_text(encoding="utf-8")
    runtime = app_root / "src" / "stock_analysis"
    skill = app_root / "skills" / "stock-analysis" / "SKILL.md"
    spec = (app_root / "sidecar.spec").read_text(encoding="utf-8")

    assert '"src/stock_analysis"' in pyproject
    assert runtime.joinpath("committee_selection.py").is_file()
    assert runtime.joinpath("integrations.py").is_file()
    assert 'version: "4.15.0"' in skill.read_text(encoding="utf-8")
    assert 'collect_submodules("stock_analysis")' in spec
    assert '("skills/stock-analysis", "skills/stock-analysis")' in spec
    assert '("skills/agent-reach", "skills/agent-reach")' in spec
    assert '("skills/primary-evidence-reach", "skills/primary-evidence-reach")' in spec
    assert app_root.joinpath("skills", "agent-reach", "SKILL.md").is_file()
    assert app_root.joinpath("skills", "primary-evidence-reach", "SKILL.md").is_file()


def test_a_share_and_global_market_pages_are_scoped_and_refreshable() -> None:
    source = (Path(__file__).parents[1] / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")

    assert 'label: "A股概览"' in source
    assert 'label: "全球概览"' in source
    assert 'label: "大盘议事厅"' in source
    assert 'title="刷新全部"' in source
    assert "row.change" in source
    assert "row.amount" in source
    assert "row.volume" in source
    assert 'refreshMarket("indices")' in source
    assert 'refreshMarket("lhb")' in source
    assert 'refreshMarket("industry_flow")' in source
    assert 'refreshGlobal("global_indices")' in source
    assert 'refreshGlobal("global_market_news")' in source
    assert 'refreshGlobal("global_market_movers")' in source
    assert 'refreshGlobal("global_earnings_calendar")' in source
    assert 'refreshGlobal("global_themes")' in source
    assert 'title="四市场领涨榜"' in source
    assert 'title="跨市场投资主题"' in source
    assert 'title="本月财报日历"' in source
    assert 'security_id: "MARKET:GLOBAL:OVERVIEW"' in source
    assert 'security_id: "MARKET:CN:OVERVIEW"' not in source
    assert "market.pulse" in source
    assert 'title="赚钱效应与上涨主线"' in source
    assert 'title="下跌风险"' in source
    assert "数据日期 {pulse.date}" in source
    assert source.count('refreshMarket("pulse")') >= 2
    assert "M3 ·" not in source
    assert "M4 ·" not in source
    assert "持仓股票 24 小时资讯" in source
    holding_news = source[
        source.index('title="持仓股票 24 小时资讯"') : source.index(
            'className="holding-news-pulse"'
        )
    ]
    assert 'refreshMarket("pulse")' in holding_news
    assert 'isRefreshing("pulse") ? "刷新中…" : "刷新"' in holding_news
    assert 'scene="market"' in source
    assert "刷新后自动识别时段" in source
    assert "停止生成" in source
    assert 'event.key === "Enter" && !event.shiftKey' in source
    assert "结合当前所有可用且完整的证据" in source
    assert (
        "<span>报告阶段</span>"
        not in source[source.index("function ResearchAssistant") : source.index("function SecurityWorkbench")]
    )
    assert "大盘议事厅" in source
    assert source.count('aria-label="大盘议事厅"') == 1
    assert 'marketScope="a-share"' not in source
    assert 'marketScope="global"' not in source
    assert "开始本时段议事" in source
    market_assistant = source[
        source.index("function ResearchAssistant") : source.index("function SecurityWorkbench")
    ]
    assert "<span>主持席</span>" in market_assistant
    assert 'localStorage.getItem("market-council-style")' in market_assistant
    assert '|| "dalio"' in market_assistant
    assert '<option value="committee">投研委员会 · 六席会审</option>' in market_assistant
    assert 'marketStyle === "committee"' in market_assistant
    assert '? "AI 市场行情助手"' not in market_assistant
    assert 'scene === "market" ? null' in market_assistant
    assert 'scene === "market" ? null : <div className="chat-composer">' in market_assistant
    assert "forceNew" in market_assistant
    assert market_assistant.count("清空对话") == 1
    assert market_assistant.count("新对话") == 1
    assert "按 stock-analysis 的问题匹配规则" not in market_assistant
    assert "依据研究问题，从 15 位投资专家中邀请 6 位互补委员" in market_assistant
    assert "会话历史" not in market_assistant
    assert 'if (scene === "market") {\n      setThreads([]);' not in market_assistant
    assert "行情阶段\\n${marketStage.label}" in market_assistant
    assert "大盘行情笔记" in source
    assert "saveNote={saveMarketNote}" in source
    market_page = source[source.index("function AShareMarketPage") : source.index("function MaterialList")]
    a_share_page = source[source.index("function AShareMarketPage") : source.index("function GlobalMarketPage")]
    global_page = source[source.index("function GlobalMarketPage") : source.index("function MarketCouncilPage")]
    assert "<ResearchAssistant" not in a_share_page
    assert "<ResearchAssistant" not in global_page
    investment_notes = source[
        source.index("function Research({") : source.index("function ResearchAssistant")
    ]
    assert "大盘行情笔记" not in market_page
    assert "大盘行情笔记" in investment_notes


def test_market_report_stage_is_returned_by_bootstrap_and_refresh(tmp_path: Path) -> None:
    current = datetime(2026, 7, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    with TestClient(create_app(tmp_path, automatic_updates=False, now_provider=lambda: current)) as client:
        assert client.get("/api/bootstrap").json()["market"]["report_stage"]["session"] == "盘中"
        refreshed = client.post("/api/market/refresh", json={"section": "lhb"}).json()
        assert refreshed["report_stage"]["session"] == "盘中"


def test_market_report_can_be_saved_as_a_market_note(tmp_path: Path) -> None:
    security_id = "MARKET:GLOBAL:OVERVIEW"
    body = "行情阶段\n7月20日盘中行情报告\n\n投委会报告\n市场宽度转弱。"
    title = "2026年7月20日 盘中报告（投委会）最终稿"
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        saved = client.post(
            "/api/research/notes",
            json={"security_id": security_id, "body": body, "title": title},
        )
        assert saved.status_code == 200
        workspace = client.get(f"/api/research/{security_id}").json()

    assert workspace["notes"][0]["body"] == body
    assert workspace["notes"][0]["title"] == title


def test_industry_flow_refresh_does_not_refresh_market_pulse(tmp_path: Path, monkeypatch) -> None:
    calls = {"industry_flow": 0, "pulse": 0}

    def flow(_):
        calls["industry_flow"] += 1
        return {
            "date": "2026-07-20",
            "source": "fixture",
            "inbound": [{"name": "医药"}],
            "outbound": [{"name": "白酒"}],
        }

    def pulse(*_args, **_kwargs):
        calls["pulse"] += 1
        return {"date": "2026-07-20", "source": "fixture", "kind": "holding_news", "news": []}

    monkeypatch.setattr("invest_vault.api.fetch_industry_money_flow", flow)
    monkeypatch.setattr("invest_vault.api.fetch_market_pulse", pulse)

    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        response = client.post("/api/market/refresh", json={"section": "industry_flow"})

    assert response.status_code == 200
    assert response.json()["completed"] == ["industry_flow"]
    assert response.json()["market"]["industry_flow"]["source"] == "fixture"
    assert calls == {"industry_flow": 1, "pulse": 0}


def test_assistant_report_renderer_turns_markdown_tables_into_real_tables() -> None:
    source = (Path(__file__).parents[1] / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")

    renderer = source[source.index("function renderInlineMarkdown") : source.index("const money")]
    assert "markdown-table" in renderer
    assert "<table" in renderer
    assert "<thead>" in renderer
    assert "<tbody>" in renderer
    assert '<ul className="report-list"' in renderer
    assert '<ol className="report-list"' in renderer


def test_investment_notes_use_semantic_preview_and_centered_detail_dialog() -> None:
    root = Path(__file__).parents[1]
    source = (root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (root / "web" / "src" / "styles.css").read_text(encoding="utf-8")
    notes = source[source.index("function NoteDisclosure") : source.index("function FinancialTable")]

    assert "<RichText text={note.body} />" in notes
    assert "…显示更多" in notes
    assert 'role="dialog"' in notes
    assert 'aria-modal="true"' in notes
    assert notes.count("<RichText text={note.body} />") >= 2
    assert "note-preview-clamp" in notes
    assert "note-detail-dialog" in notes
    assert "scrollHeight > node.clientHeight" in notes
    assert "ResizeObserver" in notes
    assert "{overflowing && (" in notes
    today = source[source.index("function Today(") : source.index("function Portfolio(")]
    assert "<NoteDisclosure note={item}" in today
    assert ".note-preview-clamp" in styles
    assert (
        "max-height:"
        in styles[styles.index(".note-preview-clamp") : styles.index(".note-preview-clamp") + 300]
    )


def test_note_editor_toolbar_applies_markdown_to_the_real_textarea() -> None:
    source = (Path(__file__).parents[1] / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    notes = source[source.index("function Research({") : source.index("function ResearchAssistant")]

    assert "noteEditorRef" in notes
    assert "applyNoteFormat" in notes
    assert 'onClick={() => applyNoteFormat("bold")}' in notes
    assert 'onClick={() => applyNoteFormat("italic")}' in notes
    assert 'onClick={() => applyNoteFormat("quote")}' in notes
    assert 'onClick={() => applyNoteFormat("unordered")}' in notes
    assert 'onClick={() => applyNoteFormat("ordered")}' in notes
    assert "expandInlineSelection" in notes
    renderer = source[source.index("function renderInlineMarkdown") : source.index("const money")]
    assert 'part.startsWith("***")' in renderer
    assert "<strong key={key}><em>" in renderer
    assert "renderInlineMarkdown" in renderer
    assert "renderInlineMarkdown(content" in renderer


def test_committee_ui_describes_stock_analysis_six_member_selection() -> None:
    source = (Path(__file__).parents[1] / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")

    assert "选择 6 位互补委员" in source
    assert "选择 1–3 位" not in source


def test_fund_manager_profile_uses_a_bounded_horizontal_scroll_region() -> None:
    root = Path(__file__).parents[1]
    source = (root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (root / "web" / "src" / "styles.css").read_text(encoding="utf-8")
    fund = source[source.index('title="基金经理画像"') : source.index('title="近期事件"')]

    assert 'className="manager-table-scroll"' in fund
    manager_styles = styles[
        styles.index(".manager-table-scroll") : styles.index(".manager-table-scroll") + 260
    ]
    assert "overflow-x: auto" in manager_styles
    assert "min-width:" in manager_styles


def test_global_search_is_scrollable_and_searches_related_symbols_and_materials() -> None:
    root = Path(__file__).parents[1]
    source = (root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (root / "web" / "src" / "styles.css").read_text(encoding="utf-8")
    palette = source[source.index("const paletteItems = useMemo") : source.index("const navigate =")]

    assert "fuzzyMatch" in palette
    assert "isSecurityCodeQuery" in palette
    assert "securityIdentifiers" in palette
    assert "note.body" in palette
    assert "holding?.symbol" in palette
    assert "materialItems" in palette
    assert "materials: filter(materialItems)" in palette
    assert 'className="palette-results"' in source
    assert ".palette-results" in styles
    assert (
        "overflow-y: auto"
        in styles[styles.index(".palette-results") : styles.index(".palette-results") + 200]
    )


def test_note_markdown_controls_render_as_semantic_ui_and_deletions_warn_permanently() -> None:
    source = (Path(__file__).parents[1] / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    renderer = source[source.index("function renderInlineMarkdown") : source.index("const money")]
    today = source[source.index("function Today(") : source.index("function Portfolio(")]
    delete_dialog = source[source.index("function ConfirmDelete") : source.index("export function App")]

    assert "<strong" in renderer
    assert "<em" in renderer
    assert "<blockquote" in renderer
    assert '<ul className="report-list"' in renderer
    assert '<ol className="report-list"' in renderer
    assert "已删除持仓" not in today
    assert "永久删除" in delete_dialog
    assert "plainMarkdown(request.label)" in delete_dialog
    assert "本地修订历史仍会保留" not in delete_dialog


def test_portfolio_exposes_cash_and_drawdown_inputs_instead_of_permanent_gaps() -> None:
    source = (Path(__file__).parents[1] / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")

    assert "现金余额（人民币）" in source
    assert "最大可承受回撤" in source
    assert 'api("/api/portfolio/risk-profile"' in source


def test_built_web_assets_are_available_to_the_service() -> None:
    assert (web_dist_directory() / "index.html").is_file()


def test_built_web_uses_the_chinese_product_language() -> None:
    web_dist = web_dist_directory()
    content = "\n".join(path.read_text(encoding="utf-8") for path in web_dist.rglob("*.html"))
    content += "\n" + "\n".join(path.read_text(encoding="utf-8") for path in web_dist.rglob("*.js"))

    assert "投资札记" in content
    assert "添加持仓" in content
    assert "我的 Vault" not in content
    assert "刷新行情" not in content


def test_web_source_has_no_file_import_control() -> None:
    source = (Path(__file__).parents[1] / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    assert 'type="file"' not in source
    assert "inputRef" not in source
    assert "/api/ledger/import-file" not in source
    assert "导出 CSV" not in source
    assert "/api/holdings/export.csv" not in source
    assert "导出 Excel" in source
    assert 'value="us_stock"' not in source
    assert "删除本行" in source
    assert "note-security" in source
    assert "cardLimitForWidth" in source
    assert "width >= 960 ? 12" in source
    assert "width >= 720 ? 8" in source
    assert "width >= 540 ? 4" in source
    assert 'className="holding-card-drag"' in source
    assert "onPointerDown" in source
    assert "当前投资观点" not in source
    assert "window.confirm" not in source


def test_market_pulse_is_peer_content_and_security_overview_stays_in_its_pane() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (app_root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "market-pulse-grid" not in source
    assert 'className="security-overview-scroll"' in source
    assert 'className="security-overview-metrics"' in source
    assert ".security-overview-scroll" in styles
    assert "overflow-x: auto" in styles
    assert ".security-evidence-pane > *" in styles
    assert "padding: 14px 8px 6px 42px" in styles


def test_security_layout_aligns_topbar_and_reflows_secondary_metrics() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (app_root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "security-layout" in source
    assert ".app-shell.security-layout .topbar" in styles
    assert "margin-inline: -18px" in styles
    overview = styles[
        styles.index(".security-overview-metrics") : styles.index(".security-overview-metrics") + 500
    ]
    assert "container-type: inline-size" in styles
    assert "@container security-evidence" in styles
    assert "grid-column" in overview or "grid-template-columns" in overview


def test_dense_workbench_layout_keeps_page_specific_structures_and_responsive_fallbacks() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (app_root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    for class_name in (
        "today-summary-strip",
        "portfolio-risk-form",
        "research-workbench",
        "security-overview-grid",
        "data-management-grid",
    ):
        assert class_name in source
        assert f".{class_name}" in styles
    assert 'active === "today" ? "has-attention"' not in source
    assert "grid-template-columns: 176px minmax(0, 1fr)" in styles
    assert "@media (max-width: 1184px)" in styles
    assert "min-height: 40px" in styles


def test_layout_regressions_keep_shell_cards_and_fund_tables_clear() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (app_root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert 'className="fund-nav-history"' in source
    assert ".status-line" in styles and "margin-inline: -24px" in styles
    assert ".holding-card-main" in styles and "padding-top: 42px" in styles
    assert ".holding-card .footer" in styles and "grid-template-columns" in styles
    assert ".fund-nav-history" in styles and "grid-column: 1 / -1" in styles
    assert ".fund-nav-history .compact-table" in styles and "min-width: 0" in styles
    assert "scrollbar-gutter: stable" in styles


def test_today_long_names_and_market_news_use_bounded_vertical_layouts() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (app_root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    for class_name in ("holding-card-meta", "market-secondary-grid", "market-side-stack", "market-news-list"):
        assert f'className="{class_name}"' in source or class_name in source
        assert f".{class_name}" in styles
    assert 'refreshMarket("a_market_news")' in source
    assert 'refreshGlobal("global_market_news")' in source
    assert 'title="A股大盘新闻"' in source
    assert 'title="24小时A股大盘新闻"' not in source
    assert "marketNews.items.slice(0, 6)" in source
    assert "themes.rows.slice(0, 6)" in source
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in styles
    assert ".market-secondary-grid" in styles and "align-items: stretch" in styles
    assert "grid-template-columns: minmax(0, 1fr);" in styles
    assert "-webkit-line-clamp: 2" in styles


def test_portfolio_and_security_pages_offer_manual_data_refresh() -> None:
    source = (Path(__file__).parents[1] / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    portfolio = source[source.index("function Portfolio(") : source.index("const marketOverviewSubject")]
    security = source[source.index("function SecurityWorkbench(") : source.index("function SettingsPage")]

    assert "refreshData" in portfolio
    assert "刷新持仓行情" in portfolio
    assert "refreshData" in security
    assert "刷新证券资料" in security
    assert 'api("/api/holdings/refresh"' in source


def test_today_holding_matters_offer_manual_refresh() -> None:
    source = (Path(__file__).parents[1] / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    today = source[source.index("function Today(") : source.index("function Portfolio(")]

    assert "refreshQuotes" in today
    assert "refreshMaterials" in today
    assert "刷新组合行情" in today
    assert "刷新持仓事项" in today
    assert 'scope=${scope}' in source


def test_today_refresh_scopes_keep_quotes_and_materials_independent(tmp_path: Path, monkeypatch) -> None:
    calls = {"live": 0, "history": 0, "financials": 0, "materials": 0}

    def live(_symbol):
        calls["live"] += 1
        return {
            "symbol": "600519",
            "market": "a",
            "asset_type": "stock",
            "name": "贵州茅台",
            "price": 1500,
            "trade_date": "2026-07-17",
            "source": "fixture-live",
        }

    def history(_security_id, requested):
        calls["history"] += 1
        return {
            "symbol": "600519",
            "market": "a",
            "asset_type": "stock",
            "name": "贵州茅台",
            "price": 1400,
            "trade_date": requested.isoformat(),
            "source": "fixture-history",
        }

    def financials(_symbol, _target):
        calls["financials"] += 1
        return None

    def materials(_symbol, _target):
        calls["materials"] += 1
        return []

    monkeypatch.setattr("invest_vault.api.fetch_public_quote", live)
    monkeypatch.setattr("invest_vault.api.fetch_security_historical_close", history)
    monkeypatch.setattr("invest_vault.api.fetch_financial_snapshot", financials)
    monkeypatch.setattr("invest_vault.api.fetch_company_announcements", materials)

    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            now_provider=lambda: datetime(2026, 7, 19, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    ) as client:
        assert client.post(
            "/api/holdings",
            json={
                "rows": [
                    {
                        "row_id": "row-1",
                        "symbol": "600519",
                        "asset_type": "a_share",
                        "invested_amount_cny": "10000",
                        "bought_on": "2026-07-01",
                    }
                ]
            },
        ).status_code == 200
        materials_response = client.post("/api/holdings/refresh?scope=materials")
        after_materials = dict(calls)
        quotes_response = client.post("/api/holdings/refresh?scope=quotes")

    assert materials_response.json()["scope"] == "materials"
    assert after_materials == {"live": 0, "history": 0, "financials": 1, "materials": 1}
    assert quotes_response.json()["scope"] == "quotes"
    assert calls["live"] == 1
    assert calls["history"] >= 1
    assert calls["financials"] == 1
    assert calls["materials"] == 1


def test_assistant_empty_state_matches_committee_information_density() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (app_root / "web" / "src" / "styles.css").read_text(encoding="utf-8")
    assistant = source[source.index("function ResearchAssistant") : source.index("function SecurityWorkbench")]

    assert "assistant-empty-state" in assistant
    assert "assistant-question-list" in assistant
    assert "已归档行情" in assistant
    assert ".assistant-empty-state" in styles


def test_research_sidebar_separates_market_overview_from_holdings() -> None:
    source = (Path(__file__).parents[1] / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    research = source[source.index("function Research({") : source.index("function ResearchAssistant")]

    assert "按大盘概览" in research
    assert "市场行情报告与结论" not in research
    assert all(label in research for label in ("盘前", "盘中", "盘后"))
    assert "market_session" in research


def test_market_note_session_is_persisted_and_returned(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        response = client.post(
            "/api/research/notes",
            json={
                "security_id": "MARKET:GLOBAL:OVERVIEW",
                "body": "行情阶段明确的市场笔记",
                "title": "2026年7月19日 盘中报告（芒格）最终稿",
                "market_session": "盘中",
            },
        )
        assert response.status_code == 200
        workspace = client.get("/api/research/MARKET%3AGLOBAL%3AOVERVIEW").json()

    assert workspace["notes"][0]["market_session"] == "盘中"


def test_market_pulse_uses_red_upside_and_green_risk_semantics() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (app_root / "web" / "src" / "styles.css").read_text(encoding="utf-8")
    market = source[source.index('title="赚钱效应与上涨主线"') : source.index('title="持仓股票 24 小时资讯"')]

    assert 'className="pulse-leaders upside"' in market
    assert 'className="pulse-leaders risk"' in market
    assert ".pulse-leaders.upside b" in styles
    assert ".pulse-leaders.risk b" in styles


def test_manual_holding_refresh_recomputes_profit_from_refreshed_prices(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "invest_vault.api.fetch_public_quote",
        lambda *_: {
            "symbol": "600519",
            "name": "贵州茅台",
            "price": 120,
            "previous_close": 118,
            "change_percent": 1.69,
            "trade_date": "2026-07-17",
            "source": "fixture-live",
        },
    )
    monkeypatch.setattr(
        "invest_vault.api.fetch_security_historical_close",
        lambda _security_id, trade_date: {
            "symbol": "600519",
            "name": "贵州茅台",
            "price": 100 if trade_date.isoformat() == "2026-07-16" else 120,
            "trade_date": trade_date.isoformat(),
            "source": "fixture-close",
            "source_ref": "https://example.test/close",
            "market": "a",
            "asset_type": "stock",
        },
    )
    for loader in ("fetch_financial_snapshot", "fetch_company_announcements"):
        monkeypatch.setattr(
            f"invest_vault.api.{loader}",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline")),
        )

    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            now_provider=lambda: datetime(2026, 7, 17, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    ) as client:
        client.post(
            "/api/holdings",
            json={
                "rows": [
                    {
                        "row_id": "manual",
                        "symbol": "600519",
                        "asset_type": "a_share",
                        "invested_amount_cny": "1000",
                        "bought_on": "2026-07-16",
                    }
                ]
            },
        )
        refreshed = client.post("/api/holdings/refresh")
        holding = client.get("/api/bootstrap?refresh=false").json()["holdings"][0]

    assert refreshed.status_code == 200
    assert refreshed.json()["refreshed"] is True
    assert holding["price"] == 120
    assert holding["estimated_profit_cny"] == 200.0


def test_hk_manual_refresh_uses_live_quote_fallback_and_exposes_valuation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "invest_vault.api.fetch_security_live_quote",
        lambda *_: {
            "symbol": "00700",
            "name": "腾讯控股",
            "price": 461.6,
            "previous_close": 454.2,
            "change_percent": 1.6292,
            "trade_date": "2026-07-17",
            "source": "tencent_kline",
            "source_ref": "https://example.test/hk",
            "pe_ttm": 16.86,
            "pb": 3.34,
            "market_cap_100m": 41945.3,
            "currency": "HKD",
        },
    )
    monkeypatch.setattr(
        "invest_vault.api.fetch_security_historical_close",
        lambda _security_id, trade_date: {
            "symbol": "00700",
            "name": "腾讯控股",
            "price": 454.2,
            "trade_date": trade_date.isoformat(),
            "source": "fixture-close",
            "source_ref": "https://example.test/close",
            "market": "hk",
            "asset_type": "stock",
        },
    )
    monkeypatch.setattr(
        "invest_vault.api.fetch_hkex_announcements",
        lambda *_: (_ for _ in ()).throw(ValueError("offline")),
    )

    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            now_provider=lambda: datetime(2026, 7, 17, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    ) as client:
        client.post(
            "/api/holdings",
            json={
                "rows": [
                    {
                        "row_id": "hk-manual",
                        "symbol": "700",
                        "asset_type": "hk_stock",
                        "invested_amount_cny": "1000",
                        "bought_on": "2026-07-16",
                    }
                ]
            },
        )
        refreshed = client.post("/api/holdings/refresh")
        holding = client.get("/api/bootstrap?refresh=false").json()["holdings"][0]

    assert refreshed.status_code == 200
    assert holding["name"] == "腾讯控股"
    assert holding["price"] == 461.6
    assert holding["change_percent"] == 1.6292
    assert holding["pe_ttm"] == 16.86
    assert holding["pb"] == 3.34
    assert holding["currency"] == "HKD"


def test_today_cards_use_pointer_dragging_from_the_card_surface() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (app_root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "onPointerDown" in source
    assert "setPointerCapture" in source
    assert "releasePointerCapture" in source
    assert "holding-card dragging" in source
    assert "touch-action: none" in styles


def test_holding_deck_has_a_bounded_checkbox_picker_and_ledger_scroll_region() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (app_root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "HoldingPicker" in source
    assert 'type="checkbox"' in source
    assert "appendHoldingCards" in source
    assert "从持仓账本导入" in source
    assert 'className="holding-card-add"' in source
    assert ".table-wrap:has(> .holdings-table)" in styles
    assert "position: sticky" in styles


def test_user_and_provider_text_is_kept_inside_its_surface() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (app_root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert 'className="note-detail-content"' in source
    assert 'className="quoted-text"' in source
    assert ".note-detail-content" in styles
    assert ".quoted-text" in styles
    assert "overflow-wrap: anywhere" in styles
    assert "white-space: pre-wrap" in styles


def test_assistant_hides_engineering_ids_and_excerpt_keeps_the_user_question() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")

    assert "cleanAssistantText" in source
    assert "<RichText text={event.payload.content}" in source
    assert "问题\\n${question}" in source
    assert "证据：${event.payload.cited_evidence_ids" not in source
    assert "每个问题都从当前标的证据重新出发" in source
    assert "清空对话" in source


def test_page_switch_keeps_cached_data_and_new_visuals_stay_inside_existing_surfaces() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (app_root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert "previousPage.current === active" not in source
    assert "页面自动刷新未完成" not in source
    assert 'refreshScope("a_share")' in source
    assert 'refreshScope("global")' in source
    assert "本次启动自动刷新已完成" in source
    assert "beforeMarketReport" not in source
    assert "今日持仓涨跌分布" in source
    assert "买入金额前三集中度" in source
    assert "展示指数涨跌广度" in source
    assert "历史财务趋势" in source
    assert "近期单位净值走势" in source
    assert ".portfolio-insight-strip" in styles
    assert ".financial-trend-grid" in styles
    assert ".mini-line-chart" in styles


def test_research_rooms_use_named_surfaces_and_role_avatars() -> None:
    web_root = Path(__file__).parents[1] / "web"
    source = (web_root / "src" / "workbench.tsx").read_text(encoding="utf-8")
    role_ids = {str(role["role_id"]) for role in AI_ROLES if role["role_id"] != "general"}
    avatar_ids = {path.stem for path in (web_root / "public" / "expert-avatars").glob("*.webp")}
    functional_avatar_ids = {
        path.stem for path in (web_root / "public" / "functional-avatars").glob("*.webp")
    }

    assert 'aria-label="大盘议事厅"' in source
    assert 'aria-label="投研大师"' in source
    assert '"投研委员会"' in source
    assert "function RoleAvatar" in source
    assert "EXPERT_AVATAR_BY_IDENTITY" in source
    assert "FUNCTIONAL_AVATAR_BY_IDENTITY" in source
    assert 'aria-label={avatarSrc ? `${name}头像` : undefined}' in source
    assert avatar_ids == role_ids
    assert functional_avatar_ids == {
        "committee",
        "committee-roundtable",
        "coordinator",
        "evidence_researcher",
        "general",
        "market-roundtable",
        "report_editor",
        "risk_manager",
    }
    assert 'identity={titleAvatarIdentity}' in source
    assert 'marketStyle === "committee" ? "market-roundtable" : marketStyle' in source
    assert 'identity={item.name}' in source
    assert 'identity={event.actor_type === "user" ? "vault-owner" : event.actor_id}' in source
    assert 'if (event.event_type.startsWith("reporting")) return "投资经理";' in source
    assert '"research-committee": "committee-roundtable"' in source
    assert '"market-roundtable": "market-roundtable"' in source
    assert 'className="role-roster"' in source
    assert "专家风格行情报告" not in source
    assert "AI 投资委员会" not in source
    assert "AI 研究助手" not in source


def test_today_note_preview_is_clamped_inside_its_grid_cell() -> None:
    styles = (Path(__file__).parents[1] / "web" / "src" / "styles.css").read_text(encoding="utf-8")
    assert ".note-preview-body" in styles
    assert "-webkit-line-clamp: 2" in styles
    assert "text-overflow: ellipsis" in styles


def test_tauri_remote_capability_allows_native_export_and_external_sources() -> None:
    app_root = Path(__file__).parents[1]
    capability = (app_root / "src-tauri" / "capabilities" / "main.json").read_text(encoding="utf-8")
    permission = (app_root / "src-tauri" / "permissions" / "save-export.toml").read_text(encoding="utf-8")
    assert '"allow-save-export"' in capability
    assert 'commands.allow = ["save_export"]' in permission
    assert '"opener:default"' in capability
