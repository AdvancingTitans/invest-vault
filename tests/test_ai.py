import json
import re
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invest_vault.ai import (
    AIUnavailableError,
    CodexAppServerProvider,
    ResearchChatStore,
    is_investment_question,
    market_report_role,
)
from invest_vault.ai_roles import committee_plan, get_role
from invest_vault.ai_skills import SKILL_CATALOG
from invest_vault.api import create_app
from invest_vault.evidence_orchestration import (
    EvidenceManifest,
    EvidenceRecord,
    EvidenceStore,
    PacketEngine,
    RoleEvidencePlanner,
)
from invest_vault.ledger import HoldingRecord, Vault
from invest_vault.research import ResearchStore


class FakeAIProvider:
    def __init__(self, *, authenticated: bool = True) -> None:
        self.authenticated = authenticated
        self.closed = False
        self.chat_calls = 0
        self.last_context = ""

    def status(self) -> dict[str, object]:
        return {
            "available": True,
            "authenticated": self.authenticated,
            "provider": "codex_app_server",
            "account": {"type": "chatgpt", "email": "user@example.test", "planType": "plus"}
            if self.authenticated
            else None,
            "detail": "Codex 已登录" if self.authenticated else "Codex 尚未登录",
        }

    def start_chatgpt_login(self) -> dict[str, object]:
        return {"type": "chatgpt", "loginId": "login-1", "authUrl": "https://auth.example.test/login-1"}

    def logout(self) -> dict[str, object]:
        self.authenticated = False
        return {"loggedOut": True}

    def list_models(self) -> list[dict[str, object]]:
        return [
            {
                "id": "gpt-5.4",
                "displayName": "GPT-5.4",
                "supportedReasoningEfforts": ["low", "medium", "high"],
            }
        ]

    def configure_models(self, settings: dict[str, dict[str, str | None]]) -> None:
        self.model_settings = settings

    def quick_note(self, raw_text: str, security_id: str) -> dict[str, object]:
        assert raw_text == "今天回调，批价没变。先不补仓。"
        assert security_id == "CN:SSE:600519:STOCK"
        return {
            "title": "回调与批价观察",
            "facts": ["用户观察到今日股价回调", "用户观察到批价未变化"],
            "user_judgements": [],
            "open_questions": [],
            "planned_actions": ["暂不补仓"],
            "tags": ["盘后观察"],
        }

    def chat(
        self,
        *,
        role: dict[str, object],
        messages: list[dict[str, str]],
        context: str,
        use_runtime_market_skill: bool = False,
    ) -> dict[str, object]:
        self.chat_calls += 1
        self.last_context = context
        assert role["role_id"] == "buffett"
        assert messages[-1]["content"] == "护城河是否仍然成立？"
        assert "EVIDENCE" in context
        return {
            "content": "按巴菲特视角看，当前证据只支持继续核实护城河。",
            "cited_evidence_ids": [
                "EVIDENCE-SKILL-test" if "EVIDENCE-SKILL-test" in context else "EVIDENCE-1"
            ],
            "assumptions": [],
            "unknowns": ["长期自由现金流证据不足"],
        }

    def close(self) -> None:
        self.closed = True


class CatalogOnlyResearchSkillLayer:
    def catalog(self) -> list[dict[str, str]]:
        return [dict(item) for item in SKILL_CATALOG]

    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        return []


def test_local_notes_never_enter_ai_context_or_evidence_store(tmp_path: Path) -> None:
    security_id = "CN:SSE:600519:STOCK"
    secret_note = "PRIVATE_NOTE_MUST_NOT_REACH_ANY_MODEL"
    with Vault(tmp_path / "vault.sqlite3") as vault:
        ResearchStore(vault).add_note(security_id=security_id, body=secret_note)
        chats = ResearchChatStore(vault, FakeAIProvider(), CatalogOnlyResearchSkillLayer())

        legacy_context = chats._context(security_id, [])
        store = chats._evidence_store(security_id, [])

        assert secret_note not in legacy_context
        assert "USER_NOTES" not in legacy_context
        assert "USER_NOTE:" not in legacy_context
        assert all(record.domain != "user-judgement" for record in store.records)
        assert all(record.provider != "Invest Vault本地笔记" for record in store.records)
        assert all(not record.evidence_id.startswith("EVIDENCE-NOTE-") for record in store.records)


class MarketReportProvider(FakeAIProvider):
    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[str] = []

    def generate_structured(self, **kwargs: object) -> dict[str, object]:
        prompt = str(kwargs["prompt"])
        self.contexts.append(prompt)
        evidence_ids = list(
            dict.fromkeys(
                item
                for item in re.findall(r'"evidence_id":\s*"([^"]+)"', prompt)
                if item.startswith("EVIDENCE-")
            )
        )
        return {
            "claims": [],
            "requirements": [],
            "open_questions": [],
            "cited_evidence_ids": evidence_ids[:1],
        }

    def chat(
        self,
        *,
        role: dict[str, object],
        messages: list[dict[str, str]],
        context: str,
        use_runtime_market_skill: bool = False,
    ) -> dict[str, object]:
        self.chat_calls += 1
        self.last_context = context
        self.contexts.append(context)
        assert role["role_id"] == "market_report"
        return {
            "content": "盘后大盘行情报告：指数回升；结合本地持仓，下一步先核对行业暴露。",
            "cited_evidence_ids": [],
            "assumptions": [],
            "unknowns": [],
        }


def test_market_report_supports_expert_style_and_committee_scene() -> None:
    role = market_report_role(get_role("buffett"))
    plan = committee_plan(
        "MARKET:GLOBAL:OVERVIEW",
        "深度复盘最新交易日盘中大盘行情并结合我的持仓",
    )

    assert role["role_id"] == "buffett"
    assert role["report_kind"] == "market"
    assert role["name"] == "巴菲特"
    assert "当前所有可用且完整的证据" in str(role["focus"])
    assert "本地持仓给出条件化观察建议" in str(role["focus"])
    assert "不得回答单股问题" not in str(role)
    assert "龙虎榜" not in str(role)
    assert plan["scene"] == "market"
    assert len(plan["roles"]) == 6
    assert plan["roles"] == [
        "simons",
        "dalio",
        "livermore",
        "o_neil",
        "minervini",
        "soros",
    ]
    assert plan["skill_version"] == "4.15.0"


def test_market_overview_accepts_committee_report_thread(tmp_path: Path) -> None:
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=FakeAIProvider(),
            research_skill_layer=CatalogOnlyResearchSkillLayer(),
        )
    ) as client:
        response = client.post(
            "/api/ai/chats",
            json={
                "security_id": "MARKET:GLOBAL:OVERVIEW",
                "role_id": "general",
                "mode": "committee",
                "title": "盘中投委会报告",
            },
        )

    assert response.status_code == 200
    assert response.json()["thread_type"] == "committee"


