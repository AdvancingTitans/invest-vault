import gzip
import json
import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from invest_vault.providers import (
    _request,
    fetch_a_share_earnings_calendar,
    fetch_a_share_hot_themes,
    fetch_a_share_index_overview,
    fetch_a_share_market_breadth,
    fetch_company_supplemental_evidence,
    fetch_cross_market_index_overview,
    fetch_financial_snapshot,
    fetch_fund_nav_close,
    fetch_fund_redemption_fee_schedule,
    fetch_global_earnings_calendar,
    fetch_global_index_overview,
    fetch_global_index_price_volume,
    fetch_global_market_movers,
    fetch_global_theme_performance,
    fetch_hk_valuation_financial_history,
    fetch_industry_money_flow,
    fetch_market_news,
    fetch_market_pulse,
    fetch_peer_valuations,
    fetch_profit_forecast,
    fetch_public_news,
    fetch_sector_price_history,
    fetch_security_live_quote,
    fetch_security_price_history,
    fetch_security_valuation,
    market_report_stage,
    parse_company_announcements,
    parse_fund_holdings_periods,
    parse_fund_profile,
    parse_hkex_announcements,
    parse_sina_quote,
    parse_tencent_history,
    parse_tencent_quote,
    previous_trade_date,
    target_trade_date,
)


def test_request_decompresses_gzip_body_without_content_encoding(monkeypatch) -> None:
    body = json.dumps({"zygcfx": []}).encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return gzip.compress(body)

    class Opener:
        def open(self, _request, timeout):
            assert timeout == 6
            return Response()

    monkeypatch.setattr("invest_vault.providers.urllib.request.build_opener", lambda *_: Opener())

    assert _request("https://emweb.securities.eastmoney.com/example") == body


def test_open_ended_fund_redemption_schedule_is_parsed_by_holding_period() -> None:
    document = b"""
    <h4><label>\xe8\xb5\x8e\xe5\x9b\x9e\xe8\xb4\xb9\xe7\x8e\x87</label></h4>
    <table><tbody><tr><td>\xe5\xb0\x8f\xe4\xba\x8e7\xe5\xa4\xa9</td><td>1.50%</td></tr>
    <tr><td>\xe5\xa4\xa7\xe4\xba\x8e\xe7\xad\x89\xe4\xba\x8e30\xe5\xa4\xa9</td><td>0.00%</td></tr></tbody></table>
    """

    result = fetch_fund_redemption_fee_schedule("519771", request=lambda _: document)

    assert result["rows"] == [
        {"holding_period": "小于7天", "redemption_fee_percent": 1.5},
        {"holding_period": "大于等于30天", "redemption_fee_percent": 0.0},
    ]


def test_hk_supplemental_company_evidence_uses_public_business_and_board_profile() -> None:
    profile = """
    <table><tr><td>主要業務</td><td>網絡遊戲、廣告及金融科技服務。</td></tr>
    <tr><td>主席名稱</td><td>馬化騰（主席兼行政總裁）</td></tr>
    <tr><td>董事會成員名單</td><td>執行董事：馬化騰</td></tr></table>
    """.encode()

    result = fetch_company_supplemental_evidence(
        "00700", name="腾讯控股", request=lambda _: profile
    )

    assert [len(section["items"]) for section in result["official_sections"]] == [1, 2]
    assert result["source"].startswith("经济通港股公司背景")


def test_hk_forecast_peer_and_history_are_parsed_without_user_confirmation() -> None:
    forecast_html = """
    <div>綜合盈利預測</div><table><tr><td>財政年度</td><td>純利</td><td>每股盈利</td><td>股息</td><td>資產</td><td>最高</td><td>最低</td></tr>
    <tr><td>2026</td><td>237,683.5</td><td>2,626.5</td><td>509.9</td><td>--</td><td>286,900</td><td>221,466</td></tr></table>
    <div>盈利預測概覽</div><table><tr><td>財年</td><td>純利</td><td>EPS</td><td>DPS</td><td>券商</td><td>評級</td><td>目標</td><td>日期</td></tr>
    <tr><td>2026</td><td>238,944</td><td>2,648</td><td>530</td><td>法巴</td><td>買入</td><td>825</td><td>28/01/2026</td></tr></table>
    """
    profile_html = """
    <a href="../industry_detail.php?nature=SNS&amp;subtype=all">軟件服務 (Software)</a>
    <td>主要業務</td><td>網絡遊戲、廣告及金融科技服務。</td>
    """
    industry_html = """
    代號<table><tr><td>00700</td><td>騰訊</td><td></td><td>440</td><td>-1%</td><td>10億</td><td>40,000億</td><td>HKD</td><td>1.2</td><td>15</td></tr>
    <tr><td>09999</td><td>網易</td><td></td><td>190</td><td>-1%</td><td>2億</td><td>6,000億</td><td>HKD</td><td>2.4</td><td>16</td></tr></table>P/E Range為
    """
    history_html = """
    <div>市場價值指標</div><table><tr><td></td><td>2025/12</td><td>與去年比較</td><td>2024/12</td></tr>
    <tr><td>每股盈利 (仙)</td><td>2,474.9</td><td>18.2%</td><td>2,093.8</td></tr>
    <tr><td>每股帳面資產淨值 ($)</td><td>126.548</td><td>--</td><td>105.535</td></tr></table>
    """

    def request(url: str) -> bytes:
        if "quote_profit" in url:
            return forecast_html.encode()
        if "quote_ci_brief" in url:
            return profile_html.encode()
        if "industry_detail" in url:
            return industry_html.encode()
        return history_html.encode()

    forecast = fetch_profit_forecast("00700", request=request)
    peers = fetch_peer_valuations("00700", request=request)
    history = fetch_hk_valuation_financial_history("00700", request=request)

    assert forecast["consensus"][0]["eps_cny"] == 26.265
    assert forecast["coverage"]["institutions"] == 1
    assert peers["status"] == "system_assessed"
    assert peers["rows"][0]["symbol"] == "09999"
    assert "无需用户确认" in peers["comparison_boundary"]
    assert [row["basic_eps"] for row in history["periods"]] == [24.749, 20.938]
    assert [row["bps"] for row in history["periods"]] == [126.548, 105.535]


