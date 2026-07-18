"""Revisioned user research records and bounded timeline projections."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from .ledger import Vault


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ThesisRevision:
    revision_id: str
    thesis_id: str
    revision_number: int
    body: str
    cited_snapshot_ids: tuple[str, ...]
    review_due_on: str | None
    created_at: str


class ResearchStore:
    def __init__(self, vault: Vault, vault_directory: Path | None = None) -> None:
        self.vault = vault
        self.vault_directory = Path(vault_directory or vault.database_path.parent)

    def revise_thesis(
        self,
        *,
        security_id: str,
        body: str,
        cited_snapshot_ids: tuple[str, ...] = (),
        review_due_on: date | None = None,
        thesis_id: str | None = None,
    ) -> ThesisRevision:
        if not body.strip():
            raise ValueError("thesis body cannot be empty")
        thesis_id = thesis_id or str(uuid4())
        connection = self.vault.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                "SELECT security_id FROM theses WHERE thesis_id = ?", (thesis_id,)
            ).fetchone()
            if existing is None:
                connection.execute("INSERT INTO theses VALUES (?, ?, ?)", (thesis_id, security_id, _now()))
                revision_number = 1
            elif existing["security_id"] != security_id:
                raise ValueError("a thesis cannot change its security")
            else:
                revision_number = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM thesis_revisions WHERE thesis_id = ?",
                        (thesis_id,),
                    ).fetchone()[0]
                )
            revision = ThesisRevision(
                revision_id=str(uuid4()),
                thesis_id=thesis_id,
                revision_number=revision_number,
                body=body.strip(),
                cited_snapshot_ids=tuple(cited_snapshot_ids),
                review_due_on=review_due_on.isoformat() if review_due_on else None,
                created_at=_now(),
            )
            connection.execute(
                "INSERT INTO thesis_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    revision.revision_id,
                    revision.thesis_id,
                    revision.revision_number,
                    revision.body,
                    __import__("json").dumps(revision.cited_snapshot_ids),
                    revision.review_due_on,
                    revision.created_at,
                ),
            )
            connection.execute(
                "INSERT INTO thesis_status_events VALUES (?, ?, 0, ?)",
                (str(uuid4()), revision.thesis_id, revision.created_at),
            )
            self._timeline(security_id, "thesis_revision", revision.revision_id, revision.created_at, "投资观点已修订")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return revision

    def add_note(self, *, security_id: str, body: str, commit: bool = True) -> str:
        if not body.strip():
            raise ValueError("note body cannot be empty")
        note_id, created_at = str(uuid4()), _now()
        self.vault.connection.execute(
            "INSERT INTO notes VALUES (?, ?, ?, ?)", (note_id, security_id, body.strip(), created_at)
        )
        self._timeline(security_id, "note", note_id, created_at, "已添加研究笔记")
        if commit:
            self.vault.connection.commit()
        return note_id

    def revise_note(self, note_id: str, *, security_id: str, body: str) -> str:
        if not body.strip():
            raise ValueError("笔记内容不能为空")
        note = self.vault.connection.execute(
            "SELECT security_id FROM notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        if note is None or str(note["security_id"]) != security_id:
            raise ValueError("笔记不存在或证券不匹配")
        revision_number = int(
            self.vault.connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM note_revisions WHERE note_id = ?",
                (note_id,),
            ).fetchone()[0]
        )
        revision_id, created_at = str(uuid4()), _now()
        self.vault.connection.execute(
            "INSERT INTO note_revisions VALUES (?, ?, ?, ?, 0, ?)",
            (revision_id, note_id, revision_number, body.strip(), created_at),
        )
        self._timeline(security_id, "note_revision", revision_id, created_at, "研究笔记已修订")
        self.vault.connection.commit()
        return revision_id

    def delete_note(self, note_id: str) -> None:
        current = self._current_note(note_id)
        if current is None:
            raise ValueError("笔记不存在或已删除")
        attachment_paths = [
            Path(str(row["storage_path"]))
            for row in self.vault.connection.execute(
                "SELECT storage_path FROM attachments WHERE note_id = ?", (note_id,)
            )
        ]
        connection = self.vault.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DELETE FROM ai_quick_notes WHERE accepted_note_id = ?", (note_id,))
            connection.execute("DELETE FROM attachments WHERE note_id = ?", (note_id,))
            connection.execute("DELETE FROM note_material_refs WHERE note_id = ?", (note_id,))
            connection.execute(
                """DELETE FROM timeline_events WHERE reference_id = ? OR reference_id IN
                (SELECT revision_id FROM note_revisions WHERE note_id = ?)""",
                (note_id, note_id),
            )
            connection.execute("DELETE FROM note_revisions WHERE note_id = ?", (note_id,))
            connection.execute("DELETE FROM notes WHERE note_id = ?", (note_id,))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        for path in attachment_paths:
            if connection.execute(
                "SELECT 1 FROM attachments WHERE storage_path = ? LIMIT 1", (str(path),)
            ).fetchone() is None:
                path.unlink(missing_ok=True)

    def _current_note(self, note_id: str):
        return self.vault.connection.execute(
            """SELECT n.note_id, n.security_id,
            COALESCE(r.body, n.body) AS body,
            COALESCE(r.revision_number, 0) AS revision_number,
            COALESCE(r.is_deleted, 0) AS is_deleted
            FROM notes n LEFT JOIN note_revisions r ON r.revision_id = (
                SELECT revision_id FROM note_revisions WHERE note_id = n.note_id
                ORDER BY revision_number DESC LIMIT 1
            ) WHERE n.note_id = ?""",
            (note_id,),
        ).fetchone()

    def delete_thesis(self, thesis_id: str) -> None:
        row = self.vault.connection.execute(
            "SELECT security_id FROM theses WHERE thesis_id = ?", (thesis_id,)
        ).fetchone()
        if row is None:
            raise ValueError("投资观点不存在")
        connection = self.vault.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """DELETE FROM timeline_events WHERE reference_id IN
                (SELECT revision_id FROM thesis_revisions WHERE thesis_id = ?)
                OR reference_id IN (SELECT event_id FROM thesis_status_events WHERE thesis_id = ?)""",
                (thesis_id, thesis_id),
            )
            connection.execute("DELETE FROM thesis_status_events WHERE thesis_id = ?", (thesis_id,))
            connection.execute("DELETE FROM thesis_revisions WHERE thesis_id = ?", (thesis_id,))
            connection.execute("DELETE FROM theses WHERE thesis_id = ?", (thesis_id,))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def add_material(
        self,
        *,
        security_id: str,
        material_type: str,
        title: str,
        published_at: date,
        source_name: str,
        source_url: str,
        excerpt: str = "",
    ) -> str:
        if not title.strip() or not source_name.strip() or not source_url.strip():
            raise ValueError("资料标题、来源和原文地址不能为空")
        material_id, created_at = str(uuid4()), _now()
        self.vault.connection.execute(
            "INSERT OR IGNORE INTO research_materials VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                material_id,
                security_id,
                material_type.strip() or "公开资料",
                title.strip(),
                published_at.isoformat(),
                source_name.strip(),
                source_url.strip(),
                excerpt.strip(),
                created_at,
            ),
        )
        stored = self.vault.connection.execute(
            "SELECT material_id FROM research_materials WHERE security_id = ? AND source_url = ?",
            (security_id, source_url.strip()),
        ).fetchone()
        if stored["material_id"] == material_id:
            self._timeline(
                security_id, "material", material_id, created_at, f"已归档{material_type.strip() or '公开资料'}"
            )
        self.vault.connection.commit()
        return str(stored["material_id"])

    def materials_synced(self, security_id: str, trade_date: date) -> bool:
        return self.vault.connection.execute(
            "SELECT 1 FROM material_sync_dates WHERE security_id = ? AND trade_date = ?",
            (security_id, trade_date.isoformat()),
        ).fetchone() is not None

    def mark_materials_synced(self, security_id: str, trade_date: date) -> None:
        self.vault.connection.execute(
            "INSERT OR IGNORE INTO material_sync_dates VALUES (?, ?, ?)",
            (security_id, trade_date.isoformat(), _now()),
        )
        self.vault.connection.commit()

    def add_note_from_material(
        self,
        *,
        security_id: str,
        material_id: str,
        quoted_text: str,
        body: str,
    ) -> str:
        material = self.vault.connection.execute(
            "SELECT security_id FROM research_materials WHERE material_id = ?", (material_id,)
        ).fetchone()
        if material is None:
            raise ValueError("引用的资料不存在")
        if material["security_id"] != security_id:
            raise ValueError("资料与笔记证券不匹配")
        if not quoted_text.strip():
            raise ValueError("摘录内容不能为空")
        note_id, created_at = str(uuid4()), _now()
        self.vault.connection.execute("BEGIN IMMEDIATE")
        try:
            self.vault.connection.execute(
                "INSERT INTO notes VALUES (?, ?, ?, ?)", (note_id, security_id, body.strip(), created_at)
            )
            self.vault.connection.execute(
                "INSERT INTO note_material_refs VALUES (?, ?, ?, ?)",
                (note_id, material_id, quoted_text.strip(), created_at),
            )
            self._timeline(security_id, "note", note_id, created_at, "已从公开资料摘录到笔记")
            self.vault.connection.commit()
        except BaseException:
            self.vault.connection.rollback()
            raise
        return note_id

    def workspace(self, security_id: str, *, limit: int = 50) -> dict[str, object]:
        limit = max(1, min(limit, 50))
        thesis = self.current_thesis(security_id)
        financial_limit = min(10, limit)
        recent_limit = limit - financial_limit
        material_rows = list(
            self.vault.connection.execute(
                "SELECT * FROM research_materials WHERE security_id = ? ORDER BY published_at DESC LIMIT ?",
                (security_id, recent_limit),
            )
        ) + list(
            self.vault.connection.execute(
                """SELECT * FROM research_materials
                WHERE security_id = ? AND material_type = '财务报告'
                ORDER BY published_at DESC LIMIT ?""",
                (security_id, financial_limit),
            )
        )
        materials = sorted(
            {row["material_id"]: dict(row) for row in material_rows}.values(),
            key=lambda row: (str(row["published_at"]), str(row["material_id"])),
            reverse=True,
        )
        notes = [
            {
                **dict(row),
                "body": row["current_body"],
                "updated_at": row["revision_created_at"] or row["created_at"],
            }
            for row in self.vault.connection.execute(
                """SELECT n.*, COALESCE(nr.body, n.body) AS current_body,
                COALESCE(nr.is_deleted, 0) AS is_deleted, nr.created_at AS revision_created_at,
                m.title AS source_title, m.source_url, mr.quoted_text
                FROM notes n
                LEFT JOIN note_revisions nr ON nr.revision_id = (
                    SELECT revision_id FROM note_revisions WHERE note_id = n.note_id
                    ORDER BY revision_number DESC LIMIT 1
                )
                LEFT JOIN note_material_refs mr ON mr.note_id = n.note_id
                LEFT JOIN research_materials m ON m.material_id = mr.material_id
                WHERE n.security_id = ? AND COALESCE(nr.is_deleted, 0) = 0
                ORDER BY COALESCE(nr.created_at, n.created_at) DESC LIMIT ?""",
                (security_id, limit),
            )
        ]
        financial_row = self.vault.connection.execute(
            "SELECT payload_json FROM financial_snapshots WHERE security_id = ? ORDER BY cutoff_date DESC LIMIT 1",
            (security_id,),
        ).fetchone()
        fund_row = self.vault.connection.execute(
            "SELECT payload_json FROM fund_snapshots WHERE security_id = ? ORDER BY cutoff_date DESC LIMIT 1",
            (security_id,),
        ).fetchone()
        return {
            "thesis": thesis.__dict__ if thesis else None,
            "materials": materials,
            "notes": notes,
            "financials": json.loads(str(financial_row["payload_json"])) if financial_row else None,
            "fund": json.loads(str(fund_row["payload_json"])) if fund_row else None,
            "timeline": self.timeline(security_id, limit=limit),
        }

    def add_attachment(self, note_id: str, source: Path, media_type: str) -> str:
        if not source.is_file():
            raise ValueError("attachment source must be a file")
        content = source.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()
        destination = self.vault_directory / "attachments" / sha256
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                file.write(content)
        attachment_id = str(uuid4())
        self.vault.connection.execute(
            "INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?, ?)",
            (attachment_id, note_id, sha256, source.name, media_type, str(destination), _now()),
        )
        self.vault.connection.commit()
        return attachment_id

    def timeline(self, security_id: str, *, limit: int = 50, cursor: str | None = None) -> dict[str, object]:
        limit = max(1, min(limit, 50))
        args: list[object] = [security_id]
        predicate = "security_id = ?"
        if cursor:
            occurred_at, event_id = cursor.split("|", 1)
            predicate += " AND (occurred_at < ? OR (occurred_at = ? AND event_id < ?))"
            args.extend((occurred_at, occurred_at, event_id))
        rows = self.vault.connection.execute(
            f"SELECT * FROM timeline_events WHERE {predicate} ORDER BY occurred_at DESC, event_id DESC LIMIT ?",
            (*args, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = f"{rows[-1]['occurred_at']}|{rows[-1]['event_id']}" if has_more and rows else None
        total = self.vault.connection.execute(
            "SELECT COUNT(*) FROM timeline_events WHERE security_id = ?", (security_id,)
        ).fetchone()[0]
        return {"items": [dict(row) for row in rows], "next_cursor": next_cursor, "total_count": total}

    def current_thesis(self, security_id: str) -> ThesisRevision | None:
        row = self.vault.connection.execute(
            """SELECT r.* FROM thesis_revisions r JOIN theses t ON t.thesis_id = r.thesis_id
            WHERE t.security_id = ? ORDER BY r.created_at DESC LIMIT 1""",
            (security_id,),
        ).fetchone()
        if row is None:
            return None
        status = self.vault.connection.execute(
            "SELECT is_deleted FROM thesis_status_events WHERE thesis_id = ? ORDER BY created_at DESC LIMIT 1",
            (row["thesis_id"],),
        ).fetchone()
        if status is not None and int(status["is_deleted"]):
            return None
        return ThesisRevision(
            revision_id=row["revision_id"],
            thesis_id=row["thesis_id"],
            revision_number=row["revision_number"],
            body=row["body"],
            cited_snapshot_ids=tuple(json.loads(row["cited_snapshot_ids_json"])),
            review_due_on=row["review_due_on"],
            created_at=row["created_at"],
        )

    def _timeline(
        self, security_id: str, event_type: str, reference_id: str, occurred_at: str, summary: str
    ) -> None:
        self.vault.connection.execute(
            "INSERT INTO timeline_events VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid4()), security_id, event_type, reference_id, occurred_at, summary),
        )