def test_market_committee_never_redirects_its_automatic_report_request(tmp_path: Path) -> None:
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=FakeAIProvider(),
            research_skill_layer=CatalogOnlyResearchSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "MARKET:GLOBAL:OVERVIEW",
                "role_id": "general",
                "mode": "committee",
                "title": "自动市场报告",
            },
        ).json()
        response = client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={
                "content": "生成7月17日盘后行情报告，结合我的本地持仓给出条件化观察建议。",
                "role_id": "general",
            },
        )
        immediate = response.json()
        for _ in range(100):
            if client.get(f"/api/ai/chats/{thread['thread_id']}").json()["active_run"]["status"] != "running":
                break
            time.sleep(0.01)
        time.sleep(0.1)

    assert response.status_code == 200
    assert immediate["status"] == "running"


def test_node_based_codex_launcher_uses_an_absolute_node_runtime(tmp_path: Path) -> None:
    launcher = tmp_path / "codex.js"
    launcher.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    provider = CodexAppServerProvider(
        tmp_path / "runtime",
        executable=str(launcher),
        node_executable="/opt/example/node",
    )

    assert provider._app_server_command() == [
        "/opt/example/node",
        str(launcher.resolve()),
        "app-server",
        "--stdio",
    ]
    assert provider._subprocess_environment()["PATH"].split(":")[:2] == [
        "/opt/example",
        str(tmp_path),
    ]


def test_app_server_startup_error_includes_sanitized_stderr(tmp_path: Path) -> None:
    launcher = tmp_path / "broken-codex"
    launcher.write_text("#!/bin/sh\necho 'node runtime missing' >&2\nexit 1\n", encoding="utf-8")
    launcher.chmod(0o755)
    provider = CodexAppServerProvider(tmp_path / "runtime", executable=str(launcher), timeout=2)

    status = provider.status()

    assert status["available"] is False
    assert "node runtime missing" in str(status["detail"])
    provider.close()


def test_bundled_stock_analysis_skill_is_used_without_user_installation(tmp_path: Path) -> None:
    bundled = tmp_path / "skills" / "stock-analysis"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("---\nname: stock-analysis\n---\n", encoding="utf-8")
    provider = CodexAppServerProvider(
        tmp_path / "runtime",
        executable="/bin/false",
        market_skill_directory=bundled,
    )

    assert provider._find_runtime_market_skill() == {
        "type": "skill",
        "name": "stock-analysis",
        "path": str(bundled.resolve()),
    }


def test_bundled_agent_reach_skill_is_used_without_user_installation(tmp_path: Path) -> None:
    bundled = tmp_path / "skills" / "agent-reach"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("---\nname: agent-reach\n---\n", encoding="utf-8")
    provider = CodexAppServerProvider(
        tmp_path / "runtime",
        executable="/bin/false",
        reach_skill_directory=bundled,
    )

    assert provider._find_runtime_reach_skill() == {
        "type": "skill",
        "name": "agent-reach",
        "path": str(bundled.resolve()),
    }


def test_bundled_primary_evidence_skill_is_used_without_user_installation(tmp_path: Path) -> None:
    bundled = tmp_path / "skills" / "primary-evidence-reach"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("---\nname: primary-evidence-reach\n---\n", encoding="utf-8")
    provider = CodexAppServerProvider(
        tmp_path / "runtime",
        executable="/bin/false",
        primary_evidence_skill_directory=bundled,
    )

    assert provider._find_runtime_primary_evidence_skill() == {
        "type": "skill",
        "name": "primary-evidence-reach",
        "path": str(bundled.resolve()),
    }


def test_ai_status_and_chatgpt_login_do_not_expose_tokens(tmp_path: Path) -> None:
    provider = FakeAIProvider()
    with TestClient(create_app(tmp_path, automatic_updates=False, ai_provider=provider)) as client:
        status = client.get("/api/ai/status")
        login = client.post("/api/ai/login/chatgpt")

    assert status.status_code == 200
    assert status.json()["authenticated"] is True
    assert "token" not in json.dumps(status.json()).lower()
    assert login.json() == {
        "type": "chatgpt",
        "loginId": "login-1",
        "authUrl": "https://auth.example.test/login-1",
    }
    assert provider.closed is True


def test_ai_settings_persist_per_task_model_without_credentials(tmp_path: Path) -> None:
    provider = FakeAIProvider()
    with TestClient(create_app(tmp_path, automatic_updates=False, ai_provider=provider)) as client:
        catalog = client.get("/api/ai/models")
        saved = client.put(
            "/api/ai/settings/models/research",
            json={"model_id": "gpt-5.4", "reasoning_effort": "high"},
        )
        settings = client.get("/api/ai/settings").json()

    assert catalog.status_code == 200
    assert catalog.json()[0]["id"] == "gpt-5.4"
    assert saved.status_code == 200
    assert settings["tasks"]["research"] == {
        "provider_id": "codex",
        "model_id": "gpt-5.4",
        "reasoning_effort": "high",
    }
    serialized = json.dumps(settings).lower()
    assert "token" not in serialized
    assert "api_key" not in serialized


def test_provider_api_keys_are_masked_encrypted_and_deletable(tmp_path: Path) -> None:
    provider = FakeAIProvider()
    secret = "sk-direct-provider-secret-2468"
    with TestClient(create_app(tmp_path, automatic_updates=False, ai_provider=provider)) as client:
        catalog = client.get("/api/ai/providers").json()["providers"]
        assert [item["provider_id"] for item in catalog] == [
            "codex",
            "openai",
            "anthropic",
            "google",
            "deepseek",
        ]
        saved = client.put("/api/ai/providers/openai/credential", json={"key": secret})
        assert saved.json() == {"provider_id": "openai", "configured": True, "masked": "••••2468"}
        configured = client.get("/api/ai/providers").json()["providers"]
        openai = next(item for item in configured if item["provider_id"] == "openai")
        assert openai["configured"] is True
        assert openai["masked"] == "••••2468"
        assert secret not in json.dumps(configured)

        routed = client.put(
            "/api/ai/settings/models/research",
            json={"provider_id": "openai", "model_id": "gpt-5.2", "reasoning_effort": None},
        )
        assert routed.status_code == 200
        assert client.get("/api/ai/settings").json()["tasks"]["research"]["provider_id"] == "openai"
        assert client.delete("/api/ai/providers/openai/credential").json()["deleted"] is True

    assert secret.encode() not in (tmp_path / "vault.sqlite3").read_bytes()


def test_ai_logout_uses_codex_account_session(tmp_path: Path) -> None:
    provider = FakeAIProvider()
    with TestClient(create_app(tmp_path, automatic_updates=False, ai_provider=provider)) as client:
        response = client.post("/api/ai/logout")
        status = client.get("/api/ai/status").json()

    assert response.status_code == 200
    assert status["authenticated"] is False


