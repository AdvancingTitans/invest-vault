"""Invest Vault adapters plus the bundled stock-analysis 4.14 runtime bridge."""

from __future__ import annotations

import html
import json
import math
import re
import time as time_module
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
POST_CLOSE_GATE = time(17, 30)
A_SHARE_HOLIDAYS_2026 = {
    date(2026, 1, 1),
    *(date(2026, 2, day) for day in range(16, 23)),
    *(date(2026, 4, day) for day in range(4, 7)),
    *(date(2026, 5, day) for day in range(1, 6)),
    *(date(2026, 6, day) for day in range(19, 22)),
    *(date(2026, 9, day) for day in range(25, 28)),
    *(date(2026, 10, day) for day in range(1, 9)),
}


def _float(value: str, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {field}") from error
    if field == "price" and result <= 0:
        raise ValueError("price must be positive")
    return result


def _market(symbol: str) -> str:
    return "sh" if symbol.startswith(("5", "6", "9")) else "sz"


def _is_trade_day(value: date) -> bool:
    return value.weekday() < 5 and value not in A_SHARE_HOLIDAYS_2026


def previous_trade_date(value: date) -> date:
    candidate = value - timedelta(days=1)
    while not _is_trade_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def target_trade_date(now: datetime | None = None) -> date:
    """Return the newest A-share date whose post-close archive may be used."""

    local = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    candidate = local.date()
    if not _is_trade_day(candidate) or local.timetz().replace(tzinfo=None) < POST_CLOSE_GATE:
        candidate = previous_trade_date(candidate)
    return candidate


def current_market_date(now: datetime | None = None) -> date:
    """Return today's trading date during a session, otherwise the last trading date."""

    local = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    return local.date() if _is_trade_day(local.date()) else previous_trade_date(local.date())


def fetch_cny_exchange_rate(currency: str, on_date: date | str | None = None) -> dict[str, object]:
    """Return one unit of currency in CNY without silently substituting a fixed rate."""

    normalized = currency.upper()
    as_of = str(on_date or current_market_date())
    if normalized == "CNY":
        return {"currency": "CNY", "rate": 1.0, "as_of": as_of, "source": "identity"}
    if normalized not in {"HKD", "USD"}:
        raise ValueError(f"暂不支持{normalized}兑人民币汇率")
    url = f"https://api.frankfurter.app/{urllib.parse.quote(as_of)}?from={normalized}&to=CNY"
    request = urllib.request.Request(url, headers={"User-Agent": "Invest-Vault/0.3"})
    with urllib.request.urlopen(request, timeout=12) as response:  # nosec B310 - fixed HTTPS host
        payload = json.loads(response.read().decode("utf-8"))
    rate = (payload.get("rates") or {}).get("CNY")
    if not isinstance(rate, (int, float)) or rate <= 0:
        raise ValueError(f"{as_of}的{normalized}兑人民币汇率不可得")
    return {
        "currency": normalized,
        "rate": float(rate),
        "as_of": str(payload.get("date") or as_of),
        "source": "Frankfurter/ECB reference rates",
        "source_ref": url,
    }


def market_report_stage(now: datetime | None = None) -> dict[str, object]:
    """Identify the A-share report stage from the trading calendar and local clock."""

    local = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    trading_day = _is_trade_day(local.date())
    clock = local.timetz().replace(tzinfo=None)
    if not trading_day:
        report_date, session = previous_trade_date(local.date()), "盘后"
    elif clock < time(9, 30):
        report_date, session = local.date(), "盘前"
    elif clock < time(15, 0):
        report_date, session = local.date(), "盘中"
    else:
        report_date, session = local.date(), "盘后"
    return {
        "session": session,
        "report_date": report_date.isoformat(),
        "is_trade_day": trading_day,
        "label": f"{report_date.month}月{report_date.day}日{session}行情报告",
    }


def _canonical_date(value: str) -> str:
    digits = re.sub(r"\D", "", value)[:8]
    if len(digits) != 8:
        raise ValueError("quote has no valid trade date")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def parse_tencent_quote(payload: str, symbol: str) -> dict[str, object]:
    match = re.search(r'="(.*)";?$', payload.strip())
    fields = match.group(1).split("~") if match else []
    if len(fields) < 39:
        raise ValueError("incomplete Tencent quote")
    return {
        "symbol": symbol,
        "market": "a",
        "asset_type": "fund" if symbol.startswith(("1", "5")) else "stock",
        "name": fields[1],
        "price": _float(fields[3], "price"),
        "previous_close": _float(fields[4], "previous_close"),
        "change": _float(fields[31], "change"),
        "change_percent": _float(fields[32], "change_percent"),
        "volume": _float(fields[6], "volume"),
        "amount": _float(fields[37], "amount"),
        "turnover": _float(fields[38], "turnover"),
        "pe_ttm": _number(fields[39]) if len(fields) > 39 else None,
        "market_cap_100m": _number(fields[44]) if len(fields) > 44 else None,
        "pb": _number(fields[46]) if len(fields) > 46 else None,
        "currency": "CNY",
        "trade_date": _canonical_date(fields[30]),
        "source": "tencent_quote",
        "source_ref": f"https://qt.gtimg.cn/q={_market(symbol)}{symbol}",
        "source_chain": ["tencent_quote"],
        "quality_flags": [],
    }


def parse_sina_quote(payload: str, symbol: str) -> dict[str, object]:
    match = re.search(r'="(.*)";?$', payload.strip())
    fields = match.group(1).split(",") if match else []
    if len(fields) < 32:
        raise ValueError("incomplete Sina quote")
    price = _float(fields[3], "price")
    previous = _float(fields[2], "previous_close")
    return {
        "symbol": symbol,
        "market": "a",
        "asset_type": "fund" if symbol.startswith(("1", "5")) else "stock",
        "name": fields[0],
        "price": price,
        "previous_close": previous,
        "change": price - previous,
        "change_percent": (price / previous - 1) * 100 if previous > 0 else None,
        "volume": _float(fields[8], "volume"),
        "amount": _float(fields[9], "amount"),
        "turnover": None,
        "currency": "CNY",
        "trade_date": _canonical_date(fields[30]),
        "source": "sina_quote",
        "source_ref": f"https://hq.sinajs.cn/list={_market(symbol)}{symbol}",
        "source_chain": ["tencent_quote", "sina_quote"],
        "quality_flags": ["turnover_unavailable"],
        "fallback_reason": "Tencent quote unavailable",
    }


def parse_tencent_history(payload: str, symbol: str, trade_date: str) -> dict[str, object]:
    code = f"{_market(symbol)}{symbol}"
    data = (json.loads(payload).get("data") or {}).get(code) or {}
    rows = data.get("qfqday") or data.get("day") or []
    row_index = next((index for index, row in enumerate(rows) if row and row[0] == trade_date), None)
    if row_index is None:
        raise ValueError(f"Tencent history has no exact trade date {trade_date}")
    row = rows[row_index]
    previous = _float(rows[row_index - 1][2], "previous_close") if row_index > 0 else None
    price = _float(row[2], "price")
    change = price - previous if previous else None
    name_fields = (data.get("qt") or {}).get(code) or []
    return {
        "symbol": symbol,
        "market": "a",
        "asset_type": "fund" if symbol.startswith(("1", "5")) else "stock",
        "name": name_fields[1] if len(name_fields) > 1 else symbol,
        "price": price,
        "previous_close": previous,
        "change": change,
        "change_percent": change / previous * 100 if change is not None and previous else None,
        "volume": _float(row[5], "volume") if len(row) > 5 else None,
        "amount": None,
        "turnover": None,
        "currency": "CNY",
        "trade_date": trade_date,
        "source": "tencent_kline",
        "source_ref": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        "source_chain": ["tencent_kline"],
        "quality_flags": [],
    }


def parse_tencent_security_history(
    payload: str, *, provider_code: str, symbol: str, trade_date: str, market: str, asset_type: str
) -> dict[str, object]:
    data = (json.loads(payload).get("data") or {}).get(provider_code) or {}
    rows = data.get("qfqday") or data.get("day") or []
    row_index = next((index for index, row in enumerate(rows) if row and row[0] == trade_date), None)
    if row_index is None:
        raise ValueError(f"Tencent history has no exact trade date {trade_date}")
    row = rows[row_index]
    previous = _float(rows[row_index - 1][2], "previous_close") if row_index > 0 else None
    price = _float(row[2], "price")
    quote_fields = (data.get("qt") or {}).get(provider_code) or []
    change = price - previous if previous else None
    return {
        "symbol": symbol,
        "market": market,
        "asset_type": asset_type,
        "name": quote_fields[1] if len(quote_fields) > 1 else symbol,
        "price": price,
        "previous_close": previous,
        "change": change,
        "change_percent": change / previous * 100 if change is not None and previous else None,
        "volume": _float(row[5], "volume") if len(row) > 5 else None,
        "amount": None,
        "turnover": None,
        "currency": "HKD" if market == "hk" else "CNY",
        "trade_date": trade_date,
        "source": "tencent_kline",
        "source_ref": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        "source_chain": ["tencent_kline"],
        "quality_flags": [],
    }


def _request(url: str) -> bytes:
    # Explicitly bypass desktop proxy for public quote endpoints and localhost isolation.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if "fund.eastmoney.com" in url or "fundf10.eastmoney.com" in url:
        referer = "https://fundf10.eastmoney.com/"
    elif "eastmoney.com" in url:
        referer = "https://quote.eastmoney.com/"
    else:
        referer = "https://finance.sina.com.cn/"
    request = urllib.request.Request(
        url,
        headers={"Referer": referer, "User-Agent": "Mozilla/5.0 InvestVault/0.1"},
    )
    with opener.open(request, timeout=6) as response:
        return response.read()


def _eastmoney_json(url: str, request: Callable[[str], bytes]) -> dict[str, Any]:
    """Use Eastmoney's delayed public host when the primary quote host disconnects."""

    candidates = [url]
    if "push2.eastmoney.com" in url:
        candidates.append(url.replace("push2.eastmoney.com", "push2delay.eastmoney.com"))
    elif "push2his.eastmoney.com" in url:
        candidates.append(url.replace("push2his.eastmoney.com", "push2delay.eastmoney.com"))
    last_error: Exception | None = None
    for index, candidate in enumerate(candidates):
        try:
            return json.loads(request(candidate).decode("utf-8"))
        except Exception as error:
            last_error = error
            if index + 1 < len(candidates):
                time_module.sleep(0.8)
    assert last_error is not None
    raise last_error


def fetch_public_quote(symbol: str, request: Callable[[str], bytes] = _request) -> dict[str, object]:
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("only six-digit A-share and listed-fund symbols are supported")
    tencent_url = f"https://qt.gtimg.cn/q={_market(symbol)}{symbol}"
    try:
        return parse_tencent_quote(request(tencent_url).decode("gbk"), symbol)
    except Exception as first_error:
        sina_url = f"https://hq.sinajs.cn/list={_market(symbol)}{symbol}"
        try:
            return parse_sina_quote(request(sina_url).decode("gbk"), symbol)
        except Exception as second_error:
            raise RuntimeError(
                f"quote providers unavailable: Tencent={first_error}; Sina={second_error}"
            ) from second_error


def fetch_security_valuation(
    security_id: str, request: Callable[[str], bytes] = _request
) -> dict[str, object]:
    """Read the current public quote valuation fields for A/HK securities."""

    parts = security_id.split(":")
    if len(parts) < 4 or parts[3] != "STOCK":
        raise ValueError("当前估值仅支持A股和港股个股")
    region, symbol = parts[0], parts[2]
    provider_code = f"{_market(symbol)}{symbol}" if region == "CN" else f"hk{symbol.zfill(5)}"
    if region not in {"CN", "HK"}:
        raise ValueError("该市场当前估值尚未通过稳定性验证")
    payload = request(f"https://qt.gtimg.cn/q={provider_code}").decode("gbk")
    match = re.search(r'="(.*)";?$', payload.strip())
    fields = match.group(1).split("~") if match else []
    if len(fields) < (59 if region == "HK" else 47):
        raise ValueError("腾讯估值响应字段不完整")
    trade_date = _canonical_date(fields[30])
    price = _number(fields[3])
    previous_close = _number(fields[4])
    change = price - previous_close if price is not None and previous_close not in (None, 0) else None
    return {
        "security_id": security_id,
        "name": fields[1],
        "symbol": symbol,
        "price": price,
        "previous_close": previous_close,
        "change": change,
        "change_percent": round(change / previous_close * 100, 4)
        if change is not None and previous_close
        else None,
        "volume": _number(fields[6]),
        "amount": _number(fields[37]),
        "turnover": _number(fields[38]),
        "pe_ttm": _number(fields[39]),
        "pb": _number(fields[58] if region == "HK" else fields[46]),
        "market_cap_100m": _number(fields[44]),
        "currency": "HKD" if region == "HK" else "CNY",
        "as_of": trade_date,
        "trade_date": trade_date,
        "source": "腾讯证券公开行情",
        "source_ref": f"https://qt.gtimg.cn/q={provider_code}",
        "source_chain": ["tencent_quote"],
        "quality_flags": [],
        "valuation_note": "PE/PB为行情源当前口径，不等于历史分位或内在价值估算。",
    }


def fetch_security_live_quote(
    security_id: str, request: Callable[[str], bytes] = _request
) -> dict[str, object]:
    """Fetch a current A/HK quote and retain a verified daily fallback for HK disconnects."""

    parts = security_id.split(":")
    if len(parts) < 4:
        raise ValueError("invalid security id")
    region, symbol, instrument = parts[0], parts[2], parts[3]
    if region == "CN":
        return fetch_public_quote(symbol, request=request)
    if region != "HK" or instrument != "STOCK":
        raise ValueError("当前行情仅支持A股、上市基金和港股个股")
    try:
        return fetch_security_valuation(security_id, request=request)
    except Exception as primary_error:
        history = fetch_security_trading_history(security_id, limit=5, request=request)
        rows = list(history["rows"])
        latest = rows[-1]
        previous = _number(rows[-2].get("close")) if len(rows) > 1 else None
        price = _number(latest.get("close"))
        if price is None:
            raise ValueError("港股日线 fallback 缺少有效收盘价") from primary_error
        change = price - previous if previous not in (None, 0) else None
        return {
            "symbol": symbol,
            "market": "hk",
            "asset_type": "stock",
            "name": str(latest.get("name") or history.get("name") or symbol),
            "price": price,
            "previous_close": previous,
            "change": change,
            "change_percent": round(change / previous * 100, 4) if change is not None and previous else None,
            "volume": latest.get("volume"),
            "amount": latest.get("amount"),
            "turnover": latest.get("turnover"),
            "pe_ttm": None,
            "pb": None,
            "market_cap_100m": None,
            "currency": "HKD",
            "trade_date": str(latest["date"]),
            "source": "tencent_kline",
            "source_ref": str(history["source_ref"]),
            "source_chain": ["tencent_quote", "tencent_kline"],
            "quality_flags": ["intraday_quote_unavailable", "valuation_unavailable"],
            "fallback_reason": f"腾讯港股实时行情不可用：{primary_error}",
        }


def fetch_profit_forecast(
    symbol: str,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    """Read public F10 consensus forecasts and retain institution-level revisions."""

    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("盈利预测目前仅支持A股")
    code = f"{'SH' if symbol.startswith(('5', '6', '9')) else 'SZ'}{symbol}"
    source_ref = f"https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/Index?code={code}"
    payload = json.loads(request(source_ref.replace("/Index?", "/PageAjax?")).decode("utf-8-sig"))
    average = next(
        (row for row in payload.get("jgyc") or [] if "平均" in str(row.get("ORG_NAME_ABBR") or "")),
        (payload.get("jgyc") or [{}])[0],
    )
    consensus: list[dict[str, object]] = []
    previous_eps: float | None = None
    for index in range(1, 5):
        year, eps = average.get(f"YEAR{index}"), _number(average.get(f"EPS{index}"))
        if year is None or eps is None:
            continue
        consensus.append(
            {
                "year": int(year),
                "year_mark": average.get(f"YEAR_MARK{index}"),
                "eps": eps,
                "pe": _number(average.get(f"PE{index}")),
                "eps_growth_percent": (
                    round((eps / previous_eps - 1) * 100, 4) if previous_eps not in (None, 0) else None
                ),
            }
        )
        previous_eps = eps
    revisions = []
    for row in payload.get("ycmx") or []:
        forecasts = [
            {
                "year": int(row[f"YEAR{index}"]),
                "eps": _number(row.get(f"EPS{index}")),
                "parent_net_profit": _number(row.get(f"PARENT_NETPROFIT{index}")),
            }
            for index in range(1, 5)
            if row.get(f"YEAR{index}") is not None and _number(row.get(f"EPS{index}")) is not None
        ]
        if forecasts:
            revisions.append(
                {
                    "publish_date": str(row.get("PUBLISH_DATE") or "")[:10],
                    "institution": str(row.get("ORG_NAME_ABBR") or ""),
                    "researcher": str(row.get("RESEARCHER") or ""),
                    "rating": row.get("RATING"),
                    "forecasts": forecasts,
                }
            )
    revisions.sort(key=lambda row: str(row["publish_date"]), reverse=True)
    institutions = {str(row["institution"]) for row in revisions if row["institution"]}
    return {
        "symbol": symbol,
        "as_of": str(average.get("PUBLISH_DATE") or revisions[0]["publish_date"] if revisions else "")[:10],
        "consensus": consensus,
        "revision_history": revisions[:30],
        "coverage": {
            "institutions": len(institutions),
            "records": len(revisions),
            "earliest": revisions[-1]["publish_date"] if revisions else None,
            "latest": revisions[0]["publish_date"] if revisions else None,
        },
        "source": "东方财富F10盈利预测",
        "source_ref": source_ref,
        "interpretation_boundary": "公开预测是卖方样本汇总，不代表公司指引；覆盖机构、预测期和修订历史必须与结论一起呈现。",
    }


def fetch_peer_valuations(
    symbol: str,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    """Propose a bounded same-sector peer set; the user remains the peer-set owner."""

    industry = fetch_stock_industry(symbol, request=request)
    classifications = list(industry.get("classification_rows") or [])
    sector_code = str((classifications[0] if classifications else {}).get("code") or "")
    if not re.fullmatch(r"BK\d{4}", sector_code):
        raise ValueError("未取得可用于候选可比公司的行业代码")
    params = urllib.parse.urlencode(
        {
            "pn": 1,
            "pz": 20,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f20",
            "fs": f"b:{sector_code}",
            "fields": "f12,f14,f2,f9,f20,f23",
        }
    )
    url = f"https://push2.eastmoney.com/api/qt/clist/get?{params}"
    payload = _eastmoney_json(url, request)
    raw = (payload.get("data") or {}).get("diff") or []
    rows = list(raw.values()) if isinstance(raw, dict) else list(raw)
    peers = [
        {
            "symbol": str(row.get("f12") or ""),
            "name": str(row.get("f14") or ""),
            "price": _number(row.get("f2")),
            "pe_dynamic": _number(row.get("f9")),
            "pb": _number(row.get("f23")),
            "market_cap": _number(row.get("f20")),
        }
        for row in rows
        if str(row.get("f12") or "") != symbol and str(row.get("f14") or "")
    ][:8]
    if not peers:
        raise ValueError("行业候选可比公司返回为空")
    return {
        "industry": industry.get("industry"),
        "sector_code": sector_code,
        "status": "provisional_requires_user_confirmation",
        "rows": peers,
        "source": "东方财富行业成分当前估值",
        "source_ref": f"https://quote.eastmoney.com/center/boardlist.html#boards-{sector_code}",
        "comparison_boundary": "这是按当前行业分类和市值生成的候选集合；用户确认业务可比性后才能升级为正式可比公司横截面。",
    }


def fetch_company_supplemental_evidence(
    symbol: str,
    *,
    name: str,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    """Collect bounded official operating/management facts plus sourced topic leads."""

    code = f"{'SH' if symbol.startswith(('5', '6', '9')) else 'SZ'}{symbol}"
    base = "https://emweb.securities.eastmoney.com/PC_HSF10"
    business_ref = f"{base}/BusinessAnalysis/Index?code={code}"
    management_ref = f"{base}/CompanyManagement/Index?code={code}"
    business = json.loads(request(business_ref.replace("/Index?", "/PageAjax?")).decode("utf-8-sig"))
    management = json.loads(request(management_ref.replace("/Index?", "/PageAjax?")).decode("utf-8-sig"))
    segment_rows = [
        {
            "report_date": str(row.get("REPORT_DATE") or "")[:10],
            "item": row.get("ITEM_NAME"),
            "revenue": _number(row.get("MAIN_BUSINESS_INCOME")),
            "cost": _number(row.get("MAIN_BUSINESS_COST")),
            "gross_profit": _number(row.get("MAIN_BUSINESS_RPOFIT")),
            "gross_margin": _number(row.get("GROSS_RPOFIT_RATIO")),
        }
        for row in business.get("zygcfx") or []
        if row.get("ITEM_NAME")
    ][:40]
    managers = [
        {
            "name": row.get("PERSON_NAME"),
            "position": row.get("POSITION"),
            "start_date": str(row.get("OFFICE_BEGIN_DATE") or "")[:10],
            "end_date": str(row.get("OFFICE_END_DATE") or "")[:10] or None,
        }
        for row in management.get("gglb") or []
        if row.get("PERSON_NAME")
    ][:20]
    changes = list(management.get("cgbd") or [])[:20]
    topic_queries = (
        ("收购、商誉与现金回报", ("收购", "商誉")),
        ("客户、订单与价格", ("客户", "订单")),
        ("毛利率与营运资本", ("毛利率", "经营现金流")),
        ("资本开支与管理层", ("资本开支", "管理层")),
    )
    searches = []
    for topic, terms in topic_queries:
        items, errors = [], []
        for term in terms:
            try:
                result = fetch_public_news(f"{name} {term}", size=8, request=request)
            except Exception as error:
                errors.append(str(error))
            else:
                items.extend(result.get("items") or [])
        deduplicated = list({str(item.get("url") or item.get("title")): item for item in items}.values())[:16]
        searches.append({"topic": topic, "items": deduplicated, "errors": errors})
    return {
        "official_sections": [
            {"topic": "分业务收入与毛利率", "items": segment_rows, "source_ref": business_ref},
            {"topic": "管理层与人员变动", "items": [*managers, *changes], "source_ref": management_ref},
        ],
        "topic_searches": searches,
        "source": "东方财富公司F10与带链接公开资讯",
        "source_ref": business_ref,
        "verification_boundary": "资讯用于自动定位原文，不将新闻标题直接升级为交易条款、客户留存、订单或资本开支拆分事实。",
    }


def fetch_security_price_history(
    security_id: str,
    *,
    limit: int = 260,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    """Fetch bounded daily OHLCV rows for correlation, drawdown and lens checks."""

    parts = security_id.split(":")
    if len(parts) < 4:
        raise ValueError("invalid security id")
    region, symbol, instrument = parts[0], parts[2], parts[3]
    if region == "CN" and instrument == "FUND":
        end = target_trade_date()
        rows = _fund_nav_rows(
            symbol, end - timedelta(days=max(limit * 2, 400)), end, request, page_size=limit
        )
        closes = [
            {
                "date": str(row["FSRQ"]),
                # Cumulative NAV avoids false returns across public share-split adjustments.
                "close": _number(row.get("LJJZ")) or _number(row.get("DWJZ")),
                "unit_nav": _number(row.get("DWJZ")),
            }
            for row in rows
        ]
        source, source_ref = "东方财富基金历史净值", f"https://fundf10.eastmoney.com/jjjz_{symbol}.html"
    else:
        return fetch_security_trading_history(security_id, limit=limit, request=request)
    closes = [row for row in closes if row["close"] not in (None, 0)][-limit:]
    if len(closes) < 2:
        raise ValueError("历史价格样本不足")
    return {
        "security_id": security_id,
        "rows": closes,
        "sample_count": len(closes),
        "as_of": closes[-1]["date"],
        "source": source,
        "source_ref": source_ref,
    }


def fetch_security_trading_history(
    security_id: str,
    *,
    limit: int = 260,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    """Fetch exchange-traded OHLCV, including listed funds, from Tencent qfq K-lines."""

    parts = security_id.split(":")
    if len(parts) < 4:
        raise ValueError("invalid security id")
    region, symbol = parts[0], parts[2]
    if region == "CN":
        provider_code = f"{_market(symbol)}{symbol}"
    elif region == "HK":
        provider_code = f"hk{symbol.zfill(5)}"
    else:
        raise ValueError("该市场交易历史尚未通过稳定性验证")
    params = urllib.parse.urlencode({"param": f"{provider_code},day,,,{limit},qfq"})
    payload = json.loads(
        request(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?{params}").decode("utf-8")
    )
    data = (payload.get("data") or {}).get(provider_code) or {}
    raw_rows = data.get("qfqday") or data.get("day") or []
    rows = [
        {
            "date": str(row[0]),
            "open": _number(row[1]),
            "close": _number(row[2]),
            "high": _number(row[3]) if len(row) > 3 else None,
            "low": _number(row[4]) if len(row) > 4 else None,
            "volume": _number(row[5]) if len(row) > 5 else None,
        }
        for row in raw_rows
        if len(row) > 2 and _number(row[2]) not in (None, 0)
    ][-limit:]
    if len(rows) < 2:
        raise ValueError("交易历史样本不足")
    quote_fields = (data.get("qt") or {}).get(provider_code) or []
    return {
        "security_id": security_id,
        "name": quote_fields[1] if len(quote_fields) > 1 else symbol,
        "rows": rows,
        "sample_count": len(rows),
        "as_of": rows[-1]["date"],
        "source": "腾讯前复权日线",
        "source_ref": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    }


def fetch_historical_close(
    symbol: str,
    trade_date: date,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("only six-digit A-share and listed-fund symbols are supported")
    target = trade_date.isoformat()
    params = urllib.parse.urlencode({"param": f"{_market(symbol)}{symbol},day,,{target},3,qfq"})
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?{params}"
    return parse_tencent_history(request(url).decode("utf-8"), symbol, target)


def fetch_security_historical_close(
    security_id: str,
    trade_date: date,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    parts = security_id.split(":")
    if len(parts) < 4:
        raise ValueError("invalid security id")
    region, symbol, instrument = parts[0], parts[2], parts[3]
    if region == "CN" and instrument == "FUND":
        return fetch_fund_nav_close(security_id, trade_date, request=request)
    if region == "CN":
        provider_code, market = f"{_market(symbol)}{symbol}", "a"
    elif region == "HK":
        provider_code, market = f"hk{symbol.zfill(5)}", "hk"
    else:
        raise ValueError("该市场历史收盘数据暂未通过稳定性验证")
    target = trade_date.isoformat()
    params = urllib.parse.urlencode({"param": f"{provider_code},day,,{target},3,qfq"})
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?{params}"
    return parse_tencent_security_history(
        request(url).decode("utf-8"),
        provider_code=provider_code,
        symbol=symbol,
        trade_date=target,
        market=market,
        asset_type="fund" if instrument == "FUND" else "stock",
    )


FUND_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"


def _fund_nav_rows(
    symbol: str, start: date, end: date, request: Callable[[str], bytes], *, page_size: int = 60
) -> list[dict[str, Any]]:
    request_size = min(max(page_size, 1), 100)
    rows: list[dict[str, Any]] = []
    for page_index in range(1, 20):
        params = urllib.parse.urlencode(
            {
                "fundCode": symbol,
                "pageIndex": page_index,
                "pageSize": request_size,
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
            }
        )
        payload = json.loads(request(f"{FUND_NAV_URL}?{params}").decode("utf-8"))
        data = payload.get("Data") or {}
        page = list(data.get("LSJZList") or [])
        rows.extend(page)
        total = int(payload.get("TotalCount") or data.get("TotalCount") or 0)
        if (
            not page
            or len(rows) >= page_size
            or (total and len(rows) >= total)
            or (not total and len(page) < request_size)
        ):
            break
    return sorted((row for row in rows if row.get("FSRQ")), key=lambda row: str(row["FSRQ"]))


def fetch_fund_nav_close(
    security_id: str,
    trade_date: date,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    parts = security_id.split(":")
    if len(parts) < 4 or parts[0] != "CN" or parts[3] != "FUND":
        raise ValueError("不是受支持的中国基金代码")
    symbol, target = parts[2], trade_date.isoformat()
    rows = _fund_nav_rows(symbol, trade_date - timedelta(days=20), trade_date, request)
    index = next((i for i, row in enumerate(rows) if str(row.get("FSRQ")) == target), None)
    if index is None:
        raise ValueError(f"东方财富基金净值缺少精确日期 {target}")
    row = rows[index]
    nav = _float(str(row.get("DWJZ") or ""), "price")
    previous = _number(rows[index - 1].get("DWJZ")) if index > 0 else None
    change_percent = _number(row.get("JZZZL"))
    change = nav - previous if previous not in (None, 0) else None
    return {
        "symbol": symbol,
        "market": "fund",
        "asset_type": "fund",
        "name": symbol,
        "price": nav,
        "previous_close": previous,
        "change": change,
        "change_percent": change_percent,
        "volume": None,
        "amount": None,
        "turnover": None,
        "currency": "CNY",
        "trade_date": target,
        "source": "eastmoney_fund_nav",
        "source_ref": f"https://fundf10.eastmoney.com/jjjz_{symbol}.html",
        "source_chain": ["eastmoney_fund_nav"],
        "quality_flags": [],
    }


def _js_string(text: str, name: str) -> str:
    match = re.search(rf"var\s+{re.escape(name)}\s*=\s*[\"'](.*?)[\"']\s*;", text, re.DOTALL)
    return html.unescape(match.group(1)).strip() if match else ""


def _js_json(text: str, name: str) -> Any:
    match = re.search(rf"var\s+{re.escape(name)}\s*=\s*(.*?)\s*;", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _basic_value(raw_html: str, label: str) -> str | None:
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw_html)))
    labels = "|".join(map(re.escape, ("管理费率", "托管费率", "销售服务费率", "最高认购费率")))
    match = re.search(rf"{re.escape(label)}\s*(.*?)\s*(?={labels}|业绩比较基准|跟踪标的|$)", text)
    value = match.group(1).strip() if match else ""
    return value if value and not value.startswith("-") else None


def _plain_text(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))).strip()


def parse_fund_profile(
    symbol: str,
    profile_js: str,
    basic_html: str,
    nav_rows: list[dict[str, Any]],
    cutoff_date: str,
) -> dict[str, object]:
    managers = []
    for row in _js_json(profile_js, "Data_currentFundManager") or []:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        profit = ((row.get("profit") or {}).get("series") or [{}])[0].get("data") or []
        managers.append(
            {
                "name": str(row["name"]),
                "work_time": str(row.get("workTime") or ""),
                "managed_scale": str(row.get("fundSize") or ""),
                "score": _number((row.get("power") or {}).get("avr")),
                "tenure_return_percent": _number((profit[0] if profit else {}).get("y")),
            }
        )
    history = [
        {
            "date": str(row.get("FSRQ")),
            "nav": _number(row.get("DWJZ")),
            "change_percent": _number(row.get("JZZZL")),
            "event": str(row.get("FHSP") or "").strip() or None,
        }
        for row in sorted(nav_rows, key=lambda item: str(item.get("FSRQ") or ""), reverse=True)[:10]
        if _number(row.get("DWJZ")) is not None
    ]
    returns = {
        label: value
        for label, variable in (
            ("近1月", "syl_1y"),
            ("近3月", "syl_3y"),
            ("近6月", "syl_6y"),
            ("近1年", "syl_1n"),
        )
        if (value := _number(_js_string(profile_js, variable))) is not None
    }
    scale_raw = _js_json(profile_js, "Data_fluctuationScale") or {}
    scale_categories = scale_raw.get("categories") or []
    scale_series = scale_raw.get("series") or []
    scale_history = [
        {
            "as_of": str(as_of),
            "size_yi": _number(point.get("y")) if isinstance(point, dict) else None,
            "quarter_change_percent": _number(str(point.get("mom") or "").replace("%", ""))
            if isinstance(point, dict)
            else None,
        }
        for as_of, point in zip(scale_categories, scale_series)
        if as_of and isinstance(point, dict) and _number(point.get("y")) is not None
    ]
    return {
        "symbol": symbol,
        "name": _js_string(profile_js, "fS_name") or symbol,
        "cutoff_date": cutoff_date,
        "nav_history": history,
        "returns": returns,
        "scale_history": scale_history,
        "fees": {
            "management_rate": _basic_value(basic_html, "管理费率"),
            "custodian_rate": _basic_value(basic_html, "托管费率"),
            "sales_service_rate": _basic_value(basic_html, "销售服务费率"),
        },
        "managers": managers[:3],
        "source": "东方财富基金净值与天天基金公开档案",
    }


def parse_fund_holdings_periods(symbol: str, raw: str) -> list[dict[str, object]]:
    """Parse disclosed quarterly holdings without treating disclosure as live positions."""

    text = raw.replace('\\"', '"').replace("\\/", "/")
    starts = [match.start() for match in re.finditer(r"<div[^>]+class=['\"][^'\"]*boxitem", text)]
    sections = [
        text[start : starts[index + 1] if index + 1 < len(starts) else len(text)]
        for index, start in enumerate(starts)
    ]
    periods: list[dict[str, object]] = []
    for section in sections:
        period_match = re.search(r"(20\d{2})年\s*([1-4])季度", section)
        if not period_match:
            continue
        holdings: list[dict[str, object]] = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", section, re.DOTALL):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            if len(cells) < 7:
                continue
            code, name = _plain_text(cells[1]), _plain_text(cells[2])
            if not re.fullmatch(r"\d{6}", code) or not name:
                continue
            weight_index, shares_index, value_index = (6, 7, 8) if len(cells) >= 9 else (4, 5, 6)
            holdings.append(
                {
                    "code": code,
                    "name": name,
                    "weight_percent": _number(_plain_text(cells[weight_index]).replace("%", "")),
                    "shares_10k": _number(_plain_text(cells[shares_index]).replace(",", "")),
                    "market_value_10k": _number(_plain_text(cells[value_index]).replace(",", "")),
                }
            )
        if not holdings:
            continue
        date_match = re.search(r"20\d{2}-\d{2}-\d{2}", _plain_text(section))
        periods.append(
            {
                "period": f"{period_match.group(1)}Q{period_match.group(2)}",
                "as_of": date_match.group(0) if date_match else None,
                "holdings": holdings[:10],
                "source": "天天基金季度持仓明细",
                "source_ref": f"https://fundf10.eastmoney.com/ccmx_{symbol}.html",
                "disclosure_note": "基金持仓按定期报告披露，不代表当前实时持仓。",
            }
        )
    periods.sort(key=lambda item: str(item["period"]), reverse=True)
    return periods


def fetch_fund_snapshot(
    symbol: str,
    cutoff: date,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("基金代码必须是6位数字")
    rows = _fund_nav_rows(symbol, cutoff - timedelta(days=45), cutoff, request)
    profile_url = f"https://fund.eastmoney.com/pingzhongdata/{symbol}.js?v={cutoff.isoformat()}"
    basic_url = f"https://fundf10.eastmoney.com/jbgk_{symbol}.html"
    holdings_url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?" + urllib.parse.urlencode(
        {
            "type": "jjcc",
            "code": symbol,
            "topline": 10,
            "year": cutoff.year - 1,
            "month": "",
            "rt": cutoff.isoformat(),
        }
    )
    current_holdings_url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?" + urllib.parse.urlencode(
        {"type": "jjcc", "code": symbol, "topline": 10, "year": "", "month": "", "rt": cutoff.isoformat()}
    )
    profile = request(profile_url).decode("utf-8", "replace")
    basic = request(basic_url).decode("utf-8", "replace")
    result = parse_fund_profile(symbol, profile, basic, rows, cutoff.isoformat())
    current_periods: list[dict[str, object]] = []
    historical_periods: list[dict[str, object]] = []
    data_gaps: list[str] = []
    try:
        current_periods = parse_fund_holdings_periods(
            symbol, request(current_holdings_url).decode("utf-8", "replace")
        )
    except Exception as error:
        data_gaps.append(f"current_holdings: {error}")
    try:
        historical_periods = parse_fund_holdings_periods(
            symbol, request(holdings_url).decode("utf-8", "replace")
        )
    except Exception as error:
        data_gaps.append(f"historical_holdings: {error}")
    result["holdings_periods"] = list(
        {str(item["period"]): item for item in [*current_periods, *historical_periods]}.values()
    )[:5]
    result["data_gaps"] = data_gaps
    return result


def fetch_stock_industry(
    symbol: str,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("行业分类目前仅支持A股")
    params = urllib.parse.urlencode(
        {
            "fltt": 2,
            "invt": 2,
            "secid": f"{1 if symbol.startswith(('5', '6', '9')) else 0}.{symbol}",
            "spt": 3,
            "pi": 0,
            "pz": 200,
            "po": 1,
            "fields": "f12,f14,f2,f3,f62,f184,f128",
        }
    )
    payload = json.loads(request(f"https://push2.eastmoney.com/api/qt/slist/get?{params}").decode("utf-8"))
    raw = (payload.get("data") or {}).get("diff") or []
    rows = list(raw.values()) if isinstance(raw, dict) else list(raw)
    classification_rows = [
        {
            "code": str(row.get("f12") or ""),
            "name": str(row.get("f14") or "").strip(),
            "change_percent": _number(row.get("f3")),
            "net_amount": _number(row.get("f62")),
            "net_ratio": _number(row.get("f184")),
        }
        for row in rows
        if str(row.get("f14") or "").strip()
    ]
    names = [str(row["name"]) for row in classification_rows]
    if not names:
        raise ValueError("东方财富行业分类返回为空")
    return {
        "symbol": symbol,
        "industry": names[0],
        "classifications": names[:5],
        "classification_rows": classification_rows[:5],
        "source": "东方财富证券所属板块",
        "source_ref": f"https://quote.eastmoney.com/{_market(symbol)}{symbol}.html",
        "taxonomy_note": "首项采用东方财富当前板块排序；分类体系变化时不可直接跨期比较。",
    }


def fetch_sector_price_history(
    sector_code: str,
    *,
    limit: int = 260,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    if not re.fullmatch(r"BK\d{4}", sector_code):
        raise ValueError("无效的东方财富板块代码")
    params = urllib.parse.urlencode(
        {
            "secid": f"90.{sector_code}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": 101,
            "fqt": 1,
            "end": 20500101,
            "lmt": min(max(limit, 2), 260),
        }
    )
    source_ref = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{params}"
    payload = _eastmoney_json(source_ref, request)
    data = payload.get("data") or {}
    rows = []
    for item in data.get("klines") or []:
        fields = str(item).split(",")
        if len(fields) < 7 or _number(fields[2]) in (None, 0):
            continue
        rows.append(
            {
                "date": fields[0],
                "open": _number(fields[1]),
                "close": _number(fields[2]),
                "high": _number(fields[3]),
                "low": _number(fields[4]),
                "volume": _number(fields[5]),
                "amount": _number(fields[6]),
                "change_percent": _number(fields[8]) if len(fields) > 8 else None,
                "turnover_percent": _number(fields[10]) if len(fields) > 10 else None,
            }
        )
    if len(rows) < 2:
        raise ValueError("板块历史量价样本不足")
    return {
        "sector_code": sector_code,
        "name": str(data.get("name") or sector_code),
        "rows": rows,
        "sample_count": len(rows),
        "as_of": rows[-1]["date"],
        "source": "东方财富板块历史日线",
        "source_ref": source_ref,
    }


EASTMONEY_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _datacenter_rows(
    report_name: str,
    *,
    filter_str: str,
    page_size: int,
    sort_columns: str,
    request: Callable[[str], bytes],
) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "reportName": report_name,
            "columns": "ALL",
            "filter": filter_str,
            "pageSize": page_size,
            "pageNumber": 1,
            "sortColumns": sort_columns,
            "sortTypes": "-1",
            "source": "WEB",
            "client": "WEB",
        }
    )
    payload = json.loads(request(f"{EASTMONEY_DATACENTER_URL}?{params}").decode("utf-8"))
    if not payload.get("success", True):
        raise RuntimeError(str(payload.get("message") or "东方财富数据中心不可用"))
    return list((payload.get("result") or {}).get("data") or [])


def _number(value: object) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _period(row: dict[str, Any]) -> str:
    return str(row.get("REPORT_DATE") or row.get("REPORTDATE") or "")[:10]


def _period_label(value: str) -> str:
    suffix = {"03-31": "一季报", "06-30": "半年报", "09-30": "三季报", "12-31": "年报"}.get(
        value[5:], "报告期"
    )
    return f"{value[:4]}年{suffix}" if len(value) >= 10 else value


def fetch_financial_snapshot(
    symbol: str,
    cutoff: date,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("财务指标目前仅支持A股")
    filter_str = f'(SECURITY_CODE="{symbol}")'
    summary = _datacenter_rows(
        "RPT_LICO_FN_CPD", filter_str=filter_str, page_size=12, sort_columns="REPORTDATE", request=request
    )
    balance = _datacenter_rows(
        "RPT_DMSK_FN_BALANCE",
        filter_str=filter_str,
        page_size=12,
        sort_columns="REPORT_DATE",
        request=request,
    )
    cashflow = _datacenter_rows(
        "RPT_DMSK_FN_CASHFLOW",
        filter_str=filter_str,
        page_size=12,
        sort_columns="REPORT_DATE",
        request=request,
    )
    balance_by_period = {_period(row): row for row in balance if _period(row)}
    cashflow_by_period = {_period(row): row for row in cashflow if _period(row)}
    periods: list[dict[str, object]] = []
    name = symbol
    for row in summary:
        period = _period(row)
        notice = str(row.get("NOTICE_DATE") or row.get("REPORTDATE") or "")[:10]
        if not period or (notice and notice > cutoff.isoformat()):
            continue
        name = str(row.get("SECURITY_NAME_ABBR") or row.get("SECURITY_NAME") or name)
        balance_row, cashflow_row = balance_by_period.get(period, {}), cashflow_by_period.get(period, {})
        operating = _number(cashflow_row.get("NETCASH_OPERATE"))
        capex = _number(cashflow_row.get("CONSTRUCT_LONG_ASSET"))
        periods.append(
            {
                "period": period,
                "period_label": _period_label(period),
                "notice_date": notice or None,
                "basic_eps": _number(row.get("BASIC_EPS")),
                "bps": _number(row.get("BPS")),
                "roe": _number(row.get("WEIGHTAVG_ROE")),
                "gross_margin": _number(row.get("XSMLL")),
                "revenue": _number(row.get("TOTAL_OPERATE_INCOME")),
                "parent_net_profit": _number(row.get("PARENT_NETPROFIT")),
                "debt_asset_ratio": _number(balance_row.get("DEBT_ASSET_RATIO")),
                "total_assets": _number(balance_row.get("TOTAL_ASSETS")),
                "total_liabilities": _number(balance_row.get("TOTAL_LIABILITIES")),
                "total_equity": _number(
                    balance_row.get("TOTAL_EQUITY") or balance_row.get("TOTAL_PARENT_EQUITY")
                ),
                "current_assets": _number(balance_row.get("TOTAL_CURRENT_ASSETS")),
                "current_liabilities": _number(balance_row.get("TOTAL_CURRENT_LIAB")),
                "cash_and_equivalents": _number(balance_row.get("MONETARYFUNDS")),
                "short_term_borrowings": _number(balance_row.get("SHORT_LOAN")),
                "current_portion_noncurrent_liabilities": _number(balance_row.get("NONCURRENT_LIAB_1YEAR")),
                "long_term_borrowings": _number(balance_row.get("LONG_LOAN")),
                "bonds_payable": _number(balance_row.get("BOND_PAYABLE")),
                "goodwill": _number(balance_row.get("GOODWILL")),
                "inventory": _number(balance_row.get("INVENTORY")),
                "accounts_receivable": _number(balance_row.get("ACCOUNTS_RECE")),
                "accounts_payable": _number(balance_row.get("ACCOUNTS_PAYABLE")),
                # Some report vintages retain prepayment/contract-liability values
                # under the legacy ADVANCE_RECEIVABLES field.
                "contract_liabilities": _number(
                    balance_row.get("CONTRACT_LIAB") or balance_row.get("ADVANCE_RECEIVABLES")
                ),
                "operating_cash_flow": operating,
                "capex_cash_paid": capex,
                "free_cash_flow": operating - capex if operating is not None and capex is not None else None,
                "net_cash_invest": _number(cashflow_row.get("NETCASH_INVEST")),
                "net_cash_finance": _number(cashflow_row.get("NETCASH_FINANCE")),
            }
        )
    periods.sort(key=lambda item: str(item["period"]), reverse=True)
    return {
        "security_id": f"CN:{'SSE' if symbol.startswith(('5', '6', '9')) else 'SZSE'}:{symbol}:STOCK",
        "symbol": symbol,
        "name": name,
        "cutoff_date": cutoff.isoformat(),
        "periods": periods[:12],
        "source": "东方财富数据中心财务摘要/资产负债表/现金流量表",
        "free_cash_flow_note": "自由现金流=经营现金流-购建长期资产支付现金，仅作公开数据口径估算。",
    }


def fetch_public_news(
    keyword: str,
    *,
    size: int = 10,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    """Search a bounded public finance-news gateway and retain source links/times."""

    clean_keyword = keyword.strip()
    if not clean_keyword:
        raise ValueError("资讯关键词不能为空")
    params = urllib.parse.urlencode(
        {
            "keyword": clean_keyword,
            "size": min(max(size, 1), 20),
            "news_type": 1,
            "lang": "zh-CN",
            "sort_type": 2,
        }
    )
    source_ref = f"https://ai-news-search.futunn.com/news_search?{params}"
    payload = json.loads(request(source_ref).decode("utf-8"))
    if payload.get("code") != 0:
        raise ValueError("公开资讯搜索返回失败")
    terms = clean_keyword.split()
    target, topic = (terms[0], "".join(terms[1:])) if len(terms) > 1 else (clean_keyword, "")
    target_aliases = {target}
    stripped_target = re.sub(r"(?:股份|集团|控股|公司|有限)$", "", target)
    if len(stripped_target) >= 2:
        target_aliases.add(stripped_target)
    if target.startswith(("贵州", "中国", "上海", "深圳")) and len(target) > 3:
        target_aliases.add(target[2:])
    if "ETF" in target.upper():
        target_aliases.update(part for part in re.split("ETF", target, flags=re.IGNORECASE) if len(part) >= 2)
    topic_alias = topic.replace("渠道", "").replace("真实", "")
    rows, seen = [], set()
    for item in payload.get("data") or []:
        title = re.sub(r"<[^>]+>", "", unescape(str(item.get("title") or ""))).strip()
        if not title or title in seen:
            continue
        if topic and (
            not any(alias in title for alias in target_aliases)
            or not any(value in title for value in {topic, topic_alias} if value)
        ):
            continue
        seen.add(title)
        try:
            published_at = datetime.fromtimestamp(
                float(item.get("publish_time")), tz=timezone.utc
            ).isoformat()
        except (TypeError, ValueError, OSError):
            published_at = None
        rows.append(
            {
                "title": title,
                "published_at": published_at,
                "source": "富途公开资讯搜索",
                "url": str(item.get("url") or ""),
            }
        )
    return {
        "keyword": clean_keyword,
        "items": rows,
        "as_of": rows[0]["published_at"] if rows else None,
        "source": "富途公开资讯搜索",
        "source_ref": source_ref,
        "evidence_note": "资讯标题用于定位线索；渠道库存、批价和政策影响需回到可访问原文或官方文件交叉验证。",
    }


def fetch_market_news(
    *,
    now: datetime | None = None,
    size: int = 6,
    news_loader: Callable[..., dict[str, object]] = fetch_public_news,
) -> dict[str, object]:
    """Return a bounded 24-hour cross-market news projection."""

    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed = observed.astimezone(timezone.utc)
    cutoff = observed - timedelta(hours=24)
    market_terms = (
        "市场",
        "收盘",
        "午评",
        "早评",
        "盘前",
        "盘后",
        "全线",
        "指数",
        "大盘",
        "行情",
        "板块",
        "资金流",
        "成交额",
        "领涨",
        "领跌",
        "震荡",
        "回落",
        "反弹",
        "走高",
        "走低",
    )
    # ponytail: this bounded headline heuristic excludes obvious issuer events; upgrade to
    # provider-side market-topic tags if the public gateway exposes them.
    issuer_terms = ("IPO", "冲刺", "招股", "拟发行", "终止发行", "上市公司控制权")
    rows: list[dict[str, object]] = []
    seen_titles: set[str] = set()
    seen_urls: set[str] = set()
    successful_queries = 0
    for region in ("A股", "港股", "美股"):
        try:
            payload = news_loader(region, size=20)
            successful_queries += 1
        except Exception:
            continue
        for item in payload.get("items") or []:
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            published = str(item.get("published_at") or "")
            try:
                published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                continue
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            published_at = published_at.astimezone(timezone.utc)
            if not (cutoff <= published_at <= observed + timedelta(minutes=5)):
                continue
            if (
                region not in title
                or not any(term in title for term in market_terms)
                or any(term in title for term in issuer_terms)
            ):
                continue
            normalized_title = re.sub(r"\W+", "", title).casefold()
            if not title or not url or normalized_title in seen_titles or url in seen_urls:
                continue
            seen_titles.add(normalized_title)
            seen_urls.add(url)
            rows.append(
                {
                    "region": region,
                    "title": title,
                    "published_at": published_at.isoformat(),
                    "url": url,
                    "source": str(item.get("source") or "富途公开资讯搜索"),
                }
            )
    if not successful_queries:
        raise ValueError("24小时大盘资讯来源暂不可用")
    rows.sort(key=lambda item: str(item["published_at"]), reverse=True)
    limit = min(max(size, 1), 10)
    return {
        "date": observed.astimezone(SHANGHAI).date().isoformat(),
        "observed_at": observed.isoformat(),
        "window_hours": 24,
        "total_count": len(rows),
        "items": rows[:limit],
        "source": "富途公开资讯搜索",
    }


INDEX_CODES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000300": "沪深300",
    "sh000688": "科创50",
}
HK_INDEX_CODES = {
    "hkHSI": "恒生指数",
    "hkHSTECH": "恒生科技指数",
    "hkHSCEI": "恒生中国企业指数",
}

# Stable Tencent batch symbols. Keep this ordered because the market page groups
# them by region without inventing a separate ranking.
GLOBAL_INDEX_CODES = (
    ("sh000001", "上证指数", "CN", "CNY"),
    ("sz399001", "深证成指", "CN", "CNY"),
    ("sz399006", "创业板指", "CN", "CNY"),
    ("sh000300", "沪深300", "CN", "CNY"),
    ("sh000688", "科创50", "CN", "CNY"),
    ("sz399005", "中小100", "CN", "CNY"),
    ("bj899050", "北证50", "CN", "CNY"),
    ("hkHSI", "恒生指数", "HK", "HKD"),
    ("hkHSCEI", "国企指数", "HK", "HKD"),
    ("hkHSTECH", "恒生科技指数", "HK", "HKD"),
    ("usINX", "标普500", "US", "USD"),
    ("usIXIC", "纳斯达克", "US", "USD"),
    ("usDJI", "道琼斯", "US", "USD"),
)


def _fetch_eastmoney_market_breadth(
    trade_date: date,
    request: Callable[[str], bytes] = _request,
    *,
    page_size: int = 500,
) -> dict[str, object]:
    """Count every current A-share row; partial pages are never market breadth."""

    page_size = max(1, min(page_size, 500))
    reported_total: int | None = None
    expected_pages = 0
    pages_fetched = 0
    changes: dict[str, float] = {}
    for page in range(1, 101):
        params = urllib.parse.urlencode(
            {
                "pn": page,
                "pz": page_size,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f12,f3",
            }
        )
        payload = json.loads(
            request(f"https://push2.eastmoney.com/api/qt/clist/get?{params}").decode("utf-8-sig")
        )
        data = payload.get("data") or {}
        total = int(data.get("total") or 0)
        raw_rows = data.get("diff") or []
        rows = list(raw_rows.values()) if isinstance(raw_rows, dict) else list(raw_rows)
        if reported_total is None:
            reported_total = total
            expected_pages = math.ceil(total / page_size) if total else 0
        if total != reported_total:
            raise ValueError("全市场涨跌家数刷新期间总数发生变化")
        for row in rows:
            symbol, change = str(row.get("f12") or ""), _number(row.get("f3"))
            if re.fullmatch(r"\d{6}", symbol) and change is not None:
                changes[symbol] = change
        pages_fetched += 1
        if page >= expected_pages:
            break
        # ponytail: this adapter is intentionally serial; the provider's public
        # rate limit is the ceiling, and a cached snapshot is the future upgrade.
        time_module.sleep(1)
    complete = bool(reported_total and pages_fetched == expected_pages and len(changes) == reported_total)
    if not complete:
        raise ValueError(
            f"全市场涨跌家数分页不完整：{len(changes)}/{reported_total or 0}，"
            f"页数{pages_fetched}/{expected_pages}"
        )
    up = sum(value > 0 for value in changes.values())
    down = sum(value < 0 for value in changes.values())
    flat = len(changes) - up - down
    return {
        "available": True,
        "trade_date": trade_date.isoformat(),
        "up": up,
        "down": down,
        "flat": flat,
        "ratio": round(up / down, 4) if down else None,
        "scope": "A股全市场个股（东方财富全分页）",
        "source": "东方财富全市场行情",
        "source_ref": "https://push2.eastmoney.com/api/qt/clist/get",
        "reported_total": reported_total,
        "valid_rows": len(changes),
        "pages_fetched": pages_fetched,
    }


def _fetch_sina_market_breadth(
    trade_date: date,
    request: Callable[[str], bytes],
    *,
    page_size: int,
) -> dict[str, object]:
    changes: dict[str, float] = {}
    pages_fetched = 0
    termination = ""
    size = max(1, min(page_size, 100))
    for page in range(1, 101):
        params = urllib.parse.urlencode(
            {
                "page": page,
                "num": size,
                "sort": "symbol",
                "asc": 1,
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "page",
            }
        )
        rows = json.loads(
            request(
                f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?{params}"
            ).decode("utf-8")
        )
        if not isinstance(rows, list):
            raise ValueError("新浪全市场行情返回格式无效")
        if not rows:
            termination = "empty_page"
            break
        pages_fetched += 1
        for row in rows:
            symbol, change = str(row.get("code") or ""), _number(row.get("changepercent"))
            if re.fullmatch(r"\d{6}", symbol) and change is not None:
                changes[symbol] = change
        if len(rows) < size:
            termination = "short_page"
            break
    if not termination or not changes:
        raise ValueError("新浪全市场行情未完成分页")
    up = sum(value > 0 for value in changes.values())
    down = sum(value < 0 for value in changes.values())
    flat = len(changes) - up - down
    return {
        "available": True,
        "trade_date": trade_date.isoformat(),
        "up": up,
        "down": down,
        "flat": flat,
        "ratio": round(up / down, 4) if down else None,
        "scope": "A股全市场个股（新浪分页至结束）",
        "source": "新浪全市场行情",
        "source_ref": "https://vip.stock.finance.sina.com.cn/quotes_service/",
        "reported_total": None,
        "valid_rows": len(changes),
        "pages_fetched": pages_fetched,
        "pagination_termination": termination,
    }


def fetch_a_share_market_breadth(
    trade_date: date,
    request: Callable[[str], bytes] = _request,
    *,
    page_size: int = 500,
) -> dict[str, object]:
    try:
        return _fetch_eastmoney_market_breadth(trade_date, request=request, page_size=page_size)
    except Exception as primary_error:
        try:
            return _fetch_sina_market_breadth(trade_date, request, page_size=page_size)
        except Exception as fallback_error:
            raise ValueError(
                f"全市场涨跌家数两路数据均不可用：东方财富={primary_error}；新浪={fallback_error}"
            ) from fallback_error


def fetch_global_index_price_volume(
    request: Callable[[str], bytes] = _request,
    *,
    limit: int = 80,
) -> dict[str, dict[str, object]]:
    """Fetch bounded OHLCV statistics for every index shown in Market Overview."""

    results: dict[str, dict[str, object]] = {}
    us_symbols = {"usINX": ".INX", "usIXIC": ".IXIC", "usDJI": ".DJI"}
    for code, name, market, _currency in GLOBAL_INDEX_CODES:
        params = urllib.parse.urlencode({"param": f"{code},day,,,{limit},qfq"})
        payload = json.loads(
            request(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?{params}").decode("utf-8")
        )
        data = (payload.get("data") or {}).get(code) or {}
        raw_rows = data.get("qfqday") or data.get("day") or []
        rows = [
            {
                "date": str(row[0]),
                "open": _number(row[1]),
                "close": _number(row[2]),
                "high": _number(row[3]) if len(row) > 3 else None,
                "low": _number(row[4]) if len(row) > 4 else None,
                "volume": _number(row[5]) if len(row) > 5 else None,
                "amount": _number(row[6]) if len(row) > 6 else None,
            }
            for row in raw_rows
            if len(row) > 2 and _number(row[2]) not in (None, 0)
        ][-limit:]
        source = "腾讯指数日线"
        source_ref = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        if len(rows) < 2 and code == "bj899050":
            try:
                query = urllib.parse.urlencode(
                    {
                        "secid": "0.899050",
                        "fields1": "f1,f2,f3,f4,f5,f6",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                        "klt": 101,
                        "fqt": 1,
                        "beg": 0,
                        "end": 20500101,
                        "lmt": limit,
                    }
                )
                payload = json.loads(
                    request(f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{query}").decode(
                        "utf-8-sig"
                    )
                )
                rows = [
                    {
                        "date": fields[0],
                        "open": _number(fields[1]),
                        "close": _number(fields[2]),
                        "high": _number(fields[3]),
                        "low": _number(fields[4]),
                        "volume": _number(fields[5]),
                        "amount": _number(fields[6]),
                    }
                    for item in ((payload.get("data") or {}).get("klines") or [])
                    if len(fields := str(item).split(",")) >= 7 and _number(fields[2]) not in (None, 0)
                ][-limit:]
                source = "东方财富北证50日线"
                source_ref = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            except Exception:
                rows = []
            if len(rows) < 2:
                query = urllib.parse.urlencode(
                    {
                        "symbol": "bj899050",
                        "scale": 240,
                        "ma": "no",
                        "datalen": limit,
                    }
                )
                try:
                    history = json.loads(
                        request(
                            f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?{query}"
                        ).decode("utf-8")
                    )
                except Exception:
                    history = []
                rows = [
                    {
                        "date": str(item.get("day") or "")[:10],
                        "open": _number(item.get("open")),
                        "close": _number(item.get("close")),
                        "high": _number(item.get("high")),
                        "low": _number(item.get("low")),
                        "volume": _number(item.get("volume")),
                        "amount": None,
                    }
                    for item in history or []
                    if _number(item.get("close")) not in (None, 0)
                ][-limit:]
                source = "新浪北证50日线"
                source_ref = "https://quotes.sina.cn/cn/"
        if len(rows) < 2 and code in us_symbols:
            query = urllib.parse.urlencode({"symbol": us_symbols[code], "num": max(limit, 30)})
            try:
                raw = request(
                    f"https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK?{query}"
                ).decode("utf-8", errors="strict")
            except Exception:
                raw = ""
            match = re.search(r"\((\[.*\])\)", raw, re.S)
            history = json.loads(match.group(1)) if match else []
            rows = [
                {
                    "date": str(item.get("d") or ""),
                    "open": _number(item.get("o")),
                    "close": _number(item.get("c")),
                    "high": _number(item.get("h")),
                    "low": _number(item.get("l")),
                    "volume": _number(item.get("v")),
                    "amount": None,
                }
                for item in history
                if _number(item.get("c")) not in (None, 0)
            ][-limit:]
            source = "新浪美股指数日线"
            source_ref = "https://stock.finance.sina.com.cn/usstock/"
        if len(rows) < 2:
            continue
        results[code] = {
            "name": name,
            "market": market,
            **summarize_price_volume_history(rows),
            "source": source,
            "source_ref": source_ref,
        }
    return results


def _index_quote_is_completed(stamp: str, market: str) -> bool:
    digits = re.sub(r"\D", "", stamp)
    if len(digits) < 12:
        return False
    source_time = int(digits[8:12])
    return source_time >= {"CN": 1500, "HK": 1600, "US": 1600}[market]


def _completed_index_history(
    code: str,
    *,
    quote_date: str,
    market: str,
    request: Callable[[str], bytes],
) -> dict[str, object]:
    params = urllib.parse.urlencode({"param": f"{code},day,,,5,qfq"})
    payload = json.loads(
        request(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?{params}").decode("utf-8")
    )
    data = (payload.get("data") or {}).get(code) or {}
    rows = data.get("qfqday") or data.get("day") or []
    if len(rows) < 2:
        raise ValueError(f"{code}缺少完整日线")
    index = len(rows) - 2 if str(rows[-1][0]) == quote_date else len(rows) - 1
    if index < 1:
        raise ValueError(f"{code}缺少上一完整交易日")
    row, previous = rows[index], rows[index - 1]
    close, previous_close = _number(row[2]), _number(previous[2])
    if close is None or close <= 0 or previous_close is None or previous_close <= 0:
        raise ValueError(f"{code}日线收盘字段无效")
    activity = _number(row[5]) if len(row) > 5 else None
    return {
        "trade_date": str(row[0]),
        "close": close,
        "change": close - previous_close,
        "change_percent": (close / previous_close - 1) * 100,
        "volume": activity if market in {"CN", "US"} else None,
        "amount": activity if market == "HK" else None,
    }


def _market_session(now: datetime, market: str, quote_date: str) -> str:
    zone = SHANGHAI if market in {"CN", "HK"} else ZoneInfo("America/New_York")
    local = now.astimezone(zone)
    if quote_date != local.date().isoformat() or local.weekday() >= 5:
        return "盘后"
    clock = local.timetz().replace(tzinfo=None)
    open_time, close_time = time(9, 30), time(15 if market == "CN" else 16, 0)
    if clock < open_time:
        return "盘前"
    return "盘中" if clock < close_time else "盘后"


def market_session_metadata(
    market: str,
    quote_date: str,
    now: datetime | None = None,
) -> dict[str, str]:
    observed = now or datetime.now(timezone.utc)
    session = _market_session(observed, market, quote_date)
    quote_day = date.fromisoformat(quote_date)
    local_day = observed.astimezone(
        SHANGHAI if market in {"CN", "HK"} else ZoneInfo("America/New_York")
    ).date()
    label_day = local_day if session in {"盘前", "盘中"} else quote_day
    suffix = "实时数据" if session != "盘后" else "收盘数据"
    label = f"{label_day.month}月{label_day.day}日{session}{suffix}"
    if session == "盘前" and quote_day != label_day:
        label += f"（价格截至{quote_day.month}月{quote_day.day}日收盘）"
    return {"session": session, "label": label}


def fetch_global_index_overview(
    request: Callable[[str], bytes] = _request,
    *,
    now_provider: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Fetch each market's latest quote and identify its live trading session."""

    observed = (now_provider or (lambda: datetime.now(timezone.utc)))()
    codes = ",".join(item[0] for item in GLOBAL_INDEX_CODES)
    payload = request(f"https://qt.gtimg.cn/q={codes}").decode("gbk", errors="ignore")
    raw: dict[str, list[str]] = {}
    for line in payload.splitlines():
        match = re.match(r'v_([^=]+)="(.*)";?\s*$', line.strip())
        if match:
            raw[match.group(1)] = match.group(2).split("~")
    rows: list[dict[str, object]] = []
    for code, name, market, currency in GLOBAL_INDEX_CODES:
        fields = raw.get(code) or []
        if len(fields) < 38:
            continue
        close, change, change_percent = _number(fields[3]), _number(fields[31]), _number(fields[32])
        try:
            trade_date = _canonical_date(fields[30])
        except ValueError:
            continue
        if close is None or close <= 0 or change is None or change_percent is None:
            continue
        volume = _number(fields[6]) if market in {"CN", "US"} else None
        amount = None
        if market == "CN":
            raw_amount = _number(fields[37])
            amount = raw_amount * 10_000 if raw_amount is not None else None
        elif market == "HK":
            raw_amount = _number(fields[6])
            amount = raw_amount * 10_000 if raw_amount is not None else None
        session = market_session_metadata(market, trade_date, observed)["session"]
        rows.append(
            {
                "code": code[2:],
                "name": name,
                "market": market,
                "currency": currency,
                "trade_date": trade_date,
                "close": close,
                "change": change,
                "change_percent": change_percent,
                "volume": volume,
                "amount": amount,
                "session": session,
            }
        )
    if len(rows) != len(GLOBAL_INDEX_CODES):
        present_names = {str(row["name"]) for row in rows}
        missing = [name for _, name, _, _ in GLOBAL_INDEX_CODES if name not in present_names]
        raise ValueError(f"指数行情不完整：{'、'.join(missing)}")
    cn_row = next((row for row in rows if row["market"] == "CN"), rows[0])
    cn_session = str(cn_row["session"])
    session_meta = market_session_metadata("CN", str(cn_row["trade_date"]), observed)
    return {
        "date": max(str(row["trade_date"]) for row in rows),
        "session": cn_session,
        "session_label": session_meta["label"],
        "observed_at": observed.isoformat(),
        "rows": rows,
        "source": "腾讯财经公开行情",
        "activity_note": "A股、港股优先展示成交额；美股指数展示成交量。各市场保留各自交易日。",
    }


def summarize_price_volume_history(rows: list[dict[str, object]]) -> dict[str, object]:
    """Return reproducible price/volume features without turning them into signals."""

    valid = [row for row in rows if isinstance(row.get("close"), (int, float)) and float(row["close"]) > 0]
    if not valid:
        return {"sample_count": 0}
    closes = [float(row["close"]) for row in valid]
    metrics: dict[str, object] = {
        "sample_count": len(valid),
        "as_of": valid[-1].get("date"),
        "latest_close": closes[-1],
        "daily_change_percent": round((closes[-1] / closes[-2] - 1) * 100, 4) if len(closes) > 1 else None,
    }
    for days in (5, 20, 60):
        metrics[f"return_{days}d_percent"] = (
            round((closes[-1] / closes[-days - 1] - 1) * 100, 4) if len(closes) > days else None
        )
    for days in (20, 50, 150, 200):
        metrics[f"sma_{days}"] = round(sum(closes[-days:]) / days, 4) if len(closes) >= days else None
    for days in (20, 60, 250):
        if len(closes) >= days:
            high, low = max(closes[-days:]), min(closes[-days:])
            metrics[f"high_{days}d"] = high
            metrics[f"low_{days}d"] = low
            metrics[f"distance_from_{days}d_high_percent"] = round((closes[-1] / high - 1) * 100, 4)
        else:
            metrics[f"high_{days}d"] = metrics[f"low_{days}d"] = None
            metrics[f"distance_from_{days}d_high_percent"] = None
    volumes = [float(row["volume"]) for row in valid[-20:] if isinstance(row.get("volume"), (int, float))]
    if len(volumes) >= 5:
        mean = sum(volumes) / len(volumes)
        variance = sum((value - mean) ** 2 for value in volumes) / len(volumes)
        metrics["volume_zscore"] = round((volumes[-1] - mean) / math.sqrt(variance), 4) if variance else 0.0
        metrics["volume_vs_20d_average"] = round(volumes[-1] / mean, 4) if mean else None
    else:
        metrics["volume_zscore"] = None
        metrics["volume_vs_20d_average"] = None
    true_ranges = []
    for index, row in enumerate(valid[-15:]):
        high, low = row.get("high"), row.get("low")
        if not isinstance(high, (int, float)) or not isinstance(low, (int, float)):
            continue
        previous = float(valid[max(0, len(valid) - 15 + index - 1)]["close"])
        true_ranges.append(
            max(float(high) - float(low), abs(float(high) - previous), abs(float(low) - previous))
        )
    metrics["atr_14"] = (
        round(sum(true_ranges[-14:]) / len(true_ranges[-14:]), 4) if len(true_ranges) >= 14 else None
    )
    metrics["interpretation_boundary"] = "历史量价统计是描述性证据，不构成趋势预测或买卖信号。"
    return metrics


def fetch_index_overview(
    trade_date: date,
    request: Callable[[str], bytes] = _request,
    *,
    region: str = "CN",
) -> dict[str, object]:
    rows = []
    codes = HK_INDEX_CODES if region == "HK" else INDEX_CODES
    for provider_code, name in codes.items():
        target = trade_date.isoformat()
        params = urllib.parse.urlencode({"param": f"{provider_code},day,,{target},80,qfq"})
        payload = json.loads(
            request(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?{params}").decode("utf-8")
        )
        data = (payload.get("data") or {}).get(provider_code) or {}
        history = data.get("qfqday") or data.get("day") or []
        index = next((i for i, row in enumerate(history) if row and row[0] == target), None)
        if index is None:
            raise ValueError(f"{name}缺少{target}精确收盘数据")
        row = history[index]
        close = float(row[2])
        previous = float(history[index - 1][2]) if index > 0 else None
        bounded_history = [
            {
                "date": str(item[0]),
                "open": _number(item[1]),
                "close": _number(item[2]),
                "high": _number(item[3]) if len(item) > 3 else None,
                "low": _number(item[4]) if len(item) > 4 else None,
                "volume": _number(item[5]) if len(item) > 5 else None,
            }
            for item in history[: index + 1]
            if len(item) > 2 and _number(item[2]) not in (None, 0)
        ]
        rows.append(
            {
                "code": provider_code[2:],
                "name": name,
                "close": close,
                "change_percent": (close / previous - 1) * 100 if previous else None,
                "volume": _number(row[5]) if len(row) > 5 else None,
                "amount": _number(row[6]) if len(row) > 6 else None,
                "price_volume": summarize_price_volume_history(bounded_history),
            }
        )
    return {"date": trade_date.isoformat(), "rows": rows, "source": "腾讯财经日线"}


def fetch_lhb(
    trade_date: date, request: Callable[[str], bytes] = _request, limit: int = 8
) -> dict[str, object]:
    raw = _datacenter_rows(
        "RPT_DAILYBILLBOARD_DETAILS",
        filter_str=f"(TRADE_DATE='{trade_date.isoformat()}')",
        page_size=limit,
        sort_columns="BILLBOARD_NET_AMT",
        request=request,
    )
    rows = []
    for row in raw:
        if str(row.get("TRADE_DATE") or "")[:10] != trade_date.isoformat():
            continue
        rows.append(
            {
                "symbol": str(row.get("SECURITY_CODE") or ""),
                "name": str(row.get("SECURITY_NAME_ABBR") or ""),
                "change_percent": _number(row.get("CHANGE_RATE")),
                "buy_amount": _number(row.get("BILLBOARD_BUY_AMT")),
                "sell_amount": _number(row.get("BILLBOARD_SELL_AMT")),
                "net_amount": _number(row.get("BILLBOARD_NET_AMT")),
                "reason": str(row.get("EXPLANATION") or ""),
            }
        )
    return {"date": trade_date.isoformat(), "rows": rows, "source": "东方财富数据中心龙虎榜"}


def fetch_industry_money_flow(
    trade_date: date, request: Callable[[str], bytes] = _request
) -> dict[str, object]:
    def ranked_rows(descending: bool) -> list[dict[str, object]]:
        params = urllib.parse.urlencode(
            {
                "pn": 1,
                "pz": 100,
                "po": 1 if descending else 0,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f62",
                "fs": "m:90+t:2",
                "fields": "f12,f14,f2,f3,f62,f184,f124",
            }
        )
        payload = json.loads(
            request(f"https://push2delay.eastmoney.com/api/qt/clist/get?{params}").decode("utf-8")
        )
        result = []
        for row in list((payload.get("data") or {}).get("diff") or []):
            timestamp = int(row.get("f124") or 0)
            source_date = datetime.fromtimestamp(timestamp, SHANGHAI).date() if timestamp else None
            if source_date == trade_date and row.get("f62") is not None:
                result.append(
                    {
                        "code": row.get("f12"),
                        "name": row.get("f14"),
                        "change_percent": _number(row.get("f3")),
                        "net_amount": _number(row.get("f62")),
                        "net_ratio": _number(row.get("f184")),
                    }
                )
        return result

    inbound_rows = ranked_rows(True)
    outbound_rows = ranked_rows(False)
    if not inbound_rows and not outbound_rows:
        raise ValueError("行业资金流响应不含目标交易日时间戳")
    inbound = [row for row in inbound_rows if float(row["net_amount"] or 0) > 0][:5]
    outbound = [row for row in outbound_rows if float(row["net_amount"] or 0) < 0][:5]
    return {
        "date": trade_date.isoformat(),
        "inbound": inbound,
        "outbound": outbound,
        "source": "东方财富行业板块资金流（延迟节点）",
    }


def _stock_analysis_limit_pools(trade_date: date) -> dict[str, Any]:
    from stock_analysis.integrations import fetch_limit_pools

    return fetch_limit_pools(trade_date.strftime("%Y%m%d"))


def fetch_market_pulse(
    trade_date: date,
    *,
    session: str,
    holdings: list[dict[str, str]],
    now: datetime | None = None,
    pools_loader: Callable[[date], dict[str, Any]] = _stock_analysis_limit_pools,
    news_loader: Callable[..., dict[str, object]] = fetch_public_news,
) -> dict[str, object]:
    """Build the stock-analysis M3/M4 surface, or pre-market holding news."""

    observed = now or datetime.now(timezone.utc)
    if session == "盘前":
        cutoff = observed - timedelta(hours=24)
        news: list[dict[str, object]] = []
        seen: set[str] = set()
        for holding in holdings:
            keyword = str(holding.get("name") or holding.get("symbol") or "").strip()
            if not keyword:
                continue
            try:
                payload = news_loader(keyword, size=6)
            except Exception:
                continue
            for item in payload.get("items") or []:
                published = str(item.get("published_at") or "")
                try:
                    published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
                title = str(item.get("title") or "").strip()
                if published_at < cutoff or not title or title in seen:
                    continue
                seen.add(title)
                news.append(
                    {
                        "symbol": holding.get("symbol"),
                        "name": holding.get("name") or holding.get("symbol"),
                        "title": title,
                        "published_at": published_at.isoformat(),
                        "url": str(item.get("url") or ""),
                        "source": str(item.get("source") or payload.get("source") or "公开财经资讯"),
                    }
                )
        news.sort(key=lambda item: str(item["published_at"]), reverse=True)
        return {
            "date": trade_date.isoformat(),
            "session": session,
            "kind": "holding_news",
            "news": news[:12],
            "source": "stock-analysis 4.14.0 · 持仓公开资讯雷达",
            "skill_version": "4.14.0",
        }

    pools = pools_loader(trade_date)
    expected_date = trade_date.strftime("%Y%m%d")

    def pool(kind: str) -> list[dict[str, Any]]:
        data = (pools.get(kind) or {}).get("data") or {}
        if data.get("qdate") and str(data["qdate"]) != expected_date:
            return []
        return list(data.get("pool") or [])

    up, down, failed = pool("zt"), pool("dt"), pool("zb")
    up_data = (pools.get("zt") or {}).get("data") or {}
    down_data = (pools.get("dt") or {}).get("data") or {}
    failed_data = (pools.get("zb") or {}).get("data") or {}
    up_count = int(up_data.get("tc") or len(up))
    down_count = int(down_data.get("tc") or len(down))
    failed_count = int(failed_data.get("tc") or len(failed))
    theme_counts = Counter(str(item.get("hybk") or "未分类") for item in up)
    first_board = sum(int((item.get("zttj") or {}).get("ct") or 1) <= 1 for item in up)
    leaders = sorted(
        (
            {
                "symbol": str(item.get("c") or ""),
                "name": str(item.get("n") or item.get("c") or ""),
                "theme": str(item.get("hybk") or ""),
                "board_days": int((item.get("zttj") or {}).get("ct") or 1),
                "seal_fund_yi": float(item.get("fund") or 0) / 100_000_000,
            }
            for item in up
        ),
        key=lambda item: (int(item["board_days"]), float(item["seal_fund_yi"])),
        reverse=True,
    )[:10]
    risk_rows = [
        {
            "symbol": str(item.get("c") or ""),
            "name": str(item.get("n") or item.get("c") or ""),
            "theme": str(item.get("hybk") or ""),
            "change_percent": float(item.get("zdp") or 0),
            "kind": label,
        }
        for label, rows in (("跌停", down), ("炸板", failed))
        for item in rows
    ][:10]
    return {
        "date": trade_date.isoformat(),
        "session": session,
        "kind": "limit_pools",
        "m3": {
            "available": bool(up_data and ("tc" in up_data or up)),
            "limit_up_count": up_count,
            "first_board_count": first_board,
            "multi_board_count": max(0, up_count - first_board),
            "seal_fund_yi": sum(float(item.get("fund") or 0) for item in up) / 100_000_000,
            "top_themes": [{"name": name, "count": count} for name, count in theme_counts.most_common(5)],
            "leaders": leaders,
        },
        "m4": {
            "available": bool(down_data or failed_data),
            "limit_down_count": down_count,
            "failed_breakout_count": failed_count,
            "failed_breakout_ratio": failed_count / (up_count + failed_count)
            if up_count or failed_count
            else None,
            "rows": risk_rows,
        },
        "source": "stock-analysis 4.14.0 · 东方财富涨跌停池",
        "skill_version": "4.14.0",
    }


def parse_company_announcements(payload: str, symbol: str, cutoff: str) -> list[dict[str, str]]:
    rows = []
    for item in (json.loads(payload).get("data") or {}).get("list") or []:
        codes = item.get("codes") or []
        if not any(str(code.get("stock_code")) == symbol for code in codes):
            continue
        published_at = str(item.get("notice_date") or "")[:10]
        if not published_at or published_at > cutoff:
            continue
        title = str(item.get("title") or item.get("title_ch") or "").strip()
        article_code = str(item.get("art_code") or "").strip()
        if not title or not article_code:
            continue
        financial_terms = ("年度报告", "半年度报告", "季度报告", "年报", "半年报", "季报")
        rows.append(
            {
                "material_type": "财务报告" if any(term in title for term in financial_terms) else "公司公告",
                "title": title,
                "published_at": published_at,
                "source_name": "东方财富公告中心",
                "source_url": f"https://data.eastmoney.com/notices/detail/{symbol}/{article_code}.html",
                "excerpt": "",
            }
        )
    return rows


def fetch_company_announcements(
    symbol: str,
    cutoff: date,
    request: Callable[[str], bytes] = _request,
) -> list[dict[str, str]]:
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("only six-digit A-share symbols are supported")
    params = urllib.parse.urlencode(
        {
            "sr": -1,
            "page_size": 100,
            "page_index": 1,
            "ann_type": "A",
            "client_source": "web",
            "stock_list": symbol,
            "f_node": 0,
            "s_node": 0,
        }
    )
    url = f"https://np-anotice-stock.eastmoney.com/api/security/ann?{params}"
    return parse_company_announcements(request(url).decode("utf-8"), symbol, cutoff.isoformat())


def parse_hkex_announcements(payload: str, symbol: str, cutoff: str) -> list[dict[str, str]]:
    response = json.loads(payload)
    raw_rows = json.loads(str(response.get("result") or "[]"))
    rows: list[dict[str, str]] = []
    financial_terms = (
        "年度業績",
        "年度业绩",
        "中期業績",
        "中期业绩",
        "季度業績",
        "季度业绩",
        "年報",
        "年报",
        "中期報告",
        "中期报告",
        "ANNUAL RESULTS",
        "INTERIM RESULTS",
        "ANNUAL REPORT",
        "INTERIM REPORT",
    )
    for item in raw_rows:
        codes = set(re.findall(r"\d{5}", _plain_text(item.get("STOCK_CODE"))))
        if symbol not in codes:
            continue
        try:
            published_at = datetime.strptime(str(item.get("DATE_TIME") or ""), "%d/%m/%Y %H:%M").date()
        except ValueError:
            continue
        if published_at.isoformat() > cutoff:
            continue
        title = _plain_text(item.get("TITLE"))
        file_link = str(item.get("FILE_LINK") or "").strip()
        if not title or not file_link.startswith("/"):
            continue
        upper_title = title.upper()
        rows.append(
            {
                "material_type": "财务报告"
                if any(term in upper_title for term in financial_terms)
                else "公司公告",
                "title": title,
                "published_at": published_at.isoformat(),
                "source_name": "香港交易所披露易",
                "source_url": f"https://www1.hkexnews.hk{file_link}",
                "excerpt": _plain_text(item.get("LONG_TEXT")),
            }
        )
    return rows


def fetch_hkex_announcements(
    symbol: str,
    cutoff: date,
    request: Callable[[str], bytes] = _request,
) -> list[dict[str, str]]:
    normalized = symbol.zfill(5)
    if not re.fullmatch(r"\d{5}", normalized):
        raise ValueError("Hong Kong stock code must contain one to five digits")
    prefix_params = urllib.parse.urlencode(
        {"callback": "callback", "lang": "ZH", "market": "SEHK", "name": normalized, "type": "A"}
    )
    prefix = request(f"https://www1.hkexnews.hk/search/prefix.do?{prefix_params}").decode("utf-8-sig")
    match = re.search(r'"stockId"\s*:\s*(\d+)', prefix)
    if not match:
        raise ValueError("HKEX did not return a matching issuer id")
    params = urllib.parse.urlencode(
        {
            "sortDir": 0,
            "sortByOptions": "DateTime",
            "category": 0,
            "market": "SEHK",
            "stockId": match.group(1),
            "documentType": -1,
            "fromDate": (cutoff - timedelta(days=365)).strftime("%Y%m%d"),
            "toDate": cutoff.strftime("%Y%m%d"),
            "title": "",
            "searchType": 0,
            "t1code": -2,
            "t2Gcode": -2,
            "t2code": -2,
            "rowRange": 100,
            "lang": "zh",
        }
    )
    url = f"https://www1.hkexnews.hk/search/titleSearchServlet.do?{params}"
    return parse_hkex_announcements(request(url).decode("utf-8-sig"), normalized, cutoff.isoformat())
