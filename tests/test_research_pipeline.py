import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invest_vault.api import create_app
from invest_vault.evidence_orchestration import QualityMetrics, estimate_tokens


class StructuredPipelineProvider:
    def __init__(
        self,
        *,
        fail_final_edit: bool = False,
        verbose_open_questions: bool = False,
        claim_repetitions: int = 0,
    ) -> None:
        self.fail_final_edit = fail_final_edit
        self.verbose_open_questions = verbose_open_questions
        self.claim_repetitions = claim_repetitions
        self.structured_calls: list[dict[str, object]] = []
        self.chat_calls: list[dict[str, object]] = []

    def status(self) -> dict[str, object]:
        return {"available": True, "authenticated": True, "provider": "fixture"}

    def start_chatgpt_login(self) -> dict[str, object]:
        return {}

    def logout(self) -> dict[str, object]:
        return {}

    def list_models(self) -> list[dict[str, object]]:
        return []

    def configure_models(self, settings: dict[str, dict[str, str | None]]) -> None:
        self.settings = settings

    def quick_note(self, raw_text: str, security_id: str) -> dict[str, object]:
        raise AssertionError("not used")

    def generate_structured(self, **kwargs: object) -> dict[str, object]:
        self.structured_calls.append(kwargs)
        prompt = str(kwargs["prompt"])
        role = kwargs["role"]
        assert isinstance(role, dict)
        evidence_ids = list(
            dict.fromkeys(re.findall(r'"evidence_id":\s*"(EVIDENCE-[A-Za-z0-9_-]+)"', prompt))
        )
        cited = evidence_ids
        return {
            "claims": [
                {
                    "claim_id": f"{role['role_id']}-{cited[0] if cited else 'UNRESOLVED'}",
                    "claim": f"{role['role_id']}完成本包分析" + "长期证据化判断" * self.claim_repetitions,
                    "status": "supported" if cited else "conditional",
                    "supporting_evidence_ids": cited,
                    "contradicting_evidence_ids": [],
                    "confidence": "medium",
                    "conditions": [],
                }
            ],
            "requirements": [],
            "open_questions": (
                [f"VERBOSE_OPEN_QUESTION_{index}_" + "长期缺口描述" * 100 for index in range(20)]
                if self.verbose_open_questions
                else []
            ),
        }

    def chat(self, **kwargs: object) -> dict[str, object]:
        self.chat_calls.append(kwargs)
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        content = str(messages[-1]["content"])
        if content.startswith("统一编辑") and self.fail_final_edit:
            raise RuntimeError("provider timeout")
        context = str(kwargs["context"])
        cited = list(dict.fromkeys(re.findall(r"EVIDENCE-[A-Za-z0-9_-]+", context)))[:1]
        return {
            "content": f"已完成：{content}",
            "cited_evidence_ids": cited,
            "assumptions": [],
            "unknowns": [],
            "reached_sources": [],
        }

    def close(self) -> None:
        return None


class PipelineSkillLayer:
    def catalog(self) -> list[dict[str, str]]:
        return []

    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        return [
            {
                "skill_id": "company-financial-quality",
                "name": "公司财务质量",
                "status": "completed",
                "gaps": [],
                "evidence": [
                    {
                        "evidence_id": "EVIDENCE-FINANCIAL-PIPELINE",
                        "kind": "financial",
                        "value": {"cash_flow": 31, "tail": "RAW_TAIL_MARKER"},
                        "as_of": "2026-06-30",
                        "provider": "fixture",
                        "source_ref": "https://example.test/financial",
                    }
                ],
            },
            {
                "skill_id": "market-context-evidence",
                "name": "市场量价",
                "status": "completed",
                "gaps": [],
                "evidence": [
                    {
                        "evidence_id": "EVIDENCE-MARKET-PIPELINE",
                        "kind": "market",
                        "value": {"trend": "up"},
                        "as_of": "2026-07-22",
                        "provider": "fixture",
                        "source_ref": "https://example.test/market",
                    }
                ],
            },
            {
                "skill_id": "portfolio-risk-evidence",
                "name": "组合风险",
                "status": "completed",
                "gaps": [],
                "evidence": [
                    {
                        "evidence_id": "EVIDENCE-PORTFOLIO-PIPELINE",
                        "kind": "portfolio",
                        "value": {"hhi": 0.4},
                        "as_of": "2026-07-22",
                        "provider": "fixture",
                        "source_ref": "https://example.test/portfolio",
                    }
                ],
            },
            {
                "skill_id": "framework-readiness",
                "name": "专家证据覆盖检查",
                "status": "completed",
                "gaps": [],
                "evidence": [
                    {
                        "evidence_id": f"EVIDENCE-READINESS-{role_id}",
                        "kind": "framework-readiness",
                        "value": {"role_id": role_id, "requirements": []},
                        "as_of": "2026-07-22",
                        "provider": "fixture",
                        "source_ref": "",
                    }
                ],
            },
        ]