def test_ai_quick_note_preserves_raw_and_requires_acceptance_before_note_write(tmp_path: Path) -> None:
    provider = FakeAIProvider()
    security_id = "CN:SSE:600519:STOCK"
    raw_text = "今天回调，批价没变。先不补仓。"
    accepted_body = (
        "回调与批价观察\n\n事实\n- 用户观察到今日股价回调\n- 用户观察到批价未变化\n\n计划\n- 暂不补仓"
    )

    with TestClient(create_app(tmp_path, automatic_updates=False, ai_provider=provider)) as client:
        draft_response = client.post(
            "/api/ai/quick-notes",
            json={"security_id": security_id, "raw_text": raw_text},
        )
        assert draft_response.status_code == 200
        draft = draft_response.json()
        assert draft["raw_text"] == raw_text
        assert draft["draft"]["title"] == "回调与批价观察"
        assert client.get(f"/api/research/{security_id}").json()["notes"] == []

        accepted = client.post(
            f"/api/ai/quick-notes/{draft['draft_id']}/accept",
            json={"body": accepted_body},
        )
        assert accepted.status_code == 200
        notes = client.get(f"/api/research/{security_id}").json()["notes"]
        assert notes[0]["body"] == accepted_body

        duplicate = client.post(
            f"/api/ai/quick-notes/{draft['draft_id']}/accept",
            json={"body": "重复保存"},
        )
        assert duplicate.status_code == 422

    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        row = connection.execute(
            "SELECT raw_text, draft_json, status, accepted_note_id FROM ai_quick_notes"
        ).fetchone()
    assert row[0] == raw_text
    assert json.loads(row[1])["planned_actions"] == ["暂不补仓"]
    assert row[2] == "accepted"
    assert row[3]


def test_roles_and_recoverable_role_chat(tmp_path: Path) -> None:
    provider = FakeAIProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=CatalogOnlyResearchSkillLayer(),
        )
    ) as client:
        roles = client.get("/api/ai/roles").json()
        skills = client.get("/api/ai/skills").json()
        assert len(roles) == 16
        assert {item["skill_id"] for item in skills} == {
            "fund-portfolio-evidence",
            "fund-liquidity-evidence",
            "company-financial-quality",
            "drawdown-attribution-readiness",
            "security-valuation-evidence",
            "portfolio-risk-evidence",
            "public-topic-evidence",
            "market-context-evidence",
            "supplemental-company-evidence",
            "execution-liquidity-evidence",
            "framework-readiness",
        }
        assert roles[0]["role_id"] == "general"
        assert all(not item["name"].endswith("框架") for item in roles)
        assert {item["role_id"] for item in roles[1:]} >= {"buffett", "munger", "duan_yongping", "simons"}

        security_id = "CN:SSE:600519:STOCK"
        created = client.post(
            "/api/ai/chats",
            json={"security_id": security_id, "role_id": "buffett", "title": "茅台护城河"},
        ).json()
        reply = client.post(
            f"/api/ai/chats/{created['thread_id']}/messages",
            json={"content": "护城河是否仍然成立？", "role_id": "buffett"},
        ).json()
        assert reply["role_id"] == "buffett"
        assert reply["cited_evidence_ids"] == []  # unknown IDs from a provider are discarded

        restored = client.get(f"/api/ai/chats/{created['thread_id']}").json()
        assert [event["actor_type"] for event in restored["events"]] == [
            "user",
            "system",
            "system",
            "assistant",
        ]
        assert [event["event_type"] for event in restored["events"]] == [
            "message.completed",
            "context.completed",
            "blackboard.completed",
            "message.completed",
        ]
        assert restored["events"][1]["event_type"] == "context.completed"
        assert restored["events"][3]["payload"]["unknowns"] == []
        assert restored["events"][3]["payload"]["audit_unknowns"] == ["长期自由现金流证据不足"]


def test_chat_refuses_off_topic_without_spending_a_provider_turn(tmp_path: Path) -> None:
    provider = FakeAIProvider()
    with TestClient(create_app(tmp_path, automatic_updates=False, ai_provider=provider)) as client:
        thread = client.post(
            "/api/ai/chats",
            json={"security_id": "CN:SSE:600519:STOCK", "role_id": "general", "title": "范围测试"},
        ).json()
        reply = client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "帮我写一首生日诗", "role_id": "general"},
        ).json()

    assert reply["refused"] is True
    assert "只讨论金融" in reply["content"]
    assert provider.chat_calls == 0


def test_market_overview_chat_only_generates_session_report_with_local_holdings(
    tmp_path: Path, monkeypatch
) -> None:
    provider = MarketReportProvider()
    monkeypatch.setattr(
        "invest_vault.ai_skills.fetch_security_price_history",
        lambda *_, **__: {
            "rows": [{"date": "2026-07-16", "close": 99}, {"date": "2026-07-17", "close": 100}]
        },
    )
    with TestClient(create_app(tmp_path, automatic_updates=False, ai_provider=provider)) as client:
        client.post(
            "/api/holdings",
            json={
                "rows": [
                    {
                        "row_id": "a",
                        "symbol": "600519",
                        "asset_type": "a_share",
                        "invested_amount_cny": "10000",
                        "bought_on": "2026-01-08",
                    }
                ]
            },
        )
        with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
            for section, payload in {
                "indices": {
                    "date": "2026-07-17",
                    "session": "盘后",
                    "session_label": "7月17日盘后收盘数据",
                    "rows": [{"name": "上证指数", "change_percent": 1.2}],
                },
                "lhb": {"date": "2026-07-17", "rows": []},
                "industry_flow": {"date": "2026-07-17", "inbound": [], "outbound": []},
            }.items():
                connection.execute(
                    "INSERT INTO market_snapshots VALUES (?, ?, ?, ?, ?)",
                    (section, "2026-07-17", "fixture", json.dumps(payload), "2026-07-17T08:00:00Z"),
                )
            connection.commit()
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "MARKET:GLOBAL:OVERVIEW",
                "role_id": "general",
                "mode": "assistant",
                "title": "市场报告",
            },
        ).json()

        refused = client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "分析贵州茅台估值", "role_id": "general"},
        ).json()
        report = client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "生成当前盘后大盘行情报告，并结合我的持仓给出下一步建议", "role_id": "general"},
        ).json()

    assert refused["refused"] is True
    assert "仅生成盘前、盘中或盘后" in refused["content"]
    assert report["role_id"] == "market_report"
    assert provider.chat_calls == 1
    assert any("market_overview" in context for context in provider.contexts)
    assert any("ledger_entries" in context for context in provider.contexts)
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        summaries = connection.execute(
            "SELECT completion_status FROM research_performance_summaries"
        ).fetchall()
    assert sorted(status for (status,) in summaries) == ["completed", "partial"]


def test_financial_scope_gate_keeps_common_short_investment_questions() -> None:
    assert is_investment_question("现在能买吗？")
    assert is_investment_question("现金流为什么变差？")
    assert not is_investment_question("帮我写一首生日诗")


