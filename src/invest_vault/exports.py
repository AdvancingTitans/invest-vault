"""Portable exports, full backups and explicit calculated portfolio facts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .ledger import Vault


def _xml(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def export_holdings_xlsx(vault: Vault) -> bytes:
    """Create one small, dependency-free XLSX workbook for user-owned holding rows."""

    asset_labels = {"a_share": "A股", "hk_stock": "港股", "us_stock": "美股", "fund": "基金"}
    rows: list[list[object]] = [["证券代码", "类型", "买入金额（人民币）", "买入日期"]]
    for holding in vault.holding_entries():
        rows.append(
            [
                holding["security_id"].split(":")[-2],
                asset_labels[holding["asset_type"]],
                float(holding["invested_amount_cny"]),
                holding["bought_on"],
            ]
        )

    def cell(reference: str, value: object, style: int = 0) -> str:
        if isinstance(value, (int, float)):
            return f'<c r="{reference}" s="{style}"><v>{value}</v></c>'
        return f'<c r="{reference}" t="inlineStr" s="{style}"><is><t>{_xml(value)}</t></is></c>'

    sheet_rows = []
    for row_number, values in enumerate(rows, 1):
        cells = "".join(
            cell(f"{chr(65 + column)}{row_number}", value, 1 if row_number == 1 else (2 if column == 2 else 0))
            for column, value in enumerate(values)
        )
        sheet_rows.append(f'<row r="{row_number}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        '<cols><col min="1" max="1" width="16" customWidth="1"/><col min="2" max="2" width="12" customWidth="1"/>'
        '<col min="3" max="3" width="22" customWidth="1"/><col min="4" max="4" width="16" customWidth="1"/></cols>'
        f'<sheetData>{"".join(sheet_rows)}</sheetData><autoFilter ref="A1:D{len(rows)}"/>'
        '</worksheet>'
    )
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="持仓" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        "xl/styles.xml": '<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Arial"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF2859C5"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": sheet,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as workbook:
        for path, content in files.items():
            workbook.writestr(path, content)
    return output.getvalue()


def export_markdown(vault: Vault, destination: Path, *, data_cutoff: str) -> Path:
    lines = ["# 投资札记导出", "", f"数据截止：`{data_cutoff}`", "", "## 账本", ""]
    lines.extend(
        f"- `{row['occurred_at']}` {row['action']} {row['security_id']}" for row in vault.export_json()
    )
    lines.extend(["", "## 研究笔记", ""])
    for row in vault.connection.execute(
        """SELECT n.security_id, COALESCE(nr.created_at, n.created_at) created_at,
        COALESCE(nr.body, n.body) body, m.title, m.source_url, r.quoted_text
        FROM notes n
        LEFT JOIN note_revisions nr ON nr.revision_id = (
            SELECT revision_id FROM note_revisions WHERE note_id = n.note_id
            ORDER BY revision_number DESC LIMIT 1
        )
        LEFT JOIN note_material_refs r ON r.note_id = n.note_id
        LEFT JOIN research_materials m ON m.material_id = r.material_id
        WHERE COALESCE(nr.is_deleted, 0) = 0 ORDER BY COALESCE(nr.created_at, n.created_at)"""
    ):
        lines.append(f"### {row['security_id']} · {row['created_at']}")
        if row["title"]:
            lines.extend(["", f"资料：[{row['title']}]({row['source_url']})", "", f"> {row['quoted_text']}"])
        lines.extend(["", row["body"], ""])
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def export_snapshot_index(vault: Vault, destination: Path, *, data_cutoff: str) -> Path:
    rows = [
        dict(row) for row in vault.connection.execute("SELECT * FROM evidence_snapshots ORDER BY observed_at")
    ]
    destination.write_text(
        json.dumps({"data_cutoff": data_cutoff, "snapshots": rows}, indent=2), encoding="utf-8"
    )
    return destination


def export_positions_csv(vault: Vault, account_id: str, destination: Path) -> Path:
    rows = [item.__dict__ for item in vault.project_positions(account_id)]
    with destination.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file, fieldnames=("security_id", "quantity", "valuation_status", "missing_fields")
        )
        writer.writeheader()
        for row in rows:
            row["missing_fields"] = ",".join(row["missing_fields"])
            writer.writerow(row)
    return destination


def create_backup(vault_directory: Path, destination: Path) -> Path:
    """Write a full, checksum-indexed archive from files already owned by this vault."""
    vault_directory, destination = Path(vault_directory), Path(destination)
    files = [path for path in vault_directory.rglob("*") if path.is_file() and path != destination]
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {
            str(path.relative_to(vault_directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files
        },
    }
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(vault_directory))
        archive.writestr("backup-manifest.json", json.dumps(manifest, sort_keys=True))
    return destination


def restore_backup(source: Path, destination_directory: Path) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        with zipfile.ZipFile(source) as archive:
            archive.extractall(temporary)
        root = Path(temporary)
        manifest = json.loads((root / "backup-manifest.json").read_text(encoding="utf-8"))
        for relative, expected in manifest["files"].items():
            content = (root / relative).read_bytes()
            if hashlib.sha256(content).hexdigest() != expected:
                raise ValueError(f"backup checksum mismatch: {relative}")
        if destination_directory.exists() and any(destination_directory.iterdir()):
            raise ValueError("restore destination must be empty")
        destination_directory.mkdir(parents=True, exist_ok=True)
        for relative in manifest["files"]:
            target = destination_directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / relative, target)
