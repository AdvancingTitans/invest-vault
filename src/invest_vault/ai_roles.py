"""App-owned, read-only research roles adapted from public investor-lens ideas."""

from __future__ import annotations

from typing import Any

from stock_analysis.committee_selection import select_committee

STOCK_ANALYSIS_SKILL_VERSION = "4.14.0"


def _role(role_id: str, name: str, focus: str, questions: str, risk: str) -> dict[str, Any]:
    return {
        "role_id": role_id,
        "name": name,
        "focus": focus,
        "questions": questions,
        "risk_focus": risk,
    }


AI_ROLES = (
    _role("general", "通用模式", "平衡整理事实、用户判断、反证和数据缺口", "先回答问题，再说明证据、推断与未知项", "证据不足、逻辑跳跃和时点混淆"),
    _role("buffett", "巴菲特", "商业质量、护城河、管理层、资本配置和安全边际", "生意是否可理解且能长期创造自由现金流？价格是否留有安全边际？", "永久损失、护城河退化、资本配置失误和高估值"),
    _role("munger", "芒格", "多元思维、反向思考、激励、机会成本", "什么最可能让判断失败？利益相关方的激励是否一致？", "坏生意、坏激励、复杂性和确认偏误"),
    _role("graham", "格雷厄姆", "资产负债表、盈利稳定性、估值纪律和下行保护", "悲观情景下资产和盈利能否保护本金？", "杠杆、周期峰值盈利、资产质量和估值回撤"),
    _role("klarman", "卡拉曼", "绝对回报、复杂性折价、催化剂和现金选择权", "折价来自真实风险还是市场厌恶？价值如何释放？", "永久损失、流动性陷阱、价值陷阱和催化剂失效"),
    _role("lynch", "彼得·林奇", "可理解的增长故事、公司类型和盈利兑现", "增长能否被财务和运营指标验证？估值与增长是否匹配？", "增长失速、故事破裂和财务质量恶化"),
    _role("o_neil", "欧奈尔", "盈利加速、行业领导力、机构需求和价格强度", "季度盈利与销售是否加速？量价是否确认领导地位？", "假突破、盈利减速、机构撤退和追高风险"),
    _role("wood", "伍德", "颠覆式创新、渗透率、技术成本曲线和长期可选性", "创新是否转化为采用率、单位经济和规模优势？", "技术路线失败、融资稀释、竞争替代和远期估值"),
    _role("dalio", "达利欧", "宏观周期、信用、流动性、相关性和情景分析", "不同增长与通胀情景下，标的和组合暴露如何变化？", "周期错判、信用收缩、相关性上升和集中度"),
    _role("soros", "索罗斯", "反身性、预期差、政策拐点和非对称性", "价格与基本面是否形成自我强化反馈？共识何时反转？", "反馈逆转、政策突变和仓位非对称失效"),
    _role("livermore", "利弗莫尔", "趋势、关键点确认和风险纪律", "趋势和成交量是否确认？判断错误的失效条件是什么？", "突破失败、逆势加仓、止损失效和过大仓位"),
    _role("minervini", "米勒维尼", "趋势模板、盈利加速、波动收缩和风险收益比", "是否为强势领导者并处于低风险观察区？", "趋势破坏、波动扩张、买点过晚和止损过宽"),
    _role("simons", "西蒙斯", "数据定义、可重复信号、样本外稳健性和交易成本", "样本、基准、因子暴露和样本外结果是否足够？", "过拟合、小样本、滑点、拥挤和不可复现"),
    _role("duan_yongping", "段永平", "商业本质、用户价值、企业文化和长期现金创造", "公司为用户创造什么长期价值？是否在能力圈内？", "文化变坏、用户心智下降、能力圈外和价格不合理"),
    _role("zhang_kun", "张坤", "高质量商业模式、长期自由现金流、竞争格局和机会成本", "ROIC、现金流、治理和竞争格局是否支持长期持有？", "资本回报下滑、治理恶化、竞争加剧和机会成本"),
    _role("feng_liu", "冯柳", "市场认知、赔率、困境反转和边际变化", "市场为何这样定价？可验证的认知差和边际改善在哪里？", "价值陷阱、改善证伪、赔率误判和流动性"),
)

ROLES_BY_ID = {role["role_id"]: role for role in AI_ROLES}

COMMITTEE_FUNCTIONS = {
    "buffett": "基本面研究", "lynch": "基本面研究", "wood": "基本面研究",
    "duan_yongping": "基本面研究", "zhang_kun": "基本面研究",
    "graham": "估值与预期", "klarman": "估值与反方审查", "feng_liu": "市场定价与反方审查",
    "munger": "反方审查", "o_neil": "市场与催化剂", "soros": "市场与催化剂",
    "livermore": "市场与催化剂", "minervini": "市场与催化剂",
    "dalio": "风险与组合", "simons": "风险与组合",
}

DEEP_RESEARCH_TERMS = (
    "深度", "复盘", "投委会", "投研委员会", "完整报告", "研究报告", "全面分析", "持仓逻辑", "原投资逻辑",
    "证伪", "情景分析", "归因", "风险审查", "行业格局", "组合影响", "六模块", "m1", "m6",
)


def is_deep_research_request(question: str) -> bool:
    """Keep committee mode for report-sized requests, not one-fact questions."""

    normalized = question.strip().lower()
    return any(term in normalized for term in DEEP_RESEARCH_TERMS) or (
        len(normalized) >= 36
        and any(term in normalized for term in ("分析", "判断", "评估", "比较", "影响", "风险", "逻辑"))
    )


def committee_plan(security_id: str, question: str) -> dict[str, Any]:
    """Use stock-analysis' question-driven six-member committee contract."""

    is_fund = security_id.endswith(":FUND")
    is_market = security_id == "MARKET:GLOBAL:OVERVIEW"
    role_ids = list(select_committee(question, asset_type="fund" if is_fund else "company"))
    return {
        "research_question": question.strip(),
        "scene": "market" if is_market else "fund" if is_fund else "security",
        "roles": role_ids,
        "assignments": [
            {"role_id": role_id, "function": COMMITTEE_FUNCTIONS[role_id]}
            for role_id in role_ids
        ],
        "reason": "协调员依据研究问题选择 6 位相关且互补的委员",
        "skill_version": STOCK_ANALYSIS_SKILL_VERSION,
        "stages": ["planning", "evidence", "analysis", "conflicts", "risk_review", "reporting"],
        "completion_criteria": ["逐项说明证据范围", "保留未解决分歧", "给出条件化观察清单", "附可读信源"],
    }


def get_role(role_id: str) -> dict[str, Any]:
    try:
        return ROLES_BY_ID[role_id]
    except KeyError as error:
        raise ValueError("未知的研究角色") from error