def test_external_evidence_store_and_packets_include_app_owned_fund_snapshot(tmp_path: Path) -> None:
    provider = FakeAIProvider()
    security_id = "CN:SSE:512480:FUND"
    with TestClient(create_app(tmp_path, automatic_updates=False, ai_provider=provider)) as client:
        with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
            connection.execute(
                "INSERT INTO fund_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "fund-snapshot-1",
                    security_id,
                    "2026-07-17",
                    "天天基金公开档案",
                    json.dumps({"name": "半导体ETF", "holdings_periods": [{"period": "2026Q1"}]}),
                    "2026-07-17T10:00:00+00:00",
                ),
            )
            connection.commit()
        thread = client.post(
            "/api/ai/chats",
            json={"security_id": security_id, "role_id": "buffett", "title": "基金证据"},
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "护城河是否仍然成立？", "role_id": "buffett"},
        )

    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        stored = connection.execute(
            "SELECT evidence_id, value_json FROM research_evidence_records "
            "WHERE domain = 'fund-portfolio-evidence'"
        ).fetchall()
        packet_ids = [
            evidence_id
            for (payload,) in connection.execute(
                "SELECT evidence_ids_json FROM research_evidence_packets WHERE role_id = 'buffett'"
            ).fetchall()
            for evidence_id in json.loads(payload)
        ]

    assert stored
    assert any("2026Q1" in value_json for _evidence_id, value_json in stored)
    assert any(evidence_id in packet_ids for evidence_id, _value_json in stored)


class FakeResearchSkillLayer:
    def catalog(self) -> list[dict[str, str]]:
        return [
            {
                "skill_id": "supplemental-company-evidence",
                "name": "测试证据",
                "description": "测试",
            }
        ]

    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        assert security_id == "CN:SSE:600519:STOCK"
        assert question == "护城河是否仍然成立？"
        assert role_id == "buffett"
        return [
            {
                "skill_id": "supplemental-company-evidence",
                "name": "测试证据",
                "description": "测试",
                "status": "partial",
                "gaps": ["管理层激励"],
                "evidence": [
                    {
                        "evidence_id": "EVIDENCE-SKILL-test",
                        "kind": "test",
                        "value": {"operating_cash_flow": 100},
                        "as_of": "2026-06-30",
                        "provider": "fixture",
                        "source_ref": "https://example.test/evidence",
                    }
                ],
            }
        ]


def test_chat_skill_tool_events_and_evidence_are_persisted_in_one_timeline(tmp_path: Path) -> None:
    provider = FakeAIProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=FakeResearchSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={"security_id": "CN:SSE:600519:STOCK", "role_id": "buffett", "title": "技能测试"},
        ).json()
        reply = client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "护城河是否仍然成立？", "role_id": "buffett"},
        ).json()
        restored = client.get(f"/api/ai/chats/{thread['thread_id']}").json()

    assert reply["cited_evidence_ids"] == ["EVIDENCE-SKILL-test"]
    assert reply["sources"] == [
        {"name": "测试证据", "url": "https://example.test/evidence", "as_of": "2026-06-30"}
    ]
    assert [event["event_type"] for event in restored["events"]] == [
        "message.completed",
        "tool.started",
        "tool.completed",
        "context.completed",
        "blackboard.completed",
        "message.completed",
    ]
    assert restored["events"][2]["payload"]["gaps"] == ["管理层激励"]


class EngineeringOutputProvider(FakeAIProvider):
    def __init__(self) -> None:
        super().__init__()
        self.messages_seen: list[list[dict[str, str]]] = []

    def chat(
        self,
        *,
        role: dict[str, object],
        messages: list[dict[str, str]],
        context: str,
        use_runtime_market_skill: bool = False,
    ) -> dict[str, object]:
        self.messages_seen.append(messages)
        return {
            "content": "**结论**：依据 EVIDENCE-SKILL-test，仍需核实。",
            "cited_evidence_ids": ["EVIDENCE-SKILL-test"],
            "assumptions": [],
            "unknowns": [],
        }


def test_each_turn_excludes_old_chat_and_clearing_permanently_deletes_the_thread(tmp_path: Path) -> None:
    provider = EngineeringOutputProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=FakeResearchSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={"security_id": "CN:SSE:600519:STOCK", "role_id": "buffett", "title": "隔离测试"},
        ).json()
        first = client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "护城河是否仍然成立？", "role_id": "buffett"},
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "护城河是否仍然成立？", "role_id": "buffett"},
        )
        archived = client.post(f"/api/ai/chats/{thread['thread_id']}/archive")
        remaining = client.get("/api/ai/chats?security_id=CN%3ASSE%3A600519%3ASTOCK").json()

    assert all(len(messages) == 1 for messages in provider.messages_seen)
    assert "EVIDENCE" not in first["content"]
    assert "**结论**" in first["content"]  # UI renders this as <strong>, not literal markers.
    assert archived.json() == {"archived": True}
    assert remaining == []
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM research_threads WHERE thread_id = ?", (thread["thread_id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM research_events WHERE thread_id = ?", (thread["thread_id"],)
            ).fetchone()[0]
            == 0
        )


def test_investor_visible_reply_rewrites_internal_research_terms() -> None:
    reply = ResearchChatStore._clean_reply(
        {
            "content": (
                "统一编辑 Claim Board；未提供 portfolio-risk-evidence.ledger_entries；"
                "全部专家未形成 Expert State；请检查 Shared Blackboard 和 Domain Packet；"
                "'utf-8' codec can't decode byte 0x8b in position 1: invalid start byte"
            ),
            "unknowns": ["empty_order_book", "execution-liquidity-evidence.order_book"],
        }
    )
    assert reply["unknowns"] == ["当前公开行情未返回有效买卖盘", "相关研究证据"]

    assert reply["content"] == (
        "统一编辑 已核验研究结论；未提供 本地持仓账本明细；"
        "全部专家未形成 专家研究结论；请检查 共享事实清单 和 分主题证据材料；"
        "公开资料暂未成功解析"
    )
    for internal_term in (
        "Claim Board",
        "portfolio-risk-evidence",
        "ledger_entries",
        "Expert State",
        "Blackboard",
        "Domain Packet",
        "codec can't decode",
    ):
        assert internal_term not in reply["content"]


