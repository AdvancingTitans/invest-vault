import json
from pathlib import Path

from invest_vault.ai_roles import AI_ROLES
from invest_vault.ai_skills import (
    FRAMEWORK_REQUIREMENTS,
    FRAMEWORK_SKILLS,
    AppResearchSkillLayer,
    _compact_upstream_pack,
    build_historical_valuation_series,
    build_peer_basket_history,
)
from invest_vault.ledger import HoldingRecord, Vault


def test_fund_skill_builds_industry_concentration_and_disclosed_rebalance(
    tmp_path: Path, monkeypatch
) -> None:
    security_id = "CN:SSE:512480:FUND"
    payload = {
        "holdings_periods": [
            {
                "period": "2026Q1",
                "as_of": "2026-03-31",
                "holdings": [
                    {"code": "600001", "name": "甲", "weight_percent": 8.0},
                    {"code": "600002", "name": "乙", "weight_percent": 5.0},
                ],
            },
            {
                "period": "2025Q4",
                "as_of": "2025-12-31",
                "holdings": [
                    {"code": "600001", "name": "甲", "weight_percent": 6.0},
                    {"code": "600003", "name": "丙", "weight_percent": 4.0},
                ],
            },
        ]
    }
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.connection.execute(
            "INSERT INTO fund_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            ("snapshot", security_id, "2026-07-17", "fixture", json.dumps(payload), "2026-07-17T00:00:00Z"),
        )
        vault.connection.commit()
        monkeypatch.setattr(
            "invest_vault.ai_skills.fetch_stock_industry",
            lambda symbol: {"industry": "行业A" if symbol == "600001" else "行业B"},
        )

        result = AppResearchSkillLayer(vault).run(
            security_id=security_id,
            question="最新持仓、行业集中度与调仓记录",
        )[0]

    value = result["evidence"][0]["value"]
    assert result["status"] == "completed"
    assert value["previous_period"] == "2025Q4"
    assert value["industry_weight_percent"] == {"行业A": 8.0, "行业B": 5.0}
    assert {item["code"]: item["status"] for item in value["quarterly_changes"]} == {
        "600001": "changed",
        "600002": "added",
        "600003": "removed",
    }


def test_company_financial_skill_builds_single_quarters_and_year_cashflow_bridge(
    tmp_path: Path, monkeypatch
) -> None:
    payload = {
        "security_id": "CN:SSE:600519:STOCK",
        "symbol": "600519",
        "source": "fixture",
        "periods": [
            {"period": "2024-12-31", "operating_cash_flow": 120, "capex_cash_paid": 20, "free_cash_flow": 100, "revenue": 200, "parent_net_profit": 80, "net_cash_invest": -10, "net_cash_finance": -30,
             "inventory": 30, "accounts_receivable": 20, "accounts_payable": 10, "contract_liabilities": 5},
            {"period": "2025-03-31", "operating_cash_flow": 30, "capex_cash_paid": 5, "free_cash_flow": 25, "revenue": 50, "parent_net_profit": 20, "net_cash_invest": -2, "net_cash_finance": -5},
            {"period": "2025-06-30", "operating_cash_flow": 70, "capex_cash_paid": 12, "free_cash_flow": 58, "revenue": 110, "parent_net_profit": 42, "net_cash_invest": -8, "net_cash_finance": -12},
            {"period": "2025-09-30", "operating_cash_flow": 65, "capex_cash_paid": 18, "free_cash_flow": 47, "revenue": 160, "parent_net_profit": 60, "net_cash_invest": -15, "net_cash_finance": -20},
            {"period": "2025-12-31", "operating_cash_flow": 90, "capex_cash_paid": 30, "free_cash_flow": 60, "revenue": 220, "parent_net_profit": 85, "net_cash_invest": -25, "net_cash_finance": -40,
             "inventory": 35, "accounts_receivable": 24, "accounts_payable": 13, "contract_liabilities": 7},
        ],
    }
    monkeypatch.setattr("invest_vault.ai_skills.fetch_financial_snapshot", lambda *_: payload)
    with Vault(tmp_path / "vault.sqlite3") as vault:
        result = AppResearchSkillLayer(vault).run(
            security_id="CN:SSE:600519:STOCK",
            question="2025年现金流下降的结构性原因与季度拆分",
        )[0]

    value = result["evidence"][0]["value"][0]
    quarters = {row["period"]: row for row in value["single_quarters"]}
    assert quarters["2025-09-30"]["operating_cash_flow"] == -5
    assert quarters["2025-12-31"]["operating_cash_flow"] == 25
    assert value["cashflow_year_bridge"]["changes"]["operating_cash_flow"] == -30
    assert value["working_capital_bridge"]["estimated_cash_effect"] == -4
    assert "不足以单独证明" in value["cashflow_year_bridge"]["interpretation_boundary"]


