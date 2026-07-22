from datetime import date
from pathlib import Path

from invest_vault import ResearchStore, Vault
from invest_vault.exports import create_backup, export_markdown, restore_backup


def test_revisioned_research_has_a_bounded_timeline(tmp_path: Path) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        research = ResearchStore(vault)
        first = research.revise_thesis(security_id="CN:SSE:600519:STOCK", body="Demand needs review.")
        second = research.revise_thesis(
            security_id="CN:SSE:600519:STOCK", thesis_id=first.thesis_id, body="Demand confirmed."
        )
        research.add_note(security_id="CN:SSE:600519:STOCK", body="Read the filing.")
        assert second.revision_number == 2
        assert research.current_thesis("CN:SSE:600519:STOCK").body == "Demand confirmed."
        timeline = research.timeline("CN:SSE:600519:STOCK", limit=2)
        assert timeline["total_count"] == 3
        assert len(timeline["items"]) == 2
        assert timeline["next_cursor"]


def test_deleting_a_note_removes_revisions_timeline_and_attachment_file(tmp_path: Path) -> None:
    vault_directory = tmp_path / "vault"
    source = tmp_path / "attachment.txt"
    source.write_text("private note attachment", encoding="utf-8")
    with Vault(vault_directory / "vault.sqlite3") as vault:
        research = ResearchStore(vault, vault_directory)
        note_id = research.add_note(security_id="CN:SSE:600519:STOCK", body="原始笔记")
        research.revise_note(note_id, security_id="CN:SSE:600519:STOCK", body="修订笔记")
        research.add_attachment(note_id, source, "text/plain")
        stored_path = Path(
            vault.connection.execute(
                "SELECT storage_path FROM attachments WHERE note_id = ?", (note_id,)
            ).fetchone()[0]
        )
        evidence_id = f"EVIDENCE-NOTE-{note_id}"
        vault.connection.execute(
            """INSERT INTO research_evidence_records
            (evidence_id, security_id, domain, subtype, entity_id, as_of, observed_at,
             source_tier, provider, source_ref, quality_status, value_json, compact_text,
             token_estimate, content_hash)
            VALUES (?, 'CN:SSE:600519:STOCK', 'user-judgement', 'note', NULL, NULL,
                    '2026-07-22T00:00:00Z', 'user_authored', 'Invest Vault本地笔记', '',
                    'available', '{"body":"修订笔记"}', '用户笔记：修订笔记', 8, ?)""",
            (evidence_id, f"hash-{note_id}"),
        )
        vault.connection.execute(
            "INSERT INTO research_evidence_links VALUES ('run-test', NULL, ?, 'available')",
            (evidence_id,),
        )
        vault.connection.commit()

        research.delete_note(note_id)

        assert not stored_path.exists()
        assert vault.connection.execute("SELECT COUNT(*) FROM notes WHERE note_id = ?", (note_id,)).fetchone()[0] == 0
        assert vault.connection.execute("SELECT COUNT(*) FROM note_revisions WHERE note_id = ?", (note_id,)).fetchone()[0] == 0
        assert vault.connection.execute("SELECT COUNT(*) FROM timeline_events WHERE reference_id = ?", (note_id,)).fetchone()[0] == 0
        assert vault.connection.execute(
            "SELECT COUNT(*) FROM research_evidence_records WHERE evidence_id LIKE ?",
            (f"{evidence_id}%",),
        ).fetchone()[0] == 0
        assert vault.connection.execute(
            "SELECT COUNT(*) FROM research_evidence_links WHERE evidence_id LIKE ?",
            (f"{evidence_id}%",),
        ).fetchone()[0] == 0


def test_backup_restore_and_markdown_export_are_verifiable(tmp_path: Path) -> None:
    with Vault(tmp_path / "vault" / "vault.sqlite3") as vault:
        ResearchStore(vault).add_note(security_id="CN:SSE:600519:STOCK", body="关注经营现金流。")
        output = export_markdown(vault, tmp_path / "export.md", data_cutoff="2026-07-12T16:00:00Z")
        backup = create_backup(tmp_path / "vault", tmp_path / "vault-backup.zip")
        exported = output.read_text(encoding="utf-8")
        assert "数据截止" in exported
        assert "关注经营现金流" in exported
    restore_backup(backup, tmp_path / "restored")
    assert (tmp_path / "restored" / "vault.sqlite3").exists()


def test_research_workspace_does_not_hide_financial_reports_behind_newer_announcements(tmp_path: Path) -> None:
    with Vault(tmp_path / "vault.sqlite3") as vault:
        research = ResearchStore(vault)
        security_id = "CN:SSE:600519:STOCK"
        research.add_material(security_id=security_id, material_type="财务报告", title="季度报告", published_at=date(2026, 3, 31), source_name="公告中心", source_url="https://example.test/report")
        for index in range(12):
            research.add_material(security_id=security_id, material_type="公司公告", title=f"公告{index}", published_at=date(2026, 6, 1), source_name="公告中心", source_url=f"https://example.test/notice/{index}")
        workspace = research.workspace(security_id)
        assert any(item["material_type"] == "财务报告" for item in workspace["materials"])
