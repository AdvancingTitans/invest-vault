import pytest

from invest_vault.evidence_orchestration import (
    ClaimBoard,
    ConflictTrigger,
    ContextBudget,
    CoverageGate,
    DomainPacketBuilder,
    EvidenceManifest,
    EvidenceRecord,
    EvidenceStore,
    ExpertExecutionScheduler,
    ExpertExecutionTask,
    ExpertResearchState,
    PacketEngine,
    ReportSectionBuilder,
    RiskReviewState,
    RoleEvidencePlanner,
    estimate_tokens,
)


def _record(
    evidence_id: str,
    domain: str,
    value: object,
    *,
    token_estimate: int = 10,
    description: str | None = None,
    entity_id: str | None = "CN:SSE:600519:STOCK",
    as_of: str = "2026-06-30",
    source_tier: str = "verified_original",
    quality_status: str = "available",
) -> EvidenceRecord:
    return EvidenceRecord.create(
        evidence_id=evidence_id,
        security_id="CN:SSE:600519:STOCK",
        domain=domain,
        subtype="fixture",
        entity_id=entity_id,
        as_of=as_of,
        observed_at="2026-07-22T10:00:00+08:00",
        source_tier=source_tier,
        provider="fixture",
        source_ref=f"fixture://{evidence_id}",
        quality_status=quality_status,
        value=value,
        compact_text=description or f"[{evidence_id}] {domain}",
        token_estimate=token_estimate,
    )


def test_evidence_store_hash_deduplicates_without_losing_complete_value() -> None:
    value = {
        "periods": [
            {"period": "2026Q1", "revenue": 100, "operating_cash_flow": 26},
            {"period": "2026Q2", "revenue": 120, "operating_cash_flow": 31},
        ],
        "nested": {"empty_fields_are_preserved": None, "raw_rows": [1, 2, 3]},
    }
    first = _record("EV-FIN-001", "company-financial-quality", value)
    duplicate = _record("EV-FIN-002", "company-financial-quality", value)
    changed = _record(
        "EV-FIN-003",
        "company-financial-quality",
        {**value, "nested": {**value["nested"], "raw_rows": [1, 2, 3, 4]}},
    )
    store = EvidenceStore().ingest([first, duplicate, changed])

    assert len(store.records) == 2
    assert store.get("EV-FIN-001").value == value
    assert store.get("EV-FIN-001").value["nested"]["empty_fields_are_preserved"] is None
    assert first.content_hash == duplicate.content_hash
    assert first.content_hash != changed.content_hash


def test_manifest_preserves_lookup_metadata_and_original_value_stays_external() -> None:
    record = _record(
        "EV-GOV-001",
        "supplemental-company-evidence",
        {"document": "完整股东大会原文", "pages": ["第一页", "第二页"]},
        token_estimate=420,
        description="资本配置与治理原文",
    )
    store = EvidenceStore().ingest([record])

    manifest = EvidenceManifest.from_store(store)
    item = manifest.get(record.evidence_id)

    assert item.evidence_id == record.evidence_id
    assert item.domain == "supplemental-company-evidence"
    assert item.description == "资本配置与治理原文"
    assert item.token_estimate == 420
    assert item.quality_status == "available"
    assert not hasattr(item, "value")
    assert store.get(item.evidence_id).value["pages"] == ["第一页", "第二页"]


def test_role_planner_separates_common_and_framework_specific_evidence() -> None:
    records = [
        _record("EV-COMMON-001", "common", {"question": "现金创造与治理"}),
        _record("EV-FIN-001", "company-financial-quality", {"cash_flow": 31}),
        _record("EV-GOV-001", "supplemental-company-evidence", {"governance": "原文"}),
        _record("EV-PORT-001", "portfolio-risk-evidence", {"hhi": 0.52}),
    ]
    store = EvidenceStore().ingest(records)
    planner = RoleEvidencePlanner()

    buffett = planner.plan(
        role_id="buffett",
        question="现金创造与治理",
        manifest=EvidenceManifest.from_store(store),
    )
    dalio = planner.plan(
        role_id="dalio",
        question="组合集中度",
        manifest=EvidenceManifest.from_store(store),
    )

    assert buffett.common_evidence_ids == ("EV-COMMON-001",)
    assert dalio.common_evidence_ids == ("EV-COMMON-001",)
    assert "EV-FIN-001" in buffett.specialized_evidence_ids
    assert "EV-GOV-001" in buffett.specialized_evidence_ids
    assert "EV-PORT-001" not in buffett.specialized_evidence_ids
    assert dalio.specialized_evidence_ids == ("EV-PORT-001",)