def test_portfolio_skill_reports_cost_weights_and_only_valid_correlations(tmp_path: Path, monkeypatch) -> None:
    histories = {
        "CN:SSE:600519:STOCK": [{"date": f"2026-01-{index:02d}", "close": 100 + index} for index in range(1, 32)]
        + [{"date": f"2026-02-{index:02d}", "close": 131 + index} for index in range(1, 31)],
        "HK:HKEX:00700:STOCK": [{"date": f"2026-01-{index:02d}", "close": 200 + index * 2} for index in range(1, 32)]
        + [{"date": f"2026-02-{index:02d}", "close": 262 + index * 2} for index in range(1, 31)],
    }
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_price_history",
        lambda security_id, **_: {"rows": histories[security_id]},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_cny_exchange_rate",
        lambda currency, on_date=None: {"currency": currency, "rate": 0.9, "as_of": str(on_date), "source": "fixture"},
    )
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.import_holdings([
            HoldingRecord("a", "CN:SSE:600519:STOCK", "a_share", "600", "2026-01-01"),
            HoldingRecord("b", "HK:HKEX:00700:STOCK", "hk_stock", "400", "2026-01-01"),
        ])
        result = AppResearchSkillLayer(vault).run(
            security_id="CN:SSE:600519:STOCK", question="当前组合持仓比例和与其他资产相关性"
        )[0]

    value = result["evidence"][0]["value"]
    assert value["selected_weight_percent"] == 60.0
    assert value["hhi"] == 0.52
    assert value["correlations"][0]["overlap_samples"] == 60
    assert value["correlations"][0]["correlation"] is not None
    assert value["drawdown_contribution_status"] == "available_market_value_weight_proxy"
    assert value["drawdown_contribution_proxy"]["analysis_start"] == "2026-01-01"
    assert {item["security_id"] for item in value["drawdown_contribution_proxy"]["contributions"]} == {
        "CN:SSE:600519:STOCK",
        "HK:HKEX:00700:STOCK",
    }


