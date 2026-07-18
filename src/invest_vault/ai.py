"""Optional Codex app-server integration and review-before-save quick notes."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .ai_roles import committee_plan, get_role, is_deep_research_request
from .ai_skills import MARKET_OVERVIEW_SECURITY_ID, AppResearchSkillLayer, ResearchSkillLayer
from .ledger import Vault
from .research import ResearchStore

CHAT_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content", "cited_evidence_ids", "assumptions", "unknowns"],
    "properties": {
        "content": {"type": "string"},
        "cited_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
}

INVESTMENT_TERMS = (
    "股票", "基金", "债券", "证券", "投资", "理财", "持仓", "组合", "市场", "行业", "公司", "财报",
    "公告", "盈利", "利润", "收入", "现金流", "估值", "市盈率", "市净率", "roe", "roic", "价格", "股价",
    "趋势", "成交量", "资金", "风险", "回撤", "收益", "分红", "护城河", "管理层", "资产", "负债", "宏观",
    "利率", "通胀", "汇率", "政策", "经济", "商业模式", "竞争", "催化剂", "证据", "这家公司", "这个标的",
    "stock", "fund", "bond", "portfolio", "market", "finance", "financial", "invest", "valuation", "earnings",
    "买入", "卖出", "加仓", "减仓", "补仓", "仓位", "能买吗", "值得买", "基本面", "技术面", "增长", "营收",
    "毛利", "净利", "股息", "净值", "波动", "牛市", "熊市",
)
FOLLOW_UP_TERMS = ("为什么", "展开", "继续", "详细", "反方", "依据", "来源", "怎么看", "是否成立", "变化", "影响")
MARKET_REPORT_TERMS = ("盘前", "盘中", "盘后", "大盘", "市场", "行情", "指数", "复盘")

MARKET_REPORT_ROLE: dict[str, object] = {
    "role_id": "market_report",
    "report_kind": "market",
    "name": "市场行情助手",
    "focus": "仅生成当前盘前、盘中或盘后大盘行情报告，并结合用户本地持仓给出条件化观察建议",
    "questions": "主要指数和成交发生了什么、行业资金如何变化、用户持仓暴露下一步应核对什么",
    "risk_focus": "数据时段错配、把成本权重冒充实时市值权重、无条件买卖建议",
}


def market_report_role(role: dict[str, object]) -> dict[str, object]:
    if role["role_id"] == "general":
        return MARKET_REPORT_ROLE
    return {
        "role_id": role["role_id"],
        "report_kind": "market",
        "name": role["name"],
        "focus": f"仅生成盘前、盘中或盘后大盘行情报告；全篇采用{role['name']}的证据优先级与判断顺序。{role['focus']}",
        "questions": f"{role['questions']}；同时说明主要指数、行业资金、龙虎榜和本地持仓暴露。",
        "risk_focus": f"{role['risk_focus']}；不得回答单股问题、不得把推导数量冒充真实成交数量。",
    }


def is_investment_question(content: str) -> bool:
    normalized = content.strip().lower()
    return any(term in normalized for term in (*INVESTMENT_TERMS, *FOLLOW_UP_TERMS))


def is_market_report_question(content: str) -> bool:
    normalized = content.strip().lower()
    return any(term in normalized for term in MARKET_REPORT_TERMS)

QUICK_NOTE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "facts", "user_judgements", "open_questions", "planned_actions", "tags"],
    "properties": {
        "title": {"type": "string"},
        "facts": {"type": "array", "items": {"type": "string"}},
        "user_judgements": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "planned_actions": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}


class AIUnavailableError(RuntimeError):
    pass


class AIProvider(Protocol):
    def status(self) -> dict[str, object]: ...

    def start_chatgpt_login(self) -> dict[str, object]: ...

    def logout(self) -> dict[str, object]: ...

    def list_models(self) -> list[dict[str, object]]: ...

    def configure_models(self, settings: dict[str, dict[str, str | None]]) -> None: ...

    def quick_note(self, raw_text: str, security_id: str) -> dict[str, object]: ...

    def chat(
        self, *, role: dict[str, object], messages: list[dict[str, str]], context: str,
        use_runtime_market_skill: bool = False,
    ) -> dict[str, object]: ...

    def close(self) -> None: ...


class CodexAppServerProvider:
    """Small synchronous JSONL client around the locally installed Codex app-server."""

    def __init__(
        self,
        runtime_directory: Path,
        *,
        executable: str | None = None,
        node_executable: str | None = None,
        timeout: float = 120.0,
        chat_timeout: float = 300.0,
        market_skill_directory: Path | None = None,
    ) -> None:
        self.runtime_directory = Path(runtime_directory)
        self.executable = executable or self._find_executable()
        self.node_executable = node_executable or self._find_node_executable()
        self.timeout = timeout
        self.chat_timeout = chat_timeout
        self.market_skill_directory = market_skill_directory or self._bundled_market_skill_directory()
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._condition = threading.Condition()
        self._start_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._responses: dict[int, dict[str, Any]] = {}
        self._notifications: list[dict[str, Any]] = []
        self._stderr_lines: list[str] = []
        self._next_id = 1
        self._fatal_error: str | None = None
        self._runtime_market_skill: dict[str, str] | None | bool = False
        self._model_settings: dict[str, dict[str, str | None]] = {}

    @staticmethod
    def _bundled_market_skill_directory() -> Path:
        frozen_root = getattr(sys, "_MEIPASS", None)
        if frozen_root:
            return Path(frozen_root) / "skills" / "stock-analysis"
        return Path(__file__).parents[2] / "skills" / "stock-analysis"

    @staticmethod
    def _find_executable() -> str | None:
        discovered = shutil.which("codex")
        if discovered:
            return discovered
        # Finder-launched macOS apps inherit a minimal PATH. These are normal
        # executable locations; credential files are never inspected.
        candidates = (
            Path.home() / ".local" / "bin" / "codex",
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
            Path("/Applications/Codex.app/Contents/Resources/codex"),
        )
        return next((str(path) for path in candidates if path.is_file()), None)

    @staticmethod
    def _find_node_executable() -> str | None:
        discovered = shutil.which("node")
        if discovered:
            return discovered
        candidates = [
            Path.home() / ".local" / "bin" / "node",
            Path("/opt/homebrew/bin/node"),
            Path("/opt/homebrew/opt/node/bin/node"),
            Path("/usr/local/bin/node"),
            Path("/usr/local/opt/node/bin/node"),
        ]
        for root in (Path("/opt/homebrew/opt"), Path("/usr/local/opt")):
            if root.is_dir():
                candidates.extend(sorted(root.glob("node@*/bin/node"), reverse=True))
        return next((str(path) for path in candidates if path.is_file()), None)

    def _app_server_command(self) -> list[str]:
        assert self.executable
        resolved = Path(self.executable).resolve()
        if resolved.suffix.lower() in {".js", ".mjs", ".cjs"}:
            if not self.node_executable:
                raise AIUnavailableError("已检测到 Codex CLI，但未找到其所需的 Node.js 运行时")
            return [self.node_executable, str(resolved), "app-server", "--stdio"]
        return [self.executable, "app-server", "--stdio"]

    def _subprocess_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        prefixes = [
            str(Path(path).parent)
            for path in (self.node_executable, self.executable)
            if path
        ]
        existing = environment.get("PATH", "")
        environment["PATH"] = os.pathsep.join(dict.fromkeys([*prefixes, *existing.split(os.pathsep)]))
        return environment

    def _ensure_started(self) -> None:
        if not self.executable:
            raise AIUnavailableError("未检测到 Codex CLI，请先安装 Codex 后再启用 AI")
        if self._process and self._process.poll() is None:
            return
        with self._start_lock:
            if self._process and self._process.poll() is None:
                return
            self.runtime_directory.mkdir(parents=True, exist_ok=True)
            self._fatal_error = None
            self._responses.clear()
            self._notifications.clear()
            self._stderr_lines.clear()
            try:
                self._process = subprocess.Popen(
                    self._app_server_command(),
                    cwd=self.runtime_directory,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self._subprocess_environment(),
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                )
            except OSError as error:
                raise AIUnavailableError(f"Codex app-server 启动失败：{error}") from error
            self._reader = threading.Thread(target=self._read_messages, name="codex-app-server", daemon=True)
            self._stderr_reader = threading.Thread(
                target=self._read_stderr, name="codex-app-server-stderr", daemon=True
            )
            self._reader.start()
            self._stderr_reader.start()
            self._request(
                "initialize",
                {"clientInfo": {"name": "invest_vault", "title": "Invest Vault", "version": "0.3.24"}},
                ensure_started=False,
            )
            self._send({"method": "initialized", "params": {}})

    def _read_messages(self) -> None:
        process = self._process
        assert process and process.stdout
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                with self._condition:
                    if "id" in message:
                        self._responses[int(message["id"])] = message
                    else:
                        self._notifications.append(message)
                    self._condition.notify_all()
        finally:
            if self._stderr_reader:
                self._stderr_reader.join(timeout=0.2)
            with self._condition:
                if self._fatal_error is None:
                    detail = self._safe_stderr_detail()
                    self._fatal_error = (
                        f"Codex app-server 启动失败：{detail}"
                        if detail
                        else "Codex app-server 已意外退出"
                    )
                self._condition.notify_all()

    def _read_stderr(self) -> None:
        process = self._process
        assert process and process.stderr
        for line in process.stderr:
            cleaned = " ".join(line.strip().split())
            if cleaned:
                with self._condition:
                    self._stderr_lines.append(cleaned[:500])
                    del self._stderr_lines[:-20]

    def _safe_stderr_detail(self) -> str:
        if not self._stderr_lines:
            return ""
        detail = self._stderr_lines[-1]
        lowered = detail.lower()
        if any(secret_word in lowered for secret_word in ("access_token", "refresh_token", "authorization:")):
            return "Codex 返回了包含敏感字段的错误；详细内容已隐藏"
        return detail[:300]

    def _send(self, message: Mapping[str, object]) -> None:
        if not self._process or not self._process.stdin or self._process.poll() is not None:
            if self._reader:
                self._reader.join(timeout=0.3)
            raise AIUnavailableError(self._fatal_error or "Codex app-server 未运行")
        try:
            with self._send_lock:
                self._process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            if self._reader:
                self._reader.join(timeout=0.3)
            raise AIUnavailableError(self._fatal_error or "无法连接 Codex app-server") from error

    def _request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        ensure_started: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if ensure_started:
            self._ensure_started()
        with self._condition:
            request_id = self._next_id
            self._next_id += 1
        self._send({"method": method, "id": request_id, "params": dict(params)})
        deadline = time.monotonic() + (timeout or self.timeout)
        with self._condition:
            while request_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AIUnavailableError(f"Codex 请求超时：{method}")
                if self._fatal_error:
                    raise AIUnavailableError(self._fatal_error)
                self._condition.wait(remaining)
            response = self._responses.pop(request_id)
        if response.get("error"):
            error = response["error"]
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise AIUnavailableError(f"Codex 请求失败：{detail}")
        return dict(response.get("result") or {})

    def status(self) -> dict[str, object]:
        if not self.executable:
            return {"available": False, "authenticated": False, "provider": "codex_app_server", "detail": "未检测到 Codex CLI"}
        try:
            result = self._request("account/read", {"refreshToken": False}, timeout=15)
        except AIUnavailableError as error:
            return {"available": False, "authenticated": False, "provider": "codex_app_server", "detail": str(error)}
        account = result.get("account")
        safe_account = None
        if isinstance(account, dict):
            safe_account = {key: account[key] for key in ("type", "email", "planType") if key in account}
        return {
            "available": True,
            "authenticated": safe_account is not None or not bool(result.get("requiresOpenaiAuth", True)),
            "provider": "codex_app_server",
            "account": safe_account,
            "detail": "Codex 已登录" if safe_account else "Codex 尚未登录",
        }

    def start_chatgpt_login(self) -> dict[str, object]:
        result = self._request(
            "account/login/start",
            {"type": "chatgpt", "appBrand": "codex", "useHostedLoginSuccessPage": True},
            timeout=30,
        )
        return {key: result[key] for key in ("type", "loginId", "authUrl") if key in result}

    def logout(self) -> dict[str, object]:
        return self._request("account/logout", {}, timeout=20)

    def list_models(self) -> list[dict[str, object]]:
        result = self._request("model/list", {"includeHidden": False}, timeout=30)
        return [dict(item) for item in result.get("data") or [] if not item.get("hidden", False)]

    def configure_models(self, settings: dict[str, dict[str, str | None]]) -> None:
        self._model_settings = settings

    def _task_overrides(self, task: str) -> dict[str, str]:
        setting = self._model_settings.get(task) or {}
        return {
            key: str(value)
            for key, value in {
                "model": setting.get("model_id"),
                "effort": setting.get("reasoning_effort"),
            }.items()
            if value
        }

    def _wait_for_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        operation: str,
        timeout: float | None = None,
    ) -> str:
        deadline = time.monotonic() + (timeout or self.timeout)
        chunks: list[str] = []
        completed_text = ""
        cursor = 0
        while True:
            with self._condition:
                while cursor >= len(self._notifications):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AIUnavailableError(f"Codex {operation}生成超时")
                    if self._fatal_error:
                        raise AIUnavailableError(self._fatal_error)
                    self._condition.wait(remaining)
                notifications = self._notifications[cursor:]
                cursor = len(self._notifications)
            for message in notifications:
                method, params = message.get("method"), message.get("params") or {}
                if params.get("threadId") != thread_id:
                    continue
                if method == "item/agentMessage/delta" and params.get("turnId") == turn_id:
                    chunks.append(str(params.get("delta") or ""))
                if method == "item/completed" and params.get("turnId") == turn_id:
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage":
                        completed_text = str(item.get("text") or "")
                if method == "turn/completed" and (params.get("turn") or {}).get("id") == turn_id:
                    turn = params["turn"]
                    if turn.get("status") != "completed":
                        error = turn.get("error") or {}
                        raise AIUnavailableError(f"Codex 生成失败：{error.get('message') or turn.get('status')}")
                    return completed_text or "".join(chunks)

    def quick_note(self, raw_text: str, security_id: str) -> dict[str, object]:
        status = self.status()
        if not status["authenticated"]:
            raise AIUnavailableError("请先使用 ChatGPT 登录 Codex")
        thread = self._request(
            "thread/start",
            {
                "ephemeral": True,
                "cwd": str(self.runtime_directory.resolve()),
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "baseInstructions": (
                    "你是投资研究速记整理器。只整理用户提供的文本，不调用工具、不读取文件、不联网、"
                    "不补充行情或事实、不提供买卖建议。事实仅指用户明确陈述的观察；猜测和感觉必须归入 user_judgements。"
                    "关联证券是应用提供的上下文，不要把证券代码或关联关系单独列为事实。"
                ),
            },
        )
        thread_id = str((thread.get("thread") or {}).get("id") or "")
        if not thread_id:
            raise AIUnavailableError("Codex 未返回 thread id")
        turn = self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": f"关联证券：{security_id}\n用户原始速记：\n{raw_text}"}],
                "outputSchema": QUICK_NOTE_SCHEMA,
                **self._task_overrides("quick_note"),
            },
        )
        turn_id = str((turn.get("turn") or {}).get("id") or "")
        if not turn_id:
            raise AIUnavailableError("Codex 未返回 turn id")
        raw_result = self._wait_for_turn(thread_id, turn_id, operation="速记")
        try:
            result = json.loads(raw_result)
        except json.JSONDecodeError as error:
            raise AIUnavailableError("Codex 返回的速记格式无效") from error
        if not isinstance(result, dict):
            raise AIUnavailableError("Codex 返回的速记格式无效")
        return result

    def _find_runtime_market_skill(self) -> dict[str, str] | None:
        if self._runtime_market_skill is not False:
            return self._runtime_market_skill or None
        bundled = self.market_skill_directory.resolve()
        if (bundled / "SKILL.md").is_file():
            self._runtime_market_skill = {
                "type": "skill", "name": "stock-analysis", "path": str(bundled)
            }
            return self._runtime_market_skill
        try:
            result = self._request(
                "skills/list",
                {"cwds": [str(self.runtime_directory.resolve())], "forceReload": False},
                timeout=20,
            )
            matches = [
                item
                for group in result.get("data") or []
                for item in group.get("skills") or []
                if item.get("name") == "stock-analysis" and item.get("enabled", True)
            ]
            preferred = next(
                (item for item in matches if "backup" not in str(item.get("path") or "").lower()),
                matches[0] if matches else None,
            )
            self._runtime_market_skill = (
                {"type": "skill", "name": "stock-analysis", "path": str(preferred["path"])}
                if preferred and preferred.get("path")
                else None
            )
        except AIUnavailableError:
            self._runtime_market_skill = None
        return self._runtime_market_skill or None

    def chat(
        self, *, role: dict[str, object], messages: list[dict[str, str]], context: str,
        use_runtime_market_skill: bool = False,
    ) -> dict[str, object]:
        status = self.status()
        if not status["authenticated"]:
            raise AIUnavailableError("请先使用 ChatGPT 登录 Codex")
        role_name = str(role["name"])
        report_rules = (
                "你只能生成当前盘前、盘中或盘后市场复盘。投委会报告固定顺序为：执行摘要；"
                "大盘指数概览；持仓分析；六模块深度复盘（M1指数与市场广度、M2板块资金、"
                "M3赚钱效应与上涨主线、M4下跌与流动性风险、M5风格分组、M6抗跌方向）；"
                "综合持仓建议与风险提示。指数和持仓必须使用Markdown表格。每个模块使用加粗的关键判断，"
                "并写判断、证据、风险或确认条件。综合建议必须包含现状总结、基准跑赢/跑输、"
                "条件化仓位动作、下一交易日观察清单、风险提示。单专家报告仍按该专家证据优先级组织，"
                "但不得省略数据日期、指数表、持仓边界、观察清单和风险提示。"
                "持仓必须引用本地账本，区分记录数量、推导数量、成本权重和实时市值估算。"
                "持仓表必须使用证据中的display_name，统一写成证券名称（代码），不得只显示代码。"
                "若所选时段与证据实际时段不一致，先披露错配，不得重标。正文不得出现接口、fallback、"
                "Evidence或Skill等工程表述。结尾原样写：以上内容仅供参考，不构成任何投资建议。"
                "股市有风险，投资需谨慎。"
            if role.get("report_kind") == "market"
            else (
                "你是投委会报告编辑器，必须执行 stock-analysis 4.12.0 Research 机构报告骨架："
                "执行摘要；核心矛盾或基金产品契约；财务或底层持仓；资本配置或业绩风险；"
                "估值与交易实现；六人投委会审议；风险与催化剂；条件化动作。"
                "报告综合已验证的质量、增长、估值和风险事实；覆盖率、内部缺口 ID、快照 ID 与"
                "工程审计结果不得出现在用户正文。不要输出强制买入或卖出结论。"
                if role.get("role_id") == "report_editor"
                else ""
            )
        )
        skill_input = self._find_runtime_market_skill() if use_runtime_market_skill else None
        skill_rules = (
            "本轮以已内置的 stock-analysis 4.12.0 skill 作为证据路由、六人投委会和报告纪律的"
            "主契约；只引用应用提供的结构化事实，不读取其他文件或改写本地账本。"
            if skill_input else "不读取文件。"
        )
        base_instructions = "".join((
            f"你是 Invest Vault 的{role_name}，使用以下分析框架而非模仿或冒充真人。",
            f"关注：{role['focus']}。核心问题：{role['questions']}。风险重点：{role['risk_focus']}。",
            "只使用应用提供的上下文和用户消息，不自行联网。",
            skill_rules,
            report_rules,
            "应用会按 AVAILABLE_SKILLS 调用受控只读工具，SKILL_RUN 和对应 EVIDENCE-SKILL 结果可作为证据；",
            "技能仍报告缺口时不得自行补全。",
            "回答前先读取专家证据覆盖检查：只把 available 当作完整证据，conditional 必须说明口径边界，",
            "missing 必须具体说明缺少哪项；不要用笼统的‘现有证据不足’替代逐项结果。",
            "事实结论必须在结构化 cited_evidence_ids 字段引用证据 ID，但正文绝不显示任何 EVIDENCE、",
            "技能ID或其他工程标识；没有证据的内容明确写成推断，缺失数据放入 unknowns。",
            "正文可用 Markdown 加粗，但不要把 Markdown 符号当普通文字解释。",
            "不虚构数字、来源或专家原话，不给确定性买卖指令。用中文直接回答。",
        ))
        thread = self._request(
            "thread/start",
            {
                "ephemeral": True,
                "cwd": str(self.runtime_directory.resolve()),
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "baseInstructions": base_instructions,
            },
        )
        thread_id = str((thread.get("thread") or {}).get("id") or "")
        if not thread_id:
            raise AIUnavailableError("Codex 未返回 thread id")
        transcript = "\n".join(f"{item['role']}: {item['content']}" for item in messages[-20:])
        inputs: list[dict[str, object]] = []
        if skill_input:
            inputs.append(skill_input)
        inputs.append({"type": "text", "text": f"应用上下文：\n{context}\n\n对话：\n{transcript}"})
        turn = self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": inputs,
                "outputSchema": CHAT_RESPONSE_SCHEMA,
                **self._task_overrides(
                    "committee" if role.get("role_id") == "report_editor" else "research"
                ),
            },
        )
        turn_id = str((turn.get("turn") or {}).get("id") or "")
        if not turn_id:
            raise AIUnavailableError("Codex 未返回 turn id")
        try:
            result = json.loads(
                self._wait_for_turn(
                    thread_id,
                    turn_id,
                    operation="深度研究",
                    timeout=self.chat_timeout,
                )
            )
        except json.JSONDecodeError as error:
            raise AIUnavailableError("Codex 返回的研究回复格式无效") from error
        if not isinstance(result, dict):
            raise AIUnavailableError("Codex 返回的研究回复格式无效")
        return result

    def close(self) -> None:
        process, self._process = self._process, None
        if not process or process.poll() is not None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


class AISettingsStore:
    TASKS = ("quick_note", "research", "committee")
    EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}

    def __init__(self, vault: Vault, provider: AIProvider) -> None:
        self.vault = vault
        self.provider = provider
        self._apply()

    def get(self) -> dict[str, object]:
        row = self.vault.connection.execute(
            "SELECT model_config_json FROM ai_provider_settings WHERE provider_id = 'codex'"
        ).fetchone()
        raw = json.loads(str(row["model_config_json"])) if row else {}
        return {
            "provider": "codex_app_server",
            "tasks": {
                task: {
                    "model_id": (raw.get(task) or {}).get("model_id"),
                    "reasoning_effort": (raw.get(task) or {}).get("reasoning_effort"),
                }
                for task in self.TASKS
            },
        }

    def put(self, task: str, *, model_id: str | None, reasoning_effort: str | None) -> dict[str, object]:
        if task not in self.TASKS:
            raise ValueError("未知的 AI 任务类型")
        if reasoning_effort and reasoning_effort not in self.EFFORTS:
            raise ValueError("不支持的推理强度")
        if model_id is not None and not model_id.strip():
            raise ValueError("模型不能为空")
        settings = self.get()
        tasks = dict(settings["tasks"])
        tasks[task] = {
            "model_id": model_id.strip() if model_id else None,
            "reasoning_effort": reasoning_effort,
        }
        now = datetime.now(timezone.utc).isoformat()
        self.vault.connection.execute(
            "INSERT INTO ai_provider_settings VALUES ('codex', 'codex_app_server', 1, ?, ?, ?) "
            "ON CONFLICT(provider_id) DO UPDATE SET model_config_json = excluded.model_config_json, updated_at = excluded.updated_at",
            (json.dumps(tasks, ensure_ascii=False), now, now),
        )
        self.vault.connection.commit()
        self._apply()
        return dict(tasks[task])

    def _apply(self) -> None:
        configure = getattr(self.provider, "configure_models", None)
        if configure:
            configure(dict(self.get()["tasks"]))


@dataclass(frozen=True)
class QuickNoteDraft:
    draft_id: str
    security_id: str
    raw_text: str
    draft: dict[str, object]
    status: str
    created_at: str
    accepted_note_id: str | None = None


class AIQuickNoteStore:
    def __init__(self, vault: Vault, research: ResearchStore) -> None:
        self.vault = vault
        self.research = research

    def create(self, *, security_id: str, raw_text: str, draft: dict[str, object]) -> QuickNoteDraft:
        created_at = datetime.now(timezone.utc).isoformat()
        item = QuickNoteDraft(str(uuid4()), security_id, raw_text.strip(), draft, "draft", created_at)
        self.vault.connection.execute(
            "INSERT INTO ai_quick_notes VALUES (?, ?, ?, ?, 'draft', NULL, ?, NULL)",
            (item.draft_id, item.security_id, item.raw_text, json.dumps(item.draft, ensure_ascii=False), item.created_at),
        )
        self.vault.connection.commit()
        return item

    def accept(self, draft_id: str, *, body: str) -> str:
        row = self.vault.connection.execute("SELECT * FROM ai_quick_notes WHERE draft_id = ?", (draft_id,)).fetchone()
        if row is None:
            raise ValueError("AI 速记草稿不存在")
        if row["status"] != "draft":
            raise ValueError("AI 速记草稿已处理")
        if not body.strip():
            raise ValueError("确认后的笔记不能为空")
        self.vault.connection.execute("BEGIN IMMEDIATE")
        try:
            note_id = self.research.add_note(
                security_id=str(row["security_id"]), body=body.strip(), commit=False
            )
            accepted_at = datetime.now(timezone.utc).isoformat()
            self.vault.connection.execute(
                "UPDATE ai_quick_notes SET status = 'accepted', accepted_note_id = ?, accepted_at = ? "
                "WHERE draft_id = ?",
                (note_id, accepted_at, draft_id),
            )
            self.vault.connection.commit()
        except BaseException:
            self.vault.connection.rollback()
            raise
        return note_id


class ResearchChatStore:
    """Persisted single-assistant threads; each turn rebuilds bounded local context."""

    def __init__(
        self,
        vault: Vault,
        provider: AIProvider,
        skill_layer: ResearchSkillLayer | None = None,
    ) -> None:
        self.vault = vault
        self.provider = provider
        self.skill_layer = skill_layer or AppResearchSkillLayer(vault)
        self._background_lock = threading.Lock()
        self._background_threads: set[threading.Thread] = set()

    def create(
        self, *, security_id: str, role_id: str, title: str, mode: str = "assistant"
    ) -> dict[str, object]:
        if mode not in {"assistant", "committee"}:
            raise ValueError("未知的聊天模式")
        now, thread_id = datetime.now(timezone.utc).isoformat(), str(uuid4())
        self.vault.connection.execute(
            "INSERT INTO research_threads VALUES (?, ?, ?, ?, NULL, 'codex_app_server', NULL, ?, 'active', ?, ?)",
            (thread_id, mode, title.strip(), security_id, role_id, now, now),
        )
        self.vault.connection.commit()
        return self.get(thread_id, include_events=False)

    def list(self, security_id: str | None = None) -> list[dict[str, object]]:
        query = "SELECT * FROM research_threads WHERE thread_type IN ('assistant', 'committee') AND status = 'active'"
        params: tuple[object, ...] = ()
        if security_id:
            query += " AND security_id = ?"
            params = (security_id,)
        return [dict(row) for row in self.vault.connection.execute(query + " ORDER BY updated_at DESC", params)]

    def archive(self, thread_id: str) -> None:
        if self.vault.connection.execute(
            "SELECT 1 FROM research_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone() is None:
            raise ValueError("研究会话不存在")
        connection = self.vault.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "DELETE FROM research_reports WHERE run_id IN (SELECT run_id FROM research_runs WHERE thread_id = ?)",
                (thread_id,),
            )
            connection.execute(
                "DELETE FROM research_evidence_links WHERE run_id IN (SELECT run_id FROM research_runs WHERE thread_id = ?)",
                (thread_id,),
            )
            connection.execute(
                "DELETE FROM research_tasks WHERE run_id IN (SELECT run_id FROM research_runs WHERE thread_id = ?)",
                (thread_id,),
            )
            connection.execute("DELETE FROM research_events WHERE thread_id = ?", (thread_id,))
            connection.execute("DELETE FROM research_runs WHERE thread_id = ?", (thread_id,))
            connection.execute("DELETE FROM research_threads WHERE thread_id = ?", (thread_id,))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def get(self, thread_id: str, *, include_events: bool = True) -> dict[str, object]:
        row = self.vault.connection.execute("SELECT * FROM research_threads WHERE thread_id = ?", (thread_id,)).fetchone()
        if row is None:
            raise ValueError("研究会话不存在")
        result = dict(row)
        if include_events:
            result["events"] = [
                {**dict(event), "payload": json.loads(str(event["payload_json"]))}
                for event in self.vault.connection.execute(
                    "SELECT * FROM research_events WHERE thread_id = ? ORDER BY sequence_number", (thread_id,)
                )
            ]
            active_run = self.vault.connection.execute(
                "SELECT run_id, status, current_stage, started_at, completed_at "
                "FROM research_runs WHERE thread_id = ? ORDER BY started_at DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            result["active_run"] = dict(active_run) if active_run else None
        return result

    def _append_event(
        self,
        thread_id: str,
        run_id: str,
        actor: str,
        role_id: str,
        payload: dict[str, object],
        *,
        event_type: str = "message.completed",
    ) -> None:
        sequence = int(self.vault.connection.execute(
            "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM research_events WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0])
        self.vault.connection.execute(
            "INSERT INTO research_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid4()), thread_id, run_id, sequence, event_type, actor, role_id, json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )
        self.vault.connection.execute(
            "UPDATE research_threads SET updated_at = ? WHERE thread_id = ?",
            (datetime.now(timezone.utc).isoformat(), thread_id),
        )

    def _context(self, security_id: str, skill_results: list[dict[str, object]] | None = None) -> str:
        lines = [
            f"SECURITY: {security_id}",
            "AVAILABLE_SKILLS: " + json.dumps(self.skill_layer.catalog(), ensure_ascii=False),
            "EVIDENCE:",
        ]
        ordered_results = sorted(
            skill_results or [],
            key=lambda item: 0 if item.get("skill_id") == "framework-readiness" else 1,
        )
        for result in ordered_results:
            lines.append(
                "SKILL_RUN: "
                + json.dumps(
                    {
                        "skill_id": result.get("skill_id"),
                        "name": result.get("name"),
                        "status": result.get("status"),
                        "gaps": result.get("gaps") or [],
                    },
                    ensure_ascii=False,
                )
            )
            for evidence in result.get("evidence") or []:
                rendered = json.dumps(evidence, ensure_ascii=False)
                lines.append(f"{evidence['evidence_id']}: " + rendered[:20_000])
        rows = self.vault.connection.execute(
            """SELECT e.snapshot_id, e.item_index, e.kind, e.value_json, e.unit, e.period_end,
                      e.provider, e.source_ref, s.effective_as_of
               FROM evidence_items e JOIN evidence_snapshots s ON s.snapshot_id = e.snapshot_id
               WHERE s.security_id = ? ORDER BY s.observed_at DESC, e.item_index LIMIT 40""",
            (security_id,),
        ).fetchall()
        for index, row in enumerate(rows, 1):
            lines.append(f"EVIDENCE-{index}: {dict(row)}")
        skill_ids = {str(result.get("skill_id")) for result in ordered_results}
        for table, prefix, covered_by in (
            ("financial_snapshots", "FINANCIAL", "company-financial-quality"),
            ("fund_snapshots", "FUND", "fund-portfolio-evidence"),
        ):
            if covered_by in skill_ids:
                continue
            snapshot = self.vault.connection.execute(
                f"SELECT snapshot_id, cutoff_date, source, payload_json, observed_at FROM {table} "
                "WHERE security_id = ? ORDER BY cutoff_date DESC LIMIT 1",
                (security_id,),
            ).fetchone()
            if snapshot:
                lines.append(
                    f"EVIDENCE-{prefix}-{snapshot['snapshot_id']}: {prefix}_SNAPSHOT "
                    + str(dict(snapshot))[:20_000]
                )
        lines.append("USER_NOTES:")
        for row in self.vault.connection.execute(
            "SELECT body, created_at FROM notes WHERE security_id = ? ORDER BY created_at DESC LIMIT 10", (security_id,)
        ):
            lines.append(f"USER_NOTE: {row['created_at']} {row['body']}")
        lines.append("PUBLIC_MATERIALS:")
        for row in self.vault.connection.execute(
            "SELECT title, published_at, source_name, source_url, excerpt FROM research_materials WHERE security_id = ? ORDER BY published_at DESC LIMIT 10",
            (security_id,),
        ):
            lines.append(f"MATERIAL: {dict(row)}")
        return "\n".join(lines)[:60_000]

    def _context_event(self, security_id: str, context: str) -> dict[str, object]:
        materials = [
            dict(row)
            for row in self.vault.connection.execute(
                "SELECT title, published_at, source_name, source_url FROM research_materials WHERE security_id = ? ORDER BY published_at DESC LIMIT 10",
                (security_id,),
            )
        ]
        evidence_count = sum(1 for line in context.splitlines() if line.startswith("EVIDENCE-"))
        note_count = sum(1 for line in context.splitlines() if line.startswith("USER_NOTE:"))
        return {
            "content": f"已刷新研究上下文：{evidence_count} 条证据、{len(materials)} 条关联资料、{note_count} 条历史笔记。",
            "evidence_count": evidence_count,
            "note_count": note_count,
            "materials": materials,
        }

    def _source_index(
        self, security_id: str, skill_results: list[dict[str, object]]
    ) -> dict[str, list[dict[str, object]]]:
        sources: dict[str, list[dict[str, object]]] = {}
        rows = self.vault.connection.execute(
            """SELECT e.provider, e.source_ref, e.period_end, s.effective_as_of
               FROM evidence_items e JOIN evidence_snapshots s ON s.snapshot_id = e.snapshot_id
               WHERE s.security_id = ? ORDER BY s.observed_at DESC, e.item_index LIMIT 40""",
            (security_id,),
        ).fetchall()
        for index, row in enumerate(rows, 1):
            sources[f"EVIDENCE-{index}"] = [{
                "name": str(row["provider"]), "url": str(row["source_ref"]),
                "as_of": str(row["period_end"] or row["effective_as_of"]),
            }]
        for table, prefix in (("financial_snapshots", "FINANCIAL"), ("fund_snapshots", "FUND")):
            row = self.vault.connection.execute(
                f"SELECT snapshot_id, cutoff_date, source FROM {table} WHERE security_id = ? ORDER BY cutoff_date DESC LIMIT 1",
                (security_id,),
            ).fetchone()
            if row:
                sources[f"EVIDENCE-{prefix}-{row['snapshot_id']}"] = [{
                    "name": str(row["source"]), "url": "", "as_of": str(row["cutoff_date"]),
                }]
        for result in skill_results:
            for evidence in result.get("evidence") or []:
                details = [{
                    "name": str(result.get("name") or evidence.get("provider") or "公开证据"),
                    "url": str(evidence.get("source_ref") or ""),
                    "as_of": str(evidence.get("as_of") or ""),
                }]
                value = evidence.get("value")
                if result.get("skill_id") == "public-topic-evidence" and isinstance(value, dict):
                    for search in value.get("searches") or []:
                        for item in (search.get("items") or [])[:4]:
                            if item.get("url"):
                                details.append(
                                    {
                                        "name": str(item.get("title") or item.get("source") or "公开资讯"),
                                        "url": str(item["url"]),
                                        "as_of": str(item.get("published_at") or "")[:10],
                                    }
                                )
                if result.get("skill_id") == "market-context-evidence" and isinstance(value, dict):
                    for item, fallback_name in (
                        (value.get("security_price_volume"), "标的前复权日线"),
                        (value.get("official_fund_nav_performance"), "基金官方累计净值"),
                        (value.get("related_sector"), "证券所属板块"),
                    ):
                        if isinstance(item, dict) and item.get("source_ref"):
                            details.append({
                                "name": str(item.get("source") or fallback_name),
                                "url": str(item["source_ref"]),
                                "as_of": str(item.get("as_of") or value.get("market_date") or ""),
                            })
                    sector = value.get("related_sector")
                    sector_history = sector.get("price_volume") if isinstance(sector, dict) else None
                    if isinstance(sector_history, dict) and sector_history.get("source_ref"):
                        details.append({
                            "name": str(sector_history.get("source") or "相关板块历史日线"),
                            "url": str(sector_history["source_ref"]),
                            "as_of": str(sector_history.get("as_of") or value.get("market_date") or ""),
                        })
                    breadth = value.get("market_breadth")
                    if isinstance(breadth, dict) and breadth.get("source_ref"):
                        details.append({
                            "name": str(breadth.get("source") or "A股全市场涨跌家数"),
                            "url": str(breadth["source_ref"]),
                            "as_of": str(breadth.get("trade_date") or value.get("market_date") or ""),
                        })
                    histories = value.get("continuous_price_volume")
                    if isinstance(histories, dict):
                        for history in histories.values():
                            if isinstance(history, dict) and history.get("source_ref"):
                                details.append({
                                    "name": str(history.get("source") or "主要指数连续量价"),
                                    "url": str(history["source_ref"]),
                                    "as_of": str(history.get("as_of") or value.get("market_date") or ""),
                                })
                sources[str(evidence["evidence_id"])] = details[:13]
        return sources

    def _send_assistant(self, *, thread_id: str, content: str, role: dict[str, object]) -> dict[str, object]:
        run_id, now = str(uuid4()), datetime.now(timezone.utc).isoformat()
        with self.vault.lock:
            thread = self.get(thread_id)
            self.vault.connection.execute(
                "INSERT INTO research_runs VALUES (?, ?, 'assistant-v1', 'running', 'analysis', ?, NULL, ?, NULL, NULL)",
                (run_id, thread_id, json.dumps({"content": content}, ensure_ascii=False), now),
            )
            self._append_event(thread_id, run_id, "user", "user", {"content": content})
            self.vault.connection.commit()
        try:
            market_scene = str(thread["security_id"]) == MARKET_OVERVIEW_SECURITY_ID
            accepted_question = is_investment_question(content) and (
                not market_scene or is_market_report_question(content)
            )
            skill_results = (
                self.skill_layer.run(
                    security_id=str(thread["security_id"]),
                    question=content,
                    role_id=str(role["role_id"]),
                )
                if accepted_question
                else []
            )
        except Exception as error:
            skill_results = [
                {
                    "skill_id": "research-evidence-router",
                    "name": "研究证据路由",
                    "description": "按问题调用受控只读证据工具。",
                    "status": "failed",
                    "gaps": [str(error)],
                    "evidence": [],
                }
            ]
        with self.vault.lock:
            for result in skill_results:
                tool_payload = {
                    "content": f"调用技能：{result['name']}",
                    "skill_id": result["skill_id"],
                    "skill_name": result["name"],
                }
                self._append_event(
                    thread_id,
                    run_id,
                    "system",
                    str(result["skill_id"]),
                    tool_payload,
                    event_type="tool.started",
                )
                self._append_event(
                    thread_id,
                    run_id,
                    "system",
                    str(result["skill_id"]),
                    {
                        **tool_payload,
                        "content": f"技能完成：{result['name']}（{result['status']}）",
                        "status": result["status"],
                        "gaps": result.get("gaps") or [],
                        "evidence_ids": [item["evidence_id"] for item in result.get("evidence") or []],
                        "sources": [
                            {
                                "name": str(result.get("name") or item.get("provider") or "公开资料"),
                                "url": str(item.get("source_ref") or ""),
                                "as_of": str(item.get("as_of") or ""),
                            }
                            for item in result.get("evidence") or []
                        ],
                        "evidence": result.get("evidence") or [],
                    },
                    event_type="tool.completed",
                )
            context = self._context(str(thread["security_id"]), skill_results)
            self._append_event(
                thread_id,
                run_id,
                "system",
                "context",
                self._context_event(str(thread["security_id"]), context),
                event_type="context.completed",
            )
            self.vault.connection.commit()
        # ponytail: each research turn is independent. The persisted timeline is for the user,
        # not implicit model memory; add explicit user-selected context later if truly needed.
        history = [{"role": "user", "content": content}]
        try:
            if market_scene and not is_market_report_question(content):
                reply = {
                    "content": "市场概览助手仅生成盘前、盘中或盘后大盘行情报告，并可结合本地持仓给出下一步观察建议。个股、基金或其他投资问题请移步证券资料助手。",
                    "cited_evidence_ids": [],
                    "assumptions": [],
                    "unknowns": [],
                    "refused": True,
                }
            elif not is_investment_question(content):
                reply = {
                    "content": "研究助手只回答金融、理财和投资相关问题。请围绕当前标的的经营、财务、估值、市场、组合或风险继续提问。",
                    "cited_evidence_ids": [],
                    "assumptions": [],
                    "unknowns": [],
                    "refused": True,
                }
            else:
                reply = self.provider.chat(
                    role=market_report_role(role) if market_scene else role,
                    messages=history,
                    context=context[:45_000] if is_deep_research_request(content) else context,
                    use_runtime_market_skill=True,
                )
            valid_evidence_ids = {
                line.split(":", 1)[0] for line in context.splitlines() if line.startswith("EVIDENCE-")
            }
            reply["cited_evidence_ids"] = [
                evidence_id
                for evidence_id in reply.get("cited_evidence_ids", [])
                if evidence_id in valid_evidence_ids
            ]
            source_index = self._source_index(str(thread["security_id"]), skill_results)
            reply["sources"] = [source
                for evidence_id in reply["cited_evidence_ids"]
                if evidence_id in source_index
                for source in source_index[evidence_id]
            ]
            reply["content"] = re.sub(
                r"[（(]?EVIDENCE(?:-(?:SKILL|FINANCIAL|FUND))?-[A-Za-z0-9_-]+[）)]?",
                "",
                str(reply.get("content") or ""),
            ).strip()
            reply_role = market_report_role(role) if market_scene and not reply.get("refused") else role
            reply["role_id"] = reply_role["role_id"]
            reply["role_name"] = reply_role["name"]
            with self.vault.lock:
                self._append_event(thread_id, run_id, "assistant", str(role["role_id"]), reply)
                self.vault.connection.execute(
                    "UPDATE research_runs SET status = 'completed', current_stage = 'completed', completed_at = ? WHERE run_id = ?",
                    (datetime.now(timezone.utc).isoformat(), run_id),
                )
                self.vault.connection.execute(
                    "UPDATE research_threads SET role_id = ?, updated_at = ? WHERE thread_id = ?",
                    (role["role_id"], datetime.now(timezone.utc).isoformat(), thread_id),
                )
                self.vault.connection.commit()
            return reply
        except BaseException as error:
            with self.vault.lock:
                self.vault.connection.execute(
                    "UPDATE research_runs SET status = 'failed', failure_json = ? WHERE run_id = ?",
                    (json.dumps({"message": str(error)}, ensure_ascii=False), run_id),
                )
                self.vault.connection.commit()
            raise

    def _committee_skill_results(
        self, *, security_id: str, question: str, role_ids: list[str]
    ) -> list[dict[str, object]]:
        by_skill: dict[str, dict[str, object]] = {}
        readiness: list[dict[str, object]] = []
        for role_id in role_ids:
            for result in self.skill_layer.run(
                security_id=security_id, question=question, role_id=role_id
            ):
                copied = dict(result)
                if copied.get("skill_id") == "framework-readiness":
                    copied["skill_id"] = f"framework-readiness-{role_id}"
                    copied["name"] = f"{get_role(role_id)['name']}证据覆盖检查"
                    readiness.append(copied)
                else:
                    by_skill.setdefault(str(copied["skill_id"]), copied)
        return [*readiness, *by_skill.values()]

    @staticmethod
    def _clean_reply(reply: dict[str, object]) -> dict[str, object]:
        reply["content"] = re.sub(
            r"[（(]?EVIDENCE(?:-(?:SKILL|FINANCIAL|FUND))?-[A-Za-z0-9_-]+[）)]?",
            "",
            str(reply.get("content") or ""),
        ).strip()
        return reply

    def _send_committee(self, *, thread_id: str, content: str) -> dict[str, object]:
        run_id, now = str(uuid4()), datetime.now(timezone.utc).isoformat()
        with self.vault.lock:
            thread = self.get(thread_id)
            deep_request = (
                str(thread["security_id"]) == MARKET_OVERVIEW_SECURITY_ID
                or is_deep_research_request(content)
            )
            running = self.vault.connection.execute(
                "SELECT 1 FROM research_runs WHERE thread_id = ? AND status = 'running' LIMIT 1",
                (thread_id,),
            ).fetchone()
            if running:
                raise ValueError("当前投委会仍在研究中，请等待本轮完成")
            plan = committee_plan(str(thread["security_id"]), content)
            self.vault.connection.execute(
                "INSERT INTO research_runs VALUES (?, ?, 'stock-analysis-4.12.0-committee-v1', 'running', 'planning', ?, ?, ?, NULL, NULL)",
                (run_id, thread_id, json.dumps({"content": content}, ensure_ascii=False), json.dumps(plan, ensure_ascii=False), now),
            )
            self._append_event(thread_id, run_id, "user", "user", {"content": content})
            self._append_event(
                thread_id,
                run_id,
                "system",
                "coordinator",
                {"content": "协调员正在拆解问题并选择研究成员。", "role_name": "协调员"},
                event_type="planning.started",
            )
            self._append_event(
                thread_id, run_id, "system", "coordinator",
                ({
                    "content": "协调员已理解研究问题并制定任务计划。",
                    "role_name": "协调员",
                    "selected_roles": [get_role(role_id)["name"] for role_id in plan["roles"]],
                    "assignments": [
                        {"name": get_role(item["role_id"])["name"], "function": item["function"]}
                        for item in plan["assignments"]
                    ],
                    "reason": plan["reason"],
                    "stages": plan["stages"],
                } if deep_request else {
                    "content": "协调员已完成问题分流。",
                    "role_name": "协调员",
                }),
                event_type="plan.completed" if deep_request else "routing.completed",
            )
            self.vault.connection.commit()

        if not deep_request:
            reply = {
                "content": "这个问题更适合普通助手快速回答。投委会模式用于个股、基金或行情复盘的深度报告；请切换到普通助手，或补充研究范围、关注风险和希望复盘的时间区间。",
                "cited_evidence_ids": [], "assumptions": [], "unknowns": [],
                "role_id": "coordinator", "role_name": "协调员", "refused": True,
                "suggested_mode": "assistant",
            }
            with self.vault.lock:
                self._append_event(thread_id, run_id, "assistant", "coordinator", reply)
                self.vault.connection.execute(
                    "UPDATE research_runs SET status = 'completed', current_stage = 'completed', completed_at = ? WHERE run_id = ?",
                    (datetime.now(timezone.utc).isoformat(), run_id),
                )
                self.vault.connection.commit()
            return reply

        worker = threading.Thread(
            target=self._run_committee_background,
            args=(run_id, thread_id, content, thread, plan),
            name=f"invest-committee-{run_id[:8]}",
            daemon=True,
        )
        with self._background_lock:
            self._background_threads.add(worker)
        worker.start()
        return {"run_id": run_id, "status": "running", "current_stage": "planning"}

    def _run_committee_background(
        self,
        run_id: str,
        thread_id: str,
        content: str,
        thread: dict[str, object],
        plan: dict[str, object],
    ) -> None:
        try:
            self._continue_committee(
                run_id=run_id,
                thread_id=thread_id,
                content=content,
                thread=thread,
                plan=plan,
            )
        except BaseException as error:
            with self.vault.lock:
                failed_at = datetime.now(timezone.utc).isoformat()
                self.vault.connection.execute(
                    "UPDATE research_runs SET status = 'failed', completed_at = ?, failure_json = ? WHERE run_id = ?",
                    (failed_at, json.dumps({"message": str(error)}, ensure_ascii=False), run_id),
                )
                self._append_event(
                    thread_id,
                    run_id,
                    "system",
                    "coordinator",
                    {
                        "content": "本轮研究未能完成。已保留已取得的证据和专家意见，可以稍后重新发起。",
                        "role_name": "协调员",
                        "gaps": ["本轮研究未完成"],
                    },
                    event_type="workflow.failed",
                )
                self.vault.connection.commit()
        finally:
            current = threading.current_thread()
            with self._background_lock:
                self._background_threads.discard(current)

    def _continue_committee(
        self,
        *,
        run_id: str,
        thread_id: str,
        content: str,
        thread: dict[str, object],
        plan: dict[str, object],
    ) -> dict[str, object]:
        with self.vault.lock:
            self.vault.connection.execute(
                "UPDATE research_runs SET current_stage = 'evidence' WHERE run_id = ?", (run_id,)
            )
            self._append_event(
                thread_id,
                run_id,
                "system",
                "evidence_collector",
                {"content": "正在按各专家框架收集并核对研究证据。", "role_name": "证据研究员"},
                event_type="evidence.started",
            )
            self.vault.connection.commit()

        role_ids = list(plan["roles"])
        try:
            skill_results = self._committee_skill_results(
                security_id=str(thread["security_id"]), question=content, role_ids=role_ids
            )
        except Exception as error:
            skill_results = [{
                "skill_id": "research-evidence-router", "name": "研究证据收集",
                "status": "failed", "gaps": [str(error)], "evidence": [],
            }]
        with self.vault.lock:
            self.vault.connection.execute(
                "UPDATE research_runs SET current_stage = 'evidence' WHERE run_id = ?", (run_id,)
            )
            for result in skill_results:
                self._append_event(
                    thread_id, run_id, "system", str(result["skill_id"]),
                    {
                        "content": f"已补充{result['name']}（{result['status']}）。",
                        "skill_name": result["name"], "status": result["status"],
                        "gaps": result.get("gaps") or [],
                        "evidence_ids": [item["evidence_id"] for item in result.get("evidence") or []],
                    },
                    event_type="tool.completed",
                )
            self.vault.connection.commit()

        context = self._context(str(thread["security_id"]), skill_results)
        source_index = self._source_index(str(thread["security_id"]), skill_results)
        valid_ids = {line.split(":", 1)[0] for line in context.splitlines() if line.startswith("EVIDENCE-")}
        opinions: list[dict[str, object]] = []
        with self.vault.lock:
            self.vault.connection.execute(
                "UPDATE research_runs SET current_stage = 'analysis' WHERE run_id = ?", (run_id,)
            )
            self._append_event(
                thread_id,
                run_id,
                "system",
                "coordinator",
                {
                    "content": "证据包已就绪，研究小组开始并行分析。",
                    "role_name": "协调员",
                    "selected_roles": [get_role(role_id)["name"] for role_id in role_ids],
                },
                event_type="analysis.started",
            )
            for role_id in role_ids:
                task_id = str(uuid4())
                self.vault.connection.execute(
                    "INSERT INTO research_tasks (task_id, run_id, parent_task_id, assigned_role, task_type, input_json, output_json, status, attempt, started_at, completed_at) VALUES (?, ?, NULL, ?, 'expert_analysis', ?, NULL, 'running', 1, ?, NULL)",
                    (task_id, run_id, role_id, json.dumps({"question": content}, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
                )
                self._append_event(
                    thread_id,
                    run_id,
                    "system",
                    role_id,
                    {
                        "content": f"{get_role(role_id)['name']}正在分析。",
                        "role_name": get_role(role_id)["name"],
                    },
                    event_type="expert.started",
                )
            self.vault.connection.commit()

        def run_expert(role_id: str) -> tuple[str, dict[str, object] | None, BaseException | None, int]:
            role = get_role(role_id)
            for attempt in (1, 2):
                try:
                    opinion = self.provider.chat(
                        role=role,
                        messages=[{"role": "user", "content": f"作为投委会研究员，围绕以下深度问题提交独立意见：{content}"}],
                        context=context[:45_000],
                        use_runtime_market_skill=True,
                    )
                    opinion = self._clean_reply(opinion)
                    opinion["cited_evidence_ids"] = [item for item in opinion.get("cited_evidence_ids", []) if item in valid_ids]
                    opinion.update({"role_id": role_id, "role_name": role["name"]})
                    return role_id, opinion, None, attempt
                except BaseException as error:
                    retryable = isinstance(error, AIUnavailableError) and any(
                        marker in str(error)
                        for marker in ("格式无效", "threads can only be started once", "Server overloaded")
                    )
                    if attempt == 1 and retryable:
                        continue
                    return role_id, None, error, attempt
            raise AssertionError("unreachable")

        # ponytail: stock-analysis bounds a committee at six independent members;
        # a standard-library pool is enough until workflows need distributed workers.
        with ThreadPoolExecutor(max_workers=len(role_ids), thread_name_prefix="invest-committee") as pool:
            futures = [pool.submit(run_expert, role_id) for role_id in role_ids]
            expert_results = [future.result() for future in as_completed(futures)]

        ordered_results = sorted(expert_results, key=lambda item: role_ids.index(item[0]))
        for role_id, opinion, error, attempts in ordered_results:
            role = get_role(role_id)
            task = self.vault.connection.execute(
                "SELECT task_id FROM research_tasks WHERE run_id = ? AND assigned_role = ?",
                (run_id, role_id),
            ).fetchone()
            if opinion is not None:
                opinions.append(opinion)
                with self.vault.lock:
                    self._append_event(thread_id, run_id, "assistant", role_id, opinion, event_type="expert.completed")
                    self.vault.connection.execute(
                        "UPDATE research_tasks SET output_json = ?, status = 'completed', attempt = ?, completed_at = ? WHERE task_id = ?",
                        (json.dumps(opinion, ensure_ascii=False), attempts, datetime.now(timezone.utc).isoformat(), task["task_id"]),
                    )
                    self.vault.connection.commit()
            else:
                assert error is not None
                with self.vault.lock:
                    self._append_event(
                        thread_id, run_id, "system", role_id,
                        {
                            "content": f"{role['name']}本轮未完成，协调员将使用其余意见继续。",
                            "role_name": role["name"],
                            "gaps": ["本轮生成未完成，可稍后重试该专家"],
                        },
                        event_type="expert.failed",
                    )
                    self.vault.connection.execute(
                        "UPDATE research_tasks SET output_json = ?, status = 'failed', attempt = ?, completed_at = ? WHERE task_id = ?",
                        (json.dumps({"message": str(error)}, ensure_ascii=False), attempts, datetime.now(timezone.utc).isoformat(), task["task_id"]),
                    )
                    self.vault.connection.commit()

        opinion_text = json.dumps(opinions, ensure_ascii=False)[:24_000]
        report_role = {
            "role_id": "report_editor", "name": "投委会报告编辑器",
            "focus": "证据边界、专家共识、关键分歧、组合风险和可复核条件",
            "questions": "哪些逻辑仍成立、哪些已削弱、哪些缺口会改变判断？",
            "risk_focus": "证据错配、虚假共识、数据缺口和无条件行动建议",
            "report_contract": "stock-analysis 4.12.0；市场使用执行摘要、指数、持仓、M1-M6、建议风险骨架；公司或基金使用 Research 机构报告骨架",
        }
        if str(thread["security_id"]) == MARKET_OVERVIEW_SECURITY_ID:
            report_role["report_kind"] = "market"
        report_context = (context[:28_000] + "\nEXPERT_OPINIONS:\n" + opinion_text)[:52_000]
        try:
            with self.vault.lock:
                self.vault.connection.execute(
                    "UPDATE research_runs SET current_stage = 'reporting' WHERE run_id = ?", (run_id,)
                )
                self._append_event(
                    thread_id,
                    run_id,
                    "system",
                    "report_editor",
                    {"content": "专家意见已汇总，正在生成最终深度报告。", "role_name": "报告编辑器"},
                    event_type="reporting.started",
                )
                self.vault.connection.commit()
            report = self.provider.chat(
                role=report_role,
                messages=[{"role": "user", "content": f"根据协调员计划和专家意见形成最终深度报告：{content}"}],
                context=report_context,
                use_runtime_market_skill=True,
            )
            report = self._clean_reply(report)
            report["cited_evidence_ids"] = [item for item in report.get("cited_evidence_ids", []) if item in valid_ids]
            report["sources"] = [source for evidence_id in report["cited_evidence_ids"] for source in source_index.get(evidence_id, [])]
            report.update({"role_id": "report_editor", "role_name": "投委会报告", "report": True})
            unresolved = list(dict.fromkeys(str(item) for opinion in opinions for item in opinion.get("unknowns", [])))[:8]
            completed_at = datetime.now(timezone.utc).isoformat()
            with self.vault.lock:
                self.vault.connection.execute(
                    "UPDATE research_runs SET current_stage = 'conflicts' WHERE run_id = ?", (run_id,)
                )
                self._append_event(
                    thread_id, run_id, "system", "coordinator",
                    {"content": "协调员已完成共识与分歧整理。", "role_name": "协调员", "gaps": unresolved},
                    event_type="conflicts.completed",
                )
                self.vault.connection.execute(
                    "UPDATE research_runs SET current_stage = 'risk_review' WHERE run_id = ?", (run_id,)
                )
                self._append_event(
                    thread_id, run_id, "system", "risk_manager",
                    {"content": "已完成风险与组合影响审查；未取得的数据继续保留为条件项。", "role_name": "风险与组合经理"},
                    event_type="risk_review.completed",
                )
                self._append_event(thread_id, run_id, "assistant", "report_editor", report, event_type="report.completed")
                self.vault.connection.execute(
                    "INSERT INTO research_reports VALUES (?, ?, 1, ?, ?, NULL, ?)",
                    (str(uuid4()), run_id, json.dumps(report, ensure_ascii=False), str(report["content"]), completed_at),
                )
                self.vault.connection.execute(
                    "UPDATE research_runs SET status = 'completed', current_stage = 'completed', completed_at = ? WHERE run_id = ?",
                    (completed_at, run_id),
                )
                self.vault.connection.execute(
                    "UPDATE research_threads SET role_id = 'coordinator', updated_at = ? WHERE thread_id = ?",
                    (completed_at, thread_id),
                )
                self.vault.connection.commit()
            return report
        except BaseException as error:
            with self.vault.lock:
                self.vault.connection.execute(
                    "UPDATE research_runs SET status = 'failed', current_stage = 'reporting', failure_json = ? WHERE run_id = ?",
                    (json.dumps({"message": str(error)}, ensure_ascii=False), run_id),
                )
                self.vault.connection.commit()
            raise

    def send(self, *, thread_id: str, content: str, role: dict[str, object]) -> dict[str, object]:
        thread = self.get(thread_id, include_events=False)
        if thread["thread_type"] == "committee":
            return self._send_committee(thread_id=thread_id, content=content)
        return self._send_assistant(thread_id=thread_id, content=content, role=role)

    def close(self, timeout: float = 2.0) -> None:
        with self._background_lock:
            threads = list(self._background_threads)
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