class CompleteEntryPointSkillLayer(PipelineSkillLayer):
    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        results = super().run(security_id=security_id, question=question, role_id=role_id)
        for skill_id in (
            "security-valuation-evidence",
            "supplemental-company-evidence",
            "execution-liquidity-evidence",
        ):
            results.append(
                {
                    "skill_id": skill_id,
                    "name": skill_id,
                    "status": "completed",
                    "gaps": [],
                    "evidence": [
                        {
                            "evidence_id": f"EVIDENCE-{skill_id.upper()}-{role_id}",
                            "kind": skill_id,
                            "value": {
                                "role_id": role_id,
                                "skill_id": skill_id,
                                "status": "available",
                            },
                            "as_of": "2026-07-22",
                            "provider": "fixture",
                            "source_ref": f"https://example.test/{skill_id}",
                        }
                    ],
                }
            )
        return results


class LargeMarketSkillLayer(CompleteEntryPointSkillLayer):
    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        results = super().run(security_id=security_id, question=question, role_id=role_id)
        for result in results:
            if result["skill_id"] == "framework-readiness":
                continue
            for evidence in result["evidence"]:
                evidence["value"] = {
                    "original": evidence["value"],
                    "complete_payload": "完整证据" * 6_000,
                }
        return results


class FailingExpertProvider(StructuredPipelineProvider):
    def generate_structured(self, **kwargs: object) -> dict[str, object]:
        self.structured_calls.append(kwargs)
        raise RuntimeError("expert fixture failure")


class PublishableBoundaryProvider(StructuredPipelineProvider):
    def generate_structured(self, **kwargs: object) -> dict[str, object]:
        self.structured_calls.append(kwargs)
        prompt = str(kwargs["prompt"])
        evidence_ids = list(
            dict.fromkeys(re.findall(r'"evidence_id":\s*"(EVIDENCE-[A-Za-z0-9_-]+)"', prompt))
        )
        support = evidence_ids[:1]
        return {
            "claims": [
                {
                    "claim_id": "PUBLISHABLE-SUPPORTED",
                    "claim": "当前现金流与量价证据共同支持经营和市场状态保持稳定",
                    "status": "supported",
                    "supporting_evidence_ids": support,
                    "contradicting_evidence_ids": [],
                    "confidence": "medium",
                    "conditions": [],
                },
                {
                    "claim_id": "PUBLISHABLE-CONDITIONAL",
                    "claim": "若后续现金流继续改善且价格保持在当前趋势区间，正向状态可以延续",
                    "status": "conditional",
                    "supporting_evidence_ids": support,
                    "contradicting_evidence_ids": [],
                    "confidence": "medium",
                    "conditions": ["下一披露期经营现金流同比改善", "收盘价保持在20日均线上方"],
                },
                {
                    "claim_id": "GAP-AS-CONCLUSION",
                    "claim": "由于长期证据不足，当前只能维持观察",
                    "status": "supported",
                    "supporting_evidence_ids": support,
                    "contradicting_evidence_ids": [],
                    "confidence": "low",
                    "conditions": [],
                },
            ],
            "requirements": [],
            "open_questions": ["长期原始资料仍需补证"],
        }

    def chat(self, **kwargs: object) -> dict[str, object]:
        self.chat_calls.append(kwargs)
        context = str(kwargs["context"])
        cited = list(dict.fromkeys(re.findall(r"EVIDENCE-[A-Za-z0-9_-]+", context)))[:1]
        return {
            "content": (
                "## 可发布结论\n"
                "- 当前现金流与量价证据共同支持经营和市场状态保持稳定。\n"
                "- 若后续现金流继续改善且价格保持在当前趋势区间，正向状态可以延续。\n"
                "- 腾讯执行成本缺少可靠实时五档。\n"
                "- 估值相对自身历史偏低，但不能直接判定为绝对低估。\n"
                "- 当前证据只能支持相对估值偏低，不能支持绝对低估的确定结论。\n"
                "- 历史估值相对偏低，\n"
                "- **历史估值相对偏低，但不能直接判定为绝对低估。**\n"
                "- 高毛利和低杠杆支持商业质量。但质量结论不能外推为现金流持续改善。\n"
                "- 相对估值处于近样本低位，因此不能给出确定低估结论。\n"
                "- 增长放缓且现金流走弱，使高质量不能自动转化为无条件加仓。\n"
                "- 因长期证据不足，当前只能维持观察。"
            ),
            "cited_evidence_ids": cited,
            "assumptions": ["长期证据不足时维持观察"],
            "unknowns": ["长期原始资料仍需补证"],
            "reached_sources": [],
        }