def test_role_planner_budget_keeps_one_record_per_mandatory_domain_before_duplicates() -> None:
    domains = (
        "company-financial-quality",
        "security-valuation-evidence",
        "market-context-evidence",
        "portfolio-risk-evidence",
        "supplemental-company-evidence",
        "execution-liquidity-evidence",
    )
    records = [_record("EV-COMMON", "common", {}, token_estimate=5)]
    for index, domain in enumerate(domains):
        records.extend(
            [
                _record(f"EV-{index}-PRIMARY", domain, {}, token_estimate=14),
                _record(f"EV-{index}-DUPLICATE", domain, {}, token_estimate=14),
            ]
        )

    plan = RoleEvidencePlanner().plan(
        role_id="klarman",
        question="完整风险复盘",
        manifest=EvidenceManifest.from_store(EvidenceStore().ingest(records)),
        token_budget=90,
    )

    selected_domains = {
        EvidenceManifest.from_store(EvidenceStore().ingest(records)).get(evidence_id).domain
        for evidence_id in plan.evidence_ids
    }
    assert set(domains) <= selected_domains
    assert plan.token_estimate <= 90


def test_role_planner_ranks_relevance_quality_recency_and_conflict_without_broadcasting_unknown_domains() -> None:
    records = [
        _record(
            "EV-QUESTION",
            "company-financial-quality",
            {"cash_flow": 31},
            description="现金流与利润一致性",
            source_tier="aggregator",
            quality_status="partial",
        ),
        _record(
            "EV-UNRELATED",
            "company-financial-quality",
            {"cash_flow": 30},
            description="普通历史记录",
            source_tier="aggregator",
            quality_status="partial",
        ),
        _record(
            "EV-QUALITY",
            "security-valuation-evidence",
            {"pe": 20},
            description="估值记录",
            source_tier="verified_original",
            quality_status="verified_original",
        ),
        _record(
            "EV-LOW-QUALITY",
            "security-valuation-evidence",
            {"pe": 21},
            description="估值记录",
            source_tier="search_snippet",
            quality_status="partial",
        ),
        _record(
            "EV-NEW",
            "market-context-evidence",
            {"index": 3200},
            description="市场记录",
            as_of="2026-06-30",
        ),
        _record(
            "EV-OLD",
            "market-context-evidence",
            {"index": 3000},
            description="市场记录",
            as_of="2016-06-30",
        ),
        _record(
            "EV-CONFLICT",
            "supplemental-company-evidence",
            {"governance": "更正"},
            description="治理原文与聚合值存在冲突",
        ),
        _record(
            "EV-NO-CONFLICT",
            "supplemental-company-evidence",
            {"governance": "普通"},
            description="治理原文",
        ),
        _record("EV-UNKNOWN", "future-private-domain", {"secret": True}),
    ]
    manifest = EvidenceManifest.from_store(EvidenceStore().ingest(records))
    selected = RoleEvidencePlanner().plan(
        role_id="buffett", question="现金流质量", manifest=manifest
    ).specialized_evidence_ids

    assert selected.index("EV-QUESTION") < selected.index("EV-UNRELATED")
    assert selected.index("EV-QUALITY") < selected.index("EV-LOW-QUALITY")
    assert selected.index("EV-NEW") < selected.index("EV-OLD")
    assert selected.index("EV-CONFLICT") < selected.index("EV-NO-CONFLICT")
    assert "EV-UNKNOWN" not in selected
    general = RoleEvidencePlanner().plan(role_id="general", question="全面分析", manifest=manifest)
    assert "EV-UNKNOWN" not in general.evidence_ids