def test_portfolio_skill_separates_close_estimate_cash_and_user_risk_threshold(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_price_history",
        lambda *_, **__: {"rows": [
            *[{"date": f"2026-01-{day:02d}", "close": 99 + day} for day in range(1, 32)],
            *[{"date": f"2026-02-{day:02d}", "close": 130 + day} for day in range(1, 18)],
        ]},
    )
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.import_holdings([HoldingRecord("a", "CN:SSE:600519:STOCK", "a_share", "1000", "2026-01-01")])
        vault.set_portfolio_profile(cash_balance_cny="500", max_drawdown_percent="12.5")
        vault.connection.execute(
            "INSERT INTO market_snapshots VALUES (?, ?, ?, ?, ?)",
            (
                "live_quote:CN:SSE:600519:STOCK",
                "2026-02-18",
                "fixture",
                json.dumps({"price": 150, "previous_close": 147, "pe_ttm": 20, "pb": 6, "market_cap_100m": 20000, "trade_date": "2026-02-18", "data_session": "盘中", "source": "fixture"}),
                "2026-02-18T03:00:00Z",
            ),
        )
        vault.connection.commit()
        result = AppResearchSkillLayer(vault).run(
            security_id="CN:SSE:600519:STOCK",
            question="实时持仓市值、成本、现金比例和可承受回撤阈值",
        )[0]

    value = result["evidence"][0]["value"]
    assert value["cost_basis_cny"] == 1000
    assert value["ledger_entries"][0]["quantity"] == 10.0
    assert value["ledger_entries"][0]["quantity_status"] == "derived_from_purchase_close"
    assert value["market_value_status"] == "available_derived_quantity_estimate"
    assert value["estimated_market_value_cny"] == 1500.0
    assert value["estimated_daily_profit_cny"] == 30.0
    assert value["valuation_as_of"]["CN:SSE:600519:STOCK"]["session"] == "盘中"
    assert value["holding_valuations"]["CN:SSE:600519:STOCK"]["price"] == 150
    assert value["holding_valuations"]["CN:SSE:600519:STOCK"]["pe_ttm"] == 20
    assert value["drawdown_contribution_status"] == "available_derived_quantity_proxy"
    assert value["cash_ratio_status"] == "available"
    assert value["cash_ratio_percent"] == 25.0
    assert value["drawdown_threshold_status"] == "available_user_defined"
    assert value["max_drawdown_percent"] == 12.5


def test_hk_holding_uses_buy_date_and_current_fx_for_quantity_market_value_and_pnl(
    tmp_path: Path, monkeypatch
) -> None:
    security_id = "HK:HKEX:00700:STOCK"
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_price_history",
        lambda *_, **__: {"rows": [
            {"date": "2026-07-07", "close": 500},
            {"date": "2026-07-17", "close": 520},
            {"date": "2026-07-18", "close": 525},
        ]},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_cny_exchange_rate",
        lambda currency, on_date=None: {
            "currency": currency,
            "rate": 0.91 if str(on_date) == "2026-07-07" else 0.93,
            "as_of": str(on_date or "2026-07-18"),
            "source": "fixture",
        },
    )
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.import_holdings([
            HoldingRecord("tencent", security_id, "hk_stock", "30030", "2026-07-07")
        ])
        result = AppResearchSkillLayer(vault)._portfolio_risk(security_id)

    value = result["evidence"][0]["value"]
    entry = value["ledger_entries"][0]
    assert entry["quantity"] == 66.0
    assert entry["quantity_status"] == "derived_from_purchase_close_and_fx"
    assert entry["purchase_fx_cny_per_unit"] == 0.91
    assert value["market_values_cny"][security_id] == 32224.5
    assert value["daily_profit_cny"][security_id] == 306.9
    assert value["holding_market_weights_percent"][security_id] == 100.0


def test_fund_evidence_reports_overlap_with_direct_holdings(tmp_path: Path, monkeypatch) -> None:
    fund_id = "CN:SSE:519771:FUND"
    payload = {
        "name": "交银优择回报灵活配置混合C",
        "holdings_periods": [{
            "period": "2026Q1",
            "as_of": "2026-03-31",
            "holdings": [
                {"code": "600519", "name": "贵州茅台", "weight_percent": 8.0},
                {"code": "00700", "name": "腾讯控股", "weight_percent": 5.0},
            ],
        }],
    }
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_stock_industry", lambda *_: {"industry": "消费"}
    )
    monkeypatch.setattr("invest_vault.ai_skills.build_fund_evidence", lambda *_: {"modules": {}})
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.import_holdings([
            HoldingRecord("fund", fund_id, "fund", "10000", "2026-01-02"),
            HoldingRecord("stock", "CN:SSE:600519:STOCK", "a_share", "20000", "2026-01-02"),
            HoldingRecord("hk", "HK:HKEX:00700:STOCK", "hk_stock", "30000", "2026-01-02"),
        ])
        result = AppResearchSkillLayer(vault)._fund_portfolio(fund_id, payload)

    value = result["evidence"][0]["value"]
    assert value["direct_holding_overlap_weight_percent"] == 13.0
    assert {row["code"] for row in value["direct_holding_overlap"]} == {"600519", "00700"}