def test_historical_assistant_events_are_sanitized_when_loaded(tmp_path: Path) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        service = ResearchChatStore(vault, FakeAIProvider())
        thread = service.create(
            security_id="CN:SSE:600519:STOCK",
            role_id="buffett",
            title="历史报告",
            mode="committee",
        )
        service._append_event(
            str(thread["thread_id"]),
            None,  # type: ignore[arg-type]
            "assistant",
            "report_editor",
            {
                "content": "仅统一 Claim Board；未提供 portfolio-risk-evidence.ledger_entries。",
                "coverage_gaps": ["empty_order_book"],
                "report": "检查 Shared Blackboard。",
            },
            event_type="report.completed",
        )
        service._append_event(
            str(thread["thread_id"]),
            None,  # type: ignore[arg-type]
            "system",
            "evidence_collector",
            {
                "content": "仍待核实：'utf-8' codec can't decode byte 0x8b in position 1: invalid start byte",
                "gaps": [
                    "已生成行业候选可比公司及同日估值；业务可比性仍需用户确认",
                    "600519交易成本代理未形成",
                ],
            },
            event_type="tool.completed",
        )
        vault.connection.commit()

        events = service.get(str(thread["thread_id"]))["events"]
        payload = events[0]["payload"]

    assert payload["content"] == "仅统一 已核验研究结论；未提供 本地持仓账本明细。"
    assert payload["coverage_gaps"] == ["当前公开行情未返回有效买卖盘"]
    assert payload["report"] == "检查 共享事实清单。"
    assert events[1]["payload"]["content"] == "仍待核实：公开资料暂未成功解析"
    assert events[1]["payload"]["gaps"] == [
        "该历史轮次仅生成候选可比公司；新分析会按行业与经营特征自动评估业务相似度",
        "该历史轮次未形成贵州茅台交易成本估算；新分析会重新计算",
    ]


def test_completed_evidence_removes_stale_expert_gap_claims() -> None:
    reply = ResearchChatStore._clean_reply(
        {
            "content": (
                "- 缺少腾讯控股本轮完整财务质量证据。\n"
                "- 腾讯仍缺港股实时盘口、标准化财务质量、分部现金流与资本配置原文。\n"
                "- 真实申赎压力仍不可验证。"
            ),
            "unknowns": [
                "尚缺A股全市场7月22日宽度、涨跌停和板块资金数据。",
                "腾讯控股是否能补齐港股实时盘口、最新财务质量与分部现金流？",
                "基金真实申赎压力仍不可验证。",
            ],
            "assumptions": [
                "腾讯港股汇率、真实数量和当前市值未在本轮包中形成完整统一口径。",
                "本轮未补齐腾讯财务质量，不能把量价弱势归因为基本面。",
                "估值为披露EPS与市场报价的代理口径，缺少历史估值分位与同行完整比较。",
            ],
            "gaps": [
                "缺少A股全市场7月22日涨跌家数、涨停/跌停、连板梯队和成交额龙头，无法判断短线主线扩散。",
                "缺少各持仓真实成交数量、券商成本、实时汇率和完整市值口径，无法计算精确仓位权重与盈亏。",
                "腾讯控股仍缺本轮可验证的与分部现金流证据。",
                "腾讯控股缺少本轮完整标准化三表、分业务利润率、现金流与资本配置证据。",
                "本地持仓缺少券商确认数量、统一汇率和完整实时市值，组合权重只能按账本投入金额条件化处理。",
                "腾讯控股缺少本轮标准化三表、现金流、分部利润和资本配置证据，无法按欧奈尔基本面质量闭环评价。",
                "本地持仓缺少券商真实成交数量、实时市值、汇率和完整组合风险贡献，现金比例与权重判断仍以账本金额为主。",
                "缺少A股涨跌家数、连板梯队、涨停/跌停扩散、行业相对强度排名和个股RS排名，欧奈尔市场确认信号仍不完整。",
                "缺少券商确认的真实持仓数量、成交价、港股买入汇率和统一实时市值，组合风险只能条件化。",
                "需补齐公司盈利质量证据。",
                "腾讯控股与交银优择回报C是否需要补齐最新财务/基金穿透证据，以确认科技暴露是否重复集中？",
                "腾讯控股与其他非茅台个股是否有同等完整的财务质量、现金流和估值证据包？",
                "腾讯控股、贵州茅台等核心持仓的本轮财务质量、现金流、估值分位和反方资料未在本专家证据包中闭环。",
                "本轮未提供腾讯完整财务质量、分业务现金流和管理层资本配置证据。",
                "A股主要宽基指数的同口径7月22日连续趋势、市场宽度、涨跌停扩散未在当前包中完整呈现。",
                "各持仓真实成交数量、成本、港股汇率、基金份额与统一实时市值仍需券商或本地账本进一步补齐。",
                "需补齐真实成交数量、港股汇率、基金份额和实时市值后才能计算精确组合风险权重",
                "在统一现金、港股汇率、持仓数量和实时市值后，用户组合中贵州茅台真实权重与风险预算是多少？",
                "用户完整组合的实时市值权重、现金比例、港股汇率口径和压力期流动性仍不可获得。",
                "需要统一港股汇率、基金和现金口径",
                "需要真实成交数量与当前市值，而非仅投入成本",
                "统一港股汇率、实时市值和现金后，茅台在总资产中的真实风险权重是多少？",
                "持仓市值、港股汇率、现金比例和实时总资产口径尚未统一",
                "完整组合实时市值、港股汇率、现金比例和压力期流动性证据是否足以评估集中度风险？",
                "用户包含现金、港股和全部证券的统一实时组合权重与集中度是多少？",
                "当前账本可证明投入记录和现金余额，但缺少统一实时市值、港股汇率和完整组合权重口径。",
                "在统一现金、港股汇率、基金和全部证券市值后，茅台真实组合权重和机会成本是多少？",
                "持仓为本地账本投入金额口径；未统一现金、港股汇率、实时市值和完整资产权重。",
                "用户完整组合按实时市值、汇率和现金统一口径后的茅台权重和集中风险是多少？",
                "需统一持仓数量、实时价格、汇率与现金口径后才能计算组合权重和集中度。",
                "完整组合实时市值、现金、港股汇率统一后，贵州茅台真实风险权重是多少？",
                "账本为唯一权威持仓来源，但未提供完整实时市值、港股汇率和现金统一口径。",
            ],
        },
        {
            "company-financial-quality",
            "market-context-evidence",
            "portfolio-risk-evidence",
            "security-valuation-evidence",
        },
    )

    assert "完整财务质量" not in reply["content"]
    assert "标准化财务质量" not in reply["content"]
    assert "7月22日宽度" not in "；".join(reply["unknowns"])
    assert "港股实时盘口" in reply["content"]
    assert "分部现金流" in reply["unknowns"][0]
    assert reply["unknowns"][-1] == "基金真实申赎压力仍不可验证。"
    assert reply["assumptions"] == [
        "组合现金、港股汇率、推导数量、市值与权重已形成统一估算；"
        "真实成交数量、费用、券商确认权重和压力期流动性仍不可得。",
        "估值为披露EPS与市场报价的代理口径。",
    ]
    joined_gaps = "；".join(reply["gaps"])
    assert "组合现金、港股汇率、推导数量、市值与权重已形成统一估算" in joined_gaps
    assert "真实成交数量、费用、券商确认权重和压力期流动性仍不可得" in joined_gaps
    assert "未统一现金" not in joined_gaps
    assert "缺少统一实时市值" not in joined_gaps
    assert "需统一持仓数量" not in joined_gaps
    assert "业务相似度" not in joined_gaps
    assert "发行人原文层面的分业务利润率" in joined_gaps


