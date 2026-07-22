"""Vault adapters that assemble evidence under the bundled stock-analysis contract."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Protocol
from uuid import uuid4

from stock_analysis.company_evidence import build_company_evidence
from stock_analysis.execution_costs import build_execution_cost_model
from stock_analysis.fund_research import build_fund_evidence

from .ledger import Vault
from .providers import (
    fetch_a_share_market_breadth,
    fetch_cny_exchange_rate,
    fetch_company_supplemental_evidence,
    fetch_financial_snapshot,
    fetch_fund_redemption_fee_schedule,
    fetch_fund_snapshot,
    fetch_global_index_price_volume,
    fetch_hk_financial_snapshot,
    fetch_hk_valuation_financial_history,
    fetch_index_overview,
    fetch_peer_valuations,
    fetch_profit_forecast,
    fetch_public_news,
    fetch_sector_price_history,
    fetch_security_price_history,
    fetch_security_trading_history,
    fetch_security_valuation,
    fetch_stock_industry,
    summarize_price_volume_history,
    target_trade_date,
)

A_SHARE_MARKET_OVERVIEW_SECURITY_ID = "MARKET:CN:OVERVIEW"
GLOBAL_MARKET_OVERVIEW_SECURITY_ID = "MARKET:GLOBAL:OVERVIEW"
MARKET_OVERVIEW_SECURITY_IDS = {
    A_SHARE_MARKET_OVERVIEW_SECURITY_ID,
    GLOBAL_MARKET_OVERVIEW_SECURITY_ID,
}
# Compatibility alias for existing imports and archived notes.
MARKET_OVERVIEW_SECURITY_ID = GLOBAL_MARKET_OVERVIEW_SECURITY_ID


def _daily_high_low_spread_bps(rows: list[dict[str, object]]) -> float | None:
    """Estimate a labelled spread proxy when a market has no public live depth feed."""

    clean = [
        (float(row["high"]), float(row["low"]))
        for row in rows[-61:]
        if isinstance(row.get("high"), (int, float))
        and isinstance(row.get("low"), (int, float))
        and float(row["low"]) > 0
        and float(row["high"]) >= float(row["low"])
    ]
    if len(clean) < 20:
        return None
    denominator = 3 - 2 * math.sqrt(2)
    estimates: list[float] = []
    for previous, current in zip(clean, clean[1:]):
        beta = math.log(previous[0] / previous[1]) ** 2 + math.log(current[0] / current[1]) ** 2
        gamma = math.log(max(previous[0], current[0]) / min(previous[1], current[1])) ** 2
        alpha = max(
            0.0,
            (math.sqrt(2 * beta) - math.sqrt(beta)) / denominator
            - math.sqrt(gamma / denominator),
        )
        estimate = 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha)) * 10_000
        if estimate > 0 and math.isfinite(estimate):
            estimates.append(estimate)
    estimates.sort()
    return round(estimates[len(estimates) // 2], 4) if estimates else None


def _execution_price_volume(history: dict[str, object], *, market: str) -> dict[str, object]:
    """Shape public daily history into explicit turnover and volatility model inputs."""

    rows = list(history.get("rows") or [])
    metrics = summarize_price_volume_history(rows)
    closes = [float(row["close"]) for row in rows[-61:] if isinstance(row.get("close"), (int, float))]
    returns = [math.log(current / previous) for previous, current in zip(closes, closes[1:]) if previous > 0]
    if len(returns) >= 20:
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
        metrics["annualized_volatility_60d_pct"] = math.sqrt(variance * 252) * 100
    amounts = [float(row["amount"]) for row in rows[-20:] if isinstance(row.get("amount"), (int, float))]
    liquidity = {}
    if amounts:
        key = "average_turnover_20d_local" if market == "hk" else "average_turnover_20d_cny"
        liquidity[key] = sum(amounts) / len(amounts)
    return {**history, "metrics": metrics, "liquidity": liquidity}


class ResearchSkillLayer(Protocol):
    def catalog(self) -> list[dict[str, str]]: ...

    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]: ...


SKILL_CATALOG = (
    {
        "skill_id": "fund-portfolio-evidence",
        "name": "基金持仓证据",
        "description": "读取定期报告重仓、行业暴露、季度调仓差异和重仓集中度。",
    },
    {
        "skill_id": "fund-liquidity-evidence",
        "name": "基金流动性证据",
        "description": "读取净值、规模变化和可得的申赎状态；真实净申赎不可得时保留缺口。",
    },
    {
        "skill_id": "company-financial-quality",
        "name": "公司财务质量",
        "description": "读取三表并拆分单季度营收、利润、经营现金流、资本开支和融资投资现金流。",
    },
    {
        "skill_id": "drawdown-attribution-readiness",
        "name": "回撤归因准备度",
        "description": "计算可验证净值回撤并检查基本面、估值与流动性归因所需证据是否齐全。",
    },
    {
        "skill_id": "security-valuation-evidence",
        "name": "当前估值证据",
        "description": "读取A股/港股当前PE、PB；基金按已披露重仓股估值计算覆盖口径。",
    },
    {
        "skill_id": "portfolio-risk-evidence",
        "name": "组合风险证据",
        "description": "按用户投入金额计算持仓比例和集中度，并在样本充足时计算历史收益相关性。",
    },
    {
        "skill_id": "public-topic-evidence",
        "name": "公开专题证据",
        "description": "围绕渠道库存、批价、税制和政策等问题检索带时间与链接的公开资讯线索。",
    },
    {
        "skill_id": "market-context-evidence",
        "name": "市场、板块与量价",
        "description": "读取主要指数、相关板块、标的日线成交量及5/20/60日量价统计。",
    },
    {
        "skill_id": "supplemental-company-evidence",
        "name": "公司经营与治理补充证据",
        "description": "自动检索公司分业务毛利率、收购、客户订单、营运资本、资本开支、管理层与资本配置资料。",
    },
    {
        "skill_id": "execution-liquidity-evidence",
        "name": "盘口与系统性流动性证据",
        "description": "读取当前五档盘口与交易成本代理，并逐项区分融资、信用、申赎和历史深度的公开数据边界。",
    },
    {
        "skill_id": "framework-readiness",
        "name": "专家证据覆盖检查",
        "description": "按当前专家逐项标明已取得、条件可用和仍缺少的框架证据。",
    },
)

FUND_PORTFOLIO_TERMS = ("持仓", "重仓", "行业", "集中度", "调仓", "换仓", "仓位")
FUND_LIQUIDITY_TERMS = ("申购", "赎回", "流动性", "规模", "份额", "压力")
FINANCIAL_TERMS = (
    "三表", "经营现金流", "自由现金流", "现金流", "资本开支", "负债", "资产负债率",
    "应收", "存货", "商誉", "roic", "fcf",
)
ALL_HOLDINGS_TERMS = ("各持仓", "全部持仓", "所有持仓", "组合内持仓")
DRAWDOWN_TERMS = ("回撤", "归因", "估值压缩", "基本面", "市场流动性")
VALUATION_TERMS = ("估值", "pe", "pb", "市盈率", "市净率", "安全边际")
PORTFOLIO_TERMS = (
    "组合", "持仓比例", "持仓市值", "仓位", "相关性", "集中度", "hhi", "资产配置",
    "现金比例", "回撤阈值", "可承受回撤", "成本",
)
PUBLIC_TOPIC_TERMS = (
    "渠道库存", "库存", "真实批价", "批价", "消费税", "税制", "政策", "终端需求",
    "动销", "经销商回款", "回款",
)
SUPPLEMENTAL_COMPANY_TERMS = (
    "收购", "交易定价", "业绩承诺", "商誉", "整合成本", "现金回报", "客户集中", "客户留存",
    "在手订单", "价格变化", "毛利率", "应收账款", "合同负债", "应付账款", "存货", "资本开支",
    "管理层", "激励", "核心人员", "资本配置",
)
MARKET_TERMS = ("今日", "行情", "复盘", "大盘", "指数", "板块", "成交量", "量价", "趋势", "均线", "突破", "相对强弱")
DEEP_EVIDENCE_TERMS = ("深度", "复盘", "投委会", "完整报告", "研究报告", "全面分析", "持仓逻辑", "原投资逻辑")

# Vault-specific fetch mapping for the bundled stock-analysis lens requirements.
# Values are local evidence adapters; qualitative gaps stay visible instead of being invented.
FRAMEWORK_SKILLS: dict[str, tuple[str, ...]] = {
    "general": (),
    "buffett": (
        "company-financial-quality", "security-valuation-evidence", "market-context-evidence",
        "supplemental-company-evidence", "execution-liquidity-evidence",
    ),
    "munger": (
        "company-financial-quality", "security-valuation-evidence", "market-context-evidence",
        "supplemental-company-evidence",
    ),
    "graham": (
        "company-financial-quality", "security-valuation-evidence", "market-context-evidence",
        "supplemental-company-evidence",
    ),
    "klarman": (
        "company-financial-quality", "security-valuation-evidence", "market-context-evidence",
        "portfolio-risk-evidence", "supplemental-company-evidence", "execution-liquidity-evidence",
    ),
    "lynch": (
        "company-financial-quality", "security-valuation-evidence", "market-context-evidence",
        "supplemental-company-evidence",
    ),
    "o_neil": (
        "company-financial-quality", "market-context-evidence", "supplemental-company-evidence",
        "execution-liquidity-evidence",
    ),
    "wood": (
        "company-financial-quality", "security-valuation-evidence", "market-context-evidence",
        "portfolio-risk-evidence", "supplemental-company-evidence",
    ),
    "dalio": ("market-context-evidence", "portfolio-risk-evidence", "execution-liquidity-evidence"),
    "soros": ("market-context-evidence", "portfolio-risk-evidence", "execution-liquidity-evidence"),
    "livermore": ("market-context-evidence", "portfolio-risk-evidence", "execution-liquidity-evidence"),
    "minervini": ("company-financial-quality", "market-context-evidence", "execution-liquidity-evidence"),
    "simons": (
        "market-context-evidence", "portfolio-risk-evidence", "security-valuation-evidence",
        "execution-liquidity-evidence",
    ),
    "duan_yongping": (
        "company-financial-quality", "security-valuation-evidence", "market-context-evidence",
        "supplemental-company-evidence",
    ),
    "zhang_kun": (
        "company-financial-quality", "security-valuation-evidence", "portfolio-risk-evidence",
        "market-context-evidence", "supplemental-company-evidence",
    ),
    "feng_liu": (
        "market-context-evidence", "security-valuation-evidence", "portfolio-risk-evidence",
        "supplemental-company-evidence", "execution-liquidity-evidence",
    ),
}

FRAMEWORK_REQUIREMENTS: dict[str, tuple[tuple[str, str | None, str], ...]] = {
    "general": (),
    "buffett": (
        ("财务质量、三表与现金创造", "company-financial-quality", "available"),
        ("估值与安全边际情景", "security-valuation-evidence", "conditional"),
        ("长期量价与市场参照", "market-context-evidence", "available"),
        ("管理层、资本配置与护城河原文", "supplemental-company-evidence", "conditional"),
    ),
    "munger": (
        ("财务质量与现金创造", "company-financial-quality", "available"),
        ("估值与机会成本", "security-valuation-evidence", "conditional"),
        ("市场参照", "market-context-evidence", "available"),
        ("治理、激励与关键反例原文", "supplemental-company-evidence", "conditional"),
    ),
    "graham": (
        ("资产负债表与盈利稳定性", "company-financial-quality", "available"),
        ("历史估值分位与保守价值情景", "security-valuation-evidence", "conditional"),
        ("市场基准与历史波动", "market-context-evidence", "available"),
        ("资产覆盖、盈利韧性与清算情景适用性", "company-financial-quality", "conditional"),
    ),
    "klarman": (
        ("财务质量与资产风险", "company-financial-quality", "available"),
        ("绝对估值与折价情景", "security-valuation-evidence", "conditional"),
        ("盘口、退出成本与系统性流动性", "execution-liquidity-evidence", "conditional"),
        ("可验证催化剂与反方资料", "supplemental-company-evidence", "conditional"),
    ),
    "lynch": (
        ("单季度收入、利润与现金流", "company-financial-quality", "available"),
        ("估值增长匹配", "security-valuation-evidence", "conditional"),
        ("行业与量价验证", "market-context-evidence", "available"),
        ("用户、同店或运营指标原文", "supplemental-company-evidence", "conditional"),
    ),
    "o_neil": (
        ("季度盈利与销售加速", "company-financial-quality", "available"),
        ("指数、行业领导力与量价", "market-context-evidence", "available"),
        ("盘口、机构需求与执行成本", "execution-liquidity-evidence", "conditional"),
    ),
    "wood": (
        ("收入、现金消耗与融资风险", "company-financial-quality", "available"),
        ("远期估值风险", "security-valuation-evidence", "conditional"),
        ("市场与行业参照", "market-context-evidence", "available"),
        ("研发、渗透率与单位经济原文", "supplemental-company-evidence", "conditional"),
    ),
    "dalio": (
        ("主要指数、板块与量价", "market-context-evidence", "available"),
        ("组合权重、集中度与相关性", "portfolio-risk-evidence", "available"),
        ("融资条件、信用与系统性流动性", "execution-liquidity-evidence", "conditional"),
    ),
    "soros": (
        ("价格、板块与市场反馈", "market-context-evidence", "available"),
        ("仓位与相关性风险", "portfolio-risk-evidence", "available"),
        ("资金反馈、盘口与流动性", "execution-liquidity-evidence", "conditional"),
    ),
    "livermore": (
        ("趋势、关键点与成交量", "market-context-evidence", "available"),
        ("仓位与组合风险", "portfolio-risk-evidence", "available"),
        ("盘中确认、盘口与执行成本", "execution-liquidity-evidence", "conditional"),
    ),
    "minervini": (
        ("盈利与销售加速", "company-financial-quality", "available"),
        ("趋势模板、相对强度与成交量", "market-context-evidence", "available"),
        ("VCP、盘口与风险收益比", "execution-liquidity-evidence", "conditional"),
    ),
    "simons": (
        ("历史样本与可复现量价特征", "market-context-evidence", "available"),
        ("组合暴露与相关性", "portfolio-risk-evidence", "available"),
        ("估值因子", "security-valuation-evidence", "conditional"),
        ("样本外、滑点、冲击与拥挤", "execution-liquidity-evidence", "conditional"),
    ),
    "duan_yongping": (
        ("长期现金创造与财务质量", "company-financial-quality", "available"),
        ("合理价格", "security-valuation-evidence", "conditional"),
        ("行业与市场参照", "market-context-evidence", "available"),
        ("用户价值、品牌心智与企业文化原文", "supplemental-company-evidence", "conditional"),
    ),
    "zhang_kun": (
        ("ROIC代理、自由现金流与财务质量", "company-financial-quality", "available"),
        ("当前估值与历史参照", "security-valuation-evidence", "conditional"),
        ("组合机会成本与相关性", "portfolio-risk-evidence", "available"),
        ("行业格局、治理与长期竞争原文", "supplemental-company-evidence", "conditional"),
    ),
    "feng_liu": (
        ("市场定价、板块与边际量价", "market-context-evidence", "available"),
        ("当前估值与赔率", "security-valuation-evidence", "conditional"),
        ("仓位与流动性承受力", "portfolio-risk-evidence", "available"),
        ("共识预期、催化剂与反方原文", "supplemental-company-evidence", "conditional"),
        ("盘口与退出成本", "execution-liquidity-evidence", "conditional"),
    ),
}


def _evidence_id(kind: str, value: object) -> str:
    digest = hashlib.sha256(
        json.dumps({"kind": kind, "value": value}, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:12]
    return f"EVIDENCE-SKILL-{digest}"


def _evidence(kind: str, value: object, *, as_of: str | None, provider: str, source_ref: str) -> dict[str, object]:
    return {
        "evidence_id": _evidence_id(kind, value),
        "kind": kind,
        "value": value,
        "as_of": as_of,
        "provider": provider,
        "source_ref": source_ref,
    }


def _max_drawdown(rows: list[dict[str, object]]) -> dict[str, object] | None:
    ordered = sorted(
        (row for row in rows if isinstance(row.get("nav"), (int, float)) and row.get("date")),
        key=lambda row: str(row["date"]),
    )
    peak_value = peak_date = None
    worst: dict[str, object] | None = None
    for row in ordered:
        value = float(row["nav"])
        if peak_value is None or value > peak_value:
            peak_value, peak_date = value, str(row["date"])
        drawdown = value / peak_value - 1 if peak_value else 0
        if worst is None or drawdown < float(worst["drawdown_percent"]) / 100:
            worst = {
                "peak_date": peak_date,
                "trough_date": str(row["date"]),
                "drawdown_percent": round(drawdown * 100, 4),
                "sample_days": len(ordered),
            }
    return worst


def _returns_by_date(rows: list[dict[str, object]]) -> dict[str, float]:
    ordered = sorted(
        ((str(row.get("date")), float(row["close"])) for row in rows if row.get("date") and row.get("close")),
        key=lambda item: item[0],
    )
    return {
        ordered[index][0]: ordered[index][1] / ordered[index - 1][1] - 1
        for index in range(1, len(ordered))
        if ordered[index - 1][1] > 0
    }


def _correlation(left: dict[str, float], right: dict[str, float]) -> tuple[float | None, int]:
    dates = sorted(set(left) & set(right))
    if len(dates) < 60:
        return None, len(dates)
    xs, ys = [left[item] for item in dates], [right[item] for item in dates]
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = math.sqrt(sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys))
    return (round(numerator / denominator, 4) if denominator else None), len(dates)


def _beta(asset: dict[str, float], benchmark: dict[str, float]) -> tuple[float | None, int]:
    dates = sorted(set(asset) & set(benchmark))
    if len(dates) < 60:
        return None, len(dates)
    xs, ys = [asset[item] for item in dates], [benchmark[item] for item in dates]
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    covariance = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    benchmark_variance = sum((y - y_mean) ** 2 for y in ys)
    return (round(covariance / benchmark_variance, 4) if benchmark_variance else None), len(dates)


def _portfolio_drawdown_proxy(
    returns: dict[str, dict[str, float]], weights: dict[str, float], *, analysis_start: str,
    weight_basis: str = "cost",
) -> dict[str, object] | None:
    usable = {security_id: rows for security_id, rows in returns.items() if security_id in weights and rows}
    if not usable:
        return None
    dates = [
        day
        for day in sorted(set.intersection(*(set(rows) for rows in usable.values())))
        if day >= analysis_start
    ]
    if len(dates) < 20:
        return None
    normalized_weight = sum(weights[security_id] for security_id in usable)
    if normalized_weight <= 0:
        return None
    active_weights = {
        security_id: weights[security_id] / normalized_weight for security_id in usable
    }
    daily = [
        sum(active_weights[security_id] * rows[day] for security_id, rows in usable.items())
        for day in dates
    ]
    wealth = peak = 1.0
    peak_index = worst_peak_index = -1
    trough_index = 0
    worst = 0.0
    for index, day_return in enumerate(daily):
        wealth *= 1 + day_return
        if wealth > peak:
            peak, peak_index = wealth, index
        drawdown = wealth / peak - 1
        if drawdown < worst:
            worst = drawdown
            worst_peak_index, trough_index = peak_index, index
    contribution_days = range(worst_peak_index + 1, trough_index + 1)
    contributions = [
        {
            "security_id": security_id,
            "weight_percent": round(weight * 100, 4),
            "weight_basis": weight_basis,
            "return_contribution_percent_points": round(
                sum(weight * rows[dates[index]] for index in contribution_days) * 100,
                4,
            ),
        }
        for security_id, rows in usable.items()
        for weight in (active_weights[security_id],)
    ]
    return {
        "analysis_start": analysis_start,
        "peak_date": dates[worst_peak_index] if worst_peak_index >= 0 else dates[0],
        "trough_date": dates[trough_index],
        "portfolio_drawdown_percent": round(worst * 100, 4),
        "overlap_samples": len(dates),
        "covered_weight_percent": round(normalized_weight * 100, 4),
        "weight_basis": weight_basis,
        "contributions": contributions,
        "method": "按本地账本投入金额固定加权日收益，识别样本内组合最大回撤；单项贡献为峰谷区间的加权日收益和。",
        "interpretation_boundary": "这是成本权重回撤贡献代理，不是按每日真实市值和交易现金流计算的精确组合业绩归因。",
    }


def _quantity_drawdown_proxy(
    prices: dict[str, list[dict[str, object]]],
    quantities: dict[str, float],
    *,
    analysis_start: str,
) -> dict[str, object] | None:
    series = {
        security_id: {
            str(row["date"]): float(row["unit_nav"] if row.get("unit_nav") else row["close"])
            for row in rows
            if row.get("date") and row.get("close")
        }
        for security_id, rows in prices.items()
        if quantities.get(security_id)
    }
    series = {security_id: rows for security_id, rows in series.items() if rows}
    if not series:
        return None
    dates = [
        day
        for day in sorted(set.intersection(*(set(rows) for rows in series.values())))
        if day >= analysis_start
    ]
    if len(dates) < 20:
        return None
    values = [
        sum(quantities[security_id] * rows[day] for security_id, rows in series.items())
        for day in dates
    ]
    peak_index = trough_index = 0
    running_peak = 0
    worst = 0.0
    for index, value in enumerate(values):
        if value > values[running_peak]:
            running_peak = index
        drawdown = value / values[running_peak] - 1 if values[running_peak] else 0
        if drawdown < worst:
            worst, peak_index, trough_index = drawdown, running_peak, index
    peak_value = values[peak_index]
    contributions = [
        {
            "security_id": security_id,
            "contribution_percent_points": round(
                quantities[security_id]
                * (rows[dates[trough_index]] - rows[dates[peak_index]])
                / peak_value
                * 100,
                4,
            ),
        }
        for security_id, rows in series.items()
    ]
    return {
        "analysis_start": dates[0],
        "analysis_end": dates[-1],
        "peak_date": dates[peak_index],
        "trough_date": dates[trough_index],
        "drawdown_percent": round(worst * 100, 4),
        "common_samples": len(dates),
        "contributions": contributions,
        "method": "按推导持仓数量和共同收盘/NAV序列计算；贡献为峰值至谷值的市值变化占组合峰值比例。",
    }


def _quarterly_decomposition(periods: list[dict[str, object]]) -> list[dict[str, object]]:
    fields = (
        "revenue",
        "parent_net_profit",
        "operating_cash_flow",
        "capex_cash_paid",
        "free_cash_flow",
        "net_cash_invest",
        "net_cash_finance",
    )
    ordered = sorted(periods, key=lambda row: str(row.get("period") or ""))
    previous_by_year: dict[str, dict[str, object]] = {}
    result = []
    for row in ordered:
        period = str(row.get("period") or "")
        if len(period) < 10:
            continue
        year, month_day = period[:4], period[5:]
        quarter = {"period": period, "period_label": row.get("period_label")}
        previous = previous_by_year.get(year)
        for field in fields:
            current = row.get(field)
            if not isinstance(current, (int, float)):
                quarter[field] = None
            elif month_day == "03-31":
                quarter[field] = current
            elif previous is None:
                quarter[field] = None
            else:
                prior = previous.get(field)
                quarter[field] = current - prior if isinstance(prior, (int, float)) else None
        previous_by_year[year] = row
        result.append(quarter)
    return list(reversed(result))


def _cashflow_year_bridge(periods: list[dict[str, object]]) -> dict[str, object] | None:
    annual = {str(row.get("period"))[:4]: row for row in periods if str(row.get("period", "")).endswith("12-31")}
    years = sorted(annual, reverse=True)
    if len(years) < 2:
        return None
    current_year, previous_year = years[:2]
    current, previous = annual[current_year], annual[previous_year]
    fields = ("revenue", "parent_net_profit", "operating_cash_flow", "capex_cash_paid", "free_cash_flow", "net_cash_invest", "net_cash_finance")
    changes = {}
    for field in fields:
        now, before = current.get(field), previous.get(field)
        changes[field] = now - before if isinstance(now, (int, float)) and isinstance(before, (int, float)) else None
    return {
        "current_year": current_year,
        "previous_year": previous_year,
        "changes": changes,
        "interpretation_boundary": "这是现金流量表与利润表的结构桥接，不足以单独证明经营现金流变化的因果原因；应再核对年报附注中的存货、应收、应付和合同负债。",
    }


def _working_capital_bridge(periods: list[dict[str, object]]) -> dict[str, object] | None:
    annual = sorted(
        (row for row in periods if str(row.get("period", "")).endswith("12-31")),
        key=lambda row: str(row.get("period")),
        reverse=True,
    )
    if len(annual) < 2:
        return None
    current, previous = annual[:2]
    fields = {
        "inventory": -1,
        "accounts_receivable": -1,
        "accounts_payable": 1,
        "contract_liabilities": 1,
    }
    changes: dict[str, float | None] = {}
    effects: list[float] = []
    for field, direction in fields.items():
        now, before = current.get(field), previous.get(field)
        change = now - before if isinstance(now, (int, float)) and isinstance(before, (int, float)) else None
        changes[field] = change
        if change is not None:
            effects.append(change * direction)
    return {
        "current_period": current.get("period"),
        "previous_period": previous.get("period"),
        "changes": changes,
        "estimated_cash_effect": round(sum(effects), 4) if effects else None,
        "method": "存货和应收增加视为现金占用；应付和合同负债增加视为现金支持。",
        "interpretation_boundary": "这是资产负债表变动的方向性桥接，不等于现金流量表附注中的精确营运资本调节项。",
    }


def build_historical_valuation_series(
    financial: dict[str, object],
    prices: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Calculate disclosure-date valuation proxies without look-ahead."""

    ordered_prices = sorted(
        (row for row in prices if row.get("date") and isinstance(row.get("close"), (int, float))),
        key=lambda row: str(row["date"]),
    )
    series = []
    for period in financial.get("periods") or []:
        notice_date = str(period.get("notice_date") or period.get("period") or "")[:10]
        candidates = [row for row in ordered_prices if str(row["date"]) <= notice_date]
        if not candidates:
            continue
        price_row = candidates[-1]
        price = float(price_row["close"])
        eps, bps = period.get("basic_eps"), period.get("bps")
        month = str(period.get("period") or "")[5:7]
        annualizer = {"03": 4.0, "06": 2.0, "09": 4 / 3, "12": 1.0}.get(month)
        annualized_eps = float(eps) * annualizer if isinstance(eps, (int, float)) and annualizer else None
        series.append({
            "report_period": period.get("period"),
            "notice_date": notice_date,
            "price_date": price_row["date"],
            "close": price,
            "pe_annualized": round(price / annualized_eps, 4) if annualized_eps and annualized_eps > 0 else None,
            "pb_reported_bps": round(price / float(bps), 4) if isinstance(bps, (int, float)) and bps > 0 else None,
        })
    series.sort(key=lambda row: str(row["notice_date"]))
    return series


