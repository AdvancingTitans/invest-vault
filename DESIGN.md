# Invest Vault design system

## Theme

Archive Noir / 证据夜航. A low-glare near-black canvas, graphite research surfaces and one cool archive-blue action color form a calm after-hours evidence workbench. The distinctive element remains the compact “盘后归档条”: completed trade date, archive time and automatic-update state form a daily notebook index rather than a market-dashboard decoration.

## Colors

| Token | Value | Use |
|---|---|---|
| `--canvas` | `#090B0F` | App background |
| `--nav` | `#0C0E13` | Persistent navigation |
| `--surface` | `#11141A` | Bounded work areas |
| `--ink` | `#F2F4F7` | Primary text |
| `--muted` | `#A6AEBA` | Secondary information |
| `--action` | `#7C9CFF` | Selection, focus and evidence indexing |
| `--success` | `#45C68B` | Available / completed and A-share down state |
| `--warning` | `#E5B65A` | Partial / review due |
| `--danger` | `#FF7369` | Unavailable / failed and A-share up state |

## Typography

System UI sans: `ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`. Data, identifiers, dates and amounts use `ui-monospace, SFMono-Regular, Menlo, monospace`. The product scale is compact: 12, 13, 14, 16, 20 and 24px.

## Layout

Desktop uses a 176px near-black navigation rail and a flexible evidence workspace. Daily archive provenance sits in a compact in-flow rail with one leading archive-blue index mark instead of a separate task column, keeping the primary reading path wide and stable. Security evidence and the research assistant stack below 1184px; below 960px navigation becomes a horizontally scrollable top row. Tables retain their columns through horizontal scrolling instead of becoming card lists.

## Components

Use 1px graphite separators, compact 8px radius only on bounded controls, 40px minimum interactive targets, native buttons and semantic headings. Fixed slots use a shared title row, provenance rail, bounded content and an explicit “View all” path. Cards rely on value steps and hairlines rather than glow or ambient shadow. Error and empty states state the provider/field/date and a concrete next action.

Metadata labels such as security codes, markets, document types and news regions are transparent archive-blue text rather than light filled pills. Semantic states alone may use the restrained red/green/yellow palette. Spacing follows a compact 4/8/12/16/24px scale. Nested metric groups use one flat separator grid; do not create a card inside every card.

大盘议事厅、投研大师与投研委员会共享“研究聊天室”语法：15 位命名投资框架和协调员、证据研究员、投资经理、风险与组合经理、通用模式使用统一的编辑插画头像，用户角色保留克制的文字头像。圆桌插画只用于独立大盘议事厅标题栏、其中的“投研委员会 · 六席会审”席位和证券资料的投研委员会标题；单专家选择、专家发言及协调/取证/编辑过程继续使用原角色头像。头像不使用真人照片，也不把分析框架冒充真人身份。大盘议事厅与今日复盘同为一级工作台，把时段、主持席和主动作收拢为一条控制带，并保留新对话/清空；A股概览、全球概览只承载可核验市场数据，不嵌入聊天室。投研大师把普通模式、委员会和框架选择放在同一层级。空状态仍是可行动的研究入口：一条范围说明、三步证据流程与有边界的示例问题。Provider 设置继续将 Codex 账户状态与加密 BYOK 凭据分开。

The normal research assistant uses the same evidence-entry density as the committee: one role-specific explanation, three visible evidence scopes and three bounded starter questions. Today card actions are scope-specific: quote/P&L refresh and holding-material refresh never share a loading label. Holding cards retain the close-to-close P/L basis but use their right footer for latest price/NAV and daily change. A vacant Today slot is one dashed `＋ 添加关注持仓` control; it lists only ledger holdings not already shown, while removal never deletes ledger data. Market report notes use a first-class title; the body remains the report and its cited context. Market notes use one compact three-segment `盘前 / 盘中 / 盘后` control backed by explicit stored session metadata. Never infer a note's market session from its creation time.

Page navigation does not trigger provider refresh: cached projections render first, one automatic refresh runs at startup, and later updates are explicit. A股概览 owns A-share indices, 龙虎榜, industry flow, news, pulse, six fixed verifiable themes and current-month representative earnings; 全球概览 owns balanced 港/日/韩/美 representative indices, liquid large-cap gainers, transparent theme-basket performance, the current month's remaining representative earnings and news. Their visible rows remain scoped projections, while the independent 大盘议事厅 deliberately combines all available A/HK/JP/KR/US, public, calculated and local-holding evidence required by each expert framework. Global news reserves at least one item per market before filling by recency; A-share news requires explicit A-share market terms, excludes non-A-share issuer stories and may widen beyond 24 hours to fill the six visible rows while preserving real publication timestamps and newest-first order. An A-share refresh with fewer than six strict matches is rejected atomically so it cannot replace the most recent complete archive with a partial list. An index that cannot be verified, including TOPIX while its free endpoint returns 403, is omitted instead of replaced with a stale or proxy value; zero or undisclosed index volume renders as unavailable. Dated market modules may fall back only to an earlier verified trading-day payload, retain that payload's real date and label it `最近可用交易日`; a fallback must never relabel stale data as today. `赚钱效应与上涨主线` and `下跌风险` always show the payload's actual date. Compact breadth bars and mini trend charts remain inside existing fixed evidence slots and always state whether they describe the displayed sample or a full market. At 760px and below the permanent sidebar collapses to a 56px horizontally scrollable navigation rail so content begins above the fold.

Every AI research surface treats the application Vault ledger as the complete holdings authority. The full name, code, type, buy date and RMB buy amount enter the model context without per-evidence or aggregate character truncation; an external `stock-analysis` profile, environment profile or remembered portfolio is forbidden. A central output guard rejects any claimed local holding outside the ledger whitelist. Framework inputs retain full upstream financial history, facts, fund data, microstructure and execution evidence; reached original issuer, exchange, regulator, fund or index sources return as structured citations. Derived quantity, FX, pairwise correlation and market beta are admitted only when their public inputs and calculation method are complete and labelled.

A-share directional semantics apply to the complete comparable metric, including secondary board-count or risk-kind labels: upside/inflow is `--danger` red and downside/outflow is `--success` green. Security identifiers and company names stay neutral so direction is not confused with identity.

## Motion

Motion intensity is 2/10. A drawer or local status change may transition with opacity/transform over 180ms; content is always visible without animation. Reduced motion makes state changes instant. No counters, staggered lists, scroll-triggered reveals or auto-rotating content.