def test_company_financial_evidence_inherits_stock_analysis_c1_to_c8_pack(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_financial_snapshot",
        lambda *_: {"symbol": "600519", "name": "贵州茅台", "source": "fixture", "periods": []},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.build_company_evidence",
        lambda *_: {"modules": {f"C{i}": {"available": True, "evidence": [], "gaps": []} for i in range(1, 9)}},
    )
    with Vault(tmp_path / "vault.sqlite3") as vault:
        results = AppResearchSkillLayer(vault).run(
            security_id="CN:SSE:600519:STOCK", question="完整三表、ROIC、管理层、估值与安全边际", role_id="buffett"
        )

    financial = next(item for item in results if item["skill_id"] == "company-financial-quality")
    inherited = financial["evidence"][0]["value"][0]["stock_analysis_evidence_pack"]
    assert set(inherited["modules"]) == {f"C{i}" for i in range(1, 9)}


def test_upstream_pack_compaction_keeps_every_evidence_item_and_audit_metadata() -> None:
    evidence = [
        {
            "metric": f"metric_{index}",
            "value": index,
            "period": "2025-12-31",
            "source": "fixture",
            "source_type": "primary",
            "confidence": "primary",
            "unused_payload": "x" * 200,
        }
        for index in range(18)
    ]
    compact = _compact_upstream_pack({
        "schema_version": "1.1",
        "symbol": "600519",
        "modules": {"C5": {"available": True, "gaps": [], "evidence": evidence}},
        "_meta": {"coverage": 100.0, "source_events": [{"source": "fixture", "status": "ok"}]},
    })

    module = compact["modules"]["C5"]
    assert module["evidence_count"] == 18
    assert len(module["evidence"]) == 18
    assert module["evidence"][-1]["metric"] == "metric_17"
    assert "unused_payload" not in module["evidence"][0]
    assert compact["source_events"] == [{"source": "fixture", "status": "ok"}]


def test_general_deep_stock_review_inherits_complete_c1_to_c8_pack(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_financial_snapshot",
        lambda *_: {"symbol": "600519", "name": "贵州茅台", "source": "fixture", "periods": []},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.build_company_evidence",
        lambda *_: {"modules": {f"C{i}": {"available": True, "evidence": [], "gaps": []} for i in range(1, 9)}},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_trading_history",
        lambda *_, **__: {"rows": []},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_price_history",
        lambda *_, **__: {"rows": []},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_index_overview",
        lambda *_, **__: {"date": "2026-07-17", "rows": []},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_valuation",
        lambda *_: {"as_of": "2026-07-17", "pe_ttm": 20, "pb": 6},
    )
    monkeypatch.setattr("invest_vault.ai_skills.fetch_profit_forecast", lambda *_: {})
    monkeypatch.setattr("invest_vault.ai_skills.fetch_peer_valuations", lambda *_: {"rows": []})
    monkeypatch.setattr("invest_vault.ai_skills.fetch_company_supplemental_evidence", lambda *_ , **__: {})
    with Vault(tmp_path / "vault.sqlite3") as vault:
        results = AppResearchSkillLayer(vault).run(
            security_id="CN:SSE:600519:STOCK",
            question="深度复盘这只股票的商业质量、估值和风险",
            role_id="general",
        )

    financial = next(item for item in results if item["skill_id"] == "company-financial-quality")
    inherited = financial["evidence"][0]["value"][0]["stock_analysis_evidence_pack"]
    assert set(inherited["modules"]) == {f"C{i}" for i in range(1, 9)}


