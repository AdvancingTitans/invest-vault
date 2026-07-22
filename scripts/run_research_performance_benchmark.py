"""Run the fixed Phase 64 committee workload against an authenticated Codex provider."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from invest_vault.api import create_app

PHASE64_COMMITTEE_REQUEST = (
    "请用六人投研委员会全面复盘贵州茅台，覆盖财务质量、估值、治理、"
    "组合集中度、相关性、流动性及主要反证。"
)


class Phase64ReplaySkillLayer:
    """Replay only the controlled public-evidence rows from the Phase 64 fixture."""

    def __init__(self, fixture_database: Path) -> None:
        self.fixture_database = fixture_database

    def catalog(self) -> list[dict[str, str]]:
        return []

    def run(
        self,
        *,
        security_id: str,
        question: str,
        role_id: str = "general",
    ) -> list[dict[str, object]]:
        del question
        grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
        domain_statuses: dict[str, list[str]] = defaultdict(list)
        with sqlite3.connect(self.fixture_database) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT evidence_id, domain, subtype, as_of, provider, source_ref,
                          quality_status, value_json
                   FROM research_evidence_records
                   WHERE security_id = ? AND domain != 'common'
                   ORDER BY domain, evidence_id""",
                (security_id,),
            ).fetchall()
        for row in rows:
            domain = str(row["domain"])
            grouped[domain].append(
                {
                    "evidence_id": str(row["evidence_id"]),
                    "kind": str(row["subtype"]),
                    "value": json.loads(str(row["value_json"])),
                    "as_of": str(row["as_of"] or ""),
                    "provider": str(row["provider"]),
                    "source_ref": str(row["source_ref"]),
                }
            )
            domain_statuses[domain].append(str(row["quality_status"]))
        results = [
            {
                "skill_id": domain,
                "name": domain,
                "status": (
                    "completed"
                    if all(status in {"available", "completed", "verified", "verified_original"}
                           for status in domain_statuses[domain])
                    else "partial"
                ),
                "gaps": [],
                "evidence": evidence,
            }
            for domain, evidence in grouped.items()
        ]
        results.append(
            {
                "skill_id": "framework-readiness",
                "name": "framework-readiness",
                "status": "completed",
                "gaps": [],
                "evidence": [
                    {
                        "evidence_id": f"EVIDENCE-BENCHMARK-READINESS-{role_id}",
                        "kind": "framework-readiness",
                        "value": {"role_id": role_id, "requirements": []},
                        "as_of": "2026-07-22",
                        "provider": "Phase 64 controlled fixture",
                        "source_ref": "",
                    }
                ],
            }
        )
        return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-database", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--reasoning-effort", default="low")
    parser.add_argument("--timeout-seconds", type=float, default=720.0)
    parser.add_argument("--summarize-existing", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_directory = args.output_directory.resolve()
    fixture_database = args.fixture_database.resolve()
    if args.summarize_existing:
        database = output_directory / "vault.sqlite3"
        if not database.is_file():
            raise FileNotFoundError(database)
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            run_row = connection.execute(
                "SELECT * FROM research_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if run_row is None:
            raise RuntimeError("No benchmark run exists")
        run = dict(run_row)
        thread = {"thread_id": str(run["thread_id"])}
        started_at = datetime.fromisoformat(str(run["started_at"]))
        completed_at = datetime.fromisoformat(str(run["completed_at"]))
        elapsed_seconds = round((completed_at - started_at).total_seconds(), 2)
    else:
        output_directory.mkdir(parents=True, exist_ok=False)
        shutil.copy2(fixture_database, output_directory / "vault.sqlite3")
        started = time.monotonic()
        with TestClient(
            create_app(
                output_directory,
                automatic_updates=False,
                research_skill_layer=Phase64ReplaySkillLayer(fixture_database),
            )
        ) as client:
            status = client.get("/api/ai/status").json()
            if not status.get("authenticated"):
                raise RuntimeError("Codex is not authenticated")
            for task in ("research", "committee"):
                response = client.put(
                    f"/api/ai/settings/models/{task}",
                    json={
                        "provider_id": "codex",
                        "model_id": args.model,
                        "reasoning_effort": args.reasoning_effort,
                    },
                )
                response.raise_for_status()
            thread = client.post(
                "/api/ai/chats",
                json={
                    "security_id": "CN:SSE:600519:STOCK",
                    "role_id": "general",
                    "mode": "committee",
                    "title": "Phase 65 性能基准",
                },
            ).json()
            client.post(
                f"/api/ai/chats/{thread['thread_id']}/messages",
                json={"content": PHASE64_COMMITTEE_REQUEST, "role_id": "general"},
            ).raise_for_status()
            deadline = time.monotonic() + args.timeout_seconds
            while True:
                restored = client.get(f"/api/ai/chats/{thread['thread_id']}").json()
                run = restored.get("active_run") or {}
                if run.get("status") != "running":
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Benchmark exceeded its timeout")
                time.sleep(1)
        elapsed_seconds = round(time.monotonic() - started, 2)

    run_id = str(run["run_id"])
    with sqlite3.connect(output_directory / "vault.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        performance = connection.execute(
            "SELECT * FROM research_performance_summaries WHERE run_id = ?", (run_id,)
        ).fetchone()
        calls = connection.execute(
            """SELECT stage, role_id, node_id, latency_ms, usage_source,
                      estimated_input_tokens, estimated_output_tokens, error_json,
                      started_at, completed_at
               FROM research_call_metrics WHERE run_id = ? ORDER BY created_at""",
            (run_id,),
        ).fetchall()
        report = connection.execute(
            "SELECT report_json FROM research_reports WHERE run_id = ?", (run_id,)
        ).fetchone()
        available_ids = {
            str(row["evidence_id"])
            for row in connection.execute(
                """SELECT DISTINCT evidence_id FROM research_evidence_links
                   WHERE run_id = ?""",
                (run_id,),
            )
        }
        expert_states = [
            json.loads(str(row["state_json"]))
            for row in connection.execute(
                """SELECT state.state_json FROM research_expert_states state
                   JOIN (
                     SELECT role_id, MAX(revision) AS revision
                     FROM research_expert_states WHERE run_id = ? GROUP BY role_id
                   ) latest ON latest.role_id = state.role_id AND latest.revision = state.revision
                   WHERE state.run_id = ? ORDER BY state.role_id""",
                (run_id, run_id),
            )
        ]
        packet_rows = connection.execute(
            """SELECT role_id, packet_id FROM research_evidence_packets
               WHERE run_id = ? ORDER BY role_id, sequence_number""",
            (run_id,),
        ).fetchall()
        completed_sections = connection.execute(
            """SELECT COUNT(*) FROM research_report_sections
               WHERE run_id = ? AND status = 'completed'""",
            (run_id,),
        ).fetchone()[0]
    with sqlite3.connect(fixture_database) as fixture_connection:
        necessary_ids = {
            str(row[0])
            for row in fixture_connection.execute(
                "SELECT evidence_id FROM research_evidence_records WHERE domain != 'common'"
            )
        }
    report_payload = json.loads(str(report["report_json"])) if report else None
    cited_ids = set(report_payload.get("cited_evidence_ids") or []) if report_payload else set()
    supporting_ids = {
        str(evidence_id)
        for state in expert_states
        for claim in state.get("claims") or []
        for evidence_id in claim.get("supporting_evidence_ids") or []
    }
    used_ids = {
        evidence_id
        for state in expert_states
        for claim in state.get("claims") or []
        for field in ("supporting_evidence_ids", "contradicting_evidence_ids")
        for evidence_id in claim.get(field) or []
    }
    coverage_statuses = [
        str(detail.get("status"))
        for state in expert_states
        for detail in (state.get("requirement_coverage") or {}).values()
    ]
    terminal_statuses = {"covered", "unavailable", "not_applicable", "conflicted"}
    domain_packets = [row for row in packet_rows if "-DOMAIN-" in str(row["packet_id"])]
    domain_packets_per_expert = {
        role_id: sum(str(row["role_id"]) == role_id for row in domain_packets)
        for role_id in {str(row["role_id"]) for row in domain_packets}
    }
    citation_validity = (
        sum(
            evidence_id in available_ids and evidence_id in supporting_ids
            for evidence_id in cited_ids
        )
        / len(cited_ids)
        if cited_ids
        else 0.0
    )
    evidence_recall = len(used_ids & necessary_ids) / len(necessary_ids) if necessary_ids else 1.0
    framework_coverage = (
        sum(status in terminal_statuses for status in coverage_statuses) / len(coverage_statuses)
        if coverage_statuses
        else 0.0
    )
    expert_call_count = sum(str(call["stage"]) == "expert_synthesis" for call in calls)
    section_call_count = sum(str(call["stage"]) == "report_section" for call in calls)
    final_editor_call_count = sum(str(call["stage"]) == "final_edit" for call in calls)
    semantic_revision_count = sum(int(state["revision"]) for state in expert_states)
    acceptance_values = {
        "expert_count": len({str(state["role_id"]) for state in expert_states}),
        "persistent_domain_packet_count": len(domain_packets),
        "max_domain_packets_per_expert": max(domain_packets_per_expert.values(), default=0),
        "ingestion_checkpoint_count": len(packet_rows),
        "expert_synthesis_call_count": expert_call_count,
        "semantic_revision_count": semantic_revision_count,
        "section_llm_call_count": section_call_count,
        "final_editor_call_count": final_editor_call_count,
        "completed_deterministic_sections": int(completed_sections),
        "final_context_tokens": int(performance["final_context_tokens"] if performance else 0),
        "evidence_recall": round(evidence_recall, 4),
        "mandatory_framework_coverage": round(framework_coverage, 4),
        "citation_validity": round(citation_validity, 4),
    }
    gates = {
        "runtime": elapsed_seconds < 600,
        "completed": str(run["status"]) == "completed",
        "expert_count": acceptance_values["expert_count"] == 6,
        "domain_packet_count": acceptance_values["persistent_domain_packet_count"] <= 18,
        "domain_packets_per_expert": acceptance_values["max_domain_packets_per_expert"] <= 3,
        "expert_synthesis_calls": expert_call_count == 6,
        "semantic_revisions": semantic_revision_count <= 10,
        "section_llm_calls": section_call_count == 0,
        "final_editor_calls": final_editor_call_count == 1,
        "final_context": 0 < acceptance_values["final_context_tokens"] < 30_000,
        "evidence_recall": evidence_recall >= 1.0,
        "mandatory_framework_coverage": framework_coverage >= 1.0,
        "citation_validity": citation_validity >= 0.95,
    }
    acceptance_passed = all(gates.values())
    baseline_path = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "performance_optimization_baseline.json"
    )
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    summary = {
        "protocol": {
            "fixture": str(fixture_database),
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "provider": "codex",
            "request": PHASE64_COMMITTEE_REQUEST,
        },
        "thread_id": str(thread["thread_id"]),
        "run_id": run_id,
        "run_status": str(run["status"]),
        "elapsed_seconds": elapsed_seconds,
        "target_seconds": 600,
        "runtime_target_met": gates["runtime"],
        "acceptance_passed": acceptance_passed,
        "gates": gates,
        "baseline": baseline,
        "performance": dict(performance) if performance else None,
        "acceptance": acceptance_values,
        "calls": [dict(call) for call in calls],
        "report_status": (
            "completed" if report_payload and report_payload.get("unified_edit_completed") else "partial"
        ),
        "report": report_payload,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    (output_directory / "benchmark-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in summary.items() if key != "report"}, ensure_ascii=False))
    if not acceptance_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