def test_market_report_stage_is_automatic_for_trade_day_and_holiday() -> None:
    premarket = market_report_stage(datetime(2026, 7, 20, 8, 45, tzinfo=ZoneInfo("Asia/Shanghai")))
    intraday = market_report_stage(datetime(2026, 7, 20, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai")))
    postmarket = market_report_stage(datetime(2026, 7, 20, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
    holiday = market_report_stage(datetime(2026, 10, 2, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai")))

    assert (premarket["session"], premarket["report_date"]) == ("盘前", "2026-07-20")
    assert intraday["session"] == "盘中"
    assert postmarket["session"] == "盘后"
    assert (holiday["session"], holiday["report_date"]) == ("盘后", "2026-09-30")
    assert holiday["label"] == "9月30日盘后行情报告"


def test_market_breadth_requires_complete_unique_population() -> None:
    def request(url: str) -> bytes:
        assert "clist/get" in url
        return json.dumps(
            {
                "data": {
                    "total": 3,
                    "diff": [
                        {"f12": "600001", "f3": 1.2},
                        {"f12": "000001", "f3": -0.5},
                        {"f12": "300001", "f3": 0},
                    ],
                }
            }
        ).encode()

    result = fetch_a_share_market_breadth(date(2026, 7, 20), request=request)

    assert result["available"] is True
    assert (result["up"], result["down"], result["flat"]) == (1, 1, 1)
    assert result["valid_rows"] == result["reported_total"] == 3


def test_market_breadth_falls_back_to_complete_sina_pages() -> None:
    def request(url: str) -> bytes:
        if "clist/get" in url:
            raise OSError("primary unavailable")
        page = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["page"][0])
        rows = (
            [
                {"code": "600001", "changepercent": "1.2"},
                {"code": "000001", "changepercent": "-0.5"},
            ]
            if page == 1
            else []
        )
        return json.dumps(rows).encode()

    result = fetch_a_share_market_breadth(date(2026, 7, 20), request=request, page_size=2)

    assert result["available"] is True
    assert result["source"] == "新浪全市场行情"
    assert result["pagination_termination"] == "empty_page"


def test_global_index_price_volume_keeps_sixty_day_statistics() -> None:
    rows = [
        [f"2026-04-{day:02d}", "100", str(100 + day), str(101 + day), str(99 + day), str(1000 + day)]
        for day in range(1, 31)
    ] + [
        [f"2026-05-{day:02d}", "130", str(130 + day), str(131 + day), str(129 + day), str(2000 + day)]
        for day in range(1, 32)
    ]

    def request(url: str) -> bytes:
        code = re.search(r"param=([^,%]+)", urllib.parse.unquote(url)).group(1)
        return json.dumps({"data": {code: {"day": rows}}}).encode()

    result = fetch_global_index_price_volume(request=request)

    assert result["sh000001"]["sample_count"] == 61
    assert result["sh000001"]["return_60d_percent"] is not None


def test_global_index_price_volume_uses_verified_fallbacks_for_bj_and_us() -> None:
    history = [
        {"d": f"2026-05-{day:02d}", "o": "100", "c": str(100 + day), "h": "120", "l": "90", "v": "1000"}
        for day in range(1, 31)
    ]

    def request(url: str) -> bytes:
        decoded = urllib.parse.unquote(url)
        if "fqkline" in url:
            code = re.search(r"param=([^,%]+)", decoded).group(1)
            return json.dumps(
                {
                    "data": {
                        code: {"day": [] if code.startswith(("bj", "us")) else [["2026-05-01", "1", "2"]] * 2}
                    }
                }
            ).encode()
        if "US_MinKService" in url:
            return f"var({json.dumps(history)})".encode()
        if "push2his" in url:
            klines = [f"2026-05-{day:02d},100,{100 + day},120,90,1000,2000" for day in range(1, 31)]
            return json.dumps({"data": {"klines": klines}}).encode()
        raise AssertionError(url)

    result = fetch_global_index_price_volume(request=request)

    assert result["bj899050"]["sample_count"] == 30
    assert result["usINX"]["sample_count"] == 30
    assert result["usIXIC"]["sample_count"] == 30
    assert result["usDJI"]["sample_count"] == 30


def test_global_index_overview_keeps_all_thirteen_closes_and_activity_fields() -> None:
    codes = {
        "sh000001": (
            "上证指数",
            "3764.15",
            "3882.41",
            "650450984",
            "20260717161402",
            "-118.26",
            "-3.05",
            "124644545",
        ),
        "sz399001": (
            "深证成指",
            "13706.88",
            "14488.65",
            "763770395",
            "20260717161451",
            "-781.77",
            "-5.40",
            "140851331",
        ),
        "sz399006": (
            "创业板指",
            "3428.63",
            "3692.46",
            "230221243",
            "20260717161406",
            "-263.83",
            "-7.15",
            "68260370",
        ),
        "sh000300": (
            "沪深300",
            "4529.10",
            "4698.43",
            "302629886",
            "20260717161408",
            "-169.33",
            "-3.60",
            "92082241",
        ),
        "sh000688": (
            "科创50",
            "1715.40",
            "1846.88",
            "16629024",
            "20260717161414",
            "-131.48",
            "-7.12",
            "19147353",
        ),
        "sz399005": (
            "中小100",
            "8604.67",
            "9045.83",
            "70341078",
            "20260717161403",
            "-441.16",
            "-4.88",
            "23147870",
        ),
        "bj899050": (
            "北证50",
            "1076.38",
            "1101.80",
            "8647241",
            "20260717153625",
            "-25.42",
            "-2.31",
            "1690599.19",
        ),
        "hkHSI": (
            "恒生指数",
            "24562.240",
            "25008.600",
            "34732530.8302",
            "2026/07/17 18:31:09",
            "-446.360",
            "-1.78",
            "34732530.830",
        ),
        "hkHSCEI": (
            "国企指数",
            "8136.730",
            "8318.140",
            "11134217.3095",
            "2026/07/17 16:08:36",
            "-181.410",
            "-2.18",
            "11134217.309",
        ),
        "hkHSTECH": (
            "恒生科技指数",
            "4623.170",
            "4834.440",
            "11550937.9524",
            "2026/07/17 16:08:36",
            "-211.270",
            "-4.37",
            "11550937.952",
        ),
        "usINX": (
            "标普500",
            "7457.69",
            "7533.77",
            "3397503316",
            "2026-07-17 16:43:30",
            "-76.08",
            "-1.01",
            "25337526504700",
        ),
        "usIXIC": (
            "纳斯达克",
            "25520.24",
            "25881.95",
            "6557165689",
            "2026-07-17 17:15:59",
            "-361.71",
            "-1.40",
            "167340466364558",
        ),
        "usDJI": (
            "道琼斯",
            "52146.42",
            "52552.97",
            "549691647",
            "2026-07-17 16:43:30",
            "-406.55",
            "-0.77",
            "28748736408349",
        ),
    }
    lines = []
    for code, (name, close, previous, volume, stamp, change, change_percent, amount) in codes.items():
        fields = [""] * 66
        fields[1], fields[3], fields[4], fields[6] = name, close, previous, volume
        fields[30], fields[31], fields[32], fields[37] = stamp, change, change_percent, amount
        lines.append(f'v_{code}="{"~".join(fields)}";')

    result = fetch_global_index_overview(request=lambda _: "\n".join(lines).encode("gbk"))

    assert [row["name"] for row in result["rows"]] == [item[0] for item in codes.values()]
    assert len(result["rows"]) == 13
    assert result["rows"][0]["change"] == -118.26
    assert result["rows"][0]["amount"] == 1246445450000
    assert result["rows"][7]["amount"] == 347325308302
    assert result["rows"][10]["amount"] is None
    assert result["rows"][10]["volume"] == 3397503316
    assert {row["trade_date"] for row in result["rows"]} == {"2026-07-17"}


def test_cross_market_overview_balances_hk_jp_kr_us_with_per_market_dates() -> None:
    tencent_rows = []
    for code, name in (
        ("hkHSI", "恒生指数"),
        ("hkHSTECH", "恒生科技指数"),
        ("usINX", "标普500"),
        ("usIXIC", "纳斯达克"),
    ):
        fields = [""] * 66
        fields[1], fields[3], fields[4], fields[6] = name, "100", "99", "12345"
        fields[30], fields[31], fields[32], fields[37] = "20260717150000", "1", "1.01", "23456"
        tencent_rows.append(f'v_{code}="{"~".join(fields)}";')

    yahoo_payload = json.dumps({
        "chart": {
            "result": [{
                "timestamp": [1752739200, 1752825600],
                "indicators": {"quote": [{"close": [40000, 40400], "volume": [1000, 1100]}]},
            }]
        }
    }).encode()
    naver_payloads = {
        "KOSPI": b'[["20260716", 1, 1, 1, 3200, 100], ["20260717", 1, 1, 1, 3232, 120]]',
        "KOSDAQ": b'[["20260716", 1, 1, 1, 800, 200], ["20260717", 1, 1, 1, 792, 220]]',
    }

    def request(url: str) -> bytes:
        if "qt.gtimg.cn" in url:
            return "\n".join(tencent_rows).encode("gbk")
        if "query1.finance.yahoo.com" in url:
            return yahoo_payload
        if "api.finance.naver.com" in url:
            symbol = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["symbol"][0]
            return naver_payloads[symbol]
        raise AssertionError(url)

    result = fetch_cross_market_index_overview(
        request=request,
        now_provider=lambda: datetime(2026, 7, 17, 7, 0, tzinfo=timezone.utc),
        scan=lambda *_: {
            "data": [{"s": "TVC:NI225", "d": ["NI225", "Japan 225 Index", 40400, 1, "JPY"]}]
        },
    )

    by_market = {
        market: [row for row in result["rows"] if row["market"] == market]
        for market in ("HK", "JP", "KR", "US")
    }
    assert {market: len(rows) for market, rows in by_market.items()} == {
        "HK": 2, "JP": 1, "KR": 2, "US": 2,
    }
    assert {row["currency"] for row in by_market["JP"]} == {"JPY"}
    assert {row["currency"] for row in by_market["KR"]} == {"KRW"}
    assert all(row["trade_date"] for row in result["rows"])
    assert result["gaps"] == []


def test_nikkei_uses_verified_tradingview_change_and_does_not_show_topix() -> None:
    tencent_rows = []
    for code, name in (("hkHSI", "恒生指数"), ("hkHSTECH", "恒生科技指数"), ("usINX", "标普500"), ("usIXIC", "纳斯达克")):
        fields = [""] * 66
        fields[1], fields[3], fields[4], fields[6] = name, "100", "99", "12345"
        fields[30], fields[31], fields[32], fields[37] = "20260720150000", "1", "1.01", "23456"
        tencent_rows.append(f'v_{code}="{"~".join(fields)}";')

    def request(url: str) -> bytes:
        if "qt.gtimg.cn" in url:
            return "\n".join(tencent_rows).encode("gbk")
        if "api.finance.naver.com" in url:
            return b'[["20260717", 1, 1, 1, 100, 100], ["20260720", 1, 1, 1, 101, 120]]'
        raise AssertionError(url)

    result = fetch_cross_market_index_overview(
        request=request,
        now_provider=lambda: datetime(2026, 7, 20, 8, tzinfo=timezone.utc),
        scan=lambda *_: {"data": [{"s": "TVC:NI225", "d": ["NI225", "Japan 225 Index", 64141.12, -4.03, "JPY"]}]},
    )
    japan = [row for row in result["rows"] if row["market"] == "JP"]
    assert [(row["name"], row["close"], round(row["change_percent"], 2)) for row in japan] == [
        ("日经225", 64141.12, -4.03)
    ]
    assert all(row["name"] != "东证指数" for row in result["rows"])


def test_global_movers_themes_and_monthly_earnings_keep_four_market_coverage() -> None:
    def now() -> datetime:
        return datetime(2026, 7, 20, 8, tzinfo=timezone.utc)

    def movers_scan(market: str, _payload: dict[str, object]) -> dict[str, object]:
        symbol = {"hongkong": "HKEX:700", "japan": "TSE:285A", "korea": "KRX:000660", "america": "NASDAQ:MU"}[market]
        return {"data": [{"s": symbol, "d": [symbol, f"{market} leader", 100, 5, 1000, "USD", 2_000_000_000_000]}]}

    movers = fetch_global_market_movers(scan=movers_scan, now_provider=now)
    assert [item["region"] for item in movers["markets"] if item["rows"]] == ["HK", "JP", "KR", "US"]

    def theme_scan(_market: str, payload: dict[str, object]) -> dict[str, object]:
        tickers = payload["symbols"]["tickers"]  # type: ignore[index]
        return {"data": [{"s": ticker, "d": [ticker, ticker, 100, 2, "USD"]} for ticker in tickers]}

    themes = fetch_global_theme_performance(scan=theme_scan, now_provider=now)
    storage = next(item for item in themes["rows"] if item["name"] == "存储与存储芯片")
    assert storage["available_markets"] == ["HK", "JP", "KR", "US"]

    release = datetime(2026, 7, 25, tzinfo=timezone.utc).timestamp()
    earnings = fetch_global_earnings_calendar(
        scan=lambda market, _payload: {"data": [{"s": f"{market}:ONE", "d": ["ONE", f"{market} company", release, 2_000_000_000_000, "USD"]}]},
        now_provider=now,
    )
    assert [item["region"] for item in earnings["markets"] if item["rows"]] == ["HK", "JP", "KR", "US"]


def test_a_share_themes_and_earnings_keep_source_scope_and_current_month() -> None:
    def now() -> datetime:
        return datetime(2026, 7, 20, 8, tzinfo=timezone.utc)
    theme_payload = {
        "data": {
            "diff": [
                {"f12": "980138", "f14": "存储芯片", "f2": 2877.93, "f3": 3.2}
            ]
        }
    }
    themes = fetch_a_share_hot_themes(
        request=lambda _url: json.dumps(theme_payload).encode(), now_provider=now
    )
    assert len(themes["rows"]) == 6
    assert themes["rows"][0] == {
        "code": "BK1036", "name": "半导体", "change_percent": None,
        "close": None, "available": False,
    }
    assert themes["rows"][1]["name"] == "存储芯片"
    assert themes["rows"][1]["close"] == 2877.93
    assert themes["rows"][1]["available"] is True
    assert "固定展示半导体" in themes["scope_note"]

    domestic_payload = {
        "result": {
            "data": [
                {
                    "SECUCODE": "600000.SH",
                    "SECURITY_NAME_ABBR": "浦发银行",
                    "APPOINT_PUBLISH_DATE": "2026-07-25 00:00:00",
                    "REPORT_TYPE_NAME": "2026年 半年报",
                }
            ]
        }
    }
    earnings = fetch_a_share_earnings_calendar(
        request=lambda _url: json.dumps(domestic_payload).encode(),
        scan=lambda *_: (_ for _ in ()).throw(AssertionError("国内预约表可用时不应调用备用源")),
        now_provider=now,
    )
    assert earnings["month"] == "2026-07"
    assert earnings["rows"][0]["release_date"] == "2026-07-25"
    assert earnings["source"] == "东方财富 A股财报预约表"


def test_verified_us_indices_do_not_invent_index_volume() -> None:
    scan_rows = [
        {"s": "TVC:NI225", "d": ["NI225", "Japan 225 Index", 64141.12, -4.03, "JPY"]},
        {"s": "SP:SPX", "d": ["SPX", "S&P 500", 7505.26, 0.64, "USD"]},
        {"s": "NASDAQ:IXIC", "d": ["IXIC", "Nasdaq Composite", 25520.24, -1.40, "USD"]},
    ]
    result = fetch_cross_market_index_overview(
        request=lambda url: (
            b'v_hkHSI="";\nv_hkHSTECH="";\nv_usINX="";\nv_usIXIC="";'
            if "qt.gtimg.cn" in url
            else b'[["20260720", 1, 1, 1, 100, 100]]'
        ),
        now_provider=lambda: datetime(2026, 7, 20, 18, tzinfo=timezone.utc),
        scan=lambda *_: {"data": scan_rows},
    )
    us_rows = [row for row in result["rows"] if row["market"] == "US"]
    assert len(us_rows) == 2
    assert all(row["volume"] is None for row in us_rows)


def test_global_index_overview_keeps_live_values_and_labels_the_active_session() -> None:
    from invest_vault.providers import GLOBAL_INDEX_CODES

    lines = []
    for code, name, market, _ in GLOBAL_INDEX_CODES:
        fields = [""] * 66
        fields[1], fields[3], fields[4], fields[6] = name, "90", "110", "5"
        fields[30] = (
            "20260720100000"
            if market == "CN"
            else "2026/07/20 10:00:00"
            if market == "HK"
            else "2026-07-20 10:00:00"
        )
        fields[31], fields[32], fields[37] = "-20", "-18.18", "5"
        lines.append(f'v_{code}="{"~".join(fields)}";')
    quote_payload = "\n".join(lines).encode("gbk")

    def request(url: str) -> bytes:
        assert "qt.gtimg.cn" in url
        return quote_payload

    result = fetch_a_share_index_overview(
        request=request,
        now_provider=lambda: datetime(2026, 7, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert {row["trade_date"] for row in result["rows"]} == {"2026-07-20"}
    assert {row["close"] for row in result["rows"]} == {90}
    assert {row["change"] for row in result["rows"]} == {-20}
    assert result["session"] == "盘中"
    assert result["session_label"] == "7月20日盘中实时数据"
    assert {row["session"] for row in result["rows"] if row["market"] == "CN"} == {"盘中"}


def test_profit_forecast_keeps_consensus_growth_and_revision_history() -> None:
    payload = {
        "jgyc": [
            {
                "PUBLISH_DATE": "2026-07-18",
                "ORG_NAME_ABBR": "近六月平均",
                "YEAR1": 2025,
                "YEAR_MARK1": "A",
                "EPS1": 2.0,
                "YEAR2": 2026,
                "YEAR_MARK2": "E",
                "EPS2": 2.4,
                "PE2": 20.0,
                "YEAR3": 2027,
                "YEAR_MARK3": "E",
                "EPS3": 3.0,
                "PE3": 16.0,
            }
        ],
        "ycmx": [
            {
                "PUBLISH_DATE": "2026-06-14",
                "ORG_NAME_ABBR": "甲证券",
                "YEAR2": 2026,
                "EPS2": 2.3,
                "YEAR3": 2027,
                "EPS3": 2.8,
            },
            {
                "PUBLISH_DATE": "2026-07-10",
                "ORG_NAME_ABBR": "乙证券",
                "YEAR2": 2026,
                "EPS2": 2.5,
                "YEAR3": 2027,
                "EPS3": 3.1,
            },
        ],
    }

    result = fetch_profit_forecast("600519", request=lambda _: json.dumps(payload).encode())

    assert result["consensus"][1]["year"] == 2026
    assert result["consensus"][1]["eps_growth_percent"] == 20.0
    assert result["revision_history"][0]["publish_date"] == "2026-07-10"
    assert result["coverage"]["institutions"] == 2


def test_company_supplement_uses_short_topic_queries_that_public_search_can_match(monkeypatch) -> None:
    queries = []
    monkeypatch.setattr(
        "invest_vault.providers.fetch_public_news",
        lambda keyword, **_: (
            queries.append(keyword)
            or {"items": [{"title": keyword, "url": f"https://example.test/{len(queries)}"}]}
        ),
    )

    result = fetch_company_supplemental_evidence(
        "300760",
        name="医药公司",
        request=lambda url: json.dumps({"zygcfx": [], "gglb": [], "cgbd": []}).encode(),
    )

    assert "医药公司 收购" in queries
    assert "医药公司 客户" in queries
    assert "医药公司 毛利率" in queries
    assert "医药公司 管理层" in queries
    assert sum(len(item["items"]) for item in result["topic_searches"]) == len(queries)


def test_peer_valuations_falls_back_to_delayed_eastmoney_host(monkeypatch) -> None:
    monkeypatch.setattr(
        "invest_vault.providers.fetch_stock_industry",
        lambda *_, **__: {"industry": "医疗设备", "classification_rows": [{"code": "BK1605"}]},
    )
    urls = []

    def request(url: str) -> bytes:
        urls.append(url)
        if "push2.eastmoney.com" in url:
            raise ConnectionError("primary disconnected")
        return json.dumps(
            {"data": {"diff": [{"f12": "300003", "f14": "同行", "f2": 10, "f9": 20, "f20": 100, "f23": 2}]}}
        ).encode()

    result = fetch_peer_valuations("300760", request=request)

    assert result["rows"][0]["name"] == "同行"
    assert any("push2delay.eastmoney.com" in url for url in urls)


def test_industry_money_flow_fetches_separate_inbound_and_outbound_rankings() -> None:
    inbound = {
        "data": {
            "diff": [
                {
                    "f12": "BK0459",
                    "f14": "元件",
                    "f3": 6.31,
                    "f62": 14137115136,
                    "f184": 9.18,
                    "f124": 1784014494,
                },
            ]
        }
    }
    outbound = {
        "data": {
            "diff": [
                {
                    "f12": "BK1036",
                    "f14": "半导体",
                    "f3": -2.1,
                    "f62": -12420579328,
                    "f184": -4.2,
                    "f124": 1784014494,
                },
            ]
        }
    }
    urls: list[str] = []

    def request(url: str) -> bytes:
        urls.append(url)
        return json.dumps(inbound if "po=1" in url else outbound).encode()

    result = fetch_industry_money_flow(date(2026, 7, 14), request=request)

    assert len(urls) == 2
    assert result["inbound"][0]["name"] == "元件"
    assert result["outbound"][0]["name"] == "半导体"
    assert result["outbound"][0]["net_amount"] < 0


def test_market_pulse_uses_stock_analysis_limit_pools_for_m3_and_m4() -> None:
    pools = {
        "zt": {
            "data": {
                "qdate": "20260717",
                "tc": 2,
                "pool": [
                    {"c": "600001", "n": "首板样本", "hybk": "医药", "fund": 200_000_000, "zttj": {"ct": 1}},
                    {"c": "600002", "n": "连板样本", "hybk": "医药", "fund": 300_000_000, "zttj": {"ct": 3}},
                ],
            }
        },
        "dt": {
            "data": {
                "qdate": "20260717",
                "tc": 1,
                "pool": [
                    {"c": "600003", "n": "跌停样本", "hybk": "消费", "zdp": -10.0},
                ],
            }
        },
        "zb": {
            "data": {
                "qdate": "20260717",
                "tc": 1,
                "pool": [
                    {"c": "600004", "n": "炸板样本", "hybk": "科技", "zdp": 4.0},
                ],
            }
        },
    }

    result = fetch_market_pulse(
        date(2026, 7, 17),
        session="盘后",
        holdings=[],
        pools_loader=lambda _: pools,
    )

    assert result["kind"] == "limit_pools"
    assert result["skill_version"] == "4.15.0"
    assert result["m3"] == {
        "available": True,
        "limit_up_count": 2,
        "first_board_count": 1,
        "multi_board_count": 1,
        "seal_fund_yi": 5.0,
        "top_themes": [{"name": "医药", "count": 2}],
        "leaders": [
            {"symbol": "600002", "name": "连板样本", "theme": "医药", "board_days": 3, "seal_fund_yi": 3.0},
            {"symbol": "600001", "name": "首板样本", "theme": "医药", "board_days": 1, "seal_fund_yi": 2.0},
        ],
    }
    assert result["m4"]["limit_down_count"] == 1
    assert result["m4"]["failed_breakout_count"] == 1
    assert result["m4"]["failed_breakout_ratio"] == 1 / 3


def test_premarket_pulse_shows_only_holding_news_from_the_last_24_hours() -> None:
    def news_loader(keyword: str, *, size: int = 10) -> dict[str, object]:
        assert keyword == "贵州茅台"
        assert size == 6
        return {
            "items": [
                {
                    "title": "茅台最新经营动态",
                    "published_at": "2026-07-20T00:30:00+00:00",
                    "url": "https://example.test/new",
                    "source": "测试资讯",
                },
                {
                    "title": "过期资讯",
                    "published_at": "2026-07-18T00:00:00+00:00",
                    "url": "https://example.test/old",
                    "source": "测试资讯",
                },
            ]
        }

    result = fetch_market_pulse(
        date(2026, 7, 20),
        session="盘前",
        holdings=[{"symbol": "600519", "name": "贵州茅台"}],
        now=datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc),
        news_loader=news_loader,
    )

    assert result["kind"] == "holding_news"
    assert [item["title"] for item in result["news"]] == ["茅台最新经营动态"]


def test_market_news_is_bounded_recent_deduplicated_and_market_wide() -> None:
    def news_loader(keyword: str, *, size: int = 10) -> dict[str, object]:
        assert size == 30
        rows = {
            "A股": [
                {
                    "title": "A股市场宽幅震荡，银行电力板块领涨",
                    "published_at": "2026-07-18T10:00:00+00:00",
                    "url": "https://example.test/a",
                    "source": "富途",
                },
                {
                    "title": "某公司拟发行A股股票",
                    "published_at": "2026-07-18T11:00:00+00:00",
                    "url": "https://example.test/company",
                    "source": "富途",
                },
            ],
            "港股": [
                {
                    "title": "港股收盘：恒指震荡回落",
                    "published_at": "2026-07-18T09:00:00+00:00",
                    "url": "https://example.test/hk",
                    "source": "富途",
                },
                {
                    "title": "港股收盘：恒指震荡回落",
                    "published_at": "2026-07-18T09:00:00+00:00",
                    "url": "https://example.test/hk-copy",
                    "source": "富途",
                },
            ],
            "美股": [
                {
                    "title": "美股全线下行，芯片板块继续承压",
                    "published_at": "2026-07-16T08:00:00+00:00",
                    "url": "https://example.test/old",
                    "source": "富途",
                },
            ],
        }
        region = "A股" if keyword in {"A股大盘", "A股市场", "沪深股市"} else keyword
        return {"items": rows[region]}

    result = fetch_market_news(
        now=datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
        news_loader=news_loader,
    )

    assert result["window_hours"] == 24
    assert result["total_count"] == 2
    assert [item["region"] for item in result["items"]] == ["A股", "港股"]
    assert [item["title"] for item in result["items"]] == [
        "A股市场宽幅震荡，银行电力板块领涨",
        "港股收盘：恒指震荡回落",
    ]


def test_a_share_market_news_uses_fallback_to_reach_five_recent_items() -> None:
    observed = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
    fallback = [
        {
            "region": "A股",
            "title": f"A股大盘第{index}条盘后行情",
            "published_at": (observed - timedelta(minutes=index)).isoformat(),
            "url": f"https://finance.sina.com.cn/{index}",
            "source": "新浪财经",
        }
        for index in range(1, 7)
    ]
    result = fetch_market_news(
        now=observed,
        size=6,
        regions=("A股",),
        news_loader=lambda *_args, **_kwargs: {"items": []},
        fallback_loader=lambda *_args: fallback,
    )
    assert len(result["items"]) == 6
    assert all(item["region"] == "A股" for item in result["items"])


def test_a_share_market_news_has_no_24_hour_limit_but_rejects_non_a_share_issuers() -> None:
    observed = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)

    def news_loader(_keyword: str, *, size: int = 30) -> dict[str, object]:
        assert size == 30
        return {"items": [
            {
                "title": "A股市场盘后：沪指震荡，两市成交额回升",
                "published_at": "2026-07-15T08:00:00+00:00",
                "url": "https://example.test/a-old",
                "source": "公开资讯",
            },
            {
                "title": "Armata股价因超级细菌疗法晚期试验进展而飙升",
                "published_at": "2026-07-20T08:00:00+00:00",
                "url": "https://example.test/armata",
                "source": "公开资讯",
            },
        ]}

    result = fetch_market_news(
        now=observed, size=1, regions=("A股",), news_loader=news_loader, max_age_hours=None
    )

    assert result["window_hours"] is None
    assert [item["title"] for item in result["items"]] == [
        "A股市场盘后：沪指震荡，两市成交额回升"
    ]


def test_a_share_market_news_rejects_partial_refresh_instead_of_overwriting_complete_archive() -> None:
    observed = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
    one_valid_item = {
        "title": "A股市场盘后：沪指震荡，两市成交额回升",
        "published_at": "2026-07-20T08:00:00+00:00",
        "url": "https://example.test/a-only",
        "source": "公开资讯",
    }

    with pytest.raises(ValueError, match="仅取得 1/6 条严格匹配结果"):
        fetch_market_news(
            now=observed,
            regions=("A股",),
            news_loader=lambda *_args, **_kwargs: {"items": [one_valid_item]},
            max_age_hours=None,
        )


def test_hkex_announcements_are_official_symbol_scoped_and_keep_pdf_links() -> None:
    payload = '{"result":"[{\\"STOCK_CODE\\":\\"00700\\u003cbr/\\u003e80700\\",\\"TITLE\\":\\"翌日披露報表\\",\\"LONG_TEXT\\":\\"翌日披露報表 - [股份購回]\\",\\"DATE_TIME\\":\\"09/07/2026 17:58\\",\\"FILE_LINK\\":\\"/listedco/listconews/sehk/2026/0709/tencent.pdf\\"},{\\"STOCK_CODE\\":\\"00005\\",\\"TITLE\\":\\"其他公司公告\\",\\"LONG_TEXT\\":\\"\\",\\"DATE_TIME\\":\\"08/07/2026 17:00\\",\\"FILE_LINK\\":\\"/other.pdf\\"},{\\"STOCK_CODE\\":\\"00700\\",\\"TITLE\\":\\"截至二零二五年十二月三十一日止年度業績公告\\",\\"LONG_TEXT\\":\\"年度業績\\",\\"DATE_TIME\\":\\"18/03/2026 12:00\\",\\"FILE_LINK\\":\\"/listedco/listconews/sehk/2026/0318/results.pdf\\"}]"}'
    rows = parse_hkex_announcements(payload, "00700", "2026-07-10")

    assert len(rows) == 2
    assert rows[0]["source_name"] == "香港交易所披露易"
    assert rows[0]["source_url"] == "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0709/tencent.pdf"
    assert rows[1]["material_type"] == "财务报告"


def test_fund_nav_close_uses_exact_official_nav_date() -> None:
    payload = '{"Data":{"LSJZList":[{"FSRQ":"2026-07-13","DWJZ":"1.2925","JZZZL":"-4.70","FHSP":""},{"FSRQ":"2026-07-10","DWJZ":"1.3563","JZZZL":"-6.38","FHSP":""}]}}'
    quote = fetch_fund_nav_close("CN:SSE:512480:FUND", date(2026, 7, 13), request=lambda _: payload.encode())
    assert quote["price"] == 1.2925
    assert quote["change_percent"] == -4.7
    assert quote["trade_date"] == "2026-07-13"
    assert quote["source"] == "eastmoney_fund_nav"


def test_fund_profile_exposes_fees_manager_and_recent_nav_without_stock_fields() -> None:
    profile = 'var fS_name = "半导体ETF国联安";var syl_1y="-8.2";var Data_currentFundManager = [{"name":"黄欣","workTime":"4年","fundSize":"120亿元","power":{"avr":"72"},"profit":{"series":[{"data":[{"y":"12.3"}]}]}}];'
    basic = "<table><tr><th>基金经理人</th><td>黄欣</td></tr><tr><th>管理费率</th><td>0.50%（每年）</td></tr><tr><th>托管费率</th><td>0.10%（每年）</td></tr></table>"
    nav = [{"FSRQ": "2026-07-13", "DWJZ": "1.2925", "JZZZL": "-4.70", "FHSP": ""}]
    result = parse_fund_profile("512480", profile, basic, nav, "2026-07-13")
    assert result["name"] == "半导体ETF国联安"
    assert result["fees"]["management_rate"] == "0.50%（每年）"
    assert result["managers"][0]["name"] == "黄欣"
    assert result["nav_history"][0]["nav"] == 1.2925


def test_fund_holdings_periods_keep_disclosure_dates_and_support_rebalance_diff() -> None:
    payload = """var apidata={ content:\"<div class='boxitem w790'><h4 class='t'><label class='left'>测试基金 2026年1季度股票投资明细</label></h4><font>2026-03-31</font><table><tr><td>1</td><td>600519</td><td>贵州茅台</td><td></td><td></td><td></td><td>8.50%</td><td>10.00</td><td>1200.00</td></tr></table></div><div class='boxitem w790'><h4 class='t'><label class='left'>测试基金 2025年4季度股票投资明细</label></h4><font>2025-12-31</font><table><tr><td>1</td><td>000858</td><td>五粮液</td><td></td><td></td><td></td><td>7.00%</td><td>20.00</td><td>900.00</td></tr></table></div>\",arryear:[2026,2025]};"""

    periods = parse_fund_holdings_periods("161725", payload)

    assert [item["period"] for item in periods] == ["2026Q1", "2025Q4"]
    assert periods[0]["as_of"] == "2026-03-31"
    assert periods[0]["holdings"][0] == {
        "code": "600519",
        "name": "贵州茅台",
        "weight_percent": 8.5,
        "shares_10k": 10.0,
        "market_value_10k": 1200.0,
    }


def test_tencent_quote_parser_preserves_real_price_and_provenance() -> None:
    fields = [""] * 47
    fields[1], fields[2], fields[3], fields[4] = "贵州茅台", "600519", "1182.19", "1199.30"
    fields[6], fields[30], fields[31], fields[32] = "24100", "20260709150000", "-17.11", "-1.43"
    fields[37], fields[38] = "403500", "0.27"
    fields[39], fields[44], fields[46] = "19.00", "15713.53", "6.75"
    payload = 'v_sh600519="' + "~".join(fields) + '";'
    quote = parse_tencent_quote(payload, "600519")
    assert quote["price"] == 1182.19
    assert quote["trade_date"] == "2026-07-09"
    assert quote["change_percent"] == -1.43
    assert quote["source"] == "tencent_quote"
    assert quote["pe_ttm"] == 19.0
    assert quote["pb"] == 6.75


def test_financial_snapshot_keeps_cashflow_bridge_fields_for_quarter_decomposition() -> None:
    def request(url: str) -> bytes:
        if "RPT_LICO_FN_CPD" in url:
            rows = [
                {
                    "SECURITY_CODE": "600519",
                    "SECURITY_NAME_ABBR": "贵州茅台",
                    "REPORT_DATE": "2025-12-31",
                    "NOTICE_DATE": "2026-03-31",
                    "TOTAL_OPERATE_INCOME": 1000,
                    "PARENT_NETPROFIT": 500,
                }
            ]
        elif "RPT_DMSK_FN_BALANCE" in url:
            rows = [
                {
                    "REPORT_DATE": "2025-12-31",
                    "TOTAL_ASSETS": 3000,
                    "TOTAL_LIABILITIES": 600,
                    "DEBT_ASSET_RATIO": 20,
                    "MONETARYFUNDS": 900,
                    "INVENTORY": 700,
                    "ACCOUNTS_RECE": 30,
                    "ACCOUNTS_PAYABLE": 80,
                    "ADVANCE_RECEIVABLES": 120,
                }
            ]
        else:
            rows = [
                {
                    "REPORT_DATE": "2025-12-31",
                    "NETCASH_OPERATE": 420,
                    "CONSTRUCT_LONG_ASSET": 80,
                    "NETCASH_INVEST": -120,
                    "NETCASH_FINANCE": -200,
                }
            ]
        return json.dumps({"success": True, "result": {"data": rows}}).encode()

    result = fetch_financial_snapshot("600519", date(2026, 7, 17), request=request)

    period = result["periods"][0]
    assert period["revenue"] == 1000
    assert period["parent_net_profit"] == 500
    assert period["capex_cash_paid"] == 80
    assert period["free_cash_flow"] == 340
    assert period["net_cash_invest"] == -120
    assert period["net_cash_finance"] == -200
    assert period["cash_and_equivalents"] == 900
    assert period["inventory"] == 700
    assert period["accounts_receivable"] == 30
    assert period["accounts_payable"] == 80
    assert period["contract_liabilities"] == 120


def test_hk_valuation_uses_hk_specific_pb_field() -> None:
    fields = [""] * 66
    fields[1], fields[2], fields[3], fields[30] = "腾讯控股", "00700", "458.60", "2026/07/17 13:16:03"
    fields[39], fields[44], fields[46], fields[58] = "16.74", "41680.09", "TENCENT", "3.31"
    payload = 'v_hk00700="' + "~".join(fields) + '";'

    result = fetch_security_valuation("HK:HKEX:00700:STOCK", request=lambda _: payload.encode("gbk"))

    assert result["pe_ttm"] == 16.74
    assert result["pb"] == 3.31
    assert result["currency"] == "HKD"


def test_hk_live_quote_preserves_quote_fields_and_valuation() -> None:
    fields = [""] * 66
    fields[1], fields[2], fields[3], fields[4] = "腾讯控股", "00700", "461.60", "454.20"
    fields[6], fields[30], fields[37], fields[38] = "188000", "2026/07/17 16:08:03", "8640000000", "0.23"
    fields[39], fields[44], fields[58] = "16.86", "41945.30", "3.34"
    payload = 'v_hk00700="' + "~".join(fields) + '";'

    result = fetch_security_live_quote("HK:HKEX:00700:STOCK", request=lambda _: payload.encode("gbk"))

    assert result["name"] == "腾讯控股"
    assert result["price"] == 461.6
    assert result["previous_close"] == 454.2
    assert result["change_percent"] == 1.6292
    assert result["pe_ttm"] == 16.86
    assert result["pb"] == 3.34
    assert result["trade_date"] == "2026-07-17"
    assert result["source_chain"] == ["tencent_quote"]


def test_hk_live_quote_falls_back_to_latest_verified_kline() -> None:
    kline = {
        "data": {
            "hk00700": {
                "day": [
                    ["2026-07-16", "450.0", "454.2", "456.0", "449.0", "1200"],
                    ["2026-07-17", "455.0", "461.6", "463.0", "453.0", "1800"],
                ],
                "qt": {"hk00700": ["", "腾讯控股"]},
            }
        }
    }

    def request(url: str) -> bytes:
        if "qt.gtimg.cn" in url:
            raise OSError("quote host disconnected")
        return json.dumps(kline).encode()

    result = fetch_security_live_quote("HK:HKEX:00700:STOCK", request=request)

    assert result["name"] == "腾讯控股"
    assert result["price"] == 461.6
    assert result["trade_date"] == "2026-07-17"
    assert result["change_percent"] == 1.6292
    assert result["source"] == "tencent_kline"
    assert result["source_chain"] == ["tencent_quote", "tencent_kline"]
    assert "quote host disconnected" in result["fallback_reason"]


def test_public_topic_news_requires_both_security_alias_and_topic() -> None:
    payload = {
        "code": 0,
        "data": [
            {
                "title": "Pentair泳池渠道库存去化",
                "publish_time": "1710000000",
                "url": "https://example.test/other",
            },
            {
                "title": "贵州茅台渠道库存下降",
                "publish_time": "1710000001",
                "url": "https://example.test/moutai",
            },
            {"title": "茅台批价企稳", "publish_time": "1710000002", "url": "https://example.test/price"},
        ],
    }

    result = fetch_public_news("贵州茅台 渠道库存", request=lambda _: json.dumps(payload).encode())

    assert [item["title"] for item in result["items"]] == ["贵州茅台渠道库存下降"]


def test_sina_quote_parser_rejects_non_positive_prices() -> None:
    fields = ["贵州茅台", "1190", "1199.30", "0", "1200", "1180", "0", "0", "2410000", "4035000000"]
    fields.extend([""] * 20)
    fields.extend(["2026-07-09", "15:00:00"])
    payload = 'var hq_str_sh600519="' + ",".join(fields) + '";'
    try:
        parse_sina_quote(payload, "600519")
    except ValueError as error:
        assert "price" in str(error)
    else:
        raise AssertionError("non-positive quote must be rejected")


def test_target_trade_date_only_advances_after_the_post_close_gate() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    assert target_trade_date(datetime(2026, 7, 13, 10, 0, tzinfo=timezone)).isoformat() == "2026-07-10"
    assert target_trade_date(datetime(2026, 7, 13, 18, 0, tzinfo=timezone)).isoformat() == "2026-07-13"
    assert target_trade_date(datetime(2026, 7, 12, 18, 0, tzinfo=timezone)).isoformat() == "2026-07-10"
    assert (
        previous_trade_date(target_trade_date(datetime(2026, 7, 13, 18, 0, tzinfo=timezone))).isoformat()
        == "2026-07-10"
    )


def test_tencent_history_requires_an_exact_completed_trade_date() -> None:
    payload = '{"data":{"sh600519":{"qfqday":[["2026-07-09","1190","1182.19","1200","1180","10"],["2026-07-10","1180","1204.98","1210","1170","12"]],"qt":{"sh600519":["","贵州茅台"]}}}}'
    quote = parse_tencent_history(payload, "600519", "2026-07-10")
    assert quote["price"] == 1204.98
    assert quote["previous_close"] == 1182.19
    assert quote["trade_date"] == "2026-07-10"
    assert quote["source"] == "tencent_kline"

    try:
        parse_tencent_history(payload, "600519", "2026-07-08")
    except ValueError as error:
        assert "exact trade date" in str(error)
    else:
        raise AssertionError("historical data must never fall back to another date")


def test_security_history_preserves_ohlcv_for_framework_analysis() -> None:
    payload = '{"data":{"sh600519":{"qfqday":[["2026-07-09","1190","1182.19","1200","1180","24100"],["2026-07-10","1180","1204.98","1210","1170","32100"]]}}}'

    result = fetch_security_price_history("CN:SSE:600519:STOCK", limit=80, request=lambda _: payload.encode())

    assert result["rows"][-1] == {
        "date": "2026-07-10",
        "open": 1180.0,
        "close": 1204.98,
        "high": 1210.0,
        "low": 1170.0,
        "volume": 32100.0,
    }


def test_fund_performance_history_uses_cumulative_nav_across_share_splits() -> None:
    payload = {
        "TotalCount": 2,
        "Data": {
            "LSJZList": [
                {"FSRQ": "2026-07-17", "DWJZ": "1.00", "LJJZ": "4.00"},
                {"FSRQ": "2026-07-16", "DWJZ": "2.00", "LJJZ": "4.00"},
            ]
        },
    }

    result = fetch_security_price_history(
        "CN:SSE:512480:FUND",
        limit=2,
        request=lambda _: json.dumps(payload).encode(),
    )

    assert [row["close"] for row in result["rows"]] == [4.0, 4.0]
    assert [row["unit_nav"] for row in result["rows"]] == [2.0, 1.0]


def test_sector_history_preserves_volume_amount_and_daily_change() -> None:
    payload = {
        "data": {
            "code": "BK0438",
            "name": "食品饮料",
            "klines": [
                "2026-07-16,100,102,103,99,1000,2000,4,2,2,1",
                "2026-07-17,102,99,104,98,1500,3000,6,-2.94,-3,1.5",
            ],
        }
    }

    result = fetch_sector_price_history("BK0438", limit=80, request=lambda _: json.dumps(payload).encode())

    assert result["name"] == "食品饮料"
    assert result["rows"][-1]["volume"] == 1500.0
    assert result["rows"][-1]["amount"] == 3000.0
    assert result["rows"][-1]["change_percent"] == -2.94


def test_company_announcements_are_symbol_scoped_and_point_in_time() -> None:
    payload = '{"data":{"list":[{"art_code":"A1","codes":[{"stock_code":"600519","short_name":"贵州茅台"}],"notice_date":"2026-04-25 00:00:00","title":"贵州茅台2026年第一季度报告"},{"art_code":"A2","codes":[{"stock_code":"000858","short_name":"五粮液"}],"notice_date":"2026-04-26 00:00:00","title":"五粮液公告"},{"art_code":"A3","codes":[{"stock_code":"600519","short_name":"贵州茅台"}],"notice_date":"2026-07-11 00:00:00","title":"贵州茅台未来公告"}]}}'
    rows = parse_company_announcements(payload, "600519", "2026-07-10")
    assert len(rows) == 1
    assert rows[0]["material_type"] == "财务报告"
    assert rows[0]["published_at"] == "2026-04-25"
    assert rows[0]["source_url"].endswith("/600519/A1.html")