def test_expert_update_drops_unknown_citations_without_dropping_the_expert() -> None:
    cleaned = ResearchChatStore._sanitize_structured_update_evidence(
        {
            "claims": [
                {
                    "claim_id": "mixed",
                    "claim": "有一条有效引用",
                    "status": "supported",
                    "supporting_evidence_ids": ["KNOWN", "MISTYPED"],
                    "contradicting_evidence_ids": [],
                    "confidence": "high",
                    "conditions": [],
                },
                {
                    "claim_id": "unknown-only",
                    "claim": "只有错误引用",
                    "status": "supported",
                    "supporting_evidence_ids": ["MISTYPED"],
                    "contradicting_evidence_ids": [],
                    "confidence": "high",
                    "conditions": [],
                },
            ],
            "requirements": [
                {"requirement": "覆盖", "evidence_ids": ["KNOWN", "MISTYPED"]}
            ],
        },
        {"KNOWN"},
    )

    assert cleaned["claims"][0]["supporting_evidence_ids"] == ["KNOWN"]
    assert cleaned["claims"][0]["status"] == "supported"
    assert cleaned["claims"][1]["status"] == "conditional"
    assert cleaned["requirements"][0]["evidence_ids"] == ["KNOWN"]


class FailingResearchSkillLayer(FakeResearchSkillLayer):
    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        raise RuntimeError("公开数据源暂时不可用")


def test_skill_source_failure_becomes_a_visible_gap_without_losing_the_chat_turn(tmp_path: Path) -> None:
    provider = FakeAIProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=FailingResearchSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={"security_id": "CN:SSE:600519:STOCK", "role_id": "buffett", "title": "失败测试"},
        ).json()
        reply = client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "护城河是否仍然成立？", "role_id": "buffett"},
        )
        restored = client.get(f"/api/ai/chats/{thread['thread_id']}").json()

    assert reply.status_code == 200
    failed = next(event for event in restored["events"] if event["event_type"] == "tool.completed")
    assert failed["payload"]["status"] == "failed"
    assert failed["payload"]["gaps"] == ["公开数据源暂时不可用"]


class CommitteeProvider(FakeAIProvider):
    def __init__(self) -> None:
        super().__init__()
        self.roles_seen: list[str] = []
        self.skill_flags: list[bool] = []

    def chat(
        self,
        *,
        role: dict[str, object],
        messages: list[dict[str, str]],
        context: str,
        use_runtime_market_skill: bool = False,
    ) -> dict[str, object]:
        self.chat_calls += 1
        self.roles_seen.append(str(role["role_id"]))
        self.skill_flags.append(use_runtime_market_skill)
        return {
            "content": f"{role['name']}已基于证据完成分析。",
            "cited_evidence_ids": ["EVIDENCE-SKILL-committee"],
            "assumptions": [],
            "unknowns": ["尚缺一项条件证据"] if role["role_id"] != "report_editor" else [],
        }


class CommitteeSkillLayer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def catalog(self) -> list[dict[str, str]]:
        return [
            {"skill_id": "market-context-evidence", "name": "投委会证据", "description": "测试"}
        ]

    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        self.calls.append((role_id, question))
        return [
            {
                "skill_id": "market-context-evidence",
                "name": "投委会证据",
                "status": "completed",
                "gaps": [],
                "evidence": [
                    {
                        "evidence_id": "EVIDENCE-SKILL-committee",
                        "kind": "committee",
                        "value": {"role": role_id},
                        "as_of": "2026-07-18",
                        "provider": "fixture",
                        "source_ref": "https://example.test/committee",
                    }
                ],
            }
        ]


def test_fund_committee_auto_assigns_roles_and_persists_report(tmp_path: Path) -> None:
    provider = CommitteeProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=CommitteeSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "CN:SSE:512480:FUND",
                "role_id": "general",
                "mode": "committee",
                "title": "基金深度复盘",
            },
        ).json()
        accepted = client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={
                "content": "请深度复盘这只基金的持仓结构、流动性压力和组合风险",
                "role_id": "general",
            },
        ).json()
        assert accepted["status"] == "running"
        deadline = time.monotonic() + 3
        while True:
            restored = client.get(f"/api/ai/chats/{thread['thread_id']}").json()
            if restored["active_run"]["status"] == "completed":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

    plan = next(item for item in restored["events"] if item["event_type"] == "plan.completed")
    assert plan["payload"]["selected_roles"] == [
        "达利欧",
        "卡拉曼",
        "西蒙斯",
        "芒格",
        "张坤",
        "巴菲特",
    ]
    assert set(provider.roles_seen) >= {
        "dalio",
        "klarman",
        "simons",
        "munger",
        "zhang_kun",
        "buffett",
    }
    assert provider.roles_seen[-1] == "report_editor"
    assert provider.skill_flags
    assert all(flag is False for flag in provider.skill_flags)
    report = next(item for item in restored["events"] if item["event_type"] == "report.completed")
    assert report["payload"]["report"] is True
    assert any(item["event_type"] == "analysis.started" for item in restored["events"])
    assert any(item["event_type"] == "reporting.started" for item in restored["events"])
    assert any(item["event_type"] == "conflicts.completed" for item in restored["events"])
    assert any(item["event_type"] == "risk_review.completed" for item in restored["events"])
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_tasks").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM research_reports").fetchone()[0] == 1


def test_investment_assistant_uses_app_packets_without_expert_runtime_skill(tmp_path: Path) -> None:
    provider = CommitteeProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=CommitteeSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "CN:SSE:600519:STOCK",
                "role_id": "buffett",
                "mode": "assistant",
                "title": "快速问题",
            },
        ).json()
        response = client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={
                "content": "现金流如何？",
                "role_id": "buffett",
            },
        )

    assert response.status_code == 200
    assert provider.skill_flags == [False, False]


def test_committee_redirects_a_simple_question_without_model_calls(tmp_path: Path) -> None:
    provider = CommitteeProvider()
    with TestClient(create_app(tmp_path, automatic_updates=False, ai_provider=provider)) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "CN:SSE:600519:STOCK",
                "role_id": "general",
                "mode": "committee",
                "title": "简单问题",
            },
        ).json()
        reply = client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={
                "content": "现在的市盈率是多少？",
                "role_id": "general",
            },
        ).json()

    assert reply["suggested_mode"] == "assistant"
    assert provider.chat_calls == 0