class FailingConflictRevisionProvider(StructuredPipelineProvider):
    def generate_structured(self, **kwargs: object) -> dict[str, object]:
        if str(kwargs["prompt"]).startswith("新增补证触发了"):
            self.structured_calls.append(kwargs)
            raise RuntimeError("supplement revision disconnected")
        return super().generate_structured(**kwargs)


class NewSupplementEvidenceSkillLayer(CompleteEntryPointSkillLayer):
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, security_id: str, question: str, role_id: str = "general") -> list[dict[str, object]]:
        self.calls += 1
        results = super().run(security_id=security_id, question=question, role_id=role_id)
        for result in results:
            if result["skill_id"] == "framework-readiness":
                result["evidence"][0]["value"]["requirements"] = [
                    {
                        "requirement": "外部反证",
                        "evidence_skill": "unavailable-supplement-domain",
                        "status": "missing",
                        "reason": "需要一次真正的统一补证",
                    }
                ]
        if self.calls > 6:
            results.append(
                {
                    "skill_id": "market-context-evidence",
                    "name": "补充市场反证",
                    "status": "completed",
                    "gaps": [],
                    "evidence": [
                        {
                            "evidence_id": "EVIDENCE-SUPPLEMENT-CONFLICT",
                            "kind": "market_conflict",
                            "value": {"trend": "reversed"},
                            "as_of": "2026-07-22",
                            "provider": "fixture",
                            "source_ref": "https://example.test/supplement-conflict",
                        }
                    ],
                }
            )
        return results


class CountingCompleteEntryPointSkillLayer(CompleteEntryPointSkillLayer):
    def __init__(self) -> None:
        self.calls = 0

    def run(
        self,
        *,
        security_id: str,
        question: str,
        role_id: str = "general",
    ) -> list[dict[str, object]]:
        self.calls += 1
        return super().run(security_id=security_id, question=question, role_id=role_id)


