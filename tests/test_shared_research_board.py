import json

import pytest

from invest_vault.evidence_orchestration import (
    EvidenceManifest,
    EvidenceRecord,
    EvidenceStore,
    RoleEvidencePlanner,
    estimate_tokens,
)
from invest_vault.shared_research_board import (
    SharedResearchBlackboard,
    SharedResearchBoard,
    SharedResearchBoardBuilder,
)


def _record(
    evidence_id: str,
    domain: str,
    *,
    quality_status: str = "available",
    value: object | None = None,
) -> EvidenceRecord:
    return EvidenceRecord.create(
        evidence_id=evidence_id,
        security_id="CN:SSE:600519:STOCK",
        domain=domain,
        subtype="fixture",
        entity_id="CN:SSE:600519:STOCK",
        as_of="2026-06-30",
        observed_at="2026-07-22T10:00:00+08:00",
        source_tier="verified_original",
        provider="fixture",
        source_ref=f"fixture://{evidence_id}",
        quality_status=quality_status,
        value=value if value is not None else {"fact": evidence_id},
        compact_text=f"[{evidence_id}] 可复核事实",
    )


def _plans(store: EvidenceStore):
    manifest = EvidenceManifest.from_store(store)
    planner = RoleEvidencePlanner()
    return {
        role_id: planner.plan(role_id=role_id, question="全面分析", manifest=manifest)
        for role_id in ("buffett", "dalio")
    }


def test_board_projects_only_shareable_facts_and_keeps_common_claims_empty() -> None:
    store = EvidenceStore().ingest(
        [
            _record("EV-COMMON", "common"),
            _record("EV-FIN", "company-financial-quality", quality_status="verified_original"),
            _record("EV-MARKET", "market-context-evidence", quality_status="completed"),
            _record("EV-PORTFOLIO", "fund-portfolio-evidence"),
            _record("EV-RISK", "portfolio-risk-evidence"),
            _record("EV-PARTIAL", "execution-liquidity-evidence", quality_status="partial"),
            _record("EV-FAILED", "supplemental-company-evidence", quality_status="failed"),
            _record("EV-JUDGEMENT", "user-judgement"),
        ]
    )

    board = SharedResearchBoardBuilder().build_once(
        run_id="RUN-1", store=store, role_plans=_plans(store)
    )

    assert [fact.evidence_id for fact in board.company_facts] == ["EV-FIN"]
    assert [fact.evidence_id for fact in board.market_context] == ["EV-MARKET"]
    assert [fact.evidence_id for fact in board.portfolio_context] == [
        "EV-COMMON",
        "EV-PORTFOLIO",
    ]
    assert board.risk_context == ()
    assert board.common_claims == ()
    assert "EV-PARTIAL" not in board.evidence_ids
    assert "EV-FAILED" not in board.evidence_ids
    assert "EV-JUDGEMENT" not in board.evidence_ids


def test_board_computes_audience_renders_by_role_and_exposes_deduplication_ids() -> None:
    store = EvidenceStore().ingest(
        [
            _record("EV-COMMON", "common"),
            _record("EV-FIN", "company-financial-quality"),
            _record("EV-MARKET", "market-context-evidence"),
            _record("EV-RISK", "portfolio-risk-evidence"),
        ]
    )
    plans = _plans(store)

    board = SharedResearchBoardBuilder().build(
        run_id="RUN-AUDIENCE", store=store, role_plans=plans
    )
    assert isinstance(board, SharedResearchBlackboard)
    buffett_projection = board.for_role("buffett")
    json.dumps(buffett_projection, ensure_ascii=False)
    buffett = json.loads(board.render_for_role("buffett"))
    dalio = json.loads(board.render_for_role("dalio"))
    dalio_prompt = board.render_prompt_for_role("dalio")

    assert board.company_facts == ()
    assert buffett["company_facts"] == []
    assert dalio["company_facts"] == []
    assert dalio["risk_context"] == []
    assert buffett["risk_context"] == []
    assert buffett["common_claims"] == []
    assert dalio["common_claims"] == []
    assert set(board.shared_evidence_ids_for("buffett")) == {
        "EV-COMMON",
        "EV-MARKET",
    }
    assert set(board.shared_evidence_ids_for("dalio")) == {
        "EV-COMMON",
        "EV-MARKET",
    }
    assert set(plans["buffett"].evidence_ids) - set(board.evidence_ids_for_role("buffett")) == {
        "EV-FIN"
    }
    assert all("value" not in item for item in buffett["market_context"])
    assert '"evidence_id": "EV-MARKET"' in dalio_prompt
    assert "audience" not in dalio_prompt
    assert estimate_tokens(dalio_prompt) < estimate_tokens(board.render_for_role("dalio"))


def test_builder_constructs_each_run_once_and_reuses_the_immutable_board() -> None:
    first_store = EvidenceStore().ingest([_record("EV-FIRST", "common")])
    replacement_store = EvidenceStore().ingest([_record("EV-REPLACEMENT", "common")])
    builder = SharedResearchBoardBuilder()

    first = builder.build_once(run_id="RUN-ONCE", store=first_store, role_plans=_plans(first_store))
    reused = builder.build_once(
        run_id="RUN-ONCE", store=replacement_store, role_plans=_plans(replacement_store)
    )
    other_run = builder.build_once(
        run_id="RUN-TWO", store=replacement_store, role_plans=_plans(replacement_store)
    )

    assert reused is first
    assert builder.get("RUN-ONCE") is first
    assert first.evidence_ids == ("EV-FIRST",)
    assert other_run.evidence_ids == ("EV-REPLACEMENT",)
    with pytest.raises(KeyError, match="尚未构建"):
        builder.get("RUN-MISSING")


def test_board_rejects_investment_conclusions() -> None:
    with pytest.raises(ValueError, match="禁止保存投资结论"):
        SharedResearchBoard(run_id="RUN-CLAIM", common_claims=("应该买入",))