def test_role_planner_does_not_let_optional_common_evidence_displace_mandatory_domains() -> None:
    records = [
        _record("EV-HOLDINGS", "common", {"holdings": []}, token_estimate=10),
        _record("EV-NOTES", "user-judgement", {"notes": "大量笔记"}, token_estimate=30),
        _record("EV-FIN", "company-financial-quality", {"cash_flow": 1}, token_estimate=10),
        _record("EV-VAL", "security-valuation-evidence", {"pe": 20}, token_estimate=10),
        _record("EV-MKT", "market-context-evidence", {"trend": "up"}, token_estimate=10),
        _record("EV-GOV", "supplemental-company-evidence", {"governance": "ok"}, token_estimate=10),
    ]

    plan = RoleEvidencePlanner().plan(
        role_id="buffett",
        question="全面分析",
        manifest=EvidenceManifest.from_store(EvidenceStore().ingest(records)),
        token_budget=50,
    )

    assert plan.common_evidence_ids == ("EV-HOLDINGS",)
    assert set(plan.specialized_evidence_ids) == {"EV-FIN", "EV-VAL", "EV-MKT", "EV-GOV"}
    assert plan.token_estimate == 50


def test_packet_engine_uses_semantic_boundaries_and_preserves_every_evidence_id() -> None:
    records = [
        _record("EV-FIN-001", "company-financial-quality", {"quarter": 1}, token_estimate=40),
        _record("EV-FIN-002", "company-financial-quality", {"quarter": 2}, token_estimate=40),
        _record("EV-GOV-001", "supplemental-company-evidence", {"meeting": 1}, token_estimate=40),
    ]
    budget = ContextBudget(
        model_context_tokens=200,
        reserved_output_tokens=30,
        reserved_reasoning_tokens=20,
        system_tokens=20,
        schema_tokens=10,
        safety_margin_tokens=20,
    )

    packets = PacketEngine().build(
        role_id="buffett",
        objective="更新研究状态",
        records=records,
        token_budget=budget.evidence_budget,
    )

    assert budget.evidence_budget == 100
    assert len(packets) == 2
    assert all(packet.token_estimate <= budget.evidence_budget for packet in packets)
    assert all(len({record.domain for record in packet.evidence}) == 1 for packet in packets)
    allocated_ids = [evidence_id for packet in packets for evidence_id in packet.evidence_ids]
    assert sorted(allocated_ids) == sorted(record.evidence_id for record in records)
    assert len(allocated_ids) == len(set(allocated_ids))


def test_packet_engine_uses_stable_domain_entity_as_of_and_objective_boundaries() -> None:
    records = [
        _record("EV-B", "company-financial-quality", {"quarter": 2}, entity_id="B"),
        _record("EV-A-NEW", "company-financial-quality", {"quarter": 2}, entity_id="A"),
        _record(
            "EV-A-OLD",
            "company-financial-quality",
            {"quarter": 1},
            entity_id="A",
            as_of="2025-12-31",
        ),
    ]
    engine = PacketEngine()
    first = engine.build(role_id="buffett", objective="现金流质量", records=records)
    second = engine.build(role_id="buffett", objective="现金流质量", records=list(reversed(records)))

    assert [packet.packet_id for packet in first] == [packet.packet_id for packet in second]
    assert [packet.evidence_ids for packet in first] == [packet.evidence_ids for packet in second]
    assert len(first) == 3
    assert all(
        len({(item.domain, item.entity_id, item.as_of) for item in packet.evidence}) == 1
        for packet in first
    )
    changed_objective = engine.build(role_id="buffett", objective="估值", records=records)
    assert [packet.packet_id for packet in first] != [packet.packet_id for packet in changed_objective]