def test_stock_review_always_carries_complete_local_holding_ledger(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_trading_history",
        lambda *_, **__: {"rows": [{"date": "2026-07-17", "close": 100, "volume": 10}]},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_price_history",
        lambda *_, **__: {"rows": [{"date": "2026-07-16", "close": 99}, {"date": "2026-07-17", "close": 100}]},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_index_overview",
        lambda *_, **__: {"date": "2026-07-17", "rows": []},
    )
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.import_holdings([
            HoldingRecord("a", "CN:SSE:600519:STOCK", "a_share", "12000", "2026-01-08"),
            HoldingRecord("b", "CN:SSE:512480:FUND", "fund", "8000", "2026-02-09"),
        ])

        results = AppResearchSkillLayer(vault).run(
            security_id="CN:SSE:600519:STOCK", question="复盘这只股票"
        )

    portfolio = next(item for item in results if item["skill_id"] == "portfolio-risk-evidence")
    value = portfolio["evidence"][0]["value"]
    assert value["ledger_completeness"] == "complete_for_cost_weight_analysis"
    assert [item["security_id"] for item in value["ledger_entries"]] == [
        "CN:SSE:600519:STOCK", "CN:SSE:512480:FUND",
    ]
    assert all(item["quantity_status"] == "unavailable_without_exact_purchase_price_or_fx" for item in value["ledger_entries"])
    assert "完整持仓" not in "；".join(portfolio["gaps"])


def test_portfolio_evidence_uses_security_name_and_code_consistently(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_price_history",
        lambda *_, **__: {"rows": [{"date": "2026-07-16", "close": 99}, {"date": "2026-07-17", "close": 100}]},
    )
    security_id = "CN:SZSE:301520:STOCK"
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.import_holdings([
            HoldingRecord("medical", security_id, "a_share", "10000", "2026-07-16"),
        ])
        vault.connection.execute(
            "INSERT INTO market_snapshots VALUES (?, ?, ?, ?, ?)",
            (
                f"live_quote:{security_id}",
                "2026-07-17",
                "fixture",
                json.dumps({"name": "万邦医药", "price": 100, "trade_date": "2026-07-17"}),
                "2026-07-17T08:00:00Z",
            ),
        )
        vault.connection.commit()
        portfolio = AppResearchSkillLayer(vault)._portfolio_risk("MARKET:GLOBAL:OVERVIEW")

    value = portfolio["evidence"][0]["value"]
    assert value["ledger_entries"][0]["name"] == "万邦医药"
    assert value["ledger_entries"][0]["symbol"] == "301520"
    assert value["ledger_entries"][0]["display_name"] == "万邦医药（301520）"
    assert value["holding_identities"][security_id]["display_name"] == "万邦医药（301520）"
    assert value["holding_weights"][0]["display_name"] == "万邦医药（301520）"
    assert value["holding_valuations"][security_id]["display_name"] == "万邦医药（301520）"