def _wait_for_run(client: TestClient, thread_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while True:
        restored = client.get(f"/api/ai/chats/{thread_id}").json()
        if restored["active_run"]["status"] != "running":
            return restored
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_all_three_research_surfaces_publish_only_evidence_supported_conclusions(
    tmp_path: Path,
) -> None:
    provider = PublishableBoundaryProvider()
    forbidden = (
        "证据不足",
        "缺少数据",
        "缺少可靠实时五档",
        "不能直接判定",
        "不能支持",
        "不能外推",
        "不能给出",
        "不能自动转化",
        "无法判断",
        "尚未覆盖",
        "维持观察",
    )
    secret_note = "PRIVATE_NOTE_MUST_NOT_REACH_ANY_MODEL"
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=CompleteEntryPointSkillLayer(),
        )
    ) as client:
        client.post(
            "/api/research/notes",
            json={
                "security_id": "CN:SSE:600519:STOCK",
                "body": secret_note,
            },
        ).raise_for_status()
        scenarios = (
            (
                "CN:SSE:600519:STOCK",
                "buffett",
                "assistant",
                "请基于当前证据分析经营、估值和风险",
            ),
            (
                "MARKET:GLOBAL:OVERVIEW",
                "simons",
                "assistant",
                "结合当前所有可用且完整的证据，生成7月22日盘后报告，并结合我的本地持仓给出条件化观察建议。",
            ),
            (
                "CN:SSE:600519:STOCK",
                "general",
                "committee",
                "请深度复盘财务、市场与组合风险",
            ),
        )
        reports: list[dict[str, object]] = []
        for security_id, role_id, mode, question in scenarios:
            thread = client.post(
                "/api/ai/chats",
                json={
                    "security_id": security_id,
                    "role_id": role_id,
                    "mode": mode,
                    "title": "可发布结论边界",
                },
            ).json()
            client.post(
                f"/api/ai/chats/{thread['thread_id']}/messages",
                json={"content": question, "role_id": role_id},
            ).raise_for_status()
            restored = _wait_for_run(client, thread["thread_id"])
            reports.append(
                next(
                    event["payload"]
                    for event in reversed(restored["events"])
                    if event["actor_type"] == "assistant"
                )
            )

    for report in reports:
        content = str(report["content"])
        assert "当前现金流与量价证据共同支持经营和市场状态保持稳定" in content
        assert "若后续现金流继续改善" in content
        assert "估值相对自身历史偏低" in content
        assert "当前证据只能支持相对估值偏低" in content
        assert "相对估值处于近样本低位" in content
        assert "增长放缓且现金流走弱" in content
        assert not any(term in content for term in forbidden)
        assert not any(line.endswith(("，", ",", "；", ";")) for line in content.splitlines())
        assert not any(re.search(r"[，,；;](?:\*{1,3}|_{1,3})?$", line) for line in content.splitlines())
        assert report["unknowns"] == []
        assert report["assumptions"] == []
        assert report["cited_evidence_ids"]
    assert all(secret_note not in str(call) for call in provider.structured_calls)
    assert all(secret_note not in str(call) for call in provider.chat_calls)
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM research_evidence_records WHERE evidence_id LIKE 'EVIDENCE-NOTE-%'"
        ).fetchone()[0] == 0
        states = [
            json.loads(row[0])
            for row in connection.execute("SELECT state_json FROM research_expert_states")
        ]
    assert any(state["open_questions"] for state in states)


