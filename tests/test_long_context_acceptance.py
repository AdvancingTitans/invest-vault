from invest_vault.evidence_orchestration import (
    EvidenceManifest,
    EvidenceRecord,
    EvidenceStore,
    ExpertResearchState,
    PacketEngine,
    QualityMetrics,
    RoleEvidencePlanner,
)


def _record(evidence_id: str, domain: str, token_estimate: int = 10_000) -> EvidenceRecord:
    return EvidenceRecord.create(
        evidence_id=evidence_id,
        security_id="CN:SSE:600519:STOCK",
        domain=domain,
        subtype="acceptance",
        entity_id="CN:SSE:600519:STOCK",
        as_of="2026-06-30",
        observed_at="2026-07-22T10:00:00+08:00",
        source_tier="verified_original",
        provider="fixture",
        source_ref=f"fixture://{evidence_id}",
        quality_status="available",
        value={"evidence_id": evidence_id},
        compact_text=f"[{evidence_id}] {domain}",
        token_estimate=token_estimate,
    )


def test_critical_evidence_is_selected_at_start_middle_and_end() -> None:
    critical = _record("EV-CRITICAL-FINANCIAL", "company-financial-quality", 100)
    distractors = [_record(f"EV-DISTRACTOR-{index}", "portfolio-risk-evidence", 100) for index in range(8)]
    selected = []
    for position in (0, 4, 8):
        records = list(distractors)
        records.insert(position, critical)
        manifest = EvidenceManifest.from_store(EvidenceStore().ingest(records))
        plan = RoleEvidencePlanner().plan(
            role_id="buffett",
            question="检查关键财务证据",
            manifest=manifest,
        )
        selected.append(set(plan.evidence_ids))

    assert all("EV-CRITICAL-FINANCIAL" in evidence_ids for evidence_ids in selected)


def test_evidence_position_keeps_reduced_claim_and_coverage_stable() -> None:
    critical = _record("EV-CRITICAL-FINANCIAL", "company-financial-quality", 100)
    distractors = [
        _record(f"EV-DISTRACTOR-{index}", "company-financial-quality", 100)
        for index in range(8)
    ]
    reduced = []
    for position in (0, 4, 8):
        records = list(distractors)
        records.insert(position, critical)
        packet = PacketEngine().build(
            role_id="buffett",
            objective="检查关键财务证据",
            records=records,
            token_budget=1_000,
        )[0]
        state = ExpertResearchState.initial("buffett", "检查关键财务证据").merge(
            packet,
            {
                "claims": [
                    {
                        "claim_id": "CASHFLOW-QUALITY",
                        "claim": "关键财务证据支持现金流质量判断",
                        "status": "supported",
                        "supporting_evidence_ids": ["EV-CRITICAL-FINANCIAL"],
                        "contradicting_evidence_ids": [],
                        "confidence": "high",
                    }
                ],
                "framework_requirements": {
                    "财务质量、三表与现金创造": {
                        "status": "covered",
                        "evidence_ids": ["EV-CRITICAL-FINANCIAL"],
                    }
                },
            },
        )
        state.validate(known_evidence_ids={record.evidence_id for record in records})
        claim = state.claims["CASHFLOW-QUALITY"]
        reduced.append(
            (
                claim.claim,
                claim.status,
                claim.supporting_evidence_ids,
                state.requirement_coverage,
            )
        )

    assert reduced[0] == reduced[1] == reduced[2]


def test_back_loaded_financial_and_governance_evidence_reaches_buffett_not_dalio() -> None:
    records = [
        *[_record(f"EV-PORT-{index}", "portfolio-risk-evidence") for index in range(5)],
        *[_record(f"EV-MARKET-{index}", "market-context-evidence") for index in range(2)],
        *[_record(f"EV-FIN-{index}", "company-financial-quality") for index in range(2)],
        _record("EV-GOV-0", "supplemental-company-evidence"),
    ]
    store = EvidenceStore().ingest(records)
    manifest = EvidenceManifest.from_store(store)
    buffett = RoleEvidencePlanner().plan(role_id="buffett", question="完整复盘", manifest=manifest)
    dalio = RoleEvidencePlanner().plan(role_id="dalio", question="组合风险", manifest=manifest)
    packets = PacketEngine().build(
        role_id="buffett",
        objective="完整复盘",
        records=[store.get(evidence_id) for evidence_id in buffett.evidence_ids],
        token_budget=30_000,
    )
    allocated = {evidence_id for packet in packets for evidence_id in packet.evidence_ids}

    assert {"EV-FIN-0", "EV-FIN-1", "EV-GOV-0"}.issubset(allocated)
    assert "EV-GOV-0" not in dalio.evidence_ids
    assert {f"EV-PORT-{index}" for index in range(5)}.issubset(set(dalio.evidence_ids))
    assert all(packet.token_estimate <= 30_000 for packet in packets)


def test_quality_metrics_use_explicit_gold_sets_and_terminal_coverage() -> None:
    assert QualityMetrics.evidence_recall({"EV-1", "EV-2"}, {"EV-1", "EV-2"}) == 1.0
    assert QualityMetrics.framework_coverage(
        {
            "financial": "covered",
            "valuation": "unavailable",
            "governance": "conflicted",
            "market": "not_applicable",
        }
    ) == 1.0
    assert QualityMetrics.citation_validity(3, 4) == 0.75
    assert QualityMetrics.contradiction_preservation(2, 2) == 1.0
    assert QualityMetrics.report_completion(9, 10) == 0.9