def test_market_overview_scene_uses_all_market_sections_and_local_holdings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_price_history",
        lambda *_, **__: {"rows": [{"date": "2026-07-16", "close": 99}, {"date": "2026-07-17", "close": 100}]},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_a_share_market_breadth",
        lambda *_args, **_kwargs: {"available": True, "up": 3200, "down": 1800, "flat": 120, "ratio": 1.7778, "scope": "A股全市场个股"},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_global_index_price_volume",
        lambda **_kwargs: {"sh000001": {"sample_count": 80, "return_5d_percent": 1.2, "return_20d_percent": 3.4, "return_60d_percent": 5.6, "volume_zscore": 0.8}},
    )
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.import_holdings([
            HoldingRecord("a", "CN:SSE:600519:STOCK", "a_share", "10000", "2026-01-08")
        ])
        for section, payload in {
            "indices": {"date": "2026-07-17", "session": "盘后", "session_label": "7月17日盘后收盘数据", "rows": [{"name": "上证指数", "change_percent": 1.2}]},
            "lhb": {"date": "2026-07-17", "rows": [{"name": "甲公司", "net_amount": 3}]},
            "industry_flow": {"date": "2026-07-17", "inbound": [{"name": "医药", "net_amount": 5}], "outbound": []},
        }.items():
            vault.connection.execute(
                "INSERT INTO market_snapshots VALUES (?, ?, ?, ?, ?)",
                (section, "2026-07-17", "fixture", json.dumps(payload), "2026-07-17T08:00:00Z"),
            )
        vault.connection.commit()

        results = AppResearchSkillLayer(vault).run(
            security_id="MARKET:GLOBAL:OVERVIEW",
            question="生成最新交易日盘前大盘行情报告并结合我的持仓给出下一步建议",
        )

    by_id = {item["skill_id"]: item for item in results}
    market = by_id["market-context-evidence"]["evidence"][0]["value"]
    holdings = by_id["portfolio-risk-evidence"]["evidence"][0]["value"]
    assert market["scene"] == "market_overview"
    assert market["session_label"] == "7月17日盘后收盘数据"
    assert market["requested_session"] == "盘前"
    assert market["session_mismatch"] is True
    assert market["major_indices"][0]["name"] == "上证指数"
    assert market["dragon_tiger"][0]["name"] == "甲公司"
    assert market["industry_flow"]["inbound"][0]["name"] == "医药"
    assert market["market_breadth"]["up"] == 3200
    assert market["continuous_price_volume"]["sh000001"]["sample_count"] == 80
    assert holdings["ledger_completeness"] == "complete_for_cost_weight_analysis"


def test_moutai_requested_evidence_distinguishes_unpublished_and_non_verifiable_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_financial_snapshot",
        lambda *_: {
            "security_id": "CN:SSE:600519:STOCK", "symbol": "600519", "name": "贵州茅台",
            "source": "fixture", "periods": [{"period": "2026-03-31", "inventory": 60, "accounts_receivable": 1,
                                                 "accounts_payable": 4, "contract_liabilities": 80}],
        },
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_valuation",
        lambda *_: {"name": "贵州茅台", "pe_ttm": 19, "pb": 6.7, "as_of": "2026-07-17"},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_public_news",
        lambda keyword, **_: {"keyword": keyword, "items": [{"title": keyword, "url": "https://example.test"}]},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_trading_history",
        lambda *_, **__: (_ for _ in ()).throw(ValueError("fixture无历史行情")),
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_profit_forecast",
        lambda *_: (_ for _ in ()).throw(ValueError("fixture无一致预测")),
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_peer_valuations",
        lambda *_: (_ for _ in ()).throw(ValueError("fixture无可比公司")),
    )
    with Vault(tmp_path / "vault.sqlite3") as vault:
        results = AppResearchSkillLayer(vault).run(
            security_id="CN:SSE:600519:STOCK",
            question="检查2026年半年报合同负债、存货、应收应付，飞天茅台批价连续序列、渠道库存动销回款、消费税正式政策和历史PE PB分位与一致预期",
        )

    by_id = {result["skill_id"]: result for result in results}
    assert any("2026年半年报尚未进入" in gap for gap in by_id["company-financial-quality"]["gaps"])
    assert any("历史估值计算暂不可得" in gap for gap in by_id["security-valuation-evidence"]["gaps"])
    assert any("连续可核验批价" in gap for gap in by_id["public-topic-evidence"]["gaps"])
    assert any("消费税正式政策原文" in gap for gap in by_id["public-topic-evidence"]["gaps"])


def test_historical_valuation_is_calculated_from_disclosure_date_closes() -> None:
    financial = {"periods": [
        {"period": "2025-12-31", "notice_date": "2026-03-30", "basic_eps": 2.0, "bps": 10.0},
        {"period": "2026-03-31", "notice_date": "2026-04-28", "basic_eps": 0.6, "bps": 10.5},
    ]}
    prices = [
        {"date": "2026-03-30", "close": 20.0},
        {"date": "2026-04-28", "close": 21.0},
    ]

    result = build_historical_valuation_series(financial, prices)

    assert result[0]["pe_annualized"] == 10.0
    assert result[1]["pe_annualized"] == 8.75
    assert result[1]["pb_reported_bps"] == 2.0
    assert result[1]["price_date"] == "2026-04-28"