def test_structured_committee_persists_packets_states_metrics_and_sections(tmp_path: Path) -> None:
    provider = StructuredPipelineProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=PipelineSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "CN:SSE:600519:STOCK",
                "role_id": "general",
                "mode": "committee",
                "title": "结构化委员会",
            },
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "请深度复盘财务、市场与组合风险", "role_id": "general"},
        )
        restored = _wait_for_run(client, thread["thread_id"])

    assert restored["active_run"]["status"] == "completed"
    assert provider.structured_calls
    expert_calls = [
        call for call in provider.structured_calls if call["role"]["role_id"] != "evidence_coordinator"
    ]
    coordinator_calls = [
        call for call in provider.structured_calls if call["role"]["role_id"] == "evidence_coordinator"
    ]
    assert all(call["use_runtime_market_skill"] is False for call in expert_calls)
    assert len(expert_calls) == 6
    assert len(coordinator_calls) == 1
    assert coordinator_calls[0]["use_runtime_market_skill"] is True
    final_call = next(
        call for call in provider.chat_calls if str(call["messages"][-1]["content"]).startswith("统一编辑")
    )
    assert "RAW_TAIL_MARKER" not in str(final_call["context"])
    assert estimate_tokens(str(final_call["context"])) < 30_000
    assert len(provider.chat_calls) == 1
    assert not any(
        str(call["messages"][-1]["content"]).startswith("只生成报告章节")
        for call in provider.chat_calls
    )
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_evidence_records").fetchone()[0] >= 4
        duplicate_links = connection.execute(
            """SELECT run_id, evidence_id, relation, COUNT(*) FROM research_evidence_links
            WHERE task_id IS NULL GROUP BY run_id, evidence_id, relation HAVING COUNT(*) > 1"""
        ).fetchall()
        packet_rows = connection.execute(
            """SELECT role_id, COUNT(*) FROM research_evidence_packets
               WHERE packet_id LIKE '%-DOMAIN-%' GROUP BY role_id"""
        ).fetchall()
        state_count = connection.execute(
            "SELECT COUNT(*) FROM research_expert_states"
        ).fetchone()[0]
        assert sum(count for _role, count in packet_rows) <= 18
        assert all(count <= 3 for _role, count in packet_rows)
        assert state_count <= 10
        assert connection.execute("SELECT COUNT(*) FROM research_call_metrics").fetchone()[0] >= 6
        assert connection.execute("SELECT COUNT(*) FROM research_claim_boards").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM research_risk_reviews").fetchone()[0] == 1
        quality = connection.execute(
            "SELECT framework_coverage_json FROM research_call_metrics "
            "WHERE framework_coverage_json IS NOT NULL"
        ).fetchone()
        sections = connection.execute(
            "SELECT status, input_json FROM research_report_sections ORDER BY sequence_number"
        ).fetchall()
        final_metric = connection.execute(
            """SELECT stage, node_id, token_budget, usage_source
               FROM research_call_metrics WHERE stage = 'final_edit'"""
        ).fetchone()
        section_call_count = connection.execute(
            "SELECT COUNT(*) FROM research_call_metrics WHERE stage = 'report_section'"
        ).fetchone()[0]
        performance = connection.execute(
            """SELECT packet_count, semantic_revision_count, section_llm_call_count,
                      final_context_tokens, completion_status
               FROM research_performance_summaries"""
        ).fetchone()
        state_payloads = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT state_json FROM research_expert_states ORDER BY role_id, revision"
            ).fetchall()
        ]
        available_ids = {
            row[0]
            for row in connection.execute("SELECT evidence_id FROM research_evidence_records")
        }
        persisted_report = json.loads(
            connection.execute("SELECT report_json FROM research_reports").fetchone()[0]
        )
    assert sections
    assert duplicate_links == []
    assert quality is not None
    assert all(status == "completed" for status, _payload in sections)
    assert all(json.loads(payload)["generation"] == "deterministic" for _status, payload in sections)
    assert final_metric == ("final_edit", "final_editor", 30_000, "estimated")
    assert section_call_count == 0
    assert performance is not None
    assert performance[0] <= 18
    assert performance[1] <= 10
    assert performance[2] == 0
    assert performance[3] < 30_000
    assert performance[4] == "completed"
    assert sum(event["event_type"] == "blackboard.completed" for event in restored["events"]) == 1
    baseline = json.loads(
        (Path(__file__).parent / "fixtures" / "performance_optimization_baseline.json").read_text()
    )
    assert performance[0] <= baseline["packet_count"] * 0.5
    assert performance[1] <= baseline["semantic_revision_count"] * 0.3
    assert performance[2] == 0 < baseline["section_llm_call_count"]
    used_ids = {
        evidence_id
        for state in state_payloads
        for claim in state["claims"]
        for key in ("supporting_evidence_ids", "contradicting_evidence_ids")
        for evidence_id in claim[key]
    }
    coverage_statuses = {
        f"{state['role_id']}:{requirement}": detail["status"]
        for state in state_payloads
        for requirement, detail in state["requirement_coverage"].items()
    }
    report_citations = persisted_report["cited_evidence_ids"]
    assert len({state["role_id"] for state in state_payloads}) == baseline["expert_count"]
    assert QualityMetrics.evidence_recall(
        used_ids, baseline["necessary_evidence_ids"]
    ) >= baseline["evidence_recall"]
    assert QualityMetrics.framework_coverage(coverage_statuses) >= baseline[
        "mandatory_framework_coverage"
    ]
    assert QualityMetrics.citation_validity(
        sum(item in available_ids for item in report_citations), len(report_citations)
    ) >= baseline["citation_validity"]
    assert QualityMetrics.contradiction_preservation(0, 0) >= baseline[
        "contradiction_preservation"
    ]


def test_market_committee_routes_oversized_evidence_without_emptying_claim_board(
    tmp_path: Path,
) -> None:
    provider = StructuredPipelineProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=LargeMarketSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "MARKET:GLOBAL:OVERVIEW",
                "role_id": "general",
                "mode": "committee",
                "title": "大盘议事厅",
            },
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={
                "content": "结合当前所有可用且完整的证据，生成7月22日盘后报告，并结合我的本地持仓给出条件化观察建议。",
                "role_id": "general",
            },
        )
        restored = _wait_for_run(client, thread["thread_id"])

    completed = [event for event in restored["events"] if event["event_type"] == "expert.completed"]
    failed = [event for event in restored["events"] if event["event_type"] == "expert.failed"]
    report = next(
        event["payload"] for event in restored["events"] if event["event_type"] == "report.completed"
    )
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        claims = connection.execute("SELECT COUNT(*) FROM research_claims").fetchone()[0]
        packets = connection.execute(
            """SELECT role_id, COUNT(*), SUM(token_estimate)
               FROM research_evidence_packets WHERE packet_id LIKE '%-DOMAIN-%'
               GROUP BY role_id"""
        ).fetchall()
    assert len(completed) == 6
    assert failed == []
    assert claims > 0
    assert report["claim_board"]["claims"]
    assert all(count <= 3 and tokens <= 90_000 for _role, count, tokens in packets)


