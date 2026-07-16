# 投资札记（Invest Vault）

一个面向中国个人投资者的本地持仓投资笔记应用。它在盘后整理持仓行情、公司公告、财务资料和基金信息，并把复盘笔记与资料摘录保存在本机。

- 本地优先：不要求账号，不默认上传持仓或笔记。
- 盘后定位：不提供盘中报价和手动行情刷新。
- 数据诚实：停牌、未披露、来源失败会显示为缺口，不补零、不伪造。
- 独立运行：安装包不依赖 `stock-analysis`、Python、Node.js 或 Rust。

> 本工具仅用于个人投资研究和记录，不构成任何投资建议。

## 下载

请从 GitHub 的 [Releases 页面](../../releases/latest)下载，不要从第三方网盘或聊天附件安装。

当前公开安装包：

| 平台 | 文件 | 状态 |
|---|---|---|
| macOS Apple Silicon（M1/M2/M3/M4） | `Invest-Vault_0.1.9_aarch64.dmg` | 已完成本机启动、退出和数据保存验证 |
| macOS Intel | 暂无 | 尚未构建原生 x86_64 sidecar |
| Windows | 暂无正式公开包 | 工作流可构建，但仍需 Windows 真机验收 |

### 校验下载文件

0.1.9 的 SHA-256：

```text
30eceb93b953d4a05f02ee8005c178799ddc3d1f25da0a637b25a27878898d2e  Invest-Vault_0.1.9_aarch64.dmg
```

在“终端”中执行：

```bash
shasum -a 256 ~/Downloads/Invest-Vault_0.1.9_aarch64.dmg
```

只有输出与上面的值完全一致时才继续安装。SHA-256 用于确认下载文件与本项目发布的文件一致，但它不能替代 Apple 公证或恶意软件检测。

## macOS 安装：没有 Apple 公证时如何打开

### 为什么会出现安全提示

当前安装包没有使用 Apple Developer ID，也没有提交 Apple 公证。包内仅使用 ad-hoc 签名维持应用组件完整性，因此从 GitHub 下载后，Gatekeeper 会把它视为“无法验证开发者”或“Apple 无法检查是否包含恶意软件”。GitHub 托管不会自动让应用获得 Apple 信任。

Apple 官方说明：[打开来自身份不明开发者的 Mac App](https://support.apple.com/guide/mac-help/open-a-mac-app-from-an-unknown-developer-mh40616/mac)、[安全地打开 Mac App](https://support.apple.com/102445)。

### 推荐安装步骤

以下步骤不需要开发者证书，也不需要用户在本机重新签名：

1. 从本仓库 Releases 下载 DMG，并按上一节核对 SHA-256。
2. 双击 DMG，把“投资札记.app”拖到“应用程序”文件夹。
3. 在“应用程序”中双击“投资札记”。macOS 会先阻止启动；关闭该提示，不要把来源不明或校验不一致的文件加入例外。
4. 打开“系统设置” → “隐私与安全性”，向下滚动到“安全性”。
5. 找到关于“投资札记”的提示，点击“仍要打开”（部分系统显示为“打开”）。该按钮通常只在刚刚尝试启动应用后约一小时内出现。
6. 输入本机登录密码或使用 Touch ID，再次点击“打开”。
7. 以后可以像普通应用一样双击启动，不需要重复操作。

如果看不到“仍要打开”，请回到第 3 步再次尝试启动，然后立刻返回“隐私与安全性”。旧版 macOS 可能允许在 Finder 中按住 Control 点击 App 后选择“打开”，但新版系统不保证保留这条路径，因此不要把它作为首选步骤。

### 无法绕过的情况

- 公司、学校或其他受管理的 Mac 可能由 MDM/管理员禁止“仍要打开”；这种设备上无法保证安装，需联系管理员。
- 当前 DMG 仅支持 Apple Silicon，Intel Mac 不能通过绕过 Gatekeeper 来解决架构不兼容。
- 如果 macOS 明确报告文件已损坏、签名结构无效，或 SHA-256 不一致，请删除文件并在 Releases 重新下载，不要继续绕过安全检查。

不建议执行 `sudo spctl --master-disable`、全局关闭 Gatekeeper，或复制来源不明的 `xattr` 命令。官方“仍要打开”只为这一款 App 建立例外，影响范围更小。

## 本地数据

macOS 数据目录：

```text
~/Library/Application Support/Invest Vault/
```

删除 App 不会自动删除这个目录。升级前可在“数据与备份”页创建完整备份。

应用会在启动、17:31 和重新回到前台时检查最近完整交易日。某个标的停牌、当日无交易或基金净值尚未披露时，会保留最后一个可核验日期，并显示本次归档覆盖率。

## 从源码运行

需要 Python 3.9+、[uv](https://docs.astral.sh/uv/) 和 Node.js 22+：

```bash
git clone https://github.com/AdvancingTitans/invest-vault.git
cd invest-vault

npm ci --prefix web
npm run build --prefix web
uv run invest-vault
```

然后访问 <http://127.0.0.1:8765>。源码运行只监听本机回环地址。

构建 macOS 桌面包还需要 Rust：

```bash
uv run --extra dev --with pyinstaller python scripts/build_sidecar.py
npx --yes @tauri-apps/cli@latest build --bundles app
codesign --force --deep --sign - "src-tauri/target/release/bundle/macos/投资札记.app"
```

完整打包命令见 [PACKAGING.md](PACKAGING.md)。

## 开发验证

```bash
uv run --extra dev pytest -q
uv run ruff check src tests
npm run build --prefix web
cargo test --manifest-path src-tauri/Cargo.toml
```

## 隐私与边界

- 持仓金额、买入日期、笔记、备份默认只保存在本机。
- 应用访问公开行情、基金资料、公司公告和港交所披露来源。
- 当前盈亏是“用户输入的人民币买入金额 × 买入日收盘价至最近盘后价的收益率”估算，不是券商账户市值。
- 应用不连接券商、不交易、不预测价格，也不提供买卖建议。

## License

[MIT](LICENSE)