def test_domain_packet_builder_consolidates_domains_without_losing_evidence() -> None:
    records = [
        _record(f"EV-FIN-{index}", "company-financial-quality", {}, token_estimate=20)
        for index in range(4)
    ] + [
        _record("EV-VAL", "security-valuation-evidence", {}, token_estimate=20),
        _record("EV-GOV", "supplemental-company-evidence", {}, token_estimate=20),
        _record("EV-LIQ", "execution-liquidity-evidence", {}, token_estimate=20),
    ]

    packets = DomainPacketBuilder().build(
        role_id="buffett",
        objective="完整专家综合",
        records=records,
        token_budget=60,
    )

    assert len(packets) == 3
    assert all(packet.token_estimate <= 60 for packet in packets)
    allocated = [evidence_id for packet in packets for evidence_id in packet.evidence_ids]
    assert sorted(allocated) == sorted(record.evidence_id for record in records)
    assert len(allocated) == len(set(allocated))
    assert any(len({record.domain for record in packet.evidence}) > 1 for packet in packets)


def test_domain_packet_prompt_render_does_not_json_escape_compact_evidence_twice() -> None:
    compact_text = '{"rows":[' + ','.join('{"value":"可复核市场事实"}' for _ in range(500)) + "]}"
    record = _record(
        "EV-MARKET-COMPACT",
        "market-context-evidence",
        {"rows": []},
        token_estimate=estimate_tokens(compact_text),
        description=compact_text,
    )
    packet = DomainPacketBuilder().build(
        role_id="dalio",
        objective="市场结构",
        records=[record],
        token_budget=30_000,
    )[0]

    rendered = packet.render()

    assert compact_text in rendered
    assert '\\"rows\\"' not in rendered
    assert estimate_tokens(rendered) <= record.token_estimate + 200


def test_domain_packet_builder_finds_exact_three_bin_packing_without_losing_evidence() -> None:
    token_sizes = (2, 2, 2, 3, 5, 6, 9)
    records = [
        _record(
            f"EV-PACK-{index}",
            "company-financial-quality",
            {"index": index},
            token_estimate=token_size,
        )
        for index, token_size in enumerate(token_sizes, 1)
    ]

    packets = DomainPacketBuilder().build(
        role_id="buffett",
        objective="精确三箱装箱",
        records=records,
        token_budget=10,
        max_packets=3,
    )

    assert len(packets) <= 3
    assert all(packet.token_estimate <= 10 for packet in packets)
    allocated = [evidence_id for packet in packets for evidence_id in packet.evidence_ids]
    assert sorted(allocated) == sorted(record.evidence_id for record in records)
    assert len(allocated) == len(set(allocated))


def test_expert_scheduler_respects_priority_payload_and_provider_capacity() -> None:
    scheduler = ExpertExecutionScheduler()
    tasks = (
        ExpertExecutionTask("dalio", 10_000, 2),
        ExpertExecutionTask("buffett", 10_000, 1),
        ExpertExecutionTask("munger", 10_000, 1),
    )

    ordered, parallelism = scheduler.schedule(tasks, provider_capacity=2)

    assert [task.role_id for task in ordered] == ["buffett", "munger", "dalio"]
    assert parallelism == 2
    assert scheduler.schedule(tasks, provider_capacity=6)[1] == 3
    large = (
        ExpertExecutionTask("buffett", 45_000, 1),
        ExpertExecutionTask("munger", 45_000, 2),
    )
    assert scheduler.schedule(large, provider_capacity=6)[1] == 2
    oversized = (
        ExpertExecutionTask("buffett", 110_001, 1),
        ExpertExecutionTask("munger", 45_000, 2),
    )
    assert scheduler.schedule(oversized, provider_capacity=6)[1] == 1


def test_context_budget_exposes_report_and_final_gates_and_callable_trimming() -> None:
    budget = ContextBudget(evidence_budget=30, report_section_budget=22, final_edit_budget=40)
    records = [
        _record("EV-1", "company-financial-quality", {}, token_estimate=15),
        _record("EV-2", "company-financial-quality", {}, token_estimate=10),
        _record("EV-3", "company-financial-quality", {}, token_estimate=10),
    ]

    assert budget.for_stage("report_section") == 22
    assert budget.for_stage("final_edit") == 40
    assert budget.allows(22, stage="report_section") is True
    assert budget.allows(23, stage="report_section") is False
    assert [item.evidence_id for item in budget.trim(records, stage="report_section")] == ["EV-1"]
    with pytest.raises(ValueError, match="超过 token 预算"):
        budget.require_within(41, stage="final_edit")