def test_all_expert_failures_skip_final_editor_and_return_explicit_partial_report(
    tmp_path: Path,
) -> None:
    provider = FailingExpertProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=CompleteEntryPointSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "MARKET:GLOBAL:OVERVIEW",
                "role_id": "general",
                "mode": "committee",
                "title": "全员失败降级",
            },
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "生成7月22日盘后市场深度报告", "role_id": "general"},
        )
        restored = _wait_for_run(client, thread["thread_id"])

    report = next(
        event["payload"] for event in restored["events"] if event["event_type"] == "report.completed"
    )
    assert report["status"] == "partial"
    assert report["unified_edit_completed"] is False
    assert "全部专家均在形成可用研究结论前失败" in report["content"]
    assert "Expert State" not in report["content"]
    assert report["completed_sections"]
    assert provider.chat_calls == []


def test_supplement_revision_disconnect_preserves_completed_experts_and_report(
    tmp_path: Path,
) -> None:
    provider = FailingConflictRevisionProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=NewSupplementEvidenceSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "MARKET:GLOBAL:OVERVIEW",
                "role_id": "general",
                "mode": "committee",
                "title": "补证断线恢复",
            },
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "生成7月22日盘后市场深度报告", "role_id": "general"},
        )
        restored = _wait_for_run(client, thread["thread_id"])

    completed = [event for event in restored["events"] if event["event_type"] == "expert.completed"]
    report = next(
        event["payload"] for event in restored["events"] if event["event_type"] == "report.completed"
    )
    assert restored["active_run"]["status"] == "completed"
    assert len(completed) == 6
    assert report["claim_board"]["claims"]
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        failed_revision = connection.execute(
            "SELECT error_json FROM research_call_metrics WHERE stage = 'expert_conflict_revision' "
            "AND error_json IS NOT NULL LIMIT 1"
        ).fetchone()
        task_statuses = {
            row[0]
            for row in connection.execute(
                "SELECT status FROM research_tasks WHERE task_type = 'expert_analysis'"
            )
        }
        conflict_revision_calls = connection.execute(
            "SELECT COUNT(*) FROM research_call_metrics "
            "WHERE stage = 'expert_conflict_revision'"
        ).fetchone()[0]
        semantic_revisions = connection.execute(
            "SELECT SUM(revision) FROM ("
            "SELECT role_id, MAX(revision) AS revision FROM research_expert_states "
            "GROUP BY role_id)"
        ).fetchone()[0]
    assert failed_revision is not None
    assert "supplement revision disconnected" in failed_revision[0]
    assert task_statuses == {"completed"}
    assert conflict_revision_calls <= 1
    assert semantic_revisions <= 7


def test_attempted_partial_coverage_is_closed_without_repeating_skill_collection(
    tmp_path: Path,
) -> None:
    skill_layer = CountingCompleteEntryPointSkillLayer()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=StructuredPipelineProvider(),
            research_skill_layer=skill_layer,
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "MARKET:GLOBAL:OVERVIEW",
                "role_id": "general",
                "mode": "committee",
                "title": "已尝试缺口门禁",
            },
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "生成7月22日盘后市场深度报告", "role_id": "general"},
        )
        restored = _wait_for_run(client, thread["thread_id"])

    assert restored["active_run"]["status"] == "completed"
    assert skill_layer.calls == 6
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        coordinator_calls = connection.execute(
            "SELECT COUNT(*) FROM research_call_metrics "
            "WHERE stage = 'coordinator_supplement'"
        ).fetchone()[0]
        partial_requirements = connection.execute(
            "SELECT COUNT(*) FROM research_expert_states "
            "WHERE state_json LIKE '%\"status\": \"partial\"%'"
        ).fetchone()[0]
    assert coordinator_calls == 0
    assert partial_requirements == 0