def test_peer_basket_builds_a_reproducible_sector_history_proxy() -> None:
    histories = [
        {"symbol": "A", "rows": [{"date": "2026-01-01", "close": 10, "volume": 100},
                                  {"date": "2026-01-02", "close": 11, "volume": 120}]},
        {"symbol": "B", "rows": [{"date": "2026-01-01", "close": 20, "volume": 200},
                                  {"date": "2026-01-02", "close": 18, "volume": 220}]},
    ]

    result = build_peer_basket_history(histories)

    assert result[0]["close"] == 100.0
    assert result[1]["close"] == 100.0
    assert result[1]["volume"] == 340.0


def test_stock_review_automatically_collects_forecast_peer_and_qualitative_sources(tmp_path: Path, monkeypatch) -> None:
    financial = {
        "security_id": "CN:SSE:600519:STOCK", "symbol": "600519", "name": "医药公司", "source": "fixture",
        "periods": [{"period": "2025-12-31", "notice_date": "2026-03-30", "basic_eps": 2.0, "bps": 10.0,
                     "revenue": 100, "parent_net_profit": 10, "roe": 12, "gross_margin": 60,
                     "debt_asset_ratio": 20, "operating_cash_flow": 12, "free_cash_flow": 8}],
    }
    monkeypatch.setattr("invest_vault.ai_skills.fetch_financial_snapshot", lambda *_: financial)
    monkeypatch.setattr("invest_vault.ai_skills.fetch_security_valuation", lambda *_: {
        "name": "医药公司", "pe_ttm": 20, "pb": 2, "price": 20, "as_of": "2026-07-20",
    })
    monkeypatch.setattr("invest_vault.ai_skills.fetch_security_trading_history", lambda *_, **__: {
        "rows": [{"date": "2026-03-30", "close": 20, "volume": 100}] * 61,
        "source": "fixture", "source_ref": "https://example.test/history",
    })
    monkeypatch.setattr("invest_vault.ai_skills.fetch_profit_forecast", lambda *_: {
        "consensus": [{"year": 2026, "eps": 2.4, "eps_growth_percent": 20}],
        "revision_history": [{"publish_date": "2026-07-10", "institution": "甲证券", "forecasts": []}],
        "coverage": {"institutions": 1}, "source": "东方财富F10盈利预测", "source_ref": "https://example.test/forecast",
    })
    monkeypatch.setattr("invest_vault.ai_skills.fetch_peer_valuations", lambda *_: {
        "status": "provisional_requires_user_confirmation", "rows": [{"symbol": "600000", "pe_ttm": 18, "pb": 1.8}],
        "source": "东方财富行业成分", "source_ref": "https://example.test/peers",
    })
    monkeypatch.setattr("invest_vault.ai_skills.fetch_company_supplemental_evidence", lambda *_, **__: {
        "official_sections": [{"topic": "主营业务与毛利率", "items": [{"title": "分业务毛利率"}]}],
        "topic_searches": [{"topic": "收购与商誉", "items": [{"title": "收购公告", "url": "https://example.test/acquisition"}]}],
        "source": "公司F10与公开资料", "source_ref": "https://example.test/company",
    })
    monkeypatch.setattr("invest_vault.ai_skills.fetch_stock_industry", lambda *_: {"industry": "医药", "classification_rows": []})
    with Vault(tmp_path / "vault.sqlite3") as vault:
        results = AppResearchSkillLayer(vault).run(
            security_id="CN:SSE:600519:STOCK",
            question="深度复盘收购、客户、订单、毛利率、营运资本、资本开支、管理层与行业量价",
            role_id="buffett",
        )

    by_id = {result["skill_id"]: result for result in results}
    valuation = by_id["security-valuation-evidence"]["evidence"][0]["value"]
    assert valuation["historical_series"][0]["pe_annualized"] == 10.0
    assert valuation["consensus_forecast"]["coverage"]["institutions"] == 1
    assert valuation["peer_valuations"]["status"] == "provisional_requires_user_confirmation"
    assert by_id["supplemental-company-evidence"]["status"] == "completed"