class BlockingCommitteeProvider(CommitteeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def chat(self, **kwargs: object) -> dict[str, object]:
        self.started.set()
        assert self.release.wait(timeout=3)
        return super().chat(**kwargs)  # type: ignore[arg-type]


def test_committee_returns_immediately_and_exposes_durable_progress(tmp_path: Path) -> None:
    provider = BlockingCommitteeProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=CommitteeSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "CN:SSE:512480:FUND",
                "role_id": "general",
                "mode": "committee",
                "title": "后台投委会",
            },
        ).json()
        started_at = time.monotonic()
        accepted = client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={
                "content": "请深度复盘这只基金的持仓、流动性和组合风险",
                "role_id": "general",
            },
        )
        assert time.monotonic() - started_at < 0.5
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "running"
        assert provider.started.wait(timeout=1)

        progress = client.get(f"/api/ai/chats/{thread['thread_id']}").json()
        assert progress["active_run"]["status"] == "running"
        assert any(item["event_type"] == "analysis.started" for item in progress["events"])
        assert any(item["event_type"] == "expert.started" for item in progress["events"])
        provider.release.set()

        deadline = time.monotonic() + 3
        while True:
            completed = client.get(f"/api/ai/chats/{thread['thread_id']}").json()
            if completed["active_run"]["status"] == "completed":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)


def test_chat_timeout_message_is_not_mislabeled_as_quick_note(tmp_path: Path) -> None:
    provider = CodexAppServerProvider(tmp_path / "runtime", executable="/bin/false", timeout=0.01)
    try:
        provider._wait_for_turn("thread", "turn", operation="深度研究", timeout=0.01)
    except Exception as error:
        assert str(error) == "Codex 深度研究生成超时"
    else:
        raise AssertionError("expected timeout")


def test_app_server_uses_authoritative_completed_agent_message_when_deltas_are_absent(
    tmp_path: Path,
) -> None:
    provider = CodexAppServerProvider(tmp_path / "runtime", executable="/bin/false")
    with provider._condition:
        provider._notifications.extend(
            [
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"type": "agentMessage", "id": "item-1", "text": '{"content":"ok"}'},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1", "status": "completed"},
                    },
                },
            ]
        )

    assert provider._wait_for_turn("thread-1", "turn-1", operation="深度研究") == '{"content":"ok"}'


class RetryOnceCommitteeProvider(CommitteeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.attempts: dict[str, int] = {}
        self.attempt_lock = threading.Lock()

    def chat(self, **kwargs: object) -> dict[str, object]:
        role = kwargs["role"]
        assert isinstance(role, dict)
        role_id = str(role["role_id"])
        with self.attempt_lock:
            self.attempts[role_id] = self.attempts.get(role_id, 0) + 1
            attempt = self.attempts[role_id]
        if role_id == "duan_yongping" and attempt == 1:
            raise AIUnavailableError("Codex 返回的研究回复格式无效")
        return super().chat(**kwargs)  # type: ignore[arg-type]


def test_committee_retries_one_transient_invalid_expert_response(tmp_path: Path) -> None:
    provider = RetryOnceCommitteeProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=CommitteeSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "CN:SSE:600519:STOCK",
                "role_id": "general",
                "mode": "committee",
                "title": "公司深度复盘",
            },
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={
                "content": "请深度复盘公司的商业质量、估值、护城河和组合风险",
                "role_id": "general",
            },
        )
        deadline = time.monotonic() + 3
        while True:
            restored = client.get(f"/api/ai/chats/{thread['thread_id']}").json()
            if restored["active_run"]["status"] != "running":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

    assert restored["active_run"]["status"] == "completed"
    assert provider.attempts["duan_yongping"] >= 2
    assert not any(
        event["event_type"] == "expert.failed" and event["actor_id"] == "duan_yongping"
        for event in restored["events"]
    )
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT attempt FROM research_tasks WHERE assigned_role = 'duan_yongping'"
            ).fetchone()[0]
            >= 2
        )


def test_normal_assistant_deep_research_uses_app_owned_packet_context(tmp_path: Path) -> None:
    provider = CommitteeProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=CommitteeSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "CN:SSE:600519:STOCK",
                "role_id": "buffett",
                "mode": "assistant",
                "title": "单专家深度复盘",
            },
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={
                "content": "请深度复盘当前持仓逻辑和主要风险",
                "role_id": "buffett",
            },
        )

    assert provider.roles_seen == ["buffett", "buffett"]
    assert provider.skill_flags == [False, False]


class PartialSkillLayer(CommitteeSkillLayer):
    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        result = super().run(security_id=security_id, question=question, role_id=role_id)
        result[0]["status"] = "partial"
        result[0]["gaps"] = ["仍需补充公开数据"]
        return result


def test_normal_assistant_routes_remaining_gaps_through_coordinator(tmp_path: Path) -> None:
    provider = CommitteeProvider()
    skill_layer = PartialSkillLayer()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=skill_layer,
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "CN:SSE:600519:STOCK",
                "role_id": "buffett",
                "mode": "assistant",
                "title": "补证",
            },
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={
                "content": "你怎么看这家公司？",
                "role_id": "buffett",
            },
        )

    # The coordinator closes a duplicate supplemental result deterministically;
    # only materially new evidence requires another reasoning call.
    assert provider.skill_flags == [False]
    assert len(skill_layer.calls) == 2
    assert skill_layer.calls[-1][0] == "general"


def test_external_store_is_lossless_and_packets_preserve_all_projected_evidence_ids(
    tmp_path: Path,
) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.import_holdings([
            HoldingRecord("a", "CN:SSE:600519:STOCK", "a_share", "10000", "2026-01-08")
        ])
        service = ResearchChatStore(vault, FakeAIProvider())
        marker = "末尾证据-不得截断"
        store = service._evidence_store(
            "MARKET:GLOBAL:OVERVIEW",
            [{
                "skill_id": "portfolio-risk-evidence",
                "name": "组合风险证据",
                "status": "completed",
                "gaps": [],
                "evidence": [{
                    "evidence_id": "EVIDENCE-SKILL-long",
                    "value": {
                        "holding_identities": {"CN:SSE:600519:STOCK": {"symbol": "600519"}},
                        "ledger_entries": [{"security_id": "CN:SSE:600519:STOCK", "tail": marker}],
                        "padding": "x" * 70_000,
                    },
                }],
            }],
        )

        raw = next(record for record in store.records if record.evidence_id == "EVIDENCE-SKILL-long")
        manifest = EvidenceManifest.from_store(store)
        plan = RoleEvidencePlanner().plan(
            role_id="general",
            question="组合风险",
            manifest=manifest,
        )
        packets = PacketEngine().build(
            role_id="general",
            objective="组合风险",
            records=[store.get(evidence_id) for evidence_id in plan.evidence_ids],
        )
        packet_ids = [evidence_id for packet in packets for evidence_id in packet.evidence_ids]
        holdings = next(record for record in store.records if record.subtype == "holdings_authority")

        assert marker in json.dumps(raw.value, ensure_ascii=False)
        assert len(json.dumps(raw.value, ensure_ascii=False)) > 70_000
        assert sorted(packet_ids) == sorted(plan.evidence_ids)
        assert len(packet_ids) == len(set(packet_ids))
        assert holdings.value["allowed_security_ids"] == ["CN:SSE:600519:STOCK"]
        assert holdings.value["external_profiles_forbidden"] is True


