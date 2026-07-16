import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from invest_vault.providers import (
    fetch_fund_nav_close,
    fetch_industry_money_flow,
    parse_company_announcements,
    parse_fund_profile,
    parse_hkex_announcements,
    parse_sina_quote,
    parse_tencent_history,
    parse_tencent_quote,
    previous_trade_date,
    target_trade_date,
)


def test_industry_money_flow_fetches_separate_inbound_and_outbound_rankings() -> None:
    inbound = {"data": {"diff": [
        {"f12": "BK0459", "f14": "元件", "f3": 6.31, "f62": 14137115136, "f184": 9.18, "f124": 1784014494},
    ]}}
    outbound = {"data": {"diff": [
        {"f12": "BK1036", "f14": "半导体", "f3": -2.1, "f62": -12420579328, "f184": -4.2, "f124": 1784014494},
    ]}}
    urls: list[str] = []

    def request(url: str) -> bytes:
        urls.append(url)
        return json.dumps(inbound if "po=1" in url else outbound).encode()

    result = fetch_industry_money_flow(date(2026, 7, 14), request=request)

    assert len(urls) == 2
    assert result["inbound"][0]["name"] == "元件"
    assert result["outbound"][0]["name"] == "半导体"
    assert result["outbound"][0]["net_amount"] < 0


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
    basic = '<table><tr><th>基金经理人</th><td>黄欣</td></tr><tr><th>管理费率</th><td>0.50%（每年）</td></tr><tr><th>托管费率</th><td>0.10%（每年）</td></tr></table>'
    nav = [{"FSRQ": "2026-07-13", "DWJZ": "1.2925", "JZZZL": "-4.70", "FHSP": ""}]
    result = parse_fund_profile("512480", profile, basic, nav, "2026-07-13")
    assert result["name"] == "半导体ETF国联安"
    assert result["fees"]["management_rate"] == "0.50%（每年）"
    assert result["managers"][0]["name"] == "黄欣"
    assert result["nav_history"][0]["nav"] == 1.2925


def test_tencent_quote_parser_preserves_real_price_and_provenance() -> None:
    fields = [""] * 39
    fields[1], fields[2], fields[3], fields[4] = "贵州茅台", "600519", "1182.19", "1199.30"
    fields[6], fields[30], fields[31], fields[32] = "24100", "20260709150000", "-17.11", "-1.43"
    fields[37], fields[38] = "403500", "0.27"
    payload = 'v_sh600519="' + "~".join(fields) + '";'
    quote = parse_tencent_quote(payload, "600519")
    assert quote["price"] == 1182.19
    assert quote["trade_date"] == "2026-07-09"
    assert quote["change_percent"] == -1.43
    assert quote["source"] == "tencent_quote"


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
    assert previous_trade_date(target_trade_date(datetime(2026, 7, 13, 18, 0, tzinfo=timezone))).isoformat() == "2026-07-10"


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


def test_company_announcements_are_symbol_scoped_and_point_in_time() -> None:
    payload = '{"data":{"list":[{"art_code":"A1","codes":[{"stock_code":"600519","short_name":"贵州茅台"}],"notice_date":"2026-04-25 00:00:00","title":"贵州茅台2026年第一季度报告"},{"art_code":"A2","codes":[{"stock_code":"000858","short_name":"五粮液"}],"notice_date":"2026-04-26 00:00:00","title":"五粮液公告"},{"art_code":"A3","codes":[{"stock_code":"600519","short_name":"贵州茅台"}],"notice_date":"2026-07-11 00:00:00","title":"贵州茅台未来公告"}]}}'
    rows = parse_company_announcements(payload, "600519", "2026-07-10")
    assert len(rows) == 1
    assert rows[0]["material_type"] == "财务报告"
    assert rows[0]["published_at"] == "2026-04-25"
    assert rows[0]["source_url"].endswith("/600519/A1.html")