def test_expert_state_merge_can_overturn_an_earlier_claim_and_reject_unknown_citations() -> None:
    packets = PacketEngine().build(
        role_id="buffett",
        objective="更新现金流判断",
        records=[
            _record("EV-FIN-001", "company-financial-quality", {"cash_flow": "stable"}),
            _record("EV-FIN-002", "company-financial-quality", {"cash_flow": "declining"}),
        ],
        token_budget=10,
    )
    state = ExpertResearchState.initial("buffett", "判断现金流质量")
    state = state.merge(
        packets[0],
        {
            "claims": [
                {
                    "claim_id": "CLAIM-001",
                    "claim": "经营现金流质量稳定",
                    "status": "supported",
                    "supporting_evidence_ids": ["EV-FIN-001"],
                    "contradicting_evidence_ids": [],
                    "confidence": "medium",
                }
            ]
        },
    )
    state = state.merge(
        packets[1],
        {
            "claims": [
                {
                    "claim_id": "CLAIM-001",
                    "claim": "经营现金流质量已被正式报告反证",
                    "status": "contradicted",
                    "supporting_evidence_ids": [],
                    "contradicting_evidence_ids": ["EV-FIN-002"],
                    "confidence": "high",
                }
            ]
        },
    )
    state.validate(known_evidence_ids={"EV-FIN-001", "EV-FIN-002"})

    claim = state.claims["CLAIM-001"]
    assert state.revision == 2
    assert state.processed_packet_ids == (packets[0].packet_id, packets[1].packet_id)
    assert claim.status == "contradicted"
    assert claim.supporting_evidence_ids == ()
    assert claim.contradicting_evidence_ids == ("EV-FIN-002",)
    assert "稳定" not in claim.claim

    invalid = state.merge(
        packets[1],
        {
            "claims": [
                {
                    "claim_id": "CLAIM-002",
                    "claim": "不存在的引用",
                    "status": "supported",
                    "supporting_evidence_ids": ["EV-NOT-FOUND"],
                    "contradicting_evidence_ids": [],
                    "confidence": "low",
                }
            ]
        },
    )
    with pytest.raises(ValueError, match="EV-NOT-FOUND"):
        invalid.validate(known_evidence_ids={"EV-FIN-001", "EV-FIN-002"})


def test_expert_state_synthesis_marks_all_packets_in_one_semantic_revision() -> None:
    packets = DomainPacketBuilder().build(
        role_id="buffett",
        objective="完整综合",
        records=[
            _record("EV-FIN", "company-financial-quality", {}, token_estimate=30),
            _record("EV-VAL", "security-valuation-evidence", {}, token_estimate=30),
        ],
        token_budget=30,
    )

    state = ExpertResearchState.initial("buffett").synthesize(
        packets,
        {
            "claims": [
                {
                    "claim_id": "QUALITY-VALUE",
                    "claim": "质量与估值需要联合判断",
                    "status": "supported",
                    "supporting_evidence_ids": ["EV-FIN", "EV-VAL"],
                    "contradicting_evidence_ids": [],
                    "confidence": "medium",
                }
            ]
        },
    )

    assert state.revision == 1
    assert state.processed_packet_ids == tuple(packet.packet_id for packet in packets)
    state.validate(known_evidence_ids={"EV-FIN", "EV-VAL"})