def test_final_edit_failure_returns_persisted_partial_report(tmp_path: Path) -> None:
    provider = StructuredPipelineProvider(fail_final_edit=True)
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=PipelineSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "CN:SSE:600519:STOCK",
                "role_id": "general",
                "mode": "committee",
                "title": "降级报告",
            },
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "请深度复盘财务、市场与组合风险", "role_id": "general"},
        )
        restored = _wait_for_run(client, thread["thread_id"])

    assert restored["active_run"]["status"] == "completed"
    report = next(event["payload"] for event in restored["events"] if event["event_type"] == "report.completed")
    assert report["unified_edit_completed"] is False
    assert report["status"] == "partial"
    assert report["completed_sections"]
    assert report["expert_states"]
    assert isinstance(report["risk_review"], dict)
    assert isinstance(report["claim_conflicts"], dict)
    assert isinstance(report["claim_board"], dict)
    assert isinstance(report["conflict_board"], dict)
    assert isinstance(report["coverage_gaps"], list)
    assert isinstance(report["citation_index"], dict)
    assert isinstance(report["gaps"], list)
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        persisted = json.loads(connection.execute("SELECT report_json FROM research_reports").fetchone()[0])
        metric = connection.execute(
            """SELECT stage, role_id, node_id, provider_type, timed_out, error_json
               FROM research_call_metrics WHERE error_json IS NOT NULL
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        summary = connection.execute(
            """SELECT completion_status, failure_stage, failure_agent, failure_node_id,
                      failure_provider, failure_json
               FROM research_performance_summaries"""
        ).fetchone()
    assert persisted["final_edit_error"] == "provider timeout"
    assert metric[:5] == (
        "final_edit",
        "report_editor",
        "final_editor",
        "StructuredPipelineProvider",
        1,
    )
    assert "provider timeout" in metric[5]
    assert summary[:5] == (
        "partial",
        "final_edit",
        "report_editor",
        "final_editor",
        "StructuredPipelineProvider",
    )
    assert "provider timeout" in summary[5]


def test_final_edit_preflight_token_failure_is_locatable_and_keeps_sections(
    tmp_path: Path,
) -> None:
    provider = StructuredPipelineProvider(claim_repetitions=6_000)
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=PipelineSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "CN:SSE:600519:STOCK",
                "role_id": "general",
                "mode": "committee",
                "title": "终编预算门禁",
            },
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "请深度复盘财务、市场与组合风险", "role_id": "general"},
        )
        restored = _wait_for_run(client, thread["thread_id"])

    report = next(
        event["payload"] for event in restored["events"] if event["event_type"] == "report.completed"
    )
    assert report["status"] == "partial"
    assert report["completed_sections"]
    assert not any(
        str(call["messages"][-1]["content"]).startswith("统一编辑")
        for call in provider.chat_calls
    )
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        metric = connection.execute(
            """SELECT stage, role_id, node_id, estimated_context_tokens, token_budget, error_json
               FROM research_call_metrics WHERE error_json IS NOT NULL
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        summary = connection.execute(
            """SELECT completion_status, failure_stage, failure_agent, failure_node_id,
                      failure_token_estimate, failure_token_budget
               FROM research_performance_summaries"""
        ).fetchone()
    assert metric[:3] == ("final_edit", "report_editor", "final_editor")
    assert metric[3] > metric[4] == 30_000
    assert "超过 token 预算" in metric[5]
    assert summary[:4] == ("partial", "final_edit", "report_editor", "final_editor")
    assert summary[4] > summary[5] == 30_000


def test_early_pipeline_failure_persists_locatable_performance_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_blackboard(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("blackboard fixture failure")

    monkeypatch.setattr(
        "invest_vault.ai.SharedResearchBoardBuilder.build",
        fail_blackboard,
    )
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=StructuredPipelineProvider(),
            research_skill_layer=PipelineSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "CN:SSE:600519:STOCK",
                "role_id": "general",
                "mode": "committee",
                "title": "早期失败定位",
            },
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "请深度复盘财务、市场与组合风险", "role_id": "general"},
        )
        restored = _wait_for_run(client, thread["thread_id"])

    assert restored["active_run"]["status"] == "failed"
    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        metric = connection.execute(
            """SELECT stage, role_id, node_id, provider_type, error_json
               FROM research_call_metrics WHERE error_json IS NOT NULL"""
        ).fetchone()
        summary = connection.execute(
            """SELECT completion_status, failure_stage, failure_agent, failure_node_id,
                      failure_provider, failure_json
               FROM research_performance_summaries"""
        ).fetchone()
    assert metric[:4] == (
        "evidence",
        "coordinator",
        "evidence:workflow",
        "StructuredPipelineProvider",
    )
    assert "blackboard fixture failure" in metric[4]
    assert summary[:5] == (
        "failed",
        "evidence",
        "coordinator",
        "evidence:workflow",
        "StructuredPipelineProvider",
    )
    assert "blackboard fixture failure" in summary[5]


