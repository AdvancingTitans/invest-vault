# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("uvicorn") + collect_submodules("stock_analysis")

a = Analysis(
    ["sidecar_entry.py"],
    pathex=["src"],
    binaries=[],
    datas=[
        ("web/dist", "web/dist"),
        ("skills/stock-analysis", "skills/stock-analysis"),
        ("skills/agent-reach", "skills/agent-reach"),
        ("skills/primary-evidence-reach", "skills/primary-evidence-reach"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="invest-vault-service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