def test_coverage_gate_retains_all_five_states_and_blocks_partial_requirements() -> None:
    records = [
        _record("EV-FIN-001", "company-financial-quality", {"cash_flow": 31}),
        _record("EV-VAL-001", "security-valuation-evidence", {"pe": 20}),
        _record("EV-CREDIT-001", "execution-liquidity-evidence", {"spread": 30}),
        _record("EV-CREDIT-002", "execution-liquidity-evidence", {"spread": 70}),
    ]
    store = EvidenceStore().ingest(records)
    packet = PacketEngine().build(role_id="general", objective="覆盖检查", records=records, token_budget=100)[
        0
    ]
    state = ExpertResearchState.initial("general", "检查框架覆盖").merge(
        packet,
        {
            "framework_requirements": {
                "financial_quality": {"status": "covered", "evidence_ids": ["EV-FIN-001"]},
                "valuation": {"status": "partial", "evidence_ids": ["EV-VAL-001"]},
                "governance": {"status": "not_applicable", "evidence_ids": []},
                "liquidation_value": {
                    "status": "unavailable",
                    "evidence_ids": [],
                    "attempted_sources": ["annual_report"],
                    "reason": "未披露权威清算价值",
                    "alternatives": [],
                    "impact": "无法形成清算价值下限",
                },
                "credit_spread": {
                    "status": "conflicted",
                    "evidence_ids": ["EV-CREDIT-001", "EV-CREDIT-002"],
                },
            }
        },
    )

    result = CoverageGate().evaluate(
        role_id="general", state=state, manifest=EvidenceManifest.from_store(store)
    )

    assert set(result.statuses.values()) == {
        "covered",
        "partial",
        "unavailable",
        "not_applicable",
        "conflicted",
    }
    assert result.can_complete is False
    assert result.actionable_requirements == ("valuation",)


def test_coverage_gate_marks_omitted_mandatory_requirements_partial_and_blocks_completion() -> None:
    record = _record("EV-FIN-001", "company-financial-quality", {"cash_flow": 31})
    store = EvidenceStore().ingest([record])
    packet = PacketEngine().build(
        role_id="buffett",
        objective="覆盖检查",
        records=[record],
        token_budget=100,
    )[0]
    state = ExpertResearchState.initial("buffett", "检查框架覆盖").merge(
        packet,
        {
            "framework_requirements": {
                "财务质量、三表与现金创造": {
                    "status": "covered",
                    "evidence_ids": ["EV-FIN-001"],
                }
            }
        },
    )

    result = CoverageGate().evaluate(
        role_id="buffett",
        state=state,
        manifest=EvidenceManifest.from_store(store),
    )

    assert result.statuses == {
        "财务质量、三表与现金创造": "covered",
        "估值与安全边际情景": "partial",
        "长期量价与市场参照": "partial",
        "管理层、资本配置与护城河原文": "partial",
    }
    assert result.can_complete is False
    assert result.actionable_requirements == (
        "估值与安全边际情景",
        "长期量价与市场参照",
        "管理层、资本配置与护城河原文",
    )


def test_conflict_trigger_is_deterministic_and_does_not_require_a_probe_call() -> None:
    trigger = ConflictTrigger()

    assert trigger.should_revise(
        new_records=[_record("EV-NEW", "company-financial-quality", {})],
        coverage_changed=True,
    )
    assert trigger.should_revise(
        new_records=[
            _record(
                "EV-CONFLICT",
                "company-financial-quality",
                {},
                description="一手来源更正并反证聚合值",
            )
        ],
        coverage_changed=False,
    )
    assert not trigger.should_revise(
        new_records=[_record("EV-STABLE", "company-financial-quality", {})],
        coverage_changed=False,
    )
    assert not trigger.should_revise(
        new_records=[
            _record(
                "EV-RISK-GAP",
                "portfolio-risk-evidence",
                {"gaps": ["历史价格不可得"]},
                quality_status="partial",
            )
        ],
        coverage_changed=False,
    )
    assert trigger.should_revise(
        new_records=[_record("EV-RISK", "portfolio-risk-evidence", {"hhi": 0.4})],
        coverage_changed=False,
    )


