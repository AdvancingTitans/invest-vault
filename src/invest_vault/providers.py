"""Minimal public quote adapters with no source-project runtime dependency."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
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
    name_fields = ((data.get("qt") or {}).get(code) or [])
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
    quote_fields = ((data.get("qt") or {}).get(provider_code) or [])
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
    referer = "https://fundf10.eastmoney.com/" if "fund.eastmoney.com" in url or "fundf10.eastmoney.com" in url else "https://finance.sina.com.cn/"
    request = urllib.request.Request(
        url,
        headers={"Referer": referer, "User-Agent": "Mozilla/5.0 InvestVault/0.1"},
    )
    with opener.open(request, timeout=6) as response:
        return response.read()


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
            raise RuntimeError(f"quote providers unavailable: Tencent={first_error}; Sina={second_error}") from second_error


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
    symbol: str, start: date, end: date, request: Callable[[str], bytes]
) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "fundCode": symbol,
            "pageIndex": 1,
            "pageSize": 60,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
        }
    )
    payload = json.loads(request(f"{FUND_NAV_URL}?{params}").decode("utf-8"))
    rows = list((payload.get("Data") or {}).get("LSJZList") or [])
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
    for row in (_js_json(profile_js, "Data_currentFundManager") or []):
        if not isinstance(row, dict) or not row.get("name"):
            continue
        profit = (((row.get("profit") or {}).get("series") or [{}])[0].get("data") or [])
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
        for label, variable in (("近1月", "syl_1y"), ("近3月", "syl_3y"), ("近6月", "syl_6y"), ("近1年", "syl_1n"))
        if (value := _number(_js_string(profile_js, variable))) is not None
    }
    return {
        "symbol": symbol,
        "name": _js_string(profile_js, "fS_name") or symbol,
        "cutoff_date": cutoff_date,
        "nav_history": history,
        "returns": returns,
        "fees": {
            "management_rate": _basic_value(basic_html, "管理费率"),
            "custodian_rate": _basic_value(basic_html, "托管费率"),
            "sales_service_rate": _basic_value(basic_html, "销售服务费率"),
        },
        "managers": managers[:3],
        "source": "东方财富基金净值与天天基金公开档案",
    }


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
    profile = request(profile_url).decode("utf-8", "replace")
    basic = request(basic_url).decode("utf-8", "replace")
    return parse_fund_profile(symbol, profile, basic, rows, cutoff.isoformat())


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
    suffix = {"03-31": "一季报", "06-30": "半年报", "09-30": "三季报", "12-31": "年报"}.get(value[5:], "报告期")
    return f"{value[:4]}年{suffix}" if len(value) >= 10 else value


def fetch_financial_snapshot(
    symbol: str,
    cutoff: date,
    request: Callable[[str], bytes] = _request,
) -> dict[str, object]:
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("财务指标目前仅支持A股")
    filter_str = f'(SECURITY_CODE="{symbol}")'
    summary = _datacenter_rows("RPT_LICO_FN_CPD", filter_str=filter_str, page_size=12, sort_columns="REPORTDATE", request=request)
    balance = _datacenter_rows("RPT_DMSK_FN_BALANCE", filter_str=filter_str, page_size=12, sort_columns="REPORT_DATE", request=request)
    cashflow = _datacenter_rows("RPT_DMSK_FN_CASHFLOW", filter_str=filter_str, page_size=12, sort_columns="REPORT_DATE", request=request)
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
                "roe": _number(row.get("WEIGHTAVG_ROE")),
                "gross_margin": _number(row.get("XSMLL")),
                "debt_asset_ratio": _number(balance_row.get("DEBT_ASSET_RATIO")),
                "operating_cash_flow": operating,
                "free_cash_flow": operating - capex if operating is not None and capex is not None else None,
            }
        )
    periods.sort(key=lambda item: str(item["period"]), reverse=True)
    return {
        "security_id": f"CN:{'SSE' if symbol.startswith(('5', '6', '9')) else 'SZSE'}:{symbol}:STOCK",
        "symbol": symbol,
        "name": name,
        "cutoff_date": cutoff.isoformat(),
        "periods": periods[:8],
        "source": "东方财富数据中心财务摘要/资产负债表/现金流量表",
        "free_cash_flow_note": "自由现金流=经营现金流-购建长期资产支付现金，仅作公开数据口径估算。",
    }


INDEX_CODES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000300": "沪深300",
    "sh000688": "科创50",
}


def fetch_index_overview(trade_date: date, request: Callable[[str], bytes] = _request) -> dict[str, object]:
    rows = []
    for provider_code, name in INDEX_CODES.items():
        target = trade_date.isoformat()
        params = urllib.parse.urlencode({"param": f"{provider_code},day,,{target},3,qfq"})
        payload = json.loads(request(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?{params}").decode("utf-8"))
        data = (payload.get("data") or {}).get(provider_code) or {}
        history = data.get("qfqday") or data.get("day") or []
        index = next((i for i, row in enumerate(history) if row and row[0] == target), None)
        if index is None:
            raise ValueError(f"{name}缺少{target}精确收盘数据")
        row = history[index]
        close = float(row[2])
        previous = float(history[index - 1][2]) if index > 0 else None
        rows.append({"code": provider_code[2:], "name": name, "close": close, "change_percent": (close / previous - 1) * 100 if previous else None})
    return {"date": trade_date.isoformat(), "rows": rows, "source": "腾讯财经日线"}


def fetch_lhb(trade_date: date, request: Callable[[str], bytes] = _request, limit: int = 8) -> dict[str, object]:
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


def fetch_industry_money_flow(trade_date: date, request: Callable[[str], bytes] = _request) -> dict[str, object]:
    def ranked_rows(descending: bool) -> list[dict[str, object]]:
        params = urllib.parse.urlencode({
            "pn": 1,
            "pz": 100,
            "po": 1 if descending else 0,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f62",
            "fs": "m:90+t:2",
            "fields": "f12,f14,f2,f3,f62,f184,f124",
        })
        payload = json.loads(request(f"https://push2delay.eastmoney.com/api/qt/clist/get?{params}").decode("utf-8"))
        result = []
        for row in list((payload.get("data") or {}).get("diff") or []):
            timestamp = int(row.get("f124") or 0)
            source_date = datetime.fromtimestamp(timestamp, SHANGHAI).date() if timestamp else None
            if source_date == trade_date and row.get("f62") is not None:
                result.append({"code": row.get("f12"), "name": row.get("f14"), "change_percent": _number(row.get("f3")), "net_amount": _number(row.get("f62")), "net_ratio": _number(row.get("f184"))})
        return result

    inbound_rows = ranked_rows(True)
    outbound_rows = ranked_rows(False)
    if not inbound_rows and not outbound_rows:
        raise ValueError("行业资金流响应不含目标交易日时间戳")
    inbound = [row for row in inbound_rows if float(row["net_amount"] or 0) > 0][:5]
    outbound = [row for row in outbound_rows if float(row["net_amount"] or 0) < 0][:5]
    return {"date": trade_date.isoformat(), "inbound": inbound, "outbound": outbound, "source": "东方财富行业板块资金流（延迟节点）"}


def parse_company_announcements(payload: str, symbol: str, cutoff: str) -> list[dict[str, str]]:
    rows = []
    for item in ((json.loads(payload).get("data") or {}).get("list") or []):
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
        "年度業績", "年度业绩", "中期業績", "中期业绩", "季度業績", "季度业绩",
        "年報", "年报", "中期報告", "中期报告", "ANNUAL RESULTS", "INTERIM RESULTS",
        "ANNUAL REPORT", "INTERIM REPORT",
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
                "material_type": "财务报告" if any(term in upper_title for term in financial_terms) else "公司公告",
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
