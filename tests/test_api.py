import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

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
    second = {**ENTRY, "record_id": "api-fund-001", "idempotency_key": "api:fund-001", "security_id": "CN:SSE:512480:FUND", "quantity": "1000", "cash_amount": "-1471.00"}
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        client.post("/api/ledger/import", json={"entries": [ENTRY, second]})
        for symbol, asset_type, price, trade_date in (("600519", "stock", 1204.98, "2026-07-10"), ("512480", "fund", 1.292, "2026-07-13")):
            client.post("/api/refresh-jobs", json={"kind": "quote", "requested_as_of": trade_date, "payload": {"symbol": symbol, "market": "a", "asset_type": asset_type, "name": symbol, "price": price, "currency": "CNY", "trade_date": trade_date, "source": "tencent_kline", "source_ref": "https://example.test"}})
        assert client.get("/api/bootstrap").json()["report_as_of"] == "2026-07-10"


def test_post_close_archive_reports_the_target_batch_and_partial_holding_coverage(tmp_path: Path, monkeypatch) -> None:
    rows = [
        {"row_id": "current", "symbol": "600519", "asset_type": "a_share", "invested_amount_cny": "1000", "bought_on": "2026-07-13"},
        {"row_id": "stale", "symbol": "002808", "asset_type": "a_share", "invested_amount_cny": "1000", "bought_on": "2026-07-13"},
    ]
    def now() -> datetime:
        return datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def quote(security_id, trade_date):
        if "600519" not in security_id:
            raise ValueError("目标交易日无交易记录")
        return {"symbol": "600519", "market": "a", "asset_type": "stock", "name": "贵州茅台", "price": 1214.88, "currency": "CNY", "trade_date": trade_date.isoformat(), "source": "test", "source_ref": "https://example.test"}

    monkeypatch.setattr("invest_vault.api.fetch_security_historical_close", quote)
    for loader in ("fetch_financial_snapshot", "fetch_company_announcements", "fetch_index_overview", "fetch_lhb", "fetch_industry_money_flow"):
        monkeypatch.setattr(f"invest_vault.api.{loader}", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline")))

    with TestClient(create_app(tmp_path, automatic_updates=True, now_provider=now)) as client:
        assert client.post("/api/holdings", json={"rows": rows}).status_code == 200
        payload = client.get("/api/bootstrap").json()

    assert payload["report_as_of"] == "2026-07-14"
    assert payload["archive_coverage"] == {"target_date": "2026-07-14", "current": 1, "total": 2, "stale": 1}
    assert {item["symbol"]: item["trade_date"] for item in payload["holdings"]} == {"002808": None, "600519": "2026-07-14"}


def test_failed_post_close_quote_is_retried_on_the_next_automatic_check(tmp_path: Path, monkeypatch) -> None:
    def now() -> datetime:
        return datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    attempts = 0

    def quote(security_id, trade_date):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("provider not ready")
        return {"symbol": "600519", "market": "a", "asset_type": "stock", "name": "贵州茅台", "price": 1214.88, "currency": "CNY", "trade_date": trade_date.isoformat(), "source": "test", "source_ref": "https://example.test"}

    monkeypatch.setattr("invest_vault.api.fetch_security_historical_close", quote)
    for loader in ("fetch_financial_snapshot", "fetch_company_announcements", "fetch_index_overview", "fetch_lhb", "fetch_industry_money_flow"):
        monkeypatch.setattr(f"invest_vault.api.{loader}", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline")))

    with TestClient(create_app(tmp_path, automatic_updates=True, now_provider=now)) as client:
        client.post("/api/holdings", json={"rows": [{"row_id": "retry", "symbol": "600519", "asset_type": "a_share", "invested_amount_cny": "1000", "bought_on": "2026-07-13"}]})
        assert client.get("/api/bootstrap").json()["holdings"][0]["trade_date"] == "2026-07-13"
        assert client.get("/api/bootstrap").json()["holdings"][0]["trade_date"] == "2026-07-14"

    assert attempts == 3


def test_incomplete_industry_flow_archive_is_repaired_on_the_next_automatic_check(tmp_path: Path, monkeypatch) -> None:
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

    repaired = {"date": "2026-07-14", "source": "test", "inbound": [{"name": "元件"}], "outbound": [{"name": "半导体"}]}
    monkeypatch.setattr("invest_vault.api.fetch_industry_money_flow", lambda _: repaired)
    for loader in ("fetch_index_overview", "fetch_lhb"):
        monkeypatch.setattr(f"invest_vault.api.{loader}", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline")))

    with TestClient(create_app(tmp_path, automatic_updates=True, now_provider=now)) as client:
        flow = client.get("/api/bootstrap").json()["market"]["industry_flow"]

    assert flow["outbound"] == [{"name": "半导体"}]


def test_user_facing_holding_form_saves_supported_markets_without_inventing_quantity(tmp_path: Path) -> None:
    rows = [
        {"row_id": "row-a", "symbol": "600519", "asset_type": "a_share", "invested_amount_cny": "10000.50", "bought_on": "2026-07-10"},
        {"row_id": "row-fund", "symbol": "512480", "asset_type": "fund", "invested_amount_cny": "8000", "bought_on": "2026-07-09"},
        {"row_id": "row-hk", "symbol": "700", "asset_type": "hk_stock", "invested_amount_cny": "12000", "bought_on": "2026-07-08"},
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
        client.post("/api/holdings", json={"rows": [{"row_id": "export-row", "symbol": "600519", "asset_type": "a_share", "invested_amount_cny": "11821.90", "bought_on": "2026-07-09"}]})
        response = client.get("/api/holdings/export.xlsx")
        assert response.status_code == 200
        assert response.content.startswith(b"PK")
        assert "spreadsheetml" in response.headers["content-type"]
        assert client.get("/api/holdings/export.csv").status_code == 404


def test_holding_form_rejects_invalid_codes_and_non_positive_amounts(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        bad_code = client.post("/api/holdings", json={"rows": [{"row_id": "bad-code", "symbol": "AAPL", "asset_type": "a_share", "invested_amount_cny": "1", "bought_on": "2026-07-10"}]})
        assert bad_code.status_code == 422
        assert "6位数字" in bad_code.json()["detail"]
        bad_amount = client.post("/api/holdings", json={"rows": [{"row_id": "bad-amount", "symbol": "600519", "asset_type": "a_share", "invested_amount_cny": "0", "bought_on": "2026-07-10"}]})
        assert bad_amount.status_code == 422
        unsupported = client.post("/api/holdings", json={"rows": [{"row_id": "unsupported", "symbol": "AAPL", "asset_type": "us_stock", "invested_amount_cny": "1", "bought_on": "2026-07-10"}]})
        assert unsupported.status_code == 422


def test_holding_form_batch_rolls_back_when_a_row_conflicts(tmp_path: Path) -> None:
    original = {"row_id": "same-row", "symbol": "600519", "asset_type": "a_share", "invested_amount_cny": "1000", "bought_on": "2026-07-10"}
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        assert client.post("/api/holdings", json={"rows": [original]}).status_code == 200
        response = client.post("/api/holdings", json={"rows": [
            {"row_id": "new-row", "symbol": "512480", "asset_type": "fund", "invested_amount_cny": "2000", "bought_on": "2026-07-09"},
            {**original, "invested_amount_cny": "9999"},
        ]})
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


def test_holding_note_and_thesis_can_be_corrected_or_deleted_without_mutating_history(tmp_path: Path) -> None:
    security_id = "CN:SSE:600519:STOCK"
    row = {"row_id": "editable", "symbol": "600519", "asset_type": "a_share", "invested_amount_cny": "10000", "bought_on": "2026-07-09"}
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        assert client.post("/api/holdings", json={"rows": [row]}).status_code == 200
        entry = client.get("/api/bootstrap").json()["holding_entries"][0]
        revised = {**row, "invested_amount_cny": "12000", "bought_on": "2026-07-10"}
        assert client.put(f"/api/holdings/{entry['holding_id']}", json=revised).status_code == 200
        assert client.get("/api/bootstrap").json()["holdings"][0]["invested_amount_cny"] == "12000"

        note_id = client.post("/api/research/notes", json={"security_id": security_id, "body": "旧笔记"}).json()["note_id"]
        assert client.put(f"/api/research/notes/{note_id}", json={"security_id": security_id, "body": "新笔记"}).status_code == 200
        assert client.get(f"/api/research/{security_id}").json()["notes"][0]["body"] == "新笔记"
        assert client.delete(f"/api/research/notes/{note_id}").status_code == 200
        assert client.get(f"/api/research/{security_id}").json()["notes"] == []

        thesis = client.post("/api/research/theses", json={"security_id": security_id, "body": "旧观点"}).json()
        revised_thesis = client.post("/api/research/theses", json={"security_id": security_id, "thesis_id": thesis["thesis_id"], "body": "新观点"})
        assert revised_thesis.json()["revision_number"] == 2
        assert client.delete(f"/api/research/theses/{thesis['thesis_id']}").status_code == 200
        assert client.get(f"/api/research/{security_id}").json()["thesis"] is None

        assert client.delete(f"/api/holdings/{entry['holding_id']}").status_code == 200
        assert client.get("/api/bootstrap").json()["holdings"] == []


def test_profit_is_estimated_from_exact_purchase_close_without_inventing_quantity(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, automatic_updates=False)) as client:
        client.post("/api/holdings", json={"rows": [{"row_id": "profit", "symbol": "600519", "asset_type": "a_share", "invested_amount_cny": "10000", "bought_on": "2026-07-09"}]})
        for trade_date, price in (("2026-07-09", 1000), ("2026-07-13", 1100)):
            client.post("/api/refresh-jobs", json={"kind": "quote", "requested_as_of": trade_date, "payload": {"symbol": "600519", "market": "a", "asset_type": "stock", "name": "贵州茅台", "price": price, "currency": "CNY", "trade_date": trade_date, "source": "test", "source_ref": "https://example.test"}})
        holding = client.get("/api/bootstrap").json()["holdings"][0]
        assert holding["quantity"] is None
        assert holding["estimated_profit_cny"] == 1000
        assert holding["estimated_profit_percent"] == 10
        assert holding["profit_basis"] == "按买入日收盘价估算"


def test_invest_vault_has_no_source_project_dependency() -> None:
    app_root = Path(__file__).parents[1]
    pyproject = (app_root / "pyproject.toml").read_text(encoding="utf-8")
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (app_root / "src" / "invest_vault").glob("*.py")
    )
    assert "stock-analysis" not in pyproject
    assert "stock_analysis" not in source
    assert "invest_vault_contract" not in source


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
    assert "replaceTrailingCards" in source
    assert "选择展示持仓" in source
    assert ".table-wrap:has(> .holdings-table)" in styles
    assert "position: sticky" in styles


def test_user_and_provider_text_is_kept_inside_its_surface() -> None:
    app_root = Path(__file__).parents[1]
    source = (app_root / "web" / "src" / "workbench.tsx").read_text(encoding="utf-8")
    styles = (app_root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

    assert 'className="user-text"' in source
    assert 'className="quoted-text"' in source
    assert ".user-text" in styles
    assert ".quoted-text" in styles
    assert "overflow-wrap: anywhere" in styles
    assert "white-space: pre-wrap" in styles


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
