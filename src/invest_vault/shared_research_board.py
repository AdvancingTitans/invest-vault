"""Deterministic shared facts for one research run.

The board projects already-normalized evidence once and lets role-specific
reasoning omit facts that every relevant expert can read from the shared layer.
It never creates investment claims or asks an LLM to summarize evidence.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field

from .evidence_orchestration import EvidenceRecord, EvidenceStore, RoleEvidencePlan

_SHAREABLE_QUALITY = frozenset({"verified", "verified_original", "available", "completed"})
_DOMAIN_CONTEXT = {
    "common": "portfolio_context",
    "company-financial-quality": "company_facts",
    "security-valuation-evidence": "company_facts",
    "supplemental-company-evidence": "company_facts",
    "market-context-evidence": "market_context",
    "fund-portfolio-evidence": "portfolio_context",
    "portfolio-risk-evidence": "risk_context",
    "execution-liquidity-evidence": "risk_context",
    "fund-liquidity-evidence": "risk_context",
    "drawdown-attribution-readiness": "risk_context",
}


@dataclass(frozen=True)
class SharedFact:
    """A sourceable fact copied without interpretation from the Evidence Store."""

    evidence_id: str
    domain: str
    subtype: str
    entity_id: str | None
    as_of: str | None
    observed_at: str
    source_tier: str
    provider: str
    source_ref: str
    quality_status: str
    compact_text: str
    audience: tuple[str, ...]

    @classmethod
    def from_record(cls, record: EvidenceRecord, *, audience: tuple[str, ...]) -> SharedFact:
        return cls(
            evidence_id=record.evidence_id,
            domain=record.domain,
            subtype=record.subtype,
            entity_id=record.entity_id,
            as_of=record.as_of,
            observed_at=record.observed_at,
            source_tier=record.source_tier,
            provider=record.provider,
            source_ref=record.source_ref,
            quality_status=record.quality_status,
            compact_text=record.compact_text,
            audience=audience,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "domain": self.domain,
            "subtype": self.subtype,
            "entity_id": self.entity_id,
            "as_of": self.as_of,
            "observed_at": self.observed_at,
            "source_tier": self.source_tier,
            "provider": self.provider,
            "source_ref": self.source_ref,
            "quality_status": self.quality_status,
            "compact_text": self.compact_text,
            "audience": self.audience,
        }


@dataclass(frozen=True)
class SharedResearchBoard:
    """Verified shared facts for a single run; conclusions are forbidden."""

    run_id: str
    company_facts: tuple[SharedFact, ...] = ()
    market_context: tuple[SharedFact, ...] = ()
    portfolio_context: tuple[SharedFact, ...] = ()
    risk_context: tuple[SharedFact, ...] = ()
    common_claims: tuple[object, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id 不能为空")
        if self.common_claims:
            raise ValueError("Shared Research Blackboard 禁止保存投资结论")

    @property
    def facts(self) -> tuple[SharedFact, ...]:
        return (
            *self.company_facts,
            *self.market_context,
            *self.portfolio_context,
            *self.risk_context,
        )

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(fact.evidence_id for fact in self.facts)

    def evidence_ids_for_role(self, role_id: str) -> tuple[str, ...]:
        """Return evidence IDs that the caller can remove from this role's packets."""

        return tuple(fact.evidence_id for fact in self.facts if role_id in fact.audience)

    def shared_evidence_ids_for(self, role_id: str) -> tuple[str, ...]:
        """Stable integration alias for role packet deduplication."""

        return self.evidence_ids_for_role(role_id)

    def for_role(self, role_id: str) -> dict[str, object]:
        """Return a JSON-serializable shared-fact projection for one role."""

        def visible(facts: tuple[SharedFact, ...]) -> list[dict[str, object]]:
            return [fact.as_dict() for fact in facts if role_id in fact.audience]

        return {
            "run_id": self.run_id,
            "role_id": role_id,
            "company_facts": visible(self.company_facts),
            "market_context": visible(self.market_context),
            "portfolio_context": visible(self.portfolio_context),
            "risk_context": visible(self.risk_context),
            "common_claims": [],
            "shared_evidence_ids": self.shared_evidence_ids_for(role_id),
        }

    def render_for_role(self, role_id: str) -> str:
        """Render only shared facts routed to the requested role."""

        return json.dumps(
            self.for_role(role_id),
            ensure_ascii=False,
            sort_keys=True,
        )

    def render_prompt_for_role(self, role_id: str) -> str:
        """Render shared facts without re-encoding their compact text as JSON strings."""

        lines = [json.dumps({"run_id": self.run_id, "role_id": role_id}, ensure_ascii=False)]
        for fact in self.facts:
            if role_id not in fact.audience:
                continue
            lines.extend(
                (
                    json.dumps(
                        {
                            "evidence_id": fact.evidence_id,
                            "domain": fact.domain,
                            "as_of": fact.as_of,
                            "quality_status": fact.quality_status,
                        },
                        ensure_ascii=False,
                    ),
                    fact.compact_text,
                )
            )
        return "\n".join(lines)


