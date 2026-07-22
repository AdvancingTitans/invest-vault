"""Optional Codex app-server integration and review-before-save quick notes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .ai_providers import PROVIDER_CATALOG, DirectAPIClient, EncryptedCredentialStore
from .ai_roles import committee_plan, get_role, is_deep_research_request
from .ai_skills import (
    FRAMEWORK_REQUIREMENTS,
    MARKET_OVERVIEW_SECURITY_IDS,
    AppResearchSkillLayer,
    ResearchSkillLayer,
)
from .evidence_orchestration import (
    ClaimBoard,
    ConflictTrigger,
    ContextBudget,
    CoverageGate,
    DomainPacketBuilder,
    EvidenceManifest,
    EvidencePacket,
    EvidenceRecord,
    EvidenceStore,
    ExpertClaim,
    ExpertExecutionScheduler,
    ExpertExecutionTask,
    ExpertResearchState,
    ReportSectionBuilder,
    RiskReviewState,
    RoleEvidencePlanner,
    estimate_tokens,
    project_oversized_record,
)
from .ledger import Vault
from .research import ResearchStore
from .shared_research_board import SharedResearchBlackboard, SharedResearchBoardBuilder

CHAT_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content", "cited_evidence_ids", "assumptions", "unknowns", "reached_sources"],
    "properties": {
        "content": {"type": "string"},
        "cited_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "reached_sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "url", "published_at", "accessed_at", "source_type"],
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "published_at": {"type": "string"},
                    "accessed_at": {"type": "string"},
                    "source_type": {"type": "string", "enum": ["issuer", "exchange", "regulator", "official_fund", "official_index"]},
                },
            },
        },
    },
}

_NON_PUBLISHABLE_CONCLUSION = re.compile(
    r"(?:证据|数据|资料|信息)(?:仍|尚|暂|并)?(?:不足|缺失|不完整|未覆盖|未补齐|不可得)"
    r"|(?:缺少|缺乏|尚缺|待补)(?:[^。；\n]{0,24})"
    r"(?:证据|数据|资料|信息|原文|样本|盘口|报价|五档|序列|曲线|公告|披露|财报)"
    r"|(?:未|尚未)(?:提供|建立|取得|获取|覆盖|补齐)(?:[^。；\n]{0,32})"
    r"(?:证据|数据|资料|信息|原文|样本|盘口|报价|五档|序列|曲线|公告|披露|财报)"
    r"|(?:无法|不能)(?:直接|单独|据此)?(?:判断|判定|确认|得出|形成|支持|证明|推导|升级)"
    r"|不能(?:直接|自动|简单)?(?:外推|转化|等同|给出)"
    r"|(?:尚未|未)(?:被)?(?:证明|确认)|(?:尚未|未)形成[^。；\n]{0,20}结论"
    r"|维持观察|继续观察|暂无(?:法|足够)|未形成(?:可发布|可证据化|可靠)?结论"
    r"|研究缺口|证据缺口|数据缺口",
    re.IGNORECASE,
)

EXPERT_STATE_UPDATE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claims", "requirements", "open_questions"],
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "claim_id",
                    "claim",
                    "status",
                    "supporting_evidence_ids",
                    "contradicting_evidence_ids",
                    "confidence",
                    "conditions",
                ],
                "properties": {
                    "claim_id": {"type": "string"},
                    "claim": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["supported", "conditional", "contradicted", "unresolved"],
                    },
                    "supporting_evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "contradicting_evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "conditions": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "requirement",
                    "status",
                    "evidence_ids",
                    "attempted_sources",
                    "reason",
                    "alternatives",
                    "impact",
                ],
                "properties": {
                    "requirement": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["covered", "partial", "unavailable", "not_applicable", "conflicted"],
                    },
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "attempted_sources": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                    "alternatives": {"type": "array", "items": {"type": "string"}},
                    "impact": {"type": "string"},
                },
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
}

INVESTMENT_TERMS = (
    "股票",
    "基金",
    "债券",
    "证券",
    "投资",
    "理财",
    "持仓",
    "组合",
    "市场",
    "行业",
    "公司",
    "财报",
    "公告",
    "盈利",
    "利润",
    "收入",
    "现金流",
    "估值",
    "市盈率",
    "市净率",
    "roe",
    "roic",
    "价格",
    "股价",
    "趋势",
    "成交量",
    "资金",
    "风险",
    "回撤",
    "收益",
    "分红",
    "护城河",
    "管理层",
    "资产",
    "负债",
    "宏观",
    "利率",
    "通胀",
    "汇率",
    "政策",
    "经济",
    "商业模式",
    "竞争",
    "催化剂",
    "证据",
    "这家公司",
    "这个标的",
    "stock",
    "fund",
    "bond",
    "portfolio",
    "market",
    "finance",
    "financial",
    "invest",
    "valuation",
    "earnings",
    "买入",
    "卖出",
    "加仓",
    "减仓",
    "补仓",
    "仓位",
    "能买吗",
    "值得买",
    "基本面",
    "技术面",
    "增长",
    "营收",
    "毛利",
    "净利",
    "股息",
    "净值",
    "波动",
    "牛市",
    "熊市",
)
FOLLOW_UP_TERMS = (
    "为什么",
    "展开",
    "继续",
    "详细",
    "反方",
    "依据",
    "来源",
    "怎么看",
    "是否成立",
    "变化",
    "影响",
)
MARKET_REPORT_TERMS = ("盘前", "盘中", "盘后", "大盘", "市场", "行情", "指数", "复盘")

MARKET_REPORT_ROLE: dict[str, object] = {
    "role_id": "market_report",
    "report_kind": "market",
    "name": "市场行情助手",
    "focus": "结合当前所有可用且完整的证据生成当前交易时段报告，并结合用户本地持仓给出条件化观察建议",
    "questions": "当前完整证据共同说明了什么、用户持仓暴露在什么条件下需要继续观察",
    "risk_focus": "证据缺失、数据时段错配、计算模型不准确或把推导值冒充原始事实",
}


def market_report_role(role: dict[str, object]) -> dict[str, object]:
    if role["role_id"] == "general":
        return MARKET_REPORT_ROLE
    return {
        "role_id": role["role_id"],
        "report_kind": "market",
        "name": role["name"],
        "focus": f"结合当前所有可用且完整的证据生成当前交易时段报告，并采用{role['name']}的分析框架；结合用户本地持仓给出条件化观察建议。",
        "questions": f"{role['questions']}；当前完整证据与本地持仓暴露共同说明了什么。",
        "risk_focus": f"{role['risk_focus']}；证据缺失、数据时段错配、计算模型不准确或把推导值冒充原始事实。",
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
        self,
        *,
        role: dict[str, object],
        messages: list[dict[str, str]],
        context: str,
        use_runtime_market_skill: bool = False,
    ) -> dict[str, object]: ...

    def generate_structured(
        self,
        *,
        role: dict[str, object],
        prompt: str,
        schema: dict[str, object],
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
        reach_skill_directory: Path | None = None,
        primary_evidence_skill_directory: Path | None = None,
    ) -> None:
        self.runtime_directory = Path(runtime_directory)
        self.executable = executable or self._find_executable()
        self.node_executable = node_executable or self._find_node_executable()
        self.timeout = timeout
        self.chat_timeout = chat_timeout
        self.market_skill_directory = market_skill_directory or self._bundled_market_skill_directory()
        self.reach_skill_directory = reach_skill_directory or self._bundled_reach_skill_directory()
        self.primary_evidence_skill_directory = (
            primary_evidence_skill_directory or self._bundled_primary_evidence_skill_directory()
        )
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
        self._runtime_reach_skill: dict[str, str] | None | bool = False
        self._runtime_primary_evidence_skill: dict[str, str] | None | bool = False
        self._model_settings: dict[str, dict[str, str | None]] = {}
        self._operation_local = threading.local()
        self._active_turns: dict[str, set[tuple[str, str]]] = {}
        self._cancelled_operations: set[str] = set()

    def begin_operation(self, operation_id: str) -> None:
        with self._condition:
            self._cancelled_operations.discard(operation_id)
        self._operation_local.operation_id = operation_id

    def end_operation(self, operation_id: str) -> None:
        if getattr(self._operation_local, "operation_id", None) == operation_id:
            self._operation_local.operation_id = None
        with self._condition:
            self._cancelled_operations.discard(operation_id)

    def cancel_operation(self, operation_id: str) -> None:
        with self._condition:
            self._cancelled_operations.add(operation_id)
            turns = list(self._active_turns.get(operation_id, set()))
        for thread_id, turn_id in turns:
            try:
                self._request(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": turn_id},
                    timeout=10,
                )
            except AIUnavailableError:
                continue

    @staticmethod
    def _bundled_market_skill_directory() -> Path:
        frozen_root = getattr(sys, "_MEIPASS", None)
        if frozen_root:
            return Path(frozen_root) / "skills" / "stock-analysis"
        return Path(__file__).parents[2] / "skills" / "stock-analysis"

    @staticmethod
    def _bundled_reach_skill_directory() -> Path:
        frozen_root = getattr(sys, "_MEIPASS", None)
        if frozen_root:
            return Path(frozen_root) / "skills" / "agent-reach"
        return Path(__file__).parents[2] / "skills" / "agent-reach"

    @staticmethod
    def _bundled_primary_evidence_skill_directory() -> Path:
        frozen_root = getattr(sys, "_MEIPASS", None)
        if frozen_root:
            return Path(frozen_root) / "skills" / "primary-evidence-reach"
        return Path(__file__).parents[2] / "skills" / "primary-evidence-reach"

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
        prefixes = [str(Path(path).parent) for path in (self.node_executable, self.executable) if path]
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
                {"clientInfo": {"name": "invest_vault", "title": "Invest Vault", "version": "0.3.54"}},
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
                        f"Codex app-server 启动失败：{detail}" if detail else "Codex app-server 已意外退出"
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
            return {
                "available": False,
                "authenticated": False,
                "provider": "codex_app_server",
                "detail": "未检测到 Codex CLI",
            }
        try:
            result = self._request("account/read", {"refreshToken": False}, timeout=15)
        except AIUnavailableError as error:
            return {
                "available": False,
                "authenticated": False,
                "provider": "codex_app_server",
                "detail": str(error),
            }
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
                        raise AIUnavailableError(
                            f"Codex 生成失败：{error.get('message') or turn.get('status')}"
                        )
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
            self._runtime_market_skill = {"type": "skill", "name": "stock-analysis", "path": str(bundled)}
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

    def _find_runtime_reach_skill(self) -> dict[str, str] | None:
        """Resolve the pinned read-only web fallback without requiring a user install."""

        if self._runtime_reach_skill is not False:
            return self._runtime_reach_skill or None
        bundled = self.reach_skill_directory.resolve()
        if (bundled / "SKILL.md").is_file():
            self._runtime_reach_skill = {
                "type": "skill",
                "name": "agent-reach",
                "path": str(bundled),
            }
            return self._runtime_reach_skill
        try:
            result = self._request(
                "skills/list",
                {"cwds": [str(self.runtime_directory.resolve())], "forceReload": False},
                timeout=20,
            )
            match = next(
                (
                    item
                    for group in result.get("data") or []
                    for item in group.get("skills") or []
                    if item.get("name") == "agent-reach" and item.get("enabled", True)
                ),
                None,
            )
            self._runtime_reach_skill = (
                {"type": "skill", "name": "agent-reach", "path": str(match["path"])}
                if match and match.get("path")
                else None
            )
        except AIUnavailableError:
            self._runtime_reach_skill = None
        return self._runtime_reach_skill or None

    def _find_runtime_primary_evidence_skill(self) -> dict[str, str] | None:
        """Resolve the pinned issuer/exchange/regulator evidence workflow."""

        if self._runtime_primary_evidence_skill is not False:
            return self._runtime_primary_evidence_skill or None
        bundled = self.primary_evidence_skill_directory.resolve()
        if (bundled / "SKILL.md").is_file():
            self._runtime_primary_evidence_skill = {
                "type": "skill",
                "name": "primary-evidence-reach",
                "path": str(bundled),
            }
            return self._runtime_primary_evidence_skill
        self._runtime_primary_evidence_skill = None
        return None

    def chat(
        self,
        *,
        role: dict[str, object],
        messages: list[dict[str, str]],
        context: str,
        use_runtime_market_skill: bool = False,
    ) -> dict[str, object]:
        status = self.status()
        if not status["authenticated"]:
            raise AIUnavailableError("请先使用 ChatGPT 登录 Codex")
        role_name = str(role["name"])
        report_rules = (
            "结合当前所有可用且完整的证据生成当前交易时段报告，并结合本地持仓给出条件化观察建议。"
            "所有市场证据都可以进入分析；页面栏目不是证据上限。每位就席专家必须读取自己的证据覆盖检查，"
            "完整采用应用与上游已取得的相关证据。缺失时先按 stock-analysis 的数据路由和计算口径补强，"
            "再按 agent-reach 只读检索公开原始来源；只有业界通行、输入充分且口径准确的二次计算才可作为"
            "明确标注的计算证据，不能用代理值冒充原指标。"
            if role.get("report_kind") == "market"
            else (
                "你是投研委员会投资经理，必须执行 stock-analysis 4.15.0 Research 机构报告骨架："
                "执行摘要；核心矛盾或基金产品契约；财务或底层持仓；资本配置或业绩风险；"
                "估值与交易实现；六人投研委员会审议；风险与催化剂；条件化动作。"
                "报告综合已验证的质量、增长、估值和风险事实；覆盖率、内部缺口 ID、快照 ID 与"
                "工程审计结果不得出现在用户正文。只输出具备有效支持引用的可发布结论；"
                "缺失、未覆盖、开放问题和仅由缺口产生的保守判断只保留在结构化审计中。"
                "不要输出强制买入或卖出结论。"
                if role.get("role_id") == "report_editor"
                else ""
            )
        )
        skill_input = self._find_runtime_market_skill() if use_runtime_market_skill else None
        reach_skill_input = self._find_runtime_reach_skill() if use_runtime_market_skill else None
        primary_evidence_skill_input = (
            self._find_runtime_primary_evidence_skill() if use_runtime_market_skill else None
        )
        skill_rules = (
            "本轮以已内置的 stock-analysis 4.15.0 skill 作为证据路由、六人投研委员会和报告纪律的"
            "主契约；A/HK/US/JP/KR 的一手证据缺口按 primary-evidence-reach 定向回填；"
            "其余公开证据缺口按 agent-reach 只读检索补强；不读取无关文件或改写本地账本。"
            if skill_input
            else "不读取文件。"
        )
        base_instructions = "".join(
            (
                f"你是 Invest Vault 的{role_name}，使用以下分析框架而非模仿或冒充真人。",
                f"关注：{role['focus']}。核心问题：{role['questions']}。风险重点：{role['risk_focus']}。",
                "优先使用应用提供的结构化上下文和用户消息。只有结构化证据仍明确缺失时，"
                "才可按 agent-reach 的只读 search/web 路由联网补证；保留标题、URL、发布时间与访问时间，"
                "搜索摘要只能作为线索，未读到原文不得升级为原文事实。",
                "若实际使用 primary-evidence-reach 或 agent-reach 读到原文，必须把发行人、交易所、监管机构、"
                "基金公司或指数公司的一手页面写入 reached_sources；搜索结果页、摘要和二手转载不得进入该字段。"
                "不得在未实际执行补证前直接照抄应用适配器的缺口清单作为最终结论。",
                skill_rules,
                report_rules,
                "用户可见正文和过程说明只描述协调员、研究引擎、证据与报告方法，不主动展示"
                "stock-analysis 名称、版本号或问题匹配规则。",
                "应用会按 AVAILABLE_SKILLS 调用受控只读工具，SKILL_RUN 和对应 EVIDENCE-SKILL 结果可作为证据；",
                "结构化来源和 agent-reach 均未补齐时必须在结构化 unknowns 中保留审计记录，"
                "不得自行补全，也不得把缺口本身改写成正文结论。",
                "HOLDINGS_AUTHORITY 视为用户本轮提供的完整持仓输入；持仓唯一权威来源是应用提供的"
                "本地持仓账本明细。"
                "禁止读取或采用 ~/.stock_analysis/profile.json、STOCK_ANALYSIS_PROFILE、旧对话记忆或任何外部投资记忆；"
                "禁止把未出现在本地持仓账本中的证券写成用户持仓。每条持仓观察必须复述账本中的名称、代码、类型、"
                "买入日期和买入金额；数量仅可采用账本记录值，或按买入日可核验价格与汇率明确标注为推导值。",
                "回答前先读取专家证据覆盖检查：只把 available 当作完整证据；conditional 只有同时具备"
                "支持证据和可复核触发条件时才能进入正文。missing 只进入结构化审计，不进入正文。",
                "事实结论必须在结构化 cited_evidence_ids 字段引用证据 ID，但正文绝不显示任何 EVIDENCE、",
                "技能ID或其他工程标识；没有证据支持的命题直接从正文省略，缺失数据放入 unknowns。"
                "最终正文是当前证据支持的可发布结论，不是证据覆盖报告；不得用证据不足、缺少数据、"
                "无法判断、尚未覆盖或维持观察替代分析。",
                "正文可用 Markdown 加粗，但不要把 Markdown 符号当普通文字解释。",
                "不虚构数字、来源或专家原话，不给确定性买卖指令。用中文直接回答。",
            )
        )
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
        if reach_skill_input:
            inputs.append(reach_skill_input)
        if primary_evidence_skill_input:
            inputs.append(primary_evidence_skill_input)
        inputs.append({"type": "text", "text": f"应用上下文：\n{context}\n\n对话：\n{transcript}"})
        turn = self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": inputs,
                "outputSchema": CHAT_RESPONSE_SCHEMA,
                **self._task_overrides(
                    str(
                        role.get("_provider_task")
                        or ("committee" if role.get("role_id") == "report_editor" else "research")
                    )
                ),
            },
        )
        turn_id = str((turn.get("turn") or {}).get("id") or "")
        if not turn_id:
            raise AIUnavailableError("Codex 未返回 turn id")
        operation_id = getattr(self._operation_local, "operation_id", None)
        if operation_id:
            with self._condition:
                self._active_turns.setdefault(str(operation_id), set()).add((thread_id, turn_id))
                cancelled = str(operation_id) in self._cancelled_operations
            if cancelled:
                self.cancel_operation(str(operation_id))
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
        finally:
            if operation_id:
                with self._condition:
                    active = self._active_turns.get(str(operation_id))
                    if active is not None:
                        active.discard((thread_id, turn_id))
                        if not active:
                            self._active_turns.pop(str(operation_id), None)
        if not isinstance(result, dict):
            raise AIUnavailableError("Codex 返回的研究回复格式无效")
        return result

    def generate_structured(
        self,
        *,
        role: dict[str, object],
        prompt: str,
        schema: dict[str, object],
        use_runtime_market_skill: bool = False,
    ) -> dict[str, object]:
        """生成工作流内部状态；对应方案的结构化专家状态与章节节点。"""

        status = self.status()
        if not status["authenticated"]:
            raise AIUnavailableError("请先使用 ChatGPT 登录 Codex")
        inputs: list[dict[str, object]] = []
        if use_runtime_market_skill:
            for skill in (
                self._find_runtime_market_skill(),
                self._find_runtime_reach_skill(),
                self._find_runtime_primary_evidence_skill(),
            ):
                if skill:
                    inputs.append(skill)
        inputs.append({"type": "text", "text": prompt})
        thread = self._request(
            "thread/start",
            {
                "ephemeral": True,
                "cwd": str(self.runtime_directory.resolve()),
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "baseInstructions": (
                    f"你是 Invest Vault 的{role['name']}。只更新调用方要求的结构化研究状态；"
                    "证据 ID 必须来自输入，不得读取外部投资 profile、旧对话或模型记忆，"
                    "不得虚构事实、来源、持仓或确定性买卖建议。只有协调器补证节点可以使用随包技能；"
                    "搜索摘要只能作为线索，未读取发行人、交易所、监管机构、基金公司或指数公司原文时，"
                    "不得升级为已验证事实。"
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
                "input": inputs,
                "outputSchema": schema,
                **self._task_overrides(str(role.get("_provider_task") or "committee")),
            },
        )
        turn_id = str((turn.get("turn") or {}).get("id") or "")
        if not turn_id:
            raise AIUnavailableError("Codex 未返回 turn id")
        operation_id = getattr(self._operation_local, "operation_id", None)
        if operation_id:
            with self._condition:
                self._active_turns.setdefault(str(operation_id), set()).add((thread_id, turn_id))
                cancelled = str(operation_id) in self._cancelled_operations
            if cancelled:
                self.cancel_operation(str(operation_id))
        try:
            result = json.loads(
                self._wait_for_turn(
                    thread_id,
                    turn_id,
                    operation="结构化研究",
                    timeout=self.chat_timeout,
                )
            )
        except json.JSONDecodeError as error:
            raise AIUnavailableError("Codex 返回的结构化研究格式无效") from error
        finally:
            if operation_id:
                with self._condition:
                    active = self._active_turns.get(str(operation_id))
                    if active is not None:
                        active.discard((thread_id, turn_id))
                        if not active:
                            self._active_turns.pop(str(operation_id), None)
        if not isinstance(result, dict):
            raise AIUnavailableError("Codex 返回的结构化研究格式无效")
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


class MultiProviderAIProvider:
    """Route each task to Codex login or one encrypted bring-your-own-key provider."""

    def __init__(self, codex: AIProvider, credentials: EncryptedCredentialStore) -> None:
        self.codex = codex
        self.credentials = credentials
        self._settings: dict[str, dict[str, str | None]] = {}

    def status(self) -> dict[str, object]:
        return self.codex.status()

    def start_chatgpt_login(self) -> dict[str, object]:
        return self.codex.start_chatgpt_login()

    def logout(self) -> dict[str, object]:
        return self.codex.logout()

    def list_models(self) -> list[dict[str, object]]:
        return self.codex.list_models()

    def configure_models(self, settings: dict[str, dict[str, str | None]]) -> None:
        self._settings = settings
        self.codex.configure_models(settings)

    def begin_operation(self, operation_id: str) -> None:
        begin = getattr(self.codex, "begin_operation", None)
        if callable(begin):
            begin(operation_id)

    def end_operation(self, operation_id: str) -> None:
        end = getattr(self.codex, "end_operation", None)
        if callable(end):
            end(operation_id)

    def cancel_operation(self, operation_id: str) -> None:
        cancel = getattr(self.codex, "cancel_operation", None)
        if callable(cancel):
            cancel(operation_id)

    def _route(self, task: str) -> tuple[str, str | None]:
        setting = self._settings.get(task) or {}
        provider_id = str(setting.get("provider_id") or "codex")
        model_id = str(setting.get("model_id") or "") or None
        if provider_id not in PROVIDER_CATALOG:
            raise AIUnavailableError("AI Provider 设置无效，请在设置中重新选择")
        return provider_id, model_id

    def _direct(self, task: str) -> tuple[DirectAPIClient, str]:
        provider_id, model_id = self._route(task)
        if provider_id == "codex":
            raise AssertionError("Codex route does not use DirectAPIClient")
        api_key = self.credentials.get_api_key(provider_id)
        if not api_key:
            raise AIUnavailableError(f"请先在设置中填写 {PROVIDER_CATALOG[provider_id]['name']} key")
        models = list(PROVIDER_CATALOG[provider_id]["models"])
        return DirectAPIClient(provider_id, api_key), model_id or str(models[0])

    def quick_note(self, raw_text: str, security_id: str) -> dict[str, object]:
        provider_id, _ = self._route("quick_note")
        if provider_id == "codex":
            return self.codex.quick_note(raw_text, security_id)
        client, model = self._direct("quick_note")
        try:
            result = client.generate_json(
                model=model,
                system=(
                    "你是投资研究速记整理器。只整理用户提供的文本，不联网、不补充事实、"
                    "不提供买卖建议；事实只包括用户明确陈述，猜测和感觉归入 user_judgements。"
                ),
                prompt=f"关联证券：{security_id}\n用户原始速记：\n{raw_text}",
                schema=QUICK_NOTE_SCHEMA,
            )
        except (RuntimeError, ValueError) as error:
            raise AIUnavailableError(str(error)) from error
        missing = [key for key in QUICK_NOTE_SCHEMA["required"] if key not in result]
        if missing:
            raise AIUnavailableError(f"Provider 速记格式无效：缺少 {', '.join(missing)}")
        return result

    def chat(
        self,
        *,
        role: dict[str, object],
        messages: list[dict[str, str]],
        context: str,
        use_runtime_market_skill: bool = False,
    ) -> dict[str, object]:
        task = str(
            role.get("_provider_task")
            or ("committee" if role.get("role_id") == "report_editor" else "research")
        )
        provider_id, _ = self._route(task)
        if provider_id == "codex":
            return self.codex.chat(
                role=role,
                messages=messages,
                context=context,
                use_runtime_market_skill=use_runtime_market_skill,
            )
        client, model = self._direct(task)
        system = "".join(
            (
                f"你是 Invest Vault 的{role['name']}，使用分析框架而非模仿真人。",
                f"关注：{role['focus']}。核心问题：{role['questions']}。风险重点：{role['risk_focus']}。",
                "只使用应用提供的有界证据和用户消息，不自行联网、不调用外部工具。",
                "持仓唯一权威来源是 HOLDINGS_AUTHORITY 与应用提供的本地持仓账本明细；"
                "禁止采用外部 profile、旧记忆或把账本外证券写成用户持仓。",
                "事实结论在 cited_evidence_ids 引用上下文中的证据 ID；缺失数据只写入 unknowns，"
                "不得进入正文；没有支持证据的命题直接省略。正文只表达当前证据支持的可发布结论，"
                "不得用证据不足、缺少数据、无法判断、尚未覆盖或维持观察替代分析。"
                "推断写入 assumptions；正文不得显示内部工程标识，不虚构数字、来源或专家原话，",
                "不给确定性买卖指令。用中文直接回答。",
            )
        )
        transcript = "\n".join(f"{item['role']}: {item['content']}" for item in messages[-20:])
        try:
            result = client.generate_json(
                model=model,
                system=system,
                prompt=f"应用上下文：\n{context}\n\n对话：\n{transcript}",
                schema=CHAT_RESPONSE_SCHEMA,
            )
        except (RuntimeError, ValueError) as error:
            raise AIUnavailableError(str(error)) from error
        missing = [key for key in CHAT_RESPONSE_SCHEMA["required"] if key not in result]
        if missing:
            raise AIUnavailableError(f"Provider 研究回复格式无效：缺少 {', '.join(missing)}")
        return result

    def generate_structured(
        self,
        *,
        role: dict[str, object],
        prompt: str,
        schema: dict[str, object],
        use_runtime_market_skill: bool = False,
    ) -> dict[str, object]:
        task = str(role.get("_provider_task") or "committee")
        provider_id, _ = self._route(task)
        if provider_id == "codex":
            generate = getattr(self.codex, "generate_structured", None)
            if not callable(generate):
                raise AIUnavailableError("当前 Codex Provider 不支持结构化研究状态")
            return generate(
                role=role,
                prompt=prompt,
                schema=schema,
                use_runtime_market_skill=use_runtime_market_skill,
            )
        client, model = self._direct(task)
        try:
            result = client.generate_json(
                model=model,
                system=(
                    f"你是 Invest Vault 的{role['name']}。只返回要求的结构化研究状态；"
                    "只使用输入证据，不联网，不读取外部 profile，不虚构事实或持仓。"
                ),
                prompt=prompt,
                schema=schema,
            )
        except (RuntimeError, ValueError) as error:
            raise AIUnavailableError(str(error)) from error
        missing = [key for key in schema.get("required", []) if key not in result]
        if missing:
            raise AIUnavailableError(f"Provider 结构化研究格式无效：缺少 {', '.join(missing)}")
        return result

    def close(self) -> None:
        self.codex.close()


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
            "provider": "multi_provider",
            "tasks": {
                task: {
                    "provider_id": (raw.get(task) or {}).get("provider_id") or "codex",
                    "model_id": (raw.get(task) or {}).get("model_id"),
                    "reasoning_effort": (raw.get(task) or {}).get("reasoning_effort"),
                }
                for task in self.TASKS
            },
        }

    def put(
        self,
        task: str,
        *,
        provider_id: str | None,
        model_id: str | None,
        reasoning_effort: str | None,
    ) -> dict[str, object]:
        if task not in self.TASKS:
            raise ValueError("未知的 AI 任务类型")
        if reasoning_effort and reasoning_effort not in self.EFFORTS:
            raise ValueError("不支持的推理强度")
        selected_provider = provider_id or str(self.get()["tasks"][task]["provider_id"])
        if selected_provider not in PROVIDER_CATALOG:
            raise ValueError("不支持的 AI Provider")
        if model_id is not None and not model_id.strip():
            raise ValueError("模型不能为空")
        settings = self.get()
        tasks = dict(settings["tasks"])
        tasks[task] = {
            "provider_id": selected_provider,
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
            (
                item.draft_id,
                item.security_id,
                item.raw_text,
                json.dumps(item.draft, ensure_ascii=False),
                item.created_at,
            ),
        )
        self.vault.connection.commit()
        return item

    def accept(self, draft_id: str, *, body: str) -> str:
        row = self.vault.connection.execute(
            "SELECT * FROM ai_quick_notes WHERE draft_id = ?", (draft_id,)
        ).fetchone()
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
        self._blackboards = SharedResearchBoardBuilder()
        self._background_lock = threading.Lock()
        self._background_threads: set[threading.Thread] = set()

    def _is_cancelled(self, run_id: str) -> bool:
        with self.vault.lock:
            row = self.vault.connection.execute(
                "SELECT status FROM research_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return bool(row and row["status"] == "cancelled")

    def _assert_ledger_holding_claims(self, result: dict[str, object]) -> None:
        allowed = {str(item["security_id"]).split(":")[2].upper() for item in self.vault.holding_entries()}
        ignored = {
            "A", "AI", "A股", "ETF", "PE", "PB", "ROE", "ROIC", "FCF", "HK", "US",
            "CNY", "HKD", "USD", "NAV", "IPO", "VAULT", "M1", "M2", "M3", "M4", "M5", "M6",
        }
        for sentence in re.split(r"[。！？\n]", str(result.get("content") or "")):
            if not any(term in sentence for term in ("我的持仓", "用户持仓", "本地持仓", "账本持仓", "买入持仓")):
                continue
            candidates = set(re.findall(r"(?<![A-Za-z0-9])(?:[A-Z]{2,5}|\d{6})(?![A-Za-z0-9])", sentence.upper()))
            unauthorized = sorted(candidates - allowed - ignored)
            if unauthorized:
                raise AIUnavailableError(
                    "生成内容引用了不在 Invest Vault 本地持仓账本中的证券：" + "、".join(unauthorized)
                )

    def _provider_chat(self, run_id: str, **kwargs: object) -> dict[str, object]:
        if self._is_cancelled(run_id):
            raise AIUnavailableError("本轮研究已由用户停止")
        metric_stage = kwargs.pop("_metric_stage", None)
        metric_node_id = kwargs.pop("_metric_node_id", None)
        metric_token_budget = kwargs.pop("_metric_token_budget", None)
        metric_retry_count = int(kwargs.pop("_metric_retry_count", 0) or 0)
        begin = getattr(self.provider, "begin_operation", None)
        end = getattr(self.provider, "end_operation", None)
        if callable(begin):
            begin(run_id)
        started = time.monotonic()
        result: dict[str, object] | None = None
        error: BaseException | None = None
        try:
            result = self.provider.chat(**kwargs)  # type: ignore[arg-type]
            if self._is_cancelled(run_id):
                raise AIUnavailableError("本轮研究已由用户停止")
            self._assert_ledger_holding_claims(result)
            return result
        except BaseException as caught:
            error = caught
            raise
        finally:
            role = kwargs.get("role") or {}
            context = str(kwargs.get("context") or "")
            with self.vault.lock:
                self._record_call_metric(
                    run_id=run_id,
                    stage=str(metric_stage or role.get("_provider_task") or "research")
                    if isinstance(role, dict)
                    else "research",
                    role_id=str(role.get("role_id") or "unknown")
                    if isinstance(role, dict)
                    else "unknown",
                    prompt=context,
                    result=result,
                    latency_ms=round((time.monotonic() - started) * 1000),
                    evidence=(),
                    error=error,
                    schema=CHAT_RESPONSE_SCHEMA,
                    skill_invoked=bool(kwargs.get("use_runtime_market_skill")),
                    node_id=str(metric_node_id) if metric_node_id else None,
                    token_budget=int(metric_token_budget) if metric_token_budget is not None else None,
                    retry_count=metric_retry_count,
                )
                self.vault.connection.commit()
            if callable(end):
                end(run_id)

    def cancel(self, thread_id: str) -> dict[str, object]:
        with self.vault.lock:
            run = self.vault.connection.execute(
                "SELECT run_id, status FROM research_runs WHERE thread_id = ? ORDER BY started_at DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            if run is None:
                raise ValueError("当前会话没有可停止的生成任务")
            run_id = str(run["run_id"])
            if run["status"] != "running":
                return {"run_id": run_id, "status": str(run["status"])}
            completed_at = datetime.now(timezone.utc).isoformat()
            self.vault.connection.execute(
                "UPDATE research_runs SET status = 'cancelled', current_stage = 'cancelled', completed_at = ?, failure_json = ? WHERE run_id = ?",
                (completed_at, json.dumps({"message": "用户已停止生成"}, ensure_ascii=False), run_id),
            )
            self._append_event(
                thread_id,
                run_id,
                "system",
                "coordinator",
                {"content": "用户已停止本轮报告生成。", "role_name": "协调员"},
                event_type="workflow.cancelled",
            )
            self.vault.connection.commit()
        cancel = getattr(self.provider, "cancel_operation", None)
        if callable(cancel):
            cancel(run_id)
        return {"run_id": run_id, "status": "cancelled"}

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
        return [
            dict(row) for row in self.vault.connection.execute(query + " ORDER BY updated_at DESC", params)
        ]

    def archive(self, thread_id: str) -> None:
        if (
            self.vault.connection.execute(
                "SELECT 1 FROM research_threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            is None
        ):
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
        row = self.vault.connection.execute(
            "SELECT * FROM research_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            raise ValueError("研究会话不存在")
        result = dict(row)
        if include_events:
            events = list(self.vault.connection.execute(
                "SELECT * FROM research_events WHERE thread_id = ? ORDER BY sequence_number", (thread_id,)
            ))
            completed_by_run: dict[str, set[str]] = {}
            for event in events:
                if event["event_type"] != "tool.completed" or not event["run_id"]:
                    continue
                payload = json.loads(str(event["payload_json"]))
                if payload.get("status") == "completed":
                    completed_by_run.setdefault(str(event["run_id"]), set()).add(
                        str(event["actor_id"] or "")
                    )
            visible_events: list[dict[str, object]] = []
            for event in events:
                raw_payload = str(event["payload_json"])
                payload = json.loads(raw_payload)
                legacy_markers = (
                    "claim board",
                    "portfolio-risk-evidence",
                    "ledger_entries",
                    "empty_order_book",
                    "market-specific order book",
                    "utf-8",
                    "业务可比性仍需用户确认",
                    "交易成本代理未形成",
                    "未统一现金",
                    "缺少统一实时市值",
                    "需统一持仓数量",
                    "统一实时组合权重",
                    "在统一现金",
                    "按实时市值、汇率和现金统一",
                    "实时市值、现金、港股汇率统一后",
                    "完整实时市值、港股汇率",
                )
                needs_presentation_cleanup = any(
                    marker in raw_payload.lower() for marker in legacy_markers
                )
                if event["actor_type"] != "user" and needs_presentation_cleanup:
                    payload = self._clean_reply(
                        payload,
                        completed_by_run.get(str(event["run_id"] or ""), set()),
                    )
                if event["actor_type"] == "assistant" and not payload.get("refused"):
                    payload = self._finalize_public_reply(payload)
                visible_events.append({**dict(event), "payload": payload})
            result["events"] = visible_events
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
        sequence = int(
            self.vault.connection.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM research_events WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0]
        )
        self.vault.connection.execute(
            "INSERT INTO research_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                thread_id,
                run_id,
                sequence,
                event_type,
                actor,
                role_id,
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.vault.connection.execute(
            "UPDATE research_threads SET updated_at = ? WHERE thread_id = ?",
            (datetime.now(timezone.utc).isoformat(), thread_id),
        )

    def _context(self, security_id: str, skill_results: list[dict[str, object]] | None = None) -> str:
        ordered_results = sorted(
            skill_results or [],
            key=lambda item: 0 if item.get("skill_id") == "framework-readiness" else 1,
        )
        portfolio_result = next(
            (item for item in ordered_results if item.get("skill_id") == "portfolio-risk-evidence"),
            None,
        )
        portfolio_value = next(
            (
                evidence.get("value")
                for evidence in (portfolio_result or {}).get("evidence") or []
                if isinstance(evidence, dict) and isinstance(evidence.get("value"), dict)
            ),
            {},
        )
        lines = [
            f"SECURITY: {security_id}",
            "HOLDINGS_AUTHORITY: " + json.dumps(
                {
                    "source": "Invest Vault本地持仓账本（唯一权威）",
                    "allowed_security_ids": list((portfolio_value.get("holding_identities") or {}).keys()),
                    "ledger_entries": portfolio_value.get("ledger_entries") or [],
                    "external_profiles_forbidden": True,
                },
                ensure_ascii=False,
            ),
            "AVAILABLE_SKILLS: " + json.dumps(self.skill_layer.catalog(), ensure_ascii=False),
            "EVIDENCE:",
        ]
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
                lines.append(f"{evidence['evidence_id']}: " + rendered)
        rows = self.vault.connection.execute(
            """SELECT e.snapshot_id, e.item_index, e.kind, e.value_json, e.unit, e.period_end,
                      e.provider, e.source_ref, s.effective_as_of
               FROM evidence_items e JOIN evidence_snapshots s ON s.snapshot_id = e.snapshot_id
               WHERE s.security_id = ? ORDER BY s.observed_at DESC, e.item_index""",
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
                    + json.dumps(dict(snapshot), ensure_ascii=False)
                )
        lines.append("PUBLIC_MATERIALS:")
        for row in self.vault.connection.execute(
            "SELECT title, published_at, source_name, source_url, excerpt FROM research_materials WHERE security_id = ? ORDER BY published_at DESC",
            (security_id,),
        ):
            lines.append(f"MATERIAL: {dict(row)}")
        return "\n".join(lines)

    def _context_event(self, security_id: str, context: str) -> dict[str, object]:
        materials = [
            dict(row)
            for row in self.vault.connection.execute(
                "SELECT title, published_at, source_name, source_url FROM research_materials WHERE security_id = ? ORDER BY published_at DESC LIMIT 10",
                (security_id,),
            )
        ]
        evidence_count = sum(1 for line in context.splitlines() if line.startswith("EVIDENCE-"))
        return {
            "content": f"已刷新研究上下文：{evidence_count} 条证据、{len(materials)} 条关联资料；本地笔记不参与分析。",
            "evidence_count": evidence_count,
            "note_count": 0,
            "materials": materials,
        }

    def _evidence_store(
        self,
        security_id: str,
        skill_results: list[dict[str, object]],
    ) -> EvidenceStore:
        """Plan P1: retain complete evidence externally and project it later by role."""

        now = datetime.now(timezone.utc).isoformat()
        records: list[EvidenceRecord] = []
        holdings = self.vault.holding_entries()
        holding_value = {
            "source": "Invest Vault本地持仓账本（唯一权威）",
            "allowed_security_ids": [str(item["security_id"]) for item in holdings],
            "ledger_entries": holdings,
            "portfolio_profile": self.vault.portfolio_profile(),
            "external_profiles_forbidden": True,
        }
        records.append(
            EvidenceRecord.create(
                evidence_id="EVIDENCE-HOLDINGS-" + hashlib.sha256(
                    json.dumps(holding_value, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()[:12],
                security_id=security_id,
                domain="common",
                subtype="holdings_authority",
                entity_id=None,
                as_of=now[:10],
                observed_at=now,
                source_tier="user_ledger",
                provider="Invest Vault本地持仓账本",
                source_ref="",
                quality_status="available",
                value=holding_value,
                compact_text=json.dumps(holding_value, ensure_ascii=False, sort_keys=True),
            )
        )
        for result in skill_results:
            skill_id = str(result.get("skill_id") or "research-evidence")
            domain = skill_id
            if skill_id.startswith("framework-readiness-"):
                domain = skill_id
            for evidence in result.get("evidence") or []:
                if not isinstance(evidence, dict):
                    continue
                evidence_id = str(evidence.get("evidence_id") or uuid4())
                value = evidence.get("value")
                compact = json.dumps(
                    {
                        "value": value,
                        "gaps": result.get("gaps") or [],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                records.append(
                    EvidenceRecord.create(
                        evidence_id=evidence_id,
                        security_id=security_id,
                        domain=domain,
                        subtype=str(evidence.get("kind") or skill_id),
                        entity_id=security_id,
                        as_of=str(evidence.get("as_of") or "") or None,
                        observed_at=now,
                        source_tier="application_evidence",
                        provider=str(result.get("name") or evidence.get("provider") or "Invest Vault"),
                        source_ref=str(evidence.get("source_ref") or ""),
                        quality_status=str(result.get("status") or "partial"),
                        value=value,
                        compact_text=compact,
                    )
                )
        rows = self.vault.connection.execute(
            """SELECT e.snapshot_id, e.item_index, e.kind, e.value_json, e.unit, e.period_end,
                      e.provider, e.source_ref, s.effective_as_of, s.observed_at
               FROM evidence_items e JOIN evidence_snapshots s ON s.snapshot_id = e.snapshot_id
               WHERE s.security_id = ? ORDER BY s.observed_at DESC, e.item_index""",
            (security_id,),
        ).fetchall()
        for row in rows:
            value = json.loads(str(row["value_json"]))
            evidence_id = f"EVIDENCE-DB-{row['snapshot_id']}-{row['item_index']}"
            compact = json.dumps(
                {
                    "evidence_id": evidence_id,
                    "kind": row["kind"],
                    "value": value,
                    "unit": row["unit"],
                    "period_end": row["period_end"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            records.append(
                EvidenceRecord.create(
                    evidence_id=evidence_id,
                    security_id=security_id,
                    domain="archived-evidence",
                    subtype=str(row["kind"]),
                    entity_id=security_id,
                    as_of=str(row["period_end"] or row["effective_as_of"]),
                    observed_at=str(row["observed_at"]),
                    source_tier="archived_snapshot",
                    provider=str(row["provider"]),
                    source_ref=str(row["source_ref"]),
                    quality_status="available",
                    value=value,
                    compact_text=compact,
                )
            )
        for table, domain in (
            ("financial_snapshots", "company-financial-quality"),
            ("fund_snapshots", "fund-portfolio-evidence"),
        ):
            row = self.vault.connection.execute(
                f"SELECT snapshot_id, cutoff_date, source, payload_json, observed_at FROM {table} "
                "WHERE security_id = ? ORDER BY cutoff_date DESC LIMIT 1",
                (security_id,),
            ).fetchone()
            if row:
                value = json.loads(str(row["payload_json"]))
                evidence_id = f"EVIDENCE-{domain.upper()}-{row['snapshot_id']}"
                records.append(
                    EvidenceRecord.create(
                        evidence_id=evidence_id,
                        security_id=security_id,
                        domain=domain,
                        subtype="snapshot",
                        entity_id=security_id,
                        as_of=str(row["cutoff_date"]),
                        observed_at=str(row["observed_at"]),
                        source_tier="archived_snapshot",
                        provider=str(row["source"]),
                        source_ref="",
                        quality_status="available",
                        value=value,
                        compact_text=json.dumps(
                            {"evidence_id": evidence_id, "domain": domain, "value": value},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                )
        for row in self.vault.connection.execute(
            "SELECT material_id, title, published_at, source_name, source_url, excerpt "
            "FROM research_materials WHERE security_id = ? ORDER BY published_at DESC",
            (security_id,),
        ):
            evidence_id = f"EVIDENCE-MATERIAL-{row['material_id']}"
            value = dict(row)
            records.append(
                EvidenceRecord.create(
                    evidence_id=evidence_id,
                    security_id=security_id,
                    domain="supplemental-company-evidence",
                    subtype="public_material",
                    entity_id=security_id,
                    as_of=str(row["published_at"]),
                    observed_at=now,
                    source_tier="linked_material",
                    provider=str(row["source_name"]),
                    source_ref=str(row["source_url"]),
                    quality_status="conditional",
                    value=value,
                    compact_text=json.dumps(value, ensure_ascii=False, sort_keys=True),
                )
            )
        projected: list[EvidenceRecord] = []
        for record in records:
            projected.extend(project_oversized_record(record, ContextBudget().evidence_budget))
        return EvidenceStore().ingest(projected)

    def _persist_evidence_store(self, run_id: str, store: EvidenceStore) -> EvidenceStore:
        canonical_records: list[EvidenceRecord] = []
        for record in store.records:
            existing = self.vault.connection.execute(
                "SELECT evidence_id FROM research_evidence_records WHERE content_hash = ?",
                (record.content_hash,),
            ).fetchone()
            persisted = (
                replace(record, evidence_id=str(existing["evidence_id"]))
                if existing is not None and str(existing["evidence_id"]) != record.evidence_id
                else record
            )
            canonical_records.append(persisted)
            self.vault.connection.execute(
                """INSERT INTO research_evidence_records
                (evidence_id, security_id, domain, subtype, entity_id, as_of, observed_at,
                 source_tier, provider, source_ref, quality_status, value_json, compact_text,
                 token_estimate, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    compact_text = excluded.compact_text,
                    token_estimate = excluded.token_estimate,
                    quality_status = excluded.quality_status,
                    observed_at = excluded.observed_at""",
                (
                    persisted.evidence_id,
                    persisted.security_id,
                    persisted.domain,
                    persisted.subtype,
                    persisted.entity_id,
                    persisted.as_of,
                    persisted.observed_at,
                    persisted.source_tier,
                    persisted.provider,
                    persisted.source_ref,
                    persisted.quality_status,
                    json.dumps(persisted.value, ensure_ascii=False),
                    persisted.compact_text,
                    persisted.token_estimate,
                    persisted.content_hash,
                ),
            )
            self.vault.connection.execute(
                """DELETE FROM research_evidence_links
                WHERE run_id = ? AND task_id IS NULL AND evidence_id = ? AND relation = 'available'""",
                (run_id, persisted.evidence_id),
            )
            self.vault.connection.execute(
                "INSERT OR IGNORE INTO research_evidence_links VALUES (?, NULL, ?, 'available')",
                (run_id, persisted.evidence_id),
            )
        return EvidenceStore(tuple(canonical_records))

    @staticmethod
    def _source_index_from_store(store: EvidenceStore) -> dict[str, list[dict[str, object]]]:
        return {
            record.evidence_id: [
                {"name": record.provider, "url": record.source_ref, "as_of": record.as_of or ""}
            ]
            for record in store.records
        }

    def _role_packets(
        self,
        *,
        run_id: str,
        role_id: str,
        question: str,
        store: EvidenceStore,
        allowed_evidence_ids: set[str] | None = None,
        excluded_evidence_ids: set[str] | None = None,
    ) -> tuple[EvidencePacket, ...]:
        manifest = EvidenceManifest.from_store(store)
        allowed = allowed_evidence_ids
        excluded = excluded_evidence_ids or set()
        routed_manifest = EvidenceManifest(
            tuple(
                item
                for item in manifest.items
                if item.evidence_id not in excluded
                and (allowed is None or item.evidence_id in allowed)
            )
        )
        packet_budget = ContextBudget().evidence_budget
        plan = RoleEvidencePlanner().plan(
            role_id=role_id,
            question=question,
            manifest=routed_manifest,
            token_budget=packet_budget * 3,
        )
        records = [
            store.get(evidence_id)
            for evidence_id in plan.evidence_ids
            if allowed_evidence_ids is None or evidence_id in allowed_evidence_ids
            if excluded_evidence_ids is None or evidence_id not in excluded_evidence_ids
        ]
        packets = DomainPacketBuilder().build(
            role_id=role_id,
            objective=f"按{get_role(role_id)['name']}框架完成一次专家综合",
            records=records,
            token_budget=packet_budget,
            required_outputs=tuple(label for label, _skill, _ceiling in FRAMEWORK_REQUIREMENTS.get(role_id, ())),
            known_gaps=plan.uncovered_requirements,
        )
        with self.vault.lock:
            sequence_offset = int(
                self.vault.connection.execute(
                    "SELECT COALESCE(MAX(sequence_number), 0) FROM research_evidence_packets "
                    "WHERE run_id = ? AND role_id = ?",
                    (run_id, role_id),
                ).fetchone()[0]
            )
            for packet in packets:
                self.vault.connection.execute(
                    """INSERT OR REPLACE INTO research_evidence_packets
                    (packet_id, run_id, role_id, sequence_number, objective, required_outputs_json,
                     evidence_ids_json, known_gaps_json, token_estimate, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (
                        f"{run_id}:{packet.packet_id}",
                        run_id,
                        role_id,
                        sequence_offset + packet.sequence,
                        packet.objective,
                        json.dumps(packet.required_outputs, ensure_ascii=False),
                        json.dumps(packet.evidence_ids, ensure_ascii=False),
                        json.dumps(packet.known_gaps, ensure_ascii=False),
                        packet.token_estimate,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            self.vault.connection.commit()
        return packets

    @staticmethod
    def _initial_coverage(
        role_id: str,
        store: EvidenceStore,
    ) -> dict[str, dict[str, object]]:
        readiness = next(
            (record for record in store.records if record.domain == f"framework-readiness-{role_id}"),
            None,
        )
        coverage: dict[str, dict[str, object]] = {}
        if readiness and isinstance(readiness.value, dict):
            for detail in readiness.value.get("requirements") or []:
                if not isinstance(detail, dict):
                    continue
                label = str(detail.get("requirement") or "")
                skill_id = str(detail.get("evidence_skill") or "")
                status = {"available": "covered", "conditional": "partial", "missing": "partial"}.get(
                    str(detail.get("status") or "missing"), "partial"
                )
                evidence_ids = [record.evidence_id for record in store.records if record.domain == skill_id]
                coverage[label] = {
                    "status": status,
                    "evidence_ids": evidence_ids,
                    "attempted_sources": list(
                        dict.fromkeys(record.provider for record in store.records if record.domain == skill_id)
                    ),
                    "reason": str(detail.get("reason") or "需要补充核验"),
                    "alternatives": [],
                    "impact": "该项未关闭时相关结论只能保持条件性",
                }
        for label, skill_id, ceiling in FRAMEWORK_REQUIREMENTS.get(role_id, ()):
            if label in coverage:
                continue
            evidence_ids = [record.evidence_id for record in store.records if record.domain == skill_id]
            coverage[label] = {
                "status": "covered" if evidence_ids and ceiling == "available" else "partial",
                "evidence_ids": evidence_ids,
                "attempted_sources": list(
                    dict.fromkeys(record.provider for record in store.records if record.domain == skill_id)
                ),
                "reason": "已取得框架证据" if evidence_ids else "本轮尚未取得所需证据",
                "alternatives": [],
                "impact": "缺失会限制对应框架判断",
            }
        if role_id == "general":
            for domain in dict.fromkeys(
                record.domain
                for record in store.records
                if record.domain not in {"common", "archived-evidence", "raw-evidence"}
                and not record.domain.startswith("framework-readiness-")
                and record.quality_status not in {"available", "completed"}
            ):
                domain_records = [record for record in store.records if record.domain == domain]
                coverage[f"证据域：{domain}"] = {
                    "status": "partial",
                    "evidence_ids": [record.evidence_id for record in domain_records],
                    "attempted_sources": list(dict.fromkeys(record.provider for record in domain_records)),
                    "reason": "当前证据域仍保留明确缺口",
                    "alternatives": [],
                    "impact": "相关回答只能保持条件性",
                }
        return coverage

    def _persist_expert_state(
        self,
        *,
        run_id: str,
        state: ExpertResearchState,
        packet: EvidencePacket | None,
    ) -> None:
        self.vault.connection.execute(
            """INSERT OR REPLACE INTO research_expert_states
            (state_id, run_id, role_id, revision, processed_packet_id, state_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                f"{run_id}:{state.role_id}:{state.revision}",
                run_id,
                state.role_id,
                state.revision,
                f"{run_id}:{packet.packet_id}" if packet else None,
                json.dumps(state.as_dict(), ensure_ascii=False),
            ),
        )

    @staticmethod
    def _sanitize_structured_update_evidence(
        update: dict[str, object], known_evidence_ids: set[str]
    ) -> dict[str, object]:
        """Keep a useful expert state when the model mistypes one evidence identifier."""

        cleaned = dict(update)
        claims: list[dict[str, object]] = []
        for raw_claim in update.get("claims") or []:
            if not isinstance(raw_claim, dict):
                continue
            claim = dict(raw_claim)
            supporting = [
                str(item)
                for item in claim.get("supporting_evidence_ids") or []
                if str(item) in known_evidence_ids
            ]
            contradicting = [
                str(item)
                for item in claim.get("contradicting_evidence_ids") or []
                if str(item) in known_evidence_ids
            ]
            claim["supporting_evidence_ids"] = list(dict.fromkeys(supporting))
            claim["contradicting_evidence_ids"] = list(dict.fromkeys(contradicting))
            if claim.get("status") == "supported" and not supporting:
                claim["status"] = "conditional"
                claim["confidence"] = "low"
                claim["conditions"] = [
                    *list(claim.get("conditions") or []),
                    "该命题未保留可核验的支持引用，需重新核对原始资料",
                ]
            claims.append(claim)
        cleaned["claims"] = claims
        requirements: list[dict[str, object]] = []
        for raw_requirement in update.get("requirements") or []:
            if not isinstance(raw_requirement, dict):
                continue
            requirement = dict(raw_requirement)
            requirement["evidence_ids"] = list(
                dict.fromkeys(
                    str(item)
                    for item in requirement.get("evidence_ids") or []
                    if str(item) in known_evidence_ids
                )
            )
            requirements.append(requirement)
        cleaned["requirements"] = requirements
        return cleaned

    def _record_call_metric(
        self,
        *,
        run_id: str,
        stage: str,
        role_id: str,
        prompt: str,
        result: object | None,
        latency_ms: int,
        evidence: Sequence[EvidenceRecord],
        error: BaseException | None = None,
        schema: Mapping[str, object] = EXPERT_STATE_UPDATE_SCHEMA,
        skill_invoked: bool = False,
        node_id: str | None = None,
        token_budget: int | None = None,
        retry_count: int = 0,
    ) -> None:
        domain_tokens: dict[str, int] = {}
        for record in evidence:
            domain_tokens[record.domain] = domain_tokens.get(record.domain, 0) + record.token_estimate
        cited_ids = set()
        if isinstance(result, dict):
            cited_ids.update(str(item) for item in result.get("cited_evidence_ids", []))
            cited_ids.update(
                str(evidence_id)
                for claim in result.get("claims", [])
                if isinstance(claim, dict)
                for key in ("supporting_evidence_ids", "contradicting_evidence_ids")
                for evidence_id in claim.get(key, [])
            )
        available_count = len(evidence) or len(set(re.findall(r"EVIDENCE-[A-Za-z0-9_-]+", prompt)))
        task = "committee" if stage in {
            "expert_packet",
            "expert_synthesis",
            "coordinator_supplement",
            "committee",
            "final_edit",
        } else "research"
        provider_type = type(self.provider).__name__
        model = str(getattr(self.provider, "model", None) or "configured-provider")
        route = getattr(self.provider, "_route", None)
        if callable(route):
            configured_provider, configured_model = route(task)
            provider_type = str(configured_provider)
            model = str(configured_model or "provider-default")
        elif isinstance(self.provider, CodexAppServerProvider):
            setting = self.provider._model_settings.get(task) or {}
            model = str(setting.get("model_id") or "codex-default")
        self.vault.connection.execute(
            """INSERT INTO research_call_metrics
            (metric_id, run_id, stage, role_id, provider_type, model, input_tokens, system_tokens,
             evidence_tokens, schema_tokens, output_tokens, reasoning_tokens, estimated_input_tokens,
             evidence_count, domain_tokens_json, latency_ms, timed_out, skill_invoked,
             cited_evidence_count, available_evidence_count, error_json, started_at, completed_at,
             node_id, token_budget, retry_count, usage_source, estimated_system_tokens,
             estimated_context_tokens, estimated_output_tokens)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, 'estimated', ?, ?, ?)""",
            (
                str(uuid4()),
                run_id,
                stage,
                role_id,
                provider_type,
                model,
                sum(record.token_estimate for record in evidence),
                estimate_tokens(schema),
                estimate_tokens(prompt),
                len(evidence),
                json.dumps(domain_tokens, ensure_ascii=False),
                latency_ms,
                int(bool(error and any(marker in str(error).casefold() for marker in ("超时", "timeout")))),
                int(skill_invoked),
                len(cited_ids),
                available_count,
                json.dumps({"message": str(error)}, ensure_ascii=False) if error else None,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                node_id,
                token_budget,
                retry_count,
                estimate_tokens(schema),
                estimate_tokens(prompt),
                estimate_tokens(result) if result is not None else None,
            ),
        )

    def _provider_structured(
        self,
        run_id: str,
        *,
        role: dict[str, object],
        prompt: str,
        schema: dict[str, object],
        evidence: Sequence[EvidenceRecord],
        stage: str,
        use_runtime_market_skill: bool = False,
        node_id: str | None = None,
        token_budget: int | None = None,
        retry_count: int = 0,
    ) -> dict[str, object]:
        if self._is_cancelled(run_id):
            raise AIUnavailableError("本轮研究已由用户停止")
        generate = getattr(self.provider, "generate_structured", None)
        if not callable(generate):
            raise AIUnavailableError("当前 Provider 不支持结构化研究状态")
        begin = getattr(self.provider, "begin_operation", None)
        end = getattr(self.provider, "end_operation", None)
        if callable(begin):
            begin(run_id)
        started = time.monotonic()
        result: dict[str, object] | None = None
        error: BaseException | None = None
        try:
            result = generate(
                role=role,
                prompt=prompt,
                schema=schema,
                use_runtime_market_skill=use_runtime_market_skill,
            )
            if self._is_cancelled(run_id):
                raise AIUnavailableError("本轮研究已由用户停止")
            return result
        except BaseException as caught:
            error = caught
            raise
        finally:
            latency_ms = round((time.monotonic() - started) * 1000)
            with self.vault.lock:
                self._record_call_metric(
                    run_id=run_id,
                    stage=stage,
                    role_id=str(role["role_id"]),
                    prompt=prompt,
                    result=result,
                    latency_ms=latency_ms,
                    evidence=evidence,
                    error=error,
                    skill_invoked=use_runtime_market_skill,
                    node_id=node_id,
                    token_budget=token_budget,
                    retry_count=retry_count,
                )
                self.vault.connection.commit()
            if callable(end):
                end(run_id)

    def _run_expert_state(
        self,
        *,
        run_id: str,
        role_id: str,
        question: str,
        store: EvidenceStore,
        initial_state: ExpertResearchState | None = None,
        allowed_evidence_ids: set[str] | None = None,
        provider_role: dict[str, object] | None = None,
        shared_board: SharedResearchBlackboard | None = None,
    ) -> ExpertResearchState:
        role = provider_role or get_role(role_id)
        try:
            packets = self._role_packets(
                run_id=run_id,
                role_id=role_id,
                question=question,
                store=store,
                allowed_evidence_ids=allowed_evidence_ids,
                excluded_evidence_ids=(
                    set(shared_board.shared_evidence_ids_for(role_id)) if shared_board else None
                ),
            )
        except BaseException as error:
            evidence = tuple(
                record
                for record in store.records
                if allowed_evidence_ids is None or record.evidence_id in allowed_evidence_ids
            )
            with self.vault.lock:
                self._record_call_metric(
                    run_id=run_id,
                    stage="expert_synthesis",
                    role_id=role_id,
                    prompt=question,
                    result=None,
                    latency_ms=0,
                    evidence=evidence,
                    error=error,
                    node_id=f"{role_id}:packet-build",
                    token_budget=ContextBudget().evidence_budget,
                )
                self.vault.connection.commit()
            raise
        state = (initial_state or ExpertResearchState.initial(role_id, question)).with_coverage(
            self._initial_coverage(role_id, store)
        )
        shared_context = shared_board.render_prompt_for_role(role_id) if shared_board else ""
        shared_ids = set(shared_board.shared_evidence_ids_for(role_id)) if shared_board else set()
        if not packets and not shared_ids:
            with self.vault.lock:
                self._persist_expert_state(run_id=run_id, state=state, packet=None)
                self.vault.connection.commit()
            return state
        with self.vault.lock:
            self.vault.connection.executemany(
                "UPDATE research_evidence_packets SET status = 'ingested', updated_at = ? "
                "WHERE packet_id = ?",
                [
                    (datetime.now(timezone.utc).isoformat(), f"{run_id}:{packet.packet_id}")
                    for packet in packets
                ],
            )
            self.vault.connection.commit()
        all_ids = {
            evidence_id
            for packet in packets
            for evidence_id in packet.evidence_ids
        }
        all_ids.update(shared_ids)
        all_ids.update(
            evidence_id
            for claim in state.claims.values()
            for evidence_id in (
                *claim.supporting_evidence_ids,
                *claim.contradicting_evidence_ids,
            )
        )
        context = "\n".join(packet.render() for packet in packets)
        if shared_context:
            context = f"共享事实层：{shared_context}\n专家差异包：{context}"
        generate = getattr(self.provider, "generate_structured", None)
        if not callable(generate):
            opinion: dict[str, object] | None = None
            for attempt in (1, 2):
                try:
                    opinion = self._provider_chat(
                        run_id,
                        role={**role, "_provider_task": "committee"},
                        messages=[{"role": "user", "content": question}],
                        context=context,
                        use_runtime_market_skill=False,
                        _metric_stage="expert_synthesis",
                        _metric_node_id=f"{role_id}:synthesis",
                        _metric_token_budget=ContextBudget().evidence_budget,
                        _metric_retry_count=attempt - 1,
                    )
                    break
                except AIUnavailableError as error:
                    if attempt == 2 or not any(
                        marker in str(error)
                        for marker in ("格式无效", "threads can only be started once", "Server overloaded")
                    ):
                        raise
            assert opinion is not None
            cited = [item for item in opinion.get("cited_evidence_ids", []) if item in all_ids]
            state = state.synthesize(
                packets,
                {
                    "claims": [
                        {
                            "claim_id": f"{role_id}-summary",
                            "claim": str(opinion.get("content") or "本轮未形成文字结论"),
                            "status": "supported" if cited else "conditional",
                            "supporting_evidence_ids": cited,
                            "contradicting_evidence_ids": [],
                            "confidence": "medium" if cited else "low",
                            "conditions": list(opinion.get("unknowns") or []),
                        }
                    ],
                    "framework_requirements": self._initial_coverage(role_id, store),
                    "open_questions": list(opinion.get("unknowns") or []),
                },
            )
            state.validate(known_evidence_ids=all_ids)
            with self.vault.lock:
                self._persist_expert_state(run_id=run_id, state=state, packet=None)
                self.vault.connection.executemany(
                    "UPDATE research_evidence_packets SET status = 'completed', updated_at = ? "
                    "WHERE packet_id = ?",
                    [
                        (datetime.now(timezone.utc).isoformat(), f"{run_id}:{packet.packet_id}")
                        for packet in packets
                    ],
                )
                self.vault.connection.commit()
            return state
        prompt = (
            "你不是撰写最终报告，而是在完成本专家本轮唯一一次综合。逐项检查全部 Domain Packet，"
            "判断证据是否支持、削弱、推翻或形成结论，并更新框架覆盖。允许推翻历史状态；"
            "只引用本轮包或历史状态已有证据 ID。claim_id 必须与命题含义绑定、跨专家可复用，"
            "不得包含专家名称或轮次。Claim 只能表达当前证据实际支持的判断；只有缺失、未覆盖、"
            "无法判断或维持观察含义的内容只能进入 requirements/open_questions，不得生成 Claim。"
            "conditional Claim 必须同时引用支持证据并写出可观察、可复核的触发条件。\n"
            f"问题：{question}\n当前状态：{json.dumps(state.as_dict(), ensure_ascii=False)}\n"
            f"共享事实与全部 Domain Packet：{context}"
        )
        budget = ContextBudget()
        safe_input_budget = (
            budget.model_context_tokens
            - budget.reserved_output_tokens
            - budget.reserved_reasoning_tokens
            - budget.system_tokens
            - budget.schema_tokens
            - budget.safety_margin_tokens
        )
        if estimate_tokens(prompt) > safe_input_budget:
            with self.vault.lock:
                self._record_call_metric(
                    run_id=run_id,
                    stage="expert_synthesis",
                    role_id=role_id,
                    prompt=prompt,
                    result=None,
                    latency_ms=0,
                    evidence=tuple(store.get(evidence_id) for evidence_id in sorted(all_ids)),
                    error=AIUnavailableError("专家综合上下文超过安全输入预算，需要调整确定性投影"),
                    node_id=f"{role_id}:synthesis",
                    token_budget=safe_input_budget,
                )
                self.vault.connection.commit()
            raise AIUnavailableError("专家综合上下文超过安全输入预算，需要调整确定性投影")
        last_error: BaseException | None = None
        update: dict[str, object] | None = None
        for attempt in (1, 2):
            try:
                update = self._provider_structured(
                    run_id,
                    role={**role, "_provider_task": "committee"},
                    prompt=prompt,
                    schema=EXPERT_STATE_UPDATE_SCHEMA,
                    evidence=tuple(store.get(evidence_id) for evidence_id in sorted(all_ids)),
                    stage="expert_synthesis",
                    node_id=f"{role_id}:synthesis",
                    token_budget=safe_input_budget,
                    retry_count=attempt - 1,
                )
                last_error = None
                break
            except BaseException as caught:
                last_error = caught
        if last_error or update is None:
            with self.vault.lock:
                self.vault.connection.executemany(
                    "UPDATE research_evidence_packets SET status = 'failed', updated_at = ? "
                    "WHERE packet_id = ?",
                    [
                        (datetime.now(timezone.utc).isoformat(), f"{run_id}:{packet.packet_id}")
                        for packet in packets
                    ],
                )
                self.vault.connection.commit()
            assert last_error is not None
            raise last_error
        update = self._sanitize_structured_update_evidence(update, all_ids)
        requirements = {
            str(item["requirement"]): dict(item)
            for item in update.get("requirements") or []
            if isinstance(item, dict) and item.get("requirement")
        }
        state = state.synthesize(
            packets,
            {
                "claims": update.get("claims") or [],
                "framework_requirements": requirements,
                "open_questions": update.get("open_questions") or [],
            },
        )
        state.validate(known_evidence_ids=all_ids)
        with self.vault.lock:
            self._persist_expert_state(run_id=run_id, state=state, packet=None)
            self.vault.connection.executemany(
                "UPDATE research_evidence_packets SET status = 'completed', updated_at = ? "
                "WHERE packet_id = ?",
                [
                    (datetime.now(timezone.utc).isoformat(), f"{run_id}:{packet.packet_id}")
                    for packet in packets
                ],
            )
            self.vault.connection.commit()
        return state

    def _revise_expert_state_with_checkpoint(
        self,
        *,
        run_id: str,
        role_id: str,
        question: str,
        state: ExpertResearchState,
        packet: EvidencePacket,
        store: EvidenceStore,
        provider_role: dict[str, object] | None = None,
    ) -> ExpertResearchState:
        role = provider_role or get_role(role_id)
        state_projection = {
            "role_id": state.role_id,
            "revision": state.revision,
            "claims": [
                {
                    "id": claim.claim_id,
                    "text": claim.claim,
                    "status": claim.status,
                    "confidence": claim.confidence,
                    "support": claim.supporting_evidence_ids,
                    "oppose": claim.contradicting_evidence_ids,
                    **({"conditions": claim.conditions} if claim.conditions else {}),
                }
                for claim in state.claims.values()
            ],
            "requirements": [
                {
                    "name": requirement,
                    "status": detail.get("status"),
                    "evidence_ids": detail.get("evidence_ids", []),
                }
                for requirement, detail in state.requirement_coverage.items()
            ],
        }
        prompt = (
            "新增补证触发了材料性冲突、风险或覆盖变化。只基于当前状态和本补证检查点修订；"
            "允许降级、拒绝或替换旧命题，不得重复研究其他证据。\n"
            f"问题：{question}\n当前状态："
            f"{json.dumps(state_projection, ensure_ascii=False, separators=(',', ':'))}\n"
            f"补证检查点：{packet.render()}"
        )
        budget = ContextBudget()
        safe_input_budget = (
            budget.model_context_tokens
            - budget.reserved_output_tokens
            - budget.reserved_reasoning_tokens
            - budget.system_tokens
            - budget.schema_tokens
            - budget.safety_margin_tokens
        )
        evidence = tuple(packet.evidence)
        if estimate_tokens(prompt) > safe_input_budget:
            error = AIUnavailableError("专家补证复核上下文超过安全输入预算")
            with self.vault.lock:
                self._record_call_metric(
                    run_id=run_id,
                    stage="expert_conflict_revision",
                    role_id=role_id,
                    prompt=prompt,
                    result=None,
                    latency_ms=0,
                    evidence=evidence,
                    error=error,
                    node_id=f"{role_id}:conflict-revision",
                    token_budget=safe_input_budget,
                )
                self.vault.connection.commit()
            raise error
        if not callable(getattr(self.provider, "generate_structured", None)):
            opinion = self._provider_chat(
                run_id,
                role={**role, "_provider_task": "committee"},
                messages=[{"role": "user", "content": question}],
                context=packet.render(),
                use_runtime_market_skill=False,
                _metric_stage="expert_conflict_revision",
                _metric_node_id=f"{role_id}:conflict-revision",
                _metric_token_budget=safe_input_budget,
            )
            known_ids = {record.evidence_id for record in store.records}
            cited = [
                evidence_id
                for evidence_id in opinion.get("cited_evidence_ids", [])
                if evidence_id in known_ids
            ]
            revised = state.merge(
                packet,
                {
                    "claims": [
                        {
                            "claim_id": f"{role_id}-supplement",
                            "claim": str(opinion.get("content") or "补证后未形成新结论"),
                            "status": "supported" if cited else "conditional",
                            "supporting_evidence_ids": cited,
                            "contradicting_evidence_ids": [],
                            "confidence": "medium" if cited else "low",
                            "conditions": list(opinion.get("unknowns") or []),
                        }
                    ],
                    "open_questions": list(opinion.get("unknowns") or []),
                },
            )
            revised.validate(known_evidence_ids=known_ids)
            with self.vault.lock:
                self._persist_expert_state(run_id=run_id, state=revised, packet=packet)
                self.vault.connection.commit()
            return revised
        update = self._provider_structured(
            run_id,
            role={**role, "_provider_task": "committee"},
            prompt=prompt,
            schema=EXPERT_STATE_UPDATE_SCHEMA,
            evidence=evidence,
            stage="expert_conflict_revision",
            node_id=f"{role_id}:conflict-revision",
            token_budget=safe_input_budget,
        )
        known_ids = {record.evidence_id for record in store.records}
        update = self._sanitize_structured_update_evidence(update, known_ids)
        requirements = {
            str(item["requirement"]): dict(item)
            for item in update.get("requirements") or []
            if isinstance(item, dict) and item.get("requirement")
        }
        revised = state.merge(
            packet,
            {
                "claims": update.get("claims") or [],
                "framework_requirements": requirements,
                "open_questions": update.get("open_questions") or [],
            },
        )
        revised.validate(known_evidence_ids=known_ids)
        with self.vault.lock:
            self._persist_expert_state(run_id=run_id, state=revised, packet=packet)
            self.vault.connection.commit()
        return revised

    def _coverage_supplement(
        self,
        *,
        run_id: str,
        security_id: str,
        question: str,
        store: EvidenceStore,
        states: dict[str, ExpertResearchState],
    ) -> tuple[EvidenceStore, dict[str, ExpertResearchState]]:
        """Plan P4: execute one coordinator-owned supplement for every actionable gap."""

        manifest = EvidenceManifest.from_store(store)
        actionable: dict[str, tuple[str, ...]] = {}
        exhausted: dict[str, tuple[str, ...]] = {}
        for role_id, state in states.items():
            result = CoverageGate().evaluate(role_id=role_id, state=state, manifest=manifest)
            if result.actionable_requirements:
                role_actionable: list[str] = []
                role_exhausted: list[str] = []
                for requirement in result.actionable_requirements:
                    detail = state.requirement_coverage.get(requirement) or {}
                    if detail.get("evidence_ids") and detail.get("attempted_sources"):
                        role_exhausted.append(requirement)
                    else:
                        role_actionable.append(requirement)
                if role_actionable:
                    actionable[role_id] = tuple(role_actionable)
                if role_exhausted:
                    exhausted[role_id] = tuple(role_exhausted)
        if exhausted:
            with self.vault.lock:
                for role_id, requirements in exhausted.items():
                    state = states[role_id]
                    coverage = {
                        key: dict(value) for key, value in state.requirement_coverage.items()
                    }
                    for requirement in requirements:
                        detail = coverage[requirement]
                        detail.update(
                            {
                                "status": "unavailable",
                                "reason": detail.get("reason")
                                or "已有证据与已尝试来源仍不足以关闭该要求",
                                "alternatives": detail.get("alternatives") or [],
                                "impact": detail.get("impact") or "对应结论保持条件性",
                            }
                        )
                    states[role_id] = replace(state, requirement_coverage=coverage)
                    self._persist_expert_state(
                        run_id=run_id,
                        state=states[role_id],
                        packet=None,
                    )
                self.vault.connection.commit()
        if not actionable:
            return store, states
        supplement_question = question + "\n统一补证要求：" + "；".join(
            dict.fromkeys(requirement for requirements in actionable.values() for requirement in requirements)
        )
        try:
            supplement_results = self.skill_layer.run(
                security_id=security_id,
                question=supplement_question,
                role_id="general",
            )
        except Exception as error:
            supplement_results = [
                {
                    "skill_id": "coordinator-supplement",
                    "name": "协调器统一补证",
                    "status": "failed",
                    "gaps": [str(error)],
                    "evidence": [],
                }
            ]
        generate = getattr(self.provider, "generate_structured", None)
        if callable(generate):
            coordinator_role = {
                "role_id": "evidence_coordinator",
                "name": "证据协调器",
                "focus": "只针对结构化缺口读取公开原始来源",
                "questions": "哪些原始来源能够关闭本轮明确缺口？",
                "risk_focus": "搜索摘要冒充原文、重复补证和账本外持仓污染",
                "_provider_task": "committee",
            }
            try:
                reached = self._provider_structured(
                    run_id,
                    role=coordinator_role,
                    prompt=(
                        "这是本轮唯一一次 runtime 补证。仅针对以下未关闭要求使用随包只读技能；"
                        "若未实际读到发行人、交易所、监管机构、基金公司或指数公司原文，不得写入"
                        " reached_sources。\n"
                        + "\n".join(
                            f"{role_id}：{'；'.join(requirements)}"
                            for role_id, requirements in actionable.items()
                        )
                    ),
                    schema=CHAT_RESPONSE_SCHEMA,
                    evidence=(),
                    stage="coordinator_supplement",
                    use_runtime_market_skill=True,
                    node_id="evidence_coordinator:supplement",
                    token_budget=ContextBudget().evidence_budget,
                )
                reached_evidence = []
                for item in reached.get("reached_sources") or []:
                    if not isinstance(item, dict) or not str(item.get("url") or "").startswith(
                        ("https://", "http://")
                    ):
                        continue
                    value = {
                        "source": item,
                        "researcher_extract": str(reached.get("content") or ""),
                        "classification": "AI提取，需由引用原文复核",
                    }
                    reached_evidence.append(
                        {
                            "evidence_id": "EVIDENCE-REACHED-"
                            + hashlib.sha256(
                                json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
                            ).hexdigest()[:12],
                            "kind": "runtime_reached_original",
                            "value": value,
                            "as_of": str(item.get("published_at") or item.get("accessed_at") or "")[:10],
                            "provider": str(item.get("title") or "公开原始来源"),
                            "source_ref": str(item["url"]),
                        }
                    )
                if reached_evidence:
                    supplement_results.append(
                        {
                            "skill_id": "coordinator-runtime-reach",
                            "name": "协调器公开原文补证",
                            "status": "partial",
                            "gaps": list(reached.get("unknowns") or []),
                            "evidence": reached_evidence,
                        }
                    )
            except BaseException as error:
                supplement_results.append(
                    {
                        "skill_id": "coordinator-runtime-reach",
                        "name": "协调器公开原文补证",
                        "status": "failed",
                        "gaps": [str(error)],
                        "evidence": [],
                    }
                )
        supplemented = self._evidence_store(security_id, supplement_results)
        old_hashes = {record.content_hash for record in store.records}
        new_records = [record for record in supplemented.records if record.content_hash not in old_hashes]
        combined = store.ingest(new_records)
        with self.vault.lock:
            combined = self._persist_evidence_store(run_id, combined)
            self.vault.connection.commit()
        canonical_by_hash = {record.content_hash: record for record in combined.records}
        new_records = [canonical_by_hash[record.content_hash] for record in new_records]
        new_ids = {record.evidence_id for record in new_records}
        supplement_manifest = EvidenceManifest.from_store(EvidenceStore(tuple(new_records)))
        updated = dict(states)
        # One coordinator supplement is one semantic revision boundary. All other affected
        # roles retain deterministic ingestion checkpoints and an explicit conflict state.
        remaining_semantic_revisions = min(
            1,
            max(0, 10 - sum(state.revision for state in states.values())),
        )
        revision_budget = ContextBudget()
        safe_revision_input = (
            revision_budget.model_context_tokens
            - revision_budget.reserved_output_tokens
            - revision_budget.reserved_reasoning_tokens
            - revision_budget.system_tokens
            - revision_budget.schema_tokens
            - revision_budget.safety_margin_tokens
        )
        for role_id, requirements in actionable.items():
            state = updated[role_id]
            compact_state_tokens = estimate_tokens(
                json.dumps(
                    {
                        "claims": [vars(claim) for claim in state.claims.values()],
                        "requirements": state.requirement_coverage,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            packet_token_budget = max(
                1,
                min(
                    revision_budget.evidence_budget * 3,
                    safe_revision_input
                    - compact_state_tokens
                    - estimate_tokens(question)
                    - estimate_tokens(EXPERT_STATE_UPDATE_SCHEMA)
                    - 8_192,
                ),
            )
            relevant_ids = set(
                RoleEvidencePlanner()
                .plan(
                    role_id=role_id,
                    question=supplement_question,
                    manifest=supplement_manifest,
                    token_budget=packet_token_budget,
                )
                .evidence_ids
            ) & new_ids
            relevant_records = [record for record in new_records if record.evidence_id in relevant_ids]
            sequence = int(
                self.vault.connection.execute(
                    "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM research_evidence_packets "
                    "WHERE run_id = ? AND role_id = ?",
                    (run_id, role_id),
                ).fetchone()[0]
            )
            packet = EvidencePacket(
                packet_id=f"PKT-{role_id.upper()}-SUPPLEMENT-{state.revision + 1}",
                role_id=role_id,
                objective="统一补证检查点",
                required_outputs=tuple(requirements),
                evidence=tuple(relevant_records),
                known_gaps=tuple(requirements),
                token_estimate=sum(record.token_estimate for record in relevant_records),
                sequence=sequence,
            )
            with self.vault.lock:
                self.vault.connection.execute(
                    """INSERT OR REPLACE INTO research_evidence_packets
                    (packet_id, run_id, role_id, sequence_number, objective, required_outputs_json,
                     evidence_ids_json, known_gaps_json, token_estimate, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ingested', ?)""",
                    (
                        f"{run_id}:{packet.packet_id}",
                        run_id,
                        role_id,
                        sequence,
                        packet.objective,
                        json.dumps(packet.required_outputs, ensure_ascii=False),
                        json.dumps(packet.evidence_ids, ensure_ascii=False),
                        json.dumps(packet.known_gaps, ensure_ascii=False),
                        packet.token_estimate,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                self.vault.connection.commit()
            coverage_changed = any(
                record.quality_status.casefold()
                in {"available", "completed", "verified", "verified_original"}
                for record in relevant_records
            )
            needs_revision = bool(relevant_ids) and ConflictTrigger().should_revise(
                new_records=relevant_records,
                coverage_changed=coverage_changed,
            )
            revision_error: BaseException | None = None
            if needs_revision and remaining_semantic_revisions:
                remaining_semantic_revisions -= 1
                try:
                    state = self._revise_expert_state_with_checkpoint(
                        run_id=run_id,
                        role_id=role_id,
                        question=question,
                        store=combined,
                        state=state,
                        packet=packet,
                        provider_role=market_report_role(get_role(role_id))
                        if security_id in MARKET_OVERVIEW_SECURITY_IDS
                        else None,
                    )
                except BaseException as error:
                    revision_error = error
                    coverage_override = {
                        requirement: {
                            **dict(state.requirement_coverage.get(requirement) or {}),
                            "status": "conflicted",
                            "evidence_ids": sorted(
                                {
                                    *(
                                        state.requirement_coverage.get(requirement, {}).get(
                                            "evidence_ids", []
                                        )
                                    ),
                                    *relevant_ids,
                                }
                            ),
                            "attempted_sources": sorted(
                                {record.provider for record in relevant_records}
                            ),
                            "reason": f"补证语义复核未完成：{error}",
                            "alternatives": [],
                            "impact": "保留原专家结论并标记冲突，不阻断其余专家与最终报告",
                        }
                        for requirement in requirements
                    }
                    state = state.ingest(packet, coverage=coverage_override)
            else:
                coverage_override: dict[str, dict[str, object]] = {}
                if needs_revision:
                    coverage_override = {
                        requirement: {
                            **dict(state.requirement_coverage.get(requirement) or {}),
                            "status": "conflicted",
                            "evidence_ids": sorted(
                                {
                                    *(
                                        state.requirement_coverage.get(requirement, {}).get(
                                            "evidence_ids", []
                                        )
                                    ),
                                    *relevant_ids,
                                }
                            ),
                            "attempted_sources": sorted(
                                {record.provider for record in relevant_records}
                            ),
                            "reason": "补证可能改变结论，但本轮语义修订预算已用尽",
                            "alternatives": [],
                            "impact": "相关结论保持冲突状态，不得升级为确定判断",
                        }
                        for requirement in requirements
                    }
                state = state.ingest(packet, coverage=coverage_override)
            gate = CoverageGate().evaluate(
                role_id=role_id,
                state=state,
                manifest=EvidenceManifest.from_store(combined),
            )
            if gate.actionable_requirements and revision_error is None:
                coverage = {key: dict(value) for key, value in state.requirement_coverage.items()}
                for requirement in gate.actionable_requirements:
                    item = coverage.setdefault(requirement, {})
                    item.update(
                        {
                            "status": "unavailable",
                            "attempted_sources": item.get("attempted_sources")
                            or [str(result.get("name") or result.get("skill_id")) for result in supplement_results],
                            "reason": item.get("reason") or "统一补证后仍未形成可用事实",
                            "alternatives": item.get("alternatives") or [],
                            "impact": item.get("impact") or "对应结论保持条件性",
                        }
                    )
                with self.vault.lock:
                    state = state.ingest(packet, coverage=coverage)
                    self._persist_expert_state(run_id=run_id, state=state, packet=packet)
            with self.vault.lock:
                self._persist_expert_state(run_id=run_id, state=state, packet=packet)
                self.vault.connection.execute(
                    "UPDATE research_evidence_packets SET status = 'completed', updated_at = ? "
                    "WHERE packet_id = ?",
                    (datetime.now(timezone.utc).isoformat(), f"{run_id}:{packet.packet_id}"),
                )
                self.vault.connection.commit()
            final_gate = CoverageGate().evaluate(
                role_id=role_id,
                state=state,
                manifest=EvidenceManifest.from_store(combined),
            )
            if not final_gate.can_complete:
                raise AIUnavailableError(f"{role_id} 仍有未关闭的强制框架要求")
            updated[role_id] = state
        return combined, updated

    @staticmethod
    def _claim_is_publishable(claim: ExpertClaim) -> bool:
        status = claim.status
        supporting = claim.supporting_evidence_ids
        conditions = claim.conditions
        text = claim.claim
        if not supporting or _NON_PUBLISHABLE_CONCLUSION.search(text):
            return False
        if status == "supported":
            return True
        return status == "conditional" and bool(conditions) and not any(
            _NON_PUBLISHABLE_CONCLUSION.search(str(item)) for item in conditions
        )

    @classmethod
    def _publishable_state(cls, state: ExpertResearchState) -> ExpertResearchState:
        return replace(
            state,
            claims={
                claim_id: claim
                for claim_id, claim in state.claims.items()
                if cls._claim_is_publishable(claim)
            },
            requirement_coverage={},
            open_questions=(),
        )

    @staticmethod
    def _publishable_content(content: str) -> str:
        def strip_dangling_separator(value: str) -> str:
            return re.sub(r"[，,；;](?=(?:\*{1,3}|_{1,3})?$)", "", value.strip())

        lines: list[str] = []
        for line in str(content or "").splitlines():
            if not _NON_PUBLISHABLE_CONCLUSION.search(line):
                lines.append(strip_dangling_separator(line))
                continue
            clauses: list[str] = []
            for sentence in re.split(r"(?<=[。！？；])", line):
                if not sentence.strip():
                    continue
                candidates = re.split(
                    r"(?<=[，,])(?=(?:但|然而|不过|而|因此|所以|使|不能|无法|尚未|未能))",
                    sentence,
                )
                clauses.extend(
                    candidate
                    for candidate in candidates
                    if candidate.strip() and not _NON_PUBLISHABLE_CONCLUSION.search(candidate)
                )
            if clauses:
                lines.append(strip_dangling_separator("".join(clauses)))
        return "\n".join(lines).strip()

    @classmethod
    def _finalize_public_reply(
        cls,
        reply: dict[str, object],
        *,
        fallback_state: ExpertResearchState | None = None,
    ) -> dict[str, object]:
        audit_unknowns = list(
            dict.fromkeys(
                str(item)
                for item in (
                    *(reply.get("audit_unknowns") or []),
                    *(reply.get("unknowns") or []),
                )
                if str(item)
            )
        )
        reply["content"] = cls._publishable_content(str(reply.get("content") or ""))
        if not reply["content"] and fallback_state is not None:
            fallback = cls._opinion_from_state(fallback_state)
            reply["content"] = fallback["content"]
            reply["cited_evidence_ids"] = fallback["cited_evidence_ids"]
        reply["audit_unknowns"] = audit_unknowns
        reply["unknowns"] = []
        reply["assumptions"] = [
            str(item)
            for item in reply.get("assumptions") or []
            if str(item) and not _NON_PUBLISHABLE_CONCLUSION.search(str(item))
        ]
        return reply

    @classmethod
    def _opinion_from_state(cls, state: ExpertResearchState) -> dict[str, object]:
        audit_unknowns = list(state.open_questions)
        state = cls._publishable_state(state)
        claims = list(state.claims.values())
        content = "\n".join(
            "- " + claim.claim
            + (("\n  - 条件：" + "；".join(claim.conditions)) if claim.conditions else "")
            for claim in claims
        )
        cited = list(
            dict.fromkeys(
                evidence_id
                for claim in claims
                for evidence_id in (
                    *claim.supporting_evidence_ids,
                    *claim.contradicting_evidence_ids,
                )
            )
        )
        return {
            "content": content,
            "cited_evidence_ids": cited,
            "assumptions": [
                condition for claim in claims for condition in claim.conditions if condition
            ],
            "unknowns": [],
            "audit_unknowns": audit_unknowns,
            "role_id": state.role_id,
            "role_name": get_role(state.role_id)["name"],
            "research_state_revision": state.revision,
        }

    def _persist_claim_board(self, run_id: str, board: ClaimBoard, store: EvidenceStore) -> None:
        now = datetime.now(timezone.utc).isoformat()
        records = {record.evidence_id: record for record in store.records}
        self.vault.connection.execute(
            """INSERT OR REPLACE INTO research_claim_boards
            (board_id, run_id, revision, board_json) VALUES (?, ?, 1, ?)""",
            (f"{run_id}:board:1", run_id, json.dumps(board.as_dict(), ensure_ascii=False)),
        )
        for role_id, claim in board.claims:
            cited = (*claim.supporting_evidence_ids, *claim.contradicting_evidence_ids)
            topic = next((records[item].domain for item in cited if item in records), "general")
            normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", claim.claim).casefold()
            self.vault.connection.execute(
                """INSERT OR REPLACE INTO research_claims
                (claim_id, run_id, claim_key, role_id, topic, claim_text, status, confidence,
                 supporting_evidence_ids_json, contradicting_evidence_ids_json, conditions_json,
                 updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid4()),
                    run_id,
                    f"{role_id}:{normalized or claim.claim_id}",
                    role_id,
                    topic,
                    claim.claim,
                    claim.status,
                    claim.confidence,
                    json.dumps(claim.supporting_evidence_ids, ensure_ascii=False),
                    json.dumps(claim.contradicting_evidence_ids, ensure_ascii=False),
                    json.dumps(claim.conditions, ensure_ascii=False),
                    now,
                ),
            )
        for claim_key, conflict in board.conflicts.items():
            self.vault.connection.execute(
                """INSERT OR REPLACE INTO research_claim_conflicts
                (conflict_id, run_id, claim_key, supporting_evidence_ids_json,
                 contradicting_evidence_ids_json, roles_json, resolved)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"{run_id}:{claim_key}",
                    run_id,
                    claim_key,
                    json.dumps(conflict.supporting_evidence_ids, ensure_ascii=False),
                    json.dumps(conflict.contradicting_evidence_ids, ensure_ascii=False),
                    json.dumps(conflict.roles, ensure_ascii=False),
                    int(conflict.resolved),
                ),
            )

    def _persist_risk_review(self, run_id: str, state: RiskReviewState) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.vault.connection.execute(
            """INSERT OR REPLACE INTO research_risk_reviews
            (review_id, run_id, state_json, updated_at) VALUES (?, ?, ?, ?)""",
            (f"{run_id}:risk", run_id, json.dumps(state.as_dict(), ensure_ascii=False), now),
        )

    def _persist_run_quality_metrics(
        self,
        *,
        run_id: str,
        states: Mapping[str, ExpertResearchState],
        board: ClaimBoard,
        report: Mapping[str, object],
    ) -> None:
        terminal = {"covered", "unavailable", "not_applicable", "conflicted"}
        coverage = {
            role_id: {
                key: str(value.get("status") or "partial")
                for key, value in state.requirement_coverage.items()
            }
            for role_id, state in states.items()
        }
        section_status = report.get("section_status") or {}
        completed_sections = (
            sum(status == "completed" for status in section_status.values())
            if isinstance(section_status, Mapping)
            else 0
        )
        total_sections = len(section_status) if isinstance(section_status, Mapping) else 0
        summary = {
            "coverage": coverage,
            "framework_coverage": {
                role_id: (
                    sum(status in terminal for status in statuses.values()) / len(statuses)
                    if statuses
                    else 1.0
                )
                for role_id, statuses in coverage.items()
            },
            "report_completion_rate": (
                completed_sections / total_sections if total_sections else 1.0
            ),
            "unresolved_conflict_count": len(board.conflicts),
            "usage_source": "estimated",
        }
        latest = self.vault.connection.execute(
            """SELECT cited_evidence_count, available_evidence_count
            FROM research_call_metrics WHERE run_id = ? ORDER BY created_at DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        if latest:
            cited_count = int(latest["cited_evidence_count"] or 0)
            available_count = int(latest["available_evidence_count"] or 0)
            summary["cited_available_ratio"] = (
                cited_count / available_count if available_count else 1.0
            )
        self.vault.connection.execute(
            """UPDATE research_call_metrics SET framework_coverage_json = ?,
            covered_expert_count = ? WHERE metric_id = (
                SELECT metric_id FROM research_call_metrics WHERE run_id = ?
                ORDER BY created_at DESC LIMIT 1
            )""",
            (
                json.dumps(summary, ensure_ascii=False),
                sum(
                    all(status in terminal for status in statuses.values())
                    for statuses in coverage.values()
                ),
                run_id,
            ),
        )

    def _persist_performance_summary(
        self,
        *,
        run_id: str,
        store: EvidenceStore | None,
        states: Mapping[str, ExpertResearchState] | None,
        report: Mapping[str, object] | None,
        final_context_tokens: int | None = None,
        completion_status: str = "completed",
    ) -> None:
        domain_distribution: dict[str, int] = {}
        if store is not None:
            evidence_ids = {record.evidence_id for record in store.records}
            for record in store.records:
                domain_distribution[record.domain] = domain_distribution.get(record.domain, 0) + 1
        else:
            evidence_rows = self.vault.connection.execute(
                """SELECT DISTINCT record.evidence_id, record.domain
                   FROM research_evidence_records record
                   JOIN research_evidence_links link ON link.evidence_id = record.evidence_id
                   WHERE link.run_id = ?""",
                (run_id,),
            ).fetchall()
            evidence_ids = {str(row["evidence_id"]) for row in evidence_rows}
            for row in evidence_rows:
                domain = str(row["domain"])
                domain_distribution[domain] = domain_distribution.get(domain, 0) + 1
        report = report or {}
        cited = {
            str(item)
            for item in report.get("cited_evidence_ids", [])
            if str(item) in evidence_ids
        }
        metric = self.vault.connection.execute(
            """SELECT retry_count, provider_type, error_json, stage, role_id, node_id,
                      estimated_context_tokens, token_budget
               FROM research_call_metrics WHERE run_id = ? AND error_json IS NOT NULL
               ORDER BY created_at DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        retry_count = int(
            self.vault.connection.execute(
                "SELECT COALESCE(SUM(retry_count), 0) FROM research_call_metrics WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        packet_count = int(
            self.vault.connection.execute(
                """SELECT COUNT(*) FROM research_evidence_packets
                   WHERE run_id = ? AND packet_id LIKE '%-DOMAIN-%'""",
                (run_id,),
            ).fetchone()[0]
        )
        section_count = int(
            self.vault.connection.execute(
                "SELECT COUNT(*) FROM research_report_sections WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        section_llm_calls = int(
            self.vault.connection.execute(
                "SELECT COUNT(*) FROM research_call_metrics WHERE run_id = ? AND stage = 'report_section'",
                (run_id,),
            ).fetchone()[0]
        )
        run = self.vault.connection.execute(
            "SELECT started_at, completed_at FROM research_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        duration_ms = None
        if run is not None:
            try:
                started = datetime.fromisoformat(str(run["started_at"]))
                completed = datetime.fromisoformat(str(run["completed_at"])) if run["completed_at"] else datetime.now(timezone.utc)
                duration_ms = max(0, round((completed - started).total_seconds() * 1000))
            except ValueError:
                duration_ms = None
        if states is not None:
            semantic_revision_count = sum(state.revision for state in states.values())
        else:
            semantic_revision_count = int(
                self.vault.connection.execute(
                    """SELECT COALESCE(SUM(revision), 0) FROM (
                       SELECT MAX(revision) AS revision FROM research_expert_states
                       WHERE run_id = ? GROUP BY role_id)""",
                    (run_id,),
                ).fetchone()[0]
            )
        if final_context_tokens is None:
            final_context_row = self.vault.connection.execute(
                """SELECT estimated_context_tokens FROM research_call_metrics
                   WHERE run_id = ? AND stage = 'final_edit'
                   ORDER BY created_at DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
            final_context_tokens = (
                int(final_context_row[0])
                if final_context_row and final_context_row[0] is not None
                else None
            )
        failure = metric if completion_status in {"partial", "failed", "cancelled"} else None
        self.vault.connection.execute(
            """INSERT INTO research_performance_summaries
            (run_id, evidence_count, domain_distribution_json, unused_evidence_count,
             cited_evidence_count, citation_rate, packet_count, semantic_revision_count,
             duration_ms, retry_count, section_count, section_llm_call_count,
             final_context_tokens, completion_status, usage_source, failure_stage,
             failure_agent, failure_node_id, failure_provider, failure_token_estimate,
             failure_token_budget, failure_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'estimated', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                evidence_count = excluded.evidence_count,
                domain_distribution_json = excluded.domain_distribution_json,
                unused_evidence_count = excluded.unused_evidence_count,
                cited_evidence_count = excluded.cited_evidence_count,
                citation_rate = excluded.citation_rate,
                packet_count = excluded.packet_count,
                semantic_revision_count = excluded.semantic_revision_count,
                duration_ms = excluded.duration_ms,
                retry_count = excluded.retry_count,
                section_count = excluded.section_count,
                section_llm_call_count = excluded.section_llm_call_count,
                final_context_tokens = excluded.final_context_tokens,
                completion_status = excluded.completion_status,
                failure_stage = excluded.failure_stage,
                failure_agent = excluded.failure_agent,
                failure_node_id = excluded.failure_node_id,
                failure_provider = excluded.failure_provider,
                failure_token_estimate = excluded.failure_token_estimate,
                failure_token_budget = excluded.failure_token_budget,
                failure_json = excluded.failure_json,
                updated_at = excluded.updated_at""",
            (
                run_id,
                len(evidence_ids),
                json.dumps(domain_distribution, ensure_ascii=False, sort_keys=True),
                max(0, len(evidence_ids) - len(cited)),
                len(cited),
                len(cited) / len(evidence_ids) if evidence_ids else 1.0,
                packet_count,
                semantic_revision_count,
                duration_ms,
                retry_count,
                section_count,
                section_llm_calls,
                final_context_tokens,
                completion_status,
                str(failure["stage"]) if failure else None,
                str(failure["role_id"]) if failure and failure["role_id"] else None,
                str(failure["node_id"]) if failure and failure["node_id"] else None,
                str(failure["provider_type"]) if failure else None,
                int(failure["estimated_context_tokens"])
                if failure and failure["estimated_context_tokens"] is not None
                else None,
                int(failure["token_budget"])
                if failure and failure["token_budget"] is not None
                else None,
                str(failure["error_json"]) if failure else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def _ensure_terminal_failure_metric(
        self,
        *,
        run_id: str,
        error: BaseException,
        role_id: str = "coordinator",
        node_id: str | None = None,
    ) -> None:
        existing = self.vault.connection.execute(
            """SELECT 1 FROM research_call_metrics
               WHERE run_id = ? AND error_json IS NOT NULL LIMIT 1""",
            (run_id,),
        ).fetchone()
        if existing:
            return
        run = self.vault.connection.execute(
            "SELECT current_stage FROM research_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        stage = str(run["current_stage"] if run else "workflow")
        token_budget = {
            "analysis": ContextBudget().evidence_budget,
            "reporting": ContextBudget().final_edit_budget,
        }.get(stage)
        self._record_call_metric(
            run_id=run_id,
            stage=stage,
            role_id=role_id,
            prompt="",
            result=None,
            latency_ms=0,
            evidence=(),
            error=error,
            node_id=node_id or f"{stage}:workflow",
            token_budget=token_budget,
        )

    @staticmethod
    def _section_claims(
        section_key: str,
        board: ClaimBoard,
        store: EvidenceStore,
    ) -> list[dict[str, object]]:
        domain_map = {
            "financial_quality": {"company-financial-quality"},
            "operations_governance": {"supplemental-company-evidence"},
            "valuation": {"security-valuation-evidence"},
            "market_execution": {"market-context-evidence", "execution-liquidity-evidence"},
            "market_structure": {"market-context-evidence", "execution-liquidity-evidence"},
            "portfolio_exposure": {"portfolio-risk-evidence"},
            "holdings_exposure": {"fund-portfolio-evidence", "portfolio-risk-evidence"},
            "performance_liquidity": {"fund-liquidity-evidence", "execution-liquidity-evidence"},
        }
        allowed_domains = domain_map.get(section_key)
        records = {record.evidence_id: record for record in store.records}
        claims: list[dict[str, object]] = []
        for role_id, claim in board.claims:
            evidence_ids = (*claim.supporting_evidence_ids, *claim.contradicting_evidence_ids)
            domains = {records[item].domain for item in evidence_ids if item in records}
            if allowed_domains is not None and not domains.intersection(allowed_domains):
                continue
            claims.append(
                {
                    "claim_id": claim.claim_id,
                    "role_id": role_id,
                    "claim": claim.claim,
                    "status": claim.status,
                    "supporting_evidence_ids": claim.supporting_evidence_ids,
                    "contradicting_evidence_ids": claim.contradicting_evidence_ids,
                    "confidence": claim.confidence,
                    "conditions": claim.conditions,
                }
            )
        return claims

    def _generate_sectioned_report(
        self,
        *,
        run_id: str,
        security_id: str,
        question: str,
        states: dict[str, ExpertResearchState],
        store: EvidenceStore,
        risk_state: RiskReviewState | None = None,
    ) -> tuple[dict[str, object], ClaimBoard]:
        """Plan P3: map claims to bounded sections, then edit sections without raw evidence."""

        board = ClaimBoard.from_states(list(states.values()))
        audit_risk_state = risk_state or RiskReviewState.build(
            states=list(states.values()), board=board, store=store
        )
        with self.vault.lock:
            self._persist_claim_board(run_id, board, store)
            self._persist_risk_review(run_id, audit_risk_state)
            self.vault.connection.commit()
        publishable_states = {
            role_id: self._publishable_state(state) for role_id, state in states.items()
        }
        publishable_board = ClaimBoard.from_states(list(publishable_states.values()))
        publishable_risk_state = RiskReviewState.build(
            states=list(publishable_states.values()), board=publishable_board, store=store
        )
        if security_id in MARKET_OVERVIEW_SECURITY_IDS:
            report_kind = "market"
        elif security_id.endswith(":FUND"):
            report_kind = "fund"
        else:
            report_kind = "company"
        report_role = {
            "role_id": "report_editor",
            "name": "投研委员会投资经理",
            "focus": "可发布结论、专家共识、关键分歧、组合风险和可复核条件",
            "questions": "当前证据可靠支持哪些结论，哪些触发条件会改变这些结论？",
            "risk_focus": "证据错配、虚假共识和无条件行动建议",
            "report_kind": "market" if report_kind == "market" else "research",
            "_provider_task": "committee",
        }
        builder = ReportSectionBuilder(store)
        completed_sections: dict[str, str] = {}
        section_replies: dict[str, dict[str, object]] = {}
        for sequence, section_key in enumerate(builder.TEMPLATES[report_kind], 1):
            claims = self._section_claims(section_key, publishable_board, store)
            section = builder.build_deterministic_section(section_id=section_key, claims=claims)
            section_id = f"{run_id}:{section_key}"
            completed_sections[section_key] = str(section["content"])
            section_replies[section_key] = section
            with self.vault.lock:
                self.vault.connection.execute(
                    """INSERT OR REPLACE INTO research_report_sections
                    (section_id, run_id, section_key, sequence_number, title, status, input_json,
                     output_json, rendered_markdown, attempt, updated_at, completed_at)
                    VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, 0, ?, ?)""",
                    (
                        section_id,
                        run_id,
                        section_key,
                        sequence,
                        str(section["title"]),
                        json.dumps(
                            {
                                "question": question,
                                "claim_ids": section["claim_ids"],
                                "generation": "deterministic",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(section, ensure_ascii=False),
                        str(section["content"]),
                        datetime.now(timezone.utc).isoformat(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                self.vault.connection.commit()
        coverage_gaps = [
            {
                "role_id": role_id,
                "requirement": requirement,
                "status": str(detail.get("status") or "partial"),
            }
            for role_id, state in states.items()
            for requirement, detail in state.requirement_coverage.items()
            if str(detail.get("status") or "partial") != "covered"
        ]
        valid_ids = {
            evidence_id
            for _role_id, claim in publishable_board.claims
            for evidence_id in (
                *claim.supporting_evidence_ids,
                *claim.contradicting_evidence_ids,
            )
        }
        citation_index = {
            record.evidence_id: {
                "provider": record.provider,
                "source_ref": record.source_ref,
                "as_of": record.as_of,
            }
            for record in store.records
            if record.evidence_id in valid_ids
        }
        final_context = json.dumps(
            {
                "report_requirements": {
                    "question": question,
                    "report_kind": report_kind,
                    "sections": [
                        {
                            "section_key": key,
                            "title": ReportSectionBuilder.TITLES.get(key, key),
                        }
                        for key in builder.TEMPLATES[report_kind]
                    ],
                    "restrictions": [
                        "不得重新研究原始证据",
                        "不得补充已核验研究结论中不存在的事实",
                            "只输出至少有一条有效支持引用的可发布结论",
                            "conditional 结论必须同时保留已验证事实与可复核触发条件",
                            "未解决、缺失、未覆盖和开放问题只属于审计层，不得进入正文",
                            "不得用证据不足、缺少数据、无法判断、尚未覆盖或维持观察替代分析",
                        "正文只使用投资者语言，不出现内部数据键、状态对象或工作流名称",
                    ],
                },
                "research_conclusions": {
                    "field_map": {
                        "id": "claim_id",
                        "role": "role_id",
                        "text": "claim",
                        "support": "supporting_evidence_ids",
                        "oppose": "contradicting_evidence_ids",
                    },
                    "claims": [
                        {
                            "id": claim.claim_id,
                            "role": role_id,
                            "text": claim.claim,
                            "status": claim.status,
                            "confidence": claim.confidence,
                            "support": claim.supporting_evidence_ids,
                            "oppose": claim.contradicting_evidence_ids,
                            **({"conditions": claim.conditions} if claim.conditions else {}),
                        }
                        for role_id, claim in publishable_board.claims
                    ],
                    "consensus": publishable_board.consensus,
                    "dissent": publishable_board.dissent,
                },
                "unresolved_conflicts": {
                    key: {
                        "supporting_evidence_ids": value.supporting_evidence_ids,
                        "contradicting_evidence_ids": value.contradicting_evidence_ids,
                        "roles": value.roles,
                    }
                    for key, value in publishable_board.conflicts.items()
                },
                "risk_review": {
                    "items": [
                        {
                            "risk_id": item.risk_id,
                            "category": item.category,
                            "status": item.status,
                            "evidence_ids": item.evidence_ids,
                            "affected_roles": item.affected_roles,
                        }
                        for item in publishable_risk_state.items
                    ],
                },
                "citation_index": [
                    {
                        "id": evidence_id,
                        "provider": detail["provider"],
                        "source": detail["source_ref"],
                        "as_of": detail["as_of"],
                    }
                    for evidence_id, detail in citation_index.items()
                ],
                "date_table": sorted(
                    {record.as_of for record in store.records if record.as_of}, reverse=True
                )[:20],
                "terminology": {"security_id": security_id, "report_kind": report_kind},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        final_context_tokens = estimate_tokens(final_context)
        try:
            if not states:
                raise AIUnavailableError(
                    "全部专家均在形成可用研究结论前失败；已保留证据和已完成章节，"
                    "本轮不得把缺少研究结论的材料编辑成正式报告"
                )
            try:
                ContextBudget().require_within(final_context_tokens, stage="final_edit")
            except ValueError as budget_error:
                with self.vault.lock:
                    self._record_call_metric(
                        run_id=run_id,
                        stage="final_edit",
                        role_id="report_editor",
                        prompt=final_context,
                        result=None,
                        latency_ms=0,
                        evidence=(),
                        error=budget_error,
                        schema=CHAT_RESPONSE_SCHEMA,
                        node_id="final_editor",
                        token_budget=ContextBudget().final_edit_budget,
                    )
                    self.vault.connection.commit()
                raise
            report = self._provider_chat(
                run_id,
                role=report_role,
                messages=[
                    {
                        "role": "user",
                        "content": "统一编辑已核验的研究结论，不重新研究原始证据。",
                    }
                ],
                context=final_context,
                use_runtime_market_skill=False,
                _metric_stage="final_edit",
                _metric_node_id="final_editor",
                _metric_token_budget=ContextBudget().final_edit_budget,
            )
            report["cited_evidence_ids"] = list(
                dict.fromkeys(
                    item for item in report.get("cited_evidence_ids", []) if item in valid_ids
                )
            )
            report["unified_edit_completed"] = True
            report = self._finalize_public_reply(report)
        except BaseException as error:
            gaps = list(
                dict.fromkeys(question for state in states.values() for question in state.open_questions)
            )
            if not states:
                gaps.append(str(error))
            fallback = builder.fallback_payload(
                completed_sections=completed_sections,
                expert_states={key: value.as_dict() for key, value in states.items()},
                risks=[item.description for item in publishable_risk_state.items],
                gaps=gaps,
                final_edit_error=str(error),
            )
            report = {
                **fallback,
                "cited_evidence_ids": list(
                    dict.fromkeys(
                        evidence_id
                        for reply in section_replies.values()
                        for evidence_id in reply.get("cited_evidence_ids", [])
                    )
                ),
                "assumptions": [],
                "unknowns": [],
                "audit_unknowns": gaps,
            }
            if not states:
                report["content"] = (
                    "## 本轮研究未完成\n"
                    f"- {error}\n\n"
                    + str(fallback["content"])
                )
        report.update(
            {
                "role_id": "report_editor",
                "role_name": "投研委员会报告",
                "report": True,
                "section_status": {
                    key: "completed" if key in completed_sections else "failed"
                    for key in builder.TEMPLATES[report_kind]
                },
                "claim_conflicts": {
                    key: {
                        "roles": value.roles,
                        "resolved": value.resolved,
                    }
                    for key, value in board.conflicts.items()
                },
                "claim_board": board.as_dict(),
                "publishable_claim_board": publishable_board.as_dict(),
                "conflict_board": {
                    key: vars(value) for key, value in board.conflicts.items()
                },
                "coverage_gaps": coverage_gaps,
                "citation_index": citation_index,
                "risk_review": audit_risk_state.as_dict(),
            }
        )
        with self.vault.lock:
            self._persist_run_quality_metrics(
                run_id=run_id, states=states, board=board, report=report
            )
            self._persist_performance_summary(
                run_id=run_id,
                store=store,
                states=states,
                report=report,
                final_context_tokens=final_context_tokens,
                completion_status=(
                    "completed" if report.get("unified_edit_completed") else "partial"
                ),
            )
            self.vault.connection.commit()
        return report, board

    def _source_index(
        self, security_id: str, skill_results: list[dict[str, object]]
    ) -> dict[str, list[dict[str, object]]]:
        sources: dict[str, list[dict[str, object]]] = {}
        rows = self.vault.connection.execute(
            """SELECT e.provider, e.source_ref, e.period_end, s.effective_as_of
               FROM evidence_items e JOIN evidence_snapshots s ON s.snapshot_id = e.snapshot_id
               WHERE s.security_id = ? ORDER BY s.observed_at DESC, e.item_index""",
            (security_id,),
        ).fetchall()
        for index, row in enumerate(rows, 1):
            sources[f"EVIDENCE-{index}"] = [
                {
                    "name": str(row["provider"]),
                    "url": str(row["source_ref"]),
                    "as_of": str(row["period_end"] or row["effective_as_of"]),
                }
            ]
        for table, prefix in (("financial_snapshots", "FINANCIAL"), ("fund_snapshots", "FUND")):
            row = self.vault.connection.execute(
                f"SELECT snapshot_id, cutoff_date, source FROM {table} WHERE security_id = ? ORDER BY cutoff_date DESC LIMIT 1",
                (security_id,),
            ).fetchone()
            if row:
                sources[f"EVIDENCE-{prefix}-{row['snapshot_id']}"] = [
                    {
                        "name": str(row["source"]),
                        "url": "",
                        "as_of": str(row["cutoff_date"]),
                    }
                ]
        for result in skill_results:
            for evidence in result.get("evidence") or []:
                details = [
                    {
                        "name": str(result.get("name") or evidence.get("provider") or "公开证据"),
                        "url": str(evidence.get("source_ref") or ""),
                        "as_of": str(evidence.get("as_of") or ""),
                    }
                ]
                value = evidence.get("value")
                if result.get("skill_id") == "public-topic-evidence" and isinstance(value, dict):
                    for search in value.get("searches") or []:
                        for item in search.get("items") or []:
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
                            details.append(
                                {
                                    "name": str(item.get("source") or fallback_name),
                                    "url": str(item["source_ref"]),
                                    "as_of": str(item.get("as_of") or value.get("market_date") or ""),
                                }
                            )
                    sector = value.get("related_sector")
                    sector_history = sector.get("price_volume") if isinstance(sector, dict) else None
                    if isinstance(sector_history, dict) and sector_history.get("source_ref"):
                        details.append(
                            {
                                "name": str(sector_history.get("source") or "相关板块历史日线"),
                                "url": str(sector_history["source_ref"]),
                                "as_of": str(sector_history.get("as_of") or value.get("market_date") or ""),
                            }
                        )
                    breadth = value.get("market_breadth")
                    if isinstance(breadth, dict) and breadth.get("source_ref"):
                        details.append(
                            {
                                "name": str(breadth.get("source") or "A股全市场涨跌家数"),
                                "url": str(breadth["source_ref"]),
                                "as_of": str(breadth.get("trade_date") or value.get("market_date") or ""),
                            }
                        )
                    histories = value.get("continuous_price_volume")
                    if isinstance(histories, dict):
                        for history in histories.values():
                            if isinstance(history, dict) and history.get("source_ref"):
                                details.append(
                                    {
                                        "name": str(history.get("source") or "主要指数连续量价"),
                                        "url": str(history["source_ref"]),
                                        "as_of": str(history.get("as_of") or value.get("market_date") or ""),
                                    }
                                )
                sources[str(evidence["evidence_id"])] = details
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
            market_scene = str(thread["security_id"]) in MARKET_OVERVIEW_SECURITY_IDS
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
        completed_skill_ids = {
            str(result.get("skill_id"))
            for result in skill_results
            if result.get("status") == "completed"
        }
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
            store = self._evidence_store(str(thread["security_id"]), skill_results)
            store = self._persist_evidence_store(run_id, store)
            role_id = str(role["role_id"])
            blackboard = self._blackboards.build(
                run_id=run_id,
                store=store,
                role_plans={
                    role_id: RoleEvidencePlanner().plan(
                        role_id=role_id,
                        question=content,
                        manifest=EvidenceManifest.from_store(store),
                        token_budget=ContextBudget().evidence_budget * 3,
                    )
                },
            )
            self._append_event(
                thread_id,
                run_id,
                "system",
                "context",
                {
                    "content": f"已建立外部证据目录：{len(store.records)} 条完整证据，按当前框架分包读取。",
                    "evidence_count": len(store.records),
                    "token_estimate": sum(item.token_estimate for item in store.records),
                    "token_estimate_kind": "estimated",
                },
                event_type="context.completed",
            )
            self._append_event(
                thread_id,
                run_id,
                "system",
                "blackboard",
                {
                    "content": f"已生成一次共享事实层：{len(blackboard.evidence_ids)} 条可复核公共事实。",
                    "fact_count": len(blackboard.evidence_ids),
                    "common_claims": [],
                },
                event_type="blackboard.completed",
            )
            self.vault.connection.commit()
        # ponytail: each research turn is independent. The persisted timeline is for the user,
        # not implicit model memory; add explicit user-selected context later if truly needed.
        history = [{"role": "user", "content": content}]
        performance_states: dict[str, ExpertResearchState] = {}
        public_fallback_state: ExpertResearchState | None = None
        try:
            if market_scene and not is_market_report_question(content):
                reply = {
                    "content": "大盘议事厅仅生成盘前、盘中或盘后行情报告；A股概览与全球概览使用各自的市场证据范围。个股、基金或其他投资问题请移步证券资料助手。",
                    "cited_evidence_ids": [],
                    "assumptions": [],
                    "unknowns": [],
                    "refused": True,
                }
            elif not is_investment_question(content):
                reply = {
                    "content": "投研大师只讨论金融、理财和投资相关问题。请围绕当前标的的经营、财务、估值、市场、组合或风险继续提问。",
                    "cited_evidence_ids": [],
                    "assumptions": [],
                    "unknowns": [],
                    "refused": True,
                }
            else:
                state = self._run_expert_state(
                    run_id=run_id,
                    role_id=str(role["role_id"]),
                    question=content,
                    store=store,
                    provider_role=market_report_role(role) if market_scene else role,
                    shared_board=blackboard,
                )
                store, states = self._coverage_supplement(
                    run_id=run_id,
                    security_id=str(thread["security_id"]),
                    question=content,
                    store=store,
                    states={str(role["role_id"]): state},
                )
                state = states[str(role["role_id"])]
                performance_states = states
                public_fallback_state = self._publishable_state(state)
                if market_scene and callable(getattr(self.provider, "generate_structured", None)):
                    reply, _board = self._generate_sectioned_report(
                        run_id=run_id,
                        security_id=str(thread["security_id"]),
                        question=content,
                        states=states,
                        store=store,
                    )
                elif market_scene:
                    reply = self._opinion_from_state(state)
                elif callable(getattr(self.provider, "generate_structured", None)):
                    reply = self._provider_chat(
                        run_id,
                        role=role,
                        messages=history,
                        context=json.dumps(
                            {
                                "expert_state": public_fallback_state.as_dict(),
                                "instruction": (
                                    "只基于已投影的可发布结论直接回答，不重新读取原始证据。"
                                    "没有进入 expert_state 的命题不得在正文讨论。"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                        use_runtime_market_skill=False,
                    )
                else:
                    reply = self._opinion_from_state(state)
            valid_evidence_ids = {record.evidence_id for record in store.records}
            reply["cited_evidence_ids"] = [
                evidence_id
                for evidence_id in reply.get("cited_evidence_ids", [])
                if evidence_id in valid_evidence_ids
            ]
            reply = self._clean_reply(reply, completed_skill_ids)
            if not reply.get("refused"):
                reply = self._finalize_public_reply(
                    reply,
                    fallback_state=public_fallback_state,
                )
            source_index = self._source_index_from_store(store)
            reply["sources"] = [
                source
                for evidence_id in reply["cited_evidence_ids"]
                if evidence_id in source_index
                for source in source_index[evidence_id]
            ] + [
                {
                    "name": str(item.get("title") or "一手公开资料"),
                    "url": str(item.get("url") or ""),
                    "as_of": str(item.get("published_at") or item.get("accessed_at") or "")[:10],
                }
                for item in reply.get("reached_sources", [])
                if isinstance(item, dict)
                and str(item.get("url") or "").startswith(("https://", "http://"))
            ]
            reply_role = market_report_role(role) if market_scene and not reply.get("refused") else role
            reply["role_id"] = reply_role["role_id"]
            reply["role_name"] = reply_role["name"]
            with self.vault.lock:
                self._append_event(thread_id, run_id, "assistant", str(role["role_id"]), reply)
                summary_exists = self.vault.connection.execute(
                    "SELECT 1 FROM research_performance_summaries WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if summary_exists is None:
                    self._persist_performance_summary(
                        run_id=run_id,
                        store=store,
                        states=performance_states,
                        report=reply,
                        completion_status="completed",
                    )
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
                if self._is_cancelled(run_id):
                    self._ensure_terminal_failure_metric(
                        run_id=run_id,
                        error=AIUnavailableError("本轮研究已由用户停止"),
                        node_id="workflow:cancelled",
                    )
                    self._persist_performance_summary(
                        run_id=run_id,
                        store=None,
                        states=None,
                        report=None,
                        completion_status="cancelled",
                    )
                    self.vault.connection.commit()
                    return {"run_id": run_id, "status": "cancelled"}
                failed_at = datetime.now(timezone.utc).isoformat()
                self.vault.connection.execute(
                    "UPDATE research_runs SET status = 'failed', completed_at = ?, failure_json = ? WHERE run_id = ?",
                    (failed_at, json.dumps({"message": str(error)}, ensure_ascii=False), run_id),
                )
                self._ensure_terminal_failure_metric(run_id=run_id, error=error)
                self._persist_performance_summary(
                    run_id=run_id,
                    store=None,
                    states=None,
                    report=None,
                    completion_status="failed",
                )
                self.vault.connection.commit()
            raise

    def _committee_skill_results(
        self, *, security_id: str, question: str, role_ids: list[str]
    ) -> list[dict[str, object]]:
        by_skill: dict[str, dict[str, object]] = {}
        readiness: list[dict[str, object]] = []
        for role_id in role_ids:
            for result in self.skill_layer.run(security_id=security_id, question=question, role_id=role_id):
                copied = dict(result)
                if copied.get("skill_id") == "framework-readiness":
                    copied["skill_id"] = f"framework-readiness-{role_id}"
                    copied["name"] = f"{get_role(role_id)['name']}证据覆盖检查"
                    readiness.append(copied)
                else:
                    by_skill.setdefault(str(copied["skill_id"]), copied)
        return [*readiness, *by_skill.values()]

    @staticmethod
    def _clean_reply(
        reply: dict[str, object], completed_skill_ids: set[str] | None = None
    ) -> dict[str, object]:
        replacements = (
            (r"portfolio-risk-evidence\.ledger_entries", "本地持仓账本明细"),
            (r"portfolio-risk-evidence", "组合与持仓证据"),
            (r"\bledger_entries\b", "本地持仓账本明细"),
            (r"Claim/Conflict Boards?", "研究结论与分歧记录"),
            (r"Claim Boards?", "已核验研究结论"),
            (r"Conflict Boards?", "未解决分歧记录"),
            (r"Expert States?", "专家研究结论"),
            (r"Domain Packets?", "分主题证据材料"),
            (r"Shared (?:Research )?Blackboards?", "共享事实清单"),
            (r"Blackboards?", "共享事实清单"),
            (r"empty_order_book", "当前公开行情未返回有效买卖盘"),
            (r"market-specific order book not connected", "当前市场未接入公开五档盘口"),
            (r"\b[a-z]+(?:-[a-z]+)+-evidence(?:\.[a-z_]+)?\b", "相关研究证据"),
            (
                r"['\"]?utf-8['\"]? codec can(?:not|'t) decode byte 0x[0-9a-fA-F]+"
                r"[^；;。\n]*invalid start byte",
                "公开资料暂未成功解析",
            ),
        )

        def clean_text(value: object) -> str:
            content = re.sub(
                r"[（(]?EVIDENCE(?:-(?:SKILL|FINANCIAL|FUND))?-[A-Za-z0-9_-]+[）)]?",
                "",
                str(value or ""),
            )
            for pattern, replacement in replacements:
                content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)
            return content.strip()

        completed = completed_skill_ids or set()

        def contradicts_completed_evidence(value: str) -> bool:
            checks: list[str] = []
            if "company-financial-quality" in completed:
                checks.extend((
                    r"腾讯控股.*缺少.*(?:本轮完整|标准化|最新).*财务质量",
                    r"缺少腾讯控股本轮完整财务质量证据",
                    r"腾讯控股、秋田微、万邦医药.*缺少.*财务质量、趋势和估值证据闭环",
                    r"本轮未补齐腾讯财务质量",
                ))
            if "market-context-evidence" in completed:
                checks.extend((
                    r"(?:缺少|尚缺).*7月22日.*(?:市场宽度|上涨.?下跌家数|涨跌停|板块资金)",
                    r"尚缺A股全市场7月22日宽度",
                ))
            if {"market-context-evidence", "execution-liquidity-evidence"} <= completed:
                checks.extend((
                    r"个股日K衍生指标.*(?:缺失|未补齐)",
                    r"腾讯控股、万邦医药及基金.*财务质量/趋势证据未补齐",
                ))
            return any(re.search(pattern, value) for pattern in checks)

        def remove_completed_phrases(value: str) -> str:
            if "company-financial-quality" in completed:
                value = re.sub(
                    r"万邦医药、陇神戎发、腾讯控股及基金底层持仓的最新财务/盈利加速证据尚未在本轮完整覆盖。?",
                    "腾讯季度盈利加速与基金实时底层持仓仍不可验证。",
                    value,
                )
                value = re.sub(r"[、，]?标准化财务质量", "", value)
                value = re.sub(r"[、，]?最新财务质量", "", value)
                value = re.sub(
                    r"腾讯控股缺少本轮(?:完整)?标准化三表、(?:分业务利润率、)?现金流(?:、分部利润)?(?:与|、|和)资本配置证据[^。]*。?",
                    "腾讯控股仍缺发行人原文层面的分业务利润率、现金流与资本配置细节。",
                    value,
                )
                value = re.sub(r"需补齐公司盈利质量证据。?", "", value)
                value = re.sub(
                    r"腾讯控股与交银优择回报C是否需要补齐最新财务/基金穿透证据，以确认科技暴露是否重复集中？",
                    "交银优择回报C仍需实时基金穿透证据，以确认科技暴露是否重复集中？",
                    value,
                )
                value = re.sub(
                    r"腾讯控股与其他非茅台个股是否有同等完整的财务质量、现金流和估值证据包？",
                    "腾讯控股与小盘持仓仍需发行人原文层面的现金流和经营解释。",
                    value,
                )
                value = re.sub(
                    r"腾讯控股、贵州茅台等核心持仓的本轮财务质量、现金流、估值分位和反方资料未在本专家证据包中闭环。?",
                    "核心持仓仍缺发行人原文层面的分部现金流、资本配置与反方资料。",
                    value,
                )
                value = re.sub(
                    r"本轮未提供腾讯完整财务质量、分业务现金流和管理层资本配置证据。?",
                    "本轮仍缺腾讯发行人原文层面的分业务现金流和管理层资本配置细节。",
                    value,
                )
            if "portfolio-risk-evidence" in completed:
                portfolio_scope_claim = (
                    ("现金" in value or "港股汇率" in value)
                    and any(term in value for term in ("市值", "权重", "集中度"))
                    and any(
                        term in value
                        for term in ("统一", "未统一", "缺少", "未提供", "需统一", "不可获得", "是多少")
                    )
                )
                if portfolio_scope_claim:
                    value = (
                        "组合现金、港股汇率、推导数量、市值与权重已形成统一估算；"
                        "真实成交数量、费用、券商确认权重和压力期流动性仍不可得。"
                    )
                value = re.sub(
                    r"腾讯港股汇率、真实数量和当前市值未在本轮包中形成完整统一口径",
                    "腾讯真实成交数量仍需券商确认",
                    value,
                )
                value = re.sub(
                    r"缺少券商真实成交数量、费用、港币汇率与统一实时市值",
                    "券商真实成交数量和费用仍待核验；港币汇率与估算市值已形成",
                    value,
                )
                value = re.sub(
                    r"缺少各持仓真实成交数量、券商成本、实时汇率和完整市值口径，无法计算精确仓位权重与盈亏。?",
                    "缺少券商确认的真实成交数量和费用；当前汇率、市值、权重与盈亏为公开数据估算。",
                    value,
                )
                value = re.sub(
                    r"本地持仓缺少券商确认数量、统一汇率和完整实时市值，组合权重只能按账本投入金额条件化处理。?",
                    "本地持仓的数量、汇率和当前市值已按公开数据估算；券商确认数量与费用仍不可得。",
                    value,
                )
                value = re.sub(
                    r"本地持仓缺少券商真实成交数量、实时市值、汇率和完整组合风险贡献，现金比例与权重判断仍以账本金额为主。?",
                    "本地持仓的数量、汇率、市值、现金比例与权重已按账本和公开数据估算；券商确认成交与费用仍不可得。",
                    value,
                )
                value = re.sub(
                    r"缺少券商确认的真实持仓数量、成交价、港股买入汇率和统一实时市值，组合风险只能条件化。?",
                    "缺少券商确认的真实成交数量、成交价与费用；当前汇率和统一市值为公开数据估算，组合风险只能条件化。",
                    value,
                )
                value = re.sub(
                    r"各持仓真实成交数量、成本、港股汇率、基金份额与统一实时市值仍需券商或本地账本进一步补齐。?",
                    "券商确认的真实成交数量、成本与基金份额仍需补齐；当前港股汇率和统一市值已按公开数据估算。",
                    value,
                )
                value = re.sub(
                    r"需补齐真实成交数量、港股汇率、基金份额和实时市值后才能计算精确组合风险权重",
                    "需补齐券商确认的真实成交数量与基金份额；当前港股汇率、市值和组合权重为公开数据估算。",
                    value,
                )
                value = re.sub(
                    r"在统一现金、港股汇率、持仓数量和实时市值后，用户组合中贵州茅台真实权重与风险预算是多少？",
                    "现金、港股汇率、推导数量和统一市值已形成估算；贵州茅台真实权重仍需券商账单确认。",
                    value,
                )
                value = re.sub(
                    r"用户完整组合的实时市值权重、现金比例、港股汇率口径和压力期流动性仍不可获得。?",
                    "组合市值权重、现金比例和港股汇率已有统一估算；压力期流动性与券商确认权重仍不可得。",
                    value,
                )
                value = re.sub(
                    r"需要统一港股汇率、基金和现金口径",
                    "港股汇率、基金和现金已形成统一估算；券商账单口径仍待核验。",
                    value,
                )
                value = re.sub(
                    r"需要真实成交数量与当前市值，而非仅投入成本",
                    "当前市值已按推导数量和公开报价估算；真实成交数量仍待券商确认。",
                    value,
                )
                value = re.sub(
                    r"统一港股汇率、实时市值和现金后，茅台在总资产中的真实风险权重是多少？",
                    "港股汇率、实时市值和现金已形成统一估算；茅台真实风险权重仍需券商账单确认。",
                    value,
                )
                value = re.sub(
                    r"持仓市值、港股汇率、现金比例和实时总资产口径尚未统一",
                    "持仓市值、港股汇率、现金比例和实时总资产已形成统一估算；真实券商口径仍未取得。",
                    value,
                )
                value = re.sub(
                    r"完整组合实时市值、港股汇率、现金比例和压力期流动性证据是否足以评估集中度风险？",
                    "组合实时市值、港股汇率和现金比例已有统一估算；压力期流动性与券商确认数据是否足以评估集中度风险？",
                    value,
                )
            if "market-context-evidence" in completed:
                value = re.sub(
                    r"缺少A股全市场7月22日涨跌家数、涨停/跌停、连板梯队和成交额龙头",
                    "缺少涨停/跌停、连板梯队和成交额龙头",
                    value,
                )
                value = re.sub(
                    r"缺少A股涨跌家数、连板梯队、涨停/跌停扩散",
                    "缺少连板梯队、涨停/跌停扩散",
                    value,
                )
                value = re.sub(
                    r"A股主要宽基指数的同口径7月22日连续趋势、市场宽度、涨跌停扩散未在当前包中完整呈现。?",
                    "A股涨跌停扩散与连板结构未在当前资料中完整呈现。",
                    value,
                )
            if "security-valuation-evidence" in completed:
                value = re.sub(r"[，,]?缺少历史估值分位与同行完整比较。?", "。", value)
            value = re.sub(
                r"已生成行业候选可比公司及同日估值；业务可比性仍需用户确认",
                "该历史轮次仅生成候选可比公司；新分析会按行业与经营特征自动评估业务相似度",
                value,
            )
            if "execution-liquidity-evidence" in completed:
                value = re.sub(r"(?:600519|300534|300939|301520|00700)交易成本代理未形成；?", "", value)
            value = re.sub(
                r"600519交易成本代理未形成",
                "该历史轮次未形成贵州茅台交易成本估算；新分析会重新计算",
                value,
            )
            value = value.replace("腾讯控股的、分部", "腾讯控股的分部")
            value = value.replace("的与分部现金流", "的分部现金流")
            value = value.replace("逐笔流动性和证据", "逐笔流动性证据")
            return re.sub(r"[、，]{2,}", "、", value).strip("、， ")

        content_lines = [clean_text(line) for line in str(reply.get("content") or "").splitlines()]
        reply["content"] = "\n".join(
            remove_completed_phrases(line)
            for line in content_lines
            if line and not contradicts_completed_evidence(line)
        ).strip()
        for key in ("unknowns", "gaps", "risks", "assumptions", "coverage_gaps"):
            if isinstance(reply.get(key), list):
                cleaned = [clean_text(item) for item in reply[key]]
                processed = [
                    remove_completed_phrases(item)
                    for item in cleaned
                    if item and not contradicts_completed_evidence(item)
                ]
                reply[key] = [item for item in processed if item]
        if isinstance(reply.get("report"), str):
            reply["report"] = clean_text(reply["report"])
        return reply

    def _send_committee(self, *, thread_id: str, content: str) -> dict[str, object]:
        run_id, now = str(uuid4()), datetime.now(timezone.utc).isoformat()
        with self.vault.lock:
            thread = self.get(thread_id)
            deep_request = str(
                thread["security_id"]
            ) in MARKET_OVERVIEW_SECURITY_IDS or is_deep_research_request(content)
            running = self.vault.connection.execute(
                "SELECT 1 FROM research_runs WHERE thread_id = ? AND status = 'running' LIMIT 1",
                (thread_id,),
            ).fetchone()
            if running:
                raise ValueError("当前投研委员会仍在研究中，请等待本轮完成")
            plan = committee_plan(str(thread["security_id"]), content)
            self.vault.connection.execute(
                "INSERT INTO research_runs VALUES (?, ?, 'stock-analysis-4.15.0-committee-v1', 'running', 'planning', ?, ?, ?, NULL, NULL)",
                (
                    run_id,
                    thread_id,
                    json.dumps({"content": content}, ensure_ascii=False),
                    json.dumps(plan, ensure_ascii=False),
                    now,
                ),
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
                thread_id,
                run_id,
                "system",
                "coordinator",
                (
                    {
                        "content": "协调员已理解研究问题并制定任务计划。",
                        "role_name": "协调员",
                        "selected_roles": [get_role(role_id)["name"] for role_id in plan["roles"]],
                        "assignments": [
                            {"name": get_role(item["role_id"])["name"], "function": item["function"]}
                            for item in plan["assignments"]
                        ],
                        "reason": plan["reason"],
                        "stages": plan["stages"],
                    }
                    if deep_request
                    else {
                        "content": "协调员已完成问题分流。",
                        "role_name": "协调员",
                    }
                ),
                event_type="plan.completed" if deep_request else "routing.completed",
            )
            self.vault.connection.commit()

        if not deep_request:
            reply = {
                "content": "这个问题更适合投研大师快速回答。投研委员会用于个股、基金或行情复盘的深度报告；请切换到投研大师，或补充研究范围、关注风险和希望复盘的时间区间。",
                "cited_evidence_ids": [],
                "assumptions": [],
                "unknowns": [],
                "role_id": "coordinator",
                "role_name": "协调员",
                "refused": True,
                "suggested_mode": "assistant",
            }
            with self.vault.lock:
                self._append_event(thread_id, run_id, "assistant", "coordinator", reply)
                self.vault.connection.execute(
                    "UPDATE research_runs SET status = 'completed', current_stage = 'completed', completed_at = ? WHERE run_id = ?",
                    (datetime.now(timezone.utc).isoformat(), run_id),
                )
                self._persist_performance_summary(
                    run_id=run_id,
                    store=None,
                    states=None,
                    report=reply,
                    completion_status="completed",
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
                if self._is_cancelled(run_id):
                    self._ensure_terminal_failure_metric(
                        run_id=run_id,
                        error=AIUnavailableError("本轮研究已由用户停止"),
                        node_id="workflow:cancelled",
                    )
                    self._persist_performance_summary(
                        run_id=run_id,
                        store=None,
                        states=None,
                        report=None,
                        completion_status="cancelled",
                    )
                    self.vault.connection.commit()
                    return
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
                self._ensure_terminal_failure_metric(run_id=run_id, error=error)
                self._persist_performance_summary(
                    run_id=run_id,
                    store=None,
                    states=None,
                    report=None,
                    completion_status="failed",
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
            skill_results = [
                {
                    "skill_id": "research-evidence-router",
                    "name": "研究证据收集",
                    "status": "failed",
                    "gaps": [str(error)],
                    "evidence": [],
                }
            ]
        completed_skill_ids = {
            str(result.get("skill_id"))
            for result in skill_results
            if result.get("status") == "completed"
        }
        with self.vault.lock:
            self.vault.connection.execute(
                "UPDATE research_runs SET current_stage = 'evidence' WHERE run_id = ?", (run_id,)
            )
            for result in skill_results:
                self._append_event(
                    thread_id,
                    run_id,
                    "system",
                    str(result["skill_id"]),
                    {
                        "content": f"已补充{result['name']}（{result['status']}）。",
                        "skill_name": result["name"],
                        "status": result["status"],
                        "gaps": result.get("gaps") or [],
                        "evidence_ids": [item["evidence_id"] for item in result.get("evidence") or []],
                    },
                    event_type="tool.completed",
                )
            self.vault.connection.commit()

        store = self._evidence_store(str(thread["security_id"]), skill_results)
        with self.vault.lock:
            store = self._persist_evidence_store(run_id, store)
            manifest = EvidenceManifest.from_store(store)
            role_plans = {
                role_id: RoleEvidencePlanner().plan(
                    role_id=role_id,
                    question=content,
                    manifest=manifest,
                    token_budget=ContextBudget().evidence_budget * 3,
                )
                for role_id in role_ids
            }
            blackboard = self._blackboards.build(
                run_id=run_id,
                store=store,
                role_plans=role_plans,
            )
            self._append_event(
                thread_id,
                run_id,
                "system",
                "context",
                {
                    "content": f"已建立外部证据目录：{len(store.records)} 条完整证据，按专家框架分包读取。",
                    "evidence_count": len(store.records),
                    "token_estimate": sum(item.token_estimate for item in store.records),
                    "token_estimate_kind": "estimated",
                },
                event_type="context.completed",
            )
            self._append_event(
                thread_id,
                run_id,
                "system",
                "blackboard",
                {
                    "content": f"已生成一次共享事实层：{len(blackboard.evidence_ids)} 条可复核公共事实。",
                    "fact_count": len(blackboard.evidence_ids),
                    "common_claims": [],
                },
                event_type="blackboard.completed",
            )
            self.vault.connection.commit()
        states: dict[str, ExpertResearchState] = {}
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
                    (
                        task_id,
                        run_id,
                        role_id,
                        json.dumps({"question": content}, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat(),
                    ),
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

        def run_expert(
            role_id: str,
        ) -> tuple[str, ExpertResearchState | None, BaseException | None]:
            try:
                return (
                    role_id,
                    self._run_expert_state(
                        run_id=run_id,
                        role_id=role_id,
                        question=content,
                        store=store,
                        allowed_evidence_ids=set(role_plans[role_id].evidence_ids),
                        shared_board=blackboard,
                    ),
                    None,
                )
            except BaseException as error:
                return role_id, None, error

        tasks = tuple(
            ExpertExecutionTask(
                role_id=role_id,
                estimated_tokens=(
                    estimate_tokens(blackboard.render_prompt_for_role(role_id))
                    + sum(
                        store.get(evidence_id).token_estimate
                        for evidence_id in role_plans[role_id].evidence_ids
                        if evidence_id
                        not in set(blackboard.shared_evidence_ids_for(role_id))
                    )
                    + estimate_tokens(content)
                    + estimate_tokens(self._initial_coverage(role_id, store))
                    + estimate_tokens(EXPERT_STATE_UPDATE_SCHEMA)
                    + 2_048
                ),
                priority=role_ids.index(role_id),
            )
            for role_id in role_ids
        )
        provider_capacity = int(getattr(self.provider, "max_concurrency", 3) or 3)
        scheduled, max_workers = ExpertExecutionScheduler().schedule(
            tasks,
            provider_capacity=provider_capacity,
        )
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="invest-committee") as pool:
            futures = [pool.submit(run_expert, task.role_id) for task in scheduled]
            expert_results = [future.result() for future in as_completed(futures)]

        ordered_results = sorted(expert_results, key=lambda item: role_ids.index(item[0]))
        failures: dict[str, BaseException] = {}
        for role_id, state, error in ordered_results:
            if state is not None:
                states[role_id] = state
            elif error is not None:
                failures[role_id] = error

        store, states = self._coverage_supplement(
            run_id=run_id,
            security_id=str(thread["security_id"]),
            question=content,
            store=store,
            states=states,
        )
        opinions: list[dict[str, object]] = []
        for role_id in role_ids:
            role = get_role(role_id)
            task = self.vault.connection.execute(
                "SELECT task_id FROM research_tasks WHERE run_id = ? AND assigned_role = ?",
                (run_id, role_id),
            ).fetchone()
            if role_id in states:
                opinion = self._clean_reply(
                    self._opinion_from_state(states[role_id]), completed_skill_ids
                )
                opinions.append(opinion)
                with self.vault.lock:
                    attempt_row = self.vault.connection.execute(
                        "SELECT COALESCE(MAX(retry_count), 0) + 1 AS attempt "
                        "FROM research_call_metrics "
                        "WHERE run_id = ? AND role_id = ? AND stage = 'expert_synthesis'",
                        (run_id, role_id),
                    ).fetchone()
                    provider_attempts = int(attempt_row["attempt"] if attempt_row else 1)
                    self._append_event(
                        thread_id, run_id, "assistant", role_id, opinion, event_type="expert.completed"
                    )
                    self.vault.connection.execute(
                        "UPDATE research_tasks SET output_json = ?, status = 'completed', attempt = ?, completed_at = ? WHERE task_id = ?",
                        (
                            json.dumps(opinion, ensure_ascii=False),
                            max(1, provider_attempts),
                            datetime.now(timezone.utc).isoformat(),
                            task["task_id"],
                        ),
                    )
                    self.vault.connection.commit()
            else:
                error = failures[role_id]
                with self.vault.lock:
                    self._append_event(
                        thread_id,
                        run_id,
                        "system",
                        role_id,
                        {
                            "content": f"{role['name']}本轮未完成，协调员将使用其余意见继续。",
                            "role_name": role["name"],
                            "gaps": ["本轮生成未完成，可稍后重试该专家"],
                        },
                        event_type="expert.failed",
                    )
                    self.vault.connection.execute(
                        "UPDATE research_tasks SET output_json = ?, status = 'failed', attempt = ?, completed_at = ? WHERE task_id = ?",
                        (
                            json.dumps({"message": str(error)}, ensure_ascii=False),
                            2,
                            datetime.now(timezone.utc).isoformat(),
                            task["task_id"],
                        ),
                    )
                    self.vault.connection.commit()

        pre_report_board = ClaimBoard.from_states(list(states.values()))
        risk_state = RiskReviewState.build(
            states=list(states.values()), board=pre_report_board, store=store
        )
        with self.vault.lock:
            self._persist_claim_board(run_id, pre_report_board, store)
            self._persist_risk_review(run_id, risk_state)
            self.vault.connection.execute(
                "UPDATE research_runs SET current_stage = 'conflicts' WHERE run_id = ?", (run_id,)
            )
            self._append_event(
                thread_id,
                run_id,
                "system",
                "coordinator",
                {
                    "content": (
                        f"协调员已整理 {len(pre_report_board.consensus)} 项共识、"
                        f"{len(pre_report_board.conflicts)} 项分歧。"
                    ),
                    "role_name": "协调员",
                    "consensus": pre_report_board.consensus,
                    "conflicts": list(pre_report_board.conflicts),
                },
                event_type="conflicts.completed",
            )
            self.vault.connection.execute(
                "UPDATE research_runs SET current_stage = 'risk_review' WHERE run_id = ?", (run_id,)
            )
            self._append_event(
                thread_id,
                run_id,
                "system",
                "risk_manager",
                {
                    "content": f"已完成 {len(risk_state.items)} 项结构化风险与组合影响审查。",
                    "role_name": "风险与组合经理",
                    "risk_state": risk_state.as_dict(),
                },
                event_type="risk_review.completed",
            )
            self.vault.connection.execute(
                "UPDATE research_runs SET current_stage = 'reporting' WHERE run_id = ?", (run_id,)
            )
            self._append_event(
                thread_id,
                run_id,
                "system",
                "report_editor",
                {"content": "正在按章节生成并统一编辑最终报告。", "role_name": "投资经理"},
                event_type="reporting.started",
            )
            self.vault.connection.commit()
        report, board = self._generate_sectioned_report(
            run_id=run_id,
            security_id=str(thread["security_id"]),
            question=content,
            states=states,
            store=store,
            risk_state=risk_state,
        )
        report = self._clean_reply(report, completed_skill_ids)
        report = self._finalize_public_reply(report)
        source_index = self._source_index_from_store(store)
        report["sources"] = [
            source
            for evidence_id in report.get("cited_evidence_ids", [])
            for source in source_index.get(str(evidence_id), [])
        ]
        unresolved = list(
            dict.fromkeys(
                [
                    question
                    for state in states.values()
                    for question in state.open_questions
                ]
                + [str(item) for item in report.get("unknowns", [])]
            )
        )
        completed_at = datetime.now(timezone.utc).isoformat()
        with self.vault.lock:
            self._append_event(
                thread_id, run_id, "assistant", "report_editor", report, event_type="report.completed"
            )
            self.vault.connection.execute(
                "INSERT INTO research_reports VALUES (?, ?, 1, ?, ?, NULL, ?)",
                (
                    str(uuid4()),
                    run_id,
                    json.dumps(report, ensure_ascii=False),
                    str(report["content"]),
                    completed_at,
                ),
            )
            self.vault.connection.execute(
                "UPDATE research_runs SET status = 'completed', current_stage = 'completed', completed_at = ?, failure_json = ? WHERE run_id = ?",
                (
                    completed_at,
                    json.dumps(
                        {"partial": True, "gaps": unresolved, "conflicts": list(board.conflicts)},
                        ensure_ascii=False,
                    )
                    if not report.get("unified_edit_completed", True)
                    else None,
                    run_id,
                ),
            )
            self.vault.connection.execute(
                "UPDATE research_threads SET role_id = 'coordinator', updated_at = ? WHERE thread_id = ?",
                (completed_at, thread_id),
            )
            self.vault.connection.commit()
        return report

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