def test_role_routes_required_evidence_without_question_keywords(tmp_path: Path, monkeypatch) -> None:
    financial = {
        "security_id": "CN:SSE:600519:STOCK",
        "symbol": "600519",
        "source": "fixture",
        "periods": [{
            "period": "2025-12-31", "revenue": 100, "parent_net_profit": 50,
            "roe": 20, "gross_margin": 80, "debt_asset_ratio": 15,
            "operating_cash_flow": 60, "free_cash_flow": 55,
        }],
    }
    monkeypatch.setattr("invest_vault.ai_skills.fetch_financial_snapshot", lambda *_: financial)
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_valuation",
        lambda *_: {"name": "贵州茅台", "pe_ttm": 19.0, "pb": 6.7, "as_of": "2026-07-17"},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_trading_history",
        lambda *_, **__: {
            "rows": [
                {"date": f"2026-04-{index:02d}", "open": 99 + index, "close": 100 + index,
                 "high": 101 + index, "low": 98 + index, "volume": 1000 + index * 10}
                for index in range(1, 29)
            ]
            + [
                {"date": f"2026-05-{index:02d}", "open": 127 + index, "close": 128 + index,
                 "high": 129 + index, "low": 126 + index, "volume": 1300 + index * 10}
                for index in range(1, 32)
            ]
            + [
                {"date": f"2026-06-{index:02d}", "open": 158 + index, "close": 159 + index,
                 "high": 160 + index, "low": 157 + index, "volume": 1700 + index * 10}
                for index in range(1, 5)
            ],
            "as_of": "2026-06-04",
            "source": "fixture",
            "source_ref": "https://example.test/history",
        },
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_index_overview",
        lambda *_, **__: {"date": "2026-07-17", "rows": [{"name": "上证指数", "change_percent": 1.0, "volume": 10}], "source": "fixture"},
    )
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_stock_industry",
        lambda *_: {"industry": "白酒", "classifications": [{"name": "白酒", "change_percent": 2.0}]},
    )
    with Vault(tmp_path / "vault.sqlite3") as vault:
        results = AppResearchSkillLayer(vault).run(
            security_id="CN:SSE:600519:STOCK", question="你怎么看？", role_id="buffett"
        )

    ids = {result["skill_id"] for result in results}
    assert {"company-financial-quality", "security-valuation-evidence", "market-context-evidence", "framework-readiness"} <= ids
    market = next(result for result in results if result["skill_id"] == "market-context-evidence")
    assert market["evidence"][0]["value"]["security_price_volume"]["volume_zscore"] is not None
    readiness = next(result for result in results if result["skill_id"] == "framework-readiness")
    assert readiness["evidence"][0]["value"]["role_id"] == "buffett"
    assert "财务质量与现金创造" in readiness["evidence"][0]["value"]["available"]


def test_all_fifteen_expert_roles_have_complete_routing_and_readiness_contracts() -> None:
    role_ids = {str(role["role_id"]) for role in AI_ROLES if role["role_id"] != "general"}

    assert role_ids == set(FRAMEWORK_SKILLS) - {"general"}
    assert role_ids == set(FRAMEWORK_REQUIREMENTS) - {"general"}
    assert all("market-context-evidence" in FRAMEWORK_SKILLS[role_id] for role_id in role_ids)
    for role_id in role_ids:
        routed = set(FRAMEWORK_SKILLS[role_id])
        required_skills = {
            skill_id
            for _, skill_id, _ in FRAMEWORK_REQUIREMENTS[role_id]
            if skill_id is not None
        }
        assert required_skills <= routed
