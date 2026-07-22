"""Evidence-first orchestration for bounded investment research contexts.

The module implements the plan's external evidence, role projection, semantic
packet, incremental expert state, coverage gate, claim board and report-section
contracts without introducing a retrieval service or GraphRAG dependency.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .ai_skills import FRAMEWORK_REQUIREMENTS, FRAMEWORK_SKILLS


def estimate_tokens(value: object) -> int:
    """Return a conservative, deterministic estimate when provider usage is absent."""

    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    remaining = re.sub(r"[\u3400-\u9fff]", "", text)
    ascii_tokens = sum(max(1, math.ceil(len(item) / 4)) for item in re.findall(r"\w+|[^\w\s]", remaining))
    return max(1, chinese + ascii_tokens)


def _semantic_fragments(value: object, token_budget: int) -> list[object]:
    if estimate_tokens(value) <= token_budget:
        return [value]
    if isinstance(value, dict):
        fragments: list[object] = []
        for key, item in value.items():
            fragments.extend({key: child} for child in _semantic_fragments(item, token_budget))
        return fragments
    if isinstance(value, list):
        fragments = []
        current: list[object] = []
        for item in value:
            item_fragments = _semantic_fragments(item, token_budget)
            for child in item_fragments:
                candidate = [*current, child]
                if current and estimate_tokens(candidate) > token_budget:
                    fragments.append(current)
                    current = []
                current.append(child)
        if current:
            fragments.append(current)
        return fragments
    if isinstance(value, str):
        units = [item for item in re.split(r"(?<=[。！？；\n])", value) if item]
        if len(units) <= 1:
            raise ValueError("单条无语义边界文本超过 packet token 预算")
        fragments: list[str] = []
        current = ""
        for unit in units:
            if current and estimate_tokens(current + unit) > token_budget:
                fragments.append(current)
                current = ""
            current += unit
        if current:
            fragments.append(current)
        return fragments
    raise ValueError("单个原子证据值超过 packet token 预算")


def project_oversized_record(
    record: EvidenceRecord,
    token_budget: int,
) -> tuple[EvidenceRecord, ...]:
    """Keep the full record external and create semantic child projections for packets."""

    if record.token_estimate <= token_budget:
        return (record,)
    raw = EvidenceRecord.create(
        evidence_id=record.evidence_id,
        security_id=record.security_id,
        domain="raw-evidence",
        subtype=f"{record.domain}:{record.subtype}",
        entity_id=record.entity_id,
        as_of=record.as_of,
        observed_at=record.observed_at,
        source_tier=record.source_tier,
        provider=record.provider,
        source_ref=record.source_ref,
        quality_status=record.quality_status,
        value=record.value,
        compact_text=f"[{record.evidence_id}] 完整原始证据保存在 Evidence Store，正文按语义子项调入。",
    )
    children = []
    for index, fragment in enumerate(_semantic_fragments(record.value, token_budget), 1):
        evidence_id = f"{record.evidence_id}-PART-{index:03d}"
        # Packet.render already carries the evidence ID, domain, date and quality.
        # Keep the child text focused on the deterministic value projection while
        # the complete original value remains in the external Evidence Store.
        compact = json.dumps(fragment, ensure_ascii=False, sort_keys=True)
        children.append(
            EvidenceRecord.create(
                evidence_id=evidence_id,
                security_id=record.security_id,
                domain=record.domain,
                subtype=f"{record.subtype}:projection",
                entity_id=record.entity_id,
                as_of=record.as_of,
                observed_at=record.observed_at,
                source_tier=record.source_tier,
                provider=record.provider,
                source_ref=record.source_ref,
                quality_status=record.quality_status,
                value=fragment,
                compact_text=compact,
            )
        )
    return (raw, *children)


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    security_id: str
    domain: str
    subtype: str
    entity_id: str | None
    as_of: str | None
    observed_at: str
    source_tier: str
    provider: str
    source_ref: str
    quality_status: str
    value: Any
    compact_text: str
    token_estimate: int
    content_hash: str
    token_estimate_kind: str = "estimated"

    @classmethod
    def create(
        cls,
        *,
        evidence_id: str,
        security_id: str,
        domain: str,
        subtype: str,
        entity_id: str | None,
        as_of: str | None,
        observed_at: str,
        source_tier: str,
        provider: str,
        source_ref: str,
        quality_status: str,
        value: Any,
        compact_text: str,
        token_estimate: int | None = None,
    ) -> EvidenceRecord:
        canonical = json.dumps(
            {
                "security_id": security_id,
                "domain": domain,
                "subtype": subtype,
                "entity_id": entity_id,
                "as_of": as_of,
                "value": value,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            evidence_id=evidence_id,
            security_id=security_id,
            domain=domain,
            subtype=subtype,
            entity_id=entity_id,
            as_of=as_of,
            observed_at=observed_at,
            source_tier=source_tier,
            provider=provider,
            source_ref=source_ref,
            quality_status=quality_status,
            value=value,
            compact_text=compact_text,
            token_estimate=token_estimate or estimate_tokens(compact_text),
            content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        )


@dataclass(frozen=True)
class EvidenceStore:
    records: tuple[EvidenceRecord, ...] = ()

    def ingest(self, records: Iterable[EvidenceRecord]) -> EvidenceStore:
        by_hash = {item.content_hash: item for item in self.records}
        for record in records:
            by_hash.setdefault(record.content_hash, record)
        return EvidenceStore(tuple(by_hash.values()))

    def get(self, evidence_id: str) -> EvidenceRecord:
        for record in self.records:
            if record.evidence_id == evidence_id:
                return record
        raise KeyError(evidence_id)


@dataclass(frozen=True)
class ManifestItem:
    evidence_id: str
    domain: str
    description: str
    as_of: str | None
    quality_status: str
    token_estimate: int
    entity_id: str | None
    observed_at: str
    source_tier: str


@dataclass(frozen=True)
class EvidenceManifest:
    items: tuple[ManifestItem, ...]

    @classmethod
    def from_store(cls, store: EvidenceStore) -> EvidenceManifest:
        return cls(
            tuple(
                ManifestItem(
                    evidence_id=record.evidence_id,
                    domain=record.domain,
                    description=record.compact_text,
                    as_of=record.as_of,
                    quality_status=record.quality_status,
                    token_estimate=record.token_estimate,
                    entity_id=record.entity_id,
                    observed_at=record.observed_at,
                    source_tier=record.source_tier,
                )
                for record in store.records
            )
        )

    def get(self, evidence_id: str) -> ManifestItem:
        for item in self.items:
            if item.evidence_id == evidence_id:
                return item
        raise KeyError(evidence_id)


class ContextBudget:
    """Approved example budget with an explicit estimated-evidence ceiling."""

    def __init__(
        self,
        *,
        model_context_tokens: int = 200_000,
        reserved_output_tokens: int = 16_000,
        reserved_reasoning_tokens: int = 40_000,
        system_tokens: int = 8_000,
        schema_tokens: int = 4_000,
        safety_margin_tokens: int = 22_000,
        evidence_budget: int | None = None,
        report_section_budget: int = 22_000,
        final_edit_budget: int = 30_000,
    ) -> None:
        self.model_context_tokens = model_context_tokens
        self.reserved_output_tokens = reserved_output_tokens
        self.reserved_reasoning_tokens = reserved_reasoning_tokens
        self.system_tokens = system_tokens
        self.schema_tokens = schema_tokens
        self.safety_margin_tokens = safety_margin_tokens
        self.report_section_budget = report_section_budget
        self.final_edit_budget = final_edit_budget
        calculated = max(
            1,
            model_context_tokens
            - reserved_output_tokens
            - reserved_reasoning_tokens
            - system_tokens
            - schema_tokens
            - safety_margin_tokens,
        )
        explicit_fields = (
            model_context_tokens,
            reserved_output_tokens,
            reserved_reasoning_tokens,
            system_tokens,
            schema_tokens,
            safety_margin_tokens,
        ) != (200_000, 16_000, 40_000, 8_000, 4_000, 22_000)
        self.evidence_budget = evidence_budget if evidence_budget is not None else (
            calculated if explicit_fields else 30_000
        )
        self.expert_packet_budget = self.evidence_budget
        for name in ("evidence_budget", "report_section_budget", "final_edit_budget"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须大于 0")

    def for_stage(self, stage: str) -> int:
        budgets = {
            "expert": self.evidence_budget,
            "report_section": self.report_section_budget,
            "final_edit": self.final_edit_budget,
        }
        try:
            return budgets[stage]
        except KeyError as error:
            raise ValueError(f"未知上下文预算阶段：{stage}") from error

    def allows(self, token_estimate: int, *, stage: str = "expert") -> bool:
        return 0 <= token_estimate <= self.for_stage(stage)

    def require_within(self, token_estimate: int, *, stage: str = "expert") -> None:
        limit = self.for_stage(stage)
        if not self.allows(token_estimate, stage=stage):
            raise ValueError(f"{stage} 上下文超过 token 预算：{token_estimate} > {limit}")

    def trim(
        self,
        items: Sequence[Any],
        *,
        stage: str = "expert",
        token_getter: Any = lambda item: item.token_estimate,
    ) -> tuple[Any, ...]:
        """按调用方既定优先级裁剪，且绝不突破所选阶段预算。"""

        selected: list[Any] = []
        used = 0
        for item in items:
            item_tokens = int(token_getter(item))
            if item_tokens < 0:
                raise ValueError("token_estimate 不能为负数")
            if used + item_tokens <= self.for_stage(stage):
                selected.append(item)
                used += item_tokens
        return tuple(selected)

    @staticmethod
    def concurrency_for(token_estimate: int) -> int:
        if token_estimate < 20_000:
            return 3
        if token_estimate <= 110_000:
            return 2
        return 1


@dataclass(frozen=True)
class ExpertExecutionTask:
    role_id: str
    estimated_tokens: int
    priority: int


class ExpertExecutionScheduler:
    """Order expert work and cap concurrency by payload and Provider capacity."""

    def schedule(
        self,
        tasks: Sequence[ExpertExecutionTask],
        *,
        provider_capacity: int,
    ) -> tuple[tuple[ExpertExecutionTask, ...], int]:
        if provider_capacity <= 0:
            raise ValueError("provider_capacity 必须大于 0")
        ordered = tuple(sorted(tasks, key=lambda item: (item.priority, item.role_id)))
        maximum_tokens = max((item.estimated_tokens for item in ordered), default=0)
        parallelism = min(
            max(1, provider_capacity),
            ContextBudget.concurrency_for(maximum_tokens),
            max(1, len(ordered)),
        )
        return ordered, parallelism


@dataclass(frozen=True)
class RoleEvidencePlan:
    role_id: str
    common_evidence_ids: tuple[str, ...]
    specialized_evidence_ids: tuple[str, ...]
    uncovered_requirements: tuple[str, ...]
    token_estimate: int

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return (*self.common_evidence_ids, *self.specialized_evidence_ids)


class RoleEvidencePlanner:
    _COMMON_DOMAINS = frozenset({"common", "archived-evidence"})
    _DOMAIN_TERMS: Mapping[str, tuple[str, ...]] = {
        "company-financial-quality": ("财务", "现金流", "收入", "利润", "盈利", "资产负债"),
        "security-valuation-evidence": ("估值", "市盈率", "市净率", "安全边际", "pe", "pb"),
        "portfolio-risk-evidence": ("组合", "持仓", "集中度", "相关性", "仓位", "回撤"),
        "market-context-evidence": ("市场", "行情", "指数", "板块", "量价", "趋势"),
        "supplemental-company-evidence": ("治理", "管理层", "资本配置", "护城河", "催化剂"),
        "execution-liquidity-evidence": ("流动性", "盘口", "成交", "执行", "滑点", "退出"),
        "fund-portfolio-evidence": ("基金", "持仓", "行业", "重仓"),
        "fund-liquidity-evidence": ("基金", "申赎", "规模", "流动性"),
        "drawdown-attribution-readiness": ("回撤", "归因", "风险"),
    }

    def plan(
        self,
        *,
        role_id: str,
        question: str,
        manifest: EvidenceManifest,
        token_budget: int | None = None,
    ) -> RoleEvidencePlan:
        domains = set(FRAMEWORK_SKILLS.get(role_id, ()))
        if role_id == "general":
            domains = {domain for values in FRAMEWORK_SKILLS.values() for domain in values}
        if any(item.domain.startswith("fund-") for item in manifest.items):
            domains.update(
                {
                    "fund-portfolio-evidence",
                    "fund-liquidity-evidence",
                    "drawdown-attribution-readiness",
                    "security-valuation-evidence",
                    "market-context-evidence",
                    "execution-liquidity-evidence",
                    "company-financial-quality",
                }
            )
        common_items = sorted(
            (item for item in manifest.items if item.domain in self._COMMON_DOMAINS),
            key=lambda item: self._sort_key(item, question),
        )
        specialized_items = [
            item
            for item in manifest.items
            if item.domain not in self._COMMON_DOMAINS
            and (
                item.domain in domains
                or item.domain == f"framework-readiness-{role_id}"
            )
        ]
        specialized_items.sort(key=lambda item: self._sort_key(item, question))
        if token_budget is not None:
            common_items, specialized_items = self._select_within_budget(
                common_items=common_items,
                specialized_items=specialized_items,
                required_domains=domains,
                token_budget=token_budget,
            )
        common = tuple(item.evidence_id for item in common_items)
        present_domains = {item.domain for item in specialized_items}
        uncovered = tuple(
            label
            for label, skill_id, _ceiling in FRAMEWORK_REQUIREMENTS.get(role_id, ())
            if skill_id is not None and skill_id not in present_domains
        )
        ids = tuple(item.evidence_id for item in specialized_items)
        token_estimate = sum(manifest.get(item).token_estimate for item in (*common, *ids))
        return RoleEvidencePlan(role_id, common, ids, uncovered, token_estimate)

    @classmethod
    def _sort_key(cls, item: ManifestItem, question: str) -> tuple[float, str, str, str, str]:
        return (
            -cls._score(item, question),
            item.domain,
            item.entity_id or "",
            item.as_of or "",
            item.evidence_id,
        )

    @classmethod
    def _score(cls, item: ManifestItem, question: str) -> float:
        normalized_question = question.casefold()
        searchable = f"{item.domain} {item.description}".casefold()
        terms = cls._DOMAIN_TERMS.get(item.domain, ())
        question_match = 1.0 if any(term.casefold() in normalized_question for term in terms) else 0.0
        if normalized_question and normalized_question in searchable:
            question_match = 1.0
        quality = {
            "verified_original": 1.0,
            "available": 0.9,
            "completed": 0.85,
            "conditional": 0.55,
            "partial": 0.45,
            "conflicted": 0.4,
            "missing": 0.0,
            "failed": 0.0,
        }.get(item.quality_status.casefold(), 0.25)
        if (
            item.quality_status.casefold() not in {"missing", "failed"}
            and item.source_tier.casefold() == "verified_original"
        ):
            quality = max(quality, 1.0)
        conflict = 1.0 if (
            item.quality_status.casefold() == "conflicted"
            or any(term in searchable for term in ("冲突", "反证", "矛盾", "差异", "更正", "修正"))
        ) else 0.0
        recency = cls._recency_score(item)
        return 0.40 + 0.25 * question_match + 0.15 * quality + 0.10 * recency + 0.10 * conflict

    @staticmethod
    def _recency_score(item: ManifestItem) -> float:
        raw = item.as_of or item.observed_at
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        return max(0.0, min(1.0, (parsed.year - 2000 + parsed.timetuple().tm_yday / 366) / 40))

    @staticmethod
    def _select_within_budget(
        *,
        common_items: Sequence[ManifestItem],
        specialized_items: Sequence[ManifestItem],
        required_domains: set[str],
        token_budget: int,
    ) -> tuple[list[ManifestItem], list[ManifestItem]]:
        if token_budget <= 0:
            raise ValueError("token_budget 必须大于 0")
        selected_common: list[ManifestItem] = []
        selected_specialized: list[ManifestItem] = []
        used = 0

        def add(item: ManifestItem, target: list[ManifestItem]) -> None:
            nonlocal used
            if item not in target and used + item.token_estimate <= token_budget:
                target.append(item)
                used += item.token_estimate

        mandatory_common = next((item for item in common_items if item.domain == "common"), None)
        if mandatory_common is not None:
            add(mandatory_common, selected_common)
        for domain in sorted(required_domains):
            candidate = next((item for item in specialized_items if item.domain == domain), None)
            if candidate is not None:
                add(candidate, selected_specialized)
        for item in common_items:
            add(item, selected_common)
        for item in specialized_items:
            add(item, selected_specialized)
        selected_specialized.sort(key=lambda item: specialized_items.index(item))
        return selected_common, selected_specialized


@dataclass(frozen=True)
class EvidencePacket:
    packet_id: str
    role_id: str
    objective: str
    required_outputs: tuple[str, ...]
    evidence: tuple[EvidenceRecord, ...]
    known_gaps: tuple[str, ...]
    token_estimate: int
    sequence: int

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.evidence)

    def render(self) -> str:
        lines = [
            json.dumps(
                {
                    "packet_id": self.packet_id,
                    "role_id": self.role_id,
                    "objective": self.objective,
                    "required_outputs": self.required_outputs,
                    "known_gaps": self.known_gaps,
                },
                ensure_ascii=False,
            )
        ]
        for item in self.evidence:
            lines.extend(
                (
                    json.dumps(
                        {
                            "evidence_id": item.evidence_id,
                            "domain": item.domain,
                            "as_of": item.as_of,
                            "quality_status": item.quality_status,
                        },
                        ensure_ascii=False,
                    ),
                    item.compact_text,
                )
            )
        return "\n".join(lines)


class PacketEngine:
    def build(
        self,
        *,
        role_id: str,
        objective: str,
        records: Sequence[EvidenceRecord],
        token_budget: int | None = None,
        required_outputs: Sequence[str] = (),
        known_gaps: Sequence[str] = (),
    ) -> tuple[EvidencePacket, ...]:
        limit = token_budget if token_budget is not None else ContextBudget().evidence_budget
        if limit <= 0:
            raise ValueError("token_budget 必须大于 0")
        packets: list[EvidencePacket] = []
        sequence = 0
        expanded: list[EvidenceRecord] = []
        for record in records:
            expanded.extend(project_oversized_record(record, limit))
        grouped: dict[tuple[str, str, str, str], list[EvidenceRecord]] = {}
        for record in expanded:
            semantic_key = (record.domain, record.entity_id or "", record.as_of or "", objective)
            grouped.setdefault(semantic_key, []).append(record)
        for semantic_key in sorted(grouped):
            domain_records = sorted(grouped[semantic_key], key=lambda item: item.evidence_id)
            current: list[EvidenceRecord] = []
            current_tokens = 0
            for record in domain_records:
                if record.token_estimate > limit:
                    raise ValueError(f"单条证据超过 packet token 预算：{record.evidence_id}")
                if current and current_tokens + record.token_estimate > limit:
                    sequence += 1
                    packets.append(
                        self._packet(role_id, objective, current, required_outputs, known_gaps, sequence)
                    )
                    current, current_tokens = [], 0
                current.append(record)
                current_tokens += record.token_estimate
            if current:
                sequence += 1
                packets.append(self._packet(role_id, objective, current, required_outputs, known_gaps, sequence))
        return tuple(packets)

    @staticmethod
    def _packet(
        role_id: str,
        objective: str,
        records: Sequence[EvidenceRecord],
        required_outputs: Sequence[str],
        known_gaps: Sequence[str],
        sequence: int,
    ) -> EvidencePacket:
        digest = hashlib.sha256(
            json.dumps(
                {
                    "objective": objective,
                    "semantic_key": [
                        records[0].domain,
                        records[0].entity_id or "",
                        records[0].as_of or "",
                    ],
                    "evidence_ids": [item.evidence_id for item in records],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:12]
        return EvidencePacket(
            packet_id=f"PKT-{role_id.upper()}-{sequence:03d}-{digest}",
            role_id=role_id,
            objective=objective,
            required_outputs=tuple(required_outputs),
            evidence=tuple(records),
            known_gaps=tuple(known_gaps),
            token_estimate=sum(item.token_estimate for item in records),
            sequence=sequence,
        )


class DomainPacketBuilder:
    """Build at most three persistent domain packets per expert.

    A domain packet is a deterministic evidence organization unit. It does not
    imply an LLM call; the caller combines every packet into one expert
    synthesis request.
    """

    def build(
        self,
        *,
        role_id: str,
        objective: str,
        records: Sequence[EvidenceRecord],
        token_budget: int | None = None,
        required_outputs: Sequence[str] = (),
        known_gaps: Sequence[str] = (),
        max_packets: int = 3,
    ) -> tuple[EvidencePacket, ...]:
        limit = token_budget if token_budget is not None else ContextBudget().evidence_budget
        if limit <= 0:
            raise ValueError("token_budget 必须大于 0")
        if max_packets <= 0:
            raise ValueError("max_packets 必须大于 0")

        expanded: list[EvidenceRecord] = []
        for record in records:
            expanded.extend(project_oversized_record(record, limit))
        ordered = sorted(
            expanded,
            key=lambda item: (
                item.domain,
                item.entity_id or "",
                item.as_of or "",
                item.evidence_id,
            ),
        )
        total_tokens = sum(item.token_estimate for item in ordered)
        if total_tokens > limit * max_packets:
            raise ValueError(
                "角色证据超过最多 3 个 Domain Packet 的安全容量："
                f"{total_tokens} > {limit * max_packets}"
            )

        # First-fit decreasing handles the normal path cheaply. If it cannot
        # stay within the three-packet contract, bounded exact packing proves
        # whether a valid allocation exists before failing closed.
        bins: list[list[EvidenceRecord]] = []
        bin_tokens: list[int] = []
        failed_record: EvidenceRecord | None = None
        for record in sorted(ordered, key=lambda item: (-item.token_estimate, item.evidence_id)):
            placed = False
            for index, used in enumerate(bin_tokens):
                if used + record.token_estimate <= limit:
                    bins[index].append(record)
                    bin_tokens[index] += record.token_estimate
                    placed = True
                    break
            if placed:
                continue
            if len(bins) >= max_packets:
                failed_record = record
                break
            bins.append([record])
            bin_tokens.append(record.token_estimate)
        if failed_record is not None:
            bins = self._exact_pack(ordered, limit=limit, max_packets=max_packets)

        packets: list[EvidencePacket] = []
        for sequence, packet_records in enumerate(bins, 1):
            packet_records.sort(
                key=lambda item: (
                    item.domain,
                    item.entity_id or "",
                    item.as_of or "",
                    item.evidence_id,
                )
            )
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "objective": objective,
                        "evidence_ids": [item.evidence_id for item in packet_records],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:12]
            packets.append(
                EvidencePacket(
                    packet_id=f"PKT-{role_id.upper()}-DOMAIN-{sequence:02d}-{digest}",
                    role_id=role_id,
                    objective=objective,
                    required_outputs=tuple(required_outputs),
                    evidence=tuple(packet_records),
                    known_gaps=tuple(known_gaps),
                    token_estimate=sum(item.token_estimate for item in packet_records),
                    sequence=sequence,
                )
            )
        return tuple(packets)

    @staticmethod
    def _exact_pack(
        records: Sequence[EvidenceRecord],
        *,
        limit: int,
        max_packets: int,
    ) -> list[list[EvidenceRecord]]:
        ordered = sorted(records, key=lambda item: (-item.token_estimate, item.evidence_id))
        bins: list[list[EvidenceRecord]] = []
        used: list[int] = []
        rejected: set[tuple[int, tuple[int, ...]]] = set()

        def place(index: int) -> bool:
            if index == len(ordered):
                return True
            state = (index, tuple(sorted(used)))
            if state in rejected:
                return False
            record = ordered[index]
            seen_loads: set[int] = set()
            for bin_index, load in enumerate(used):
                if load in seen_loads or load + record.token_estimate > limit:
                    continue
                seen_loads.add(load)
                bins[bin_index].append(record)
                used[bin_index] += record.token_estimate
                if place(index + 1):
                    return True
                used[bin_index] -= record.token_estimate
                bins[bin_index].pop()
            if len(bins) < max_packets:
                bins.append([record])
                used.append(record.token_estimate)
                if place(index + 1):
                    return True
                used.pop()
                bins.pop()
            rejected.add(state)
            return False

        if not place(0):
            raise ValueError("角色证据无法在不丢失内容的前提下合并为最多 3 个 Domain Packet")
        return bins


@dataclass(frozen=True)
class ExpertClaim:
    claim_id: str
    claim: str
    status: str
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    confidence: str
    conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpertResearchState:
    role_id: str
    question: str = ""
    claims: Mapping[str, ExpertClaim] = field(default_factory=dict)
    requirement_coverage: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    open_questions: tuple[str, ...] = ()
    processed_packet_ids: tuple[str, ...] = ()
    revision: int = 0

    @classmethod
    def initial(cls, role_id: str, question: str = "") -> ExpertResearchState:
        return cls(role_id=role_id, question=question)

    def merge(
        self,
        packet: EvidencePacket,
        new_state: Mapping[str, object],
    ) -> ExpertResearchState:
        return self.revise(new_state, processed_packet_ids=(packet.packet_id,))

    def revise(
        self,
        new_state: Mapping[str, object],
        *,
        processed_packet_ids: Sequence[str] = (),
    ) -> ExpertResearchState:
        claim_updates = new_state.get("claims") or []
        if not isinstance(claim_updates, Sequence):
            raise ValueError("claims 必须是数组")
        claims = dict(self.claims)
        for update in claim_updates:
            if not isinstance(update, Mapping):
                raise ValueError("claim update 必须是 object")
            supporting = tuple(str(item) for item in update.get("supporting_evidence_ids", []))
            contradicting = tuple(str(item) for item in update.get("contradicting_evidence_ids", []))
            status = str(update["status"])
            if status == "supported" and not supporting:
                raise ValueError("supported claim 至少需要一条支持证据")
            claim = ExpertClaim(
                claim_id=str(update["claim_id"]),
                claim=str(update["claim"]),
                status=status,
                supporting_evidence_ids=supporting,
                contradicting_evidence_ids=contradicting,
                confidence=str(update.get("confidence") or "low"),
                conditions=tuple(str(item) for item in update.get("conditions", [])),
            )
            claims[claim.claim_id] = claim
        coverage_update = new_state.get("framework_requirements")
        if coverage_update is not None and not isinstance(coverage_update, Mapping):
            raise ValueError("framework_requirements 必须是 object")
        coverage = dict(self.requirement_coverage)
        if isinstance(coverage_update, Mapping):
            coverage.update({str(key): value for key, value in coverage_update.items()})
        open_questions = new_state.get("open_questions")
        if open_questions is not None and not isinstance(open_questions, Sequence):
            raise ValueError("open_questions 必须是数组")
        processed = tuple(dict.fromkeys((*self.processed_packet_ids, *processed_packet_ids)))
        return replace(
            self,
            claims=claims,
            requirement_coverage=coverage,
            open_questions=tuple(open_questions) if open_questions is not None else self.open_questions,
            processed_packet_ids=processed,
            revision=self.revision + 1,
        )

    def synthesize(
        self,
        packets: Sequence[EvidencePacket],
        new_state: Mapping[str, object],
    ) -> ExpertResearchState:
        """Apply one semantic revision after deterministic ingestion of all packets."""

        if not packets:
            return self.revise(new_state)
        return self.revise(
            new_state,
            processed_packet_ids=tuple(packet.packet_id for packet in packets),
        )

    def with_coverage(
        self, coverage: Mapping[str, Mapping[str, object]]
    ) -> ExpertResearchState:
        merged = dict(coverage)
        merged.update(self.requirement_coverage)
        return replace(self, requirement_coverage=merged)

    def ingest(
        self,
        packet: EvidencePacket,
        *,
        coverage: Mapping[str, Mapping[str, object]] | None = None,
    ) -> ExpertResearchState:
        """Record deterministic packet metadata without a semantic revision."""

        merged_coverage = dict(self.requirement_coverage)
        if coverage is not None:
            merged_coverage.update(coverage)
        processed = tuple(dict.fromkeys((*self.processed_packet_ids, packet.packet_id)))
        return replace(
            self,
            requirement_coverage=merged_coverage,
            processed_packet_ids=processed,
        )

    def validate(
        self,
        packet: EvidencePacket | None = None,
        known_evidence_ids: Iterable[str] = (),
    ) -> None:
        known = set(known_evidence_ids)
        if packet:
            known.update(packet.evidence_ids)
            if packet.packet_id not in self.processed_packet_ids:
                raise ValueError("当前 packet 未写入 processed_packet_ids")
        for claim in self.claims.values():
            cited = set(claim.supporting_evidence_ids) | set(claim.contradicting_evidence_ids)
            unknown = sorted(cited - known) if known else []
            if unknown:
                raise ValueError("专家状态包含未知证据引用：" + "、".join(unknown))

    def as_dict(self) -> dict[str, object]:
        return {
            "role_id": self.role_id,
            "question": self.question,
            "claims": [vars(item) for item in self.claims.values()],
            "requirement_coverage": self.requirement_coverage,
            "open_questions": self.open_questions,
            "processed_packet_ids": self.processed_packet_ids,
            "revision": self.revision,
        }


VALID_COVERAGE_STATES = {"covered", "partial", "unavailable", "not_applicable", "conflicted"}


@dataclass(frozen=True)
class CoverageResult:
    statuses: Mapping[str, str]
    can_complete: bool
    actionable_requirements: tuple[str, ...]


class CoverageGate:
    def evaluate(
        self,
        *,
        role_id: str,
        state: ExpertResearchState,
        manifest: EvidenceManifest,
    ) -> CoverageResult:
        coverage = state.requirement_coverage
        mandatory = tuple(label for label, _skill_id, _ceiling in FRAMEWORK_REQUIREMENTS.get(role_id, ()))
        requirements = tuple(dict.fromkeys((*mandatory, *coverage)))
        known_ids = {item.evidence_id for item in manifest.items}
        statuses: dict[str, str] = {}
        actionable: list[str] = []
        for requirement in requirements:
            item = coverage.get(requirement) or {"status": "partial", "evidence_ids": []}
            status = str(item.get("status") or "partial")
            if status not in VALID_COVERAGE_STATES:
                raise ValueError(f"未知覆盖状态：{status}")
            if status == "unavailable":
                for field_name in ("attempted_sources", "reason", "alternatives", "impact"):
                    if field_name not in item:
                        raise ValueError(f"unavailable 缺少字段：{field_name}")
            cited = {str(item) for item in item.get("evidence_ids", [])}
            unknown = sorted(cited - known_ids)
            if unknown:
                raise ValueError("覆盖状态包含未知证据引用：" + "、".join(unknown))
            statuses[requirement] = status
            if status == "partial":
                actionable.append(requirement)
        return CoverageResult(
            statuses=statuses,
            can_complete=self.can_complete(statuses, requirements),
            actionable_requirements=tuple(actionable),
        )

    @staticmethod
    def can_complete(statuses: Mapping[str, str], requirements: Sequence[str]) -> bool:
        terminal = {"covered", "unavailable", "not_applicable", "conflicted"}
        return all(statuses.get(requirement) in terminal for requirement in requirements)


class ConflictTrigger:
    """Deterministically decide whether supplemental evidence needs semantic revision."""

    _CONFLICT_TERMS = ("冲突", "反证", "更正", "修正", "不一致", "contradict")
    _RISK_DOMAINS = frozenset({"portfolio-risk-evidence", "execution-liquidity-evidence"})
    _USABLE_QUALITY = frozenset(
        {"available", "completed", "verified", "verified_original", "conflicted"}
    )

    def should_revise(
        self,
        *,
        new_records: Sequence[EvidenceRecord],
        coverage_changed: bool,
    ) -> bool:
        if coverage_changed:
            return True
        return any(
            record.quality_status.casefold() == "conflicted"
            or (
                record.domain in self._RISK_DOMAINS
                and record.quality_status.casefold() in self._USABLE_QUALITY
            )
            or any(term in record.compact_text.casefold() for term in self._CONFLICT_TERMS)
            for record in new_records
        )


@dataclass(frozen=True)
class ClaimConflict:
    claim_id: str
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    roles: tuple[str, ...]
    resolved: bool = False


@dataclass(frozen=True)
class ClaimBoard:
    claims: tuple[tuple[str, ExpertClaim], ...]
    conflicts: Mapping[str, ClaimConflict]
    consensus: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    dissent: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "claims": [
                {"role_id": role_id, **vars(claim)} for role_id, claim in self.claims
            ],
            "consensus": self.consensus,
            "dissent": self.dissent,
            "conflicts": {
                claim_key: vars(conflict) for claim_key, conflict in self.conflicts.items()
            },
        }

    @classmethod
    def from_states(cls, states: Sequence[ExpertResearchState]) -> ClaimBoard:
        entries: list[tuple[str, ExpertClaim]] = []
        grouped: dict[str, list[tuple[str, ExpertClaim]]] = {}
        for state in states:
            for claim in state.claims.values():
                entries.append((state.role_id, claim))
                grouped.setdefault(claim.claim_id, []).append((state.role_id, claim))
        conflicts: dict[str, ClaimConflict] = {}
        consensus: dict[str, tuple[str, ...]] = {}
        dissent: list[str] = []
        for claim_id, items in grouped.items():
            supporting = tuple(dict.fromkeys(eid for _, claim in items for eid in claim.supporting_evidence_ids))
            contradicting = tuple(
                dict.fromkeys(eid for _, claim in items for eid in claim.contradicting_evidence_ids)
            )
            if supporting and contradicting:
                conflicts[claim_id] = ClaimConflict(
                    claim_id=claim_id,
                    supporting_evidence_ids=supporting,
                    contradicting_evidence_ids=contradicting,
                    roles=tuple(dict.fromkeys(role_id for role_id, _ in items)),
                )
                dissent.append(claim_id)
            roles = tuple(dict.fromkeys(role_id for role_id, _ in items))
            statuses = {claim.status for _role_id, claim in items}
            if len(roles) > 1 and len(statuses) == 1 and claim_id not in conflicts:
                consensus[claim_id] = roles
        return cls(tuple(entries), conflicts, consensus, tuple(dissent))


@dataclass(frozen=True)
class RiskReviewItem:
    risk_id: str
    category: str
    status: str
    description: str
    evidence_ids: tuple[str, ...]
    affected_roles: tuple[str, ...]
    conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskReviewState:
    items: tuple[RiskReviewItem, ...]
    dimensions: Mapping[str, Mapping[str, object]]
    unresolved_gaps: tuple[str, ...]

    @classmethod
    def build(
        cls,
        *,
        states: Sequence[ExpertResearchState],
        board: ClaimBoard,
        store: EvidenceStore,
    ) -> RiskReviewState:
        records = {record.evidence_id: record for record in store.records}
        items: list[RiskReviewItem] = []
        for role_id, claim in board.claims:
            cited = tuple(dict.fromkeys((*claim.supporting_evidence_ids, *claim.contradicting_evidence_ids)))
            domains = {records[item].domain for item in cited if item in records}
            is_risk = bool(
                claim.contradicting_evidence_ids
                or claim.status in {"conditional", "conflicted", "unsupported"}
                or domains.intersection({"portfolio-risk-evidence", "execution-liquidity-evidence"})
            )
            if not is_risk:
                continue
            category = (
                "portfolio"
                if "portfolio-risk-evidence" in domains
                else "liquidity"
                if "execution-liquidity-evidence" in domains
                else "thesis"
            )
            items.append(
                RiskReviewItem(
                    risk_id=f"RISK-{hashlib.sha256(f'{role_id}:{claim.claim_id}'.encode()).hexdigest()[:12]}",
                    category=category,
                    status=claim.status,
                    description=claim.claim,
                    evidence_ids=cited,
                    affected_roles=(role_id,),
                    conditions=claim.conditions,
                )
            )
        for conflict_id, conflict in board.conflicts.items():
            items.append(
                RiskReviewItem(
                    risk_id=f"RISK-CONFLICT-{hashlib.sha256(conflict_id.encode()).hexdigest()[:12]}",
                    category="conflict",
                    status="conflicted",
                    description=f"观点冲突：{conflict_id}",
                    evidence_ids=tuple(
                        dict.fromkeys(
                            (*conflict.supporting_evidence_ids, *conflict.contradicting_evidence_ids)
                        )
                    ),
                    affected_roles=conflict.roles,
                )
            )
        gaps = tuple(dict.fromkeys(question for state in states for question in state.open_questions))
        def evidence_with_fields(domain: str, fields: set[str]) -> tuple[str, ...]:
            def contains(value: object) -> bool:
                if isinstance(value, Mapping):
                    return bool(fields.intersection(str(key).casefold() for key in value)) or any(
                        contains(item) for item in value.values()
                    )
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    return any(contains(item) for item in value)
                return False

            return tuple(
                record.evidence_id
                for record in store.records
                if record.domain == domain
                and record.quality_status.casefold() not in {"missing", "failed"}
                and contains(record.value)
            )

        dimensions: dict[str, Mapping[str, object]] = {}
        for name, domain, fields in (
            (
                "portfolio_concentration",
                "portfolio-risk-evidence",
                {"hhi", "weights", "concentration", "holding_weights"},
            ),
            ("correlation", "portfolio-risk-evidence", {"correlation", "correlations"}),
            (
                "liquidity",
                "execution-liquidity-evidence",
                {"spread", "order_book", "liquidity", "turnover", "execution_cost"},
            ),
        ):
            evidence_ids = evidence_with_fields(domain, fields)
            dimensions[name] = {
                "status": "covered" if evidence_ids else "unavailable",
                "evidence_ids": evidence_ids,
                "gaps": [] if evidence_ids else [f"未取得{name}证据，不得推断"],
            }
        constraint_ids = tuple(
            record.evidence_id
            for record in store.records
            if record.domain == "common"
            and isinstance(record.value, Mapping)
            and isinstance(record.value.get("portfolio_profile"), Mapping)
            and record.value["portfolio_profile"].get("max_drawdown_percent")
        )
        dimensions["user_constraints"] = {
            "status": "covered" if constraint_ids else "unavailable",
            "evidence_ids": constraint_ids,
            "gaps": [] if constraint_ids else ["未录入用户可承受回撤阈值，不得推断"],
        }
        dimensions["security_risks"] = {
            "status": "covered" if items else "unavailable",
            "evidence_ids": tuple(dict.fromkeys(eid for item in items for eid in item.evidence_ids)),
            "gaps": [] if items else ["未形成可引用的单标的风险结论"],
        }
        return cls(tuple(items), dimensions, gaps)

    def as_dict(self) -> dict[str, object]:
        return {
            "items": [vars(item) for item in self.items],
            "dimensions": self.dimensions,
            "unresolved_gaps": self.unresolved_gaps,
        }


@dataclass(frozen=True)
class ReportSectionContext:
    section_id: str
    evidence_ids: tuple[str, ...]
    text: str


class ReportSectionBuilder:
    TEMPLATES = {
        "market": ("executive_summary", "market_structure", "portfolio_exposure", "m1_m6", "risks"),
        "company": (
            "executive_summary",
            "core_conflict",
            "financial_quality",
            "operations_governance",
            "valuation",
            "market_execution",
            "consensus_dissent",
            "risks_catalysts",
        ),
        "fund": (
            "executive_summary",
            "fund_contract",
            "holdings_exposure",
            "performance_liquidity",
            "valuation",
            "consensus_dissent",
            "risks_conditions",
        ),
    }
    TITLES = {
        "executive_summary": "执行摘要",
        "market_structure": "市场结构",
        "portfolio_exposure": "本地持仓暴露",
        "m1_m6": "M1–M6 市场复盘",
        "risks": "风险与观察条件",
        "core_conflict": "核心矛盾",
        "financial_quality": "财务质量",
        "operations_governance": "经营、治理与资本配置",
        "valuation": "估值与安全边际",
        "market_execution": "市场与交易实现",
        "consensus_dissent": "委员会共识与分歧",
        "risks_catalysts": "风险、催化剂与条件",
        "fund_contract": "基金合同与产品特征",
        "holdings_exposure": "持仓与行业暴露",
        "performance_liquidity": "业绩与流动性",
        "risks_conditions": "风险与重估条件",
    }

    def __init__(self, store: EvidenceStore) -> None:
        self.store = store

    def build_context(
        self,
        *,
        section_id: str,
        claims: Sequence[Mapping[str, object]],
    ) -> ReportSectionContext:
        evidence_ids = tuple(
            dict.fromkeys(
                str(evidence_id)
                for claim in claims
                for key in ("supporting_evidence_ids", "contradicting_evidence_ids")
                for evidence_id in claim.get(key, [])
            )
        )
        evidence = [self.store.get(evidence_id) for evidence_id in evidence_ids]
        text = json.dumps(
            {
                "section_id": section_id,
                "claims": list(claims),
                "evidence": [
                    {"evidence_id": item.evidence_id, "compact_text": item.compact_text}
                    for item in evidence
                ],
            },
            ensure_ascii=False,
        )
        return ReportSectionContext(section_id, evidence_ids, text)

    def build_deterministic_section(
        self,
        *,
        section_id: str,
        claims: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        """Create a recoverable section checkpoint without invoking an LLM."""

        title = self.TITLES.get(section_id, section_id)
        cited = tuple(
            dict.fromkeys(
                str(evidence_id)
                for claim in claims
                for key in ("supporting_evidence_ids", "contradicting_evidence_ids")
                for evidence_id in claim.get(key, [])
            )
        )
        lines = [f"## {title}"]
        if claims:
            for claim in claims:
                lines.append("- " + str(claim.get("claim") or ""))
                conditions = [str(item) for item in claim.get("conditions") or [] if str(item)]
                if conditions:
                    lines.append("  - 条件：" + "；".join(conditions))
        else:
            lines = []
        return {
            "section_id": section_id,
            "title": title,
            "content": "\n".join(lines),
            "claim_ids": [str(claim.get("claim_id") or "") for claim in claims],
            "cited_evidence_ids": cited,
            "generation": "deterministic",
        }

    @staticmethod
    def fallback_payload(
        *,
        completed_sections: Mapping[str, str],
        expert_states: Mapping[str, object],
        risks: Sequence[str],
        gaps: Sequence[str],
        final_edit_error: str,
    ) -> dict[str, object]:
        return {
            "status": "partial",
            "unified_edit_completed": False,
            "content": "\n\n".join(
                content for content in completed_sections.values() if str(content).strip()
            ),
            "completed_sections": dict(completed_sections),
            "expert_states": dict(expert_states),
            "risks": list(risks),
            "gaps": list(gaps),
            "final_edit_error": final_edit_error,
        }


class QualityMetrics:
    """Deterministic acceptance metrics; callers supply their gold/necessary sets."""

    @staticmethod
    def evidence_recall(used_ids: Iterable[str], necessary_ids: Iterable[str]) -> float:
        necessary = set(necessary_ids)
        return 1.0 if not necessary else len(set(used_ids) & necessary) / len(necessary)

    @staticmethod
    def framework_coverage(statuses: Mapping[str, str]) -> float:
        if not statuses:
            return 1.0
        terminal = {"covered", "unavailable", "not_applicable", "conflicted"}
        return sum(status in terminal for status in statuses.values()) / len(statuses)

    @staticmethod
    def citation_validity(valid_citations: int, total_citations: int) -> float:
        return 1.0 if total_citations == 0 else valid_citations / total_citations

    @staticmethod
    def contradiction_preservation(preserved: int, expected: int) -> float:
        return 1.0 if expected == 0 else preserved / expected

    @staticmethod
    def report_completion(completed: int, total: int) -> float:
        return 1.0 if total == 0 else completed / total