class SharedResearchBoardBuilder:
    """Build at most one immutable board per run and reuse it for every expert."""

    def __init__(self) -> None:
        self._boards: dict[str, SharedResearchBoard] = {}
        self._lock = threading.Lock()

    def build_once(
        self,
        *,
        run_id: str,
        store: EvidenceStore,
        role_plans: Mapping[str, RoleEvidencePlan],
    ) -> SharedResearchBoard:
        with self._lock:
            existing = self._boards.get(run_id)
            if existing is not None:
                return existing
            board = self._build(run_id=run_id, store=store, role_plans=role_plans)
            self._boards[run_id] = board
            return board

    def build(
        self,
        *,
        run_id: str,
        store: EvidenceStore,
        role_plans: Mapping[str, RoleEvidencePlan],
    ) -> SharedResearchBoard:
        """Stable integration entry point; equivalent to :meth:`build_once`."""

        return self.build_once(run_id=run_id, store=store, role_plans=role_plans)

    def get(self, run_id: str) -> SharedResearchBoard:
        with self._lock:
            try:
                return self._boards[run_id]
            except KeyError as error:
                raise KeyError(f"研究运行尚未构建 Shared Research Blackboard：{run_id}") from error

    @staticmethod
    def _build(
        *,
        run_id: str,
        store: EvidenceStore,
        role_plans: Mapping[str, RoleEvidencePlan],
    ) -> SharedResearchBoard:
        if not run_id:
            raise ValueError("run_id 不能为空")

        routed_ids = {
            role_id: frozenset(plan.evidence_ids) for role_id, plan in role_plans.items()
        }
        contexts: dict[str, list[SharedFact]] = {
            "company_facts": [],
            "market_context": [],
            "portfolio_context": [],
            "risk_context": [],
        }
        for record in sorted(store.records, key=lambda item: item.evidence_id):
            context_name = _DOMAIN_CONTEXT.get(record.domain)
            if context_name is None or record.quality_status.casefold() not in _SHAREABLE_QUALITY:
                continue
            audience = tuple(
                sorted(role_id for role_id, evidence_ids in routed_ids.items() if record.evidence_id in evidence_ids)
            )
            if len(audience) < 2:
                continue
            contexts[context_name].append(SharedFact.from_record(record, audience=audience))

        return SharedResearchBoard(
            run_id=run_id,
            company_facts=tuple(contexts["company_facts"]),
            market_context=tuple(contexts["market_context"]),
            portfolio_context=tuple(contexts["portfolio_context"]),
            risk_context=tuple(contexts["risk_context"]),
        )


# The optimization plan uses the longer Blackboard name. Keep both spellings as
# the same immutable data model so callers do not need an adapter.
SharedResearchBlackboard = SharedResearchBoard