def _latest_percentile(series: list[dict[str, object]], field: str) -> float | None:
    values = [float(row[field]) for row in series if isinstance(row.get(field), (int, float))]
    if not values:
        return None
    latest = values[-1]
    return round(sum(value <= latest for value in values) / len(values) * 100, 2)


def _financial_quality_metrics(periods: list[dict[str, object]]) -> dict[str, object]:
    roic_rows: list[dict[str, object]] = []
    balance_rows: list[dict[str, object]] = []
    annual_profits: list[float] = []
    annual_eps: list[float] = []
    for row in periods:
        period = str(row.get("period") or "")
        profit = row.get("parent_net_profit")
        factor = 4 if period.endswith("03-31") else 2 if period.endswith("06-30") else 4 / 3 if period.endswith("09-30") else 1
        assets = row.get("total_assets")
        current_liabilities = row.get("current_liabilities")
        cash = row.get("cash_and_equivalents")
        invested_capital = (
            float(assets) - float(current_liabilities) - float(cash or 0)
            if assets is not None and current_liabilities is not None else None
        )
        roic_proxy = (
            float(profit) * factor / invested_capital * 100
            if profit is not None and invested_capital and invested_capital > 0 else None
        )
        roic_rows.append({
            "period": period,
            "annualized_parent_profit": round(float(profit) * factor, 4) if profit is not None else None,
            "operating_invested_capital_proxy": round(invested_capital, 4) if invested_capital is not None else None,
            "roic_proxy_percent": round(roic_proxy, 4) if roic_proxy is not None else None,
        })
        short_debt = sum(float(row.get(key) or 0) for key in ("short_term_borrowings", "current_portion_noncurrent_liabilities"))
        balance_rows.append({
            "period": period,
            "short_debt": round(short_debt, 4),
            "cash_to_short_debt": round(float(cash) / short_debt, 4) if cash is not None and short_debt > 0 else None,
            "goodwill_to_assets_percent": round(float(row.get("goodwill")) / float(assets) * 100, 4)
            if row.get("goodwill") is not None and assets else None,
            "receivables": row.get("accounts_receivable"),
            "inventory": row.get("inventory"),
            "long_term_borrowings": row.get("long_term_borrowings"),
            "bonds_payable": row.get("bonds_payable"),
        })
        if period.endswith("12-31") and profit is not None:
            annual_profits.append(float(profit))
        if period.endswith("12-31") and row.get("basic_eps") is not None:
            annual_eps.append(float(row["basic_eps"]))
    annual_profits.sort()
    annual_eps.sort()
    return {
        "roic_proxy_history": roic_rows,
        "balance_sheet_risk_history": balance_rows,
        "normalized_parent_profit_median": annual_profits[len(annual_profits) // 2] if annual_profits else None,
        "normalized_eps_median": annual_eps[len(annual_eps) // 2] if annual_eps else None,
        "normalization_sample_years": len(annual_profits),
        "method_boundary": "ROIC代理=年化归母净利润÷（总资产-流动负债-货币资金）；不等同于税后经营利润口径ROIC。正常化盈利取已披露年报中位数。",
    }


def _compact_upstream_pack(pack: dict[str, object]) -> dict[str, object]:
    modules = pack.get("modules") or {}
    meta = pack.get("_meta") if isinstance(pack.get("_meta"), dict) else {}
    return {
        "schema_version": pack.get("schema_version"),
        "symbol": pack.get("symbol") or pack.get("code"),
        "name": pack.get("name"),
        "trade_date": pack.get("trade_date"),
        "market": pack.get("market"),
        "quote": pack.get("quote"),
        "financial_facts": pack.get("financial_facts") or [],
        "financial_history": pack.get("financial_history") or [],
        "price_volume": pack.get("price_volume"),
        "estimate": pack.get("estimate"),
        "profile": pack.get("profile"),
        "holdings": pack.get("holdings"),
        "index_snapshot": pack.get("index_snapshot"),
        "index_comparison": pack.get("index_comparison"),
        "premium_discount": pack.get("premium_discount"),
        "coverage": meta.get("coverage"),
        "source_events": meta.get("source_events") or [],
        "modules": {
            key: {
                "available": value.get("available"),
                "gaps": value.get("gaps") or [],
                "evidence_count": len(value.get("evidence") or []),
                # ponytail: retain every upstream fact while dropping only explicitly
                # non-contract fixture/debug baggage. If upstream adds a real field it
                # passes through automatically instead of needing another adapter patch.
                "evidence": [
                    {fact_key: fact_value for fact_key, fact_value in item.items() if fact_key != "unused_payload"}
                    if isinstance(item, dict) else item
                    for item in value.get("evidence") or []
                ],
            }
            for key, value in modules.items()
            if isinstance(value, dict)
        },
        "microstructure": pack.get("microstructure"),
        "execution_cost_model": pack.get("execution_cost_model"),
    }


def build_peer_basket_history(histories: list[dict[str, object]]) -> list[dict[str, object]]:
    """Build an equal-weight normalized history when a sector index source is unavailable."""

    by_security = []
    for history in histories:
        rows = {
            str(row["date"]): row
            for row in history.get("rows") or []
            if row.get("date") and isinstance(row.get("close"), (int, float)) and float(row["close"]) > 0
        }
        if rows:
            first = rows[sorted(rows)[0]]
            by_security.append((rows, float(first["close"])))
    if len(by_security) < 2:
        return []
    dates = sorted(set.intersection(*(set(rows) for rows, _ in by_security)))
    result = []
    for day in dates:
        normalized = [float(rows[day]["close"]) / base * 100 for rows, base in by_security]
        volumes = [float(rows[day]["volume"]) for rows, _ in by_security if isinstance(rows[day].get("volume"), (int, float))]
        result.append({
            "date": day,
            "close": round(sum(normalized) / len(normalized), 4),
            "volume": round(sum(volumes), 4) if volumes else None,
        })
    return result


class AppResearchSkillLayer:
    """Deterministically routes a question to bounded, read-only evidence collectors."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self._upstream_company_packs: dict[tuple[str, str], dict[str, object]] = {}
        self._upstream_fund_packs: dict[tuple[str, str], dict[str, object]] = {}

    def catalog(self) -> list[dict[str, str]]:
        return [dict(item) for item in SKILL_CATALOG]

    def _company_pack(self, symbol: str, as_of: str) -> dict[str, object]:
        normalized_as_of = "".join(character for character in as_of if character.isdigit())[:8]
        if len(normalized_as_of) != 8:
            raise ValueError("公司证据截止日期格式无效")
        key = (symbol, normalized_as_of)
        if key not in self._upstream_company_packs:
            self._upstream_company_packs[key] = build_company_evidence(symbol, normalized_as_of)
        return self._upstream_company_packs[key]

    def _fund_pack(self, symbol: str, as_of: str) -> dict[str, object]:
        normalized_as_of = "".join(character for character in as_of if character.isdigit())[:8]
        if len(normalized_as_of) != 8:
            raise ValueError("基金证据截止日期格式无效")
        key = (symbol, normalized_as_of)
        if key not in self._upstream_fund_packs:
            self._upstream_fund_packs[key] = build_fund_evidence(symbol, normalized_as_of)
        return self._upstream_fund_packs[key]

    def _cny_rate(self, currency: str, as_of: str) -> dict[str, object]:
        if currency == "CNY":
            return {"currency": "CNY", "rate": 1.0, "as_of": as_of, "source": "identity"}
        row = self.vault.connection.execute(
            "SELECT rate, effective_as_of FROM fx_observations "
            "WHERE base_currency = ? AND quote_currency = 'CNY' AND effective_as_of = ? "
            "ORDER BY observed_at DESC LIMIT 1",
            (currency, as_of),
        ).fetchone()
        if row:
            return {"currency": currency, "rate": float(row["rate"]), "as_of": row["effective_as_of"], "source": "Invest Vault汇率快照"}
        result = fetch_cny_exchange_rate(currency, as_of)
        self.vault.connection.execute(
            "INSERT INTO fx_observations VALUES (?, ?, 'CNY', ?, ?, ?, NULL)",
            (str(uuid4()), currency, str(result["rate"]), str(result["as_of"]), datetime.now(timezone.utc).isoformat()),
        )
        self.vault.connection.commit()
        return result

    def _latest_fund(self, security_id: str) -> dict[str, object] | None:
        row = self.vault.connection.execute(
            "SELECT payload_json FROM fund_snapshots WHERE security_id = ? ORDER BY cutoff_date DESC LIMIT 1",
            (security_id,),
        ).fetchone()
        return json.loads(str(row["payload_json"])) if row else None

    def _ensure_fund(self, security_id: str) -> dict[str, object]:
        existing = self._latest_fund(security_id)
        if existing and existing.get("holdings_periods"):
            return existing
        cutoff, symbol = target_trade_date(), security_id.split(":")[2]
        payload = fetch_fund_snapshot(symbol, cutoff)
        self.vault.connection.execute(
            "INSERT OR IGNORE INTO fund_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (
                _evidence_id("fund_snapshot", {"security_id": security_id, "cutoff": cutoff.isoformat()}),
                security_id,
                cutoff.isoformat(),
                str(payload["source"]),
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.vault.connection.commit()
        return payload

    def _financial(self, symbol: str, cutoff: date) -> dict[str, object]:
        exchange = "SSE" if symbol.startswith(("5", "6", "9")) else "SZSE"
        security_id = f"CN:{exchange}:{symbol}:STOCK"
        row = self.vault.connection.execute(
            "SELECT payload_json FROM financial_snapshots WHERE security_id = ? ORDER BY cutoff_date DESC LIMIT 1",
            (security_id,),
        ).fetchone()
        if row:
            return json.loads(str(row["payload_json"]))
        payload = fetch_financial_snapshot(symbol, cutoff)
        self.vault.connection.execute(
            "INSERT OR IGNORE INTO financial_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (
                _evidence_id("financial_snapshot", {"security_id": security_id, "cutoff": cutoff.isoformat()}),
                security_id,
                cutoff.isoformat(),
                str(payload["source"]),
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.vault.connection.commit()
        return payload

    def _fund_portfolio(
        self, security_id: str, payload: dict[str, object], *, inherit_stock_analysis: bool = False
    ) -> dict[str, object]:
        periods = list(payload.get("holdings_periods") or [])
        current = periods[0] if periods else {}
        previous = periods[1] if len(periods) > 1 else {}
        current_rows = list(current.get("holdings") or [])
        previous_by_code = {str(item.get("code")): item for item in previous.get("holdings") or []}
        industries: dict[str, list[dict[str, object]]] = defaultdict(list)
        industry_gaps: list[str] = []
        for item in current_rows:
            code = str(item.get("code") or "")
            try:
                industry = fetch_stock_industry(code)
            except Exception as error:
                industry_gaps.append(f"{code}: {error}")
                continue
            enriched = {**item, "industry": industry.get("industry")}
            industries[str(industry.get("industry") or "未分类")].append(enriched)
        concentration = {
            industry: round(sum(float(item.get("weight_percent") or 0) for item in items), 4)
            for industry, items in industries.items()
        }
        current_by_code = {str(item.get("code")): item for item in current_rows}
        direct_symbols = {
            str(item["security_id"]).split(":")[2].lstrip("0") or "0"
            for item in self.vault.holding_entries()
            if str(item["security_id"]) != security_id
        }
        direct_overlap = [
            item for item in current_rows
            if (str(item.get("code") or "").lstrip("0") or "0") in direct_symbols
        ]
        changes = []
        for code in sorted(set(current_by_code) | set(previous_by_code)):
            current_item, previous_item = current_by_code.get(code), previous_by_code.get(code)
            changes.append(
                {
                    "code": code,
                    "name": (current_item or previous_item or {}).get("name"),
                    "current_weight_percent": (current_item or {}).get("weight_percent"),
                    "previous_weight_percent": (previous_item or {}).get("weight_percent"),
                    "change_percent_points": round(
                        float((current_item or {}).get("weight_percent") or 0)
                        - float((previous_item or {}).get("weight_percent") or 0),
                        4,
                    ),
                    "status": "added" if current_item and not previous_item else "removed" if previous_item and not current_item else "changed",
                }
            )
        value = {
            "current_period": current.get("period"),
            "current_as_of": current.get("as_of"),
            "previous_period": previous.get("period"),
            "holdings": current_rows,
            "top10_weight_percent": round(sum(float(item.get("weight_percent") or 0) for item in current_rows), 4),
            "industry_weight_percent": concentration,
            "quarterly_changes": changes,
            "direct_holding_overlap": direct_overlap,
            "direct_holding_overlap_weight_percent": round(
                sum(float(item.get("weight_percent") or 0) for item in direct_overlap), 4
            ),
            "disclosure_note": "定期报告持仓不是实时仓位；调仓仅表示两个披露期之间的差异。",
        }
        gaps = []
        if inherit_stock_analysis:
            try:
                value = {
                    "stock_analysis_evidence_pack": _compact_upstream_pack(self._fund_pack(
                        security_id.split(":")[2], target_trade_date().isoformat()
                    )),
                    **value,
                }
            except Exception as error:
                gaps.append(f"stock-analysis基金证据包暂不可得：{error}")
        if not current_rows:
            gaps.append("latest_disclosed_holdings")
        if not previous:
            gaps.append("previous_disclosed_holdings")
        if industry_gaps:
            gaps.append("industry_classification: " + "; ".join(industry_gaps))
        return self._result("fund-portfolio-evidence", value, gaps, current.get("as_of"))

    def _fund_liquidity(self, payload: dict[str, object]) -> dict[str, object]:
        scale = list(payload.get("scale_history") or [])
        value = {
            "scale_history": scale,
            "latest_nav_rows": list(payload.get("nav_history") or []),
            "interpretation_boundary": "规模环比同时受净申赎和净值涨跌影响，不能当作真实净申赎流量。",
        }
        gaps = ["公开资料未提供可核验的净申购赎回流量", "公开资料未提供连续日频基金份额"]
        return self._result(
            "fund-liquidity-evidence",
            value,
            gaps,
            str(scale[-1].get("as_of")) if scale else None,
        )

    def _execution_liquidity(
        self,
        security_id: str,
        fund_payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        target = target_trade_date().isoformat()
        gaps: list[str] = []
        current_order_books: list[dict[str, object]] = []
        execution_models: list[dict[str, object]] = []
        order_book_boundaries: list[dict[str, str]] = []
        fund_disclosures: list[dict[str, object]] = []
        security_trend_metrics: list[dict[str, object]] = []

        if security_id in MARKET_OVERVIEW_SECURITY_IDS:
            security_ids = [
                str(item["security_id"])
                for item in self.vault.holding_summaries()
            ]
        elif security_id.startswith("CN:") and security_id.endswith(":STOCK"):
            security_ids = [security_id]
        else:
            security_ids = []

        for held_security_id in security_ids:
            symbol = held_security_id.split(":")[2]
            upstream_symbol = f"{symbol}.HK" if held_security_id.startswith("HK:") else symbol
            execution_market = "hk" if held_security_id.startswith("HK:") else "a"
            try:
                if held_security_id.endswith(":FUND"):
                    pack = self._fund_pack(symbol, target)
                else:
                    pack = self._company_pack(upstream_symbol, target)
            except Exception as error:
                gaps.append(f"{symbol}当前盘口与交易成本代理暂不可得：{error}")
                continue
            microstructure = pack.get("microstructure")
            execution = pack.get("execution_cost_model")
            price_volume = pack.get("price_volume") or {}
            price_rows = list(price_volume.get("rows") or [])
            if (
                not held_security_id.endswith(":FUND")
                and (len(price_rows) < 60 or not any(row.get("amount") for row in price_rows))
            ):
                try:
                    price_volume = _execution_price_volume(
                        fetch_security_trading_history(held_security_id, limit=260),
                        market=execution_market,
                    )
                except Exception as error:
                    gaps.append(f"{symbol}连续量价暂不可得：{error}")
            if held_security_id.endswith(":FUND") and not price_volume.get("rows"):
                disclosed_fund = self._ensure_fund(held_security_id)
                profile_fees = ((pack.get("profile") or {}).get("fees") or {})
                purchase_fee = profile_fees.get("front_end_rate_pct")
                try:
                    redemption_schedule = fetch_fund_redemption_fee_schedule(symbol)
                except Exception as error:
                    redemption_schedule = {"rows": [], "error": str(error)}
                microstructure = {
                    "available": False,
                    "applicable": False,
                    "symbol": symbol,
                    "reason": "为开放式基金，按净值申赎，不适用五档盘口",
                }
                execution = {
                    "available": purchase_fee is not None or bool(redemption_schedule.get("rows")),
                    "symbol": symbol,
                    "instrument_type": "open_ended_fund",
                    "market": "fund_nav_subscription_redemption",
                    "currency": "CNY",
                    "model_status": "partial_open_ended_fund_fee_schedule",
                    "purchase_fee_pct": purchase_fee,
                    "source_purchase_fee_pct": profile_fees.get("front_end_source_rate_pct"),
                    "redemption_fee_status": "公开期限费率表已取得",
                    "redemption_fee_schedule": redemption_schedule,
                    "missing_inputs": [],
                    "scenarios": [],
                    "limitations": [
                        "开放式基金按确认净值申赎，不使用场内价差、盘口深度或成交冲击模型。",
                        "赎回成本须按公开期限费率、实际持有天数和先进先出规则匹配。",
                    ],
                }
                fund_disclosures.append({
                    "security_id": held_security_id,
                    "name": disclosed_fund.get("name") or symbol,
                    "latest_disclosed_holdings": list(disclosed_fund.get("holdings_periods") or [])[:2],
                    "scale_history": list(disclosed_fund.get("scale_history") or []),
                    "nav_history": list(disclosed_fund.get("nav_history") or [])[:20],
                    "redemption_fee_schedule": redemption_schedule,
                    "disclosure_boundary": "定期报告持仓不是实时完整仓位；公开规模变化同时受净值和申赎影响，不能冒充真实净申赎。",
                })
                try:
                    fund_history = fetch_security_price_history(held_security_id, limit=260)
                except Exception:
                    pass
                else:
                    security_trend_metrics.append({
                        "security_id": held_security_id,
                        "name": disclosed_fund.get("name") or symbol,
                        **summarize_price_volume_history(list(fund_history.get("rows") or [])),
                    })
            elif isinstance(execution, dict) and execution.get("available"):
                execution = {
                    **execution,
                    "spread_input": "current_order_book",
                }
            elif isinstance(price_volume, dict):
                spread_bps = _daily_high_low_spread_bps(list(price_volume.get("rows") or []))
                if spread_bps is not None:
                    execution = build_execution_cost_model(
                        symbol=upstream_symbol,
                        price_volume=price_volume,
                        microstructure={"spread_bps": spread_bps},
                        market=execution_market,
                        currency="HKD" if execution_market == "hk" else "CNY",
                    )
                    execution = {
                        **execution,
                        "spread_input": "daily_high_low_estimate",
                        "limitations": [
                            "当前公开五档不可得，价差采用日线高低价估算，不代表实时可成交价差。",
                            *list(execution.get("limitations") or []),
                        ],
                    }
            if isinstance(microstructure, dict) and microstructure.get("available"):
                current_order_books.append({"security_id": held_security_id, **microstructure})
            elif isinstance(microstructure, dict) and microstructure.get("applicable") is False:
                order_book_boundaries.append({
                    "security_id": held_security_id,
                    "boundary": str(microstructure.get("reason") or "该品种不适用五档盘口"),
                })
            else:
                reason = microstructure.get("reason") if isinstance(microstructure, dict) else None
                if reason == "market-specific order book not connected":
                    reason = "当前市场未接入可靠的公开五档盘口"
                boundary = f"当前五档盘口不可得{f'：{reason}' if reason else ''}"
                order_book_boundaries.append({"security_id": held_security_id, "boundary": boundary})
                if not (isinstance(execution, dict) and execution.get("available")):
                    gaps.append(f"{symbol}{boundary}")
            if isinstance(execution, dict) and execution.get("available"):
                execution_models.append({"security_id": held_security_id, **execution})
            else:
                missing_inputs = "、".join(str(item) for item in (execution or {}).get("missing_inputs") or [])
                gaps.append(
                    f"{symbol}交易成本估算仍缺少{missing_inputs}"
                    if missing_inputs else f"{symbol}交易成本估算所需公开数据暂不完整"
                )
            if not held_security_id.endswith(":FUND") and price_volume.get("rows"):
                security_trend_metrics.append({
                    "security_id": held_security_id,
                    "name": str(pack.get("name") or symbol),
                    **dict(price_volume.get("metrics") or summarize_price_volume_history(
                        list(price_volume.get("rows") or [])
                    )),
                })

        value = {
            "current_order_book_snapshots": current_order_books,
            "order_book_boundaries": order_book_boundaries,
            "execution_cost_models": execution_models,
            "fund_disclosures": fund_disclosures,
            "security_trend_metrics": security_trend_metrics,
            "fund_scale_history": list((fund_payload or {}).get("scale_history") or []),
            "systemic_liquidity_boundaries": {
                "financing_conditions": "未建立统一、可核验的融资余额、融资利率与期限结构序列",
                "credit_spreads": "未建立与持仓风险同口径、同期限的信用利差曲线",
                "fund_redemption_pressure": "公开规模变化同时受净值与申赎影响，不能证明真实赎回压力",
                "historical_order_book_depth": "当前五档快照不能替代历史逐笔或全市场盘口深度",
            },
            "interpretation_boundary": (
                "盘口只代表抓取时点；交易成本为公开价差、成交额与换手率代理，"
                "不能替代券商逐笔成交、冲击成本或系统性流动性压力测试。"
            ),
        }
        return self._result("execution-liquidity-evidence", value, gaps, target)

    def _drawdown(self, payload: dict[str, object]) -> dict[str, object]:
        drawdown = _max_drawdown(list(payload.get("nav_history") or []))
        value = {
            "observable_nav_drawdown": drawdown,
            "attribution_status": "insufficient_for_causal_split",
            "required_for_full_attribution": [
                "同区间重仓股收益贡献",
                "重仓估值倍数变化",
                "重仓财务披露变化",
                "基金份额与申赎净流量",
                "市场与行业基准收益",
            ],
        }
        return self._result(
            "drawdown-attribution-readiness",
            value,
            ["缺少完整估值倍数历史", "缺少净申购赎回流量", "现有公开数据不足以做确定性因果归因"],
            str(drawdown.get("trough_date")) if drawdown else None,
        )

    def _financial_result(
        self, payloads: list[dict[str, object]], gaps: list[str], as_of: str,
        *, inherit_stock_analysis: bool = False,
    ) -> dict[str, object]:
        companies = []
        for payload in payloads:
            periods = [
                {
                    **row,
                    "period": row.get("period") or row.get("report_date"),
                    "parent_net_profit": row.get("parent_net_profit", row.get("parent_netprofit")),
                    "roe": row.get("roe", row.get("roe_weighted")),
                    "free_cash_flow": row.get("free_cash_flow", row.get("free_cash_flow_lite")),
                }
                for row in list(payload.get("periods") or [])
            ]
            annual_periods = [
                row for row in periods if str(row.get("period_label") or "").endswith("FY")
            ]
            for index, row in enumerate(annual_periods):
                previous = annual_periods[index + 1] if index + 1 < len(annual_periods) else {}
                current_equity = row.get("stockholders_equity")
                previous_equity = previous.get("stockholders_equity")
                net_profit = row.get("parent_net_profit")
                if (
                    row.get("roe") is None
                    and isinstance(net_profit, (int, float))
                    and isinstance(current_equity, (int, float))
                    and current_equity != 0
                    and isinstance(previous_equity, (int, float))
                    and previous_equity != 0
                ):
                    row["roe"] = net_profit / ((current_equity + previous_equity) / 2) * 100
            required_fields = (
                "revenue", "parent_net_profit", "roe", "gross_margin",
                "debt_asset_ratio", "operating_cash_flow", "free_cash_flow",
            )
            latest = next(
                (
                    row for row in periods
                    if sum(row.get(field) is not None for field in required_fields) >= 4
                ),
                periods[0] if periods else {},
            )
            missing_fields = [
                label
                for field, label in (
                    ("revenue", "营业收入"), ("parent_net_profit", "归母净利润"), ("roe", "ROE"),
                    ("gross_margin", "毛利率"), ("debt_asset_ratio", "资产负债率"),
                    ("operating_cash_flow", "经营现金流"), ("free_cash_flow", "自由现金流-lite"),
                )
                if latest.get(field) is None
            ]
            if missing_fields:
                gaps.append(
                    f"{payload.get('name') or payload.get('symbol') or '公司'}"
                    f"最新可形成完整分析的报告期仍缺少：{'、'.join(missing_fields)}"
                )
            upstream_pack = payload.get("stock_analysis_evidence_pack")
            if inherit_stock_analysis and upstream_pack is None:
                try:
                    upstream_pack = _compact_upstream_pack(self._company_pack(
                        str(payload.get("symbol") or ""), as_of
                    ))
                except Exception as error:
                    gaps.append(f"{payload.get('name') or payload.get('symbol') or '公司'}的stock-analysis C1-C8证据包暂不可得：{error}")
            company = {
                    **({"stock_analysis_evidence_pack": upstream_pack} if upstream_pack else {}),
                    **payload,
                    "periods": periods,
                    "single_quarters": _quarterly_decomposition(periods),
                    "cashflow_year_bridge": _cashflow_year_bridge(periods),
                    "working_capital_bridge": _working_capital_bridge(periods),
                    "financial_quality_metrics": _financial_quality_metrics(periods),
                    "analysis_boundary": "季度拆分按累计披露值相减；结构变化是可核验线索，不自动等同于管理层解释或因果结论。",
                }
            companies.append(company)
        return self._result("company-financial-quality", companies, gaps, as_of)

    def _all_holdings_financial_result(
        self, *, as_of: date, inherit_stock_analysis: bool
    ) -> dict[str, object]:
        payloads: list[dict[str, object]] = []
        gaps: list[str] = []
        for holding in self.vault.holding_summaries():
            security_id = str(holding["security_id"])
            if not security_id.endswith(":STOCK"):
                continue
            symbol = security_id.split(":")[2]
            if security_id.startswith("CN:"):
                try:
                    payloads.append(self._financial(symbol, as_of))
                except Exception as error:
                    gaps.append(f"{symbol}：{error}")
                continue
            upstream_symbol = f"{symbol}.HK" if security_id.startswith("HK:") else symbol
            if security_id.startswith("HK:"):
                try:
                    payloads.append(fetch_hk_financial_snapshot(symbol))
                except Exception as fallback_error:
                    gaps.append(f"{symbol}：港股公开财务表暂不可得：{fallback_error}")
                else:
                    continue
            try:
                pack = self._company_pack(upstream_symbol, as_of.isoformat())
            except Exception as error:
                payloads.append({
                    "security_id": security_id, "symbol": upstream_symbol, "name": symbol,
                    "source": "stock-analysis公司证据包", "periods": [],
                })
                gaps.append(f"{symbol}：stock-analysis公司证据包暂不可得：{error}")
            else:
                payloads.append({
                    "security_id": security_id,
                    "symbol": upstream_symbol,
                    "name": pack.get("name") or symbol,
                    "source": "stock-analysis公司证据包",
                    "periods": pack.get("financial_history") or [],
                    "stock_analysis_evidence_pack": _compact_upstream_pack(pack),
                })
        return self._financial_result(
            payloads, gaps, as_of.isoformat(), inherit_stock_analysis=inherit_stock_analysis
        )

    def _valuation(self, security_id: str, fund_payload: dict[str, object] | None) -> dict[str, object]:
        if not security_id.endswith(":FUND"):
            value = fetch_security_valuation(security_id)
            gaps = [] if value.get("pe_ttm") is not None or value.get("pb") is not None else ["行情源未提供PE/PB"]
            symbol = security_id.split(":")[2]
            try:
                if security_id.startswith("HK:"):
                    financial = fetch_hk_valuation_financial_history(symbol)
                    converted_periods = []
                    for period in financial.get("periods") or []:
                        rate = self._cny_rate("HKD", str(period.get("notice_date") or ""))
                        hkd_to_cny = float(rate["rate"])
                        converted_periods.append({
                            **period,
                            "basic_eps": (
                                float(period["basic_eps"]) / hkd_to_cny
                                if period.get("basic_eps") is not None else None
                            ),
                            "bps": (
                                float(period["bps"]) / hkd_to_cny
                                if period.get("bps") is not None else None
                            ),
                            "currency": "HKD",
                            "fx_as_of": rate.get("as_of"),
                            "hkd_to_cny": hkd_to_cny,
                        })
                    financial = {**financial, "periods": converted_periods}
                else:
                    financial = self._financial(symbol, target_trade_date())
                history = fetch_security_trading_history(security_id, limit=1250)
                historical = build_historical_valuation_series(financial, list(history.get("rows") or []))
            except Exception as error:
                historical = []
                gaps.append(f"历史估值计算暂不可得：{error}")
            else:
                if len(historical) < 4:
                    gaps.append(f"历史估值仅形成{len(historical)}个披露日样本，分位代表性有限")
            try:
                forecast = fetch_profit_forecast(symbol)
            except Exception as error:
                forecast = None
                gaps.append(f"公开一致预测暂不可得：{error}")
            try:
                peers = fetch_peer_valuations(symbol)
            except Exception as error:
                peers = None
                gaps.append(f"候选可比公司横截面暂不可得：{error}")
            value.update({
                "historical_series": historical,
                "historical_percentiles": {
                    "pe_annualized_percentile": _latest_percentile(historical, "pe_annualized"),
                    "pb_reported_bps_percentile": _latest_percentile(historical, "pb_reported_bps"),
                    "sample_count": len(historical),
                },
                "historical_method": (
                    "港股年度指标采用次年6月30日作为保守公开可得日，并按该日港元兑人民币参考汇率换算；"
                    "A股取财报披露日或此前最近交易日。PE按累计EPS年化，PB按披露BPS计算；均为可复算代理序列，不冒充行情源历史TTM口径。"
                    if security_id.startswith("HK:")
                    else "取财报披露日或此前最近交易日的前复权收盘价；PE按累计EPS年化，PB按披露BPS计算。它是可复算代理序列，不冒充行情源历史TTM口径。"
                ),
                "consensus_forecast": forecast,
                "peer_valuations": peers,
            })
            quality = _financial_quality_metrics(list(financial.get("periods") or [])) if 'financial' in locals() else {}
            normalized_eps = quality.get("normalized_eps_median")
            current_price = value.get("price") or value.get("close")
            value["normalized_earnings"] = quality
            value["conservative_value_scenarios"] = [
                {
                    "earnings_multiple": multiple,
                    "value_per_share": round(float(normalized_eps) * multiple, 4),
                    "margin_of_safety_percent": round(
                        (float(normalized_eps) * multiple / float(current_price) - 1) * 100, 4
                    ) if current_price else None,
                }
                for multiple in (10, 12, 15)
            ] if normalized_eps else []
            value["intrinsic_value_boundary"] = "基于历史年报EPS中位数的10/12/15倍情景，不是唯一内在价值；未纳入用户要求回报率、未来增长与资本成本时不得输出确定估值。"
            return self._result("security-valuation-evidence", value, gaps, str(value.get("as_of") or ""))
        periods = list((fund_payload or {}).get("holdings_periods") or [])
        holdings = list(periods[0].get("holdings") or []) if periods else []
        rows, errors = [], []
        for holding in holdings:
            symbol = str(holding.get("code") or "")
            exchange = "SSE" if symbol.startswith(("5", "6", "9")) else "SZSE"
            try:
                valuation = fetch_security_valuation(f"CN:{exchange}:{symbol}:STOCK")
            except Exception as error:
                errors.append(f"{symbol}：{error}")
                continue
            rows.append({**holding, **valuation})
        total_weight = sum(float(row.get("weight_percent") or 0) for row in holdings)
        covered_weight = sum(float(row.get("weight_percent") or 0) for row in rows if row.get("pe_ttm") or row.get("pb"))
        value = {
            "disclosed_period": periods[0].get("period") if periods else None,
            "constituent_valuations": rows,
            "top_holdings_weight_percent": round(total_weight, 4),
            "valuation_covered_weight_percent": round(covered_weight, 4),
            "interpretation_boundary": "基金没有单一公司PE/PB；这里只展示最近披露重仓股的当前估值及覆盖率，不把未披露持仓补齐。",
        }
        gaps = errors + ([] if holdings else ["缺少最近披露的基金重仓股"])
        return self._result("security-valuation-evidence", value, gaps, str(periods[0].get("as_of") if periods else ""))

    def _portfolio_risk(self, security_id: str) -> dict[str, object]:
        entries = self.vault.holding_entries()
        position_quantities: dict[str, float] = defaultdict(float)
        for account in self.vault.connection.execute("SELECT account_id FROM accounts"):
            for position in self.vault.project_positions(str(account["account_id"])):
                position_quantities[position.security_id] += float(position.quantity)
        totals: dict[str, float] = defaultdict(float)
        for item in entries:
            totals[str(item["security_id"])] += float(item["invested_amount_cny"])
        total = sum(totals.values())
        weights = {key: value / total for key, value in totals.items()} if total else {}
        holding_identities: dict[str, dict[str, str]] = {}
        for held_security_id in totals:
            symbol = held_security_id.split(":")[2]
            name = ""
            live_row = self.vault.connection.execute(
                "SELECT payload_json FROM market_snapshots WHERE section = ? ORDER BY observed_at DESC LIMIT 1",
                (f"live_quote:{held_security_id}",),
            ).fetchone()
            if live_row:
                name = str(json.loads(str(live_row["payload_json"])).get("name") or "")
            if not name and held_security_id.endswith(":FUND"):
                fund_row = self.vault.connection.execute(
                    "SELECT payload_json FROM fund_snapshots WHERE security_id = ? ORDER BY cutoff_date DESC LIMIT 1",
                    (held_security_id,),
                ).fetchone()
                if fund_row:
                    name = str(json.loads(str(fund_row["payload_json"])).get("name") or "")
            if not name:
                evidence_rows = self.vault.connection.execute(
                    "SELECT e.value_json FROM evidence_items e JOIN evidence_snapshots s ON s.snapshot_id = e.snapshot_id "
                    "WHERE s.security_id = ? ORDER BY s.observed_at DESC LIMIT 20",
                    (held_security_id,),
                )
                for evidence_row in evidence_rows:
                    evidence_value = json.loads(str(evidence_row["value_json"]))
                    candidate = evidence_value.get("name") if isinstance(evidence_value, dict) else None
                    if candidate:
                        name = str(candidate)
                        break
            name = name or symbol
            holding_identities[held_security_id] = {
                "security_id": held_security_id,
                "name": name,
                "symbol": symbol,
                "display_name": f"{name}（{symbol}）",
            }
        correlations, gaps = [], []
        selected_cost = totals.get(security_id, 0.0)
        cash_totals: dict[str, float] = defaultdict(float)
        for account in self.vault.connection.execute("SELECT account_id FROM accounts"):
            for currency, amount in self.vault.project_cash(str(account["account_id"])).items():
                cash_totals[currency] += float(amount)
        cash_balances = {currency: round(amount, 4) for currency, amount in cash_totals.items()}
        portfolio_profile = self.vault.portfolio_profile()
        max_drawdown_percent = (
            float(portfolio_profile["max_drawdown_percent"])
            if portfolio_profile.get("max_drawdown_percent") is not None
            else None
        )
        return_series: dict[str, dict[str, float]] = {}
        price_rows: dict[str, list[dict[str, object]]] = {}
        oldest_bought_on = min((date.fromisoformat(str(item["bought_on"])) for item in entries), default=target_trade_date())
        history_limit = min(1250, max(260, (target_trade_date() - oldest_bought_on).days * 5 // 7 + 10))
        for held_security_id in totals:
            try:
                history = fetch_security_price_history(held_security_id, limit=history_limit)
                rows = list(history["rows"])
                price_rows[held_security_id] = rows
                return_series[held_security_id] = _returns_by_date(rows)
            except Exception as error:
                gaps.append(f"{held_security_id}历史价格：{error}")
        ledger_entries: list[dict[str, object]] = []
        derived_quantities: dict[str, float] = defaultdict(float)
        purchase_fx_rates: dict[str, dict[str, object]] = {}
        for item in entries:
            held_security_id = str(item["security_id"])
            actual_quantity = position_quantities.get(held_security_id)
            purchase_row = next(
                (
                    row for row in price_rows.get(held_security_id, [])
                    if str(row.get("date")) == str(item["bought_on"])
                ),
                None,
            )
            purchase_price = (
                float(purchase_row.get("unit_nav") or purchase_row.get("close"))
                if purchase_row and (purchase_row.get("unit_nav") or purchase_row.get("close"))
                else None
            )
            currency = {"hk_stock": "HKD", "us_stock": "USD"}.get(
                str(item["asset_type"]), "CNY"
            )
            purchase_fx = None
            if purchase_price not in (None, 0):
                try:
                    purchase_fx = self._cny_rate(currency, str(item["bought_on"]))
                except Exception as error:
                    gaps.append(f"{holding_identities[held_security_id]['display_name']}买入日汇率：{error}")
            can_derive = purchase_price not in (None, 0) and purchase_fx is not None
            quantity = (
                float(actual_quantity)
                if actual_quantity is not None
                else float(item["invested_amount_cny"]) / (purchase_price * float(purchase_fx["rate"]))
                if can_derive and purchase_price and purchase_fx
                else None
            )
            if actual_quantity is None and quantity is not None:
                derived_quantities[held_security_id] += quantity
            if purchase_fx:
                purchase_fx_rates[held_security_id] = purchase_fx
            ledger_entries.append({
                **holding_identities[held_security_id],
                "security_id": held_security_id,
                "asset_type": str(item["asset_type"]),
                "bought_on": str(item["bought_on"]),
                "invested_amount_cny": float(item["invested_amount_cny"]),
                "purchase_price": round(purchase_price, 6) if purchase_price is not None else None,
                "purchase_currency": currency,
                "purchase_fx_cny_per_unit": round(float(purchase_fx["rate"]), 8) if purchase_fx else None,
                "purchase_fx_as_of": purchase_fx.get("as_of") if purchase_fx else None,
                "quantity": round(quantity, 8) if quantity is not None else None,
                "quantity_status": (
                    "recorded" if actual_quantity is not None
                    else "derived_from_purchase_close_and_fx" if quantity is not None and currency != "CNY"
                    else "derived_from_purchase_close" if quantity is not None
                    else "unavailable_without_exact_purchase_price_or_fx"
                ),
            })
        aggregate_quantities = {
            held_security_id: (
                float(position_quantities[held_security_id])
                if held_security_id in position_quantities
                else derived_quantities.get(held_security_id, 0.0)
            )
            for held_security_id in totals
        }
        asset_types = {str(item["security_id"]): str(item["asset_type"]) for item in entries}
        market_values: dict[str, float] = {}
        daily_profit: dict[str, float] = {}
        valuation_as_of: dict[str, dict[str, object]] = {}
        holding_valuations: dict[str, dict[str, object]] = {}
        current_fx_rates: dict[str, dict[str, object]] = {}
        for held_security_id, quantity in aggregate_quantities.items():
            rows = price_rows.get(held_security_id, [])
            live_row = self.vault.connection.execute(
                "SELECT payload_json FROM market_snapshots WHERE section = ? ORDER BY observed_at DESC LIMIT 1",
                (f"live_quote:{held_security_id}",),
            ).fetchone()
            live_quote = json.loads(str(live_row["payload_json"])) if live_row else {}
            fallback_price = rows[-1].get("unit_nav") or rows[-1].get("close") if rows else None
            holding_valuations[held_security_id] = {
                **holding_identities[held_security_id],
                "price": live_quote.get("price") or fallback_price,
                "pe_ttm": live_quote.get("pe_ttm"),
                "pb": live_quote.get("pb"),
                "market_cap_100m": live_quote.get("market_cap_100m"),
                "currency": live_quote.get("currency") or {
                    "hk_stock": "HKD", "us_stock": "USD",
                }.get(asset_types.get(held_security_id), "CNY"),
                "trade_date": live_quote.get("trade_date") or (rows[-1].get("date") if rows else None),
                "source": live_quote.get("source") or ("公开历史收盘/NAV" if rows else None),
            }
            currency = str(holding_valuations[held_security_id]["currency"])
            try:
                current_fx = self._cny_rate(currency, str(holding_valuations[held_security_id]["trade_date"] or target_trade_date()))
            except Exception as error:
                gaps.append(f"{holding_identities[held_security_id]['display_name']}当前汇率：{error}")
                current_fx = None
            if current_fx:
                current_fx_rates[held_security_id] = current_fx
            if not quantity or not rows or current_fx is None:
                continue
            latest_price = float(
                live_quote.get("price") or rows[-1].get("unit_nav") or rows[-1]["close"]
            )
            fx_rate = float(current_fx["rate"])
            market_values[held_security_id] = quantity * latest_price * fx_rate
            previous_price = live_quote.get("previous_close")
            if previous_price is None and len(rows) > 1:
                previous_price = rows[-2].get("unit_nav") or rows[-2]["close"]
            if previous_price is not None:
                previous_price = float(previous_price)
                daily_profit[held_security_id] = quantity * (latest_price - previous_price) * fx_rate
            valuation_as_of[held_security_id] = {
                **holding_identities[held_security_id],
                "price": round(latest_price, 6),
                "trade_date": live_quote.get("trade_date") or rows[-1].get("date"),
                "session": live_quote.get("data_session") or "盘后",
                "source": live_quote.get("source") or "公开历史收盘/NAV",
                "currency": currency,
                "fx_cny_per_unit": round(fx_rate, 8),
                "fx_as_of": current_fx.get("as_of"),
            }
        holding_ids = list(totals)
        for index, left_id in enumerate(holding_ids):
            for right_id in holding_ids[index + 1:]:
                correlation, samples = _correlation(
                    return_series.get(left_id, {}), return_series.get(right_id, {})
                )
                correlations.append({
                    "left": holding_identities[left_id],
                    "right": holding_identities[right_id],
                    "correlation": correlation,
                    "overlap_samples": samples,
                })
                if correlation is None:
                    gaps.append(f"{left_id}与{right_id}重合收益样本不足60个")
        betas: list[dict[str, object]] = []
        try:
            benchmark_payload = fetch_global_index_price_volume(limit=history_limit)
        except Exception as error:
            benchmark_payload = {}
            gaps.append(f"组合基准历史暂不可得：{error}")
        benchmark_by_asset = {
            "a_share": "sh000300", "fund": "sh000300",
            "hk_stock": "hkHSI", "us_stock": "usINX",
        }
        for held_security_id in holding_ids:
            benchmark_id = benchmark_by_asset.get(asset_types.get(held_security_id, ""))
            benchmark_rows = (
                list((benchmark_payload.get(benchmark_id) or {}).get("rows") or [])
                if benchmark_id else []
            )
            beta, samples = _beta(
                return_series.get(held_security_id, {}), _returns_by_date(benchmark_rows)
            )
            betas.append({
                **holding_identities[held_security_id],
                "benchmark": benchmark_id,
                "beta": beta,
                "overlap_samples": samples,
            })
            if beta is None:
                gaps.append(f"{held_security_id}与基准重合收益样本不足60个，beta暂不可得")
        analysis_start = max((str(item["bought_on"]) for item in entries), default="")
        market_value_total = sum(market_values.values())
        market_value_complete = bool(entries) and len(market_values) == len(totals)
        market_value_weights = {
            key: value / market_value_total for key, value in market_values.items()
        } if market_value_total else {}
        quantity_drawdown = (
            _quantity_drawdown_proxy(price_rows, aggregate_quantities, analysis_start=analysis_start)
            if aggregate_quantities
            and all(aggregate_quantities.values())
            and all(asset_type != "hk_stock" for asset_type in asset_types.values())
            else None
        )
        drawdown_proxy = quantity_drawdown or _portfolio_drawdown_proxy(
            return_series,
            market_value_weights if market_value_complete else weights,
            analysis_start=analysis_start,
            weight_basis="current_market_value" if market_value_complete else "invested_cost",
        )
        if drawdown_proxy:
            for contribution in drawdown_proxy.get("contributions") or []:
                identity = holding_identities.get(str(contribution.get("security_id")))
                if identity:
                    contribution.update(identity)
        cny_cash = cash_balances.get("CNY")
        cash_ratio = (
            cny_cash / (cny_cash + market_value_total) * 100
            if cny_cash is not None and market_value_complete and cny_cash + market_value_total > 0
            else None
        )
        value = {
            "ledger_source": "Invest Vault本地持仓账本",
            "ledger_entries": ledger_entries,
            "holding_identities": holding_identities,
            "ledger_completeness": (
                "complete_for_cost_weight_analysis" if ledger_entries else "no_holdings"
            ),
            "ledger_completeness_note": (
                "账本已包含证券代码、买入日期和人民币投入金额。A股和人民币基金在取得买入日精确收盘/NAV后，"
                "按投入金额除以该价格推导数量；这是估算投影，不回写为真实成交数量。港股仍需买入日汇率。"
            ),
            "weight_basis": "用户录入的人民币投入金额，不是实时市值",
            "cost_basis_cny": round(selected_cost, 4),
            "selected_weight_percent": round(weights.get(security_id, 0) * 100, 4),
            "holding_weights_percent": {key: round(value * 100, 4) for key, value in weights.items()},
            "holding_weights": [
                {
                    **holding_identities[key],
                    "weight_percent": round(weight * 100, 4),
                }
                for key, weight in weights.items()
            ],
            "holding_market_weights_percent": {
                key: round(weight * 100, 4) for key, weight in market_value_weights.items()
            },
            "hhi": round(sum(value * value for value in weights.values()), 6),
            "correlations": correlations,
            "betas": betas,
            "correlation_note": "相关性基于公开日收盘/NAV收益率，至少60个重合样本；它描述历史共同波动，不保证未来关系。",
            "market_value_status": (
                "available_derived_quantity_estimate" if market_value_complete
                else "partial_derived_quantity_estimate" if market_values
                else "unavailable_without_exact_purchase_price_or_fx"
            ),
            "estimated_market_value_cny": round(market_value_total, 4) if market_values else None,
            "market_values_cny": {key: round(value, 4) for key, value in market_values.items()},
            "market_value_rows": [
                {**holding_identities[key], "estimated_market_value_cny": round(amount, 4)}
                for key, amount in market_values.items()
            ],
            "estimated_daily_profit_cny": round(sum(daily_profit.values()), 4) if daily_profit else None,
            "daily_profit_cny": {key: round(value, 4) for key, value in daily_profit.items()},
            "daily_profit_rows": [
                {**holding_identities[key], "estimated_daily_profit_cny": round(amount, 4)}
                for key, amount in daily_profit.items()
            ],
            "valuation_as_of": valuation_as_of,
            "purchase_fx_rates": purchase_fx_rates,
            "current_fx_rates": current_fx_rates,
            "holding_valuations": holding_valuations,
            "market_value_note": "市值和当日盈亏按推导数量及最新可得盘中报价或收盘/NAV估算，不代表券商成交数量、手续费或真实成交结果。",
            "cash_balances": cash_balances,
            "cash_ratio_status": "available" if cash_ratio is not None else "unavailable_without_comparable_cash_and_market_value",
            "cash_ratio_percent": round(cash_ratio, 4) if cash_ratio is not None else None,
            "cash_ratio_note": "现金比例只有在用户录入现金账本且持仓市值口径可比时才能计算。",
            "drawdown_threshold_status": "available_user_defined" if max_drawdown_percent is not None else "user_input_required",
            "max_drawdown_percent": max_drawdown_percent,
            "drawdown_threshold_note": "可承受回撤阈值属于用户风险约束，不从历史波动或专家框架自动推断。",
            "drawdown_contribution_status": (
                "available_derived_quantity_proxy" if quantity_drawdown
                else "available_market_value_weight_proxy" if drawdown_proxy and market_value_complete
                else "available_cost_weight_proxy" if drawdown_proxy
                else "unavailable_without_aligned_history"
            ),
            "drawdown_contribution_proxy": drawdown_proxy,
            "drawdown_contribution_note": (
                "优先使用真实或推导数量；含港股时改用当前人民币市值权重与证券本币日收益代理，"
                "未纳入逐日汇率变化，因此不冒充券商级精确归因。"
            ),
        }
        if not totals:
            gaps.append("当前 Vault 未录入持仓，无法计算真实组合权重、集中度和相关性")
        elif len(totals) < 2:
            gaps.append("组合内不足两个有效标的，无法计算跨资产相关性")
        if cash_ratio is None:
            gaps.append("现金账本或可比持仓市值不完整，无法计算现金比例")
        if max_drawdown_percent is None:
            gaps.append("未录入用户可承受回撤阈值")
        return self._result("portfolio-risk-evidence", value, gaps, target_trade_date().isoformat())

    def _public_topics(self, security_id: str, question: str, fund_payload: dict[str, object] | None) -> dict[str, object]:
        names: list[str] = []
        if fund_payload:
            names.append(str(fund_payload.get("name") or security_id.split(":")[2]))
            periods = list(fund_payload.get("holdings_periods") or [])
            if periods:
                names.extend(str(item.get("name") or "") for item in list(periods[0].get("holdings") or [])[:3])
        else:
            try:
                names.append(str(fetch_security_valuation(security_id).get("name") or security_id.split(":")[2]))
            except Exception:
                names.append(security_id.split(":")[2])
        topics = [
            canonical
            for canonical, aliases in (
                ("库存", ("渠道库存", "库存")),
                ("批价", ("真实批价", "批价")),
                ("消费税", ("消费税", "税制")),
                ("政策", ("政策",)),
                ("终端需求", ("终端需求",)),
                ("动销回款", ("动销", "经销商回款", "回款")),
            )
            if any(alias in question for alias in aliases)
        ][:3]
        searches, gaps = [], []
        for name in [item for item in dict.fromkeys(names) if item][:4]:
            for topic in topics:
                try:
                    searches.append(fetch_public_news(f"{name} {topic}", size=6))
                except Exception as error:
                    gaps.append(f"{name} {topic}：{error}")
        value = {
            "searches": searches,
            "verification_boundary": "资讯标题只能作为研究线索。真实批价需要明确采样渠道、地区和日期；库存需要公司/渠道调研口径；消费税方案及影响测算需以正式政策文本和公司财务口径为准。",
        }
        if not any(search.get("items") for search in searches):
            gaps.append("未检索到带来源链接的专题资讯")
        if "批价" in question:
            gaps.append("公开资讯标题不能替代带采样渠道、地区和日期的连续可核验批价时间序列")
        if any(term in question for term in ("渠道库存", "动销", "回款")):
            gaps.append("渠道库存、动销和经销商回款缺少公司披露或可交叉验证的连续统一口径")
        if "消费税" in question:
            gaps.append("尚未取得消费税正式政策原文、实施范围、税率、征收环节及公司传导机制的完整证据链")
        return self._result("public-topic-evidence", value, gaps, target_trade_date().isoformat())

    def _supplemental_company(self, security_id: str) -> dict[str, object]:
        symbol = security_id.split(":")[2]
        try:
            name = str(fetch_security_valuation(security_id).get("name") or symbol)
        except Exception:
            name = symbol
        value = fetch_company_supplemental_evidence(symbol, name=name)
        official = [item for section in value.get("official_sections") or [] for item in section.get("items") or []]
        sourced = [item for search in value.get("topic_searches") or [] for item in search.get("items") or []]
        gaps = []
        if not official:
            gaps.append("公司F10未返回可核对的分业务或管理层记录")
        if not sourced:
            gaps.append("未检索到带原文链接的收购、客户、订单、资本开支或治理补充资料")
        return self._result("supplemental-company-evidence", value, gaps, target_trade_date().isoformat())

    def _market_context(self, security_id: str, question: str = "") -> dict[str, object]:
        target = target_trade_date()
        gaps: list[str] = []
        if security_id in MARKET_OVERVIEW_SECURITY_IDS:
            is_a_share_only = security_id == A_SHARE_MARKET_OVERVIEW_SECURITY_ID
            sections: dict[str, dict[str, object]] = {}
            section_names = (
                ("indices", "lhb", "industry_flow", "pulse", "a_market_news", "a_share_themes", "a_share_earnings_calendar")
                if is_a_share_only
                else (
                    "indices", "lhb", "industry_flow", "pulse", "a_market_news", "a_share_themes",
                    "a_share_earnings_calendar", "global_indices", "global_market_news",
                    "global_market_movers", "global_earnings_calendar", "global_themes",
                )
            )
            for section in section_names:
                row = self.vault.connection.execute(
                    "SELECT trade_date, source, payload_json, observed_at FROM market_snapshots "
                    "WHERE section = ? ORDER BY observed_at DESC LIMIT 1",
                    (section,),
                ).fetchone()
                if row:
                    sections[section] = json.loads(str(row["payload_json"]))
                else:
                    gaps.append(f"大盘概览缺少{section}最新快照")
            indices = sections.get("indices", {})
            lhb = sections.get("lhb", {})
            flow = sections.get("industry_flow", {})
            pulse = sections.get("pulse", {})
            a_market_news = sections.get("a_market_news", {})
            global_market_news = sections.get("global_market_news", {})
            requested_session = next((item for item in ("盘前", "盘中", "盘后") if item in question), None)
            global_indices = sections.get("global_indices", {})
            actual_session = indices.get("session") or global_indices.get("session")
            market_breadth: dict[str, object] = {"available": False, "trade_date": target.isoformat()}
            continuous_price_volume: dict[str, object] = {}
            try:
                market_breadth = fetch_a_share_market_breadth(target)
            except Exception as error:
                gaps.append(f"A股全市场涨跌家数暂不可得：{error}")
            try:
                continuous_price_volume = fetch_global_index_price_volume()
            except Exception as error:
                gaps.append(f"主要指数连续量价暂不可得：{error}")
            if not continuous_price_volume:
                gaps.append("主要指数连续量价没有形成有效样本")
            market_news = list(a_market_news.get("items") or []) + list(global_market_news.get("items") or [])
            if not market_news:
                gaps.append("隔夜至当前时点没有取得可核验的大盘新增资讯")
            session_label = str(indices.get("session_label") or global_indices.get("session_label") or "")
            if actual_session == "盘前" or "盘前" in session_label:
                gaps.append("盘前集合竞价仅在交易所实时窗口可验证；当前证据未形成完整竞价快照")
            if not market_breadth.get("available"):
                gaps.append("当前时点实时涨跌家数未形成完整全市场分页样本")
            if not flow.get("date") or str(flow.get("date")) != str(indices.get("date")):
                gaps.append("当前时点实时行业资金流未与指数日期形成同日完整样本")
            freshness_audit = {
                "a_share_index_date": indices.get("date"),
                "breadth_date": market_breadth.get("trade_date"),
                "industry_flow_date": flow.get("date"),
                "pulse_date": pulse.get("date"),
                "a_market_news_date": a_market_news.get("date"),
                "a_share_themes_date": (sections.get("a_share_themes") or {}).get("date"),
                "a_share_earnings_month": (sections.get("a_share_earnings_calendar") or {}).get("month"),
                "auction_snapshot_available": False,
                "current_order_book_scope": "由盘口与系统性流动性证据按A股持仓逐只提供当前快照",
            }
            if not is_a_share_only:
                freshness_audit.update({
                    "global_index_date": global_indices.get("date"),
                    "global_market_news_date": global_market_news.get("date"),
                    "global_movers_date": (sections.get("global_market_movers") or {}).get("date"),
                    "global_earnings_month": (sections.get("global_earnings_calendar") or {}).get("month"),
                    "global_themes_date": (sections.get("global_themes") or {}).get("date"),
                })
            liquidity_boundaries = {
                "financing_conditions": "当前证据包没有统一、可核验的融资余额与融资利率期限序列",
                "order_book_depth": "单只A股可取得当前五档盘口；历史全市场盘口深度不可回溯",
                "credit_spreads": "尚未建立与持仓风险口径一致的信用利差曲线",
                "fund_net_flows": "基金规模变化不能替代真实净申购赎回",
            }
            if not is_a_share_only:
                liquidity_boundaries.update({
                    "cross_currency": "各市场成交额和市值使用各自本币，跨币种比较前必须统一口径",
                    "leaderboard_scope": "领涨榜经过市值与成交筛选，不代表无门槛全市场排序",
                    "theme_method": "主题为透明代表证券等权代理，不冒充交易所主题指数",
                })
            value = {
                "scene": "market_overview",
                "market_scope": "a_share" if is_a_share_only else "all_available_markets",
                "market_date": indices.get("date") or global_indices.get("date"),
                "session": actual_session,
                "session_label": session_label or None,
                "requested_session": requested_session,
                "session_mismatch": bool(requested_session and actual_session != requested_session),
                "major_indices": indices.get("rows") or [],
                "global_indices": [] if is_a_share_only else global_indices.get("rows") or [],
                "market_breadth": market_breadth,
                "continuous_price_volume": continuous_price_volume,
                "dragon_tiger": lhb.get("rows") or [],
                "industry_flow": {
                    "date": flow.get("date"),
                    "inbound": flow.get("inbound") or [],
                    "outbound": flow.get("outbound") or [],
                },
                "limit_up_down_diffusion": pulse if pulse.get("kind") == "limit_pools" else None,
                "premarket_holding_news": pulse.get("news") if pulse.get("kind") == "holding_news" else None,
                "a_share_market_news": a_market_news.get("items") or [],
                "a_share_themes": sections.get("a_share_themes"),
                "a_share_earnings_calendar": sections.get("a_share_earnings_calendar"),
                "global_market_news": [] if is_a_share_only else global_market_news.get("items") or [],
                "global_market_movers": None if is_a_share_only else sections.get("global_market_movers"),
                "global_earnings_calendar": None if is_a_share_only else sections.get("global_earnings_calendar"),
                "global_themes": None if is_a_share_only else sections.get("global_themes"),
                "overnight_market_news": market_news,
                "freshness_audit": freshness_audit,
                "liquidity_boundaries": liquidity_boundaries,
                "report_boundary": (
                    "历史A股概览线程仅保留A股证据兼容。"
                    if is_a_share_only
                    else "使用当前全部可用市场、公开资料、可审计计算与本地持仓证据；页面栏目不是证据上限。"
                ),
            }
            return self._result(
                "market-context-evidence",
                value,
                gaps,
                str(indices.get("date") or global_indices.get("date") or target.isoformat()),
            )
        try:
            history = fetch_security_trading_history(security_id, limit=260)
            price_volume = summarize_price_volume_history(list(history.get("rows") or []))
            price_volume.update({
                "security_id": security_id,
                "source": history.get("source"),
                "source_ref": history.get("source_ref"),
            })
        except Exception as error:
            price_volume = {"security_id": security_id, "sample_count": 0}
            gaps.append(f"标的历史量价暂不可得：{error}")
        official_nav = None
        if security_id.endswith(":FUND"):
            try:
                nav_history = fetch_security_price_history(security_id, limit=260)
                official_nav = summarize_price_volume_history(list(nav_history.get("rows") or []))
                official_nav.update({"source": nav_history.get("source"), "source_ref": nav_history.get("source_ref")})
            except Exception as error:
                gaps.append(f"基金官方净值历史暂不可得：{error}")
        row = self.vault.connection.execute(
            "SELECT trade_date, payload_json FROM market_snapshots WHERE section = 'indices' ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()
        indices = (
            json.loads(str(row["payload_json"]))
            if not security_id.startswith("HK:") and row and str(row["trade_date"]) == target.isoformat()
            else None
        )
        index_rows = list((indices or {}).get("rows") or [])
        if indices is None or not index_rows or all(item.get("volume") is None for item in index_rows):
            try:
                indices = fetch_index_overview(target, region="HK" if security_id.startswith("HK:") else "CN")
            except Exception as error:
                indices = {"date": target.isoformat(), "rows": []}
                gaps.append(f"主要指数暂不可得：{error}")
        related_sector: dict[str, object] | None = None
        if security_id.startswith("CN:") and security_id.endswith(":STOCK"):
            try:
                related_sector = fetch_stock_industry(security_id.split(":")[2])
                classification_rows = list(related_sector.get("classification_rows") or [])
                if classification_rows and classification_rows[0].get("code"):
                    try:
                        sector_history = fetch_sector_price_history(str(classification_rows[0]["code"]), limit=260)
                    except Exception as error:
                        peer_histories = []
                        try:
                            peers = fetch_peer_valuations(security_id.split(":")[2])
                            for peer in list(peers.get("rows") or [])[:6]:
                                symbol = str(peer.get("symbol") or "")
                                exchange = "SSE" if symbol.startswith(("5", "6", "9")) else "SZSE"
                                try:
                                    history = fetch_security_trading_history(
                                        f"CN:{exchange}:{symbol}:STOCK", limit=260
                                    )
                                except Exception:
                                    continue
                                peer_histories.append({"symbol": symbol, "rows": history.get("rows") or []})
                            basket = build_peer_basket_history(peer_histories)
                        except Exception:
                            basket = []
                        if basket:
                            related_sector["price_volume"] = {
                                **summarize_price_volume_history(basket),
                                "rows": basket,
                                "source": "候选同行等权量价代理",
                                "source_ref": peers.get("source_ref"),
                                "method": f"{len(peer_histories)}只行业候选公司前复权价格归一至100后等权；成交量为样本合计。",
                            }
                            gaps.append(f"官方板块历史接口不可用，已改用同行等权代理：{error}")
                        else:
                            gaps.append(f"相关板块历史量价暂不可得：{error}")
                    else:
                        related_sector["price_volume"] = {
                            **summarize_price_volume_history(list(sector_history.get("rows") or [])),
                            "source": sector_history.get("source"),
                            "source_ref": sector_history.get("source_ref"),
                        }
            except Exception as error:
                gaps.append(f"相关板块暂不可得：{error}")
        if int(price_volume.get("sample_count") or 0) < 61:
            gaps.append("标的量价历史不足61个有效交易日，不能形成完整5/20/60日比较")
        if security_id.endswith(":STOCK") and price_volume.get("volume_zscore") is None:
            gaps.append("标的成交量序列不足，无法计算20日成交量异常")
        value = {
            "market_date": indices.get("date"),
            "major_indices": indices.get("rows") or [],
            "security_price_volume": price_volume,
            "official_fund_nav_performance": official_nav,
            "related_sector": related_sector,
            "interpretation_boundary": "指数、板块和历史量价用于比较与验证；相关性、趋势和成交量异常不自动构成因果结论或交易信号。",
        }
        return self._result("market-context-evidence", value, gaps, str(indices.get("date") or target.isoformat()))

    def _framework_readiness(
        self,
        *,
        security_id: str,
        role_id: str,
        results: list[dict[str, object]],
    ) -> dict[str, object]:
        requirements = FRAMEWORK_REQUIREMENTS.get(role_id, FRAMEWORK_REQUIREMENTS["general"])
        by_skill = {str(result["skill_id"]): result for result in results}
        material_count = int(self.vault.connection.execute(
            "SELECT COUNT(*) FROM research_materials WHERE security_id = ?", (security_id,)
        ).fetchone()[0])
        available: list[str] = []
        conditional: list[str] = []
        missing: list[str] = []
        details = []
        for label, skill_id, completed_ceiling in requirements:
            if skill_id is None:
                status = "missing"
                reason = "结构化来源与联网补证均不得用代理指标捏造该项；需要权威原文或用户输入"
            else:
                result = by_skill.get(skill_id)
                if result is None or result.get("status") == "failed":
                    status, reason = "missing", "本轮未取得所需证据"
                elif completed_ceiling == "conditional":
                    status, reason = "conditional", "已取得部分证据，口径或历史覆盖不足"
                else:
                    evidence_values = [
                        item.get("value")
                        for item in result.get("evidence") or []
                        if isinstance(item, dict)
                    ]
                    if any(value not in (None, [], {}) for value in evidence_values):
                        status, reason = "available", "本轮已取得可核验数据；其他字段缺口另行保留"
                    else:
                        status, reason = "missing", "本轮结果没有形成可用事实"
            {"available": available, "conditional": conditional, "missing": missing}[status].append(label)
            details.append({"requirement": label, "status": status, "reason": reason, "evidence_skill": skill_id})
        value = {
            "role_id": role_id,
            "security_id": security_id,
            "available": available,
            "conditional": conditional,
            "missing": missing,
            "requirements": details,
            "archived_material_count": material_count,
            "coverage_note": "缺失表示当前证据包尚未证明该项，不等于公开世界不存在数据；条件可用项不得升级为确定结论。",
        }
        return self._result("framework-readiness", value, [f"仍需补充：{item}" for item in missing], target_trade_date().isoformat())

    def _result(self, skill_id: str, value: object, gaps: list[str], as_of: str | None) -> dict[str, object]:
        meta = next(item for item in SKILL_CATALOG if item["skill_id"] == skill_id)
        source_ref = {
            "fund-portfolio-evidence": "https://fundf10.eastmoney.com/",
            "fund-liquidity-evidence": "https://fundf10.eastmoney.com/",
            "drawdown-attribution-readiness": "https://fundf10.eastmoney.com/",
            "company-financial-quality": "https://datacenter.eastmoney.com/",
            "security-valuation-evidence": "https://qt.gtimg.cn/",
            "portfolio-risk-evidence": "https://web.ifzq.gtimg.cn/",
            "public-topic-evidence": "https://ai-news-search.futunn.com/",
            "market-context-evidence": "https://web.ifzq.gtimg.cn/",
            "supplemental-company-evidence": "https://emweb.securities.eastmoney.com/PC_HSF10/",
            "execution-liquidity-evidence": "https://hq.sinajs.cn/",
            "framework-readiness": "",
        }[skill_id]
        evidence = _evidence(
            skill_id,
            value,
            as_of=as_of,
            provider="Invest Vault app-owned adapter",
            source_ref=source_ref,
        )
        return {**meta, "status": "partial" if gaps else "completed", "evidence": [evidence], "gaps": gaps}

    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        normalized = question.lower()
        is_market_overview = security_id in MARKET_OVERVIEW_SECURITY_IDS
        is_fund = security_id.endswith(":FUND")
        results: list[dict[str, object]] = []
        requested = set(FRAMEWORK_SKILLS.get(role_id, FRAMEWORK_SKILLS["general"]))
        deep_review = any(term in normalized for term in DEEP_EVIDENCE_TERMS)
        # Every research turn receives the local ledger. Missing quantity is a precision
        # boundary, not a reason to claim the user's holdings are absent.
        requested.add("portfolio-risk-evidence")
        if is_market_overview:
            requested.update({"market-context-evidence", "execution-liquidity-evidence"})
        if any(term in normalized for term in FINANCIAL_TERMS):
            requested.add("company-financial-quality")
        if any(term in normalized for term in VALUATION_TERMS):
            requested.add("security-valuation-evidence")
        if any(term in normalized for term in PORTFOLIO_TERMS):
            requested.add("portfolio-risk-evidence")
        if any(term in normalized for term in MARKET_TERMS):
            requested.add("market-context-evidence")
        if deep_review and not is_market_overview and not is_fund:
            requested.update({
                "company-financial-quality",
                "security-valuation-evidence",
                "supplemental-company-evidence",
                "execution-liquidity-evidence",
            })
        if any(term in normalized for term in PUBLIC_TOPIC_TERMS):
            requested.add("public-topic-evidence")
        if not is_market_overview and not is_fund and (role_id != "general" or any(term in normalized for term in SUPPLEMENTAL_COMPANY_TERMS)):
            requested.add("supplemental-company-evidence")
        if not requested:
            requested.add("market-context-evidence")
        if is_fund:
            # Every fund framework starts from the same disclosed F1-F8 pack, liquidity
            # boundary, drawdown sample and constituent valuation. A role may change the
            # weighting, never make these baseline facts disappear.
            requested.update({
                "fund-portfolio-evidence",
                "fund-liquidity-evidence",
                "drawdown-attribution-readiness",
                "security-valuation-evidence",
                "market-context-evidence",
                "execution-liquidity-evidence",
            })
            if role_id != "general" or deep_review:
                requested.add("company-financial-quality")
        fund_payload = self._ensure_fund(security_id) if is_fund else None
        all_holdings_financial = (
            any(term in normalized for term in ALL_HOLDINGS_TERMS)
            and any(term in normalized for term in FINANCIAL_TERMS)
        )
        if all_holdings_financial:
            results.append(self._all_holdings_financial_result(
                as_of=target_trade_date(), inherit_stock_analysis=role_id != "general"
            ))
            requested.discard("company-financial-quality")
        elif is_market_overview and "company-financial-quality" in requested:
            results.append(self._all_holdings_financial_result(
                as_of=target_trade_date(), inherit_stock_analysis=True
            ))
            requested.discard("company-financial-quality")
        if is_market_overview and "security-valuation-evidence" in requested:
            valuations: list[object] = []
            valuation_gaps: list[str] = []
            for holding in self.vault.holding_summaries():
                held_security_id = str(holding["security_id"])
                try:
                    held_fund_payload = self._ensure_fund(held_security_id) if held_security_id.endswith(":FUND") else None
                    result = self._valuation(held_security_id, held_fund_payload)
                    valuations.extend(
                        [item.get("value") for item in result.get("evidence") or [] if item.get("value")]
                    )
                    valuation_gaps.extend(
                        f"{held_security_id}：{item}" for item in result.get("gaps") or []
                    )
                except Exception as error:
                    valuation_gaps.append(f"{held_security_id}：{error}")
            results.append(self._result(
                "security-valuation-evidence",
                valuations,
                valuation_gaps or ([] if valuations else ["本地持仓未形成可核验估值证据"]),
                target_trade_date().isoformat(),
            ))
            requested.discard("security-valuation-evidence")
        if is_market_overview and "supplemental-company-evidence" in requested:
            supplements: list[object] = []
            supplemental_gaps: list[str] = []
            for holding in self.vault.holding_summaries():
                held_security_id = str(holding["security_id"])
                if not held_security_id.endswith(":STOCK"):
                    continue
                try:
                    result = self._supplemental_company(held_security_id)
                    supplements.extend(
                        [item.get("value") for item in result.get("evidence") or [] if item.get("value")]
                    )
                    supplemental_gaps.extend(
                        f"{held_security_id}：{item}" for item in result.get("gaps") or []
                    )
                except Exception as error:
                    supplemental_gaps.append(f"{held_security_id}：{error}")
            results.append(self._result(
                "supplemental-company-evidence",
                supplements,
                supplemental_gaps or ([] if supplements else ["本地股票持仓未形成公开定性补充证据"]),
                target_trade_date().isoformat(),
            ))
            requested.discard("supplemental-company-evidence")
        if is_fund and fund_payload:
            if "fund-portfolio-evidence" in requested:
                results.append(self._fund_portfolio(
                    security_id, fund_payload, inherit_stock_analysis=True
                ))
            if "fund-liquidity-evidence" in requested:
                results.append(self._fund_liquidity(fund_payload))
            if "drawdown-attribution-readiness" in requested:
                results.append(self._drawdown(fund_payload))
            if "company-financial-quality" in requested:
                periods = list(fund_payload.get("holdings_periods") or [])
                holdings = list(periods[0].get("holdings") or [])[:10] if periods else []
                financials, gaps = [], []
                cutoff = target_trade_date()
                for holding in holdings:
                    symbol = str(holding.get("code") or "")
                    try:
                        financials.append(self._financial(symbol, cutoff))
                    except Exception as error:
                        gaps.append(f"{symbol}: {error}")
                results.append(self._financial_result(
                    financials, gaps, cutoff.isoformat(), inherit_stock_analysis=True
                ))
            if "security-valuation-evidence" in requested:
                results.append(self._valuation(security_id, fund_payload))
        elif not is_market_overview and not is_fund and "company-financial-quality" in requested:
            symbol, cutoff = security_id.split(":")[2], target_trade_date()
            if security_id.startswith("CN:"):
                try:
                    financial = self._financial(symbol, cutoff)
                except Exception as error:
                    results.append(self._financial_result([], [str(error)], cutoff.isoformat()))
                else:
                    result = self._financial_result(
                        [financial], [], cutoff.isoformat(), inherit_stock_analysis=True
                    )
                    half_year = re.search(r"(20\d{2})年半年报", question)
                    if half_year and not any(
                        str(period.get("period")) == f"{half_year.group(1)}-06-30"
                        for period in financial.get("periods") or []
                    ):
                        result["gaps"].append(
                            f"截至证据截止日，{half_year.group(1)}年半年报尚未进入公开财务源；"
                            "合同负债、存货、应收应付及现金流附注不能提前补写"
                        )
                        result["status"] = "partial"
                    results.append(result)
            else:
                upstream_symbol = f"{symbol}.HK" if security_id.startswith("HK:") else symbol
                try:
                    pack = self._company_pack(upstream_symbol, cutoff.isoformat())
                except Exception as error:
                    results.append(self._financial_result([], [str(error)], cutoff.isoformat()))
                else:
                    results.append(self._financial_result(
                        [{
                            "security_id": security_id,
                            "symbol": upstream_symbol,
                            "name": pack.get("name") or symbol,
                            "source": "stock-analysis公司证据包",
                            "periods": pack.get("financial_history") or [],
                            "stock_analysis_evidence_pack": _compact_upstream_pack(pack),
                        }],
                        [], cutoff.isoformat(), inherit_stock_analysis=True,
                    ))
        if not is_market_overview and not is_fund and "security-valuation-evidence" in requested:
            try:
                results.append(self._valuation(security_id, None))
            except Exception as error:
                results.append(self._result("security-valuation-evidence", [], [str(error)], target_trade_date().isoformat()))
        if "portfolio-risk-evidence" in requested:
            results.append(self._portfolio_risk(security_id))
        if "public-topic-evidence" in requested:
            results.append(self._public_topics(security_id, question, fund_payload))
        if "market-context-evidence" in requested:
            results.append(self._market_context(security_id, question))
        if "execution-liquidity-evidence" in requested:
            results.append(self._execution_liquidity(security_id, fund_payload))
        if not is_market_overview and not is_fund and "supplemental-company-evidence" in requested:
            try:
                results.append(self._supplemental_company(security_id))
            except Exception as error:
                results.append(self._result(
                    "supplemental-company-evidence", [], [str(error)], target_trade_date().isoformat()
                ))
        results.append(self._framework_readiness(security_id=security_id, role_id=role_id, results=results))
        return results
