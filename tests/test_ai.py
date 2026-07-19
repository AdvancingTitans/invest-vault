import json
import sqlite3
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from invest_vault.ai import (
    AIUnavailableError,
    CodexAppServerProvider,
    is_investment_question,
    market_report_role,
)
from invest_vault.ai_roles import committee_plan, get_role
from invest_vault.ai_skills import SKILL_CATALOG
from invest_vault.api import create_app


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


class MarketReportProvider(FakeAIProvider):
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
        assert role["role_id"] == "market_report"
        assert "market_overview" in context
        assert "ledger_entries" in context
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
    assert "仅生成盘前、盘中或盘后" in str(role["focus"])
    assert plan["scene"] == "market"
    assert len(plan["roles"]) == 6
    assert plan["roles"] == [
        "livermore",
        "buffett",
        "munger",
        "duan_yongping",
        "zhang_kun",
        "graham",
    ]
    assert plan["skill_version"] == "4.14.0"


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
        assert [event["actor_type"] for event in restored["events"]] == ["user", "system", "assistant"]
        assert restored["events"][1]["event_type"] == "context.completed"
        assert restored["events"][2]["payload"]["unknowns"] == ["长期自由现金流证据不足"]


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


def test_financial_scope_gate_keeps_common_short_investment_questions() -> None:
    assert is_investment_question("现在能买吗？")
    assert is_investment_question("现金流为什么变差？")
    assert not is_investment_question("帮我写一首生日诗")


def test_chat_context_includes_app_owned_financial_and_fund_snapshots(tmp_path: Path) -> None:
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

    assert "fund-portfolio-evidence" in provider.last_context
    assert "2026Q1" in provider.last_context


class FakeResearchSkillLayer:
    def catalog(self) -> list[dict[str, str]]:
        return [{"skill_id": "test-skill", "name": "测试证据", "description": "测试"}]

    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        assert security_id == "CN:SSE:600519:STOCK"
        assert question == "护城河是否仍然成立？"
        assert role_id == "buffett"
        return [
            {
                "skill_id": "test-skill",
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
    def catalog(self) -> list[dict[str, str]]:
        return [{"skill_id": "committee-evidence", "name": "投委会证据", "description": "测试"}]

    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        return [
            {
                "skill_id": "committee-evidence",
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
    assert set(provider.roles_seen[:6]) == {
        "dalio",
        "klarman",
        "simons",
        "munger",
        "zhang_kun",
        "buffett",
    }
    assert provider.roles_seen[-1] == "report_editor"
    assert provider.skill_flags == [True, True, True, True, True, True, True]
    report = next(item for item in restored["events"] if item["event_type"] == "report.completed")
    assert report["payload"]["report"] is True
    assert any(item["event_type"] == "analysis.started" for item in restored["events"])
    assert any(item["event_type"] == "reporting.started" for item in restored["events"])
    assert any(item["event_type"] == "conflicts.completed" for item in restored["events"])
    assert any(item["event_type"] == "risk_review.completed" for item in restored["events"])
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_tasks").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM research_reports").fetchone()[0] == 1


def test_every_investment_assistant_turn_loads_bundled_stock_analysis_skill(tmp_path: Path) -> None:
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
    assert provider.skill_flags == [True]


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
    assert provider.attempts["duan_yongping"] == 2
    assert not any(
        event["event_type"] == "expert.failed" and event["actor_id"] == "duan_yongping"
        for event in restored["events"]
    )
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT attempt FROM research_tasks WHERE assigned_role = 'duan_yongping'"
            ).fetchone()[0]
            == 2
        )


def test_normal_assistant_can_use_runtime_market_skill_for_deep_research(tmp_path: Path) -> None:
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

    assert provider.roles_seen == ["buffett"]
    assert provider.skill_flags == [True]


class PartialSkillLayer(CommitteeSkillLayer):
    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        result = super().run(security_id=security_id, question=question, role_id=role_id)
        result[0]["status"] = "partial"
        result[0]["gaps"] = ["仍需补充公开数据"]
        return result


def test_normal_assistant_loads_stock_analysis_when_evidence_pass_still_has_gaps(tmp_path: Path) -> None:
    provider = CommitteeProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=PartialSkillLayer(),
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

    assert provider.skill_flags == [True]