def test_role_packets_do_not_refill_budget_with_evidence_outside_original_role_plan(
    tmp_path: Path,
) -> None:
    def record(evidence_id: str, token_estimate: int, observed_at: str) -> EvidenceRecord:
        return EvidenceRecord.create(
            evidence_id=evidence_id,
            security_id="MARKET:GLOBAL:OVERVIEW",
            domain="market-context-evidence",
            subtype="fixture",
            entity_id="MARKET:GLOBAL:OVERVIEW",
            as_of="2026-07-22",
            observed_at=observed_at,
            source_tier="verified_original",
            provider="fixture",
            source_ref="fixture://market",
            quality_status="available",
            value={"fact": evidence_id},
            compact_text=f"[{evidence_id}] 市场事实",
            token_estimate=token_estimate,
        )

    with Vault(tmp_path / "vault.sqlite3") as vault:
        service = ResearchChatStore(vault, FakeAIProvider())
        thread = service.create(
            security_id="MARKET:GLOBAL:OVERVIEW",
            role_id="general",
            title="角色证据计划回归",
            mode="committee",
        )
        run_id = "RUN-ROLE-PLAN"
        vault.connection.execute(
            "INSERT INTO research_runs VALUES (?, ?, 'test', 'running', 'evidence', '{}', NULL, ?, NULL, NULL)",
            (run_id, thread["thread_id"], "2026-07-22T00:00:00+00:00"),
        )
        store = EvidenceStore().ingest(
            [
                record("EVIDENCE-OUTSIDE-PLAN", 90_000, "2026-07-22T10:00:00+00:00"),
                record("EVIDENCE-ALLOWED", 100, "2026-07-21T10:00:00+00:00"),
            ]
        )

        packets = service._role_packets(
            run_id=run_id,
            role_id="dalio",
            question="盘后市场复盘",
            store=store,
            allowed_evidence_ids={"EVIDENCE-ALLOWED"},
        )

        assert [evidence_id for packet in packets for evidence_id in packet.evidence_ids] == [
            "EVIDENCE-ALLOWED"
        ]


def test_evidence_persistence_reuses_canonical_id_for_cross_run_duplicate_content(
    tmp_path: Path,
) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        service = ResearchChatStore(vault, FakeAIProvider())
        thread = service.create(
            security_id="MARKET:GLOBAL:OVERVIEW",
            role_id="general",
            title="跨运行证据去重",
            mode="committee",
        )
        for run_id in ("RUN-FIRST", "RUN-SECOND"):
            vault.connection.execute(
                "INSERT INTO research_runs VALUES (?, ?, 'test', 'running', 'evidence', '{}', NULL, ?, NULL, NULL)",
                (run_id, thread["thread_id"], "2026-07-22T00:00:00+00:00"),
            )

        original = EvidenceRecord.create(
            evidence_id="EVIDENCE-ORIGINAL",
            security_id="MARKET:GLOBAL:OVERVIEW",
            domain="market-context-evidence",
            subtype="fixture",
            entity_id="MARKET:GLOBAL:OVERVIEW",
            as_of="2026-07-22",
            observed_at="2026-07-22T10:00:00+00:00",
            source_tier="verified_original",
            provider="fixture",
            source_ref="fixture://market",
            quality_status="available",
            value={"index": 1},
            compact_text="原始市场事实",
        )
        duplicate = replace(
            original,
            evidence_id="EVIDENCE-NEW-RUN-ID",
            observed_at="2026-07-22T11:00:00+00:00",
            compact_text="同一市场事实的刷新投影",
        )

        first = service._persist_evidence_store("RUN-FIRST", EvidenceStore((original,)))
        second = service._persist_evidence_store("RUN-SECOND", EvidenceStore((duplicate,)))

        assert first.records[0].evidence_id == "EVIDENCE-ORIGINAL"
        assert second.records[0].evidence_id == "EVIDENCE-ORIGINAL"
        assert vault.connection.execute(
            "SELECT COUNT(*) FROM research_evidence_records WHERE content_hash = ?",
            (original.content_hash,),
        ).fetchone()[0] == 1
        assert vault.connection.execute(
            "SELECT evidence_id FROM research_evidence_links WHERE run_id = 'RUN-SECOND'"
        ).fetchone()[0] == "EVIDENCE-ORIGINAL"


def test_research_service_rejects_holding_claims_for_symbols_outside_local_ledger(
    tmp_path: Path,
) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        vault.import_holdings([
            HoldingRecord("a", "CN:SSE:600519:STOCK", "a_share", "10000", "2026-01-08")
        ])
        service = ResearchChatStore(vault, FakeAIProvider())
        with pytest.raises(AIUnavailableError, match="TSLA"):
            service._assert_ledger_holding_claims({"content": "用户持仓 TSLA 需要继续观察。"})


def test_research_service_does_not_treat_product_name_as_an_unauthorized_symbol(
    tmp_path: Path,
) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        service = ResearchChatStore(vault, FakeAIProvider())
        service._assert_ledger_holding_claims(
            {"content": "Invest Vault 本地持仓账本是本轮唯一持仓权威。"}
        )


def test_user_can_stop_an_active_assistant_generation(tmp_path: Path) -> None:
    class StoppableProvider(FakeAIProvider):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.cancelled = threading.Event()

        def begin_operation(self, operation_id: str) -> None:
            self.operation_id = operation_id

        def end_operation(self, operation_id: str) -> None:
            assert operation_id == self.operation_id

        def cancel_operation(self, operation_id: str) -> None:
            assert operation_id == self.operation_id
            self.cancelled.set()

        def chat(self, **_kwargs: object) -> dict[str, object]:
            self.started.set()
            self.cancelled.wait(timeout=3)
            raise AIUnavailableError("interrupted")

    class EmptySkillLayer:
        def catalog(self) -> list[dict[str, str]]:
            return []

        def run(self, **_kwargs: object) -> list[dict[str, object]]:
            return []

    provider = StoppableProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=EmptySkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={"security_id": "CN:SSE:600519:STOCK", "role_id": "buffett", "title": "停止测试"},
        ).json()
        response: dict[str, object] = {}

        def send() -> None:
            response["value"] = client.post(
                f"/api/ai/chats/{thread['thread_id']}/messages",
                json={"content": "请分析这家公司", "role_id": "buffett"},
            )

        worker = threading.Thread(target=send)
        worker.start()
        assert provider.started.wait(timeout=2)
        stopped = client.post(f"/api/ai/chats/{thread['thread_id']}/cancel")
        worker.join(timeout=3)
        detail = client.get(f"/api/ai/chats/{thread['thread_id']}").json()

    assert stopped.status_code == 200
    assert stopped.json()["status"] == "cancelled"
    assert detail["active_run"]["status"] == "cancelled"
    assert any(event["event_type"] == "workflow.cancelled" for event in detail["events"])
    assert not worker.is_alive()