def test_final_editor_uses_coverage_projection_instead_of_repeating_full_open_questions(
    tmp_path: Path,
) -> None:
    provider = StructuredPipelineProvider(verbose_open_questions=True)
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=PipelineSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": "CN:SSE:600519:STOCK",
                "role_id": "general",
                "mode": "committee",
                "title": "终编投影",
            },
        ).json()
        client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": "请深度复盘财务、市场与组合风险", "role_id": "general"},
        )
        restored = _wait_for_run(client, thread["thread_id"])

    final_call = next(
        call
        for call in provider.chat_calls
        if str(call["messages"][-1]["content"]).startswith("统一编辑")
    )
    final_context = json.loads(str(final_call["context"]))
    report = next(
        event["payload"] for event in restored["events"] if event["event_type"] == "report.completed"
    )
    assert "VERBOSE_OPEN_QUESTION" not in str(final_call["context"])
    assert all("claim_ids" not in section for section in final_context["report_requirements"]["sections"])
    assert final_context["research_conclusions"]["field_map"]["text"] == "claim"
    assert "role_id" not in final_context["research_conclusions"]["claims"][0]
    assert isinstance(final_context["citation_index"], list)
    assert report["unified_edit_completed"] is True


@pytest.mark.parametrize(
    ("security_id", "mode", "role_id", "question"),
    [
        (
            "CN:SSE:600519:STOCK",
            "assistant",
            "buffett",
            "请深度复盘公司的财务质量、估值和主要风险",
        ),
        (
            "CN:SSE:512480:FUND",
            "assistant",
            "dalio",
            "请深度复盘基金的持仓结构、流动性和组合风险",
        ),
        (
            "CN:SSE:600519:STOCK",
            "committee",
            "general",
            "请投委会深度复盘公司的财务、市场与组合风险",
        ),
        (
            "MARKET:GLOBAL:OVERVIEW",
            "assistant",
            "dalio",
            "生成当前盘后全球市场报告，并结合我的持仓说明组合风险",
        ),
        (
            "MARKET:GLOBAL:OVERVIEW",
            "committee",
            "general",
            "生成当前盘后大盘行情报告，并结合我的持仓给出下一步建议",
        ),
    ],
)
def test_all_research_entry_points_persist_packetized_evidence_and_expert_state(
    tmp_path: Path,
    security_id: str,
    mode: str,
    role_id: str,
    question: str,
) -> None:
    provider = StructuredPipelineProvider()
    with TestClient(
        create_app(
            tmp_path,
            automatic_updates=False,
            ai_provider=provider,
            research_skill_layer=CompleteEntryPointSkillLayer(),
        )
    ) as client:
        thread = client.post(
            "/api/ai/chats",
            json={
                "security_id": security_id,
                "role_id": role_id,
                "mode": mode,
                "title": "分包研究入口验收",
            },
        ).json()
        response = client.post(
            f"/api/ai/chats/{thread['thread_id']}/messages",
            json={"content": question, "role_id": role_id},
        )
        assert response.status_code == 200, response.text
        if mode == "committee":
            restored = _wait_for_run(client, thread["thread_id"])
            assert restored["active_run"]["status"] == "completed"

    with sqlite3.connect(tmp_path / "vault.sqlite3") as connection:
        packet_count = connection.execute(
            "SELECT COUNT(*) FROM research_evidence_packets"
        ).fetchone()[0]
        state_count = connection.execute(
            "SELECT COUNT(*) FROM research_expert_states"
        ).fetchone()[0]

    assert packet_count > 0
    assert state_count > 0
