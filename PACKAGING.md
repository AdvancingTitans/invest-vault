# 桌面端打包

“投资札记”把 FastAPI/SQLite 服务打包为目标平台原生的 PyInstaller sidecar。Tauri 为每次启动分配独立回环端口，并通过一次性令牌和 API 协议校验服务身份；退出应用时停止该服务。用户无需安装 Python 或其他源码项目。

## Local macOS build

```bash
cd apps/invest-vault
npm ci --prefix web
npm run build --prefix web
uv run --extra dev --with pyinstaller python scripts/build_sidecar.py
npx --yes @tauri-apps/cli@latest build --bundles app
codesign --force --deep --sign - \
  "src-tauri/target/release/bundle/macos/投资札记.app"
package_stage="$(mktemp -d)"
cp -R "src-tauri/target/release/bundle/macos/投资札记.app" "$package_stage/投资札记.app"
ln -s /Applications "$package_stage/Applications"
hdiutil create -volname "投资札记 0.3.34" \
  -srcfolder "$package_stage" \
  -ov -format UDZO \
  "src-tauri/target/release/bundle/dmg/Invest-Vault_0.3.34-local-aarch64.dmg"
```

The `hdiutil` step intentionally avoids Finder/AppleScript automation while retaining a visible Applications destination. It is suitable for an unsigned local build. Public distribution still requires Apple signing and notarization.

The PyInstaller sidecar also freezes `skills/stock-analysis/`, `skills/primary-evidence-reach/` and the scoped `skills/agent-reach/` fallback as read-only data. Verify the final sidecar, not only the source tree:

```bash
uv run --with pyinstaller pyi-archive_viewer -l \
  "src-tauri/target/release/bundle/macos/投资札记.app/Contents/MacOS/invest-vault-service" \
  | rg 'skills/(stock-analysis/(SKILL.md|config/lenses/buffett.json|scripts/lens_registry.py)|primary-evidence-reach/SKILL.md|agent-reach/(SKILL.md|references/search.md))'
```

## Windows build

Run `.github/workflows/invest-vault-desktop.yml` on a Windows runner. It builds the sidecar on Windows and then lets Tauri produce NSIS `.exe` and MSI installers. Do not copy a macOS sidecar into a Windows package or claim acceptance before install/launch/upgrade/restore has run on Windows.