def test_claim_board_preserves_support_and_contradiction_instead_of_silently_choosing() -> None:
    support_packet = PacketEngine().build(
        role_id="buffett",
        objective="现金流",
        records=[_record("EV-AGG-001", "company-financial-quality", {"aggregate": "up"})],
        token_budget=100,
    )[0]
    contradict_packet = PacketEngine().build(
        role_id="munger",
        objective="现金流",
        records=[_record("EV-REPORT-001", "company-financial-quality", {"official": "down"})],
        token_budget=100,
    )[0]
    supporting = ExpertResearchState.initial("buffett", "现金流质量").merge(
        support_packet,
        {
            "claims": [
                {
                    "claim_id": "CASHFLOW-QUALITY",
                    "claim": "现金流质量改善",
                    "status": "supported",
                    "supporting_evidence_ids": ["EV-AGG-001"],
                    "contradicting_evidence_ids": [],
                    "confidence": "medium",
                }
            ]
        },
    )
    contradicting = ExpertResearchState.initial("munger", "现金流质量").merge(
        contradict_packet,
        {
            "claims": [
                {
                    "claim_id": "CASHFLOW-QUALITY",
                    "claim": "正式公告显示现金流质量下降",
                    "status": "contradicted",
                    "supporting_evidence_ids": [],
                    "contradicting_evidence_ids": ["EV-REPORT-001"],
                    "confidence": "high",
                }
            ]
        },
    )

    board = ClaimBoard.from_states([supporting, contradicting])
    conflict = board.conflicts["CASHFLOW-QUALITY"]

    assert conflict.supporting_evidence_ids == ("EV-AGG-001",)
    assert conflict.contradicting_evidence_ids == ("EV-REPORT-001",)
    assert conflict.roles == ("buffett", "munger")
    assert conflict.resolved is False


def test_report_sections_receive_minimum_evidence_and_final_failure_returns_fallback_data() -> None:
    store = EvidenceStore().ingest(
        [
            _record("EV-VAL-001", "security-valuation-evidence", {"pe": 20}),
            _record("EV-FIN-001", "company-financial-quality", {"cash_flow": 31}),
            _record("EV-MKT-001", "market-context-evidence", {"index": 3000}),
        ]
    )
    claims = [
        {
            "claim_id": "VALUATION-001",
            "claim": "估值位于历史中位",
            "status": "supported",
            "supporting_evidence_ids": ["EV-VAL-001"],
            "contradicting_evidence_ids": [],
            "confidence": "medium",
        }
    ]
    builder = ReportSectionBuilder(store)

    context = builder.build_context(section_id="valuation", claims=claims)
    fallback = builder.fallback_payload(
        completed_sections={"valuation": "已完成估值章节"},
        expert_states={"buffett": {"revision": 2}},
        risks=["现金流与估值结论存在分歧"],
        gaps=["权威清算价值未披露"],
        final_edit_error="provider timeout",
    )

    assert context.evidence_ids == ("EV-VAL-001",)
    assert "EV-VAL-001" in context.text
    assert "EV-FIN-001" not in context.text
    assert "EV-MKT-001" not in context.text
    assert fallback["status"] == "partial"
    assert fallback["unified_edit_completed"] is False
    assert fallback["completed_sections"] == {"valuation": "已完成估值章节"}
    assert fallback["expert_states"]["buffett"]["revision"] == 2
    assert fallback["risks"] == ["现金流与估值结论存在分歧"]
    assert fallback["gaps"] == ["权威清算价值未披露"]
    assert fallback["final_edit_error"] == "provider timeout"


def test_risk_review_keeps_five_dimensions_and_does_not_infer_user_constraints() -> None:
    store = EvidenceStore().ingest(
        [
            _record("EV-PORT-001", "portfolio-risk-evidence", {"hhi": 0.4}),
            _record("EV-LIQ-001", "execution-liquidity-evidence", {"spread": 0.01}),
        ]
    )
    state = ExpertResearchState.initial("dalio")
    board = ClaimBoard.from_states([state])

    review = RiskReviewState.build(states=[state], board=board, store=store)

    assert set(review.dimensions) == {
        "security_risks",
        "portfolio_concentration",
        "correlation",
        "liquidity",
        "user_constraints",
    }
    assert review.dimensions["portfolio_concentration"]["status"] == "covered"
    assert review.dimensions["correlation"]["status"] == "unavailable"
    assert review.dimensions["liquidity"]["status"] == "covered"
    assert review.dimensions["user_constraints"]["status"] == "unavailable"
    assert review.dimensions["user_constraints"]["gaps"]
